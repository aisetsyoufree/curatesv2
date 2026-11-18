"""#115: the worker's configuration names every path, creates nothing, and fails closed."""

import json
import os
from pathlib import Path

import pytest

from cu_rates.db import upgrade_database
from cu_rates.fetch_policy import TermsLedger
from cu_rates.snapshot import Denylist
from cu_rates.worker_config import (
    SETTINGS,
    WorkerConfigError,
    build_registry,
    check_config,
    main,
    paths_from_environment,
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    database = tmp_path / "data" / "operational.sqlite"
    database.parent.mkdir()
    upgrade_database(database)
    TermsLedger(tmp_path / "private" / "terms.json").initialize()
    Denylist(tmp_path / "independent" / "denylist.json").initialize()
    Denylist(tmp_path / "automatic" / "denylist.json").initialize()
    for name in ("state", "artifacts"):
        (tmp_path / name).mkdir(mode=0o700)
    return {
        "OPERATIONAL_DB": str(database),
        "TERMS_LEDGER_PATH": str(tmp_path / "private" / "terms.json"),
        "DENYLIST_PATH": str(tmp_path / "independent" / "denylist.json"),
        "AUTOMATIC_DENYLIST_PATH": str(tmp_path / "automatic" / "denylist.json"),
        "WORKER_STATE_DIR": str(tmp_path / "state"),
        "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
    }


def _tree(root: Path) -> list[str]:
    """Everything under root except SQLite's own WAL side files, which opening any WAL-mode
    database (even read-only) may create next to it; they are not configuration."""
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if not p.name.endswith(("-shm", "-wal"))
    )


def test_a_complete_configuration_passes(env: dict[str, str]) -> None:
    report = check_config(paths_from_environment(env))
    assert report.ok, [c for c in report.checks if c.status != "ok"]
    assert {c.name for c in report.checks} >= {
        "operational_database",
        "terms_ledger",
        "operator_denylist",
        "automatic_denylist",
        "state_directory",
        "artifact_root",
        "ledgers_are_separate",
    }


@pytest.mark.parametrize("name", SETTINGS)
def test_every_setting_is_required(env: dict[str, str], name: str) -> None:
    del env[name]
    with pytest.raises(WorkerConfigError, match=name):
        paths_from_environment(env)


@pytest.mark.parametrize("bad", ["relative/path", "", "/x/../y", "//host/x", "/with space"])
def test_paths_must_be_absolute_and_normalized(env: dict[str, str], bad: str) -> None:
    env["TERMS_LEDGER_PATH"] = bad
    with pytest.raises(WorkerConfigError, match="TERMS_LEDGER_PATH"):
        paths_from_environment(env)


def test_the_worker_cannot_share_the_operator_ledger_for_its_own_blocks(
    env: dict[str, str],
) -> None:
    env["AUTOMATIC_DENYLIST_PATH"] = env["DENYLIST_PATH"]
    with pytest.raises(WorkerConfigError, match="must differ"):
        paths_from_environment(env)


def test_ledgers_in_one_directory_are_flagged(env: dict[str, str], tmp_path: Path) -> None:
    sibling = Path(env["DENYLIST_PATH"]).parent / "automatic.json"
    Denylist(sibling).initialize()
    env["AUTOMATIC_DENYLIST_PATH"] = str(sibling)
    report = check_config(paths_from_environment(env))
    failed = {c.name for c in report.checks if c.status == "fail"}
    assert failed == {"ledgers_are_separate"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("OPERATIONAL_DB", "operational_database"),
        ("TERMS_LEDGER_PATH", "terms_ledger"),
        ("DENYLIST_PATH", "operator_denylist"),
        ("AUTOMATIC_DENYLIST_PATH", "automatic_denylist"),
        ("WORKER_STATE_DIR", "state_directory"),
        ("ARTIFACT_ROOT", "artifact_root"),
    ],
)
def test_each_missing_piece_fails_its_own_check_and_is_never_created(
    env: dict[str, str], tmp_path: Path, name: str, expected: str
) -> None:
    missing = tmp_path / "nowhere" / "x"
    env[name] = str(missing)
    before = _tree(tmp_path)
    report = check_config(paths_from_environment(env))
    assert not report.ok
    assert {c.name for c in report.checks if c.status == "fail"} >= {expected}
    assert _tree(tmp_path) == before  # the check created nothing
    assert not missing.exists() and not missing.parent.exists()


@pytest.mark.parametrize("name", ["TERMS_LEDGER_PATH", "DENYLIST_PATH", "AUTOMATIC_DENYLIST_PATH"])
def test_an_invalid_ledger_fails_closed(env: dict[str, str], name: str) -> None:
    Path(env[name]).write_text("{not json")
    assert not check_config(paths_from_environment(env)).ok


@pytest.mark.parametrize("name", ["WORKER_STATE_DIR", "ARTIFACT_ROOT"])
def test_private_directories_must_not_be_open_to_others(env: dict[str, str], name: str) -> None:
    os.chmod(env[name], 0o755)
    report = check_config(paths_from_environment(env))
    detail = next(c.detail for c in report.checks if c.status == "fail")
    assert "accessible to group or others" in detail


def test_building_the_registry_opens_and_creates_nothing(
    env: dict[str, str], tmp_path: Path
) -> None:
    for key in ("TERMS_LEDGER_PATH", "AUTOMATIC_DENYLIST_PATH"):
        Path(env[key]).unlink()
    before = _tree(tmp_path)
    registry = build_registry(paths_from_environment(env))
    assert registry.automatic is not None
    assert registry.suppression is registry.automatic  # blocks go to the worker's own ledger
    assert registry.denylist.path == Path(env["DENYLIST_PATH"])
    assert _tree(tmp_path) == before


def test_the_cli_reports_json_and_a_distinct_exit_code(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)
    assert main(["check"]) == 1  # nothing configured: refuses, does not guess
    assert json.loads(capsys.readouterr().out)["ok"] is False
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert main(["check"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    Path(env["DENYLIST_PATH"]).write_text("{not json")
    assert main(["check"]) == 1
    assert main([]) == 2 and main(["start"]) == 2  # there is no way to start a worker from here
