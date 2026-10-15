# ADR-0028: Anchored changefeed and checkpointed event relay

- **Status:** Candidate implementation; release qualification separate
- **Date:** 2026-10-15
- **Related:** [0027](0027-durable-outbox-and-explicit-delivery-reconciliation.md), [0026](0026-work-items-and-authenticated-feedback-handoff.md), [0025](0025-bounded-composition-locks-and-update-impact.md)

## Context

Work Items already commits each mutation and its metadata event in one SQLite transaction. Outbox independently persists and delivers notifications. Two sequential API calls do not atomically save a work-item change and queue its notification: a crash can occur between them. A source journal plus idempotent transfer can preserve the source intent without coupling both storage engines or hiding delivery uncertainty.

## Decision

Release Work Items 0.2 with an operator-scoped, tenant-wide ascending changefeed over its existing committed events. Persist a stable journal identity. Every resumed read binds the journal, tenant and exact previous event's sequence, ID and canonical digest. Read a page and its head from one database snapshot. Refuse changed or missing anchors. Existing compatible 0.1 databases may bootstrap the additional journal metadata transactionally; foreign or altered schemas/identity markers refuse. This is a declared journal identity, not remote attestation or a universal backup-restore proof.

Add an independent Event Relay context with private checkpoint storage, explicit host profiles and a bounded run API. The relay requires a Work Items 0.2 operator and Outbox 0.1 submitter in the same configured tenant, verifies sessions and conditional identity/configuration/edition contracts, validates the entire selected feed page before queue effects, and transforms only selected source metadata into notification payloads. Stable profile+journal+event identities produce deterministic Outbox idempotency keys.

Commit a source checkpoint only after validating the target's exact enqueue acknowledgment. A target commit whose response is lost leaves the checkpoint unchanged. An explicit rerun queues the same stable identity and recovers the original enqueue snapshot without duplicating the record or resetting later delivery state. Serialize runs for a checkpoint store; never interpret process restart as approval to advance or resend. The relay can enqueue but has no notification-send authority.

## Alternatives

A mutation handler that calls a second service inline retains the crash window and makes source availability depend on network delivery. Sharing or attaching both SQLite databases would couple deployment and has multi-file crash semantics requiring separate qualification. Automatically rewinding cursors, accepting unknown schemas or clearing idempotency on error would conceal lost/duplicate work. Global source cursors without tenant and event anchors permit confused-scope or restore mistakes.

## Consequences

The source mutation and event intent are atomic locally; cross-service transfer remains an explicit, recoverable protocol with idempotency, not a distributed atomic transaction or universal exactly-once business outcome. Metadata-only notification payloads contain no report title, description or media. Identifiers and digests remain sensitive/linkable and require access/retention policy.

Destination, principal, configuration or edition changes require explicit qualification/migration; new source implementations can be added through exact adapter contracts rather than blind version acceptance. Historical source events remain edition-tagged. Durable checkpoints and relay observations support diagnostics without a central collector or watcher. Host filesystem/network/CA, tokens and profiles remain trusted. Restoring or deleting source/target/checkpoint state needs a coordinated recovery policy; a surviving journal anchor cannot prove all historical business truth.

## Validation contract

Independently test journal authorization and anchors, atomic mutation/event rollback, changed schemas, bounded snapshot reads, complete-page validation before effects, target identity races, strict acknowledgments, cursor advancement ordering, checkpoint races and payload privacy. Run real loopback HTTP subprocesses for Feedback → Work Items → Event Relay → Outbox → signed receiver, including lost enqueue acknowledgment and lost delivery acknowledgment, process/store restarts and explicit reconciliation. Retain exact versions, source/download hashes, CI and website receipts. Passing fixtures do not establish enterprise admission, real customer acceptance, provider compatibility or autonomous promotion.
