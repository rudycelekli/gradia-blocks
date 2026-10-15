# Gradia Skills: pinned reference distribution and static intake

Retrieve a small summary, select one version/hash, then fetch only the supporting
references the job needs. Gradia-authored instructions/code are Apache-2.0;
upstream bundles retain their own pinned MIT or Apache-2.0 licenses. This package
is not a blanket relicensing of upstream work.

The first expanded intake inventories **678 skills across six pinned source
repositories**. It selects 38 complete candidates for inspection. The current
screen exposes **20 upstream and four Gradia references**, with 18 candidates
quarantined. Inventory, selection, static screening, behavioral evaluation and
production admission are distinct stages. No “all skills are qualified” claim is
made.

## Read and compose

- `GET /api/blocks/v1/skills?q=...&category=...&origin=...&limit=12&cursor=0`:
  bounded metadata; instruction/resource bodies are absent.
- `GET /api/blocks/v1/skills/{id}?version=...&sha256=...`: selected pinned text.
- `GET /api/blocks/v1/skills/{id}/resources/{resourceId}?version=...&sha256=...`:
  selected declared text resource with its own hash. The query pins its parent
  skill; verify the returned resource hash too.
- `GET /api/blocks/v1/skills/plan?ids=id-a,id-b`: up to eight selected references,
  exact-hash instruction deduplication, declared permissions/tools, overlap and
  dependency warnings. Compatibility stays `not_evaluated`.

Instructions never grant host/tool permissions. Do not run fetched commands or
install dependencies automatically. Some references require Ruflo, Microsoft,
GitHub or other host capabilities that have not been verified in your target.
A composition plan does not execute the workflow or prove its compatibility.

The source registry in `sources/sources.json` retains exact commits, scoped
license hashes, file inventories, dependency observations and exclusions.
Upstream bytes are unchanged. `THIRD-PARTY-NOTICES.md` links the distributed
bundles to their original source and license files. Quarantined candidate raw
files are excluded from published archives; their metadata remains inspectable.
Some unselected repositories or nested skills have unclear/restricted rights:
being public is not itself a redistribution license.

## Reproduce the static screen

Python 3.11+ is recommended; this tooling uses the standard library only:

```sh
PYTHONPATH=packages/blocks-skills python -m unittest discover -s packages/blocks-skills/tests -v
PYTHONPATH=packages/blocks-skills python -m gradia_skills.intake \
  --root packages/blocks-skills \
  --manifest packages/blocks-skills/sources/sources.json \
  --output /tmp/gradia-skill-screen.json
PYTHONPATH=packages/blocks-skills python -m gradia_skills.verify --root packages/blocks-skills
```

A successful CLI run means screening completed, not that all candidates passed.
Inspect its per-candidate statuses. The screen checks pins, complete file/hash
inventories, applicable license hashes, local reference containment, text/size
bounds, symlinks/hardlinks, duplicate text and a few authority-escalation signals.
Executable/binary assets require a separate review and are not made retrievable.
Heuristics have false positives and false negatives; neither legal clearance,
semantic security, tool compatibility nor task quality is established.

The distribution verifier requires the recorded screened identities to match a
fresh screen. Expected quarantined raw bundles can be absent in an export;
missing source cannot become retrievable. A changed source or quality rule needs
new screening and deliberate review.

## Version and update rules

Gradia references carry semantic editions; upstream editions carry exact commit
identity and content hashes. Source bytes and attribution remain separate from
Gradia routing/qualification metadata. Exact normalized duplicates become alias
candidates; lexical overlap is a review hint, never automatic equivalence.
Unmodified source reuse comes first. A combined/improved skill is a new artifact
with inherited terms and independent behavioral acceptance cases.

Existing source ZIP editions cannot silently change: archive generation compares
entry names and contents and refuses conflicting bytes. New archives use fixed
metadata for reproducibility. The monorepo website generator is not included in
this standalone export; `gradia_skills` supplies reusable screen/projection code.

Private enterprise registries, SSO/SCIM, organization allowlists, publication
roles, signed approvals, revocation, private retrieval, billing and hosted script
execution remain roadmap. Do not send private instructions to public endpoints.
No continuing background research/update job is registered by this package.
