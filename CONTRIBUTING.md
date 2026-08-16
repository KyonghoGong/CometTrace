# Contributing

Thanks for your interest in this forensic tool.

## The engine is frozen

`src/comettrace/legacy_engine.py` (report schema 0.37) is the exact
code whose outputs are cited in the accompanying paper. Its SHA-256 is pinned
in `release_manifest.json` and `comettrace/__init__.py`.

**Do not modify the engine in place.** Any change to its bytes breaks the
provenance check (`python -m comettrace.provenance`) and invalidates
the paper's reproducibility claim. New analytical behavior belongs in new,
separately versioned modules — see `docs/REFACTORING_NOTES.md` for the
planned second-stage structure.

## Everything else is open to improvement

Packaging, CLI wrapper, docs, tests, and CI can be changed freely. Please:

- keep changes behavior-preserving for the engine;
- run `python -m comettrace.provenance` before opening a PR;
- if you have acquisitions, run
  `python tests/test_output_equivalence.py <acquisition.zip>` to confirm the
  packaged and legacy entry points still agree.

## Never commit evidence

Do not add real `.ldb`, `.log`, `.zip`, or output JSON/HTML. They can contain
personal data (prompts, emails, calendar entries, account artifacts).