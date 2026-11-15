#!/usr/bin/env bash
# Source this from the repository root after scripts/test_db_env.sh.
# core_stack_start <offline: true|false> creates a fresh disposable database
# container (and, when offline is true, the offline AI gateway), exports the
# SWITCHBOARD_* settings core needs, and installs traps that remove everything
# when the calling script exits.

core_stack_cleanup() {
    if [[ $core_stack_offline == true ]]; then
        if ! (cd -- "$core_stack_repo" && "${core_stack_ai_compose[@]}" down); then
            printf 'Warning: cleanup failed for Docker Compose project %s. Containers may remain.\n' \
                "$SWITCHBOARD_CORE_TEST_CONTAINER" >&2
        fi
    fi
    docker rm --force --volumes "$SWITCHBOARD_CORE_TEST_CONTAINER" >/dev/null
}

core_stack_start() {
    core_stack_offline=$1
    core_stack_repo="$PWD"
    unset SWITCHBOARD_OFFLINE_GATEWAY_URL

    export SWITCHBOARD_CORE_TEST_MARKER SWITCHBOARD_HEALTH_TOKEN SWITCHBOARD_SLACK_SIGNING_SECRET
    SWITCHBOARD_CORE_TEST_MARKER=$(openssl rand -hex 32)
    SWITCHBOARD_HEALTH_TOKEN=$(openssl rand -hex 32)
    SWITCHBOARD_SLACK_SIGNING_SECRET=$(openssl rand -hex 32)
    export SWITCHBOARD_SLACK_APP_ID=A000FIXTURE

    export SWITCHBOARD_CORE_TEST_GATEWAY
    SWITCHBOARD_CORE_TEST_GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
    local container
    container="switchboard-core-check-$(openssl rand -hex 12)"
    export SWITCHBOARD_CORE_TEST_CONTAINER="$container"
    docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.core-test.yml \
        --profile test run --detach --no-deps --pull never --name "$container" \
        --publish '127.0.0.1::5432' db-check --core-integration >/dev/null
    core_stack_ai_compose=(docker compose --env-file .env.example -p "$container" --profile offline-ai)
    trap core_stack_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    local ready=false attempt logs
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

    local published
    published=$(docker port "$container" 5432/tcp)
    if [[ ! $published =~ ^127\.0\.0\.1:([0-9]+)$ ]]; then
        printf 'Unexpected database port binding\n' >&2
        exit 1
    fi
    export SWITCHBOARD_DB_HOST=127.0.0.1 SWITCHBOARD_DB_PORT="${BASH_REMATCH[1]}"
    export SWITCHBOARD_DB_NAME=switchboard_test

    if [[ $core_stack_offline == true ]]; then
        export SWITCHBOARD_OFFLINE_MASTER_KEY
        SWITCHBOARD_OFFLINE_MASTER_KEY="sk-$(openssl rand -hex 32)"
        "${core_stack_ai_compose[@]}" up -d --pull never offline-relay
        local gateway
        gateway=$("${core_stack_ai_compose[@]}" port offline-relay 4000)
        if [[ ! $gateway =~ ^127\.0\.0\.1:[0-9]+$ ]]; then
            printf 'Unexpected offline gateway port binding: %s\n' "$gateway" >&2
            "${core_stack_ai_compose[@]}" logs --tail 30 litellm
            exit 1
        fi
        export SWITCHBOARD_OFFLINE_GATEWAY_URL="http://$gateway"
        for ((attempt = 0; attempt < 60; attempt++)); do
            curl -fsS "$SWITCHBOARD_OFFLINE_GATEWAY_URL/health/liveliness" >/dev/null 2>&1 && break
            sleep 1
        done
        if ! curl -fsS "$SWITCHBOARD_OFFLINE_GATEWAY_URL/health/liveliness" >/dev/null; then
            "${core_stack_ai_compose[@]}" logs --tail 35 litellm
            exit 1
        fi
    fi
}
