# Gradia Composition Planner — reference 0.1.0

Find the consumers affected by an adapter update before replacing their locks. This independently runnable, Apache-2.0 reference uses Python's standard library. It never fetches a provider, executes a workflow or grants promotion authority.

## Working scope

- Build a reproducible lock over the selected roots' transitive dependency closure. Retain exact edition, artifact and contract pins, port connections and unresolved inputs.
- Compare two authorized snapshots using the union of old/new reverse dependency edges. Changed or removed nodes and changed connections identify direct/transitive consumers, workflows and private variants.
- Report stale dependency pins, port schema mismatches, removed roots and unknown verification. An invalid candidate cannot become a lock. Repinning alone retains `compatibility: UNKNOWN` and `promotionAuthorized: false`.
- Reproduce a stored lock against the operator's graph, scope and roots through `verify_lock`. Recomputing an altered lock's hash cannot make it match the expected plan.
- Generate a dependency Mermaid diagram from the same validated graph. Diagrams depict declared dependencies, not observed runtime calls.

The fictional example pins existing Feedback/Ruflo manifest identities, then proposes a synthetic Ruflo contract change. The impact report identifies the bridge, feedback-triage workflow and tenant-private customization. The workflow and customization are fixtures; they have not been executed. See [example provenance](examples/provenance.json).

## Run from repository root

```sh
PYTHONPATH=packages/blocks-composition python -m gradia_composition lock \
  --graph packages/blocks-composition/examples/baseline.json \
  --scope demo-tenant --root custom-triage

PYTHONPATH=packages/blocks-composition python -m gradia_composition impact \
  --graph packages/blocks-composition/examples/baseline.json \
  --candidate packages/blocks-composition/examples/candidate-stale-pins.json \
  --scope demo-tenant --root custom-triage

PYTHONPATH=packages/blocks-composition python -m gradia_composition diagram \
  --graph packages/blocks-composition/examples/baseline.json --scope demo-tenant

PYTHONPATH=packages/blocks-composition python -m unittest discover \
  -s packages/blocks-composition/tests -v
```

CLI exit 0 means the requested plan/report was produced. An impact report may have blockers and unknown verification; exit 0 never means a safe update. Malformed or unauthorized graph requests emit `REFUSED` to stderr with exit 2 and no partial result.

Python API:

```python
from gradia_composition import read_json, build_lock, compare_graphs, verify_lock

graph = read_json("packages/blocks-composition/examples/baseline.json")
roots = ["custom-triage"]
lock = build_lock(graph, roots, expected_scope="demo-tenant")
verification = verify_lock(lock, graph, roots, expected_scope="demo-tenant")
```

## Contract and trust

Graph schema `gradia.composition.graph.v1` requires exactly `schema`, `scope`, `nodes`, `connections`. Nodes carry exactly `id`, `kind`, `edition`, `artifactSha256`, `contractSha256`, `scope`, `requires`, `inputs`, `outputs`. Kinds are bridge/block/workflow/variant/skill. Dependency pins contain exactly id/edition/artifactSha256/contractSha256; every dependency must exist. Ports map names to schema SHA-256 strings. Connections declare fromNode/fromPort/toNode/toPort, must follow a declared dependency and have a single writer per input. Schema identity is checked; schema subtyping, field mappings, authority/effects, delivery and recovery semantics are not implemented.

`expected_scope` is supplied by trusted host/operator code, independently of graph contents. It is not authentication. A service must derive it from actual authenticated authorization; never copy an untrusted graph's scope into it. Public nodes cannot require tenant-private nodes. A graph may contain only public and the specified tenant's nodes. Foreign scope is refused without foreign IDs in the error. Public publication must use a separately reviewed public-only snapshot; impact reports can include authorized private node names and must remain in that tenant. Locks omit unselected private nodes but retain a digest of the supplied graph; do not treat such a digest as an anonymization guarantee.

Limits: 1 MiB canonical document/file, depth32, 30,000 visited values, 128 nodes, 512 dependencies/connections, eight roots, 32 ports per direction. Identifiers are restricted ASCII. Duplicate fields, cycles, missing nodes, unsupported fields, floats/nonfinite scalars, executable/diagram syntax in IDs and shared/cyclic direct Python containers are refused. The local reader refuses symlink leaves, hardlinks and nonregular files. Parent directories must still be operator-controlled; this is not a filesystem sandbox.

SHA-256 pins are declared identities, not signatures, authorization or artifact verification. A full attestation/consumer-contract evaluator must independently verify source bytes, provider effects, grants, workload outcomes and recovery before promotion. Coverage is only the supplied snapshot, not an inventory of all deployments. Renames are removal/addition; there is no inferred semantic equivalence. No automatic migration, resolver installation, background watcher, canary, rollback executor or continuing service is active.

## Evidence and telemetry

Locks and impact reports contain metadata and canonical hashes; they include graph IDs/ports within the authorized scope. Reports list the exact baseline/candidate identity, affected consumers, blockers and per-node `UNKNOWN` verification. They do not collect payloads or contact an external telemetry collector. Planning cost is not measured. Hosts may wrap calls in the existing metadata tracing contract; this package has no managed tracing backend.

The tests include separate-process CLI checks and independently authored adversarial fixtures. Mechanical validation is not enterprise admission or a claim that arbitrary composed software works.

`observe_plan('lock', graph=graph, roots=roots, expected_scope='demo-tenant')` wraps the API in an optional `{result, receipt}` envelope. It also supports impact/diagram/verify-lock. Each receipt has a generated correlation ID, UTC timestamp, elapsed time, fixed operation/result code, output identity and explicit unknown cost. A refused call has no result. Receipt metadata omits tenant names, node labels, input bodies and raw errors; returned plan contents remain tenant-scoped. A host can store receipts locally or export them through its approved collector. Nothing is transmitted or persisted automatically, and CLI parsing attempts occur outside this wrapper.
