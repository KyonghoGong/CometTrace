# CometTrace

*Comet Browser/Computer Artifact Reconstructor*

CometTrace is an open-source forensic reconstruction tool that reconstructs
per-thread behavioral timelines from Perplexity Comet's IndexedDB LevelDB
artifacts and annotates the recovered evidence with persistence state.

The tool accompanies the paper "Reconstructing Agentic AI Behavior from
Browser Artifacts: A Forensic Case Study of Perplexity Comet" and implements
the paper's five-layer reconstruction model. Its reconstruction coverage is
evaluated in the paper against recorded ground truth.

This repository is a source-code release. It intentionally excludes raw
browser acquisitions and generated JSON/HTML reports because those files may
contain personal or case-sensitive evidence.

## Requirements

- **Python 3.10-3.12.** Python 3.11 or 3.12 is recommended. The paper reports
  Python 3.12, but the exact patch version used for the original
  paper-associated outputs was not recorded in the available release
  materials.
- `ccl_chromium_reader` and `ccl_simplesnappy` (CCL Solutions Group Ltd.),
  installed from the source commits pinned in the installation scripts and
  recorded in `release_manifest.json`.
- `Brotli`, `zstandard`, and `zstd`, which are required by the LevelDB parsing
  dependencies.

The CCL commits pinned in this repository define the 2026-08-09 post-hoc
revalidation environment. They are not claimed to be the unrecorded commits
used to generate the original paper-associated outputs.

## Engine freeze policy

The reconstruction engine
(`src/comettrace/legacy_engine.py`, report `schema_version` 0.37) is
**release-frozen** and is shipped byte-identical to the schema 0.37 engine
associated with the paper analysis.

Its SHA-256 digest is pinned in `release_manifest.json` and
`comettrace/__init__.py` and can be checked at any time:

```bash
python -m comettrace.provenance