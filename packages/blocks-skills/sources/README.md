# Pinned upstream skill intake

`sources.json` records six pinned public GitHub repository trees and 678 discovered `SKILL.md` paths. It imports 38 bounded candidate directories, preserving the complete selected directory and the applicable upstream license bytes. They are third-party reference material, not Gradia-created skills, not an endorsement, and not production-admitted capabilities.

The files were fetched through GitHub's API and raw-file endpoints. No upstream install scripts, executable assets, model providers, authenticated enterprise tools or skill instructions were executed. The retrieval budget was extended from 30 to 40 candidates to add twelve complete text-focused skills; the six-repository, 30 MiB download and per-file/directory limits remained in force. Actual copied content is approximately 1.63 MB across 244 files.

`repositories` contains the broad discovery inventory. `sources` contains license-scoped pinned sources, because a repository-wide license assumption would be incorrect for Anthropic and OpenAI. `candidates` contains exact file hashes, completeness gaps, executable assets, dependencies and overlap clusters. A recorded `imported-reference` status means the source was copied with provenance; only the separate Gradia screening receipt can declare static screening passed. Behavioral quality and production admission remain unevaluated.

Known exclusions:

- Vercel Agent Skills: no explicit license file found in the pinned repository tree, so inventory only.
- Anthropic DOCX, PDF, PPTX and XLSX skills: per-skill license restrictions independently checked and hashed; no skill content copied.
- Script-bearing candidates remain subject to execution review. Text-only does not imply safe, complete, effective or authorized.
- Some upstream skills reference absent companion scripts or unselected skills. These remain quarantined, with reasons preserved.

Overlap clusters recommend task routing rather than concatenating all prompts. Existing upstream bytes remain unmodified. An agent must retrieve only the selected skill and required companions; retrieved instructions grant no authority over the host's permissions or approval policy.
