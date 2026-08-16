# Refactoring Notes

## Goal

This refactoring changes project structure only, for public GitHub release
and maintainability. It makes no change to analysis behavior.

The original single-file script was moved to a package layout:

```text
original: comet_browser_reconstruct.py
new:      src/comettrace/legacy_engine.py
```

`legacy_engine.py` preserves the original analysis logic byte-for-byte. The
newly added code only provides:

- Python package layout
- CLI entry point
- backward-compatible launcher
- install scripts
- VS Code launch configuration
- documentation

## Behavior preservation

The engine is release-frozen. Its SHA-256 digest is pinned in
`release_manifest.json` and `comettrace/__init__.py` and can be re-checked at
any time:

```bash
python -m comettrace.provenance
```

The digest of the engine as shipped and validated (report schema_version
0.37) is:

```text
f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80
```

An earlier historical manifest (`refactor_manifest.historical.json`) pinned a
different digest (`6d9c702d...`) for an earlier engine revision. The value
above is the engine actually released and cited in the paper.

## Why the engine was not split into many modules

The engine is a large file with many accumulated version-patch layers
(v0.2 through v0.37) that override earlier definitions. Physically splitting
these functions would change import dependencies and override order, which
could subtly alter behavior. To keep the released engine identical to the one
whose outputs are cited in the paper, this release follows three rules:

1. Leave the engine logic unchanged (frozen).
2. Provide only a public, installable package structure around it.
3. Defer any second-stage modularization until sufficient test fixtures exist.

## Suggested second-stage refactor

Once behavior-equivalence tests are in place, the engine could be split into,
for example:

```text
src/comettrace/
  input_discovery.py
  indexeddb_extract.py
  records.py
  grouping.py
  classification.py
  extractors/
    prompt.py
    metadata.py
    actions.py
    urls.py
    reasoning.py
    final_answer.py
    temporal.py
  interpretation/
    deletion.py
    private_mode.py
    residue.py
    task_outcome.py
  render/
    json_report.py
    html_report.py
```

Before any such split, the same dataset should be compared across the old and
new code for: JSON semantic diff, thread count, prompt/final-answer
extraction, Browser Control / Computer mode classification, deletion/private/
residual fields, and HTML rendering.

## Public release caution

Do not commit real `.ldb`, `.log`, `.zip`, or output JSON/HTML to the
repository. They can contain prompts, emails, calendar entries, uploaded-file
URLs, and account artifacts.
