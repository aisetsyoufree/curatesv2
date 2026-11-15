# Round-2 audit synthesis

Date: 2026-11-14

Integrator: GPT-6.1 Sol / Codex

## Bottom line

All three independent reviewers returned `APPROVE_WITH_CHANGES`. None recommended a
new planning cycle. T004 is the correct next task after the accepted packet fixes.

## Accepted now

| Finding | Agreement | Decision |
|---|---|---|
| Jumbo ranked without a scenario | Opus, Grok, Gemini | Correct. Make jumbo browse-only for the first release; no speculative jumbo scenario or conforming-limit service |
| iCloud worktree still stores Git metadata in iCloud | Opus | Correct and technically material. T004 creates a full authoritative clone outside iCloud and freezes the current copy as a planning mirror |
| T004 promised one command but listed four | Opus | Correct. Add `scripts/check` and keep individual checks underneath it |
| T005 claimed parallel execution under the same writer | Opus, Grok, Gemini | Correct. Block T005 on T004 |
| T010 needs T004's reproducible tooling | Gemini; supported by Opus | Correct. Block T010 on T004 and name its contract test command |
| Program taxonomy mixed separate dimensions | Opus; Grok noted precedence gap | Correct. Separate conforming status, government guaranty, borrower programs, and partner programs |
| Home-equity/HELOC scenarios lacked scenario inputs | Opus | Correct. Add property value, first lien, CLTV, and credit score |
| Shared vendor/CUSO URLs need an explicit relationship | Opus | Correct. Add service-provider and institution-source relationship concepts |
| Expiry/withdrawal failed if publisher was down | Opus, Grok | Correct and safety-critical. Add `valid_until`, read-time suppression, and an emergency denylist |
| WAL sidecars conflict with atomic public DB swap | Opus | Correct. Public snapshots are immutable rollback-journal files with versioned pointer swaps |
| Rendered DOM/OCR locators and corrections missing | Opus | Correct. Add locator types and `Correction` |
| Form collection could create synthetic leads | Opus; Grok policy concern | Correct. T007 permits only approved anonymous public calculators and no personal identifiers |
| Roster/identity and later tasks lacked safe edges | Opus, Grok | Correct. Tighten dependency graph through schema, evidence, publication, site, security, and Pi stages |
| Definition of ready required fixtures universally | Grok | Correct. Require fixtures only for tasks that consume them |
| Membership eligibility must be visible | Gemini | Correct product requirement; avoid inventing final enums before evidence |
| Current Pi OS/RAM should be verified, not assumed | Opus | Correct. T006 selects and records a currently supported 64-bit release and hardware facts |

## Valid but defer to evidence or the named task

| Finding | Decision |
|---|---|
| 30-day lock, 780 score, zero points, 50-bp review threshold, and 72/96-hour clocks | Keep as explicit initial assumptions; validate against seed/pilot evidence before broad publication |
| HELOC `index + margin` calculation | Specify provenance and completeness in T010; do not blindly hard-code a rate without an approved index observation and source rule |
| Review-queue throughput | Measure during the pilot and use it in launch readiness; not a T004 blocker |
| First public cohort | Define during pilot/launch gating, after real coverage is known |
| Backup mechanism | Choose one in T061 after Pi testing |
| Legal advertising implications | Record as unresolved counsel questions in T007; models do not give the project legal clearance |

## Rejected as fluff, overreach, or incorrect priority

- Another planning cycle before T004.
- A new jumbo scenario with invented loan values before source evidence.
- Exact membership enum values before roster research.
- More loan terms, ARM ranking scenarios, government-program ranking, or a fee engine
  before the vertical slice and pilot.
- Client-side personalized calculators, container orchestration, GTM, live AdSense,
  commercial CMP integration, CAPTCHA bypass, or a runtime LLM.
- Writing full packets for every blocked future task now.
- Treating multiple `ready` tasks as inherently contradictory; the conflict existed
  only where the same packet explicitly claimed parallel work by one writer.
- Requiring every transitive dependency to be repeated on every ledger row. Direct
  safety-critical edges were added where they prevent premature work.

## Next action

Run T004 only. After its external clone and quality baseline are verified, unlock
T005, T007, and T010 for separately owned sessions; T010 is the next architecture
task and must be independently reviewed before schema or scraping implementation.
