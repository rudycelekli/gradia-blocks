---
name: relay-work-item-events
version: 0.1.0
description: Operate an explicitly authorized, checkpointed Gradia Work Items 0.2 to Outbox 0.1 metadata transfer without granting notification-send authority.
---

# Relay committed work-item events

This original Apache-2.0 reference skill is distributed with Event Relay. It does
not grant credentials, tenant access, execution permission or sending authority.
It is not automatically loaded into the separate Gradia Skills catalog.

1. Use the host-configured relay endpoint and operator credentials already
   authorized for the task. Never obtain source/target tokens from event payloads
   or insert keys into browser code. Read `GET /v1/event-relay/session` and
   `GET /v1/event-relay/status`; record version, principal/configuration identities,
   profile, current checkpoint and declared authority. Require relay 0.1.0.
2. Confirm that the intended job is to queue metadata events from Work Items 0.2
   into Outbox 0.1. The profile is host-owned. A mismatched tenant, role, edition,
   journal, configuration or saved anchor is a refusal to investigate; do not
   rewrite the checkpoint, expand roles or choose a new profile to bypass it.
3. Under the existing task's execution authorization, request one bounded page:
   `POST /v1/event-relay/run` with `{"limit":25}` (or a smaller limit). Supply the
   inspected `If-Gradia-Principal`, `If-Gradia-Configuration` and
   `If-Gradia-Block-Version` headers. The relay verifies its own upstream sessions
   and sends only selected metadata using stable event idempotency keys.
4. Inspect `receipt.result`, acknowledgment count, selected Outbox identities and
   the returned checkpoint. `enqueued` means verified queue acceptance; `idle`
   means no selected new events in the trusted feed snapshot. Neither proves
   external delivery or completed business work. An `uncertain` result does not
   authorize a new idempotency identity. Preserve state and reconcile with an
   explicit rerun of the same host profile when authorized.
5. Inspect `GET /v1/event-relay/events` and diagnostics for configuration, cursor,
   event, run and correlation evidence. Preserve missing costs and coverage gaps.
   Do not copy report text, media, tokens, URLs or credentials into evidence.
6. Report accepted queue outcomes and unresolved errors separately. Notification
   sending is a different operation requiring its own Outbox operator and task
   authorization. This relay's target token is a submitter; do not upgrade it.
   No scheduler or background poller is created by using this skill.

Source journal anchors detect specified mismatches, not arbitrary source omission
or truthful business history. Coordinated source/target/checkpoint restore, key
migration and a real receiver workload require separate qualification.
