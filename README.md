# CometTrace

*Comet Browser/Computer Artifact Reconstructor*

Version 0.38.6 writes report schema 0.38. Select `--engine v037` to run
the frozen schema 0.37 engine. The default engine preserves verbatim answers in
completed nested workflows when the retained child result is unambiguous.
It uses the owning entry's backend UUID and narrows recipient and calendar
field attribution when a prompt or unlabelled form input is ambiguous.

For a staged comparison, supply
`--before-stage` and `--after-stage` alongside the two input paths.

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

* **Python 3.10-3.12.** Python 3.11 or 3.12 is recommended. The paper reports
  Python 3.12, but the exact patch version used for the original
  paper-associated outputs was not recorded in the available release
  materials.
* `ccl_chromium_reader` and `ccl_simplesnappy` (CCL Solutions Group Ltd.),
  installed from the source commits pinned in the installation scripts and
  recorded in `release_manifest.json`.
* `Brotli`, `zstandard`, and `zstd`, which are required by the LevelDB parsing
  dependencies.

The CCL commits pinned in this repository define the 2026-08-09 post-hoc
revalidation environment. They are not claimed to be the unrecorded commits
used to generate the original paper-associated outputs.

## Installation

From the repository root, create a Python 3.10–3.12 virtual environment.

In Windows PowerShell, choose one command to create the environment. With
the Python launcher and Python 3.12 installed, use:

```powershell
py -3.12 -m venv .venv
```

If your installation provides `python` directly, verify that it reports
Python 3.10, 3.11 or 3.12, then create the environment:

```powershell
python --version
python -m venv .venv
```

For either Windows setup, run these two commands:

```powershell
.\.venv\Scripts\python.exe scripts/install_ccl_dependencies.py
.\.venv\Scripts\python.exe -m pip install .
```

Linux or macOS:

```sh
python3.12 -m venv .venv
.venv/bin/python scripts/install_ccl_dependencies.py
.venv/bin/python -m pip install .
```

Use that environment's Python for the checks below. In VS Code, select the
environment with **Python: Select Interpreter** and open a new terminal.
See [VS Code usage](docs/VS_CODE_USAGE.md) for the F5 configurations and tasks.

```sh
python -m pip check
python -m comettrace.provenance
python tools/verify_release.py
python -m unittest discover -s tests -v
python -m comettrace --help
```

## Research coverage

The validated acquisitions cover Comet 148 and 156. See the [schema guide](docs/SUPPORTED_SCHEMAS.md)
for the stored modes, the [architecture guide](docs/ARCHITECTURE.md) for the reconstruction
pipeline and the [limitations](docs/LIMITATIONS.md) for attribution and concurrency boundaries.

## Windows terminal examples

From the project directory, create an output directory and supply your own acquisition paths.
Use different output basenames for each run you want to keep.

```powershell
New-Item -ItemType Directory -Force output | Out-Null
.\.venv\Scripts\python.exe -m comettrace --input "C:\Data\older.zip" --output output/older.json --html-output output/older.html
.\.venv\Scripts\python.exe -m comettrace --input "C:\Data\newer.zip" --output output/newer.json --html-output output/newer.html
```

JSON contains per-thread records and evidence locations. HTML displays the reconstruction
for review. Keep the acquisitions, report outputs and local account identifiers private.
The release manifest pins the entry-point digest and all schema 0.38 evidence modules.

For a single stage, the named JSON and optional HTML each contain that stage.
For an automatic multi-stage run, the named JSON maps each stage and source
directory to its files. The named HTML is an index linking to the stage HTML
files beside it. Stage filenames preserve Unicode and spaces and receive a
suffix when names collide. Supply distinct JSON and HTML paths. Reusing a
basename replaces that run's reports after all requested reports render.

## Installing from the cumulative patch

The ordinary clone of the release commit and the public source ZIP are ready
to install. To reconstruct the same source from the published baseline on
Windows, place the cumulative patch in the current directory and use a new
checkout as shown below. Each Git command must succeed before continuing.
The baseline predates the LF attributes. Applying to the index first lets
Git use the release attributes from the first write to disk.

```powershell
git clone --no-checkout https://github.com/KyonghoGong/CometTrace.git CometTrace-patched
cd CometTrace-patched
git read-tree 0cb17124a3e96f624896bcf33caed6f8afc68c89
git apply --cached --check --whitespace=error-all ../CometTrace-0cb17124-to-0.38.6.patch
git apply --cached --whitespace=error-all ../CometTrace-0cb17124-to-0.38.6.patch
git checkout-index --all
```

Run the installation and verification commands above in `CometTrace-patched`.

## Quick Start

Run CometTrace against a browser acquisition and generate JSON and HTML
reconstruction reports:

```bash
comettrace \
  --input "path/to/acquisition.zip" \
  --output reconstruction.json \
  --html-output reconstruction.html
```

The input is your own copied acquisition ZIP or directory. A reference filter
can legitimately return an empty `threads` list with exit code 0. Inspect
`source.database_selection` for the selected database, its
`perplexity_cache_record_count` in `available_databases`, and the selection
reason. A readable store with zero recognized keys limits what can be inferred.
An unavailable database or object store fails the command. For staged bundles,
inspect the individual stage reports.

Additional examples, including Browser Control, Computer mode, direct LevelDB
input, and deletion-related reconstruction, are available in
`examples/commands.md`.

## Interpreting schema 0.38

In schema 0.38, `classification.execution_mode` remains a compatibility category.
`observed_backend` contains the owning entry's retained mode/model fields and
source reference. `observed_tool_events` identifies recorded workflow tool steps.
An invocation alone does not establish a successful browser action. A browser
skill link in the query prefix is recorded separately. The tested ASI records
do not identify which UI control the user selected. Read `ui_selection.status`
before interpreting a backend category as a UI mode.

`--browser-only` retains the historical scope: it skips Computer/ASI groups
and list-only promotion, including ASI workflows that invoked browser tools.
Search records can remain. The CLI prints this limitation to stderr and includes
the omitted grouped browser-tool IDs in JSON diagnostics. Omit the option to
include those tasks. Use `--database` to inspect other available accounts.

Completed workflow answer payloads and completed answer markdown can supply a
full answer. An unsupported structure can yield an unavailable answer. Explicit
sidebar answer previews, including short strings, are marked as previews.
Reported filename extraction from answer prose targets PDF. Explicit labels
or matching quotes delimit names containing spaces. Extension case and Unicode
are preserved. Browser History is a separate source. A completed download claim
and a corroborating History row are separate fields.

Stage JSON contains `diagnostics` and `source.history_read_status`.
Distinguish a missing History file, a read failure, a successfully read empty
table, an unrecognized store schema, and a reference filter with no matches.
Readable empty results retain exit 0; inaccessible stores and failed database
selection retain an error exit. These behaviors were tested on retained
acquisitions and synthetic schema controls. For concurrent analysis, run
separate CLI processes; the patch layer temporarily changes module globals.

## Engine freeze policy

The reconstruction engine
(`src/comettrace/legacy_engine.py`, report `schema_version` 0.37) is
**release-frozen** and is shipped byte-identical to the schema 0.37 engine
associated with the paper analysis.

Its SHA-256 digest is pinned in `release_manifest.json` and
`comettrace/__init__.py` and can be checked at any time:

```bash
python -m comettrace.provenance
```
