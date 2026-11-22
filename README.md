# CometTrace

**Browser artifact reconstruction for Perplexity Comet**  
**Release:** v0.38.6 · **Python:** 3.10–3.12 · **Outputs:** JSON + HTML

CometTrace reconstructs task-level evidence from **forensic copies** of Comet's
IndexedDB/LevelDB data: prompts, workflow steps, recorded tool events, final
answers, and persistence/deletion indicators where the evidence supports them.
It accompanies *Reconstructing Agentic AI Behavior from Browser Artifacts:
A Forensic Case Study of Perplexity Comet*.

**This repository contains source code, not browser acquisitions or personal data.**
You must supply your own authorized, copied acquisition.

## 1. Install on Windows (PowerShell)

Open PowerShell or the VS Code terminal:

```powershell
git clone https://github.com/KyonghoGong/CometTrace.git
cd CometTrace
py -3.12 -m venv .venv
```

If `py` is unavailable, use `python --version` to confirm Python 3.10,
3.11, or 3.12, then run `python -m venv .venv` instead.

Complete the installation using the new environment:

```powershell
.\.venv\Scripts\python.exe scripts/install_ccl_dependencies.py
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m pip check
```

The dependency installer downloads the pinned CCL forensic parsing libraries.

## 2. Analyze an acquisition

Replace `C:\Data\acquisition.zip` with the path to **your own** copied
acquisition. Run from the CometTrace project folder:

```powershell
New-Item -ItemType Directory -Force output | Out-Null
.\.venv\Scripts\python.exe -m comettrace --input "C:\Data\acquisition.zip" --output "output\case.json" --html-output "output\case.html"
```

Open `output\case.html` to review the reconstruction; `output\case.json`
contains the structured results and evidence references.

- **Separate acquisitions:** use different output names (for example,
  `case1.json` and `case2.json`) to keep both results.
- **Multi-stage ZIP:** the named JSON describes the stages; the named HTML
  becomes an index linking to each stage's JSON/HTML reports. Stage names
  are kept distinct so reports do not silently overwrite each other.
- **Specific stage or thread:** use `--stage` or `--target-reference` when
  applicable. See [command examples](examples/commands.md).
- **Default:** omit `--browser-only` to include Computer/ASI workflows that
  use browser tools. That flag keeps the **legacy Browser Agent-only scope**.

The original command form is unchanged: `--input`, `--output`, and
`--html-output`.

## 3. Install on Ubuntu / Linux

```bash
git clone https://github.com/KyonghoGong/CometTrace.git
cd CometTrace
python3 -m venv .venv
.venv/bin/python scripts/install_ccl_dependencies.py
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
mkdir -p output
.venv/bin/python -m comettrace --input "/path/to/acquisition.zip" --output "output/case.json" --html-output "output/case.html"
```

Use Python 3.10–3.12. The GitHub CI matrix covers **Windows and Ubuntu**
with Python 3.10, 3.11, and 3.12. macOS is not part of that tested matrix.

## 4. What the results do—and do not—mean

- **Validated acquisitions:** Comet 148-series and Comet 156.0.8078.48 /
  Perplexity Build 132. Other versions and unknown schemas are not guaranteed.
- **Observed records vs. interpretation:** a recorded tool invocation or
  reported success does not independently prove the external action occurred.
- **UI selection:** some Search+BC / Computer / Computer+BC records share
  `ASI/ASI` backend values; the exact UI choice may be **not established**.
- **Deletion:** a UI-deleted thread can leave forensic residues; individual
  deleted keys and retained content must be assessed separately.
- **Empty results:** `--target-reference` can legitimately return no matching
  threads (exit 0). Check the JSON diagnostics and database-selection details.
- **Execution model:** independent CLI processes are supported; concurrent
  analyses inside one Python process are not validated.

For the frozen historical engine, add `--engine v037`. The default v0.38
engine reports schema 0.38. The original v0.37 engine is preserved byte-for-byte;
see [release provenance](release_manifest.json).

## 5. Documentation and checks

- [More CLI examples](examples/commands.md) — filters, stage selection,
  deletion comparisons, and direct LevelDB input
- [Supported schemas](docs/SUPPORTED_SCHEMAS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Known limitations](docs/LIMITATIONS.md)
- [VS Code guide](docs/VS_CODE_USAGE.md)
- [Contributing](CONTRIBUTING.md) · [Security reports](SECURITY.md)
- [Citation information](CITATION.cff)

To verify an installed copy, run these commands using its Python interpreter:

```powershell
.\.venv\Scripts\python.exe -m comettrace.provenance
.\.venv\Scripts\python.exe tools/verify_release.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**Privacy:** work from authorized copies, not an active browser profile.
Acquisitions and generated reports can contain prompts, email contents,
account identifiers, local paths, and browsing history. **Do not commit
them to a public repository.**
