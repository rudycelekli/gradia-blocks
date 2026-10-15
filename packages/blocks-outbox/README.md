# Gradia Outbox 0.1

**Queue a notification. Inspect its delivery. Recover uncertainty without blindly sending again.**

Original Apache-2.0, independently runnable Python/HTTP reference. Suitable for
qualifying a custom app-to-service webhook protocol. This package does not send
email, Slack or Teams messages directly; those need separately qualified adapters.
No managed service, background sender, customer deployment or enterprise admission
is implied by this reference release.

## Run the complete local example

Python 3.12 is recommended; package floor is Python 3.11. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r packages/blocks-outbox/requirements-tested.txt
.venv/bin/pip install --no-deps -e packages/blocks-outbox
.venv/bin/python -m pytest -c /dev/null -p no:cacheprovider packages/blocks-outbox/tests -q
.venv/bin/python packages/blocks-outbox/examples/tcp_replay.py
```

The script generates ephemeral credentials, starts separate loopback sender and
receiver processes, queues a fictional work-item event, sends it once, restarts
both services and inspects persistent state. A second fixture commits the receiver
record and drops its acknowledgment; explicit receipt lookup recovers acceptance
without another POST. Stores and processes are cleaned up. It makes no external
provider/model calls and uses no customer data. The receiver is a protocol fixture,
not a production notification service.

## Embed the engine

```python
import secrets
from gradia_outbox import Destination, Engine, Principal

# Private writable parent directory is owned by the host. This is a placeholder
# HTTPS destination; do not send until an authorized receiver is configured.
destinations = {
    'ops': Destination(
        tenant_id='acme', url='https://receiver.example.invalid/webhook',
        signing_key=secrets.token_hex(32), receiver_id='operations-receiver',
    )
}
engine = Engine('/private/host/path/outbox.sqlite', destinations)
app = Principal('acme', 'workflow', 'submitter')
queued = engine.enqueue(
    app, 'ops', 'work_item.proposed',
    {'item_id': 'approved-reference-id', 'status': 'proposed'},
    'workflow-event-123',
)
# Queueing sends nothing. Only an explicitly authorized operator can call:
# engine.deliver(operator, queued['item']['id'], expected_revision=1)
```

Persist host secrets outside application/client payloads. Regenerating the key at
every startup changes configuration and refuses the existing database. The example
above is a one-time configuration illustration, not a secret-rotation mechanism.
Raw authorized payloads live in the private queue database; HTTP item reads expose
metadata only. Metadata telemetry is separate from payload storage.

## Host the API

`create_app(database_path, api_keys, destinations)` accepts private host mappings:

- Bearer key → `Principal(tenant_id, actor_id, role)`. Keys are 24–256 printable
  ASCII characters, no defaults. Roles are `submitter` or `operator`.
- Destination ID → `Destination(tenant_id, url, signing_key, receiver_id, timeout)`.
  URLs are fixed HTTPS origins/paths; plain HTTP requires a literal loopback IP.
  Userinfo, queries, fragments, whitespace and unbounded URLs are refused. Timeouts
  are positive and at most three seconds per network operation.
- Destination configuration, including a signing-key fingerprint, is pinned to the
  private database. Changes require an explicit offline migration. There is no
  automatic rotation, URL rebinding or queued-payload migration.

Server entrypoint:

```sh
GRADIA_OUTBOX_DATABASE=/private/host/path/outbox.sqlite \
GRADIA_OUTBOX_CREDENTIALS_FILE=/private/host/path/credentials.json \
GRADIA_OUTBOX_DESTINATIONS_FILE=/private/host/path/destinations.json \
GRADIA_OUTBOX_PORT=3193 \
.venv/bin/python -m gradia_outbox.server
```

Both configuration files must be regular mode-0600 files, without symlinks or
hardlinks, at most 64 KiB, with no duplicate/nonfinite JSON fields. Credentials JSON
maps keys to principal objects; destinations JSON maps IDs to destination objects.
The entrypoint binds `127.0.0.1`; a production host must qualify its TLS proxy,
authorization, process lifecycle, backups and outbound network policy.

## HTTP contract

Every application endpoint requires host-configured bearer authentication. Send
`Content-Type: application/json` for writes. See [OpenAPI](examples/outbox-openapi.json).

| Method / path | Behavior |
| --- | --- |
| `GET /v1/outbox/session` | Edition, principal identity and configuration digest |
| `GET /v1/outbox/destinations` | Same-tenant destination IDs/receiver IDs; no URLs or secrets |
| `POST /v1/outbox` | Queue `{destination_id,event_type,payload}`; required `Idempotency-Key` |
| `GET /v1/outbox?limit=50` | Own-actor items; operators see their tenant; bounded snapshot, no pagination |
| `GET /v1/outbox/{id}` | Metadata, state, revision, payload digest, timestamps and attempt ID |
| `POST /v1/outbox/{id}/deliver` | Operator-only one explicit attempt; `{expected_revision}` |
| `POST /v1/outbox/{id}/reconcile` | Operator-only signed receipt lookup; `{expected_revision}` |
| `POST /v1/outbox/{id}/mark-uncertain` | Operator asserts crashed sender is quiescent; `{expected_revision}` |
| `GET /v1/outbox/{id}/events` | Operator-only last 100 durable mutation events, newest first |
| `GET /v1/outbox/diagnostics/attempts` | Operator-only last 100 process-local HTTP observations in tenant |

Payload must be a bounded JSON object: canonical UTF-8 ≤16 KiB, nesting ≤16,
finite numbers and interoperable integers. HTTP bodies ≤48 KiB and ≤1,000 JSON
nodes; duplicate fields are refused. IDs/idempotency keys are bounded ASCII
identifiers. HTTP destination/event IDs use letters, digits, dot, dash or underscore;
the engine's internal identifier format also supports colon. List limit ≤100;
unsupported/duplicate list query parameters refuse.

Conditional `If-Gradia-Principal` (SHA-256 of canonical sorted principal JSON),
`If-Gradia-Configuration` and `If-Gradia-Block-Version` headers refuse a stale
binding before access or sending. Responses carry server-generated correlation IDs,
principal fingerprint when authenticated, and `X-Gradia-Cost-Status: unknown`.
A configuration digest does not grant authority or attest a remote deployment.

Enqueue idempotency is scoped to tenant + actor + key. Same input returns the
**original queued snapshot**, even after delivery. Different input under the same
key refuses. Use GET for current state. Operators cannot view another tenant;
submitters cannot send, reconcile, mark uncertainty or inspect mutation events.

## Delivery protocol

Immutable canonical envelope fields:

```json
{"schema":"gradia.webhook.v1","delivery_id":"stable-id","tenant_id":"acme","event_type":"work_item.proposed","payload_sha256":"sha256-of-canonical-payload","payload":{"item_id":"example"},"created_at":"2026-10-15T00:00:00+00:00"}
```

Canonicalization is sorted keys, compact separators, UTF-8 without ASCII escaping,
finite JSON, nesting ≤16 and integers within ±(2^53−1). Sign **exact emitted bytes**.
The receiver must atomically deduplicate the delivery ID and bind its accepted
record to the complete envelope. A real business side effect must participate in
that transaction or retain its own explicit uncertainty; this fixture only stores
a receipt. Signature verification is not proof of business completion.

Headers: `X-Gradia-Timestamp` (Unix seconds), `X-Gradia-Delivery-Id`,
`X-Gradia-Attempt-Id`, `X-Gradia-Payload-Sha256`, `X-Gradia-Signature`.
The signature is `sha256=` plus hex HMAC-SHA256 with the host signing key:

- POST: `timestamp + '.' + canonical_envelope_bytes`.
- GET receipt: `timestamp + '.GET.' + exact_receipt_url + '.' + canonical_envelope_bytes`.
  Receipt URL is configured webhook URL without trailing slash + `/receipts/{delivery_id}`.
  Receiver retrieves its stored envelope; missing receipt returns 404.

The receiver should enforce timestamp freshness and constant-time verification.
The local fixture enforces a 60-second window. An acknowledgment is HTTP 200/201,
identity-encoded `application/json`, ≤16 KiB, with **exact** fields and types:

```json
{"schema":"gradia.webhook.ack.v1","delivery_id":"stable-id","payload_sha256":"sha256-of-canonical-payload","receiver_id":"host-configured-receiver","accepted":true}
```

Its `X-Gradia-Ack-Signature` is `sha256=` + hex HMAC-SHA256 over
`'gradia.webhook.ack.v1.' + request_timestamp + '.' + attempt_id + '.' + canonical_ack_bytes`.
Raw unsigned/extra-field/encoded/malformed/wrong-identity acknowledgments refuse.
HTTP success alone cannot mark delivery. Response signatures prove possession of
the configured key, not correctness or truth of the receiver's business record.

No environment proxies, redirects, default client cookies/authorization or caller
headers are forwarded. HTTPS certificate validation uses HTTPX's normal transport.
Destination/DNS/CA trust, injected test transports and host filesystem remain host
responsibilities. This is not a universal SSRF firewall or network-isolation proof.
Response bytes and cooperative read duration are bounded; network timeouts are per
operation, not OS-enforced wall-clock cancellation.

## State and recovery

`queued → in_flight → delivered | uncertain`. Claim and telemetry commit before
network I/O; concurrent senders cannot claim the same queued item. The original
payload is never sent again by this engine once claimed. No timer, lease expiry or
startup reset sends automatically.

Any transport error, receiver rejection or invalid acknowledgment after claiming
leaves `uncertain`. Explicit reconciliation only GETs a signed receipt. A missing,
invalid or unavailable receipt stays uncertain, with a new revision and metadata
event. It does not prove non-delivery and does not authorize a resend.

A crash may leave `in_flight`. First quiesce the sending process; an operator may
then mark it uncertain and reconcile. The API cannot prove quiescence. If a live
sender races that operation, its state update refuses rather than overwriting the
operator's new revision. Stronger process fencing is a future qualification gate.

## Telemetry and current limits

Durable events include block/version/configuration, event ID/time, tenant/actor,
item/destination/event type, revisions/state, payload digest, attempt/correlation
IDs, and signed-ack digest/receiver/authentication method on acceptance. Mutation
and event share a transaction. HTTP attempts add duration, status and coverage in
a bounded volatile ring. Neither includes payloads, URLs, signing keys, bearer
credentials, response bodies or exception text. Digests and identifiers can still
be sensitive/linkable; access and retention policies are required.

Cost, human involvement, billability and business acceptance remain unknown or not
evaluated. Receiver-protocol acceptance is a narrower fact. No external collector,
model judge, scheduler or watcher is enabled. SQLite preserves explicit state;
there is no HA, quotas/retention service, dead-letter replay, automatic retry,
full audit-retention guarantee, verified key rotation or enterprise admission.
The current reference uses a custom protocol; generic providers need adapters.

[Source architecture](examples/outbox.mmd) · [Manifest](block.manifest.json).
