#!/usr/bin/env bash
# Refresh the daily Kite access token.
#
# Kite access tokens expire every day at roughly 06:00 IST, so run this once per
# trading day before starting the bot. It prints the Kite login URL, asks for the
# request_token from the redirect, and writes KITE_ACCESS_TOKEN back into .env
# (every other line is preserved).
#
#   ./scripts/unix/login.sh
#   ./scripts/unix/login.sh --env-file /etc/kite-alerts/kite-alerts.env
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
ENV_FILE=".env"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file)
      [ $# -ge 2 ] || {
        printf 'error: --env-file needs a value\n' >&2
        exit 64
      }
      ENV_FILE=$2
      shift 2
      ;;
    -h | --help)
      printf 'usage: login.sh [--env-file PATH]\n'
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 64
      ;;
  esac
done

if [ ! -x "${VENV_PYTHON}" ]; then
  printf 'error: no virtual environment found. Run ./scripts/unix/setup.sh first.\n' >&2
  exit 1
fi

cd "${REPO_ROOT}"
set +e
"${VENV_PYTHON}" -m kite_alerts.cli.main --env-file "${ENV_FILE}" login
CODE=$?
set -e

if [ "${CODE}" -eq 0 ]; then
  printf 'Token refreshed. You can now start ./scripts/unix/run_bot.sh\n'
else
  printf 'Login failed with exit code %s. The message above says why.\n' "${CODE}" >&2
fi
exit "${CODE}"
