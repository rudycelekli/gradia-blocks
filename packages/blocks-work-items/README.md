# Gradia Work Items — reference 0.1.0

Create durable work-item proposals, apply explicit state transitions and connect an authorized Feedback intake to human triage. This Apache-2.0 package runs independently with Python3.11+, FastAPI and a private SQLite file. It supplies an HTTP state-management API and a host-configured Feedback handoff; a visual Kanban board is not implemented.

## Working capability

- Host-configurable states, transition edges and initial state. Defaults: proposed → open → in_progress → done, cancellation from active states, explicit done → open reopening.
- Tenant/role/actor access rules. Submitters create/read their own items; operators can read and triage within their tenant; bridge principals can bind Feedback sources and reconcile their own pipeline.
- Idempotent creation, revision-safe changes and atomic durable mutation events. Concurrent duplicates share one outcome; competing updates cannot both consume the same revision.
- Source binding uniqueness per tenant, configured bridge actor, source kind and source ID, even across new idempotency keys and process restarts. Changed content conflicts rather than overwriting a proposal.
- Explicit feature flags for writes, assignments and transitions. They can change on restart. Structural workflow changes refuse an existing database until an explicit offline migration is designed.
- Authenticated session identity and conditional principal/configuration headers. The Feedback bridge validates both tenants and pins request authority before transferring text.
- Metadata-only mutation events, bounded HTTP-attempt diagnostics and handoff receipts with correlation/time/duration, coverage, result and unknown cost. Handoff receipts retain verified principal/configuration hashes and per-service correlation IDs where a response was received. No external telemetry collector activates automatically.

## Host API

```python
from gradia_work_items import create_app, Principal, Settings, Transition

settings = Settings(
    states=("proposed", "open", "done"), initial_status="proposed",
    transitions=(Transition(from_state="proposed", to_state="open"),
                 Transition(from_state="open", to_state="done")),
    assignments=True,
)
app = create_app(
    "/absolute/private/work-items.sqlite",
    api_keys={host_secret: Principal(tenant_id="tenant-a", actor_id="triage", role="operator")},
    settings=settings,
)
```

The host provides secrets; no default key or public binding is included. Tenant/role/actor come from host credentials, never a request-body tenant field. Assignee is an opaque label, not a grant, directory check or notification. API tokens belong on the server; never embed operator or bridge keys in browser code.

Install from this source folder or set `PYTHONPATH=packages/blocks-work-items`. The supplied `requirements-tested.txt` records exercised direct dependency versions; it is not a transitive production lock.

An explicit loopback server can be started with:

```sh
export GRADIA_WORK_ITEMS_CREDENTIALS_FILE=/absolute/private/work-items-credentials.json
export GRADIA_WORK_ITEMS_DATABASE=/absolute/private/work-items.sqlite
# Optional private JSON Settings file and port; default port is3192.
export GRADIA_WORK_ITEMS_SETTINGS_FILE=/absolute/private/work-items-settings.json
PYTHONPATH=packages/blocks-work-items python -m gradia_work_items.server
```

Credential-file shape is `{ "HOST_SECRET": { "tenant_id": "tenant-a", "actor_id": "triage", "role": "operator" } }`. Put actual secrets only in a private host file, mode0600, outside this package. Configuration files are bounded64KiB, regular, nonsymlink and nonhardlinked. Omit the optional settings variable to use defaults.

## HTTP contract

All `/v1/work-items` operations require bearer authentication. OpenAPI describes schemas without granting authority.

| Operation | Scope and behavior |
|---|---|
| `GET /session` | Authenticated tenant/actor/role, block edition and configuration digest |
| `GET /config` | Current host-owned workflow and feature profile |
| `POST /v1/work-items` | Required `Idempotency-Key`; title, optional description, optional bridge-only source |
| `GET /v1/work-items?limit=25&cursor=…` | Tenant-scoped list; optional status/assignee filters; nonoperators see their own actor's records; limit≤100 |
| `GET /{id}` | Same record-level read rules |
| `PATCH /{id}` | Operator only; strict integer expected_revision plus status and/or nullable assignee |
| `GET /{id}/events?after=0&limit=100` | Operator only, tenant-scoped durable mutation metadata |
| `GET /by-source/feedback/{source_id}` | Bridge-only reconciliation in that actor's tenant/pipeline |
| `GET /diagnostics/attempts` | Operator-only last100 volatile metadata attempts for that tenant |

Paths abbreviated with `/session`, `/{id}`, etc. share the `/v1/work-items` prefix. Invalid fields, duplicate JSON keys, oversized/deep payloads and illegal transitions refuse. An update with an old revision returns409. Feature flags never confer extra roles. No-op updates refuse rather than creating fictional work.

Create acknowledgment: `{status:"acknowledged",item:{…},replayed:false}`. A repeated create returns the original create snapshot with `replayed:true`, even if an operator later changed the item. Read the item to obtain current state. Source-bound retries with different content return409. The durable create outcome and its idempotency key commit atomically with the mutation event before acknowledgment.

`If-Gradia-Principal`, `If-Gradia-Configuration` and `If-Gradia-Block-Version` optionally bind a request to the earlier authenticated session fingerprint/profile; mismatch returns409 before access/write. Responses after authentication expose `X-Gradia-Principal-Sha256` over canonical tenant/actor/role metadata. This digest is not a credential, signature or independent identity attestation. Services and their HTTPS origins remain trusted host configuration.

## Actual Feedback → proposal handoff

Requires **Feedback0.2.1 operator** and **Work Items0.1.0 bridge**, both in the explicitly configured same tenant. Older Feedback0.2.0 lacks the identity/conditional contract and cannot use this profile. Use separate least-scoped host tokens. Feedback is a separate package; core Work Items does not import or require it.

```python
from gradia_work_items.handoff import FeedbackHandoff

with FeedbackHandoff(
    source_origin="https://feedback.example.com",
    target_origin="https://work-items.example.com",
    source_token=host_feedback_operator_secret,
    target_token=host_work_items_bridge_secret,
    tenant_id="tenant-a", profile_id="feedback-proposal",
) as bridge:
    outcome = bridge.run(feedback_id, expected_revision=1)
```

The runner checks authenticated tenants/roles/editions, reconciles an existing source binding, checks a proposal-enabled target configuration, retrieves the exact Feedback revision and writes one proposal. Requests carry principal/configuration conditions, including the source read. Credential rebinds and profile changes refuse instead of silently moving data or using a changed workflow. The target must initially create `proposed` items; the bridge never advances them through human triage states.

Only the message and a bounded derived title are copied. A source digest binds selected id/revision/message/category fields, not every attachment or the truth of the original submission. Photos, screenshots, videos, audio and context links stay in Feedback; the source ID supports a separately authorized retrieval. Incoming content is data and is never treated as agent instructions or code.

`proposed` means a creation snapshot was acknowledged. `recovered` means an already bound current item was read. Neither means the work was accepted or completed. A timeout, bad acknowledgment, unknown status or changed response identity after a POST can return `uncertain`, because the database may already have committed. **Do not generate a new source identity to retry.** Explicitly rerun the same id/revision with the same configured actor; reconciliation recovers the existing item before a new write. A changed source revision conflicts and needs an explicit operator decision. No background polling or automatic retry is started.

Stable bridge actors define pipeline namespaces. Different configured actors can intentionally produce separate proposals for the same feedback. Renaming an actor can change that namespace and needs a migration plan. `profile_id` changes the idempotency-key derivation; it does not bypass source-binding uniqueness or create a new authority scope. Human changes to a recovered item are preserved.

## Bounds and evidence

Requests≤48KiB; states≤12 and edges≤40; titles≤180 characters, descriptions≤10,000. Handoff responses≤64KiB with identity encoding; compressed responses refuse. URLs and credentials are configured by the host, never supplied through Feedback data. Plain HTTP is restricted to explicit loopback origins; HTTPS uses normal certificate verification. Proxy-environment and redirect following are disabled for the default clients. There is a cooperative15-second invocation budget and≤3-second per-operation timeout; these are library/network bounds, not an OS-enforced cancellation sandbox.

HTTP attempt rings reset on restart, evict old entries and exclude queries, bodies and tokens. Durable events cover committed mutations, not all read/refusal attempts. SQLite payload records intentionally store the authorized title/description; “metadata-only telemetry” does not mean payload-free application storage. Quotas, retention, off-site backups, key lifecycle, SSO, HA and complete audit export remain host/enterprise work.

Database startup checks table/column/index structure plus persisted workflow identity. It is not cryptographic database attestation; parent directories and the host filesystem must remain trusted. Nonregular, leaf symlink, hardlink or group/world-accessible database files refuse. Multiworker throughput and crash/FS failure recovery are not comprehensively qualified.

Run regression tests and the two independent-service TCP replays from repository root:

```sh
PYTHONPATH=packages/blocks-work-items:packages/blocks-feedback:packages/blocks-composition python -m pytest \
  -c /dev/null -p no:cacheprovider packages/blocks-work-items/tests packages/blocks-feedback/tests -q

PYTHONPATH=packages/blocks-work-items:packages/blocks-feedback python \
  packages/blocks-work-items/examples/tcp_replay.py
```

The replay uses generated private credentials/stores and real loopback TCP. It exercises normal creation and intentional post-commit acknowledgment loss, target restart, source reconciliation, human transitions, stale revision rejection and metadata-only durable events. Fixtures are synthetic; no customer data, model/provider calls, enterprise admission or broad business acceptance is established.

The generated `examples/feedback-to-work-item.graph.json`, matching `.lock.json` and `.mmd` bind current declared implementation fingerprints and API schema identities. `examples/build_recipe.py` regenerates these from the adjacent Feedback and Composition packages. The lock retains compatibility UNKNOWN and no promotion authority; separate TCP/test receipts establish only the exercised local fixture. Rebuild from repository root with all three packages on PYTHONPATH when changing a pinned implementation or schema.
