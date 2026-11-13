#!/usr/bin/env bash
# Source this from a disposable test runner.
for name in SWITCHBOARD_DB_BOOTSTRAP_PASSWORD SWITCHBOARD_DB_OWNER_PASSWORD \
    SWITCHBOARD_CORE_PASSWORD SWITCHBOARD_CONFIG_LOADER_PASSWORD \
    SWITCHBOARD_N8N_MOCK_PASSWORD SWITCHBOARD_REPORTING_PASSWORD; do
    value=$(openssl rand -hex 32) || return 1
    export "$name=$value"
done
unset name value
