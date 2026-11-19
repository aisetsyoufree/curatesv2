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

Supported CSS is deliberately bounded: `#id`, then child (` > `) or descendant (` `) steps
`tag` with optional `:nth-child(N)` / `:nth-of-type(N)`, for example
`#rates tbody tr:nth-child(3) > td:nth-child(2)`, plus the existing fixture selectors. Every
selector must resolve to exactly one element. HTML must be UTF-8; unsupported layouts
(including omitted closing tags) fail closed.

Tolerant mode (`sections`, introduced in parser 1.1.0, retained in 1.2.0): list the ids of the subtrees the configuration
reads. Only those elements are parsed; the rest of the page (stray `</link>`, unclosed body,
menus) may be imbalanced and is never used as evidence, and the layout pin covers only those sections.
Imbalance, duplicate attributes or excessive depth inside a section still fail the capture, a
section id must occur once in the page, and every selector must start with a listed id.
`<sup>` footnote markers are removed from text in this mode, by tag only; `*` or `(1)` are not.
Without `sections` the whole document must be balanced as before.
A `<sup>` is stripped only as an unambiguous footnote marker: 1-2 digits or `* † ‡ §`, with
visible text before it (across element boundaries) not ending in a digit or `.`, and nothing
visible after it up to the end of the selected node. Any marker that fails this (for example
`6.12<sup>5</sup>` or `5<sup>1</sup>.5%`) fails the capture. A non-marker superscript is ordinary
text. Hidden content (`hidden`, `aria-hidden="true"` in any case, inline `display:none` or
`visibility:hidden`, `details`, `dialog`) on or under a section or inside/above a selected node is
refused, as is a section inside `noscript` or `template`. A section's ancestor tags and
attributes are pinned in the layout hash. Availability note: per-session CMS classes, nonces or
ids on those ancestors change the pin and will make captures fail low until the configuration is
re-reviewed; pick sections whose ancestors are stable.

**Sections MUST include every qualifying heading, caption, note and disclosure that gives the
rates their meaning** (which product, rate type, conditions, footnote text). Text outside the
listed sections is not pinned or used as evidence. Only the unspecified-purpose absence veto
described below scans it; other changes there cannot be detected. A label and its
value must resolve to the same table row (`tr`) unless the label is in `thead`/`tfoot` or the
field is a `group_header`; this is checked on the resolved elements at extraction.

Group sub-header rows: point a field at the sub-header cell and set `group_header: true`
(optionally with `excerpt` plus `context_text`). The value is stated by the page, but the
row/group association is structural, so such rows are at most medium confidence.

Rows whose source does not state scenario assumptions: set `browse_only: true` with the
canonical `comparison_target` and no `scenario.*` fields. Lock days, points, loan amount,
credit score and the rest stay "unstated" in `scenario_matches` (never configured as if
stated), the gate reports `scenario_unstated` and the row can only be browse-only. Points and
effective time are optional for such rows (gate: `points_unknown`, `effective_date_missing`).
Mortgage rows still require a stated purpose unless explicitly reviewed under parser 1.2.0's
`allow_unspecified_purpose: true`. That flag requires no `details.purpose` field and no
`comparison_target`; it may compose with `browse_only: true`, still without `scenario.*`
fields. The adapter checks the **whole stored page**, including outside configured sections,
for purchase/refinance/cash-out/home-buying and Spanish compra/refinanci stems; even a
negated mention refuses the row. The full UTF-8 body is entity-decoded, NFKC-normalized,
and stripped of soft hyphens and zero-width format characters. Attributes, comments and
inert markup participate in this conservative veto, as does text joined across tags.
This scan tolerates outside-section imbalance and supplies no evidence or values.
Missing purpose is normalized as `unspecified_mortgage`, labeled "Loan purpose not
stated by the source", and can only appear in browse/detail, never rankings or `/compare`.
Without the reviewed flag, unstated mortgage purpose still fails closed. Equity rows must
state lien position. Group headers, prose dates, and all visibility/footnote/row guards apply
equally to unspecified rows.

Prose dates: `date: {template, format, timezone}` on `effective_at`, e.g. template
`Rates accurate as of {value}.` with format `Month D, YYYY`. Surrounding text must match the
whole node exactly, the date is parsed strictly (no impossible dates, no other spellings) and
becomes the start of that day in the reviewed IANA timezone; medium confidence. Bodies over 2 MiB
or nesting beyond 128 elements fail with `ValueError` and low extraction confidence.
Tree traversals are iterative; field readers reuse the parsed trees during extraction.
Unspecified-purpose rows additionally scan the full page for the absence veto. Field evidence
still excludes `script`, `style`, `template`, and `noscript` subtrees. Trusted loading isolates any
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
also fail closed. T040c adds the unspecified purpose/kind under contract and taxonomy 1.1;
old 1.0 records remain readable. Deploy 1.1 readers before producing new-shape records.
Canonical dimensions may use exact source values or explicit ranges. This first adapter
requires rate/APR and closed-end term; rows without `browse_only` also require points and
effective time. Mortgages require rate type, purpose (unless the reviewed absence flag is set),
and lock days (unless browse-only). Unstated dates/assumptions are never filled in. HELOC
formula/intro extraction and other vendor/browser/PDF variants remain separate work.

The canonical JSON hash is bound into the approved adapter identity without changing
`SourceApproval`: approve `ParserVersion(name="static-html",
version=f"1.2.0+cfg.{config.sha256}")` (or use `configuration_parser(config)`). The same
identity is carried in extraction provenance and source fingerprints. An unqualified
`1.2.0` approval is insufficient. Versions 1.0.0 and 1.1.0 are no longer dispatched: 1.2.0
combines v2 with reviewed purpose absence. Canonical configuration hashes now include
`allow_unspecified_purpose`, even when false, so all static approvals need renewal. Every
configuration edit (including selectors, columns,
labels, comparison targets, field paths, and excerpts) needs a new approval identity
and re-extraction; old and newly generated offers both fail verification against a
changed configuration until approved. Layout approval remains a separate required pin.
Full re-normalized values, scenario proofs and provenance must also match. Replay ignores
only lifecycle fields and the compatible schema envelope version, so stored 1.0 offers
can replay under the 1.1 normalizer without relaxing value or provenance equality.

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
