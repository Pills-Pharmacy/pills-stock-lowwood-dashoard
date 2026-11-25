"""Guards for the handover: the files a fresh clone depends on must stay consistent."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def lock_versions() -> dict[str, tuple[int, ...]]:
    return {
        m.group(1).lower(): version(m.group(2))
        for m in re.finditer(
            r"^([A-Za-z0-9_.-]+)==([\w.]+)", read("requirements.lock"), re.MULTILINE
        )
    }


def floors(name: str) -> dict[str, tuple[int, ...]]:
    found = {}
    for line in read(name).splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)>=([\d.]+)", line.strip())
        if match:
            found[match.group(1).lower()] = version(match.group(2))
    return found


def test_lock_satisfies_every_floor_in_both_requirements_files():
    pinned = lock_versions()
    for name in ("requirements.txt", "requirements-dev.txt"):
        for package, floor in floors(name).items():
            assert package in pinned, f"{package} missing from requirements.lock"
            assert pinned[package] >= floor, f"{package} {pinned[package]} below floor {floor}"


def test_requirements_floors_match_the_plan():
    assert set(floors("requirements.txt")) == {
        "streamlit",
        "pandas",
        "openpyxl",
        "rapidfuzz",
        "pyyaml",
        "jinja2",
    }
    assert {"pytest", "pytest-cov", "ruff"} <= set(floors("requirements-dev.txt"))


def test_coverage_gate_is_at_least_80_percent():
    config = tomllib.loads(read("pyproject.toml"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert int(re.search(r"--cov-fail-under=(\d+)", addopts).group(1)) >= 80
    assert config["tool"]["ruff"]["line-length"] == 100


def test_streamlit_config_keeps_uploads_safe_and_private():
    config = tomllib.loads(read(".streamlit/config.toml"))
    assert config["server"]["maxUploadSize"] == 100
    assert config["server"]["enableXsrfProtection"] is True
    assert config["browser"]["gatherUsageStats"] is False


def test_gitignore_keeps_real_data_out_but_lets_fixtures_in():
    entries = set(read(".gitignore").split())
    assert {".venv/", "*.csv", "!tests/fixtures/*.csv", "*.xlsx", "outputs/"} <= entries
    assert "confirmed_matches.csv" in entries


def test_readme_covers_setup_for_both_platforms_the_fallback_and_open_questions():
    text = read("README.md")
    for needed in (
        "Activate.ps1",
        "source .venv/bin/activate",
        "streamlit run app.py",
        "pytest",
        "ruff check .",
        "If the app is unavailable",
        "latest HTML report",
        "re-run the audit",
        "OPEN_QUESTIONS.md",
    ):
        assert needed in text, needed


def test_readme_names_every_module_in_the_layout():
    text = read("README.md")
    modules = (
        "app",
        "pipeline",
        "config",
        "schema",
        "load",
        "clean",
        "match",
        "analyse",
        "activity",
        "usage",
        "export",
    )
    for module in modules:
        assert f"`{module}.py`" in text, module


def test_open_questions_seed_is_still_present():
    if not (ROOT / "OPEN_QUESTIONS.md").exists():
        pytest.skip("OPEN_QUESTIONS.md is kept out of the hosting repo")
    text = read("OPEN_QUESTIONS.md")
    for seed in (
        "Which branch is each robot in",
        "one file or one per branch",
        "Do both robots export in the same format",
        "share a product code",
        "expiry dates",
        "separate robot dispensing",
        "thresholds and usage window",
        "Hosting decision",
    ):
        assert seed.lower() in text.lower(), seed
