#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PORT="${PORT:-8088}"
export DASHBOARD_DATA_PATH="${DASHBOARD_DATA_PATH:-/home/const/subnet66/tau/workspace/validate/netuid-66/dashboard_data.json}"
export SWEBENCH_BENCHMARK_ROOT="${SWEBENCH_BENCHMARK_ROOT:-/home/const/subnet66/tau/workspace/validate/netuid-66/benchmarks/swebench-verified}"
export SUBMISSIONS_API_UPSTREAM="${SUBMISSIONS_API_UPSTREAM:-http://127.0.0.1:8066/api/submissions}"
export TAU_SRC="${TAU_SRC:-/home/const/subnet66/tau/src}"
exec /home/const/subnet66/.venv/bin/python3 serve.py
