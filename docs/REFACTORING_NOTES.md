# Refactoring Notes

## Purpose

CometTrace is the open-source forensic reconstruction tool accompanying the
paper "Reconstructing Agentic AI Behavior from Browser Artifacts: A Forensic
Case Study of Perplexity Comet".

The current package structure was created to:

- provide an installable Python package;
- expose a stable command-line interface;
- retain a backward-compatible launcher;
- document the frozen reconstruction engine;
- support provenance and integrity verification; and
- prepare for future modular development.

The packaging work does not modify the frozen schema 0.37 reconstruction
engine.

## Current package structure

The analytical engine is stored in:

```text
src/comettrace/legacy_engine.py
```

The backward-compatible launcher is:

```text
comet_browser_reconstruct.py
```

The packaged command-line entry point is:

```text
comettrace
```

The package entry point is defined as:

```text
comettrace = comettrace.cli:main
```

The surrounding package provides:

- Python package metadata;
- a packaged CLI;
- the backward-compatible launcher;
- dependency-installation scripts;
- engine-integrity verification;
- entry-point equivalence testing;
- VS Code launch configurations;
- CI checks; and
- public documentation.

## Frozen engine policy

The schema 0.37 reconstruction engine is release-frozen.

Its SHA-256 digest is:

```text
f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80
```

The digest is recorded in:

```text
release_manifest.json
src/comettrace/__init__.py
```

It can be verified with:

```bash
python -m comettrace.provenance
```

A successful check confirms that the engine file is byte-identical to the
engine pinned by this source release.

The check does not independently establish:

- byte-identical reproduction of archived paper-associated reports;
- output equivalence across different operating systems;
- output equivalence across different Python versions;
- output equivalence across different parser dependency revisions; or
- independent reproduction of the paper's numerical results.

Complete report generation depends on both the frozen engine and its execution
environment.

## Historical manifest

The historical file:

```text
docs/refactor_manifest.historical.json
```

records an earlier engine revision identified as schema 0.27.

That historical manifest contains the earlier digest prefix:

```text
6d9c702d...
```

The historical digest must not be used to verify the current schema 0.37
engine.

The current schema 0.37 digest is:

```text
f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80
```

The historical file is retained only to document the development history and
the distinction between the earlier and current engine revisions.

## Dependency environment

The reconstruction engine relies on external LevelDB parsing dependencies,
including:

```text
ccl_chromium_reader
ccl_simplesnappy
```

The dependency commits used for the 2026-08-09 post-hoc revalidation are
recorded in:

```text
release_manifest.json
scripts/install_ccl_dependencies.ps1
scripts/install_ccl_dependencies.sh
```

These commits define the documented post-hoc revalidation environment.

They are not claimed to be the unrecorded dependency revisions used to
generate the original paper-associated outputs.

A later parser revision may decode records that an earlier revision classified
as bad or undecoded. Consequently, parsed-record counts, live-record counts,
relevant-record counts, and summary values can differ even when the frozen
CometTrace engine is unchanged.

## Why the engine remains a single frozen module

The current engine contains accumulated analytical layers developed through
schema versions preceding 0.37.

Some later definitions intentionally override or extend earlier definitions.
Physically separating these components without comprehensive fixtures could
change:

- import order;
- override order;
- shared state;
- record normalization;
- thread grouping;
- classification behavior;
- deletion and private-mode interpretation; and
- JSON or HTML report generation.

For that reason, the current source release follows these rules:

1. preserve `legacy_engine.py` without analytical modification;
2. provide packaging and verification layers around the frozen engine;
3. document its exact SHA-256 digest;
4. separate engine integrity from complete report reproducibility; and
5. defer modular analytical changes until sufficient validation fixtures and
   comparison procedures exist.

## Current wrapper boundary

The following files may provide packaging or invocation behavior but must not
silently alter the frozen engine's analytical logic:

```text
src/comettrace/cli.py
src/comettrace/__main__.py
comet_browser_reconstruct.py
src/comettrace/provenance.py
```

Changes to these files should be checked to confirm that:

- the same engine remains selected;
- command-line arguments are passed through correctly;
- report output is not silently transformed; and
- legacy and packaged entry points remain equivalent within the same
  environment.

The supplied comparison command is:

```bash
python tests/test_output_equivalence.py <acquisition>
```

This test compares the two entry points within one current environment. It is
not a comparison against archived paper-associated reports.

## Proposed second-stage modular structure

A future analytical version may separate the engine into modules such as:

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
    typed_payloads.py
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

This structure is a future design proposal. It is not the structure of the
current frozen analytical engine.

## Validation requirements for a future refactor

Before replacing or modularizing the frozen engine, a new analytical version
should be validated using:

- synthetic public fixtures;
- independently authorized Comet acquisitions;
- fixed Python and dependency versions;
- documented input hashes;
- documented expected-output hashes; and
- explicit comparison rules.

At minimum, the comparison should examine:

1. engine and package version;
2. parsed-record counts;
3. Live and Deleted record-state counts;
4. bad or undecoded record counts;
5. relevant-record filtering;
6. thread grouping;
7. Browser Control and Computer mode classification;
8. L1 thread and prompt reconstruction;
9. L2 plan and workflow reconstruction;
10. L3 action and typed-input reconstruction;
11. L4 provider-labeled thought recovery where present;
12. L5 final-answer recovery;
13. deletion and private-mode interpretation;
14. residual-record handling;
15. JSON semantic differences; and
16. HTML report rendering.

A future analytical engine must receive:

- a new package version;
- a new engine schema version where applicable;
- a new engine SHA-256 digest;
- a new release manifest;
- new validation results; and
- documentation of differences from schema 0.37.

## Meaning of entry-point equivalence

Entry-point equivalence means that the following commands invoke the same
frozen engine within the same current environment:

```bash
python comet_browser_reconstruct.py \
  --input <acquisition> \
  --output legacy.json
```

```bash
python -m comettrace \
  --input <acquisition> \
  --output packaged.json
```

Randomized temporary extraction-directory paths may be canonicalized before
comparison.

Entry-point equivalence does not mean that:

- different CCL commits produce identical decoded records;
- different operating systems produce byte-identical path fields;
- different Python versions produce byte-identical reports; or
- the public source repository independently reproduces all paper tables
  without the corresponding research acquisitions and ground truth.

## Public release scope

The public repository releases CometTrace source code.

It does not include:

- raw Comet browser profiles;
- raw LevelDB acquisitions;
- the private experimental dataset;
- generated JSON reports;
- generated HTML reports;
- personal email or calendar data;
- account identifiers; or
- private screenshot and uploaded-file content.

This separation protects sensitive forensic evidence while allowing
researchers to apply the tool to independently acquired artifacts that they
are authorized to examine.

## Evidence-handling requirements

Do not commit real:

```text
*.ldb
*.log
*.zip
*.sqlite
*.sqlite3
output/
outputs/
reconstruction.json
reconstruction.html
raw_records/
```

Generated reports and raw acquisitions may contain:

- prompts;
- email and calendar content;
- account identifiers;
- local usernames and host paths;
- uploaded-file URLs;
- screenshot references;
- downloaded-document information; and
- residual records from earlier tasks.

Use synthetic fixtures for public regression tests whenever possible.

## Summary

The current CometTrace release preserves the schema 0.37 analytical engine and
provides packaging, provenance, documentation, and verification layers around
it.

The engine SHA-256 establishes source-file integrity.

Parser dependency revisions and host-environment details remain necessary for
complete report reproducibility.

Future analytical refactoring must be separately versioned and validated
rather than implemented as an undocumented modification to the frozen engine.