#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/test_db_env.sh
source scripts/core_stack.sh
offline=false
n8n=false
case "${1:-}" in
    '') ;;
    --offline-ai) offline=true ;;
    --offline-n8n) offline=true; n8n=true ;;
    *) printf 'Usage: test_core.sh [--offline-ai|--offline-n8n]\n' >&2; exit 2 ;;
esac

unset SWITCHBOARD_TEST_N8N
if [[ $n8n == true ]]; then
    export SWITCHBOARD_TEST_N8N=1
fi

core_stack_start "$offline"

(
    cd services/core
    python -m pytest -q -rs --tb=short
    if [[ $offline == true ]]; then
        python -m app.eval_runner --tenant 10000000-0000-0000-0000-000000000001 --version 3
    fi
)
