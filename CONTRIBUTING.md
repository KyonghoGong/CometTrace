# Contributing to CometTrace

Thank you for your interest in contributing to CometTrace.

CometTrace is an open-source forensic reconstruction tool accompanying the
paper "Reconstructing Agentic AI Behavior from Browser Artifacts: A Forensic
Case Study of Perplexity Comet".

Contributions should preserve the distinction between:

- the frozen reconstruction engine associated with the paper analysis;
- packaging, documentation, and verification improvements; and
- new analytical behavior requiring separate validation and versioning.

## Frozen reconstruction engine

The reconstruction engine is:

```text
src/comettrace/legacy_engine.py
```

Its report schema version is:

```text
0.37
```

Its SHA-256 digest is pinned in:

```text
release_manifest.json
src/comettrace/__init__.py
```

The digest can be verified with:

```bash
python -m comettrace.provenance
```

The SHA-256 check verifies that the engine file is byte-identical to the engine
pinned by this source release.

It does not independently establish:

- byte-identical reproduction of archived paper-associated outputs;
- equivalence across different parser dependency revisions;
- cross-platform report equivalence; or
- independent reproduction of the paper's numerical results.

## Do not modify the frozen engine in place

Do not directly modify:

```text
src/comettrace/legacy_engine.py
```

A byte-level change to this file:

- breaks the pinned SHA-256 integrity check;
- creates a different analytical engine version;
- requires new validation evidence; and
- requires separate versioning and documentation.

New extraction logic, classification rules, reconstruction layers, or
interpretation behavior should be implemented in separately versioned modules.

See:

```text
docs/REFACTORING_NOTES.md
```

for the planned second-stage structure.

## Contributions that do not modify the engine

The following areas can be improved without changing the frozen reconstruction
logic:

- package metadata;
- CLI wrappers;
- documentation;
- installation scripts;
- dependency pinning;
- provenance checks;
- entry-point equivalence tests;
- CI configuration;
- user-interface and report-presentation layers; and
- security and evidence-handling guidance.

Changes in these areas must not silently change the behavior of the frozen
engine.

## Dependency-pin changes

The CCL parser dependencies are recorded in:

```text
release_manifest.json
scripts/install_ccl_dependencies.ps1
scripts/install_ccl_dependencies.sh
```

If a dependency commit is changed, update all three files together.

Do not describe a newly selected dependency commit as the original
paper-output environment unless that claim is supported by contemporaneous
environment records.

If a new commit is selected for post-hoc revalidation:

1. identify the commit explicitly;
2. record the validation date and purpose;
3. rerun the available acquisitions;
4. document any decoded-record or summary differences; and
5. update affected paper or release values before claiming reproducibility.

## Version consistency

When changing the package version, update all applicable version records
together:

```text
pyproject.toml
src/comettrace/__init__.py
release_manifest.json
CITATION.cff
.github/workflows/ci.yml
```

A packaging- or documentation-only change must not modify the frozen engine
digest.

An analytical engine change requires a new engine version, new SHA-256 digest,
and new validation evidence.

## Local development setup

Use Python 3.10 through 3.12. Python 3.11 or 3.12 is recommended.

Install the pinned forensic dependencies:

```bash
bash scripts/install_ccl_dependencies.sh
```

On Windows PowerShell:

```powershell
.\scripts\install_ccl_dependencies.ps1
```

Install CometTrace in editable mode:

```bash
python -m pip install -e .
```

Verify the parser import:

```bash
python -c "import ccl_chromium_reader; print('CCL import: OK')"
```

## Required checks before a pull request

### 1. Compile the Python source

```bash
python -m compileall -q \
  src \
  tests \
  tools \
  comet_browser_reconstruct.py
```

### 2. Verify engine integrity

```bash
python -m comettrace.provenance
```

The result must report:

```text
status      : VERIFIED
```

### 3. Verify the CLI

```bash
comettrace --help
```

### 4. Verify entry-point equivalence when authorized evidence is available

```bash
python tests/test_output_equivalence.py <acquisition>
```

This test compares the legacy launcher and packaged entry point within the
same current environment.

It does not compare generated reports against archived paper-associated
outputs and must not be described as independent reproduction of the paper's
numerical results.

## Documentation requirements

Documentation changes should clearly distinguish:

- recovered artifacts from derived interpretation;
- profile-wide residual records from target-associated records;
- Browser Control evidence from Computer mode evidence;
- engine integrity from report reproducibility;
- original experiment records from post-hoc revalidation; and
- source-code availability from dataset availability.

Scenario-specific statements should identify their validation scope and should
not imply that unvalidated acquisitions were independently re-examined.

## Never commit forensic evidence

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

Forensic acquisitions and generated reports may contain:

- user prompts;
- email and calendar content;
- account identifiers;
- local host paths;
- uploaded-file URLs;
- screenshot references;
- downloaded document information; and
- residual records from earlier tasks.

Use synthetic or independently authorized test fixtures for public tests.

## Personal and case-sensitive information

Before committing documentation, examples, fixtures, or screenshots:

1. remove personal email addresses;
2. remove account identifiers;
3. replace local usernames and host paths with placeholders;
4. remove signed or private URLs;
5. remove real prompt and calendar content unless explicitly authorized; and
6. confirm that no raw acquisition data is included.

## Security reports

Potential vulnerabilities or data-handling problems should be reported using
the process described in:

```text
SECURITY.md
```

Do not disclose sensitive forensic content in a public issue.

## Pull-request description

A pull request should state:

- the files changed;
- whether the frozen engine was modified;
- whether the engine SHA-256 still matches;
- which dependency environment was used;
- which checks were run;
- whether an acquisition was used for testing; and
- whether any generated output was reviewed for sensitive information.

## License

By contributing to CometTrace, you agree that your contribution will be
licensed under the Apache License, Version 2.0, as described in:

```text
LICENSE
```