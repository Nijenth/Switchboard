$ErrorActionPreference = 'Stop'

function Invoke-Docker {
    & docker --context desktop-linux @args
    if ($LASTEXITCODE -ne 0) { throw "Docker exited $LASTEXITCODE" }
}

$repo = Split-Path -Parent $PSScriptRoot
$container = 'switchboard-core-check-' + [Guid]::NewGuid().ToString('N')
$passwordVars = @(
    'SWITCHBOARD_DB_BOOTSTRAP_PASSWORD', 'SWITCHBOARD_DB_OWNER_PASSWORD',
    'SWITCHBOARD_CORE_PASSWORD', 'SWITCHBOARD_CONFIG_LOADER_PASSWORD',
    'SWITCHBOARD_N8N_MOCK_PASSWORD', 'SWITCHBOARD_REPORTING_PASSWORD', 'SWITCHBOARD_HEALTH_TOKEN',
    'SWITCHBOARD_CORE_TEST_MARKER'
)
$testVars = $passwordVars + @('SWITCHBOARD_DB_HOST', 'SWITCHBOARD_DB_PORT', 'SWITCHBOARD_DB_NAME', 'SWITCHBOARD_CORE_TEST_GATEWAY')
$previous = @{}
foreach ($name in $testVars) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }

Push-Location -LiteralPath $repo
try {
    foreach ($name in $passwordVars) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [Environment]::SetEnvironmentVariable($name, [BitConverter]::ToString($bytes).Replace('-', ''))
    }
    $python = Join-Path $repo '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Create the workspace Python 3.12 virtualenv first' }

    $compose = @('compose', '--env-file', '.env.example', '-f', 'docker-compose.yml',
        '-f', 'docker-compose.core-test.yml', '--profile', 'test')
    $env:SWITCHBOARD_CORE_TEST_GATEWAY = Invoke-Docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
    $null = Invoke-Docker @compose run --detach --no-deps --pull never --name $container `
        --publish '127.0.0.1::5432' db-check --core-integration

    try {
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $logs = Invoke-Docker logs $container
            if ($logs -contains 'Core integration database ready') {
                $ready = $true
                break
            }
            if ((Invoke-Docker inspect --format '{{.State.Running}}' $container) -ne 'true') {
                throw 'Disposable integration database exited during startup'
            }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw 'Disposable core integration database did not become ready' }
        $published = Invoke-Docker port $container 5432/tcp
        if ($published -notmatch '^127\.0\.0\.1:(\d+)$') { throw 'Unexpected database port binding' }
        $env:SWITCHBOARD_DB_HOST = '127.0.0.1'
        $env:SWITCHBOARD_DB_PORT = $Matches[1]
        $env:SWITCHBOARD_DB_NAME = 'switchboard_test'

        Push-Location -LiteralPath (Join-Path $repo 'services/core')
        try {
            & $python -m pytest -q -rs --tb=short
            if ($LASTEXITCODE -ne 0) { throw "pytest exited $LASTEXITCODE" }
        } finally { Pop-Location }
    } finally {
        $null = Invoke-Docker rm --force --volumes $container
    }
} finally {
    foreach ($name in $testVars) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
    Pop-Location
}
