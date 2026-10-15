# Gradia candidate admission screen — local prototype

This standard-library Python module screens immutable block candidates against independently supplied review receipts. It **does not run candidate code**, infer rights from a license name, certify enterprise readiness, or merge/promote anything. A passing screen produces `ELIGIBLE_FOR_REVIEW`; production release remains a separate human decision. No AI model, Agentic QE integration, remote service, API server, or background watcher runs here.

## Run

From the repository root, with Python 3.12+:

```sh
PYTHONPATH=packages/blocks-admission python3 -m unittest discover -s packages/blocks-admission/tests -v
PYTHONPATH=packages/blocks-admission python3 -m gradia_admission \
  --artifact-root /absolute/path/to/immutable-block \
  --manifest /absolute/path/to/control/candidate.json --fingerprint
PYTHONPATH=packages/blocks-admission python3 -m gradia_admission \
  --artifact-root /absolute/path/to/immutable-block \
  --manifest /absolute/path/to/control/candidate.json \
  --policy /absolute/path/to/trusted-operator/policy.json \
  --receipt /absolute/path/to/control/behavior-receipt.json
```

Supply one `--receipt` for **each** required gate, repeating the argument (maximum 64 receipts, 2 MiB per JSON control input). Exit codes: `0` eligible or fingerprint success; `1` blocked by missing/failed/untrusted evidence; `2` invalid input. Standard output is JSON. The sample candidate is intentionally fictional and has no accepted receipts.

For in-process integration, import `evaluate(manifest, artifact_root: Path, receipts, policy)` and handle `InputError`. The returned object includes gate `PASS`/`FAIL`/`UNKNOWN` results, exact hashes, blockers, decision and `promotion_authorized: false`. An authenticated future admission API can wrap this function; there is no endpoint in this prototype.

## Control-plane schema and trust

`examples/candidate.json` documents the manifest shape. This is a **screening envelope**, separate from any block's native runtime manifest: adapt its identifier, source revision, license, relevant profiles, budgets and privacy configuration without modifying source packages. Store the envelope, policy and receipts outside the artifact root to avoid circular hashes. CLI enforces this boundary.

Supported profiles are `service`, `ui` and `enterprise_connector`; choose every relevant profile. The base gates are rights, behavior, security, tenant isolation, privacy, API contract, telemetry, composition, upgrade, recovery, maintainer and support. UI adds accessibility. Enterprise connectors add identity, permissions and provider-failure checks. Unknown profiles and inapplicable/duplicate receipts block screening. Adding a new profile requires a protocol change and fresh evidence.

An operator-controlled policy has this form:

```json
{
  "schema_version": 1,
  "protocol_sha256": "replace-with-fingerprint-output",
  "limits": {"max_cost_usd": 2, "max_duration_seconds": 120},
  "reviewers": {
    "security-review-runner": {"kind": "automated", "gates": ["security"]},
    "rights-reviewer": {"kind": "human", "gates": ["rights"]}
  },
  "receipt_sha256": ["canonical-digest-of-independently-reviewed-receipt"]
}
```

Reviewer entries must be objects with kind `human` or `automated` and a nonempty unique list of exact supported gate names; strings and substring scopes are rejected. Receipt pins must be unique lowercase SHA-256 strings. Malformed reviewer policies produce `INPUT_ERROR`. Populate reviewers/scopes and receipt pins independently of the candidate author. A receipt has this shape:

```json
{
  "schema_version": 1,
  "candidate_id": "example.feedback/0.1",
  "artifact_sha256": "exact-tree-fingerprint",
  "manifest_sha256": "exact-screening-envelope-fingerprint",
  "protocol_sha256": "exact-protocol-fingerprint",
  "reviewer_id": "security-review-runner",
  "gate": "security",
  "verdict": "PASS",
  "checks": {"total": 12, "passed": 12, "failed": 0, "skipped": 0},
  "evidence_refs": ["private-evidence://immutable-run/security-report"],
  "measurements": {"cost_usd": 0, "duration_seconds": 18}
}
```

These are illustrative values, not actual evidence. Pin a canonical receipt digest with `gradia_admission.evaluator.digest(receipt_object)`. JSON object keys are sorted, UTF-8 uses non-ASCII characters directly, and separators are compact. JSON duplicate keys and nonfinite numbers are rejected. Reformatting JSON does not change its canonical hash. Changing the manifest—including its profiles—invalidates existing receipts.

Hashes **bind content; they do not prove its truth or establish reviewer identity**. Pins are trust anchors administered outside the candidate boundary; they are not signatures. Anyone who controls the policy can approve arbitrary assertions. Do not accept a policy bundled by the candidate or let a candidate execution write the trusted policy. Protect and review the evaluator revision and protocol separately in CI. Evidence references are retained for the maintainer; this tool does not fetch them or verify their contents. A trusted reviewer must inspect immutable reports before pinning. Rights, maintainer and support gates require a policy-designated human reviewer, regardless of model recommendations.

Each gate receipt needs a positive complete check count, zero skips/failures, explicit verdict and evidence references. Cost and duration measurements must be finite and nonnegative; unknown values block. Declared budgets cannot exceed the operator's limits, and each receipt must stay within candidate budgets. These are **per-receipt limits**, not an aggregate experiment budget or a spend limiter: the screen never starts jobs or spends money. A separate orchestrator must enforce aggregate cost and hard execution deadlines before running checks.

## Exact artifact identity

The tree fingerprint hashes a canonical sorted JSON array of `{path, bytes, sha256}` entries. Paths are relative POSIX paths; bytes are content length; the inner hash is SHA-256 of file bytes. Source, tests, documentation, lockfiles and dotfiles are included. File permissions and timestamps are not included. Fixed excluded directories anywhere in the tree: `.git`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.venv`, `node_modules`; fixed excluded suffixes: `.pyc`, `.pyo`. Every encountered symlink is rejected, including an excluded directory entry if that entry itself is a symlink. Contents of excluded directories are not traversed or inspected. Special files and empty artifacts are rejected. Limits are 10,000 included files, 100 MiB of included data, 20,000 encountered directory entries (including excluded entries), and directory depth 64. Traversal streams entries instead of materializing an unbounded directory listing. Entry/depth limits are evaluator constants; protect the evaluator revision as well as the protocol hash. Dependency bytes inside excluded directories are **not attested**; lockfiles do not prove resolved dependencies are safe.

Hash an immutable staged snapshot. This scanner checks size while reading but is not a filesystem snapshot or protection against concurrent writers. Release packaging must separately attest the final distribution, dependencies, executable permissions, build environment and evaluator revision. The protocol fingerprint is the canonical hash of bundled `protocol.json`; any protocol change invalidates prior pins.

## What passing means

The screen says the configured evidence set is complete and consistent with its declared scope. The gate names are a **minimum checklist**, not implementations of every test: each candidate needs an independent test plan covering its actual threats and contracts. Reviewers must test the evaluator too and keep protected holdout cases separate from generation. Cross-tenant requests, secret/redaction failures, retry ambiguity, provider revocations, accessibility, API breakage, consumer compatibility and rollback require runnable tests appropriate to the candidate. An open-source source repository, a successful unit test or a model opinion is insufficient.

The current regression suite includes a fully synthetic eligible case and deliberately broken evidence: changed source, changed profiles, missing/duplicate/unknown gates, self-declared approval, unauthorized or stale receipts, skipped/zero/failed checks, unknown verdicts, unsafe privacy declarations, exceeded budgets, automated-only rights review, stale protocols, symlinks and malformed JSON. These tests verify screening behavior, **not any real block's admission**.
