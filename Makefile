PYTHON ?= python3
CONFIG ?= config/zoos.yaml
DB ?= data/zoofan.db
REPORT_DIR ?= reports

PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff
MYPY ?= $(PYTHON) -m mypy

# The quality-contract tests intentionally invoke ``make -n`` to inspect the
# default target commands.  Keep the aggregate's child pytest process from
# inheriting caller Make overrides (or MAKEFLAGS), otherwise those nested
# inspections stop being hermetic when offline-gates is called with paths.
OFFLINE_GATE_TEST_ENV = env -u MAKEFLAGS -u MFLAGS -u MAKEOVERRIDES -u PYTHON -u CONFIG -u DB -u REPORT_DIR -u OFFLINE_DB -u PYTEST -u RUFF -u MYPY -u FIXTURE_TESTS -u CONFIG_TESTS -u INTEGRATION_TESTS -u GOLDEN_TESTS -u QUALITY_TESTS

# The named test groups are deterministic and use only local fixtures/fakes.
# Keep these lists explicit: the quality-gate contract tests that every
# ``tests/test_*.py`` module belongs to exactly one taxonomy group.
FIXTURE_TESTS ?= tests/test_fetcher.py tests/test_live_markup_regressions.py tests/test_normalization.py tests/test_parsers.py tests/test_phase0_numeric_dates.py tests/test_phase0_sources.py tests/test_url_policy.py
CONFIG_TESTS ?= tests/test_config.py
INTEGRATION_TESTS ?= tests/test_cli.py tests/test_control.py tests/test_console_api.py tests/test_console_crawler_events.py tests/test_console_events_storage.py tests/test_console_services.py tests/test_console_ui.py tests/test_crawler.py tests/test_dashboard.py tests/test_failure_isolation.py tests/test_live_smoke.py tests/test_module_cli.py tests/test_start_script.py tests/test_storage.py
GOLDEN_TESTS ?= tests/test_endgoal.py tests/test_reporting.py
QUALITY_TESTS ?= tests/test_packaging.py tests/test_quality_gates.py

.PHONY: test lint typecheck validate-config fixture-tests config-tests integration-tests golden-tests quality-tests build-acceptance-report offline-gates endgoal

test:
	$(PYTEST) -q

lint:
	$(RUFF) check cli.py zoofan tests

typecheck:
	$(MYPY) cli.py zoofan

validate-config:
	$(PYTHON) cli.py --config "$(CONFIG)" validate-config --json

fixture-tests:
	$(PYTEST) -q $(FIXTURE_TESTS)

config-tests:
	$(PYTEST) -q $(CONFIG_TESTS)

integration-tests:
	$(PYTEST) -q $(INTEGRATION_TESTS)

golden-tests:
	$(PYTEST) -q $(GOLDEN_TESTS)

quality-tests:
	$(PYTEST) -q $(QUALITY_TESTS)

# This command reads the existing DB through the reporting layer.  It never
# invokes crawl/endgoal and therefore does not make network requests or
# replace the database.  The reporting API writes into REPORT_DIR/latest and
# atomically replaces only its own generated files.
build-acceptance-report:
	@test -n "$(DB)" || { echo "build-acceptance-report requires DB=/path/to/usable.sqlite" >&2; exit 2; }
	@test -s "$(DB)" || { echo "build-acceptance-report requires a non-empty SQLite DB: $(DB)" >&2; exit 2; }
	@$(PYTHON) -c 'import sqlite3,sys; p=sys.argv[1]; c=sqlite3.connect(p); names={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type=\x27table\x27")}; missing={"zoos","articles","crawl_runs"}-names; zoo_count=c.execute("SELECT COUNT(*) FROM zoos").fetchone()[0] if "zoos" in names else 0; run_count=c.execute("SELECT COUNT(*) FROM crawl_runs").fetchone()[0] if "crawl_runs" in names else 0; c.close(); assert not missing and zoo_count > 0 and run_count > 0, "missing required tables or crawl evidence: " + ",".join(sorted(missing))' "$(DB)"
	$(PYTHON) cli.py --config "$(CONFIG)" build-acceptance-report --db "$(DB)" --output-dir "$(REPORT_DIR)"

# Run every offline gate directly, without invoking another Make target.  A
# caller must provide OFFLINE_DB explicitly; the ordinary DB default may be a
# zero-byte placeholder and is intentionally not accepted here.
offline-gates:
	@test -n "$(OFFLINE_DB)" || { echo "offline-gates requires OFFLINE_DB=/path/to/usable.sqlite" >&2; exit 2; }
	@test -s "$(OFFLINE_DB)" || { echo "offline-gates requires a non-empty SQLite DB: $(OFFLINE_DB)" >&2; exit 2; }
	@$(PYTHON) -c 'import sqlite3,sys; p=sys.argv[1]; c=sqlite3.connect(p); names={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type=\x27table\x27")}; missing={"zoos","articles","crawl_runs"}-names; zoo_count=c.execute("SELECT COUNT(*) FROM zoos").fetchone()[0] if "zoos" in names else 0; run_count=c.execute("SELECT COUNT(*) FROM crawl_runs").fetchone()[0] if "crawl_runs" in names else 0; c.close(); assert not missing and zoo_count > 0 and run_count > 0, "missing required tables or crawl evidence: " + ",".join(sorted(missing))' "$(OFFLINE_DB)"
	$(PYTHON) cli.py --config "$(CONFIG)" validate-config --json
	$(RUFF) check cli.py zoofan tests
	$(MYPY) cli.py zoofan
	$(OFFLINE_GATE_TEST_ENV) $(PYTEST) -q
	$(PYTHON) cli.py --config "$(CONFIG)" build-acceptance-report --db "$(OFFLINE_DB)" --output-dir "$(REPORT_DIR)"

# Deliberately retained as the sole live, destructive acceptance workflow.
endgoal:
	$(PYTHON) cli.py --config "$(CONFIG)" endgoal
