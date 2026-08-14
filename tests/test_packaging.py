"""Offline checks for runtime resources declared in the package metadata."""

import fnmatch
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "zoofan"


def _declared_package_data_patterns(package="zoofan"):
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(
        r"(?ms)^\[tool\.setuptools\.package-data\](.*?)(?=^\[|\Z)",
        metadata,
    )
    assert section is not None, "pyproject.toml must declare package data"
    match = re.search(
        rf"(?ms)^{re.escape(package)}\s*=\s*\[(.*?)\]",
        section.group(1),
    )
    assert match is not None, f"pyproject.toml must declare {package} package data"
    return re.findall(r'"([^"]+)"', match.group(1))


def _declared_package_finder_includes():
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(
        r"(?ms)^\[tool\.setuptools\.packages\.find\](.*?)(?=^\[|\Z)",
        metadata,
    )
    assert section is not None, "pyproject.toml must configure setuptools package discovery"
    match = re.search(r"(?m)^include\s*=\s*\[(.*?)\]", section.group(1))
    assert match is not None, "setuptools package discovery must declare include patterns"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_dashboard_runtime_resources_are_explicitly_declared():
    patterns = _declared_package_data_patterns("zoofan")
    resources = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for directory in (PACKAGE_ROOT / "templates", PACKAGE_ROOT / "static")
        for path in directory.rglob("*")
        if path.is_file()
    ]

    assert resources, "dashboard package resources should exist"
    for resource in resources:
        assert any(fnmatch.fnmatch(resource, pattern) for pattern in patterns), (
            f"runtime resource {resource!r} is not covered by package data"
        )


def test_authoritative_config_is_packaged_without_a_source_copy():
    source = ROOT / "config" / "zoos.yaml"
    assert source.is_file(), "the repository config source must remain present"
    assert (source.parent / "__init__.py").is_file(), "config must be a package for wheel installation"

    includes = _declared_package_finder_includes()
    assert any(fnmatch.fnmatch("config", pattern) for pattern in includes), (
        "setuptools package discovery must include the config package"
    )

    patterns = _declared_package_data_patterns("config")
    assert any(fnmatch.fnmatch("zoos.yaml", pattern) for pattern in patterns), (
        "config package data must include the authoritative zoos.yaml"
    )

    source_copies = sorted(ROOT.glob("config/zoos.yaml"))
    assert source_copies == [source], "config/zoos.yaml must have exactly one source copy"
