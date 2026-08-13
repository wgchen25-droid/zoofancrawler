#!/usr/bin/env bash
# Start the local dashboard and control services against one persistent database.

set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
CONFIG="$ROOT_DIR/config/zoos.yaml"
DB="$ROOT_DIR/data/zoofan.db"
HOST="127.0.0.1"
DASHBOARD_PORT=${DASHBOARD_PORT:-8000}
CONTROL_PORT=${CONTROL_PORT:-8001}
DASHBOARD_URL="http://$HOST:$DASHBOARD_PORT"
CONTROL_URL="http://$HOST:$CONTROL_PORT"
DASHBOARD_PID=""
CONTROL_PID=""
CLEANED_UP=0

die() {
    echo "start.sh: $*" >&2
    exit 1
}

warn() {
    echo "start.sh: warning: $*" >&2
}

valid_port() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$((10#$1))" -ge 1 ] && [ "$((10#$1))" -le 65535 ]
}

port_available() {
    "$PYTHON_BIN" - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    # Do not enable SO_REUSEADDR: this is a preflight for an active listener.
    sock.bind(("127.0.0.1", port))
except OSError as exc:
    raise SystemExit("cannot bind 127.0.0.1:{0}: {1}".format(port, exc))
finally:
    sock.close()
PY
}

cleanup() {
    status=$?
    if [ "$CLEANED_UP" -eq 1 ]; then
        return "$status"
    fi
    CLEANED_UP=1

    if [ -n "$DASHBOARD_PID" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        kill "$DASHBOARD_PID" 2>/dev/null || true
    fi
    if [ -n "$CONTROL_PID" ] && kill -0 "$CONTROL_PID" 2>/dev/null; then
        kill "$CONTROL_PID" 2>/dev/null || true
    fi
    if [ -n "$DASHBOARD_PID" ]; then
        wait "$DASHBOARD_PID" 2>/dev/null || true
    fi
    if [ -n "$CONTROL_PID" ]; then
        wait "$CONTROL_PID" 2>/dev/null || true
    fi
    return "$status"
}

on_interrupt() {
    exit "$1"
}

is_running() {
    kill -0 "$1" 2>/dev/null
}

http_ready() {
    "$PYTHON_BIN" - "$1" <<'PY'
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

try:
    response = urlopen(sys.argv[1], timeout=1)
    status = getattr(response, "status", response.getcode())
    raise SystemExit(0 if 200 <= status < 400 else 1)
except (HTTPError, URLError, OSError):
    raise SystemExit(1)
PY
}

wait_for_service() {
    service_name=$1
    service_url=$2
    service_pid=$3
    attempts=0
    while [ "$attempts" -lt 20 ]; do
        if ! is_running "$service_pid"; then
            die "$service_name exited before becoming healthy"
        fi
        if http_ready "$service_url"; then
            # A listener could appear between the preflight bind and the CLI
            # process binding its port. Do not accept that other service's 200.
            sleep 1
            if is_running "$service_pid"; then
                return 0
            fi
            die "$service_name exited before becoming healthy"
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    die "$service_name did not become healthy at $service_url within 20 seconds"
}

if [ -n "${PYTHON_BIN:-}" ]; then
    : # An explicit interpreter is useful for virtual environments and tests.
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3)
else
    die "Python 3.9+ was not found. Create .venv and install dependencies with: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
fi

if ! "$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9+ is required")

try:
    import flask  # noqa: F401
    import yaml  # noqa: F401
except ImportError as exc:
    raise SystemExit("missing Python dependency: {0}".format(exc.name))
PY
then
    die "Python or dependencies are unavailable. Install the declared dependencies with: $PYTHON_BIN -m pip install -r $ROOT_DIR/requirements.txt"
fi

valid_port "$DASHBOARD_PORT" || die "DASHBOARD_PORT must be an integer from 1 to 65535"
valid_port "$CONTROL_PORT" || die "CONTROL_PORT must be an integer from 1 to 65535"
[ "$DASHBOARD_PORT" != "$CONTROL_PORT" ] || die "DASHBOARD_PORT and CONTROL_PORT must be different"
[ -f "$CONFIG" ] || die "configuration file is missing: $CONFIG"
[ -f "$ROOT_DIR/cli.py" ] || die "CLI entry point is missing: $ROOT_DIR/cli.py"

port_available "$DASHBOARD_PORT" || die "DASHBOARD_PORT $DASHBOARD_PORT is unavailable on 127.0.0.1"
port_available "$CONTROL_PORT" || die "CONTROL_PORT $CONTROL_PORT is unavailable on 127.0.0.1"

mkdir -p "$ROOT_DIR/data" || die "cannot create data directory: $ROOT_DIR/data"

trap cleanup EXIT
trap 'on_interrupt 130' INT
trap 'on_interrupt 143' TERM

echo "Starting dashboard at $DASHBOARD_URL (database: $DB)"
"$PYTHON_BIN" "$ROOT_DIR/cli.py" --config "$CONFIG" dashboard \
    --db "$DB" --host "$HOST" --port "$DASHBOARD_PORT" --control-url "$CONTROL_URL" &
DASHBOARD_PID=$!
wait_for_service "dashboard" "$DASHBOARD_URL/" "$DASHBOARD_PID"

echo "Starting control service at $CONTROL_URL (database: $DB)"
"$PYTHON_BIN" "$ROOT_DIR/cli.py" --config "$CONFIG" control \
    --db "$DB" --host "$HOST" --port "$CONTROL_PORT" --dashboard-url "$DASHBOARD_URL" &
CONTROL_PID=$!
wait_for_service "control service" "$CONTROL_URL/" "$CONTROL_PID"

echo "Dashboard: $DASHBOARD_URL (pid $DASHBOARD_PID)"
echo "Control:   $CONTROL_URL (pid $CONTROL_PID)"
echo "Press Ctrl-C to stop both services."

if [ "${ZOOFAN_NO_OPEN:-}" != "1" ]; then
    case "$(uname -s)" in
        Darwin)
            open "$CONTROL_URL" >/dev/null 2>&1 || warn "could not open $CONTROL_URL"
            ;;
        Linux)
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$CONTROL_URL" >/dev/null 2>&1 || warn "could not open $CONTROL_URL"
            else
                warn "xdg-open is unavailable; open $CONTROL_URL manually"
            fi
            ;;
        *) warn "open $CONTROL_URL in a browser" ;;
    esac
fi

while :; do
    if ! is_running "$DASHBOARD_PID"; then
        wait "$DASHBOARD_PID" || true
        die "dashboard exited"
    fi
    if ! is_running "$CONTROL_PID"; then
        wait "$CONTROL_PID" || true
        die "control service exited"
    fi
    sleep 1
done
