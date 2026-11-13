#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/test_db_env.sh

export SWITCHBOARD_CORE_TEST_MARKER SWITCHBOARD_HEALTH_TOKEN SWITCHBOARD_SLACK_SIGNING_SECRET
SWITCHBOARD_CORE_TEST_MARKER=$(openssl rand -hex 32)
SWITCHBOARD_HEALTH_TOKEN=$(openssl rand -hex 32)
SWITCHBOARD_SLACK_SIGNING_SECRET=$(openssl rand -hex 32)
export SWITCHBOARD_SLACK_APP_ID=A000FIXTURE
unset SWITCHBOARD_TEST_N8N

export SWITCHBOARD_CORE_TEST_GATEWAY
SWITCHBOARD_CORE_TEST_GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
container="switchboard-core-check-$(openssl rand -hex 12)"
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.core-test.yml \
    --profile test run --detach --no-deps --pull never --name "$container" \
    --publish '127.0.0.1::5432' db-check --core-integration >/dev/null
trap 'docker rm --force --volumes "$container" >/dev/null' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ready=false
for ((attempt = 0; attempt < 60; attempt++)); do
    logs=$(docker logs "$container" 2>&1)
    if [[ $logs == *'Core integration database ready'* ]]; then
        ready=true
        break
    fi
    [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]] || break
    sleep 1
done
if [[ $ready != true ]]; then
    printf 'Disposable core integration database did not become ready\n' >&2
    exit 1
fi

published=$(docker port "$container" 5432/tcp)
if [[ ! $published =~ ^127\.0\.0\.1:([0-9]+)$ ]]; then
    printf 'Unexpected database port binding\n' >&2
    exit 1
fi
export SWITCHBOARD_DB_HOST=127.0.0.1 SWITCHBOARD_DB_PORT="${BASH_REMATCH[1]}"
export SWITCHBOARD_DB_NAME=switchboard_test

cd services/core
python -m pytest -q -rs --tb=short
