# ADR-0026: Work items and authenticated Feedback handoff

- **Status:** Implemented reference; release evidence separate
- **Date:** 2026-10-15
- **Related:** [Composition decision](0025-composition-locks-and-impact.md), [Feedback reference](../../packages/blocks-feedback/README.md)

## Context

The composition planner can bind declared graphs and find impacted consumers, but its initial workflow was fictional. The next useful capability is a durable work-item proposal that an operator can accept and move through explicit states. A cross-service timeout can happen after a successful commit; recreating the proposal under another identity would duplicate work. Credentials and configurations can also change between inspection and execution.

## Decision

Create the independent Work Items bounded context in `packages/blocks-work-items`, using typed contracts, SQLite transaction/domain state checks, application HTTP routes and a separate host-configured handoff adapter. Keep payload storage distinct from metadata-only evidence. No dependency on Gradia's main backend or a public cloud account is introduced.

Require tenant/actor/role authority from configured bearer keys, compare-and-swap revisions, allowed transitions, idempotency and pipeline-scoped source uniqueness. Store mutation, acknowledgment identity and event in one transaction. Retain original create snapshots on replay; use reads for current state. Pin structural workflow identity in the database; feature switches can change on restart without rewriting historical rules.

Add Feedback patch0.2.1's authenticated session identity and conditional principal/configuration contract. The handoff checks both tenants/roles/editions, uses per-request identity/profile fences, requires a proposal-only target profile, and transfers only text plus selected source metadata. A changed profile or credential binding refuses before access/write. Bounded malformed responses or acknowledgment loss after a POST yield uncertainty and explicit source reconciliation; the runner never retries automatically or overwrites human triage.

## Alternatives

Keeping a graph-only demo would not test persistence and ambiguous effects. Automatically advancing work would confuse a proposal with acceptance. Assuming that two host tokens share a tenant would make configuration mistakes invisible. Global source uniqueness would prevent intentional separate pipelines; actor-scoped uniqueness requires stable pipeline identities and an explicit actor-rename migration plan.

## Consequences

The first actual composed reference runs through two HTTP services and a restart, with explicit human transitions. Optional state/assignee filters make board/queue integrations possible, but no Kanban UI is shipped. Metadata receipts support observability without a central collector. SSO, key lifecycle, quotas, HA, broad workload qualification and enterprise admission remain separate work.

SQLite schema/profile checks are defensive startup guards, not cryptographic database or filesystem attestation. HTTPS origins, secret files, directories and host runtime remain trusted. Cooperative timeouts do not establish OS-enforced cancellation. Source hashes bind selected fields, not customer truth, complete media or business acceptance.

## Validation

76 combined API/bridge/host tests pass locally, including 31 independently authored adversarial cases. Review caught adopted foreign schemas, incomplete POST acknowledgment checks and a discarded source configuration pin; fixes and regressions are retained. TCP fixtures run independent subprocesses with temporary private stores and generated credentials. Both normal acknowledgment and deliberate post-commit response loss recover one item after target restart, reject stale updates and retain four metadata mutation events through human-selected transitions.

Exact source, CI, diagram/lock replay and website receipts are recorded under `artifacts/work-items-2026-10-15/`. Synthetic fixture success does not qualify arbitrary client workflows. No watcher, automatic migration/promotion or managed work-item backend is enabled. Ruflo's connector still required authentication; native execution and independent native review were used.
