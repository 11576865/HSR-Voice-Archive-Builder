# Agent execution protocol

This repository participates in UIGS. Product code and project-specific evidence remain authoritative here.

## Highest-priority execution safety

Interactive agent sessions are disposable workers. Persisted repository state is the recovery source of truth.

1. Convert broad/open-ended requests into one bounded coherent work unit at a time.
2. Persist useful work early. Do not leave substantial progress only in conversation memory, hidden reasoning, a temporary worktree, or unpushed edits.
3. Before long CI, emulator/device tests, renders, deployments, cross-repository work, or other asynchronous/external operations, commit and push the coherent work completed so far whenever safe.
4. A pushed commit / branch / PR is the default execution boundary for the current work unit.
5. Pending CI, emulator/device tests, native probes, renders, deployments, reviews, or UIGS/Foundry processing do not block Agent Execution Complete.
6. After starting or locating an external job, perform at most one immediate status read unless the user explicitly requested synchronous waiting. If it is queued/running, report **Pending External Validation** and stop.
7. If an already-visible failure is directly caused by the current work unit, one bounded repair generation may be performed. After re-pushing the repair, do not poll the next generation; report it Pending and stop.
8. For a goal expected to span multiple work units, maintain a durable task record under `.uigs/tasks/<task-id>.json`. It must contain at least:
   - goal;
   - status;
   - current bounded work unit;
   - completed units;
   - next units;
   - branch / PR when applicable;
   - latest durable commit;
   - external validation states;
   - blockers;
   - updated timestamp.
9. On "continue" or recovery, read the durable task record, branch/PR, and latest durable commit before relying on conversation history.
10. UIGS Intake is bounded post-processing. Emit/update one narrow record or mark it Pending; do not wait for Foundry CI, collection, triage, reporting, or promotion.
11. Never claim external completion without evidence. Keep these states distinct:
    - Work Unit Complete
    - Durable Checkpoint
    - Agent Execution Complete
    - Pending External Validation
    - External Validation Complete
    - Project Complete

## UIGS relationship

- Read `.uigs/project.json` when present.
- Product implementation, tests, runtime behavior, releases, and device results remain authoritative in this repository.
- Reusable cross-project knowledge belongs in UIGS-Foundry, but downstream Foundry processing is a separate lifecycle from this product-side agent turn.
- If a Foundry write cannot be completed immediately, report **Pending** rather than extending the current turn.

The objective is durable eventual completion: an interrupted session must be resumable from repository state alone.
