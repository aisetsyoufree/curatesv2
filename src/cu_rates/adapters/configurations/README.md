# Static HTML source configuration

Deploy reviewed `<source_id>.json` files here. No real source is configured or approved
by this change. Tests use only `tests/fixtures/adapters/*.rates.json` and invented
`.example` pages. Raw captures remain in the private artifact store.

The JSON shape is validated by `SourceConfiguration` in `static_html.py`. It binds a
source ID/URL and charter to a reviewed layout digest and explicit offer/product IDs.
Each row declares a comparison target (or null) and field paths with:

- A uniquely resolving selector for the value, and a selector/exact text for its label.
- Optional literal excerpts for prose or assumptions. These require `context_text`
  matching the complete normalized node text, so changed negation/qualifications fail.
- Table column maps are expressed as `#row-id > td:nth-child(N)` field selectors;
  definition lists use the same explicit child selection. There is no heuristic search,
  fallback column matching, institution-specific scraper branch, or external parser.

Supported CSS is deliberately bounded: `#id`, `#id > tag:nth-child(N)`, and the existing
fixture selectors. IDs must resolve uniquely. HTML must be UTF-8 and explicitly balanced;
unsupported layouts (including omitted closing tags) fail closed. Review a new adapter
version for unsupported variants rather than silently recovering the page structure.

Use `extract_stored_capture(conn, store, source, fetch_run)` after an authorized direct
capture is in `ArtifactStore`. It reloads the source approval from the database, checks
its adapter/version, profile and structural pin, and records extraction/candidate history,
normalized observations, eligible T045 validation receipts, and typed scenario proofs.
Existing extraction failure and normalization failure tables retain diagnostics. The
routine neither fetches nor approves/publishes anything. The caller owns the transaction;
run IDs are append-only and may not be reused.

Confidence is reconstructed deterministically on every extraction and re-extraction:
`high` requires every required field/assumption, exact unique labels, literal evidence,
valid normalization and unchanged layout. `medium` satisfies those same checks using
verbatim fragments in pinned prose context. Any failed check is `low`; failed extractions
are recorded and never promoted. Contract records and schema versions are unchanged.
Canonical dimensions may use exact source values or explicit ranges. This first adapter
requires rate/APR, points, closed-end term and effective time; mortgages also require
purpose, rate type and lock days. Unstated dates/assumptions are never filled in. HELOC
formula/intro extraction and other vendor/browser/PDF variants remain separate work.

The canonical JSON hash is carried in provenance and re-checked against deployment data.
An edit cannot silently reinterpret a stored offer: full re-normalized values, scenario
proofs and provenance must match. Layout approval is separate from configuration; both
must match. Changing deployment configuration requires review and re-extraction.
`verify_offer_against_stored_bytes` dispatches using the persisted source approval, not
a parser supplied by an offer. Trusted loading and snapshot admission both call it.
Fixture adapters require explicit `allow_fixture=True`; the production publisher also
retains its independent reserved-host checks. Identity/source/product/first-offer review,
two captures, T045, freshness, corrections and withdrawal rules still apply. Source
onboarding, live transport authorization and independent review are not granted here.
