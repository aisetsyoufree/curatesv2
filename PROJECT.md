# CU-Rates.org V2 — Project Control Document

> **Canonical source of truth.** Every model and human contributor must read this
> document before starting a task and update the task ledger and change log before
> declaring work complete.

| Field | Value |
|---|---|
| Project | CU-Rates.org V2 |
| Status | Planning and foundation |
| Product owner | Sid |
| Primary product manager / architect | OpenAI GPT-6.1 Sol through Codex |
| Repository | `cu-rates.org-v2` |
| Last updated | 2026-11-14 |
| Current milestone | M0 — Governance and specification |
| Current task | T003 — Reconcile Opus 5.5 review findings |

## 1. How to use this document

Before doing any work, every model must:

1. Read this entire document and `AGENTS.md`.
2. Inspect `git status` and the recent commit history.
3. Identify the exact task ID being worked on.
4. Confirm that the task is ready and does not overlap another active task.
5. Work only within the task's scope and acceptance criteria.

Before declaring a task complete, every model must:

1. Run the required tests and record the commands and results.
2. Update the task ledger in this document.
3. Add a short entry to the change log.
4. Produce the handoff described in Section 13.
5. Leave unrelated files and user work untouched.

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

- Active, NCUA-insured credit unions headquartered in Massachusetts.
- NCUA charter number is the preferred durable identity key.
- Institutions without a verified charter remain in review and cannot publish rates.

Later expansion may include credit unions headquartered elsewhere that both accept
Massachusetts members and lend on Massachusetts property. That is explicitly out of
the first release unless added through a recorded decision.

### 4.2 Product families

Catalog and eventually publish:

- First-lien purchase mortgages
- Rate-and-term mortgage refinances
- Cash-out mortgage refinances
- Fixed home-equity loans
- Home-equity lines of credit (HELOCs)

Classify and retain, but do not publish in the first release unless scope changes:

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
- Structured JSON logs
- Separate web and worker processes
- Systemd timers/services on Raspberry Pi 5
- Caddy or Nginx as reverse proxy
- Atomic public snapshot generation
- Automated database and artifact backups

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
- Environment-configured `SITE_URL`, GA4 ID, Tag Manager ID, Search Console
  verification value, and future AdSense publisher ID
- Google Analytics 4 through the Google tag or Google Tag Manager
- Google Consent Mode v2 with denied defaults until the user's applicable consent
  choice is known
- A replaceable consent-management layer; use a Google-certified CMP when required
  for the served regions and advertising configuration
- XML sitemap containing only canonical, resolving, publishable URLs
- `robots.txt`, `ads.txt`, canonical tags, Open Graph tags, and useful JSON-LD
- Descriptive titles, headings, breadcrumbs, and internal linking
- Stable page URLs for institutions, products, methodology, and coverage
- Core Web Vitals budgets and reserved ad dimensions to prevent layout shifts
- Privacy, cookie, terms, methodology, editorial, correction, and contact pages
- Analytics events for source-link clicks, filters, exports, and freshness warnings
- No ads on quarantine, error, thin-content, or unverified-data pages

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
- RateSource
- FetchProfile
- FetchRun
- RawArtifact
- ExtractionRun
- ExtractedCandidate
- NormalizedOffer
- ValidationResult
- ReviewDecision
- PublishedSnapshot

Product-specific details should use typed structures rather than one enormous flat
table. Mortgage, home-equity loan, and HELOC records share a base offer contract but
retain their distinct terms.

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
- Update PROJECT.md task status and change log before completion.

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
| T003 | Reconcile review findings | GPT-6.1 Sol + Sid | ready | Accepted changes recorded; disagreements and owner decisions documented |
| T004 | Initialize repository quality baseline | Sonnet | blocked | Await T003; packaging, lint, typing, tests, and CI commands are reproducible |

### M1 — Data contract and schema

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T010 | Write canonical domain data contract | GPT-6.1 Sol | blocked | Await T003; institution, product, source, artifact, candidate, offer, validation, and publication contracts approved |
| T011 | Define mortgage, home-equity, and HELOC taxonomies | Sonnet + Opus review | blocked | Product and purpose semantics cover fixtures and reject unrelated loans |
| T012 | Implement database schema and migrations | Sonnet | blocked | Fresh and migrated databases pass contract tests |
| T013 | Add provenance and publication-boundary tests | Gemini Flash | blocked | No offer can publish without approved source, artifact, evidence locator, and passed validation |

### M2 — Institution, product, and source registry

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T020 | Import current official MA NCUA roster | Sonnet | blocked | Reproducible import with source metadata and reconciliation report |
| T021 | Reconcile institution identities | Grok research + human review | blocked | Every row matched, explicitly excepted, or held for review |
| T022 | Catalog home-lending products | Grok research | blocked | Every in-scope CU has evidence-backed product statuses |
| T023 | Verify source URLs and fetch profiles | Grok research + Sid approval | blocked | Each source is institution-bound, product-bound, evidence-backed, and approved |
| T024 | Build registry validation tooling | Sonnet | blocked | Duplicate identities, cross-CU URL collisions, invalid statuses, and missing evidence fail validation |

### M3 — Representative pilot and raw collection

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T030 | Select 10–12 platform-diverse pilot CUs | GPT-6.1 Sol + Sid | blocked | Static, PDF, vendor, iframe, JS/XHR, form-gated, quote-only, and hostile cases represented |
| T031 | Implement immutable artifact store | Sonnet | blocked | Every fetch stores content, hash, metadata, timestamps, source, and code version |
| T032 | Implement polite fetch worker | Grok Build | blocked | Rate limiting, caching, identification, failure states, and robots decisions recorded |
| T033 | Capture approved pilot fixtures | Grok Build + human review | blocked | Reproducible sanitized fixtures and metadata exist for every pilot archetype |

### M4 — Extraction, normalization, and validation

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T040 | Static HTML adapter | Sonnet | blocked | Positive and negative fixtures pass with evidence locators |
| T041 | Vendor adapter framework | Grok Build | blocked | Known platforms route deterministically and cannot bypass product classification |
| T042 | PDF text adapter | Sonnet | blocked | Text PDFs extract source-grounded candidates; image-only files quarantine |
| T043 | Browser/XHR adapter | Grok Build | blocked | Dynamic sources store network evidence and deterministic scenario inputs |
| T044 | Normalize product-specific offers | Sonnet | blocked | Mortgage, home-equity, and HELOC records retain distinct semantics |
| T045 | Deterministic validation and quarantine | Sonnet + Grok audit | blocked | Field checks, market/context checks, change detection, and reason codes are tested |

### M5 — Publication and educational website

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T050 | Atomic public snapshot builder | Sonnet | blocked | Only current validated offers publish; previous good snapshot survives failure |
| T051 | Server-rendered website foundation | Sonnet | blocked | Accessible, responsive, crawlable pages with no client-only data dependency |
| T052 | CU profiles and coverage pages | Sonnet | blocked | Every in-scope CU has an honest public status and evidence links where appropriate |
| T053 | Product comparison experiences | Sonnet | blocked | Like-for-like mortgage, home-equity, and HELOC views expose assumptions and freshness |
| T054 | Educational content system | Gemini Flash + human review | blocked | Original, accurate, source-linked content with editorial metadata |
| T055 | Analytics, SEO, consent, and ad-readiness | Sonnet + Gemini verification | blocked | GA4/GTM, Consent Mode v2, sitemap, canonical, metadata, `ads.txt`, and performance tests pass |

### M6 — Raspberry Pi operations and expansion

| ID | Task | Owner | Status | Acceptance criteria |
|---|---|---|---|---|
| T060 | Pi web/worker deployment | Grok Build | blocked | Separate services, least privilege, health checks, and reproducible deployment |
| T061 | Scheduling, backups, and restore test | Sonnet | blocked | Scheduled collection, verified backups, and documented restore rehearsal |
| T062 | Freshness and regression monitoring | Sonnet | blocked | Alerts cover stale sources, zero-row regressions, structural drift, and publish failure |
| T063 | Expand by verified platform batch | Assigned per batch | blocked | Each expansion meets pilot-level evidence and test standards |

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
PROJECT.md updated: yes/no
Recommended next task: <one task ID>
```

## 14. Definition of ready

A task is ready only when:

- Its dependencies are done.
- Scope and acceptance criteria are explicit.
- Its input data or fixtures are available.
- One writer owns it.
- It does not require an unresolved product-owner decision.

## 15. Definition of done

A task is done only when:

- The acceptance criteria are satisfied.
- Relevant automated tests pass.
- Failure paths and negative cases are tested.
- Documentation describes actual behavior.
- No unrelated changes are included.
- The task ledger and change log are updated.
- The handoff identifies remaining risks honestly.

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

## 17. Review register

| Date | Reviewer | Scope | Result | Report |
|---|---|---|---|---|
| 2026-11-14 | Claude Opus 5.5 | Initial project-control document | Direction approved; four design blockers identified for T003 | `reviews/2026-11-14-opus-5.5-initial-review.md` |

## 18. Change log

| Date | Task | Change | Author |
|---|---|---|---|
| 2026-11-14 | T001 | Created V2 project-control document, architecture, model roles, operating protocol, and initial task ledger | GPT-6.1 Sol |
| 2026-11-14 | T002 | Recorded independent Opus 5.5 review; moved project to review reconciliation | GPT-6.1 Sol |

