# Skill intake evaluator tests

Run from the repository root:

```sh
PYTHONPATH=packages/blocks-skills python -m unittest discover -s packages/blocks-skills/tests -v
```

The fixtures are synthetic and created inside temporary directories. A benign
baseline carries complete byte hashes, a pinned source, an attributed license and
a real relative resource. Each adversarial case changes one property of that
baseline so a rejection is meaningful rather than a side effect of missing setup.

The suite checks path and symlink containment, complete inventories, byte and
license integrity, source identity and revision, local references, normalized
duplicates, conspicuous authority escalation, bounded content, no script execution
and no raw-content disclosure in reports. The unreachable external reference checks
that a normal link does not require fetching its destination during screening.

Passing these tests does not establish semantic prompt-injection safety, licensing
rights, tool availability, upstream quality, performance, or production admission.
The static screener remains one evidence source in a separate approval process.
