# Gradia Event Relay 0.1 — reference

Original Apache-2.0 metadata-only bridge from the Work Items 0.2 changefeed to
the Durable Webhook Outbox 0.1 enqueue API. One explicit operator invocation
processes at most 25 committed mutation events. It never sends a webhook,
starts a watcher, retries automatically, changes a work item, or grants delivery.
The Outbox operator separately chooses whether to deliver queued notifications.

## What crosses the boundary

Each queued payload contains only these bounded metadata fields:

```json
{
  "source": {
    "journal_id": "canonical UUID",
    "sequence": 1,
    "event_id": "canonical UUID",
    "event_sha256": "SHA-256 of the exact original metadata event JSON",
    "block_version": "0.2.0"
  },
  "work_item": {
    "item_id": "canonical UUID",
    "revision": 1,
    "operation": "item.proposed"
  }
}
```

`item.proposed` maps to `work_item.proposed`; `item.updated` maps to
`work_item.updated`. Titles, descriptions, attachments, customer messages,
authorization credentials, configured URLs, and the full source event are not
forwarded. Metadata remains potentially sensitive and stays tenant scoped.
A source hash binds bytes; it does not prove the business event was true.

## Install and compose

Python 3.11 or newer. The exact development dependency pins are in
`requirements-tested.txt`.

```sh
python -m pip install -e packages/blocks-event-relay
```

```python
from gradia_event_relay import Relay

with Relay(
    "/private/operator-owned/relay.sqlite",
    source_origin="https://work-items.example.test",
    target_origin="https://outbox.example.test",
    source_token=host_secret_store.read("work-items-operator"),
    target_token=host_secret_store.read("outbox-submitter"),
    tenant_id="example-tenant",
    profile_id="work-item-notifications-v1",
    destination_id="notifications",
) as relay:
    result = relay.run(limit=10)
```

The secret-store object above is a host integration example, not an included
secret manager. Origins, credentials, tenant, profile and destination are
host-owned constructor configuration, never caller-supplied run parameters.
Changing configuration requires an explicit offline migration/new checkpoint.
Keep profile IDs stable and unique per intended notification pipeline. Separate
profiles deliberately produce separate notifications. The deterministic Outbox
idempotency key hashes the profile, source journal and original event ID.

## Scope, validation and recovery

The source must authenticate as an operator on Work Items **0.2.0**; the target
must authenticate as a submitter on Outbox **0.1.0**, both in the expected tenant.
The source changefeed can include historical metadata event editions **0.1.0**
and **0.2.0**. Unknown editions, fields, operations or malformed events fail
closed. The relay validates the entire bounded page before producing target
effects. Session principal/configuration hashes and source journal identity
are pinned in its private local checkpoint at the first successful bind. Every
subsequent read/write carries conditional principal, configuration and edition
headers, with the response principal fingerprint checked.

The local checkpoint contains a cursor plus the exact source event ID/hash at
that cursor. A subsequent source read validates that anchor in the source
transaction. Missing/replaced anchors and journal changes stop progress. This
is bounded rollback detection, not a complete source database attestation or
protection against a source that lies consistently.

After each typed Outbox acknowledgment, checkpoint advancement and a metadata
event commit atomically. The acknowledgment must describe the original queued
revision-1 creation snapshot, a null delivery attempt, matching destination,
event type and payload hash, strict boolean replay marker, canonical UUID and
valid timezone-aware timestamps. A valid replay can describe the original
creation even when the Outbox item has subsequently been delivered. The relay
never mutates that item's state.

A target commit followed by response loss returns `uncertain` and leaves the
cursor before that event. An explicit later `run()` reuses the exact same key,
recovers the original creation acknowledgment, then advances once. Previously
acknowledged earlier events in the same invocation remain committed. There is
no distributed transaction and no claim of exactly-once external delivery.
Retain the Outbox idempotency records and both databases for this guarantee.

Results are `enqueued`, `idle`, `refused`, or `uncertain`, with a sanitized
receipt and bounded item IDs. `acknowledged_count` counts verified enqueues in
this invocation. A local persistence failure requires operator investigation;
do not discard databases, reset cursors, or rename profiles to bypass it.

## HTTP facade

`create_app(relay, api_keys)` accepts a host-created Relay plus a bounded mapping
of private bearer strings to `Principal(tenant_id, actor_id, role="operator")`.
Only the configured tenant's operators can call the following endpoints:

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/v1/event-relay/session` | Authenticated edition/principal/config identity |
| POST | `/v1/event-relay/run` | Body `{"limit": 1}`; one bounded explicit run |
| GET | `/v1/event-relay/status` | Metadata checkpoint and capability limits |
| GET | `/v1/event-relay/events?limit=100` | Last 1–100 durable metadata events |
| GET | `/v1/event-relay/diagnostics/attempts` | Bounded process-local HTTP attempt rings |

Optional `If-Gradia-Principal`, `If-Gradia-Configuration` and
`If-Gradia-Block-Version` fences are checked before work. Duplicate authority
headers and unknown/duplicate query parameters refuse. The request stream is
bounded to 4 KiB; duplicate-key, nonfinite and excessive-depth JSON refuse.
Successful session, status and run responses have strict, closed Pydantic
contracts in OpenAPI, including checkpoint anchors, transfer acknowledgments and
all run receipt metadata. Optional unavailable correlation/binding fields stay
absent; the response facade does not invent observations.

API documentation is available through the reference FastAPI OpenAPI document;
no real upstream service is contacted by constructing the app or reading docs.

## Private server configuration

Set these environment variables to host-owned paths; do not put tokens in the
shell command, repository or environment itself:

- `GRADIA_EVENT_RELAY_DATABASE`: persistent SQLite file.
- `GRADIA_EVENT_RELAY_CREDENTIALS_FILE`: JSON object mapping API bearer credentials
  to objects with `tenant_id`, `actor_id` and optional `role: "operator"`.
- `GRADIA_EVENT_RELAY_PROFILE_FILE`: JSON object with exactly `source_origin`,
  `target_origin`, `source_token`, `target_token`, `tenant_id`, `profile_id`,
  `destination_id`.
- Optional `GRADIA_EVENT_RELAY_PORT`, default `3194`.

```sh
python -m gradia_event_relay.server
```

The launcher binds `127.0.0.1` only. Supply your own production TLS termination,
authentication provisioning and operational controls. Configuration files must
be private regular files (no group/other permissions, leaf symlinks or hardlinks),
at most 64 KiB. Database and the stable `.run-lock` sidecar also require private
regular files. All conforming processes serialize the entire run using a
nonblocking POSIX flock on that sidecar. macOS SQLite conflicts with a flock on
the database inode itself, so the private sidecar is intentional. Trust and
protect the parent directory; never replace/delete lock or DB files while any
relay process exists. This is local single-host coordination, not a distributed
lease or multi-host HA mechanism.

Outbound origins require HTTPS with normal CA validation, except explicitly
configured literal loopback HTTP IPs. No environment proxy, redirects, inherited
client default headers/auth/cookies, or caller URLs. Each operation has at most
three seconds of HTTP timeout and the invocation has a cooperative 20-second
budget. It is not an OS-enforced deadline. Responses require identity encoding
and JSON, are bounded to 256 KiB, and reject duplicate keys, nonfinite numbers
and depth beyond 16. Injected HTTPX clients are a host testing trust boundary.

## Telemetry and limits

Checkpoint/binding, acknowledged enqueue and invocation outcome events persist
in SQLite. A checkpoint/event write shares one transaction. Every event records
edition/configuration/tenant/profile/time, metadata privacy and explicit
unknown cost, unevaluated acceptance/billability and unknown human involvement.
Source metadata, payload hashes and Outbox IDs establish correlation. They do
not establish business acceptance, truth, savings or invoice eligibility.

The HTTP API joins its generated correlation UUID and configured operator actor
to the invocation receipt, durable binding/enqueue/outcome events and upstream
attempts. Valid UUID response correlation IDs link source reads and target
enqueues to their respective service logs. Host Python callers may explicitly
supply bounded `correlation_id` and `actor_id` keyword arguments to `run()`;
these are host attribution metadata, not authorization or proof of human action.

API and upstream HTTP attempts have separate last-100 process-local rings.
Request URLs, query strings, credentials and payload bodies are excluded.
These rings disappear on restart. Durable event retention, encryption at rest,
quotas, hosted storage, SSO, multi-host leases, database restoration attestation,
background scheduling and customer-specific approval policies are not included.
This package is a self-hosted reference, not enterprise admission or a guarantee
that arbitrary software updates cannot break its pinned integration.

## Agent execution contract

Use the packaged [relay-work-item-events skill](skills/relay-work-item-events.md)
for bounded agent invocation and recovery. The skill is distribution guidance;
it does not grant credentials, delivery permission or autonomous scheduling.

## Verification

The integration tests require neighboring reference packages on `PYTHONPATH`:

```sh
PYTHONPATH=packages/blocks-event-relay:packages/blocks-work-items:packages/blocks-feedback:packages/blocks-outbox:packages/blocks-composition python -m pytest -c /dev/null -p no:cacheprovider packages/blocks-event-relay/tests -q
PYTHONPATH=packages/blocks-event-relay:packages/blocks-work-items:packages/blocks-feedback:packages/blocks-outbox python packages/blocks-event-relay/examples/full_tcp_replay.py
```

The recipe tests require the neighboring Feedback, Work Items, Outbox and
Composition packages from this repository (or matching source archives extracted
into the same `packages/` directory). The full TCP replay in
`examples/full_tcp_replay.py` additionally uses Feedback
and its first handoff. It uses fictional tenant content and independent local
processes; its receipt is separate evidence from production qualification.
