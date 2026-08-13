"""Offline contract checks for the local two-service launcher."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "start.sh"


def test_start_script_has_portable_syntax_and_required_command_contract():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    content = SCRIPT.read_text(encoding="utf-8")
    assert 'ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)' in content
    assert '--config "$CONFIG" dashboard' in content
    assert '--db "$DB" --host "$HOST" --port "$DASHBOARD_PORT" --control-url "$CONTROL_URL"' in content
    assert '--config "$CONFIG" control' in content
    assert '--db "$DB" --host "$HOST" --port "$CONTROL_PORT" --dashboard-url "$DASHBOARD_URL"' in content
    assert 'mkdir -p "$ROOT_DIR/data"' in content
    assert "scheduler" not in content
    assert "crawl all" not in content
    assert "wait -n" not in content
    assert "readlink -f" not in content


def test_control_start_failure_stops_already_started_dashboard(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config").mkdir()
    (workspace / "config" / "zoos.yaml").write_text("zoos: []\n", encoding="utf-8")
    shutil.copy2(SCRIPT, workspace / "start.sh")
    (workspace / "cli.py").write_text("# fake CLI; PYTHON_BIN handles this file\n", encoding="utf-8")

    marker = tmp_path / "dashboard-stopped"
    argument_log = tmp_path / "arguments.log"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        textwrap.dedent(
            """\
                #!/usr/bin/env bash
                if [ "$1" = "-" ]; then
                    program=$(cat)
                    case "$program" in
                        *"import socket"*) printf '%s\\n' "$program" | python3 - "$2"; exit $? ;;
                    esac
                    exit 0
                fi
            printf '%s\\n' "$*" >> "$START_LOG"
            case " $* " in
                *" dashboard "*)
                    trap 'touch "$DASHBOARD_STOPPED"; exit 0' INT TERM
                    while :; do sleep 1; done
                    ;;
                *" control "*) exit 1 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PYTHON_BIN": str(fake_python),
            "START_LOG": str(argument_log),
            "DASHBOARD_STOPPED": str(marker),
            "ZOOFAN_NO_OPEN": "1",
            "DASHBOARD_PORT": "18100",
            "CONTROL_PORT": "18101",
        }
    )
    result = subprocess.run(
        ["bash", str(workspace / "start.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
    )

    assert result.returncode != 0
    assert "control service exited" in result.stderr
    assert marker.exists(), result.stderr
    arguments = argument_log.read_text(encoding="utf-8")
    assert "--config " + str(workspace / "config" / "zoos.yaml") in arguments
    assert " dashboard --db " + str(workspace / "data" / "zoofan.db") in arguments
    assert "--host 127.0.0.1 --port 18100" in arguments
    assert " control --db " + str(workspace / "data" / "zoofan.db") in arguments
    assert "--host 127.0.0.1 --port 18101" in arguments
    assert "--dashboard-url http://127.0.0.1:18100" in arguments


def test_port_preflight_stops_before_either_cli_process_starts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config").mkdir()
    (workspace / "config" / "zoos.yaml").write_text("zoos: []\n", encoding="utf-8")
    shutil.copy2(SCRIPT, workspace / "start.sh")
    (workspace / "cli.py").write_text("# fake CLI; PYTHON_BIN handles this file\n", encoding="utf-8")

    argument_log = tmp_path / "arguments.log"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = "-" ]; then
                program=$(cat)
                case "$program" in
                    *"import socket"*) printf '%s\\n' "$program" | python3 - "$2"; exit $? ;;
                esac
                exit 0
            fi
            printf '%s\\n' "$*" >> "$START_LOG"
            exit 0
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    occupied_port = listener.getsockname()[1]
    control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    control_socket.bind(("127.0.0.1", 0))
    control_port = control_socket.getsockname()[1]
    control_socket.close()
    try:
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHON_BIN": str(fake_python),
                "START_LOG": str(argument_log),
                "ZOOFAN_NO_OPEN": "1",
                "DASHBOARD_PORT": str(occupied_port),
                "CONTROL_PORT": str(control_port),
            }
        )
        result = subprocess.run(
            ["bash", str(workspace / "start.sh")],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=8,
        )
    finally:
        listener.close()

    assert result.returncode != 0
    assert "DASHBOARD_PORT {0} is unavailable on 127.0.0.1".format(occupied_port) in result.stderr
    assert not argument_log.exists()
