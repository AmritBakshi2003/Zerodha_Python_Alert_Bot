#!/usr/bin/env bash
# Supervised live run of kite-alerts on Linux/macOS. No Docker, no extra tools.
#
#   * restarts after a crash with exponential backoff, up to --max-restarts;
#   * never restarts on a configuration (exit 2) or authentication (exit 3)
#     failure - those need a human: fix .env, or refresh the daily token;
#   * passes --duration so the bot shuts itself down cleanly shortly after
#     SESSION_CLOSE instead of being killed mid-write;
#   * stays out of the way on a weekend or after the close, and waits if it is
#     started shortly before 09:00 IST.
#
# Supervisor activity is appended to <LOG_DIR>/supervisor-YYYYMMDD.log.
# Exit codes: 0 clean stop, 1 gave up after the restart limit, 2 configuration
# error, 3 authentication error.
#
#   ./scripts/unix/run_bot.sh
#   ./scripts/unix/run_bot.sh --force --once
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

ENV_FILE=".env"
STOP_AT=""
MAX_RESTARTS=5
BACKOFF_BASE=5
BACKOFF_MAX=300
MAX_WAIT_FOR_OPEN=120
ONCE=0
FORCE=0

usage() {
  cat <<'USAGE'
usage: run_bot.sh [options]

  --env-file PATH                 environment file handed to the bot (default: .env)
  --stop-at HH:MM                 IST time to stop (default: SESSION_CLOSE + 5 min)
  --max-restarts N                consecutive crash restarts before giving up (default: 5)
  --backoff-base-seconds N        first restart delay, doubling per attempt (default: 5)
  --backoff-max-seconds N         cap for the restart delay (default: 300)
  --max-wait-for-open-minutes N   how long to wait for 09:00 IST (default: 120)
  --once                          single attempt, no restarts
  --force                         skip the trading-day and market-hours gate
                                  (supervised trials only)
  -h, --help                      show this message
USAGE
}

need_value() {
  [ $# -ge 2 ] || {
    printf 'error: %s needs a value\n' "$1" >&2
    exit 64
  }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) need_value "$@"; ENV_FILE=$2; shift 2 ;;
    --stop-at) need_value "$@"; STOP_AT=$2; shift 2 ;;
    --max-restarts) need_value "$@"; MAX_RESTARTS=$2; shift 2 ;;
    --backoff-base-seconds) need_value "$@"; BACKOFF_BASE=$2; shift 2 ;;
    --backoff-max-seconds) need_value "$@"; BACKOFF_MAX=$2; shift 2 ;;
    --max-wait-for-open-minutes) need_value "$@"; MAX_WAIT_FOR_OPEN=$2; shift 2 ;;
    --once) ONCE=1; shift ;;
    --force) FORCE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# India has never observed DST, so Asia/Kolkata is exact. Using TZ keeps this
# correct on a host clock set to another local timezone.
ist_stamp() { TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S'; }
ist_clock() { TZ=Asia/Kolkata date '+%H:%M'; }
ist_weekday() { TZ=Asia/Kolkata date '+%u'; }
ist_ymd() { TZ=Asia/Kolkata date '+%Y%m%d'; }

# Seconds since midnight from HH:MM or HH:MM:SS (portable: no GNU date -d).
seconds_of_day() {
  local value=$1 hours minutes seconds
  hours=${value%%:*}
  value=${value#*:}
  minutes=${value%%:*}
  seconds=${value#*:}
  if [ "${minutes}" = "${value}" ]; then
    seconds=0
  fi
  printf '%s\n' $((10#${hours} * 3600 + 10#${minutes} * 60 + 10#${seconds}))
}

read_env_value() {
  local key=$1 fallback=$2 file=$3 line name value
  if [ ! -f "${file}" ]; then
    printf '%s\n' "${fallback}"
    return 0
  fi
  while IFS= read -r line || [ -n "${line}" ]; do
    line=${line%$'\r'}
    case "${line}" in '' | '#'*) continue ;; esac
    case "${line}" in *=*) ;; *) continue ;; esac
    name=${line%%=*}
    name=${name//[[:space:]]/}
    [ "${name}" = "${key}" ] || continue
    value=${line#*=}
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    value=${value#\"}
    value=${value%\"}
    value=${value#\'}
    value=${value%\'}
    printf '%s\n' "${value}"
    return 0
  done <"${file}"
  printf '%s\n' "${fallback}"
}

LOG_DIR_SETTING=$(read_env_value LOG_DIR 'logs' "${REPO_ROOT}/${ENV_FILE}")
case "${LOG_DIR_SETTING}" in
  /*) LOG_DIR=${LOG_DIR_SETTING} ;;
  *) LOG_DIR="${REPO_ROOT}/${LOG_DIR_SETTING}" ;;
esac
mkdir -p "${LOG_DIR}"
SUPERVISOR_LOG="${LOG_DIR}/supervisor-$(ist_ymd).log"

write_log() {
  local level=$1
  shift
  local line
  line="$(ist_stamp) [${level}] $*"
  printf '%s\n' "${line}"
  printf '%s\n' "${line}" >>"${SUPERVISOR_LOG}"
}

# Reject a bad time here: the arithmetic below would otherwise abort with a raw
# "value too great for base" from the shell instead of a message an operator can
# act on.
validate_hhmm() {
  local value=$1 label=$2 hours rest minutes seconds
  if ! printf '%s' "${value}" | grep -Eq '^[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$'; then
    write_log ERROR "${label} must be HH:MM or HH:MM:SS (got '${value}')."
    exit 2
  fi
  hours=${value%%:*}
  rest=${value#*:}
  minutes=${rest%%:*}
  if [ "${minutes}" = "${rest}" ]; then seconds=0; else seconds=${rest#*:}; fi
  if [ "${hours}" -gt 23 ] || [ "${minutes}" -gt 59 ] || [ "${seconds}" -gt 59 ]; then
    write_log ERROR "${label} is out of range (got '${value}')."
    exit 2
  fi
}

if [ -n "${STOP_AT}" ]; then
  validate_hhmm "${STOP_AT}" '--stop-at'
  STOP_SECONDS=$(seconds_of_day "${STOP_AT}")
else
  CLOSE_VALUE=$(read_env_value SESSION_CLOSE '15:30' "${REPO_ROOT}/${ENV_FILE}")
  validate_hhmm "${CLOSE_VALUE}" 'SESSION_CLOSE'
  STOP_SECONDS=$(($(seconds_of_day "${CLOSE_VALUE}") + 300))
fi
if [ "${STOP_SECONDS}" -gt 86399 ]; then
  STOP_SECONDS=86399
fi
printf -v STOP_HHMM '%02d:%02d' $((STOP_SECONDS / 3600)) $(((STOP_SECONDS % 3600) / 60))

write_log INFO "supervisor starting (stop at ${STOP_HHMM} IST, repo ${REPO_ROOT})"

if [ ! -x "${VENV_PYTHON}" ]; then
  write_log ERROR 'no virtual environment found; run ./scripts/unix/setup.sh first.'
  exit 1
fi

if [ "${FORCE}" -eq 0 ]; then
  WEEKDAY=$(ist_weekday)
  if [ "${WEEKDAY}" -ge 6 ]; then
    write_log INFO "today is a weekend (ISO weekday ${WEEKDAY}); NSE is closed, nothing to do."
    exit 0
  fi
  NOW_SECONDS=$(seconds_of_day "$(TZ=Asia/Kolkata date '+%H:%M:%S')")
  if [ "${NOW_SECONDS}" -ge "${STOP_SECONDS}" ]; then
    write_log INFO "it is $(ist_clock) IST, past ${STOP_HHMM}; the market is closed."
    exit 0
  fi
  OPEN_SECONDS=32400 # 09:00 IST
  if [ "${NOW_SECONDS}" -lt "${OPEN_SECONDS}" ]; then
    WAIT_SECONDS=$((OPEN_SECONDS - NOW_SECONDS))
    if [ "${WAIT_SECONDS}" -gt $((MAX_WAIT_FOR_OPEN * 60)) ]; then
      write_log WARN "it is $(ist_clock) IST and the session opens at 09:15; waiting would take $((WAIT_SECONDS / 60)) min (limit ${MAX_WAIT_FOR_OPEN}). Start the bot closer to the open."
      exit 0
    fi
    write_log INFO "before 09:00 IST; waiting $((WAIT_SECONDS / 60)) min for the session to open."
    while [ "$(seconds_of_day "$(TZ=Asia/Kolkata date '+%H:%M:%S')")" -lt "${OPEN_SECONDS}" ]; do
      sleep 30
    done
  fi
  write_log INFO 'session window reached; starting the bot.'
fi

if [ "${ONCE}" -eq 1 ]; then
  MAX_ATTEMPTS=1
else
  MAX_ATTEMPTS=$((MAX_RESTARTS + 1))
fi

cd "${REPO_ROOT}"
ATTEMPT=0
while :; do
  NOW_SECONDS=$(seconds_of_day "$(TZ=Asia/Kolkata date '+%H:%M:%S')")
  REMAINING=$((STOP_SECONDS - NOW_SECONDS))
  if [ "${FORCE}" -eq 0 ] && [ "${REMAINING}" -le 0 ]; then
    write_log INFO "past ${STOP_HHMM} IST; stopping."
    exit 0
  fi

  RUN_ARGS=(-m kite_alerts.cli.main --env-file "${ENV_FILE}" run)
  if [ "${REMAINING}" -gt 0 ]; then
    # Letting the bot enforce the deadline itself means it exits through its own
    # shutdown path (SQLite closed, "stopped" written to the audit log) instead
    # of being terminated at market close.
    RUN_ARGS+=(--duration "${REMAINING}")
  fi

  ATTEMPT=$((ATTEMPT + 1))
  write_log INFO "starting the bot (attempt ${ATTEMPT} of ${MAX_ATTEMPTS})"

  set +e
  "${VENV_PYTHON}" "${RUN_ARGS[@]}"
  CODE=$?
  set -e
  write_log INFO "the bot exited with code ${CODE}"

  if [ "${CODE}" -eq 0 ]; then
    write_log INFO 'clean exit (market close, duration reached or Ctrl-C); supervisor done.'
    exit 0
  fi
  if [ "${CODE}" -eq 2 ]; then
    write_log ERROR 'configuration error (exit 2); not restarting. Fix .env (KITE_API_KEY, KITE_API_SECRET, SYMBOLS) and start again.'
    exit 2
  fi
  if [ "${CODE}" -eq 3 ]; then
    write_log ERROR 'authentication error (exit 3); not restarting. Run ./scripts/unix/login.sh, then start the bot again.'
    exit 3
  fi

  if [ "${ATTEMPT}" -ge "${MAX_ATTEMPTS}" ]; then
    write_log ERROR "giving up after ${ATTEMPT} attempt(s); the last exit code was ${CODE}."
    exit 1
  fi

  DELAY=${BACKOFF_BASE}
  STEP=1
  while [ "${STEP}" -lt "${ATTEMPT}" ]; do
    DELAY=$((DELAY * 2))
    STEP=$((STEP + 1))
    if [ "${DELAY}" -ge "${BACKOFF_MAX}" ]; then
      DELAY=${BACKOFF_MAX}
      break
    fi
  done
  if [ "${DELAY}" -lt 0 ]; then
    DELAY=0
  fi
  write_log WARN "crash (exit ${CODE}); restarting in ${DELAY}s."
  if [ "${DELAY}" -gt 0 ]; then
    sleep "${DELAY}"
  fi
done
