"""Private, local, content-addressed store for raw fetched evidence.

Layout under the store root (POSIX, owner-only)::

    blobs/<aa>/<sha256>          immutable body bytes, named by their SHA-256
    observations/<run_id>/<sha256(source_url)>.json
                                 append-only ``RawArtifact`` metadata, one per fetch
    staging/                     same-filesystem scratch space for atomic publication

Bodies and observations are separate. Identical bytes are stored once, but every
fetch keeps its own observation, so repeated content never erases a fetch event.

Retry semantics (``ArtifactStore.put``): an observation is identified by
``(fetch_run_id, source_url)``. Repeating a ``put`` with the same bytes and identical
metadata (including ``fetched_at``) is an idempotent retry and returns the stored
record. Any different metadata or body for that identity is a conflicting duplicate and
raises ``ObservationConflictError``; stored history is never replaced.

Failure semantics: the body is published before the observation, so a committed
observation always has its body. A failure in between leaves at most an unreachable
orphan body, which is harmless and is reused by a later ``put`` of the same bytes.

Files are published by writing a staged file, then ``link()`` into place, which fails
rather than replaces. Existing bodies are re-hashed before being trusted, and a
mismatch raises ``ArtifactCorruptError`` without touching the file.

The configured root's parent must be provisioned and trusted; processes running as
the store owner must not concurrently rename or directly edit its directories.
"""

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.evidence import RawArtifact

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_DIR_MODE = 0o700
_FILE_MODE = 0o400


class ArtifactStoreError(Exception):
    """Base class for store failures."""


class UnsafeStorePathError(ArtifactStoreError):
    """The root or a path beneath it is a symlink, foreign, or accessible to others."""


class ArtifactCorruptError(ArtifactStoreError):
    """Stored bytes or metadata no longer match their address or recorded size."""


class ObservationConflictError(ArtifactStoreError):
    """An observation with the same run and URL exists with different content."""


class ArtifactNotFoundError(ArtifactStoreError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_private_dir(path: Path) -> None:
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):  # also rejects symlinks: lstat does not follow
        raise UnsafeStorePathError(f"not a real directory: {path}")
    if st.st_uid != os.getuid():
        raise UnsafeStorePathError(f"not owned by the current user: {path}")
    if st.st_mode & 0o077:
        raise UnsafeStorePathError(f"accessible to group/other: {path}")


def _private_dir(path: Path) -> Path:
    with suppress(FileExistsError):
        os.mkdir(path, _DIR_MODE)
    _check_private_dir(path)
    # A losing concurrent mkdir still needs the directory entry persisted.
    _sync_dir(path.parent)
    return path


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    def __init__(self, root: Path | str) -> None:
        root = Path(root)
        if not root.is_absolute() or ".." in root.parts:
            raise UnsafeStorePathError(f"store root must be an absolute, normalized path: {root}")
        self._root = _private_dir(root)
        self._blobs = _private_dir(root / "blobs")
        self._observations = _private_dir(root / "observations")
        self._staging = _private_dir(root / "staging")

    # -- paths (every component is derived from a validated hex digest or run id) --

    def _blob_path(self, artifact_id: str) -> Path:
        if not _SHA256_RE.fullmatch(artifact_id):
            raise ValueError("artifact_id must be a lowercase SHA-256 hex digest")
        return self._blobs / artifact_id[:2] / artifact_id

    def _observation_path(self, fetch_run_id: str, source_url: str) -> Path:
        if not _RUN_ID_RE.fullmatch(fetch_run_id):
            raise ValueError("unsafe fetch_run_id")
        return self._observations / fetch_run_id / f"{_sha256(source_url.encode())}.json"

    # -- atomic no-replace publication --

    def _check_parents(self, directory: Path) -> None:
        _check_private_dir(self._root)
        current = self._root
        for part in directory.relative_to(self._root).parts:
            current /= part
            _check_private_dir(current)

    def _publish(self, final: Path, data: bytes) -> bool:
        """Write ``data`` to ``final`` unless it exists; True when this call created it."""
        self._check_parents(final.parent.parent)
        self._check_parents(self._staging)
        _private_dir(final.parent)
        staged = self._staging / uuid.uuid4().hex
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fchmod(f.fileno(), _FILE_MODE)
                os.fsync(f.fileno())
            try:
                os.link(staged, final)
            except FileExistsError:
                # A concurrent writer may have linked the file but not synced it yet.
                _sync_dir(final.parent)
                return False
            _sync_dir(final.parent)
            return True
        finally:
            staged.unlink(missing_ok=True)

    def _read_file(self, path: Path) -> bytes:
        self._check_parents(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != _FILE_MODE
            ):
                raise UnsafeStorePathError(f"not an owner-only read-only file: {path}")
            return f.read()

    # -- bodies --

    def _ensure_body(self, artifact_id: str, body: bytes) -> None:
        path = self._blob_path(artifact_id)
        if not self._publish(path, body):
            self.read_body(artifact_id)  # existing blob: verify, never overwrite

    def read_body(self, artifact_id: str) -> bytes:
        """Return the body bytes after verifying their SHA-256."""
        path = self._blob_path(artifact_id)
        try:
            data = self._read_file(path)
        except FileNotFoundError:
            raise ArtifactNotFoundError(artifact_id) from None
        if _sha256(data) != artifact_id:
            raise ArtifactCorruptError(f"body does not match its digest: {artifact_id}")
        return data

    # -- observations --

    def put(
        self,
        body: bytes,
        *,
        media_type: str,
        source_url: str,
        fetched_at: datetime,
        http_status: int,
        fetch_run_id: str,
    ) -> RawArtifact:
        """Store a body and append its fetch observation; see module docstring."""
        record = RawArtifact(
            schema_version=CONTRACT_VERSION,
            artifact_id=_sha256(body),
            media_type=media_type,
            source_url=source_url,
            fetched_at=fetched_at,
            http_status=http_status,
            byte_length=len(body),
            fetch_run_id=fetch_run_id,
        )
        path = self._observation_path(record.fetch_run_id, record.source_url)
        self._ensure_body(record.artifact_id, body)
        if (
            not self._publish(path, record.model_dump_json().encode() + b"\n")
            and self._load(path) != record
        ):
            raise ObservationConflictError(
                f"run {fetch_run_id!r} already recorded a different observation for this URL"
            )
        return record

    def get_observation(self, fetch_run_id: str, source_url: str) -> RawArtifact:
        path = self._observation_path(fetch_run_id, source_url)
        try:
            record = self._load(path)
        except FileNotFoundError:
            raise ArtifactNotFoundError(f"{fetch_run_id} {source_url}") from None
        if (record.fetch_run_id, record.source_url) != (fetch_run_id, source_url):
            raise ArtifactCorruptError(f"observation does not match its path: {path}")
        return record

    def observations(self, artifact_id: str | None = None) -> Iterator[RawArtifact]:
        """Yield every stored observation (optionally for one body), ordered by fetch time."""
        self._check_parents(self._observations)
        found = []
        for run in self._observations.iterdir():
            self._check_parents(run)
            for path in run.glob("*.json"):
                record = self._load(path)
                if path != self._observation_path(record.fetch_run_id, record.source_url):
                    raise ArtifactCorruptError(f"observation does not match its path: {path}")
                if artifact_id is None or record.artifact_id == artifact_id:
                    found.append(record)
        yield from sorted(found, key=lambda r: (r.fetched_at, r.fetch_run_id, r.source_url))

    def _load(self, path: Path) -> RawArtifact:
        data = self._read_file(path)
        try:
            return RawArtifact.model_validate_json(data)
        except ValidationError as exc:
            raise ArtifactCorruptError(f"unreadable observation: {path}") from exc

    def read(self, record: RawArtifact) -> bytes:
        """Return the body for an observation, also checking its recorded size."""
        data = self.read_body(record.artifact_id)
        if len(data) != record.byte_length:
            raise ArtifactCorruptError(f"size differs from observation: {record.artifact_id}")
        return data
