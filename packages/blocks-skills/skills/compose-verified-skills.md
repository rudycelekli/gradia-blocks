---
name: compose-verified-skills
version: 0.1.0
description: Select and pin complementary Gradia reference skills while avoiding duplicate instructions and unsupported compatibility claims.
---

# Compose a skill plan

Search `GET /api/blocks/v1/skills?q=<job>&limit=12`. Summaries identify origin,
license, edition, content hash, screening status and host-tool dependencies.
Retrieve instructions only for the selected job; category overlap is a routing
hint, not evidence that two skills are interchangeable.

1. Identify the required outcomes and the host tools already authorized for them.
   Prefer an existing maintained skill over restating its instructions. Keep
   unrelated capabilities out of the selection.
2. Select at most eight complementary retrievable IDs. Request
   `GET /api/blocks/v1/skills/plan?ids=<id-a>,<id-b>`. The response pins content and
   resources, unions declared permissions and reports duplicate/overlap warnings.
   It performs no execution or permission grant.
3. Retrieve each selected detail using `?version=<edition>&sha256=<digest>`.
   Refuse a mismatched edition/hash. Load supporting resources through the detail's
   listed resource endpoints only when the active task needs them; verify their
   hashes too. Preserve the source license and attribution.
4. Assign one responsibility to each selected skill. For overlap, choose an owner
   and keep other references as optional evidence. Preserve distinct platform,
   language, license and host-tool requirements rather than flattening them.
5. Report missing tools or dependent skills before attempting their operations.
   Instructions and `requiredPermissions` describe requirements; the human and
   host still determine actual authority. Never run fetched scripts automatically.
6. Execute only separately authorized work. Compare the composition against a
   frozen task baseline and independent acceptance cases. Static screening alone
   establishes neither task quality nor safety. Record failures, human effort,
   latency and known/missing costs.

An improved merged skill is a new artifact: retain upstream provenance and terms,
change its edition/hash, and independently evaluate it. A plan's
`compatibility: not_evaluated` must not be relabeled as tested compatibility.
