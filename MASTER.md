# CU-Rates.org V2 — Master Project Document

> **Canonical project specification.** This document owns the product vision,
> architecture, policies, technology choices, model roles, and delivery rules.
> GitHub Issues and the GitHub Project own task status and execution evidence.

| Field | Value |
|---|---|
| Product | CU-Rates.org V2 |
| Product owner | Sid |
| Product manager / architect / integrator | Claude (orchestrator), with Codex, Gemini and Claude subagents as workers and independent reviewers |
| Repository | [aisetsyoufree/curatesv2](https://github.com/aisetsyoufree/curatesv2) |
| Work tracker | [CU-Rates.org V2 GitHub Project](https://github.com/users/aisetsyoufree/projects/1) |
| Authoritative local clone | `/Users/sid/Code/cu-rates.org-v2` |
| Last updated | 2026-11-19 |

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
- A fresh clone can bootstrap, test, ingest a reproducible pilot batch, and build the website.
- The production system runs reliably on a Raspberry Pi 5.
- Models may assist with collection and extraction outside the app. Validation,
  publication, and public serving never depend on a runtime LLM.

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

Approved anonymous calculators may receive only the canonical scenario values and a ZIP
code inside the verified footprint. A source requiring a name, contact data, street
address, consent, or lead submission remains quote-only. Form-generated quotes are
labeled and never power statewide summary claims.

## 5. Non-negotiable data principles

1. **Fail closed:** uncertainty goes to quarantine, never the public feed.
2. **Provenance or no publication:** every public value has an artifact and locator.
3. **Raw before normalized:** preserve the source response before extraction.
4. **Immutable history:** append observations; do not overwrite collection history.
5. **Deterministic gate:** models can propose extractions, but rules verify evidence,
   normalize values, and decide whether offers qualify for publication.
6. **LLMs propose and audit:** no LLM directly approves a financial value.
7. **Human-approved identity:** discovered URLs are untrusted until institution and
   product relationships are verified.
8. **Explicit absence:** quote-only, login-required, blocked, and no-public-rate are
   legitimate evidence-backed results.
9. **Like-for-like comparison:** rankings use one declared scenario and expose points,
   conditions, eligibility, and coverage.
10. **No fabricated completeness:** report coverage by pipeline stage and status.
11. **Bounded freshness:** degrade after 72 hours without successful re-observation
    and withdraw rate comparisons after 96 hours. D-23 sets the pilot cadence.
12. **Independent expiry:** the web layer enforces `valid_until` even if workers fail.
13. **Emergency withdrawal:** an independent denylist can suppress a source, offer,
    product, institution, or snapshot with actor, time, and reason.
14. **Constrained auto-publication:** never bypass the gate. D-20, D-21, and D-22
    supersede the original review policy in D-03 where specified. If the current
    implementation still routes a case to review (for example, a large rate movement),
    keep it in review until a separately approved change aligns the gate. This pivot
    does not change the gate, approvals, or review requirements.
15. **Polite collection:** honor robots decisions, identify the project, rate-limit,
    cache, and never bypass authentication, CAPTCHAs, or bot protection.
16. **Source abandonment review:** an effective date older than 90 days plus no value
    change for 60 days triggers human review even when fetching still succeeds.

### Collection and source-approval policy

This policy is the minimum gate for every source. It is an engineering policy, not a
legal opinion. A source remains blocked when the policy cannot establish that automated
collection is appropriate.

**Approval status:** approved by the product owner in T007; see decision D-12. Product
approval does not substitute for source-by-source review or legal clearance.

#### Operator-led, AI-assisted lane (D-23)

1. **Discover and capture.** Codex leads public-rate research against the official
   roster. Verify the institution, product, and official or authorized-provider source.
   Use approved public evidence only. A permitted operator capture must record the
   actual URL, capture ID, UTC observation time, media type, and artifact hash; save
   the material needed to trace each value to its original row and qualifications.
   Search snippets, URLs alone, screenshots without traceable claims, and exported AI
   spreadsheets are leads, not verified observations. Do not use a model or another
   browser to route around a blocked source or automate an unapproved collection path.
2. **Extract and cross-check.** Codex proposes structured candidates from the stored
   evidence. Opus or Gemini independently reads the same artifact without seeing
   the first extraction; a human can substitute. Compare product and purpose, note
   rate and APR, term and ARM adjustment interval, points, conditions, borrower
   assumptions, and whether all public rows were found. Record tool/model and
   recipe version, independent result, and material disagreements privately.
3. **Corroborate and validate.** Import to private staging only. Independently
   resolve each material field to stored evidence before trusting it. A second
   model reading an artifact is not a second capture under D-20. Corroborating
   captures must independently support the same offer, terms, and conditions.
   When reusing the same approved page, log separate successful captures at least
   three hours apart and obey any stricter profile spacing. Identical bytes are
   acceptable; a cached result, reused capture, or HTTP 304 is not corroboration.
4. **Route and publish.** Use evidence-backed outcomes, not an LLM probability:
   - **Verified / eligible for comparison:** corroborated, current, fully supported
     claims that match a canonical scenario and pass all existing approvals and gates.
   - **Verified / browse-only:** a publishable, source-supported advertised value
     whose scenario is unstated, different, or out of ranking scope. Show the reason;
     never infer missing conditions or rank the row.
   - **Hold:** conflicting, stale, unsupported, or unverifiable claims stay private.
     Recheck the original with the operator or a reviewer; never resolve by majority
     vote. Quote-only, blocked, and no-public-rate observations are coverage findings,
     not offers.
5. **Measure.** Keep dated dispositions for every in-scope charter and separately
   report field accuracy and public-row recall against an independently checked
   inventory. Silence on one page never proves that an institution lacks a product.

This lane is a design, not an implemented importer or approval to acquire, publish,
schedule, or deploy. The source, product, privacy, retention, and withdrawal controls
still apply. Operator captures use only permitted public access; any automated
retrieval remains subject to the controls below, including D-15. No borrower inputs,
applications, lead forms, sign-ins, paywalls, or bot-block bypasses in this pilot.

#### Automated collection controls (retained; not the pilot path)

- Identify the project with one stable user agent that links to the public methodology
  and contact page. Do not impersonate a consumer browser, rotate identities, hide
  automation, or use stealth plugins. Before collection begins, the user-agent URL must
  resolve to a public project identity, methodology, and role-based contact route. During
  development the public repository, `MASTER.md`, and issue tracker satisfy this gate;
  production collection uses the website methodology and contact pages.
- Limit collection to one concurrent request per origin and per registrable domain, and
  two concurrent registrable domains globally. Wait at least five seconds between
  requests governed by the same limit and reduce frequency when a source signals
  pressure.
- Cache successful responses and validators. Do not re-download unchanged content, and
  do not fetch a public source more often than the approved schedule requires.
- Retry only transient network errors, HTTP 408, 429, and 5xx responses. Make at most
  two retries using exponential backoff with a 30-second base and 15-minute cap. Honor
  `Retry-After`; when it exceeds 15 minutes, defer collection to the next scheduled run.
  Never turn a persistent failure into a high-frequency retry loop.
- Evaluate `robots.txt` before the first automated collection and cache the decision for
  no more than 24 hours. A disallow rule blocks the affected automated path. A 401, 403,
  429, 5xx, timeout, or unreachable response with no valid cached decision pauses
  collection for that origin; other `4xx` responses mean no robots restrictions. Honor
  `Crawl-delay` when it exceeds five seconds. Evaluate robots separately for every origin
  contacted, including vendor and rendered-page XHR origins.
- Review applicable public terms during source approval and after a detected material
  change. An explicit automation prohibition, unresolved conflict, or unclear right to
  retain the required evidence blocks the source pending product-owner or counsel review.
  Store the reviewed terms URL, retrieval time, and normalized legal-text hash; re-check
  at least quarterly. A hash change triggers triage and pauses the source when the change
  may affect automation, evidence retention, or publication.
- Never bypass authentication, paywalls, CAPTCHAs, bot protection, rate limits, access
  controls, or geographic restrictions. Do not accept a vendor's hidden endpoint as
  public merely because it is technically reachable.
- Browser automation is permitted only when approved public content requires rendering.
  Record the same project identity and limits as HTTP collection; do not simulate human
  behavior to defeat controls. Block third-party advertising, analytics, and unrelated
  subresources. Dismiss a cookie banner only when doing so grants no consent and submits
  no data.
- An anonymous public calculator may receive only the approved canonical scenario and a
  ZIP code inside the verified footprint. Never submit a real or invented person, street
  address, email, telephone number, Social Security number, income, employer, consent,
  credit inquiry, application, appointment, or lead form.
- Capture only the smallest screenshot or rendered region needed for private evidence.
  Never publish captured pages, vendor responses, or full screenshots.
- Enforce limits in code, not by convention. All source requests pass through the fetch
  gateway or browser request interceptor; no other module may open source connections.
  Spacing and retry limits govern top-level navigations, documents, PDFs, and rate-bearing
  API/XHR requests. Required same-page static subresources use a separate per-render
  request, byte, and time budget; unrelated resources are blocked. Persist next-allowed
  times and backoff state in the operational database so limits survive restarts and
  overlapping timers. Use one worker lock, a monotonic clock, verified system time, and
  automated tests for spacing, concurrency, retry, robots, budgets, and denylist behavior.
- On Raspberry Pi, run collection under a dedicated unprivileged user with systemd memory
  and task limits, at most one browser instance, and owner-only artifact, cache, and
  database paths on NVMe/SSD. Off-device backup encryption is waived by D-20; expire backed-up artifacts
  within 30 days after their logged local deletion.

#### Source approval checklist and classifications

A source becomes `approved` only after the following are recorded. Verify institution
identity and product authorization. D-21 and D-22 govern the limited exceptions to
manual review; operator-led capture needs an explicit approved procedure for each source.

1. The institution identity, official domain, product relationship, and lending
   footprint are verified.
2. The rate location is an institution or authorized-provider page for that product.
3. Robots, applicable terms, authentication, form inputs, and access controls have been
   reviewed with no unresolved block.
4. The permitted capture method, schedule, limits, artifact type, and expected locators
   are named. Automated paths also name the fetch profile and responsible adapter.
   Browser profiles set tested per-render request, byte, and time budgets.
5. A fixture proves collection can occur without personal data, lead submission,
   circumvention, or unrelated browsing. Committed fixtures are synthetic or original
   and contain no third-party text; real captures remain only in the private store and
   never enter the repository, CI, or exports.
6. Retention, public excerpt, freshness, correction contact, and withdrawal behavior are
   defined.
7. Record the approval, actor, time, evidence, and conditions. Source approval does
   not approve the first observation or authorize publication.

Use these evidence-backed source classifications:

- `approved`: collection can use only the explicitly approved capture method and
  limits. This status alone does not enable a worker, scheduler, or publication.
- `quote_only`: the institution offers the product but no comparable public rate can be
  collected without borrower data or human contact.
- `login_required`: the rate is visible only after authentication.
- `no_public_rate`: the product is verified but no public rate was found after the
  approved search procedure.
- `blocked`: robots, terms, access controls, anti-bot measures, legal uncertainty, or an
  explicit owner decision prohibits collection.
- `unsupported`: the public source exists but no approved adapter can yet collect it
  reliably.
- `unknown`: identity, product, source, or collection suitability has not been verified.

`quote_only`, `login_required`, `no_public_rate`, `blocked`, `unsupported`, and `unknown`
never publish a rate. They may appear as coverage results when their evidence is fresh.
A blocked source appears only as a generic unavailable result when the product identity
is independently verified and the product owner approves display; do not publish the
block reason or identify a stop/takedown request.

A stop, takedown, or cease request; a new robots or terms prohibition; a CAPTCHA or bot
challenge; or three consecutive scheduled runs ending in 403/429 immediately moves a
source to `blocked` and denylisting. Retries within one run count as one failed run. Only
a recorded product-owner or counsel decision with actor, time, evidence, and rationale
may restore it to `approved`. Five
consecutive failed scheduled runs trigger human review and classification as
`unsupported` or `blocked`. These rules supplement the source-abandonment review in
principle 16.

### Evidence, privacy, and retention policy

- Raw HTML, JSON, PDFs, rendered DOM, images, screenshots, and vendor responses are
  private evidence. They are encrypted or access-controlled at rest, excluded from the
  public repository and exports, and never relicensed.
- Public pages may show the source URL, observation time, scenario, assumptions, review
  status, and the minimum excerpt needed to support a value. Excerpts are plain text,
  limited to the relevant label/value and nearby qualification, and never reconstruct a
  source page.
- Limit published excerpts to 300 characters per field and exclude them from CC BY 4.0
  exports. Exports contain normalized values, source URL, observation time, artifact
  hash, locator, and review status only. Original source text, excerpts, and artifacts
  are never exported; pages label excerpts as third-party text excluded from project
  licensing.
- A `304 Not Modified` response or unchanged content hash is a successful re-observation
  for freshness. Record the observation without storing a duplicate artifact.
- Preserve immutable artifact hashes, locators, collection metadata, parser version,
  validation results, review decisions, corrections, and withdrawals for the life of
  the project. Retain private third-party artifacts for 24 months after their last use in
  a public snapshot, then delete them through a logged retention job unless a dispute,
  correction, or legal hold requires longer retention.
- Retain routine failed-fetch bodies for no more than 30 days and operational logs for
  90 days. Logs must exclude query values or payloads that could contain personal data.
- The project does not intentionally collect borrower personal data. If unsolicited
  personal data arrives through contact or correction channels, restrict access, remove
  it from working records, and delete it as soon as the request can be handled safely.

### Corrections, disputes, and emergency withdrawal

- Provide a public correction channel that accepts a source URL, affected field, reason,
  and optional evidence. It must state that users must not submit applications, account
  numbers, financial records, or other personal data.
- Record each request as an append-only correction case with received time, reporter
  category, affected entity, evidence, decision, actor, and resolution time. Do not edit
  collection history to make an error disappear. Store reporter reply contact only in a
  restricted mutable field, delete it 90 days after case resolution, and never copy it
  into append-only history.
- Triage a credible active-rate, identity, privacy, or security report within one
  business day. Triage other reports within five business days. These are operational
  targets, not promises of lender response or rate availability.
- A reviewer may immediately place an offer, product, institution, source, or snapshot on
  the independent denylist. Public reads enforce the denylist even when collection and
  publication workers are unavailable.
- Correction decisions may confirm, amend, quarantine, withdraw, or reject a claim. A
  corrected value requires new evidence and normal publication validation; a reporter's
  assertion is never itself a publishable source.
- Do not forward a reporter's contact details or message to a credit union or vendor
  without explicit permission. Do not use the correction channel for marketing or leads.

### Licensing, trademarks, and legal-review boundary

- MIT applies only to original V2 code whose authorship the project can establish.
- CC BY 4.0 applies only to original V2 documentation and normalized exports. Exported
  records retain source attribution and do not grant rights in third-party content.
- Third-party pages, rate sheets, PDFs, screenshots, logos, vendor responses, and copied
  fixtures are excluded from project licensing and remain private evidence.
- Use institution and vendor names only as factual identifiers. Do not display logos,
  slogans, trade dress, or imply sponsorship, endorsement, partnership, or official
  status without documented permission or a confirmed license.
- The following remain questions for qualified counsel before a broad public launch:
  automated collection under source-specific terms; retention and excerpting of rate
  sheets and rendered pages; database or compilation rights; logo and trademark use;
  computer-access law and enforceability of website terms; accuracy, defamation, and
  disclaimer exposure; privacy and analytics obligations; advertising or affiliate
  relationships; correction/dispute procedures; and federal or Massachusetts
  mortgage-advertising rules that may apply to comparisons.

## 6. Architecture

The staged pipeline is independently rerunnable and records inputs, outputs,
failures, timestamps, configuration, and code version:

1. Import official institution roster.
2. Reconcile institution identity, membership, address, and lending footprint.
3. Catalog home-lending products and explicit absence.
4. Approve product-bound source URLs and fetch profiles.
5. Capture source evidence under the recorded approval and store it privately.
6. Stage model-extracted candidates and independent checks with field-level
   evidence, or use an existing approved adapter. Intake cannot approve or publish.
7. Normalize candidates into typed product-specific offers.
8. Apply deterministic field and cross-record validation.
9. Resolve evidence holds and complete required reviews before publication.
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

The launch candidate is an operator-led workflow with model-assisted extraction and
an offline import into SQLite. Existing adapter and transport code stays available
where a demonstrated need justifies it. The following apply to optional automated
collection work; they are not default requirements for the pilot.

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
- Local verified backups and documented restore tests (off-device and encryption waived by D-20)

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
| Product manager / architect / integrator | Claude (orchestrator), with Codex, Gemini and Claude subagents as workers and independent reviewers | Product decisions, architecture, issue specifications, disagreement resolution, integration, and final QA |
| Primary implementation | Claude Sonnet 5.5 | Bounded features, migrations, tests, and fixes from one approved issue |
| Implementation fallback | Claude Sonnet 4.6 Thinking | Same role when the preferred model is unavailable |
| Research and primary rate extraction | Codex | Find official sources and propose evidence-linked candidates; never self-certify |
| Independent rate verification | Claude Opus or Gemini | Read stored evidence independently; flag mismatched fields and missing rows |
| Bounded implementation | Grok Build | Implement scoped features and tests; adapter work only when justified |
| Mechanical/high-volume work | Gemini Flash | Coverage inventory, fixtures, test expansion, and reviewed educational drafts |
| Architecture review | Claude Opus | Independent review; report first and do not edit by default |
| Adversarial audit | Grok | Challenge attribution, taxonomy, validation, and publication safety |
| Product approval | Sid | Scope, source disputes, major tradeoffs, and release approval |

LLMs can assist development, research, and extraction. They are never the source of
record or runtime publishers. One writer owns one issue. Reviewers do not edit unless
separately assigned remediation.

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
- **M3 — Representative pilot:** 10–12 source-diverse institutions, approved public
  pages, independently checked coverage, and a real-data rehearsal.
- **M4 — Extraction and validation:** private batch intake, evidence binding,
  normalization, independent verification, validation, and quarantine.
- **M5 — Publication and educational website:** snapshots, profiles, comparisons,
  education, analytics/SEO/ad readiness, corrections, and launch gate.
- **M6 — Raspberry Pi operations and expansion:** deployment, scheduling, backup,
  monitoring, security audit, and verified expansion batches.

## 12. Resolved decisions

| ID | Decision |
|---|---|
| D-01 | Use the four declared canonical scenarios; form-generated quotes are labeled and never power statewide summary language. |
| D-02 | Original collection target: daily. Degrade after 72 hours, withdraw comparisons after 96 hours, and support immediate manual withdrawal. D-23 changes the pilot cadence, not these thresholds. |
| D-03 | Original automatic publication policy: previously reviewed, structurally unchanged offers must satisfy every evidence, condition, validation, freshness, movement, product, and points rule. See D-20 through D-22 for amendments and D-23 for the new intake lane. |
| D-04 | Rank conventional conforming offers only; keep jumbo, government, first-time-buyer, and partner programs browse-only until modeled. |
| D-05 | Rate without APR is institution-detail-only and excluded from rankings and headlines. |
| D-06 | MIT for original code; CC BY 4.0 for original documentation and exported normalized datasets; third-party artifacts remain private and are not relicensed. |
| D-07 | Use a supported Pi OS 64-bit release, Python 3.12 through `uv`, and NVMe/SSD; verify exact Pi release, RAM, Playwright arm64, and storage behavior early. |
| D-08 | Use GA4 `gtag.js`, basic Consent Mode v2 denied until consent, and no launch advertising. |
| D-09 | Use Caddy behind Cloudflare Tunnel/CDN with no inbound router port; document a fallback before production if rejected. |
| D-10 | Use the outside-iCloud clone as authoritative; the iCloud repository is an integrator-updated checkpoint mirror only. |
| D-11 | `MASTER.md` is the only durable project specification; GitHub Issues/Project replace repository task ledgers and standalone review reports. |
| D-12 | Adopt §5 as the collection, evidence, corrections, and licensing policy. Publish only verified public rate facts and their context; keep full source captures private. |
| D-13 | Risky changes (contracts, publication, collection, database, security) require an independent review by a different reviewer or vendor than the author before merge; a writer never approves its own work. The integrator merges by pull request only after `scripts/check`, the format check, and CI pass, and records evidence on the issue (D-11). Adopted 2026-11-16. |
| D-14 | Institution identity data comes from the NCUA quarterly call-report ZIP, downloaded manually by the product owner once per quarter and imported from the local file. No automated retrieval of NCUA or Mass.gov data is approved; the Division of Banks cross-check list is still to be identified. The raw file stays private and only normalized fields are published. Approved 2026-11-16 (issue #18). |
| D-15 | Live automated collection stays disabled until the gateway-owned pinned dialer (issue #68) is independently reviewed and enabled by a separate decision, and each source has its own recorded approval under §5. Having the dialer code is not approval to run it. Adopted 2026-11-16; D-23 describes the separate operator-led pilot. |
| D-16 | Workers run headless in visible terminals with Grok 4.7 (high reasoning), Gemini 3.8 Flash (high) through Antigravity, and GPT-6.1-Sol (high) through Codex. Before assigning work the orchestrator checks the worker's 5-hour and weekly quota and assigns only if both are at least 15%; Codex is reserved for independent reviews of high-risk work. Adopted 2026-11-16. |
| D-17 | The project's Raspberry Pi 5 may receive real installs of the reviewed deploy bundle (T060). Installation grants no launch permission: the worker service and timer stay stopped, disabled and masked, and the bundle's identity (commit) is recorded at install, until the D-15 conditions and a recorded per-source approval under D-18 are met. No private host details (addresses, users, key paths) enter the repository or public issues. Approved 2026-11-17. |
| D-18 | The product owner delegates authorization of bounded acquisition from a source (not publication) to the orchestrator, subordinate to every §5 and D-15 control, including human identity approval of the source and human review of its first observation and values. A source may be authorized only when robots and terms are reviewed and permit automated access, the pinned dialer (issue #68) is merged and independently reviewed, and limits and recheck intervals are set. Publication is a separate step: a value publishes only when the deterministic gate passes with at least two separately captured authoritative evidence paths (not model agreement) that match product, scenario and time. LLMs and web search may assist discovery, extraction and cross-checking but are never the source of record; unverifiable values fail closed. Each authorization is recorded append-only on the source's issue and spot-checked by a human at least quarterly; anyone may stop collection or withdraw a source, only the product owner may restore a blocked source, and the owner may revoke this delegation at any time. Approved 2026-11-17, revised after independent review. |
| D-19 | Repository governance: `main` is protected for everyone including administrators (pull request required, required `check` status, branch up to date, no force-push or deletion, conversation resolution); tags are protected by a ruleset; merged branches are deleted automatically; wiki disabled; secret scanning with push protection, private vulnerability reporting, Dependabot alerts and security updates enabled; Actions run with read-only token permissions, cannot approve pull requests, require SHA-pinned actions and require approval for outside contributors. Routine dependency updates may be merged by the orchestrator only when CI is green, the change is a development or build-tool range with no effect on contracts, publication, collection, database or security, and the merge is by pull request with evidence on the issue; anything else follows D-13. Residual risks accepted: workers share the owner's credentials today (separate least-privilege credentials are a follow-up) and workflow changes are not yet covered by an independent required reviewer. The product owner accepted T054 content and T021 on 2026-11-17; the NCUA importer mapping must still be confirmed, with the file hash, mapping and reconciliation results recorded in the issue, against the first real quarterly file (amends D-14, which keeps manual download). Approved 2026-11-17, revised after independent review. |
| D-20 | Owner decisions of 2026-11-18, replacing the earlier stricter readings. (1) Two "separately captured" evidence paths may be two captures of the same approved page taken at least 3 hours apart (the minimum spacing is set per source profile and never below 3 hours), because this is an educational site; identical bytes are acceptable, a single capture or a 304 revalidation is not. (2) Encrypted and off-device backups are not required; backups stay local, verified and access-controlled (the §5 requirement for encrypted evidence at rest is relaxed to access-controlled). (3) After a source is approved and its first observation and values were human-reviewed (D-18), later observations, including rate changes of any size that pass the deterministic gate, publish automatically when extraction confidence is high or medium; low-confidence extractions are quarantined for human re-run or review and never publish. All other controls stay: human identity approval, robots/terms, the pinned dialer, two-path corroboration, fail-closed gating, withdrawal and denylist. Approved by the product owner 2026-11-18. |
| D-21 | Owner decision of 2026-11-18, amending D-18 and D-20: the human review of a source's first observation and values is dropped. A source's first observation may publish automatically when the deterministic gate passes (two corroborating captures per D-20) and extraction confidence is high or medium; low confidence is quarantined for human re-run or review. Human identity approval of the source and institution (including Massachusetts scope), robots and terms review that permits automated access, the pinned dialer, per-source limits, the denylist and withdrawal, corrections handling and the quarterly spot-check all stay. Approved by the product owner 2026-11-18. |
| D-22 | Owner decisions of 2026-11-18 on approvals. (1) The per-product review gate stays: the product owner approves each product's definition before anything about it publishes. (2) Source approval (identity match to the roster, robots and terms permitting automated access, adapter selected, limits set) may be granted automatically by the orchestrator when every automated check passes and a dry-run extraction of the stored page has high confidence; any uncertainty (unreadable terms, ambiguous robots, medium or low confidence, an access block, a CAPTCHA) routes the source to the product owner's approval queue instead, and a source that is blocked or prohibits automated access is never auto-approved. Auto-approvals are recorded append-only on the source's issue and are covered by the quarterly human spot-check. (3) Cloudflare Tunnel exposure (T060c) happens only after the product owner has tested the site locally from the Pi. Approved by the product owner 2026-11-18. |
| D-23 | Owner-approved 2026-11-19: adopt the operator-led AI-assisted lane in §5 for the pilot and defer new scraper families unless measured coverage, accuracy, or operating effort justifies them. This supplements D-15/D-17/D-18; it does not enable the worker or waive source/product approval, D-20 corroboration, or any publication gate. Codex leads pilot research and extraction (exception to D-16); Opus, Gemini, or a human independently checks it. For the pilot, plan at least two operator-run refresh windows per week, never more than 96 hours apart, preferably no more than 72; D-02's 72/96-hour thresholds are unchanged and the production cadence depends on pilot results. This decision does not authorize scheduling. Keep any stricter current code behavior until a separate, reviewed change aligns it with prior decisions. See [pivot issue](https://github.com/aisetsyoufree/curatesv2/issues/158). |

Reopening a resolved decision requires a recorded rationale and impact review in the
relevant GitHub issue or pull request, followed by an update to this document.

## 13. Current implemented foundation

This section describes what exists in the repository, not what is planned. Live
collection remains disabled, and the operator-led batch intake and gate integration
described in D-23 are not implemented yet.

- **Toolchain:** `uv`-managed Python 3.12, committed lockfile, Ruff, strict mypy, pytest, and
  the executable `scripts/check` quality gate.
- **Contracts (`cu_rates.contracts`):** frozen, versioned Pydantic v2 records at schema 1.0
  (additive changes only), a separate taxonomy version 1.0 (product scope, out-of-scope
  products, collection method, finding and review states; findings carry evidence and no
  rate fields), and offer fingerprints (`fp1`, pinned to its original hashes, and `fp2`).
- **Evidence (`artifact_store`):** private SHA-256 content-addressed store with immutable
  per-fetch observations and tamper detection.
- **Fetch worker (`fetch_worker`, `fetch_robots`, `fetch_dialer`):** policy-gated collector
  covering robots decisions, spacing, bounded retries with `Retry-After`, the 403/429 pause
  and block rules, challenge detection, and refusal of non-global destinations and of redirects.
  The fetch worker can dispatch a pinned transport (`fetch_dialer.py`), but live collection
  remains disabled and the worker service and timer remain masked per D-15 and D-17.
- **Publication gate (`contracts.publication`):** deterministic, fail-closed evaluator with
  stable reason codes: approved source and fetch-profile digest, current evidence, review and
  fingerprint binding, scenario evidence, corrections, source/offer/product/institution/
  snapshot withdrawals, and HTTP 304 refresh only with a matching strong ETag. A scenario
  match is never publication approval.
- **Normalization and validation (`normalize`, `validation`):** pure deterministic layers.
  Normalization turns extracted candidates into CANDIDATE offers that preserve source text,
  locators, and rule IDs and keep null, zero, unknown, and not-applicable distinct.
  Validation runs ten versioned rules and returns quarantine records with their evidence.
- **Database (`db`, `alembic`):** SQLAlchemy 2 and Alembic over SQLite in WAL mode, frozen
  migrations in chain order (`t012_initial`, `t020_roster_import`, `t014_trusted_records`,
  `aud4b_roster_guards`, `aud73_unique_guards`, `aud98_canonical_dimensions`,
  `aud94_identity_reviews`, `t056a_source_review_requests`), enforced foreign keys,
  composite institution-bound references, and append-only history guarded by triggers
  and an engine check.
- **Roster import (`roster`):** repeatable, source-versioned import from a local CSV with the
  raw file kept as evidence; it never marks a charter verified.
- **Offline vertical slice and adapters (`pipeline`, `adapters`):** synthetic pipeline from
  mock fetch to a reviewed publication and a verified 304 republication; real static HTML
  adapter v2 (`adapters.static_html`) with configuration registry and candidate normalization.
- **Website foundation (`web`):** server-rendered Flask pages with strict headers and no
  third-party requests, optional analytics disabled, educational articles that are all
  marked unreviewed (draft banner, `noindex`, excluded from the sitemap) until a human
  reviews them, comparison views reading sealed snapshots, and institution profile pages
  (`/credit-unions`). It never opens the worker database.
- **Public snapshots and denylists (`snapshot`):** atomic public snapshot builder and reader,
  versioned publisher, and independent operator and automatic suppression denylists.
- **Correction intake and operations (`intake`, `ops`, `review`):** private append-only
  correction inbox database with triage CLI and contact purging; local verified backup/restore/prune;
  periodic health and data quality checks; and private operator review console CLI and local server.

Task status and next work are intentionally not duplicated here; use the GitHub Project and
Issues.
