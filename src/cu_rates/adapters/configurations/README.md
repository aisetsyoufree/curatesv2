# Static HTML source configuration

Deploy reviewed `<source_id>.json` files here. No real source is configured or approved
by this change. Tests use only `tests/fixtures/adapters/*.rates.json` and invented
`.example` pages. Raw captures remain in the private artifact store.

The JSON shape is validated by `SourceConfiguration` in `static_html.py`. It binds a
source ID/URL and charter to a reviewed layout digest and explicit offer/product IDs.
Each row declares a comparison target (or null) and field paths with:

- A uniquely resolving selector for the value, and a selector/exact text for its label.
- Numeric prose uses `context_template`, for example `Note rate: {value} (invented)`.
  Exactly one `{value}` slot captures a decimal percentage at that position; all
  surrounding text must match the complete normalized node literally. Set `excerpt`
  and `context_text` to null; literal rate excerpts/contexts fail closed. Rate changes do not change the configuration digest;
  changed negation, added qualifications, multiple values, or moved values fail closed.
- Optional literal excerpts for non-rate assumptions require `context_text` matching
  the complete normalized node text, so changed negation/qualifications fail.
- Table column maps are expressed as `#row-id > td:nth-child(N)` field selectors;
  definition lists use the same explicit child selection. There is no heuristic search,
  fallback column matching, institution-specific scraper branch, or external parser.

Supported CSS is deliberately bounded: `#id`, `#id > tag:nth-child(N)`, and the existing
fixture selectors. IDs must resolve uniquely. HTML must be UTF-8 and explicitly balanced;
unsupported layouts (including omitted closing tags) fail closed. Bodies over 2 MiB
or nesting beyond 128 elements fail with `ValueError` and low extraction confidence.
Tree traversals are iterative and each extraction parses the body once. Text excludes
`script`, `style`, `template`, and `noscript` subtrees. Trusted loading isolates any
capture that raises during verification; other captures can still be checked. Review a
new adapter version for unsupported variants rather than silently recovering the page structure.

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
are recorded and never promoted. All rows are normalized and validated before any
candidates, offers, fingerprints, or proofs are inserted. Any row exception makes the
whole extraction failed/low with zero candidates; missing products and failed validation
also fail closed. Contract records and schema versions are unchanged.
Canonical dimensions may use exact source values or explicit ranges. This first adapter
requires rate/APR, points, closed-end term and effective time; mortgages also require
purpose, rate type and lock days. Unstated dates/assumptions are never filled in. HELOC
formula/intro extraction and other vendor/browser/PDF variants remain separate work.

The canonical JSON hash is bound into the approved adapter identity without changing
`SourceApproval`: approve `ParserVersion(name="static-html",
version=f"1.0.0+cfg.{config.sha256}")` (or use `configuration_parser(config)`). The same
identity is carried in extraction provenance and source fingerprints. An unqualified
`1.0.0` approval is insufficient. Every configuration edit (including selectors, columns,
labels, comparison targets, field paths, and excerpts) needs a new approval identity
and re-extraction; old and newly generated offers both fail verification against a
changed configuration until approved. Layout approval remains a separate required pin.
Full re-normalized values, scenario proofs and provenance must also match.

The `git_sha` used for re-extraction is self-attested: verification reuses the value
in the stored offer's provenance while executing the currently deployed code. It does
not check out or authenticate historical code or prove that code produced the claim.
Approval pins the adapter/configuration identity; deployment integrity remains an
operational responsibility.
`verify_offer_against_stored_bytes` dispatches using the persisted source approval, not
a parser supplied by an offer. Trusted loading and snapshot admission both call it.
Fixture adapters require explicit `allow_fixture=True`; the production publisher also
retains its independent reserved-host checks. Identity/source/product review,
two captures, T045, freshness, corrections and withdrawal rules still apply. Source
onboarding, live transport authorization and independent review are not granted here.
