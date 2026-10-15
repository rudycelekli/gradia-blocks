# Gradia Blocks

**Adaptable capabilities. Explicit contracts. Evidence before promotion.**

Gradia is an AI Research & Implementation Lab. This repository starts our open capability library with a working **Feedback reference block**, an independent **candidate admission screen**, and two selectively fetched **Skills**. Build a small useful workflow, inspect its contract, and adapt it to your application.

![Feedback architecture](diagrams/feedback-architecture.svg)

## What you can run

| Artifact | What it does | Current boundary |
|---|---|---|
| [Feedback](packages/blocks-feedback/README.md) | FastAPI/SQLite API, tenant-scoped reports, operator triage, idempotency, local telemetry, optional audio attachments | Local reference candidate; no managed hosting or production admission |
| [TypeScript client](packages/blocks-feedback/typescript/client.ts) | Calls the same HTTP contract; distinguishes acknowledgment from uncertain transport | A client, not another server implementation |
| [React widget](packages/blocks-feedback/typescript/feedback-widget.tsx) | Configurable labels/accent/categories, optional explicit microphone capture and audio review | Host supplies transport and authority; never embed operator keys in a browser |
| [Admission screen](packages/blocks-admission/README.md) | Checks pinned artifacts and independently supplied gate receipts | Does not run tests, prove claims or merge changes |
| [Skills](packages/blocks-skills/README.md) | First-party integration/review instructions with explicit scope | Instructions grant no tool permissions or credentials |
| [Diagrams](diagrams/README.md) | Standalone interactive HTML and SVG; typed source IR | Planned nodes remain labeled; no external provider calls |

## Explore and discover

Try the [Blocks & Skills explorer](https://www.gradiahq.com/blocks). The public
catalog and reference-skill endpoints provide metadata and instructions, not
hosted feedback execution or private tenant access.

A native [Rust telemetry validator](packages/blocks-contracts-rust/README.md)
checks the current Python metadata event contract. It is a contract core, not a
Rust-native feedback server or a throughput claim. Run it with:

```sh
cargo test --locked --manifest-path packages/blocks-contracts-rust/Cargo.toml
```

## First five minutes

Python 3.12+ is recommended for the full reference suite. From this repository:

```sh
python3 -m venv .venv
.venv/bin/pip install -e './packages/blocks-feedback[test]'
.venv/bin/python -m pytest -c /dev/null -p no:cacheprovider packages/blocks-feedback/tests -q
PYTHONPATH=packages/blocks-admission .venv/bin/python -m unittest discover -s packages/blocks-admission/tests -v
```

Follow the [Feedback local-server instructions](packages/blocks-feedback/README.md#run-locally) to create a private credential file and start the service at `127.0.0.1:3191`. Open `http://127.0.0.1:3191/docs` for the generated OpenAPI contract. No default credentials, external provider account or API spend is required. Keep the local database and credential file outside source control.

For the client and widget checks, use Node 22 and npm:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run test:client
npm run check:widget
```

Feedback acknowledgment follows a committed transaction. Retry an uncertain submission only with its original payload/key; operator triage uses an expected revision. Local telemetry excludes message/audio bodies and does not phone home. Audio is optional and off by default; the reference does not transcribe it.

## Compose deliberately

Start with feedback → tenant-scoped triage. The [workflow diagram](diagrams/feedback-workflow.html) shows how approvals, notifications and linked evidence could extend it; those planned blocks are not shipped here. Language-neutral HTTP contracts allow additional SDKs and implementations, each with its own conformance evidence. Supported runtime and schema editions matter more than the number of languages listed.

Read [SECURITY.md](SECURITY.md) before exposing the reference remotely. Review [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a capability. These are development artifacts: no certification, complete security proof, universal compatibility or autonomous merge authority is claimed.

## License

Gradia-authored source is Apache-2.0. Diagram output preserves Archify MIT attribution and embedded font licenses. Third-party dependencies keep their licenses; see [NOTICE](NOTICE). This export contains no customer implementations, private operational documents or provider credentials.
