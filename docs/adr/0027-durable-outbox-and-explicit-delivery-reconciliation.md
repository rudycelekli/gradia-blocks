# ADR-0027: Durable outbox and explicit delivery reconciliation

- **Status:** Candidate implementation; qualification and release evidence separate
- **Date:** 2026-10-15
- **Related:** [0026](0026-work-items-and-authenticated-feedback-handoff.md), [0025](0025-bounded-composition-locks-and-update-impact.md)

## Context

Reusable workflows need notification delivery. A timeout after a receiver commits is ambiguous: repeating the request can duplicate a message or business action. An HTTP success alone does not prove the intended receiver accepted the intended event. User-supplied URLs or credentials would expand sending authority. Existing blocks must remain independently usable.

## Decision

Create an original Apache-2.0 Outbox bounded context with typed enqueue/delivery contracts, durable SQLite records, role/tenant authorization and an HTTP API. Applications enqueue under actor-scoped idempotency identities. Operators explicitly claim and deliver one queued item. Commit the in-flight state before network I/O. Refuse another delivery for an item that is already in flight, uncertain or delivered.

The host configures fixed destination URLs, tenant bindings, receiver identities and signing secrets outside request payloads. Pin destination configuration in the database so restart cannot silently send existing payloads to a changed destination. Sign webhook envelopes and validate bounded identity-bound receiver acknowledgments. Disable environment proxies and redirects; permit HTTPS and explicit literal-loopback HTTP fixtures. Host-approved DNS, certificates, destinations and runtime remain trusted configuration.

Network failure, rejection or malformed acknowledgment after claiming an item leaves uncertainty. An explicit operator receipt lookup can recover acceptance without resending the payload. A receiver must implement the matching durable delivery-ID deduplication and receipt protocol. Generic Slack, email or webhook endpoints are not compatible until a dedicated adapter is qualified.

A process crash can leave an in-flight record. Startup does not reset it: that could race a live sender. An operator may mark it uncertain only after quiescing the sender, then reconcile with the receiver. No automatic retry, background worker or delivery timeout-based lease reclamation is introduced.

## Alternatives

A best-effort fire-and-forget webhook loses recovery evidence. Retrying every non-2xx or timeout confuses delivery uncertainty with known failure. A queue that resets in-flight items at startup can cause concurrent duplicate sends. Restricting all delivery to an in-memory stub would not establish persistence or actual transport behavior.

## Consequences

Queue persistence, claimant exclusion and metadata events can be tested independently from network delivery. Accepted receiver protocol receipts remain distinct from human acceptance, downstream business completion or billability. Outbound payload storage necessarily contains the authorized notification body; metadata telemetry excludes it. Quotas, retention, key rotation/migrations, HA, receiver business transactions and broader workload qualification remain separate gates.

## Validation contract

Run direct library and API tests plus independently authored adversarial cases. Prove concurrent claimant exclusion, isolation, idempotency, configuration drift refusal, malformed/forged acknowledgment refusal and metadata privacy. Run independent local HTTP subprocess fixtures with durable receiver storage: normal acknowledgment, post-commit response loss, restart and receipt reconciliation. Record exact source, archive, CI and production explorer evidence before promoting the catalog entry. Local fixtures do not establish enterprise admission or arbitrary-provider compatibility.
