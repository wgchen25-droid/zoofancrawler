"""Offline contract tests for the repository's Makefile quality gates."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
import sys

import pytest

from zoofan.models import CrawlRun, Zoo
from zoofan.storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
REQUIRED_TARGETS = (
    "test",
    "lint",
    "typecheck",
    "validate-config",
    "fixture-tests",
    "config-tests",
    "integration-tests",
    "golden-tests",
    "quality-tests",
    "build-acceptance-report",
    "offline-gates",
    "endgoal",
)

TEST_GROUP_VARIABLES = (
    "FIXTURE_TESTS",
    "CONFIG_TESTS",
    "INTEGRATION_TESTS",
    "GOLDEN_TESTS",
    "QUALITY_TESTS",
)


def _dry_run(target: str) -> str:
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _makefile_variable(name: str) -> set[Path]:
    contents = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{name} \?= (.+)$", contents, flags=re.MULTILINE)
    assert match, f"missing explicit test taxonomy variable: {name}"
    return {ROOT / item for item in match.group(1).split()}


def _usable_database(path: Path) -> None:
    """Create the smallest database accepted by the offline report gate."""

    with SQLiteStorage(path) as storage:
        storage.upsert_zoo(Zoo(id="quality-zoo", slug="quality-zoo", name="Quality Zoo"))
        run = storage.start_crawl_run(CrawlRun(batch_id="quality-batch"))
        assert run.id is not None
        storage.finish_crawl_run(run.id)


def test_makefile_declares_all_quality_targets():
    contents = MAKEFILE.read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):", contents, flags=re.MULTILINE))
    assert set(REQUIRED_TARGETS).issubset(declared)


def test_test_taxonomy_covers_each_test_module_once():
    """Keep named groups complete without recursively invoking Make."""

    expected = set((ROOT / "tests").glob("test_*.py"))
    grouped = [_makefile_variable(name) for name in TEST_GROUP_VARIABLES]
    flattened = [path for group in grouped for path in group]
    assert set(flattened) == expected
    assert len(flattened) == len(set(flattened)), "a test module belongs to multiple groups"
    assert all(path.is_file() for path in flattened)


def test_quality_targets_have_executable_dry_run_commands():
    for target in REQUIRED_TARGETS:
        output = _dry_run(target)
        assert output.strip(), target


def test_offline_gate_is_direct_and_requires_explicit_database():
    output = _dry_run("offline-gates")
    assert 'test -n ""' in output
    assert re.search(r"(?m)^\s*(?:\$\(MAKE\)|make)\b", output) is None
    assert "offline-gates requires OFFLINE_DB" in output
    assert '--db ""' in output
    assert "cli.py endgoal" not in output


@pytest.mark.parametrize("report_as_make_override", (False, True))
def test_offline_gate_path_overrides_do_not_leak_into_nested_quality_checks(
    tmp_path: Path, report_as_make_override: bool
):
    """The aggregate gate keeps nested Makefile contract checks at defaults.

    The test runs only the two quality-contract checks that inspect nested
    dry-run output, avoiding recursive invocation of this aggregate target.
    REPORT_DIR is always temporary so the OFFLINE_DB-only invocation does not
    create a repository-root report; the second case additionally passes it
    as an explicit Make override, matching the documented command shape.
    """

    database = tmp_path / "offline.db"
    report_dir = tmp_path / "reports"
    _usable_database(database)

    quality_tests = (
        f"{sys.executable} -m pytest tests/test_quality_gates.py -q "
        "-k 'test_offline_gate_is_direct_and_requires_explicit_database or "
        "test_report_gate_is_read_only_and_does_not_run_crawl'"
    )
    command = [
        "make",
        "--no-print-directory",
        "offline-gates",
        f"OFFLINE_DB={database}",
    ]
    environment = os.environ.copy()
    # Keep the real validation/report path while limiting this regression to
    # the nested-contract behavior rather than recursively running the suite.
    environment["PYTEST"] = quality_tests
    environment["REPORT_DIR"] = str(report_dir)
    if report_as_make_override:
        command.append(f"REPORT_DIR={report_dir}")

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (report_dir / "latest" / "run-summary.json").is_file()


def test_declared_dev_tools_match_quality_commands():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "dev = [" in pyproject
    assert '"ruff>=' in pyproject and '"mypy>=' in pyproject
    assert '"types-PyYAML>=' in pyproject
    assert re.search(r"^ruff>=", requirements, flags=re.MULTILINE)
    assert re.search(r"^mypy>=", requirements, flags=re.MULTILINE)
    assert re.search(r"^types-PyYAML>=", requirements, flags=re.MULTILINE)
    assert "$(PYTHON) -m ruff" in MAKEFILE.read_text(encoding="utf-8")
    assert "$(PYTHON) -m mypy" in MAKEFILE.read_text(encoding="utf-8")


def test_mypy_does_not_hide_missing_imports():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "ignore_missing_imports = true" not in pyproject


def test_local_apscheduler_stub_is_configured_and_complete():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'mypy_path = ["stubs"]' in pyproject
    stub_root = ROOT / "stubs" / "apscheduler"
    assert (stub_root / "__init__.pyi").is_file()
    assert (stub_root / "py.typed").is_file()
    blocking = stub_root / "schedulers" / "blocking.pyi"
    assert blocking.is_file()
    contents = blocking.read_text(encoding="utf-8")
    for signature in ("class BlockingScheduler", "def add_job", "def start", "def shutdown", "running"):
        assert signature in contents


def test_offline_gates_do_not_declare_live_network_commands():
    network_tokens = ("curl", "wget", "httpx.get", "requests.get", "playwright install")
    for target in REQUIRED_TARGETS[:-1]:
        output = _dry_run(target).lower()
        assert not any(token in output for token in network_tokens), target


def test_report_gate_is_read_only_and_does_not_run_crawl():
    output = _dry_run("build-acceptance-report")
    assert "build-acceptance-report" in output
    assert "--db \"data/zoofan.db\"" in output
    assert "--output-dir \"reports\"" in output
    assert re.search(r"cli\.py[^\n]*\bcrawl\b", output) is None
    assert " endgoal" not in output
    assert "test -s \"data/zoofan.db\"" in output
    assert "crawl evidence" in output


def test_live_acceptance_is_explicitly_isolated_to_endgoal():
    output = _dry_run("endgoal")
    assert 'cli.py --config "config/zoos.yaml" endgoal' in output
