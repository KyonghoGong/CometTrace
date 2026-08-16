# CometTrace

*Comet Browser/Computer Artifact Reconstructor*

Forensic tool that reconstructs conversational and agentic interactions from
Perplexity Comet's IndexedDB LevelDB store, per thread, with persistence-state
annotation. Companion tool to the paper "Reconstructing Agentic AI Behavior
from Browser Artifacts: A Forensic Case Study of Perplexity Comet".

## Requirements

- **Python 3.10-3.12.** Use 3.11 or 3.12. The forensic parsing dependency
  (`ccl_chromium_reader`) does not currently import cleanly on Python 3.13+
  (including 3.14); on those versions the tool exits with
  `ccl_chromium_reader is not installed`. The paper's results were produced
  on Python 3.12.
- `ccl_chromium_reader` and `ccl_simplesnappy` (CCL Solutions Group Ltd.),
  installed from source — see Install below and paper Section III-E.
- `Brotli` and `zstd` decompression codecs. `ccl_chromium_reader` imports
  the `zstd` module at load time; if it is missing, `import
  ccl_chromium_reader` fails with `ModuleNotFoundError: No module named
  'zstd'`. The install script below installs both.

## Engine freeze policy

The reconstruction engine (`src/comettrace/legacy_engine.py`,
report `schema_version` 0.37) is **release-frozen**: it is the exact code
whose outputs are cited in the paper, and it is shipped byte-identical.
Its SHA-256 digest is pinned in `release_manifest.json` and in
`comettrace/__init__.py`, and can be re-checked at any time:

    python -m comettrace.provenance

All other files (CLI wrapper, packaging, docs, tests) are presentation and
reproducibility layers and make no logic changes. Future analytical changes
will go into new, separately versioned modules, never into the frozen engine.

## What the tool reconstructs

- conversational vs. agentic threads; Browser Control vs. Computer mode
- user prompt and thread metadata; reference-code anchors
- plan/workflow structures, tool input/output events, typed-payload candidates
- Computer/ASI-mode provider-labeled thought records (action rationales)
- Live / Deleted / stale / residual record-state interpretation
- private-mode markers; final answer; JSON and HTML reports

## Install

    # 0) confirm a supported interpreter (3.10-3.12); on Windows use the
    #    launcher to select it explicitly, e.g. `py -3.12`
    python --version

    # 1) forensic parsing dependencies (Brotli, zstd, and the CCL readers)
    bash scripts/install_ccl_dependencies.sh      # or .ps1 on Windows

    # 2) this package (editable)
    python -m pip install -e .

    # 3) verify the parsing dependency imports
    python -c "import ccl_chromium_reader; print('ok')"

## Run

    # single acquisition (folder or zip containing the Comet Perplexity
    # IndexedDB LevelDB directory)
    comettrace --input <acquisition> --output reconstruction.json \
        [--html-output reconstruction.html]

    # staged before/after comparison (deletion series)
    comettrace --before <created.zip> --after <deleted.zip> \
        --output comparison.json

    # legacy invocation remains valid
    python comet_browser_reconstruct.py --input <acquisition> --output out.json

`--target-reference <code>` filters the report for a case-study reference
code; it is a presentation filter and is not used as a parser rule.

## Verify reproducibility

    python tests/test_output_equivalence.py <acquisition.zip> [...]

Checks (1) the engine digest and (2) that the packaged CLI and the legacy
launcher produce byte-identical JSON for each acquisition.

For byte-stable full-report reproduction, pin the exact `ccl_chromium_reader`
commit and record the host Python version (see `release_manifest.json`);
some path- and environment-sensitive fields otherwise vary across hosts while
all `extraction_summary` and summary values remain identical.

## Redaction note

Reports may embed account identifiers recovered from list-cache keys.
Before publishing any output, redact account identifiers and host paths.
Do **not** commit real `.ldb`, `.log`, `.zip`, or output JSON/HTML to the
repository — they can contain prompts, emails, calendar entries, uploaded
file URLs, and account artifacts.

## Intended use and disclaimer

This is a digital-forensics research tool. It is intended for lawful
examination of data that you are authorized to access — your own evidence,
or evidence you are permitted to analyze under an appropriate legal or
investigative mandate. You are responsible for complying with all applicable
laws and for protecting any personal data recovered. The software is provided
"as is", without warranty, under the terms of the license below.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Citation

If you use this tool, please cite the accompanying paper (see
[CITATION.cff](CITATION.cff)).
