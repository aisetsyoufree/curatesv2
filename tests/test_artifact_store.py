"""Synthetic tests for the private content-addressed artifact store."""

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cu_rates.artifact_store import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactStore,
    ObservationConflictError,
    UnsafeStorePathError,
)

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
URL = "https://example.test/rates"
BODY = b"<html>synthetic \xff\x00 bytes\r\n</html>"


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


def put(store: ArtifactStore, body: bytes = BODY, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "media_type": "text/html; charset=utf-8",
        "source_url": URL,
        "fetched_at": T0,
        "http_status": 200,
        "fetch_run_id": "run-1",
    }
    kwargs.update(overrides)
    return store.put(body, **kwargs)


def blob(store: ArtifactStore, artifact_id: str) -> Path:
    return store._blob_path(artifact_id)


def test_digest_is_of_exact_bytes(store: ArtifactStore) -> None:
    record = put(store)
    assert record.artifact_id == hashlib.sha256(BODY).hexdigest()
    assert record.byte_length == len(BODY)
    assert store.read_body(record.artifact_id) == BODY
    other = put(store, BODY + b"\n", fetch_run_id="run-2")
    assert other.artifact_id != record.artifact_id


def test_empty_body_is_supported(store: ArtifactStore) -> None:
    record = put(store, b"")
    assert record.byte_length == 0
    assert store.read(record) == b""


def test_cross_run_dedup_keeps_every_observation(store: ArtifactStore) -> None:
    first = put(store, fetch_run_id="run-1")
    second = put(store, fetch_run_id="run-2", fetched_at=T0 + timedelta(days=1), http_status=203)
    assert first.artifact_id == second.artifact_id
    assert len(list((store._root / "blobs").glob("*/*"))) == 1
    seen = list(store.observations(first.artifact_id))
    assert [r.fetch_run_id for r in seen] == ["run-1", "run-2"]
    assert [r.http_status for r in seen] == [200, 203]


def test_same_run_different_url_is_distinct(store: ArtifactStore) -> None:
    put(store)
    put(store, source_url="https://example.test/other")
    assert len(list(store.observations())) == 2


def test_restart_readback_preserves_metadata(tmp_path: Path) -> None:
    root = tmp_path / "store"
    written = put(ArtifactStore(root), media_type="application/pdf", http_status=206)
    reopened = ArtifactStore(root)
    assert reopened.get_observation("run-1", URL) == written
    assert written.media_type == "application/pdf"
    assert (written.source_url, written.fetched_at, written.http_status) == (URL, T0, 206)
    assert (written.fetch_run_id, written.byte_length) == ("run-1", len(BODY))
    assert list(reopened.observations()) == [written]
    assert reopened.read(written) == BODY


def test_idempotent_retry_returns_same_record(store: ArtifactStore) -> None:
    first = put(store)
    assert put(store) == first
    assert len(list(store.observations())) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetched_at": T0 + timedelta(seconds=1)},
        {"http_status": 404},
        {"media_type": "text/plain"},
    ],
)
def test_conflicting_retry_is_rejected(store: ArtifactStore, overrides: dict[str, Any]) -> None:
    first = put(store)
    with pytest.raises(ObservationConflictError):
        put(store, **overrides)
    assert store.get_observation("run-1", URL) == first


def test_conflicting_body_for_same_observation_is_rejected(store: ArtifactStore) -> None:
    first = put(store)
    with pytest.raises(ObservationConflictError):
        put(store, b"different")
    assert store.get_observation("run-1", URL) == first
    assert store.read(first) == BODY


def test_tampered_body_detected_on_read_and_never_overwritten(store: ArtifactStore) -> None:
    record = put(store)
    path = blob(store, record.artifact_id)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    path.chmod(0o400)
    with pytest.raises(ArtifactCorruptError):
        store.read_body(record.artifact_id)
    with pytest.raises(ArtifactCorruptError):
        put(store, fetch_run_id="run-2")  # same bytes, corrupt existing blob
    assert path.read_bytes() == b"tampered"
    assert not (store._root / "observations" / "run-2").exists()


def test_size_mismatch_detected(store: ArtifactStore) -> None:
    record = put(store)
    with pytest.raises(ArtifactCorruptError):
        store.read(record.model_copy(update={"byte_length": len(BODY) + 1}))


def test_corrupt_or_misplaced_observation_detected(store: ArtifactStore) -> None:
    put(store)
    path = next((store._root / "observations").glob("*/*.json"))
    path.chmod(0o600)
    path.write_text("{not json")
    path.chmod(0o400)
    with pytest.raises(ArtifactCorruptError):
        store.get_observation("run-1", URL)
    with pytest.raises(ArtifactCorruptError):
        list(store.observations())


def test_missing_items_raise_not_found(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.read_body("0" * 64)
    with pytest.raises(ArtifactNotFoundError):
        store.get_observation("run-1", URL)


def test_failed_observation_write_leaves_no_dangling_observation(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link

    def flaky(src: Any, dst: Any, **kw: Any) -> None:
        if "observations" in str(dst):
            raise OSError("simulated crash")
        real_link(src, dst, **kw)

    monkeypatch.setattr(os, "link", flaky)
    with pytest.raises(OSError, match="simulated"):
        put(store)
    assert list(store.observations()) == []
    assert list((store._root / "staging").iterdir()) == []  # staged file cleaned up
    monkeypatch.undo()
    record = put(store)  # orphan body is reused, retry succeeds
    assert store.read(record) == BODY


def test_partial_staged_file_is_ignored(store: ArtifactStore) -> None:
    (store._root / "staging" / "deadbeef").write_bytes(b"half a wri")
    record = put(store)
    assert store.read(record) == BODY
    assert list(store.observations()) == [record]


def test_permissions_are_owner_only(store: ArtifactStore) -> None:
    put(store)
    for path in store._root.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode & 0o077 == 0, path
        if path.is_file():
            assert mode & 0o222 == 0, path  # read-only once published


def test_read_rejects_insecure_file_permissions(store: ArtifactStore) -> None:
    record = put(store)
    blob(store, record.artifact_id).chmod(0o444)
    with pytest.raises(UnsafeStorePathError):
        store.read(record)


def test_open_store_refuses_replaced_intermediate_directory(
    store: ArtifactStore, tmp_path: Path
) -> None:
    record = put(store)
    blobs = store._root / "blobs"
    outside = tmp_path / "moved-blobs"
    blobs.rename(outside)
    blobs.symlink_to(outside)
    with pytest.raises(UnsafeStorePathError):
        store.read(record)
    with pytest.raises(UnsafeStorePathError):
        put(store, fetch_run_id="run-2")


def test_concurrent_retries_preserve_all_fetch_events(store: ArtifactStore) -> None:
    run_ids = ["run-1", "run-2"] * 4
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda run: put(store, fetch_run_id=run), run_ids))
    assert len({record.artifact_id for record in results}) == 1
    assert [record.fetch_run_id for record in store.observations()] == ["run-1", "run-2"]
    assert all(store.read(record) == BODY for record in results)


def test_unsafe_roots_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(UnsafeStorePathError):
        ArtifactStore(link)
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    with pytest.raises(UnsafeStorePathError):
        ArtifactStore(open_dir)
    with pytest.raises(UnsafeStorePathError):
        ArtifactStore("relative/store")
    with pytest.raises(UnsafeStorePathError):
        ArtifactStore(tmp_path / ".." / "x")


def test_symlinked_child_rejected(tmp_path: Path) -> None:
    root = tmp_path / "store"
    ArtifactStore(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "blobs").rmdir()
    (root / "blobs").symlink_to(elsewhere)
    with pytest.raises(UnsafeStorePathError):
        ArtifactStore(root)


@pytest.mark.parametrize("run_id", ["../evil", "a/b", "", "UPPER"])
def test_unsafe_run_id_rejected(store: ArtifactStore, run_id: str) -> None:
    with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError
        put(store, fetch_run_id=run_id)
    assert not (store._root.parent / "evil").exists()
