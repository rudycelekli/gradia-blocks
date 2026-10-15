# Gradia Ruflo bridge — local reference 0.1.0

Package the existing Ruflo orchestration platform behind a small reviewed contract. This is an adapter, not a reimplementation, a complete Ruflo API, or a managed agent service. Upstream remains independently maintained. No upstream code or node_modules are bundled.

## Qualified scope

Verified against installed **@claude-flow/cli 3.56.9**, MIT, entry `bin/cli.js` SHA-256 `8a1020f603b35cbcabd37279bcaaf8934ff690337c0a9e8e20977bccbfddbebd`, Node 22.19.0. Read `provider.manifest.json` for the exact public operation catalog:

- `system.version`: inspect pinned edition metadata and CLI exit.
- `swarm.status`: invoke the real CLI status command; return metadata-only execution receipt.
- `swarm.init`: initialize local swarm coordination state, explicit permission and idempotency key required. Supported topology enum, maximum four agents configured. Initialization is not model-backed agent execution or proof of a running swarm.

CLI stdout/stderr are hashed and withheld. Status additionally returns selected bounded coordination fields (IDs, state, counts), excluding objectives and free-form logs. Non-JSON status output is reported unavailable. The adapter does not interpret a zero exit code as successful business execution. Callers receive process outcome, bounded output count/hash, duration, correlation ID, exact contract edition/hash and unknown cost. HTTP responses also carry correlation, duration and unknown-cost headers. No request body, credentials or raw output is written to telemetry.

## Host configuration

Python 3.12; core uses the standard library. Optional API requires separately installed FastAPI and Starlette; tests additionally use httpx. This repository does not install Ruflo. An operator supplies its reviewed existing runtime and canonical host-owned tenant workspaces. A future installation must be explicitly approved, package-version pinned and requalified; the adapter never downloads or updates dependencies.

```python
from pathlib import Path
from gradia_bridges import Bridge, Principal, Registry, RufloEdition
from gradia_bridges.api import create_app

edition = RufloEdition(
    node=Path("/absolute/reviewed/node"),
    entry=Path("/absolute/reviewed/node_modules/@claude-flow/cli/bin/cli.js"),
    manifest=Path("/absolute/blocks-ruflo/provider.manifest.json"),
)
bridge = Bridge(
    Registry([edition]),
    workspaces={"tenant-a": Path("/absolute/private/workspaces/tenant-a")},
    journal=Path("/absolute/private/bridge-receipts.sqlite"),
)
# Load this credential from the host secret store; never commit it or embed in browser code.
app = create_app(bridge, api_keys={host_scoped_secret: Principal(
    "tenant-a", frozenset({"ruflo:inspect", "ruflo:swarm:init"})
)})
```

Set `PYTHONPATH=packages/blocks-ruflo` when running from the repository root. Deploy the factory through the host's separately authorized ASGI configuration. No default public service, network binding, credentials, automatic startup or provider key is supplied.

Authenticated endpoints:

- `GET /v1/providers/ruflo/catalog?edition=3.56.9` → contract and canonical `contractSha256`.
- `POST /v1/providers/ruflo/execute` → generic reviewed-operation dispatcher. JSON requires exactly `edition`, `operation`, `arguments`, `expected_contract_hash`. Mutation additionally requires `Idempotency-Key` header. Request body ≤4096 bytes; unknown arguments and operations refused.

```json
{"edition":"3.56.9","operation":"swarm.init","arguments":{"topology":"hierarchical","max_agents":2},"expected_contract_hash":"COPY_THE_EXACT_CATALOG_HASH"}
```

Caller-supplied executable paths, directories, arbitrary CLI flags, providers, environment variables and credential payloads are unsupported. Host workspace mappings must be distinct and nonoverlapping. Every invocation uses a private workspace HOME and an explicit minimal environment, without inherited cloud credentials; `RUFLO_DAEMON_AUTOSTART=0` is always set.

## Failure and recovery

Mutation keys are reserved durably before invocation. Reusing the same key with the same request returns its existing receipt; changed requests conflict. Pending/interrupted requests remain uncertain and never rerun automatically. Timeouts, output overflow or nonzero mutation exits are uncertain because an external side effect may already have occurred. Inspect the authorized workspace before deciding on a new request. Capacity refusal after reservation is intentionally conservative and leaves a pending key; no effect is claimed. Journal failure after CLI completion likewise requires reconciliation. Keep journal backups coordinated with the workspace. Journal keys have no automatic expiry in this reference.

Maximum two in-flight processes per Bridge instance, 15-second default deadline (host maximum20), combined64KiB stdout/stderr cap; POSIX process-group cleanup attempts ordinary descendants. Multi-process deployments do not share a global semaphore. This is not an OS sandbox: workspace mapping is not filesystem/network confinement. Upstream code may read files accessible to its process or make network calls; minimal environment is not a security boundary. Use a separate unprivileged user/container with network, filesystem, process and memory restrictions before exposing to untrusted tenants. Detached descendants and resource limits beyond time/output are not fully qualified. Windows is unsupported.

Only the entry file and package identity are checked on each invocation. Transitive dependency and native runtime integrity are not comprehensively attested. Source pinning does not establish legal clearance, behavioral quality or safety. No provider-backed agent execution, model calls, paid provider routing, cross-tenant service deployment or production admission has been performed.

## Edition and capability growth

The operation catalog is data; it cannot inject executable templates. Application code owns the `OperationSpec` validators, permission/effect bindings and argv generation. A central flat schema validator checks exact fields, required/optional status, enums, supported scalar types and bounds before calling the host builder. Unsupported schema definitions refuse registration. Manifest permission/effect/schema must match these host-registered bindings; manifest edits cannot weaken mutation authority. New reviewed host specs can be registered without changing the generic HTTP dispatcher. `Registry` explicitly registers qualified `(provider_id, edition)` pairs through the `ProviderEdition` protocol. `RufloEdition(expected_version="…")` and the provider-neutral `LocalCliEdition` bind the expected package/version in host code; the manifest must match both. The generic `/v1/providers/{provider}/catalog|execute` gateway stays unchanged when a reviewed local CLI plugin or edition is registered. A synthetic second provider and newer edition test this extension mechanism; only Ruflo3.56.9 has actual upstream CLI qualification here. Adding an upstream operation means proposing a new manifest/code adapter, input/output semantics, authority/effect definition, rights review, regression and known-bad tests, compatibility/impact packet, rollback and independent admission. A changed edition/contract remains refused until the host registers it. No watcher, hot-loading or automatic future-command activation exists.

Run deterministic fixture/API tests and optional pinned-local smoke:

```sh
PYTHONPATH=packages/blocks-ruflo python -m unittest discover -s packages/blocks-ruflo/tests -v
```

The actual-local smoke is opt-in through host `GRADIA_RUFLO_TEST_ENTRY` and `GRADIA_RUFLO_TEST_NODE` absolute paths; it skips when these are absent. CI without that runtime proves fixture behavior only. Local smoke invokes version, status, init and a separate-process status confirming persisted coordination state inside a disposable private workspace with no inherited provider credentials; it does not call a model.

Original bridge code: Apache-2.0. Upstream @claude-flow/cli: MIT, [source](https://github.com/ruvnet/claude-flow). Upstream trademarks are not a partnership claim.

## Observed memory integration blocker

Local investigation used actual `memory store --no-embedding`, `retrieve` and `list` in a disposable workspace. Store exited0 but warned that sql.js fallback failed `wal_checkpoint(passive)` and persistence was not guaranteed; the following separate-process retrieve exited1 (key missing), and list returned empty. Memory operations are therefore **not exposed** in this edition. Durable memory integration needs a qualified native runtime/persistence profile and restart acceptance tests. No rebuild or new native dependency installation was performed.

## Attempt telemetry

Authenticated `GET /v1/providers/ruflo/diagnostics` returns only the caller tenant's last100 volatile HTTP-attempt metadata records: constant operation, result/status, generated correlation, timestamp, duration and unknown cost. Unauthenticated/unknown-tenant records stay in a separate private ring unavailable through this API. Rings evict older records and reset on process restart; no central collection or complete audit coverage is implied. Query strings, arguments, tokens, workspace paths, raw output and memory values are excluded. Durable execution receipts remain in the separate SQLite journal for mutations.

An edition object pins its manifest bytes when registered. In-place manifest changes refuse until a new explicitly reviewed edition object is registered; workflows retain their expected contract hash. This does not attest the full transitive runtime or make upstream files immutable.
