# Gradia block telemetry contracts — Rust reference core

An actual Rust library and CLI validates the **current Gradia Feedback metadata event contract**. This is a native Rust contract core, not a Rust feedback HTTP service, SDK for every block, or a performance claim. The working Feedback reference API remains Python.

## Run

Rust/Cargo 1.90.0 was used locally. Dependencies are exactly pinned (`serde_json 1.0.154`, `serde 1.0.229`) and resolved checksums are preserved in `Cargo.lock`. Packages are downloaded from the Cargo registry, not vendored.

```sh
cargo test --locked --manifest-path packages/blocks-contracts-rust/Cargo.toml
cargo run --quiet --locked --manifest-path packages/blocks-contracts-rust/Cargo.toml -- packages/blocks-contracts-rust/fixtures/python-events.json
# Or stream one event / an event array:
cat packages/blocks-contracts-rust/fixtures/python-events.json | packages/blocks-contracts-rust/target/debug/gradia-block-contracts -
```

The CLI accepts stdin by default or one file argument, reads at most **1 MiB**, and accepts one event or an array of 1–4096 events. Successful stdout is `{"valid":true,"events":5}` for the fixture. Invalid JSON, duplicate keys, oversize input or invalid contracts return a nonzero status and a JSON error on stderr. Errors do not echo event content. Library callers use `validate_event(&serde_json::Value)` or `validate_document`; duplicate-key protection is performed by the CLI parser before producing a Value.

## Validated scope

Both supported coverage profiles are explicit:

- `committed_mutations_only`: durable mutation event metadata, feedback identity and revision, persisted result, unevaluated acceptance and billability.
- `volatile_http_attempts`: route-template operation, HTTP method/status/result, optional refusal code, configured or unauthenticated actor identity.

Checks include exact coverage-specific field allowlists, required identifiers, SHA-256-shaped configuration digest, UUID-shaped event/correlation IDs, calendar-valid UTC timestamps, finite nonnegative durations, `privacy=metadata_only`, and scoped identity rules. Configured principals require tenant and actor IDs. Only unauthenticated volatile attempts may use null tenant/actor IDs. Unknown costs must have null amount/currency; measured or estimated costs require a finite nonnegative amount and a three-letter uppercase currency code. Currency codes are syntactically checked, not verified against an ISO registry. Optional mutation `sequence` supports the API's exported event representation.

Unexpected raw payload or credential fields are rejected. This is **not a content classification or secret-detection system**: arbitrary strings in allowed identifier fields are not evidence of correct tenant ownership or absence of encoded secrets. Hash shape does not verify a configuration; metadata does not prove an event occurred. The tool never sends data, calls models/providers, starts a server, authorizes a transaction, measures costs, executes candidate commands, or certifies production readiness.

## Cross-language evidence

`fixtures/python-events.json` contains five synthetic events exported through the actual Python Feedback API/attempt store from source revision `d7a9b2945f860bedb106f31230343ae7b3c37bea`: a committed mutation, successful HTTP attempts, a validation refusal and an unauthenticated refusal. A temporary SQLite database and synthetic tenant/operator were used; no request body, bearer credential or customer data is included. The anonymous ring was read directly in the test harness; tenant diagnostics do not expose it.

Eight Rust tests cover Python compatibility, altered privacy/coverage/identity/configuration/date/duration, missing metadata, unknown-vs-measured cost semantics, malformed documents and actual CLI success/failure/duplicate-key/size behavior. These are contract checks, not a throughput benchmark, API integration qualification or evidence that all telemetry is complete.

Owned code is Apache-2.0; see LICENSE. Dependency licenses remain their own. The crate is `publish=false`; nothing was published to crates.io.
