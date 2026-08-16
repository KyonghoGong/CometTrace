# VS Code Usage

## 1. Open the folder

Open the `comettrace_release` folder in VS Code. If a "Restricted Mode"
banner appears, choose **Manage → Trust** to enable running code.

## 2. Select a Python interpreter

Use Python 3.10-3.12 (3.11 or 3.12 recommended; the parsing dependency
`ccl_chromium_reader` does not import on 3.13+). Press `Ctrl+Shift+P` ->
**Python: Select Interpreter** and choose a supported version.

## 3. Install dependencies

```powershell
.\scripts\install_ccl_dependencies.ps1
python -m pip install -e .
```

After installation, confirm the parsing dependency imports:

```powershell
python -c "import ccl_chromium_reader; print('ok')"
```

If this prints `ModuleNotFoundError: No module named 'zstd'`, the codec is
missing; run `python -m pip install zstandard zstd` and try again.

## 4. Run from the Run and Debug panel

Open **Run and Debug** (`Ctrl+Shift+D`) and pick a configuration from the
dropdown, then press the green play button:

- **1. Verify engine freeze (SHA-256)** - checks the engine digest.
- **2. Print package metadata** - prints version and report schema.
- **3. Show CLI help** - shows the CLI options (imports the engine, so it
  requires the parsing dependency).
- **Run CometTrace (needs ccl + real --input)** - runs a real analysis.

Configurations 1 and 2 require no forensic dependency (ccl) and no virtual
environment; they use the interpreter selected in step 2. To run a real
analysis, edit the `--input` argument in `.vscode/launch.json` to point at
your acquisition (a scenario `.zip` or a profile folder), then run
**Run CometTrace (needs ccl + real --input)**.

## 5. Run from the terminal instead

```powershell
python -m comettrace --input "<acquisition.zip>" --output output\reconstruction.json --html-output output\reconstruction.html
```

On Windows with multiple Python versions, select a supported one explicitly
with the launcher, e.g. `py -3.12 -m comettrace ...`.

## Note

`legacy_engine.py` is release-frozen. Do not edit it: any change breaks the
pinned SHA-256 digest and the paper's reproducibility claim. If you modify it
for debugging, restore it and re-run `python -m comettrace.provenance` to
confirm the digest matches before producing citable output.
