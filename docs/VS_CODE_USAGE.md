# VS Code Usage

## 1. Open the repository root

Open the CometTrace repository root in VS Code.

The selected folder should directly contain files and directories such as:

```text
pyproject.toml
README.md
release_manifest.json
src/
scripts/
tests/
```

Do not open only the `src` directory or an outer folder that does not contain
`pyproject.toml`.

If VS Code displays a **Restricted Mode** banner, review the repository and
select **Manage → Trust** before running the supplied configurations.

## 2. Select a supported Python interpreter

CometTrace supports Python 3.10 through 3.12. Python 3.11 or 3.12 is
recommended.

The pinned forensic parsing dependency does not currently import cleanly on
Python 3.13 or later.

Open the command palette:

```text
Ctrl+Shift+P
```

Select:

```text
Python: Select Interpreter
```

Then choose a supported interpreter.

A project-specific virtual environment is recommended. On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

After creating the virtual environment, select its Python interpreter in
VS Code.

## 3. Install the pinned forensic dependencies

From the repository root, run:

```powershell
.\scripts\install_ccl_dependencies.ps1
```

The script installs the CCL parser commits recorded in
`release_manifest.json`.

These commits define the documented post-hoc revalidation environment. They
are not claimed to be the unrecorded parser revisions used for the original
paper-associated outputs.

Install the CometTrace package:

```powershell
python -m pip install -e .
```

Confirm that the parser imports successfully:

```powershell
python -c "import ccl_chromium_reader; print('CCL import: OK')"
```

Confirm the installed CometTrace package version:

```powershell
python -c "import comettrace; print(comettrace.__version__)"
```

## 4. Verify engine integrity

Run:

```powershell
python -m comettrace.provenance
```

A valid result reports:

```text
status      : VERIFIED
```

This confirms that `src/comettrace/legacy_engine.py` matches the SHA-256 digest
pinned by the source release.

The check verifies engine-file integrity only. It does not independently
establish that a generated report is byte-identical to an earlier
paper-associated output.

## 5. Use the Run and Debug panel

Open the VS Code **Run and Debug** panel:

```text
Ctrl+Shift+D
```

Select one of the supplied configurations.

### 1. Verify engine integrity (SHA-256)

Checks whether the frozen reconstruction engine matches its pinned SHA-256
digest.

This configuration does not require a forensic acquisition or the CCL parser.

### 2. Print package metadata

Prints:

- CometTrace package version;
- report schema version;
- pinned engine SHA-256 prefix; and
- console-entry-point registration status.

This configuration does not require a forensic acquisition or the CCL parser.

### 3. Show CometTrace CLI help (requires CCL)

Imports the reconstruction engine and displays the available command-line
arguments.

This configuration requires the pinned CCL dependencies.

### 4. Run CometTrace (requires CCL and acquisition)

Runs an actual reconstruction.

Before running it, open:

```text
.vscode/launch.json
```

Replace:

```text
C:/Path/To/Copied-Profile-or-Acquisition.zip
```

with the path to an authorized acquisition, copied profile, or acquisition
ZIP.

Do not commit the resulting local path to the public repository.

## 6. Observed Windows profile layout

In the study environment, the Comet profile root followed this structure:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\<Profile>\
```

The target Perplexity IndexedDB LevelDB directory was located at:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\<Profile>\
IndexedDB\https_www.perplexity.ai_0.indexeddb.leveldb
```

`<Profile>` represents the active Chromium-style profile directory, such as:

```text
Default
Profile 1
Profile 10
```

For an active profile named `Profile 10`, the correct profile root is:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\Profile 10\
```

The following older example is not the observed study path:

```text
%LOCALAPPDATA%\Perplexity\Comet\Profile10\Default\
```

Analyze a forensic copy or acquisition ZIP rather than a live browser profile.

## 7. Run from the terminal

A basic reconstruction can be run with:

```powershell
python -m comettrace `
  --input "C:\Path\To\Acquisition.zip" `
  --output "output\reconstruction.json" `
  --html-output "output\reconstruction.html"
```

The installed console command can also be used:

```powershell
comettrace `
  --input "C:\Path\To\Acquisition.zip" `
  --output "output\reconstruction.json" `
  --html-output "output\reconstruction.html"
```

On Windows with multiple installed Python versions, select a supported version
explicitly when needed:

```powershell
py -3.12 -m comettrace `
  --input "C:\Path\To\Acquisition.zip" `
  --output "output\reconstruction.json"
```

## 8. Verify entry-point equivalence

If an authorized acquisition is available, run:

```powershell
python tests\test_output_equivalence.py `
  "C:\Path\To\Acquisition.zip"
```

This test verifies:

1. the frozen engine SHA-256; and
2. same-environment output equivalence between the legacy launcher and the
   packaged entry point.

It does not reproduce the paper's numerical results or compare the generated
report against an archived paper-associated output.

## 9. Troubleshooting

### `ModuleNotFoundError: No module named 'zstd'`

Run the pinned dependency installation script again:

```powershell
.\scripts\install_ccl_dependencies.ps1
```

Then confirm that VS Code is using the same Python interpreter in which the
dependencies were installed.

### `ccl_chromium_reader is not installed`

Check the selected interpreter:

```powershell
python --version
python -c "import sys; print(sys.executable)"
```

Reinstall the pinned dependencies in that interpreter:

```powershell
.\scripts\install_ccl_dependencies.ps1
```

### Input path not found

Confirm that the path points to one of the following:

- the target IndexedDB LevelDB folder;
- a copied profile or scenario folder containing that directory; or
- an acquisition ZIP containing that directory.

Wrap Windows paths containing spaces in quotation marks.

## 10. Engine modification warning

`src/comettrace/legacy_engine.py` is release-frozen.

Do not modify it in place. Any byte-level change:

- breaks the pinned SHA-256 integrity check;
- creates a different analytical engine version; and
- requires separate validation and versioning.

If the file is temporarily modified for debugging, restore the released
version and run:

```powershell
python -m comettrace.provenance
```

before producing any citable output.

## 11. Evidence-handling warning

Do not commit:

- raw `.ldb` or `.log` files;
- acquisition ZIP files;
- SQLite databases;
- generated JSON reports;
- generated HTML reports; or
- recovered screenshots and uploaded-file content.

These files may contain prompts, email or calendar content, account
identifiers, local paths, uploaded-file URLs, and other sensitive evidence.