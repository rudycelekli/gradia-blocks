# Gradia Feedback block · local reference candidate

One behavioral HTTP contract for a feedback widget, operator queue or agent.
The Python/FastAPI reference persists tenant-scoped reports in SQLite with selectable media storage. A
TypeScript client calls the same API. This is runnable local code, **not an
admitted production package, a hosted service or a native multi-language server**.
No customer implementation was copied into this package.

## Run locally

Requires Python 3.11+. From the repository root:

```sh
python -m venv /tmp/gradia-feedback-env
/tmp/gradia-feedback-env/bin/pip install -e './packages/blocks-feedback[test]'
/tmp/gradia-feedback-env/bin/python -m pytest -c /dev/null -p no:cacheprovider packages/blocks-feedback/tests -q
```

Generate a local, owner-readable credential file and persistent database directory:

```sh
umask 077
mkdir -p /tmp/gradia-feedback-local
/tmp/gradia-feedback-env/bin/python -c 'import json,secrets; from pathlib import Path; Path("/tmp/gradia-feedback-local/credentials.json").write_text(json.dumps({secrets.token_urlsafe(32):{"tenant_id":"demo","actor_id":"local-operator","role":"operator"}}))'
export GRADIA_FEEDBACK_CREDENTIALS_FILE=/tmp/gradia-feedback-local/credentials.json
export GRADIA_FEEDBACK_DATABASE=/tmp/gradia-feedback-local/feedback.sqlite
/tmp/gradia-feedback-env/bin/python -m gradia_feedback.server
```

The server binds **127.0.0.1:3191**. Open `/docs` for the generated API schema.
No default token exists. The credential file maps each server-owned bearer key
to its tenant, actor and role. A caller cannot choose its tenant by payload or
header. Use TLS and host authentication/authorization before any remote exposure.
Do not embed these long-lived operator keys in browser bundles. A host BFF may
keep credentials server-side and enforce its own user authority; that integration
is a separate qualification. The reference adds no permissive CORS policy.

Use `GRADIA_FEEDBACK_SETTINGS_FILE` for a JSON `BlockSettings` configuration;
unknown fields and executable configuration are rejected. `GRADIA_FEEDBACK_PORT`
changes only the loopback port. The app factory accepts a `Path`, explicit
`dict[str, Principal]`, and `BlockSettings` for embedding in a trusted host.
Do not share one actor credential between users if user attribution is required.

## API v1

All routes require `Authorization: Bearer <configured key>`. Tenant identity is
assigned exclusively by that credential. OpenAPI/docs expose schemas, not data.

| Method and endpoint | Authority | Behavior |
|---|---|---|
| `GET /v1/feedback/config` | Submitter or operator | Safe design/content/features/config plus configuration digest |
| `POST /v1/feedback` | Submitter or operator | Message/category/context; optional audio; `Idempotency-Key` required |
| `GET /v1/feedback?limit=25&cursor=` | Operator | Tenant-only list, maximum 100 rows, opaque ID cursor |
| `GET /v1/feedback/{id}` | Operator | Tenant-only record; foreign tenant IDs return 404 |
| `PATCH /v1/feedback/{id}` | Operator | Triage with mandatory `expected_revision` |
| `GET /v1/feedback/{id}/events?after=0&limit=100` | Operator | Bounded committed-mutation audit sequence |
| `GET /v1/feedback/{id}/audio` | Operator | Private legacy audio download when enabled |
| `GET /v1/feedback/{id}/attachments/{attachment_id}` | Operator | Own-tenant photo/screenshot/video bytes; feature gate and SHA256 integrity check |
| `GET /v1/feedback/diagnostics/attempts` | Operator | Own-tenant bounded process-memory HTTP attempt spans and coverage gaps |

Submission JSON:

```json
{"message":"Keyboard navigation loses focus", "category":"bug", "context":{"app_id":"host-a","page_url":"/account"}}
```

A first committed submission returns HTTP 201:

```json
{"status":"acknowledged","feedback":{"id":"server-generated-id","revision":1,"status":"new","category":"bug","message":"Keyboard navigation loses focus","context":{"app_id":"host-a","page_url":"/account"},"assignee":null,"audio":null,"created_at":"server-UTC-time","updated_at":"server-UTC-time"},"replayed":false}
```

The feedback row, mutation evidence and original idempotency receipt commit in
one transaction **before** acknowledgment. Retry an uncertain response with the
same payload and key. Replays return HTTP 200 and the original receipt; later
triage does not alter the original submission receipt. A different payload with
the same tenant/actor/key returns 409. Keys currently persist for the database
lifetime; no expiry policy is implemented. Exact retry does not generate another
audit mutation. Changed configuration/disabled features may refuse a retry;
operators can reconcile persisted records through tenant queries.

Triage body: `{"expected_revision":1,"status":"in_review","assignee":"owner"}`.
Allowed states are `new`, `in_review`, `resolved`, `dismissed`; all transitions
among these states are allowed by this reference profile. Status is case state,
not business acceptance or billability. A stale revision returns 409. Setting
`assignee` to null clears it; null status/category is rejected. Assignment is an
opaque identifier and does not validate host-directory membership.

Errors use `{"error":{"code":"stable_code","message":"safe explanation"}}`.
401 requires credentials, 403 denies role/disabled feature, 404 hides missing or
foreign records, 409 reports conflicts, 413 caps bytes, 422 rejects malformed
contracts, and 503 means persistence was not acknowledged. Network errors/timeouts
are **uncertain**, never successful submissions. The TypeScript client preserves
this distinction and performs no blind automatic retry. It validates an explicit
submission or triage receipt with a positive revision before returning success; callers must render acknowledgment
only after that promise completes. The deadline bounds credential lookup, transport and JSON reading; a caller may still need to stop its own noncooperative credential task after timeout.

## Configuration and optional audio

Four strict namespaces: `design` (accent hex, radius, density, placement),
`content` (plain text labels), `features` (`enabled`, `audio`, `triage`, `photo`, `screenshot`, `video`), `config`
(categories, message limit, audio limit). Settings are host-owned. A feature flag
never weakens mandatory tenant or audit controls. Consumers must render content
as text and implement their own accessibility/conformance checks.

Audio defaults **off**. When explicitly enabled, `audio` is
`{"mime_type":"audio/ogg","data_base64":"..."}`. Accepted MIME types are
`audio/webm`, `audio/ogg`, `audio/wav`, with matching file signatures; maximum
256 KiB decoded legacy audio. The total HTTP-body limit is derived from the
configured media allowance and enforced for streamed bodies too.
Normalize MediaRecorder MIME parameters before submission. This service stores
and returns bytes; it does not record microphones, transcribe, decode, inspect
spoken content, certify media safety, fetch remote URLs or call providers.
Boundaries/consent/retention for real recording belong to the host integration.

## Evidence, storage and lifecycle

Committed mutations emit local audit records with block/version/configuration,
tenant/actor/case/revision, correlation/edition, operation, timestamp, measured handler-to-write
latency, coverage, and explicit **unknown cost**. Acceptance and billability are
`not_evaluated`. Events exclude report text/audio and do not phone home. Event
latency excludes final database commit and network transport; it is not end-to-end
latency. Human involvement is explicitly unknown for configured principals; no human role is inferred from a key. The durable audit uses `coverage=committed_mutations_only`. A second mandatory process-memory ring collects safe HTTP attempt metadata, including reads and refusals, with `coverage=volatile_http_attempts`. Each configured tenant has a maximum 100 entries; eviction and collection-failure counters are exposed to its operators. Unauthenticated attempts have a separate bounded ring accessible only to the embedding host, and cannot inherit a caller-supplied tenant. Route templates, methods, status, stable safe refusal codes, server-generated correlation IDs, measured duration and unknown cost are collected; raw URL paths/queries, body contents, tokens and audio are excluded. Mutation and HTTP evidence share correlation IDs. Process restart loses the volatile ring; no retention/collector availability claim is made. Attempt collection failure leaves a committed acknowledgment unchanged and increments its tenant gap counter. The event mechanism is mandatory for writes and cannot be disabled by feature flags. A local event reader is included; vendor collection, monetization, optional host sinks, provider spans and delivery workers remain separate adapters.

Schema version 1 is stored in `block_metadata`; unsupported versions refuse.
Version0.2 adds an attachment table to schema1 without rewriting existing feedback.
Cross-backend media migration, automatic downgrade or a hosted bridge is not implemented.
Stop writers, back up the entire SQLite file, restore into an isolated host and
rerun acceptance before changing a version. There is no purge/export management,
encryption at rest, rate limiting, identity provider, secret rotation endpoint,
anti-abuse pipeline, availability SLO or production backup qualification here.
New database files use mode 0600. On POSIX, existing databases with group/world permissions refuse without changing their permissions; the owner can explicitly apply chmod 600. Database symlinks refuse. Keep the containing directory private; data/audio are sensitive customer content.
The reference binds locally and requires explicit credentials so those missing
operational controls cannot be mistaken for production admission.

`block.manifest.json` gives agent-readable capabilities, typed port names,
authority/effects and limitations. `/openapi.json` gives the full request/response
schema. A manifest is not a signed artifact lock or permission grant.

The package license is Apache-2.0, scoped to this directory. Third-party
requirements retain their licenses; this does not relicense the containing repo.
`requirements-tested.txt` records tested direct versions, not a transitive lock.
Python is the only server implementation. `typescript/client.ts` is a typed HTTP
client; Go/JVM/.NET clients and native implementations require
separate integration and behavior evidence.

## TypeScript client transport checks

Compile the standalone client with an available TypeScript compiler and run the
Node transport tests (no credentials or provider calls):

```sh
mkdir -p /tmp/gradia-feedback-client-test
tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM --outDir /tmp/gradia-feedback-client-test packages/blocks-feedback/typescript/client.ts
GRADIA_FEEDBACK_COMPILED_CLIENT=/tmp/gradia-feedback-client-test/client.js node --test packages/blocks-feedback/typescript/client.test.cjs
```


## Media edition 0.2.0: screenshots, photos and video

All media features default off. Use one of the complete settings presets in
[`examples/text-only.json`](examples/text-only.json) or
[`examples/media-enabled.json`](examples/media-enabled.json):

```sh
export GRADIA_FEEDBACK_SETTINGS_FILE="$PWD/packages/blocks-feedback/examples/media-enabled.json"
# Keep the credentials and database configuration from the local quickstart.
/tmp/gradia-feedback-env/bin/python -m gradia_feedback.server
```

The request adds `attachments` while retaining the separate legacy `audio` field:

```json
{
  "message": "The chart disappears after filtering",
  "category": "bug",
  "attachments": [
    {"kind": "screenshot", "mime_type": "image/png", "data_base64": "<base64 bytes>"}
  ]
}
```

`photo` and `screenshot` accept PNG, JPEG or WebP; `video` accepts WebM or MP4.
The server rejects an unsupported kind/MIME combination, mismatched signatures,
invalid base64, unknown fields, disabled feature or excessive count/bytes before
acknowledging persistence. Defaults: four attachments, 5MiB each, 10MiB aggregate;
configurable hard ceilings: eight, 16MiB each and 32MiB aggregate. Legacy audio
retains its 256KiB ceiling. JSON/base64 has encoding overhead; this is a bounded
small-evidence profile, not resumable video or unlimited session recording.

A returned feedback record contains only attachment IDs, kind, MIME, decoded
size and SHA256. Retrieve bytes with the operator-only attachment endpoint.
Credentials establish tenant identity; knowing an attachment ID grants no access.
Downloads use `Cache-Control: no-store`, `nosniff` and attachment disposition.
The diagnostic event contract excludes captured contents, filenames and paths.
Format signatures do not certify media decoding, safety, recording duration or
accuracy. Uploaded files may contain metadata such as EXIF; no automatic metadata
stripping, content redaction, malware scanning or transcription is claimed.

## Choose persistence per host

Metadata, original idempotency receipts and audit events always use SQLite in
this reference. Select where the **new attachment bytes** live at startup:

| Profile | Configuration | Persistence boundary |
|---|---|---|
| Database | Default, or `GRADIA_FEEDBACK_ATTACHMENT_STORAGE=sqlite` | Media blobs and metadata commit in the same SQLite transaction |
| Local files | `GRADIA_FEEDBACK_ATTACHMENT_STORAGE=filesystem` plus `GRADIA_FEEDBACK_ATTACHMENT_DIRECTORY=/absolute/private/media` | Owner-private files are flushed before metadata commit and acknowledgment |

```sh
umask 077
mkdir -p /tmp/gradia-feedback-local/media
export GRADIA_FEEDBACK_ATTACHMENT_STORAGE=filesystem
export GRADIA_FEEDBACK_ATTACHMENT_DIRECTORY="$(cd /tmp/gradia-feedback-local/media && pwd -P)"
/tmp/gradia-feedback-env/bin/python -m gradia_feedback.server
```

The library equivalent is `create_app(database, keys, settings,
attachment_storage=AttachmentStorageSettings(backend="filesystem",
directory="/absolute/private/media"))`. Import this settings class from
`gradia_feedback.contracts`. The private directory is never returned by the
configuration endpoint. Existing paths with permissive permissions or symlinks
are refused; the service does not silently chmod or repurpose them. Legacy audio
continues in the database for compatibility.

Filesystem writes and SQLite are not one distributed transaction. A failed
submission rolls back database changes and attempts to remove files it staged.
Process death or failed cleanup may leave unreferenced files. Stop **all writers**,
back up the database and directory together, and inspect
`app.state.store.orphan_files()` during an exclusive maintenance window. It is
inspection only; never delete its results during active writes. Preserve a
verified backup before any backend/root change. Encryption, database replication,
PostgreSQL/S3 adapters, lifecycle deletion and retention automation remain work
for separately qualified storage profiles.

## Browser integration

`typescript/feedback-widget.tsx` is a transport-injected React component. Feature
flags and limits belong to the host configuration; server enforcement remains
authoritative. Capture starts only from an explicit action. Screen capture asks
the browser's picker for a user-selected source; camera/microphone use their
own grants. Permission denial and unsupported APIs leave written feedback usable.
Users preview/remove attachments before submitting. Disabling a feature or
unmounting stops owned capture tracks and releases preview URLs. File upload is
the fallback for browsers without recording support. No screen or camera stream
is started in the background.

For other stacks, use the OpenAPI HTTP contract and your own interface. Browser
credentials must come from an authorized host/BFF or scoped session; do not ship
long-lived operator credentials in client bundles. Native mobile capture,
iframe permissions policies, real devices and each customer host require their
own integration acceptance. A browser preview that reports `delivery: preview`
is intentionally separate from a durable service acknowledgment.

Screen-capture requirements and browser limitations follow the
[MediaDevices.getDisplayMedia reference](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia);
recording follows the [MediaRecorder reference](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder).
