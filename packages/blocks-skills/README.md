# Gradia Skills reference distribution

Two first-party Apache-2.0 instruction artifacts are served on demand by the public catalog API. An agent can fetch a small summary, retrieve one pinned version and use it without installing a local skill folder. The host still stores versioned content; retrieval is not permission to execute it.

- `GET /api/blocks/v1/skills`: bounded public summaries.
- `GET /api/blocks/v1/skills/{id}`: selected reference content, version, digest, required permissions and limitations.

Enterprise private registries, SSO/SCIM, organization allowlists, publication roles, signed approvals, revocation, regional retention, usage billing and authenticated private retrieval remain roadmap. Do not send private instructions to these public endpoints. No claim of globally best skills, secure arbitrary skill execution or a live enterprise service is intended.

This export contains the source instruction artifacts; the hosted catalog API and its projection generator live in the main Gradia application. The content hash binds original UTF-8 Markdown bytes. Pin the hash before execution and follow the host's tool grants. A changed version is a new review decision; a public cache lifetime does not guarantee immediate revocation. Public reference editions are immutable; these prototype routes are not a revocation service.
