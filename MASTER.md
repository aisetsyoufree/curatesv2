# CU-Rates.org V2 — Master Project Document

> **Canonical project specification.** This document owns the product vision,
> architecture, policies, technology choices, model roles, and delivery rules.
> GitHub Issues and the GitHub Project own task status and execution evidence.

| Field | Value |
|---|---|
| Product | CU-Rates.org V2 |
| Product owner | Sid |
| Product manager / architect | OpenAI GPT-6.1 Sol through Codex |
| Repository | [aisetsyoufree/curatesv2](https://github.com/aisetsyoufree/curatesv2) |
| Work tracker | [CU-Rates.org V2 GitHub Project](https://github.com/users/aisetsyoufree/projects/1) |
| Authoritative local clone | `/Users/sid/Code/cu-rates.org-v2` |
| Last updated | 2026-11-15 |

## 1. Vision and objective

Build a professional, educational, evidence-backed website and open normalized
dataset that accounts for Massachusetts credit unions offering residential
home-lending products and helps people discover and compare publicly advertised
offers.

The project has two equal purposes:

1. Provide a useful public service for Massachusetts borrowers.
2. Serve as a disciplined learning project covering Python, data engineering,
   responsible collection, testing, web development, LLM-assisted delivery,
   analytics, SEO, advertising readiness, and Raspberry Pi operations.

The product is informational and educational. It is not a lender, broker, quote
service, financial adviser, or substitute for lender disclosures. Advertised offers
are discovery information, not personalized offers.

## 2. Definition of success

Success does not mean extracting a rate from every institution. Some institutions
publish no rates, require authentication, block automation, or quote only after
receiving borrower information.

Success means:

- Every in-scope credit union has a durable identity and verified status.
- Every known home-lending product is cataloged, including explicit absence states.
- Every approved source belongs to the correct institution and product.
- Every published financial value traces to stored source evidence.
- Mortgage, refinance, fixed home-equity, and HELOC semantics remain distinct.
- Uncertain, stale, anomalous, or ungrounded data is quarantined.
- Public pages expose freshness, assumptions, eligibility, coverage, and limitations.
- A fresh clone can bootstrap, test, collect the pilot, and build the website.
- The production system runs reliably on a Raspberry Pi 5.
- Routine collection and publication require no runtime LLM.

## 3. Scope

### Institutions

The first release covers all active credit unions headquartered in Massachusetts,
imported from current NCUA data and cross-checked against the Massachusetts Division
of Banks. Credit unions without a verified home-lending product remain in the
directory with that status.

- NCUA charter number is the durable institution key.
- Lifecycle states are `active`, `merged`, `liquidated`, `converted`, and
  `under_review`; a merger stores its successor charter separately.
- Only active institutions with a verified charter may publish offers.
- Out-of-state credit unions serving Massachusetts are a later expansion.

### Product families

- `mortgage`: first-lien fixed or adjustable-rate mortgage
- `home_equity_loan`: closed-end fixed home-equity loan
- `heloc`: open-end home-equity line of credit

Loan purpose is separate from product family:

- `purchase`
- `rate_term_refinance`
- `cash_out_refinance`

Program dimensions are composable:

- `conforming_status`: `conforming`, `high_balance_conforming`, `jumbo`, `unknown`
- `government_guaranty`: `none`, `fha`, `va`, `usda`, `unknown`
- `borrower_programs`: may include `first_time_buyer`
- `partner_programs`: may include MassHousing-related programs

The first release ranks only conventional conforming offers. Jumbo, government,
first-time-buyer, and partner-program offers are browse-only until their scenarios,
eligibility, and fees are modeled and approved.

Construction, renovation, land, reverse-mortgage, commercial, unsecured, vehicle,
and deposit products are classified when encountered but not published in the first
release.

## 4. Financial terminology and comparison rules

- Refinance is a purpose, not a product family.
- Note rate and APR are separate and cannot be substituted.
- APY is not part of the loan-rate schema.
- `points = null` means unknown; `points = 0` means explicitly zero.
- Introductory HELOC rates are distinct from post-intro variable rates.
- Source effective time, observation time, and publication time are separate.
- Financial percentages use `Decimal` or lossless scaled integers, never binary float.
- Original source text is retained alongside normalized values.
- "As low as" values, tiers, discounts, and borrower requirements are conditions.
- A note rate without APR may be shown with an explicit label on an institution page
  but cannot enter rankings or headline comparisons.
- Timestamps are stored in UTC and displayed in `America/New_York`.

ARM records include initial period, adjustment interval, index, margin, and caps.
HELOC records include index, margin, intro rate and duration, floor, ceiling, draw and
repayment periods, lien position, and maximum CLTV. Missing values remain null and are
never inferred silently.

### Canonical scenarios

Rankings require an exact source-stated value or a published range containing the
scenario value. `matches`, `conflicts`, and `unstated` are distinct states; unstated
assumptions remain browse-only until evidence supports a narrower rule.

**Mortgage purchase — 30-year fixed:** owner-occupied single-family primary
residence; conventional conforming purchase; $500,000 property; $400,000 loan; 80%
LTV; 780 credit score; 30-day lock; zero points; no relationship or autopay discount.

**Rate-and-term refinance — 30-year fixed:** owner-occupied single-family primary
residence; conventional conforming; $500,000 property; $400,000 balance; 80% LTV;
780 credit score; 30-day lock; no cash out; zero points; no relationship or autopay
discount.

**Fixed home-equity loan:** owner-occupied single-family primary residence; $500,000
property; $350,000 first lien; $50,000 second-lien loan; 80% CLTV; 780 credit score;
ten-year fixed term; no relationship discount.

**HELOC:** owner-occupied single-family primary residence; $500,000 property;
$350,000 first lien; $50,000 second-lien line; 80% CLTV; 780 credit score; no
relationship discount. Rankings use post-intro variable APR and group identical
draw/repayment structures.

Approved anonymous calculators may receive only a ZIP code inside the verified
footprint. A source requiring a name, contact data, street address, consent, or lead
submission remains quote-only. Form-generated quotes are labeled and never power
statewide summary claims.

## 5. Non-negotiable data principles

1. **Fail closed:** uncertainty goes to quarantine, never the public feed.
2. **Provenance or no publication:** every public value has an artifact and locator.
3. **Raw before normalized:** preserve the source response before extraction.
4. **Immutable history:** append observations; do not overwrite collection history.
5. **Deterministic routine:** parsers and rules perform normal extraction and checks.
6. **LLMs propose and audit:** no LLM directly approves a financial value.
7. **Human-approved identity:** discovered URLs are untrusted until institution and
   product relationships are verified.
8. **Explicit absence:** quote-only, login-required, blocked, and no-public-rate are
   legitimate evidence-backed results.
9. **Like-for-like comparison:** rankings use one declared scenario and expose points,
   conditions, eligibility, and coverage.
10. **No fabricated completeness:** report coverage by pipeline stage and status.
11. **Bounded freshness:** collect daily; degrade after 72 hours without successful
    re-observation and withdraw rate comparisons after 96 hours.
12. **Independent expiry:** the web layer enforces `valid_until` even if workers fail.
13. **Emergency withdrawal:** an independent denylist can suppress an offer, product,
    institution, or snapshot with actor, time, and reason.
14. **Constrained auto-publication:** previously reviewed offers may republish only
    when source and parser remain approved, structure is unchanged, conditions and
    evidence are complete, validations pass, point structure and product identity are
    unchanged, and note-rate/APR movements are no more than 50 basis points. All first
    publications, new products, structural changes, OCR values, larger movements, or
    changed points require review.
15. **Polite collection:** honor robots decisions, identify the project, rate-limit,
    cache, and never bypass authentication, CAPTCHAs, or bot protection.
16. **Source abandonment review:** an effective date older than 90 days plus no value
    change for 60 days triggers human review even when fetching still succeeds.

## 6. Architecture

The staged pipeline is independently rerunnable and records inputs, outputs,
failures, timestamps, configuration, and code version:

1. Import official institution roster.
2. Reconcile institution identity, membership, address, and lending footprint.
3. Catalog home-lending products and explicit absence.
4. Approve product-bound source URLs and fetch profiles.
5. Collect raw HTML, PDF, JSON/XHR, rendered DOM, images, and HTTP metadata.
6. Extract source-grounded candidates without changing meaning.
7. Normalize candidates into typed product-specific offers.
8. Apply deterministic field and cross-record validation.
9. Review quarantined, new, or structurally changed offers.
10. Build and verify an atomic public snapshot.
11. Serve comparisons, profiles, education, methodology, coverage, and exports.
12. Monitor freshness, structural drift, coverage regression, and publication health.

### Core entities

- Institution, InstitutionIdentityReview, MembershipRule, LendingFootprint
- ProductOffering, OfferCondition, ComparisonScenario
- ServiceProvider, InstitutionSourceRelationship, RateSource, FetchProfile
- FetchRun, RawArtifact, EvidenceLocator
- ParserVersion, SourceFingerprint, ExtractionRun, ExtractedCandidate
- NormalizedOffer, ValidationResult, ReviewDecision, Correction, Withdrawal
- PublishedSnapshot

Mortgage, fixed home-equity, and HELOC records share a base offer but keep typed
product-specific details.

### Evidence and storage

- HTML locators use XPath or stable CSS plus normalized-text hash.
- Rendered DOM locators add frame path, rendered-text hash, and response IDs.
- PDF locators use page, bounding box, and extracted-text hash.
- Image/OCR locators use image region, image hash, and recognized-text hash.
- JSON/XHR locators use RFC 6901 JSON Pointer plus response hash.
- Every extraction stores Git SHA, parser/version, configuration hash, and
  normalization version; derived fields cite a deterministic rule ID.
- Raw artifacts use a private SHA-256 content-addressed filesystem store. The public
  product exposes source URL, observation time, bounded excerpt, and assumptions—not
  raw third-party artifacts.

The web process never opens the worker database. Publication creates a verified,
versioned `published.sqlite` in rollback-journal mode, exposes it read-only/immutable
through an atomic pointer swap, and reopens it on version change. Public snapshots do
not use WAL sidecars. Read-time expiry and the emergency denylist remain effective
when collection or publication is down.

## 7. Technology stack

### Application

- Python 3.12, pinned with `uv` and a committed lockfile
- Flask 3 with Jinja server-rendered templates
- HTMX only for focused progressive enhancement, vendored locally
- Tailwind standalone CLI, compiled locally
- Pydantic v2 contracts
- SQLAlchemy 2 and Alembic
- SQLite with WAL for the single-writer operational database

### Collection

- `httpx` for HTTP
- BeautifulSoup/lxml for HTML
- Playwright only when a browser is required
- `pdfplumber` for text PDFs
- OCR only for image-only sources and human-reviewed first publication
- Content hashing to avoid reprocessing unchanged artifacts

### Quality and operations

- Pytest, Ruff, strict mypy, and `scripts/check`
- Structured JSON logs
- Separate web and worker processes
- A supported Raspberry Pi OS 64-bit release on Raspberry Pi 5
- NVMe/SSD for databases, WAL, caches, and artifacts; no SD-card runtime state
- Systemd services/timers and Caddy
- Cloudflare Tunnel/CDN with no inbound router port, subject to deployment review
- Off-device backups and documented restore tests

The authoritative clone, virtual environment, databases, caches, Playwright data,
and artifacts stay outside cloud-synchronized folders. The iCloud copy is only an
integrator-updated checkpoint mirror.

### Deliberate exclusions

- No React/client-only SPA, microservice fleet, or Kubernetes initially
- No runtime LLM, chatbot, user reviews, lead selling, or personalized pricing
- No advertising at launch
- No public financial value without saved evidence

## 8. Website, analytics, SEO, and advertising readiness

- Canonical origin: `https://cu-rates.org`
- Server-rendered, crawlable, accessible, responsive HTML
- GA4 through `gtag.js`; no GTM without a later decision
- Basic Google Consent Mode v2 with storage denied until consent
- Replaceable consent layer; certified CMP only when required
- Canonical URLs, sitemap, robots, breadcrumbs, Open Graph, and conservative JSON-LD
- Thin or unverified pages use `noindex` and stay out of the sitemap
- `ads.txt` only when a real publisher ID exists
- CSP compatible with the chosen Google tags
- Core Web Vitals budgets and reserved future ad dimensions
- Privacy, cookies, terms, methodology, editorial, corrections, and contact pages
- No ads on quarantine, error, thin, or unverified pages
- No "best rate" or recommendation language; APR sorting occurs only inside one
  canonical scenario with rate, points, eligibility, freshness, and coverage visible

Ads remain disabled until the product offers reliable data, original educational
content, correct consent behavior, and independent user value.

## 9. Delivery roles

Model availability is verified immediately before assignment.

| Role | Preferred model | Responsibility |
|---|---|---|
| Product manager / architect / integrator | GPT-6.1 Sol through Codex | Product decisions, architecture, issue specifications, disagreement resolution, integration, and final QA |
| Primary implementation | Claude Sonnet 5.5 | Bounded features, migrations, tests, and fixes from one approved issue |
| Implementation fallback | Claude Sonnet 4.6 Thinking | Same role when the preferred model is unavailable |
| Scraper and long-running implementation | Grok Build | Adapters, collection debugging, and bounded implementation |
| Mechanical/high-volume work | Gemini Flash | Fixtures, test expansion, repetitive transformation, and reviewed educational drafts |
| Architecture review | Claude Opus | Independent review; report first and do not edit by default |
| Adversarial audit | Grok | Challenge attribution, taxonomy, validation, and publication safety |
| Product approval | Sid | Scope, source disputes, major tradeoffs, and release approval |

LLMs are development assistants and auditors, never runtime publishers. One writer
owns one issue. Reviewers do not edit unless separately assigned remediation.

## 10. Work tracking and enterprise delivery rules

GitHub Issues are the source of truth for stories. The GitHub Project is the source of
truth for status, priority, milestone, and sequencing. Do not recreate a task ledger
in this file.

Workflow:

`Backlog → Ready → In Progress → In Review → QA → Done`

Each issue contains:

- Stable task ID and milestone
- Objective and explicit non-goals
- Owner/model and dependencies
- Allowed change scope
- Acceptance criteria
- Exact verification commands or required evidence
- Data, migration, publication, and security impact
- Completion evidence, commit/PR, review result, and residual risks

### Definition of Ready

- Dependencies are done.
- Scope, non-goals, acceptance criteria, and required inputs are explicit.
- Fixtures exist when the task consumes them.
- One writer owns it.
- Required product decisions are resolved.
- Verification is named before implementation starts.

### Definition of Done

- Acceptance criteria are satisfied.
- Positive, negative, and failure-path tests proportional to risk pass.
- Independent code or design review is complete.
- Documentation describes actual behavior.
- No unrelated changes or secrets are included.
- Data work includes provenance and reconciliation counts.
- Public-site work includes accessibility, responsive layout, link, metadata, and
  freshness checks.
- The issue records commands/results, commit or PR, impact, assumptions, residual
  risks, and reviewer approval.
- Any changed architecture or product rule is updated here in the same change.

Workers use branches named `task/<ID>-<short-name>`. The integrator reviews and merges
accepted work. Workers do not merge their own branches or begin the next issue.

Review reports are not permanent standalone documents. Task-specific findings live
in the issue/PR; durable product or architecture decisions are incorporated here.

## 11. Milestone roadmap

- **M0 — Foundation and governance:** repository baseline, V1 inventory, collection
  policy, and Pi feasibility.
- **M1 — Contract and vertical slice:** canonical contract, seed evidence, storage,
  fetch worker, invariants, and one static CU end to end.
- **M2 — Institution/product/source registry:** official roster, identity, products,
  source profiles, and registry validation.
- **M3 — Representative pilot:** 10–12 platform-diverse institutions and fixtures.
- **M4 — Extraction and validation:** HTML, vendor, PDF, browser/XHR adapters,
  normalization, validation, and quarantine.
- **M5 — Publication and educational website:** snapshots, profiles, comparisons,
  education, analytics/SEO/ad readiness, corrections, and launch gate.
- **M6 — Raspberry Pi operations and expansion:** deployment, scheduling, backup,
  monitoring, security audit, and verified expansion batches.

## 12. Resolved decisions

| ID | Decision |
|---|---|
| D-01 | Use the four declared canonical scenarios; form-generated quotes are labeled and never power statewide summary language. |
| D-02 | Collect daily, degrade after 72 hours, withdraw comparisons after 96 hours, and support immediate manual withdrawal. |
| D-03 | Auto-publish only previously reviewed, structurally unchanged offers satisfying every evidence, condition, validation, freshness, movement, product, and points rule. |
| D-04 | Rank conventional conforming offers only; keep jumbo, government, first-time-buyer, and partner programs browse-only until modeled. |
| D-05 | Rate without APR is institution-detail-only and excluded from rankings and headlines. |
| D-06 | MIT for original code; CC BY 4.0 for original documentation and exported normalized datasets; third-party artifacts remain private and are not relicensed. |
| D-07 | Use a supported Pi OS 64-bit release, Python 3.12 through `uv`, and NVMe/SSD; verify exact Pi release, RAM, Playwright arm64, and storage behavior early. |
| D-08 | Use GA4 `gtag.js`, basic Consent Mode v2 denied until consent, and no launch advertising. |
| D-09 | Use Caddy behind Cloudflare Tunnel/CDN with no inbound router port; document a fallback before production if rejected. |
| D-10 | Use the outside-iCloud clone as authoritative; the iCloud repository is an integrator-updated checkpoint mirror only. |
| D-11 | `MASTER.md` is the only durable project specification; GitHub Issues/Project replace repository task ledgers and standalone review reports. |

Reopening a resolved decision requires a recorded rationale and impact review in the
relevant GitHub issue or pull request, followed by an update to this document.

## 13. Current implemented foundation

The repository currently has:

- `uv`-managed Python 3.12 with `.python-version`, PEP 621 metadata, and `uv.lock`
- Minimal typed `cu_rates` package
- Ruff, strict mypy, pytest, and the executable `scripts/check` quality gate
- Smoke tests, clean-environment verification, package build verification, and
  fail-fast proof for the quality gate

Task status and next work are intentionally not duplicated here; use the GitHub
Project and Issues.
