#!/usr/bin/env bash
# Create the virtual environment, install kite-alerts and validate the config.
#
# Idempotent: an existing .venv is reused and an existing .env is never
# overwritten, so this is safe to re-run after every update.
#
#   ./scripts/unix/setup.sh
#   PYTHON_BIN=python3.12 ./scripts/unix/setup.sh
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
SKIP_INSTALL=0

usage() {
  cat <<'USAGE'
usage: setup.sh [--skip-install] [--help]

  --skip-install   do not touch the venv or run pip; only create .env if missing
                   and run config-check
  --help           show this message

Environment:
  PYTHON_BIN       interpreter used to build the venv (default: python3, then python)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-install) SKIP_INSTALL=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

find_python() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    command -v "${PYTHON_BIN}" || die "PYTHON_BIN='${PYTHON_BIN}' was not found on PATH."
    return 0
  fi
  local candidate
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done
  die "no python3 on PATH. Install Python 3.10+ (on Debian/Ubuntu: sudo apt install python3 python3-venv)."
}

printf 'kite-alerts setup - %s\n\n' "${REPO_ROOT}"

if [ -x "${VENV_PYTHON}" ]; then
  printf '  venv        : already present, reusing\n'
else
  BASE_PYTHON=$(find_python)
  VERSION=$("${BASE_PYTHON}" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null | head -n 1 || true)
  # Never compare an unvalidated string: a wrapper or a broken interpreter would
  # otherwise fail later with an arithmetic error instead of this message.
  if ! printf '%s' "${VERSION}" | grep -Eq '^[0-9]+\.[0-9]+$'; then
    die "could not read a Python version from ${BASE_PYTHON} (got '${VERSION}')."
  fi
  MAJOR=${VERSION%%.*}
  MINOR=${VERSION#*.}
  if [ "${MAJOR}" -lt 3 ] || { [ "${MAJOR}" -eq 3 ] && [ "${MINOR}" -lt 10 ]; }; then
    die "Python ${VERSION} was found at ${BASE_PYTHON}, but kite-alerts needs 3.10 or newer."
  fi
  printf '  python      : %s (Python %s)\n' "${BASE_PYTHON}" "${VERSION}"
  printf '  venv        : creating %s\n' "${VENV_DIR}"
  "${BASE_PYTHON}" -m venv "${VENV_DIR}" || die "could not create the virtual environment (on Debian/Ubuntu install python3-venv)."
fi

if [ "${SKIP_INSTALL}" -eq 1 ]; then
  printf '  install     : skipped (--skip-install)\n'
else
  printf '  install     : python -m pip install -e .\n'
  "${VENV_PYTHON}" -m pip install --disable-pip-version-check -e "${REPO_ROOT}" ||
    die "pip install failed."
fi

ENV_PATH="${REPO_ROOT}/.env"
if [ -f "${ENV_PATH}" ]; then
  printf '  .env        : already present, left untouched\n'
else
  cp "${REPO_ROOT}/.env.example" "${ENV_PATH}"
  chmod 600 "${ENV_PATH}" 2>/dev/null || true
  printf '  .env        : created from .env.example - open it and fill in the values\n'
fi

printf '  config-check:\n'
cd "${REPO_ROOT}"
"${VENV_PYTHON}" -m kite_alerts.cli.main --env-file "${ENV_PATH}" config-check ||
  die "config-check failed."

cat <<'NEXT'

Next steps
  1. Edit .env: set KITE_API_KEY, KITE_API_SECRET and SYMBOLS.
  2. ./scripts/unix/login.sh     - once per trading day, refreshes the token
  3. ./scripts/unix/run_bot.sh   - during market hours

Full walkthrough and troubleshooting: deploy/README.md
NEXT
