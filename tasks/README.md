# Task packets

Every implementation task receives one packet named `<TASK_ID>.md`. The packet is
the worker's writable source of truth; `PROJECT.md` remains integrator-owned.

Each packet must contain:

- Objective and explicit non-goals
- Dependencies and allowed files
- Acceptance criteria
- Exact verification commands
- Data, migration, and publication impact
- Handoff with results, assumptions, and unresolved risks

Workers update only their assigned packet. The integrator reviews its evidence and
then updates the canonical ledger.
