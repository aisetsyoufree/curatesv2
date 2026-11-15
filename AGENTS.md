# Contributor Instructions

`PROJECT.md` is the canonical source of truth for CU-Rates.org V2.

Before starting any task, every human or model contributor must:

1. Read `PROJECT.md` completely.
2. Inspect `git status` and recent commits.
3. Identify the assigned task ID and verify it is ready.
4. Work only within that task's scope and acceptance criteria.

Before declaring an implementation task complete, update the assigned task packet
under `tasks/`, run its required verification, and provide the Section 13 handoff.
Do not edit the task ledger, decision log, review register, or change log unless you
are the designated product manager/integrator or Sid. Review-only contributors must
not edit the repository; the integrator records their reports and updates the ledger.

One writer owns each implementation task. Do not begin a blocked task, broaden its
scope, or silently change an acceptance criterion. If code and `PROJECT.md` disagree,
stop and report the conflict in the task packet.

Implementation work uses one branch per task, named `task/<ID>-<short-name>`. The
integrator reviews the diff and recorded verification, merges or cherry-picks the
accepted commit into the authoritative active clone, and alone updates `PROJECT.md`.
Workers do not merge their own branches or continue into the next task.

Do not publish or invent financial data. Every publication path must remain
fail-closed and source-grounded. Preserve unrelated and uncommitted work.

