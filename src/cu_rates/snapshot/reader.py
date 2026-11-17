"""Read the current version without importing or opening operational storage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cu_rates.contracts.publication import DEGRADE_AFTER, WITHDRAW_AFTER, Placement

from ._database import POINTER, VERSIONS, read_version, verify_database
from ._files import SnapshotError, require_directory
from ._models import SnapshotView, aware_time
from .denylist import Denylist


class SnapshotReader:
    """One immutable connection per request, safe to share across web threads.

    Reopens the version selected by one atomic pointer read. No mutable database
    connections or cached denylist entries survive a request. SnapshotError means
    unavailable: callers must not serve previously cached/unfiltered rates instead.
    """

    def __init__(self, root: Path, denylist_path: Path) -> None:
        self.root = root.absolute()
        self._denylist = Denylist(denylist_path)

    def read(self, *, at: datetime | None = None) -> SnapshotView:
        """Use the server clock by default; `at` is a trusted clock override for tests."""
        try:
            require_directory(self.root)
            require_directory(self.root / VERSIONS)
        except OSError as exc:
            raise SnapshotError("public snapshot directory unavailable") from exc
        version = read_version(self.root / POINTER)
        info, offers = verify_database(self.root / VERSIONS / version.snapshot_id, version)
        now = aware_time(datetime.now(UTC) if at is None else at)
        if info.published_at > now:
            raise SnapshotError("public snapshot is dated in the future")
        active = self._denylist.active_targets(now)
        if ("snapshot", info.snapshot_id) in active:
            return SnapshotView(info=info, offers=())
        visible = []
        for offer in offers:
            if (
                now >= offer.valid_until
                or now - offer.observed_at > WITHDRAW_AFTER
                or ("offer", offer.offer_id) in active
                or ("source", offer.source_id) in active
                or ("product", offer.product_id) in active
                or ("institution", str(offer.institution_charter)) in active
            ):
                continue
            if now - offer.observed_at > DEGRADE_AFTER and offer.placement is Placement.RANKED:
                offer = offer.model_copy(update={"placement": Placement.BROWSE_ONLY})
            visible.append(offer)
        return SnapshotView(info=info, offers=tuple(visible))
