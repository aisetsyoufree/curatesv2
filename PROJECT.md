# CU-Rates.org V2 — Project Control Document

> **Canonical source of truth.** Every model and human contributor must read this
> document before starting a task. Workers write task handoffs under `tasks/`; only
> the product manager/integrator or Sid updates this ledger and change log.

| Field | Value |
|---|---|
| Project | CU-Rates.org V2 |
| Status | Planning and foundation |
| Product owner | Sid |
| Primary product manager / architect | OpenAI GPT-6.1 Sol through Codex |
| Repository | `cu-rates.org-v2` |
| Last updated | 2026-11-14 |
| Current milestone | M0 — Governance and specification |
| Current task | T004 — Initialize repository quality baseline |

## 1. How to use this document

Before doing any work, every model must:

1. Read this entire document and `AGENTS.md`.
2. Inspect `git status` and the recent commit history.
3. Identify the exact task ID being worked on.
4. Confirm that the task is ready and does not overlap another active task.
5. Work only within the task's scope and acceptance criteria.

Before declaring an implementation task complete, every model must:

1. Run the required tests and record the commands and results.
2. Update its task packet under `tasks/<TASK_ID>.md`.
3. Produce the handoff described in Section 13.
4. Leave unrelated files and user work untouched.

Review-only models do not edit the repository. The integrator records their reports
under `reviews/` and updates this document after evaluating the findings.

If implementation and this document disagree, stop and resolve the disagreement
in this document before continuing. The project plan must describe the system that
actually exists, not an aspirational or obsolete version of it.

## 2. Objective

Build a professional, educational, evidence-backed website and open dataset that
accounts for Massachusetts credit unions offering residential home-lending
products and helps users discover and compare publicly advertised offers.

The project has two equally important purposes:

1. Provide a useful public service for Massachusetts borrowers.
2. Serve as a disciplined learning project covering Python, data engineering,
   web scraping, testing, web development, responsible LLM use, analytics, SEO,
   advertising readiness, and Raspberry Pi operations.

This project is informational and educational. It is not a lender, broker, rate
quote service, or provider of financial advice. Advertised rates are discovery
information, not personalized offers.

## 3. Definition of success

Success does **not** mean extracting a rate from every institution. Some credit
unions publish no rates, require authentication, block automation, or quote only
after receiving borrower details.

Success means:

- Every in-scope credit union has a durable identity and a verified status.
- Every known home-lending product is cataloged, even when no rate is public.
- Every approved source belongs to the correct institution and product.
- Every published number traces to saved source evidence.
- Mortgage, refinance, home-equity loan, and HELOC terms are modeled correctly.
- Uncertain, stale, anomalous, or ungrounded data is quarantined rather than shown.
- The website clearly communicates freshness, assumptions, eligibility, and limits.
- A fresh clone can bootstrap, test, scrape the pilot set, and build the website.
- The production website and scraper run reliably on a Raspberry Pi 5.
- The system does not require an LLM to perform routine collection or publication.

## 4. V2 scope

### 4.1 Institutions

Initial scope:

- All active credit unions headquartered in Massachusetts, imported from the NCUA
  quarterly call-report data and cross-checked against the Massachusetts Division
  of Banks list.
- Credit unions offering no home-lending products remain in scope with that status.
- NCUA charter number is the durable institution identity key.
- Institution lifecycle values are `active`, `merged_into:<charter>`, `liquidated`,
  `converted`, and `under_review`.
- Only active institutions with a verified charter may publish offers.

Later expansion may include credit unions headquartered elsewhere that both accept
Massachusetts members and lend on Massachusetts property. That is explicitly out of
the first release unless added through a recorded decision.

### 4.2 Product families, purposes, and programs

Product families publishable in the first release:

- `mortgage`: first-lien fixed or ARM, conforming or jumbo
- `home_equity_loan`: closed-end fixed home-equity loan
- `heloc`: open-end home-equity line of credit

Loan purpose is an offer attribute, not a product family:

- `purchase`
- `rate_term_refinance`
- `cash_out_refinance`

One offer may apply to multiple purposes when the source says so.

Loan program is also an attribute:

- `conventional`
- `jumbo`
- `fha`
- `va`
- `usda`
- `first_time_buyer`
- `agency_partner`, including MassHousing-related programs

The first release ranks conventional and jumbo offers. Government, first-time-buyer,
and agency-partner programs are cataloged and may appear on institution detail pages,
but remain browse-only until their assumptions and fees are modeled and approved.

Classify and retain, but do not publish in the first release:

- Construction and renovation loans
- Land loans
- Reverse mortgages
- Commercial real-estate loans
- Unsecured, auto, RV, boat, motorcycle, and deposit products

### 4.3 Terminology rules

- Refinance is generally a **purpose**, not a product family.
- Interest or note rate and APR are separate fields and may not be substituted.
- APY is a deposit-product concept and is not part of the loan-rate schema.
- `points = null` means unknown; `points = 0` means explicitly zero.
- An introductory HELOC rate is not the same as its post-intro variable rate.
- A lender's published effective date, our observation time, and our publication
  time are three separate timestamps.
- Rates and other financial percentages are stored as `Decimal` values or integer
  thousandths of a percentage point, together with the original source text. Binary
  floating-point values are prohibited for financial facts.
- "As low as" rates, tiers, and conditional discounts are represented as
  `OfferCondition` records. A rate without its conditions is incomplete.
- ARM offers record initial period, adjustment interval, index, margin, and caps;
  unavailable values remain explicitly null rather than inferred.
- HELOC offers record index, margin, introductory rate and length, floor, ceiling,
  draw period, repayment period, lien position, and maximum CLTV; unavailable values
  remain explicitly null.
- A note rate without an APR may appear on an institution detail page, clearly
  labeled, but it is excluded from rankings and headline comparisons.
- All timestamps are stored in UTC and displayed in `America/New_York`.

### 4.4 Canonical comparison scenarios

The canonical scenarios are used for form-gated collection and public rankings.
Rows that do not match a scenario remain browseable but are not ranked with it.

**Mortgage purchase — 30-year fixed:** owner-occupied single-family primary
residence; purchase; conventional conforming loan; $500,000 property value;
$400,000 loan; 80% LTV; 780 credit score; 30-day lock; no relationship or autopay
discount; zero points. If a zero-point row is unavailable, no substitute is ranked.

**Mortgage rate-and-term refinance — 30-year fixed:** owner-occupied single-family
primary residence; conventional conforming loan; $500,000 property value; $400,000
balance; 80% LTV; 780 credit score; 30-day lock; no cash out; no relationship or
autopay discount; zero points.

**Fixed home-equity loan:** owner-occupied single-family primary residence; second
lien; $50,000 loan; maximum 80% CLTV; ten-year fixed term; no relationship discount.

**HELOC:** owner-occupied single-family primary residence; second lien; $50,000 line;
maximum 80% CLTV; no relationship discount. Rankings use the post-intro variable APR,
never the introductory rate, and group identical draw/repayment structures.

If a form requires geography, use an address inside the verified lending footprint
and store it with the scenario. A generated form quote is labeled as such and is not
eligible for a statewide lowest-rate headline.

## 5. Non-negotiable data principles

1. **Fail closed:** uncertainty goes to quarantine, never to the public feed.
2. **Provenance or no publication:** every public field traces to a stored artifact
   and a row, element, text span, or structured-data locator.
3. **Raw before normalized:** save the source response before extraction.
4. **Append, do not overwrite:** collection history is immutable.
5. **Deterministic first:** parsers and validation rules perform routine work.
6. **LLMs propose and audit:** an LLM never directly approves a financial value.
7. **Human-approved source identity:** a discovered URL is not trusted until its
   institution and product relationship is verified.
8. **Explicit absence:** quote-only, login-required, blocked, and no-public-rate
   states are legitimate results when supported by evidence.
9. **Comparable by default:** rankings use like-for-like scenarios and expose points
   and assumptions. Non-comparable rows may be browsed but not ranked together.
10. **No fabricated completeness:** coverage is reported by pipeline stage and status.
11. **Bounded freshness:** collection runs daily. Public offers become degraded after
    72 hours without successful re-observation and are withdrawn after 96 hours. The
    96-hour bound accommodates weekends and holidays but is never extended silently.
12. **Withdrawal on demand:** an offer, product, institution, or entire snapshot can
    be unpublished in one command, effective on the next atomic snapshot swap, with
    a recorded actor, time, and reason.
13. **Constrained auto-publication:** an existing offer may publish without renewed
    human review only when the source and parser version are approved, its normalized
    structural fingerprint is unchanged, required evidence and conditions are
    present, all validations pass, note-rate and APR changes are each no more than 50
    basis points, and product identity and point structure are unchanged. First
    publications, new products, fingerprint changes, OCR-derived values, larger
    movements, or changed points require a `ReviewDecision`.
14. **Polite, non-circumventing collection:** honor `robots.txt`, identify the project
    with a contact User-Agent, rate-limit per host, cache aggressively, and never
    bypass authentication, CAPTCHAs, or bot protection. `blocked` is a valid result.
15. **Source abandonment review:** a source-effective date older than 90 days combined
    with no value changes for 60 days triggers human review even when daily fetches
    succeed.

## 6. Target technology stack

The stack should remain small, conventional, Raspberry Pi-friendly, and easy for
future contributors to understand.

### Application

- Python 3.12 or newer
- Flask 3 with Jinja server-rendered templates
- HTMX only for focused progressive enhancement
- Tailwind CSS compiled locally; no production CDN dependency
- Pydantic v2 for boundary contracts
- SQLAlchemy 2 and Alembic for persistence and migrations
- SQLite with WAL mode and a single-writer design

### Collection and extraction

- `httpx` for HTTP collection
- `BeautifulSoup`/`lxml` for HTML parsing
- Playwright only for browser-required sources
- `pdfplumber` for text PDFs
- OCR only for image-only sources and always with human verification before first
  publication
- Content hashing to avoid re-extracting unchanged artifacts

### Quality and operations

- Pytest, Ruff, and mypy
- `uv` with a committed lockfile and a pinned, uv-managed Python 3.12 interpreter
- Tailwind standalone CLI so Node.js is not required
- HTMX vendored locally rather than loaded from a CDN
- Structured JSON logs
- Separate web and worker processes
- Raspberry Pi OS Bookworm 64-bit on Raspberry Pi 5
- NVMe/SSD storage for databases, WAL files, and artifacts; no SD-card runtime data
- Systemd timers/services on Raspberry Pi 5
- Caddy or Nginx as reverse proxy
- Atomic public snapshot generation
- Off-device database and artifact backups using the SQLite online backup API or
  Litestream, with documented restore tests
- Source may remain in the requested iCloud folder during planning, but active
  implementation worktrees, `.venv`, databases, WAL/SHM files, Playwright caches,
  and raw artifacts must live outside cloud-synchronized folders

### Deliberate exclusions

- No React or client-only SPA for the initial product
- No microservice fleet or Kubernetes
- No runtime LLM dependency
- No chatbot, user reviews, lead-selling, or advertising in the initial milestone
- No public rate without saved source evidence

## 7. Analytics, SEO, and advertising readiness

The site must be ready for future Google Analytics and AdSense use without making
advertising the product's purpose.

Required foundation:

- Server-rendered crawlable HTML
- One canonical apex origin: `https://cu-rates.org`
- Environment-configured `SITE_URL`, GA4 ID, Search Console
  verification value, and future AdSense publisher ID
- Google Analytics 4 through `gtag.js`; Google Tag Manager is out of scope unless a
  recorded decision adds it
- Google Consent Mode v2 in basic mode with `analytics_storage`, `ad_storage`,
  `ad_user_data`, and `ad_personalization` denied globally until consent is granted
- A replaceable consent-management layer; use a Google-certified CMP when required
  for the served regions and advertising configuration
- XML sitemap containing only canonical, resolving, publishable URLs
- `robots.txt`, canonical tags, Open Graph tags, and useful JSON-LD
- `ads.txt` only when a real AdSense publisher ID is configured
- Descriptive titles, headings, breadcrumbs, and internal linking
- Stable page URLs for institutions, products, methodology, and coverage
- Pages without meaningful original or publishable content use `noindex` and are
  omitted from the sitemap
- A Content Security Policy explicitly compatible with the selected Google tags
- Core Web Vitals budgets and reserved ad dimensions to prevent layout shifts
- Privacy, cookie, terms, methodology, editorial, correction, and contact pages
- Analytics events for source-link clicks, filters, exports, and freshness warnings
- No ads on quarantine, error, thin-content, or unverified-data pages
- No "best rate" or recommendation language; default sorting is by APR only within
  one canonical scenario, with rate and points visible

Ads are not activated until the product has reliable data, original educational
content, correct consent behavior, and sufficient value independent of advertising.

## 8. Target architecture

The system is a staged pipeline. Each stage is independently rerunnable and records
its inputs, outputs, failures, timestamps, and code version.

1. **Roster:** import the official MA credit-union population.
2. **Institution verification:** reconcile identity, address, homepage, eligibility,
   and lending footprint.
3. **Product catalog:** record which home-lending products each institution offers.
4. **Source registry:** verify product/source URLs and assign fetch strategies.
5. **Fetch:** collect HTML, PDF, JSON/XHR, screenshots, and HTTP metadata.
6. **Extract:** produce source-grounded candidate fields without changing meaning.
7. **Normalize:** convert candidates into canonical product-specific records.
8. **Validate:** apply deterministic field and cross-record checks.
9. **Review:** resolve quarantined or changed products through human review.
10. **Publish:** atomically produce a current, evidence-backed public snapshot.
11. **Present:** serve comparisons, institution profiles, educational material,
    coverage, methodology, and downloadable data.
12. **Monitor:** detect stale sources, structural changes, coverage regressions, and
    publication failures.

### Core data entities

- Institution
- InstitutionIdentityReview
- MembershipRule
- LendingFootprint
- ProductOffering
- OfferCondition
- ComparisonScenario
- RateSource
- FetchProfile
- FetchRun
- RawArtifact
- EvidenceLocator
- ParserVersion
- SourceFingerprint
- ExtractionRun
- ExtractedCandidate
- NormalizedOffer
- ValidationResult
- ReviewDecision
- Withdrawal
- PublishedSnapshot

Product-specific details should use typed structures rather than one enormous flat
table. Mortgage, home-equity loan, and HELOC records share a base offer contract but
retain their distinct terms.

Evidence locators are artifact-specific:

- HTML: XPath or stable CSS selector plus a hash of the normalized text span
- PDF: page number plus bounding box and extracted-text hash
- JSON/XHR: RFC 6901 JSON Pointer plus response hash

Every extraction records the Git SHA, parser ID and version, configuration hash, and
normalization version. Derived fields cite the deterministic rule ID that produced
them.

Raw artifacts live in a SHA-256 content-addressed filesystem store, not SQLite BLOBs.
The worker database stores metadata and references. Raw artifacts and screenshots are
private operational evidence; the public site exposes only source URL, observation
time, relevant text excerpt, and normalized assumptions.

The web process never opens the worker database. The publisher creates a separate
read-only `published.sqlite`, verifies it, and swaps it into place with an atomic
rename. If publication fails, the last good snapshot remains only until its bounded
freshness limit; after that, rate comparisons are withdrawn while institution and
methodology pages remain available.

## 9. Model inventory and roles

Availability changes. Before assigning a task, verify the account's current model
list rather than relying only on vendor announcements.

| Role | Preferred model | Responsibility |
|---|---|---|
| Product manager and architect | GPT-6.1 Sol / Codex | Own this document, define contracts, create task packets, resolve disagreements, and perform integration reviews |
| Primary implementation | Claude Sonnet 5.5 | Implement bounded features, migrations, tests, and fixes from approved task packets |
| Primary implementation fallback | Claude Sonnet 4.6 Thinking in Antigravity | Same role when 5.5 is unavailable |
| Long-running scraper work | Grok 4.7 Build | Source adapters, debugging, extraction investigations, and large bounded coding tasks |
| Mechanical and high-volume work | Gemini 3.8 Flash | Fixtures, test expansion, documentation, repetitive transformations, and UI content |
| Architecture review | Claude Opus 5.5 | Independent specification and design review; report first, do not edit by default |
| Architecture review fallback | Claude Opus 4.6 Thinking | Use when Opus 5.5 is unavailable |
| Adversarial audit | Grok 4.7 | Challenge product classification, source attribution, validation, and publication safety |
| Final approval | Sid | Approve scope, source disputes, major tradeoffs, and releases |

### Current locally verified model availability (2026-11-14)

- Antigravity CLI: Gemini 3.8 Flash, Claude Sonnet 4.6 Thinking, Claude Opus
  4.6 Thinking, GPT-OSS-120B.
- Grok Build CLI: Grok 4.7, Grok 4.7 Build Fast, Grok 4.6, Grok 4.5.
- Claude Code is installed; verify the account's selected model at task start.

## 10. Usage-budget strategy

GPT-6.1 Sol should perform a small fraction of total implementation work and be used
where its product context and integration judgment create the most leverage.

Use Sol for:

- Architecture and data-contract decisions
- High-quality task specifications and acceptance criteria
- Resolving reviewer disagreements
- Difficult cross-component diagnoses
- Milestone and release reviews

Do not spend Sol allowance on:

- Boilerplate or routine CRUD
- Repetitive fixture generation
- CSS polishing
- Mechanical refactoring
- Bulk source research
- Long unattended adapter implementation

Operational rules:

- One bounded task per model session.
- Prefer standard speed and medium reasoning; increase only when justified.
- Store durable context here instead of relying on long chat history.
- Use fresh sessions with a compact handoff for new milestones. A new session reduces
  context overhead but does not reset provider usage limits.
- Coding agents work in isolated branches or worktrees. Only one writer owns a task.
- Review agents produce reports unless explicitly assigned an implementation task.

## 11. Standard task prompt

Use this template when assigning work to any model:

```text
You are working on CU-Rates.org V2.

Repository: <absolute path>
Task: <TASK_ID — task title>

Before acting:
1. Read AGENTS.md and PROJECT.md completely.
2. Inspect git status and recent commits.
3. Restate the task scope, acceptance criteria, and files you expect to touch.

Rules:
- Work only on the assigned task.
- Preserve unrelated and uncommitted work.
- Do not publish or invent financial data.
- Every published-data path must remain fail-closed and source-grounded.
- Add or update tests proportional to risk.
- Run the required verification commands.
- Update tasks/<TASK_ID>.md with the handoff before completion.
- Do not edit PROJECT.md; the integrator updates the canonical ledger.

Deliver:
- Outcome summary
- Files changed
- Test commands and results
- Assumptions and unresolved risks
- Commit hash, if commits are authorized
- Recommended next task, without beginning it
```

For a review-only task, replace the update requirement with:

```text
Do not edit the repository. Return findings ordered by severity, identify explicit
agreements/disagreements, and recommend exact changes. Separate blockers from optional
improvements and avoid proposing features outside the stated objective.
```

## 12. Task ledger

Statuses: `blocked`, `ready`, `in_progress`, `review`, `done`, `deferred`.

Only one task may be `in_progress` per writer. A task is `done` only when its
acceptance criteria and verification are recorded.

### M0 — Governance and specification

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T001 | Create V2 repository and canonical control document | GPT-6.1 Sol | done | Repository folder, `PROJECT.md`, and `AGENTS.md` exist |
| T002 | Independent review of this plan | Claude Opus 5.5 | done | Review covers objective, scope, architecture, stack, risks, model workflow, and missing decisions |
| T003 | Reconcile Opus review findings | GPT-6.1 Sol + Sid | done | Accepted edits and D-01 through D-10 recorded; task dependencies corrected |
| T004 | Initialize repository quality baseline | Sonnet | ready | Active worktree and runtime paths are outside iCloud; `uv`, packaging, lint, typing, tests, and one canonical verification command are reproducible |
| T005 | Inventory reusable V1 assets | Sonnet | ready | V1 fixtures, sources, platform knowledge, content, and assets cataloged only as unverified leads; no V1 rate is trusted |
| T006 | Run Raspberry Pi platform spike | Grok Build | blocked | Await T004; Python 3.12, Playwright Chromium arm64, SQLite WAL on NVMe, and memory use verified on Pi OS Bookworm |
| T007 | Write collection, evidence, and licensing policy | GPT-6.1 Sol + Sid | ready | Robots/ToS decisions, User-Agent, rate limits, public-evidence rules, corrections, MIT code license, and CC BY 4.0 dataset/docs license recorded |

### M1 — Data contract, seed evidence, and vertical slice

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T010 | Write canonical domain data contract | GPT-6.1 Sol | ready | Institution, product, condition, scenario, source, artifact, evidence, candidate, offer, validation, withdrawal, and publication contracts approved |
| T008 | Select three seed CUs and one static vertical-slice CU | GPT-6.1 Sol + Sid | blocked | Await T005/T007/T010; seed set covers static positive, non-mortgage negative, and one difficult source |
| T031 | Implement immutable artifact store | Sonnet | blocked | Await T004/T010; every artifact is content-addressed and stores HTTP metadata, timestamps, source, hash, code version, and retention class |
| T032 | Implement minimal polite fetch worker | Grok Build | blocked | Await T007/T031; User-Agent, per-host limits, robots decision, caching, failure states, and no-circumvention rules are enforced |
| T009 | Capture source-grounded seed fixtures | Grok Build + Sid review | blocked | Await T008/T032; fixtures and manifests cite RawArtifact IDs and cover positive and negative cases |
| T011 | Finalize mortgage, home-equity, and HELOC taxonomies | Sonnet + Opus review | blocked | Await T009/T010; semantics cover seed fixtures, conditions, scenarios, and unrelated-product rejection |
| T012 | Implement database schema and migrations | Sonnet | blocked | Await T010/T011; fresh and migrated databases pass contract tests with Decimal-safe financial storage |
| T013 | Specify and implement publication-boundary invariants | Sol specifies; Sonnet implements; Gemini expands | blocked | Await T010; no offer publishes without approved institution/source/parser, current artifact, locator, conditions, validation, freshness, and review eligibility |
| T014 | Deliver one-CU static end-to-end vertical slice | Sonnet | blocked | Await T012/T013/T031/T032; one approved source flows artifact → extract → normalize → validate → `published.sqlite` → server-rendered page |

### M2 — Institution, product, and source registry

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T020 | Import current official MA NCUA roster | Sonnet | blocked | Await T004; reproducible NCUA import, MA Division of Banks cross-check metadata, lifecycle states, and reconciliation report |
| T021 | Reconcile institution identities | Grok research + Sid review | blocked | Await T020; every row matched, explicitly excepted, lifecycle-classified, or held for review |
| T022 | Catalog home-lending products | Grok research | blocked | Await T021/T031/T032; every product claim cites a RawArtifact ID and explicit status |
| T023 | Verify source URLs and fetch profiles | Grok research + Sid approval | blocked | Await T021/T031/T032; each source is institution-bound, product-bound, artifact-backed, and approved |
| T024 | Build registry validation tooling | Sonnet | blocked | Await T012/T023; duplicate identities, cross-CU URL collisions, invalid states, and missing artifact evidence fail validation |

### M3 — Representative pilot expansion

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T030 | Select 10–12 platform-diverse pilot CUs | GPT-6.1 Sol + Sid | blocked | Await T014/T023; static, PDF, vendor, iframe, JS/XHR, form-gated, quote-only, and blocked cases represented |
| T033 | Capture approved pilot fixtures | Grok Build + Sid review | blocked | Await T030/T032; reproducible sanitized fixtures and RawArtifact manifests exist for every pilot archetype |

### M4 — Extraction, normalization, and validation

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T040 | Static HTML adapter | Sonnet | blocked | Positive and negative fixtures pass with evidence locators |
| T041 | Vendor adapter framework | Grok Build | blocked | Known platforms route deterministically and cannot bypass product classification |
| T042 | PDF text adapter | Sonnet | blocked | Text PDFs extract source-grounded candidates; image-only files quarantine |
| T043 | Browser/XHR adapter | Grok Build | blocked | Dynamic sources store network evidence and deterministic scenario inputs |
| T044 | Normalize product-specific offers | Sonnet | blocked | Mortgage, home-equity, and HELOC records retain distinct semantics |
| T045 | Deterministic validation and quarantine | Sonnet + Grok audit | blocked | Field checks, family-specific APR logic, scenario matching, 50-bp review threshold, fingerprints, freshness, and reason codes are tested |

### M5 — Publication and educational website

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T050 | Atomic public snapshot builder | Sonnet | blocked | Only current validated offers publish; atomic read-only `published.sqlite`, 72/96-hour freshness transitions, emergency withdrawal, and fail-safe replacement are tested |
| T051 | Server-rendered website foundation | Sonnet | blocked | Accessible, responsive, crawlable pages with no client-only data dependency |
| T052 | CU profiles and coverage pages | Sonnet | blocked | Every in-scope CU has an honest public status and evidence links where appropriate |
| T053 | Product comparison experiences | Sonnet | blocked | Like-for-like mortgage, home-equity, and HELOC views expose assumptions and freshness |
| T054 | Educational content system | Gemini Flash + human review | blocked | Original, accurate, source-linked content with editorial metadata |
| T055 | Analytics, SEO, consent, and ad-readiness | Sonnet + Gemini verification | blocked | `gtag.js`, basic Consent Mode v2, CSP, sitemap, canonical, `noindex`, conditional `ads.txt`, structured metadata, and performance tests pass |
| T056 | Launch gate and corrections workflow | GPT-6.1 Sol + Sid | blocked | Milestone exit criteria, security headers, legal/editorial pages, correction intake, emergency withdrawal rehearsal, and content review pass |

### M6 — Raspberry Pi operations and expansion

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T060 | Pi web/worker deployment | Grok Build | blocked | Separate services, least privilege, health checks, and reproducible deployment |
| T061 | Scheduling, backups, and restore test | Sonnet | blocked | Scheduled collection, verified backups, and documented restore rehearsal |
| T062 | Freshness and regression monitoring | Sonnet | blocked | Alerts cover stale sources, zero-row regressions, structural drift, and publish failure |
| T063 | Expand by verified platform batch | Assigned per batch | blocked | Each expansion meets pilot-level evidence and test standards |
| T064 | Security hardening and adversarial audit | Grok 4.7 + Sonnet remediation | blocked | Dependency, secret, auth, CSP, input, file, network, service, and Pi exposure findings resolved or explicitly accepted |

## 13. Handoff template

Every completed implementation task must leave this handoff:

```text
Task: <ID and title>
Status: <review or done>
Outcome: <what now works>
Files changed: <list>
Verification: <commands and results>
Data impact: <none, migration, registry change, publication change>
Assumptions: <list>
Unresolved risks: <list>
Commit: <hash or not committed>
Task packet updated: yes/no
Recommended next task: <one task ID>
```

## 14. Definition of ready

A task is ready only when:

- Its dependencies are done.
- Scope and acceptance criteria are explicit.
- Its input data or fixtures are available.
- One writer owns it.
- It does not require an unresolved product-owner decision.
- A task packet exists at `tasks/<TASK_ID>.md` with scope, allowed files,
  dependencies, acceptance criteria, and exact verification commands.

## 15. Definition of done

A task is done only when:

- The acceptance criteria are satisfied.
- Relevant automated tests pass.
- Failure paths and negative cases are tested.
- Documentation describes actual behavior.
- No unrelated changes are included.
- The task packet contains the handoff, commands, results, and remaining risks.
- An independent review report is committed when the task or milestone requires one.
- The integrator has reviewed the evidence and updated the task ledger and change log.

For data work, done also requires provenance and reconciliation counts. For public
website work, done also requires accessibility, responsive layout, link crawling,
metadata, and freshness verification.

## 16. Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-11-14 | Build V2 as a clean sibling repository; preserve V1 as reference | V1 contains valuable research but excessive historical and orphaned implementation code |
| 2026-11-14 | Use server-rendered Flask/Jinja rather than a client-only SPA | Simpler Pi operation, crawlable HTML, performance, analytics, and advertising compatibility |
| 2026-11-14 | Track 100% of in-scope institutions rather than promise 100% rate extraction | Public rates are sometimes unavailable, gated, blocked, or personalized |
| 2026-11-14 | Treat LLMs as development assistants and auditors, not runtime publishers | Reliability, cost, reproducibility, and financial-data safety |
| 2026-11-14 | Use one writer per task with independent review | Prevent conflicting multi-model edits and unclear ownership |
| 2026-11-14 | Rank only offers matching a declared canonical scenario | APR and rate comparisons are meaningful only under like-for-like assumptions |
| 2026-11-14 | Withdraw offers after 96 hours without successful re-observation | A hard freshness bound is safer than silently presenting stale financial data |
| 2026-11-14 | Keep raw evidence private and publish only bounded excerpts and source links | Preserve auditability while limiting redistribution and privacy risk |
| 2026-11-14 | Keep active worktrees and mutable runtime state outside iCloud | Avoid SQLite, Playwright, and synchronization corruption on the Pi and Mac |

## 17. Review register

| Date | Reviewer | Scope | Result | Report |
|---|---|---|---|---|
| 2026-11-14 | Claude Opus 5.5 | Initial project-control document | Direction approved; four design blockers identified for T003 | `reviews/2026-11-14-opus-5.5-initial-review.md` |
| 2026-11-14 | GPT-6.1 Sol | Reconciliation of initial Opus review | Accepted design blockers and high-value recommendations; recorded D-01 through D-10 | `tasks/T003.md` |

## 18. Change log

| Date | Task | Change | Author |
|---|---|---|---|
| 2026-11-14 | T001 | Created V2 project-control document, architecture, model roles, operating protocol, and initial task ledger | GPT-6.1 Sol |
| 2026-11-14 | T002 | Recorded independent Opus 5.5 review; moved project to review reconciliation | GPT-6.1 Sol |
| 2026-11-14 | T003 | Integrated Opus findings: scenarios, conditions, freshness, publication rules, evidence model, corrected dependencies, Pi constraints, and governance | GPT-6.1 Sol |

## 19. Resolved design decisions

These decisions close the initial Opus review blockers. Reopening one requires a new
dated decision-log entry and an impact review of contracts, fixtures, and publication.

| ID | Decision | Resolution |
|---|---|---|
| D-01 | Canonical scenarios | Use the four scenarios in Section 4.4; form-generated quotes are labeled and never power statewide lowest-rate headlines |
| D-02 | Freshness | Collect daily, mark degraded after 72 hours, withdraw rate comparisons after 96 hours, and support immediate manual withdrawal |
| D-03 | Auto-publication | Permit only previously reviewed, structurally unchanged offers that satisfy every condition in Principle 13; otherwise quarantine for review |
| D-04 | First-release program scope | Rank conventional and jumbo; catalog government, agency, and first-time-buyer programs as browse-only until modeled |
| D-05 | Rate without APR | Show only on an institution detail page with an explicit label; exclude from rankings and headlines |
| D-06 | Licensing and public evidence | MIT for code, CC BY 4.0 for original documentation and exported normalized datasets; raw third-party artifacts remain private and are not relicensed or redistributed |
| D-07 | Pi platform | Raspberry Pi OS Bookworm 64-bit, Python 3.12 managed by `uv`, and NVMe/SSD for all mutable data; verify Playwright arm64 early |
| D-08 | Analytics and consent | Use GA4 `gtag.js`, not GTM; initialize Consent Mode v2 in basic mode with storage denied until consent; no advertising at launch |
| D-09 | Network edge | Use Caddy locally behind Cloudflare Tunnel/CDN, with no inbound router port; document an alternate direct-hosting path before production if the tunnel is rejected |
| D-10 | Repository and runtime location | Keep this planning repository in the requested iCloud folder, but create the active implementation worktree, virtual environment, caches, databases, WAL/SHM files, and artifacts outside iCloud in T004 |

