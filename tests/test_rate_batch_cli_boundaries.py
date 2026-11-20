"""Safety checks at the operator CLI boundary."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pytest import CaptureFixture

from cu_rates.artifact_store import ArtifactStore
from cu_rates.db.migrate import upgrade_database
from cu_rates.rate_batch import compute_batch_digest
from cu_rates.rate_batch.cli import main, run_intake


def _payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "batch_id": "synthetic-cli",
        "operator": "fixture",
        "model_name": "fixture",
        "tool_name": "fixture",
        "recipe_version": "v1",
        "submitted_at": "2026-11-19T19:00:00Z",
    }
    payload["batch_digest"] = compute_batch_digest(payload)
    return payload


def test_cli_dry_run_does_not_change_sqlite_journal_mode(tmp_path: Path) -> None:
    database = tmp_path / "staging.sqlite"
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    root = tmp_path / "evidence"
    ArtifactStore(root)
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(_payload()))

    report = run_intake(batch, database, root, dry_run=True, now=datetime(2026, 11, 20, tzinfo=UTC))

    assert report.dry_run
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert conn.execute("SELECT COUNT(*) FROM staged_rate_batches").fetchone() == (0,)


def test_cli_input_errors_do_not_echo_private_rows(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    database = tmp_path / "staging.sqlite"
    upgrade_database(database)
    root = tmp_path / "evidence"
    ArtifactStore(root)
    batch = tmp_path / "batch.json"
    payload = _payload()
    payload["observations"] = [{"unsupported_field": "fixture-private-do-not-echo"}]
    payload["batch_digest"] = compute_batch_digest(payload)
    batch.write_text(json.dumps(payload))

    assert main([str(batch), "--db", str(database), "--store", str(root), "--json"]) == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == {"error": "invalid_batch"}
    assert "fixture-private-do-not-echo" not in (out + err)
