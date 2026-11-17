"""Offline registry audit; a clean report is never publication authorization.

``read_registry`` copies a consistent DB transaction; ``validate_registry`` is pure.
Coverage counts institution/family slots, not offers. ``absent`` means no catalog
record (not evidence of no product); ``verified`` means a fresh evidenced product
classification, not an approved institution or publishable rate. Existing explicit
absence states stay separate. Any conflicting classifications fail closed.

The frozen schema has no institution-identity review or relationship-evidence link.
A verified charter or relationship therefore reports missing provenance rather than
inventing one from unrelated evidence. MA scope is supported only by membership in
an importer-produced MA roster, whose raw-file hash is retained separately from
fetch artifacts. This audit checks stored evidence references, not private bytes.
"""

import argparse
import hashlib
import ipaddress
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection, create_engine, select

from cu_rates.contracts import (
    Institution,
    InstitutionSourceRelationship,
    ProductFinding,
    ProductIdentity,
    Provenance,
    RateSource,
    SourceApproval,
    product_content_sha256,
)
from cu_rates.contracts.enums import ProductFamily
from cu_rates.contracts.evidence import locator_fits_media_type
from cu_rates.db import schema


class Reason(StrEnum):
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_RECORD = "invalid_record"
    INVALID_SOURCE_URL = "invalid_source_url"
    MA_SCOPE_UNPROVEN = "ma_scope_unproven"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    ROSTER_STATUS_CONFLICT = "roster_status_conflict"
    ROSTER_IDENTITY_CONFLICT = "roster_identity_conflict"
    IDENTITY_EVIDENCE_MISSING = "identity_evidence_missing"
    RELATIONSHIP_EVIDENCE_MISSING = "relationship_evidence_missing"
    EVIDENCE_MISSING = "evidence_missing"
    RELATIONSHIP_UNVERIFIED = "relationship_unverified"
    BLOCKED_AND_VERIFIED = "blocked_and_verified"
    CLASSIFICATION_CONFLICT = "classification_conflict"
    STALE = "stale"
    UNSUPPORTED_PUBLICATION = "unsupported_publication"
    PUBLICATION_INSTITUTION_UNVERIFIED = "publication_institution_unverified"
    PUBLICATION_SOURCE_UNAPPROVED = "publication_source_unapproved"
    PUBLICATION_PRODUCT_UNAPPROVED = "publication_product_unapproved"


class CoverageState(StrEnum):
    ABSENT = "absent"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    NOT_OFFERED = "not_offered"
    NO_PUBLIC_RATE = "no_public_rate"
    QUOTE_ONLY = "quote_only"
    LOGIN_REQUIRED = "login_required"
    STALE = "stale"


class ReportModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Finding(ReportModel):
    institution_charter: int | None
    entity: str
    entity_id: str
    reason: Reason


class Coverage(ReportModel):
    family: ProductFamily
    state: CoverageState


class InstitutionReport(ReportModel):
    charter_number: int
    findings: tuple[Finding, ...]
    coverage: tuple[Coverage, ...]


class RegistryReport(ReportModel):
    report_version: str = "1.0"
    as_of: datetime
    freshness_hours: int = 72
    withdrawal_hours: int = 96
    institutions: tuple[InstitutionReport, ...]
    findings: tuple[Finding, ...]
    reason_counts: dict[str, int]
    coverage_counts: dict[str, int]

    def canonical_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


# Rows deliberately remain untrusted until revalidated. This also permits auditing
# malformed legacy DBs and testing duplicate keys that SQLite normally prohibits.
Row = Mapping[str, Any]
TABLE_NAMES = (
    "institutions",
    "products",
    "product_borrower_programs",
    "product_partner_programs",
    "rate_sources",
    "institution_source_relationships",
    "source_approvals",
    "source_approval_evidence",
    "product_findings",
    "finding_purposes",
    "finding_evidence",
    "review_decisions",
    "review_evidence",
    "provenance",
    "artifact_blobs",
    "artifact_observations",
    "fetch_runs",
    "roster_imports",
    "roster_import_entries",
    "normalized_offers",
    "offer_evidence",
    "snapshot_entries",
    "withdrawal_entries",
    "withdrawal_lifts",
)


@dataclass(frozen=True)
class Registry:
    tables: Mapping[str, Sequence[Row]]

    def rows(self, name: str) -> Sequence[Row]:
        return self.tables.get(name, ())


def read_registry(conn: Connection) -> Registry:
    """SELECT only; caller supplies a transaction spanning all reads."""
    return Registry(
        {
            name: tuple(
                dict(row) for row in conn.execute(select(schema.metadata.tables[name])).mappings()
            )
            for name in TABLE_NAMES
        }
    )


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def valid_source_url(value: str) -> bool:
    """Strict canonical HTTPS/DNS syntax; never resolve or contact a host.

    All queries are rejected, avoiding guessing which parameters contain secrets.
    ASCII/punycode confusables, local-use names, ports and ambiguous encodings fail
    closed. Syntax alone cannot establish the public IP of a DNS name.
    """
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        labels = host.split(".")
        if (
            not value.startswith("https://")
            or not value.isascii()
            or any(c.isspace() or ord(c) < 33 for c in value)
            or any(c in value for c in ("?", "#", "\\"))
            or parsed.netloc != host
            or len(host) > 253
            or len(labels) < 2
            or not labels[-1].isalpha()
            or host.endswith((".localhost", ".local", ".internal", ".home", ".lan"))
            or any(not _LABEL.fullmatch(label) or label.startswith("xn--") for label in labels)
        ):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except ValueError:
        return False


M = TypeVar("M", bound=BaseModel)


def validate_registry(registry: Registry, *, as_of: datetime) -> RegistryReport:
    """Rebuild and audit registry facts with an explicit UTC evaluation instant."""
    if as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    findings: set[tuple[int | None, str, str, Reason]] = set()

    def add(charter: int | None, entity: str, key: object, reason: Reason) -> None:
        findings.add((charter if isinstance(charter, int) else None, entity, str(key), reason))

    def checked(
        model: type[M], data: Row, entity: str, key: object, charter: int | None
    ) -> M | None:
        try:
            return model.model_validate(data)
        except (ValidationError, ValueError, TypeError):
            add(charter, entity, key, Reason.INVALID_RECORD)
            return None

    def index(name: str, key: str) -> dict[Any, Row]:
        grouped: dict[Any, list[Row]] = {}
        for row in registry.rows(name):
            grouped.setdefault(row.get(key), []).append(row)
        result: dict[Any, Row] = {}
        for value, rows in grouped.items():
            if len(rows) != 1:
                add(
                    value if name == "institutions" else None,
                    name,
                    value,
                    Reason.DUPLICATE_IDENTIFIER,
                )
            else:
                result[value] = rows[0]
        return result

    institutions = index("institutions", "charter_number")
    products = index("products", "product_id")
    sources = index("rate_sources", "source_id")
    relationships = index("institution_source_relationships", "relationship_id")
    approvals = index("source_approvals", "approval_id")
    finding_rows = index("product_findings", "finding_id")
    reviews = index("review_decisions", "review_id")
    provenance = index("provenance", "provenance_id")
    blobs = index("artifact_blobs", "artifact_id")
    runs = index("fetch_runs", "run_id")
    imports = index("roster_imports", "import_id")
    offers = index("normalized_offers", "offer_id")
    parsed_products: dict[str, ProductIdentity] = {}
    parsed_institutions: dict[int, Institution] = {}
    parsed_sources: dict[str, RateSource] = {}
    parsed_provenance: dict[str, Provenance] = {}

    for key, row in provenance.items():
        data = {
            "artifact_id": row["artifact_id"],
            "locator": row["locator_json"],
            "source_text": row["source_text"],
            "extraction": {
                "git_sha": row["git_sha"],
                "parser": {"name": row["parser_name"], "version": row["parser_version"]},
                "configuration_sha256": row["configuration_sha256"],
                "normalization_version": row["normalization_version"],
                "rule_id": row["rule_id"],
            },
        }
        proof = checked(Provenance, data, "provenance", key, row["institution_charter"])
        if proof is not None:
            parsed_provenance[key] = proof

    def evidence(
        link_table: str, column: str, key: str, charter: int, source_id: str | None = None
    ) -> tuple[Provenance, ...]:
        links = [r for r in registry.rows(link_table) if r[column] == key]
        valid: list[Provenance] = []
        for link in links:
            pid = link["provenance_id"]
            ref = parsed_provenance.get(pid)
            row = provenance.get(pid)
            if ref is None or row is None or row["institution_charter"] != charter:
                continue
            observations = [
                o
                for o in registry.rows("artifact_observations")
                if o["artifact_id"] == ref.artifact_id
            ]
            if ref.artifact_id not in blobs:
                continue
            if any(
                (run := runs.get(o["fetch_run_id"])) is not None
                and run["status"] == "succeeded"
                and (
                    source_id is None
                    or (
                        run["source_id"] == source_id
                        and source_id in sources
                        and o["source_url"] == sources[source_id]["url"]
                    )
                )
                and 200 <= o["http_status"] < 300
                and o["fetched_at"] <= as_of
                and locator_fits_media_type(ref.locator, o["media_type"])
                for o in observations
            ):
                valid.append(ref)
        # Partial evidence is never quietly discarded.
        return tuple(valid) if links and len(valid) == len(links) else ()

    for charter, row in institutions.items():
        if (
            not isinstance(charter, int)
            or isinstance(charter, bool)
            or not 0 < charter < 10**8
            or not isinstance(row.get("legal_name"), str)
            or not row["legal_name"].strip()
        ):
            add(charter, "institutions", charter, Reason.INVALID_IDENTIFIER)
        institution_record = checked(Institution, row, "institutions", charter, charter)
        if not isinstance(charter, int):
            continue
        if institution_record is not None:
            parsed_institutions[charter] = institution_record
        roster = [
            r
            for r in registry.rows("roster_import_entries")
            if r["charter_number"] == charter
            and r["import_id"] in imports
            and imports[r["import_id"]]["retrieved_at"] <= as_of
        ]
        if not roster:
            add(charter, "institutions", charter, Reason.MA_SCOPE_UNPROVEN)
        else:
            # Consider all entries at the newest official date, never insertion order.
            latest_date = max(r["source_as_of_date"] for r in roster)
            latest_roster = [r for r in roster if r["source_as_of_date"] == latest_date]
            for r in latest_roster:
                if r["status"] != row.get("status"):
                    add(charter, "institutions", charter, Reason.ROSTER_STATUS_CONFLICT)
                if r["legal_name"] != row.get("legal_name") or r[
                    "successor_charter_number"
                ] != row.get("successor_charter_number"):
                    add(charter, "institutions", charter, Reason.ROSTER_IDENTITY_CONFLICT)
                if not re.fullmatch(r"[0-9a-f]{64}", imports[r["import_id"]]["artifact_id"]):
                    add(charter, "institutions", charter, Reason.EVIDENCE_MISSING)
        if row.get("charter_verified"):
            add(charter, "institutions", charter, Reason.IDENTITY_EVIDENCE_MISSING)

    for key, row in sources.items():
        if not valid_source_url(row["url"]):
            owners = {
                r["institution_charter"] for r in relationships.values() if r["source_id"] == key
            }
            if not owners:
                add(None, "rate_sources", key, Reason.INVALID_SOURCE_URL)
            for owner in owners:
                add(owner, "rate_sources", key, Reason.INVALID_SOURCE_URL)
        source_record = checked(RateSource, row, "rate_sources", key, None)
        if source_record is not None and valid_source_url(source_record.url):
            parsed_sources[key] = source_record

    for key, row in products.items():
        data = {k: v for k, v in row.items() if k != "notes_json"}
        data["notes"] = row["notes_json"]
        for name in ("borrower", "partner"):
            data[f"{name}_programs"] = sorted(
                r["program"]
                for r in registry.rows(f"product_{name}_programs")
                if r["product_id"] == key
            )
        product_record = checked(ProductIdentity, data, "products", key, row["institution_charter"])
        if product_record is not None:
            parsed_products[key] = product_record

    verified_relationships: set[tuple[int, str, str | None]] = set()
    for key, row in relationships.items():
        charter = row["institution_charter"]
        relationship_record = checked(
            InstitutionSourceRelationship, row, "relationships", key, charter
        )
        if (
            relationship_record is not None
            and relationship_record.verified
            and relationship_record.approved_at is not None
            and relationship_record.approved_at <= as_of
        ):
            verified_relationships.add(
                (charter, relationship_record.source_id, relationship_record.product_id)
            )
            add(charter, "relationships", key, Reason.RELATIONSHIP_EVIDENCE_MISSING)

    def related(charter: int, source: str, product: str | None) -> bool:
        return (charter, source, product) in verified_relationships

    def latest(rows: Sequence[Row], time_key: str, id_key: str) -> list[Row]:
        eligible = [r for r in rows if r[time_key] <= as_of]
        if not eligible:
            return []
        instant = max(r[time_key] for r in eligible)
        # Conflicting simultaneous decisions must all pass; no arbitrary winning id.
        return sorted((r for r in eligible if r[time_key] == instant), key=lambda r: r[id_key])

    lifts = {
        r["withdrawal_id"] for r in registry.rows("withdrawal_lifts") if r["lifted_at"] <= as_of
    }
    withdrawals = {
        (r["scope"], r["target_id"])
        for r in registry.rows("withdrawal_entries")
        if r["withdrawn_at"] <= as_of and r["withdrawal_id"] not in lifts
    }
    blocked_sources = {target for scope, target in withdrawals if scope == "source"}
    approved_sources: set[tuple[int, str]] = set()
    for source in sources:
        decisions = latest(
            [r for r in approvals.values() if r["source_id"] == source], "decided_at", "approval_id"
        )
        for rel in relationships.values():
            if rel["source_id"] != source:
                continue
            charter = rel["institution_charter"]
            good = bool(decisions) and source in parsed_sources
            for row in decisions:
                refs = evidence(
                    "source_approval_evidence", "approval_id", row["approval_id"], charter, source
                )
                data = {
                    k: v
                    for k, v in row.items()
                    if k not in {"adapter_name", "adapter_version", "approved_origins_json"}
                }
                data.update(
                    evidence=refs,
                    approved_origins=row["approved_origins_json"] or (),
                    adapter=None
                    if row["adapter_name"] is None
                    else {"name": row["adapter_name"], "version": row["adapter_version"]},
                )
                approval_record = checked(
                    SourceApproval, data, "source_approvals", row["approval_id"], charter
                )
                if row["decision"] == "approved" and not refs:
                    add(charter, "source_approvals", row["approval_id"], Reason.EVIDENCE_MISSING)
                good = (
                    good
                    and approval_record is not None
                    and approval_record.approved
                    and bool(refs)
                    and (row["source_url"] == sources[source]["url"])
                )
            if good and source in blocked_sources:
                add(charter, "rate_sources", source, Reason.BLOCKED_AND_VERIFIED)
            if good and source not in blocked_sources:
                approved_sources.add((charter, source))

    product_states: dict[str, CoverageState] = {}
    for key, product in parsed_products.items():
        charter = product.institution_charter
        candidates = [
            r
            for r in finding_rows.values()
            if r["product_id"] == key and r["institution_charter"] == charter
        ]
        current = latest(candidates, "observed_at", "finding_id")
        states: set[CoverageState] = set()
        for row in current:
            refs = evidence(
                "finding_evidence", "finding_id", row["finding_id"], charter, row["source_id"]
            )
            data = dict(row)
            data.update(
                evidence=refs,
                purposes=sorted(
                    r["purpose"]
                    for r in registry.rows("finding_purposes")
                    if r["finding_id"] == row["finding_id"]
                ),
            )
            finding_record = checked(
                ProductFinding, data, "product_findings", row["finding_id"], charter
            )
            if row["review_state"] != "verified":
                continue
            if not refs:
                add(charter, "product_findings", row["finding_id"], Reason.EVIDENCE_MISSING)
            if not related(charter, row["source_id"], key):
                add(charter, "product_findings", row["finding_id"], Reason.RELATIONSHIP_UNVERIFIED)
                continue
            if finding_record is None or not refs or row["source_id"] not in parsed_sources:
                continue
            if (
                finding_record.family != product.family
                or finding_record.rate_availability != product.rate_availability
            ):
                add(charter, "products", key, Reason.CLASSIFICATION_CONFLICT)
                states.add(CoverageState.UNKNOWN)
                continue
            if as_of - finding_record.observed_at > timedelta(hours=72):
                add(charter, "product_findings", row["finding_id"], Reason.STALE)
                states.add(CoverageState.STALE)
            elif finding_record.rate_availability == "public_rate":
                states.add(CoverageState.VERIFIED)
            else:
                states.add(CoverageState(finding_record.rate_availability))
        if len(states) > 1:
            add(charter, "products", key, Reason.CLASSIFICATION_CONFLICT)
        product_states[key] = next(iter(states)) if len(states) == 1 else CoverageState.UNKNOWN

    approved_products: set[str] = set()
    for key, product in parsed_products.items():
        decisions = latest(
            [r for r in reviews.values() if r["subject"] == "product" and r["subject_id"] == key],
            "decided_at",
            "review_id",
        )
        good = bool(decisions)
        for row in decisions:
            refs = evidence(
                "review_evidence", "review_id", row["review_id"], product.institution_charter
            )
            if row["verdict"] == "approved" and not refs:
                add(
                    product.institution_charter,
                    "review_decisions",
                    row["review_id"],
                    Reason.EVIDENCE_MISSING,
                )
            good = (
                good
                and bool(refs)
                and row["verdict"] == "approved"
                and (row["reviewed_product_sha256"] == product_content_sha256(product))
            )
        if good:
            approved_products.add(key)
            if product.rate_availability == "blocked":
                add(product.institution_charter, "products", key, Reason.BLOCKED_AND_VERIFIED)
            if (
                product_states.get(key) != CoverageState.VERIFIED
                or product.lifecycle != "active"
                or not any(
                    (product.institution_charter, source) in approved_sources
                    and related(product.institution_charter, source, key)
                    for source in sources
                )
                or product.institution_charter not in parsed_institutions
                or not parsed_institutions[product.institution_charter].may_publish
                or any(
                    f[0] == product.institution_charter
                    and f[3]
                    in {
                        Reason.IDENTITY_EVIDENCE_MISSING,
                        Reason.RELATIONSHIP_EVIDENCE_MISSING,
                        Reason.MA_SCOPE_UNPROVEN,
                        Reason.ROSTER_STATUS_CONFLICT,
                        Reason.ROSTER_IDENTITY_CONFLICT,
                    }
                    for f in findings
                )
            ):
                add(product.institution_charter, "products", key, Reason.UNSUPPORTED_PUBLICATION)

    snapshot_offers = {r["offer_id"] for r in registry.rows("snapshot_entries")}
    for key, row in offers.items():
        if row["status"] not in ("approved", "published") and key not in snapshot_offers:
            continue
        charter, source, product_id = (
            row["institution_charter"],
            row["source_id"],
            row["product_id"],
        )
        if not related(charter, source, product_id):
            add(charter, "normalized_offers", key, Reason.RELATIONSHIP_UNVERIFIED)
        institution = parsed_institutions.get(charter)
        if institution is None or not institution.may_publish:
            add(charter, "normalized_offers", key, Reason.PUBLICATION_INSTITUTION_UNVERIFIED)
        if (charter, source) not in approved_sources:
            add(charter, "normalized_offers", key, Reason.PUBLICATION_SOURCE_UNAPPROVED)
        if product_id not in approved_products:
            add(charter, "normalized_offers", key, Reason.PUBLICATION_PRODUCT_UNAPPROVED)
        if (
            institution is None
            or not institution.may_publish
            or product_id not in approved_products
            or (charter, source) not in approved_sources
            or not related(charter, source, product_id)
            # These proofs cannot be represented in the frozen schema.
            or any(
                f[0] == charter
                and f[3]
                in {
                    Reason.IDENTITY_EVIDENCE_MISSING,
                    Reason.RELATIONSHIP_EVIDENCE_MISSING,
                    Reason.MA_SCOPE_UNPROVEN,
                    Reason.ROSTER_STATUS_CONFLICT,
                    Reason.ROSTER_IDENTITY_CONFLICT,
                    Reason.INVALID_IDENTIFIER,
                }
                for f in findings
            )
        ):
            add(charter, "normalized_offers", key, Reason.UNSUPPORTED_PUBLICATION)
        if (
            row["observed_at"] > as_of
            or as_of - row["observed_at"] > timedelta(hours=72)
            or row["valid_until"] <= as_of
            or row["valid_until"] > row["observed_at"] + timedelta(hours=96)
        ):
            add(charter, "normalized_offers", key, Reason.STALE)
        if not evidence("offer_evidence", "offer_id", key, charter, source):
            add(charter, "normalized_offers", key, Reason.EVIDENCE_MISSING)
        if product_id in products and products[product_id]["rate_availability"] == "blocked":
            add(charter, "normalized_offers", key, Reason.BLOCKED_AND_VERIFIED)

    # History remains auditable even after a newer classification supersedes it.
    for key, row in finding_rows.items():
        refs = evidence(
            "finding_evidence", "finding_id", key, row["institution_charter"], row["source_id"]
        )
        data = dict(row)
        data.update(
            evidence=refs,
            purposes=sorted(
                r["purpose"] for r in registry.rows("finding_purposes") if r["finding_id"] == key
            ),
        )
        checked(ProductFinding, data, "product_findings", key, row["institution_charter"])
        if row["review_state"] == "verified" and not refs:
            add(row["institution_charter"], "product_findings", key, Reason.EVIDENCE_MISSING)

    for key, row in approvals.items():
        if row["decision"] != "approved":
            continue
        owners = {
            r["institution_charter"]
            for r in relationships.values()
            if r["source_id"] == row["source_id"]
        }
        if not owners:
            add(None, "source_approvals", key, Reason.RELATIONSHIP_UNVERIFIED)
        for charter in owners:
            if not evidence(
                "source_approval_evidence", "approval_id", key, charter, row["source_id"]
            ):
                add(charter, "source_approvals", key, Reason.EVIDENCE_MISSING)
    for key, row in reviews.items():
        if row["verdict"] != "approved":
            continue
        subject = (
            products.get(row["subject_id"])
            if row["subject"] == "product"
            else offers.get(row["subject_id"])
        )
        if subject is None:
            add(None, "review_decisions", key, Reason.INVALID_RECORD)
        elif not evidence("review_evidence", "review_id", key, subject["institution_charter"]):
            add(subject["institution_charter"], "review_decisions", key, Reason.EVIDENCE_MISSING)

    ordered = tuple(
        Finding(institution_charter=c, entity=e, entity_id=k, reason=r)
        for c, e, k, r in sorted(
            findings, key=lambda f: (-1 if f[0] is None else f[0], f[1], f[2], f[3])
        )
    )
    institution_reports: list[InstitutionReport] = []
    counts = Counter({state.value: 0 for state in CoverageState})
    for charter in sorted(c for c in institutions if isinstance(c, int)):
        coverage: list[Coverage] = []
        for family_value in sorted(ProductFamily):
            family = ProductFamily(family_value)
            matching = [
                p
                for p in parsed_products.values()
                if p.institution_charter == charter and p.family == family
            ]
            states = {product_states.get(p.product_id, CoverageState.UNKNOWN) for p in matching}
            state = (
                CoverageState.ABSENT
                if not matching
                else next(iter(states))
                if len(states) == 1
                else CoverageState.UNKNOWN
            )
            # A malformed product cannot inflate absence coverage.
            if any(
                r["institution_charter"] == charter
                and r["family"] == family
                and k not in parsed_products
                for k, r in products.items()
            ):
                state = CoverageState.UNKNOWN
            if not matching and any(
                r["institution_charter"] == charter
                and r["review_state"] != "rejected"
                and (r["family"] == family or r["scope"] == "unknown")
                for r in finding_rows.values()
            ):
                state = CoverageState.UNKNOWN
            coverage.append(Coverage(family=family, state=state))
            counts[state.value] += 1
        institution_reports.append(
            InstitutionReport(
                charter_number=charter,
                findings=tuple(f for f in ordered if f.institution_charter == charter),
                coverage=tuple(coverage),
            )
        )
    return RegistryReport(
        as_of=as_of,
        institutions=tuple(institution_reports),
        findings=ordered,
        reason_counts=dict(sorted(Counter(f.reason.value for f in ordered).items())),
        coverage_counts=dict(sorted(counts.items())),
    )


def report_from_database(path: Path, *, as_of: datetime) -> RegistryReport:
    """Open an existing local SQLite DB read-only, without operational WAL setup."""
    path = path.resolve(strict=True)
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("use a checkpointed offline database copy; a nonempty WAL is present")
    uri = path.as_uri() + "?mode=ro&immutable=1"
    engine = create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(uri, uri=True, autocommit=False)
    )
    try:
        with engine.connect() as conn:
            return validate_registry(read_registry(conn), as_of=as_of)
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output is not None and (
        args.output.resolve()
        in {
            args.database.resolve(),
            Path(str(args.database.resolve()) + "-wal"),
            Path(str(args.database.resolve()) + "-shm"),
        }
        or (args.output.exists() and args.database.exists() and args.output.samefile(args.database))
    ):
        parser.error("output must not overwrite the database")
    report = report_from_database(args.database, as_of=args.as_of)
    envelope = json.dumps(
        {"report": json.loads(report.canonical_json()), "sha256": report.sha256},
        sort_keys=True,
        separators=(",", ":"),
    )
    print(envelope)
    if args.output is not None:
        args.output.write_text(envelope + "\n", encoding="ascii")
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
