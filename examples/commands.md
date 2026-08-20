# Example Commands

Replace all example paths and reference codes with values appropriate for your
own authorized acquisition.

The `--target-reference` value must match a reference code actually recovered
from the supplied acquisition. If it does not match, the filtered report may
be empty.

## Observed Comet profile layout

In the study environment, the Windows profile root followed this structure:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\<Profile>\
```

The relevant Perplexity IndexedDB LevelDB directory was located at:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\<Profile>\
IndexedDB\https_www.perplexity.ai_0.indexeddb.leveldb
```

`<Profile>` is a placeholder for the active Chromium-style profile directory,
such as:

```text
Default
Profile 1
Profile 10
```

For example, if the active profile is `Profile 10`, the profile root is:

```text
%LOCALAPPDATA%\Perplexity\Comet\User Data\Profile 10\
```

It is not:

```text
%LOCALAPPDATA%\Perplexity\Comet\Profile10\Default\
```

Analyze a forensic copy or acquisition ZIP rather than a live browser profile.

## Profile-wide inventory

```powershell
$acquisition = "C:\Path\To\Copied-Profile-or-Acquisition.zip"

comettrace `
  --input "$acquisition" `
  --output "output\inventory.json" `
  --html-output "output\inventory.html"
```

When `--target-reference` is omitted, CometTrace produces a profile-wide
inventory. Residual records from earlier tasks may therefore appear in the
report.

## Browser Control target filter

```powershell
$acquisition = "C:\Path\To\Acquisition.zip"
$referenceCode = "YOUR_REFERENCE_CODE"

comettrace `
  --input "$acquisition" `
  --target-reference "$referenceCode" `
  --output "output\browser_case.json" `
  --html-output "output\browser_case.html"
```

The target-reference option is a report filter. It does not change the
underlying LevelDB parsing rules.

## Computer mode target filter

```powershell
$acquisition = "C:\Path\To\Acquisition.zip"
$referenceCode = "YOUR_REFERENCE_CODE"

comettrace `
  --input "$acquisition" `
  --target-reference "$referenceCode" `
  --output "output\computer_case.json" `
  --html-output "output\computer_case.html"
```

Computer mode evidence may be distributed across multiple linked records
rather than contained in a single thread record.

## Deletion comparison

```powershell
$beforeAcquisition = "C:\Path\To\Before-Deletion.zip"
$afterAcquisition = "C:\Path\To\After-Deletion.zip"

comettrace `
  --before "$beforeAcquisition" `
  --after "$afterAcquisition" `
  --output "output\deletion_comparison.json" `
  --html-output "output\deletion_comparison.html"
```

Interpret the comparison using the acquisition sequence and external
ground-truth record. The presence or absence of a list entry alone should not
be treated as proof that all underlying thread content was deleted.

## Direct LevelDB folder with matching blob folder

```powershell
$leveldb = "C:\Path\To\https_www.perplexity.ai_0.indexeddb.leveldb"
$blob = "C:\Path\To\https_www.perplexity.ai_0.indexeddb.blob"

comettrace `
  --input "$leveldb" `
  --blob-input "$blob" `
  --output "output\reconstruction.json" `
  --html-output "output\reconstruction.html"
```

The blob folder is optional unless matching blob-backed values are available
and required for the examination.

## Single scenario acquisition ZIP

```powershell
$acquisition = "C:\Path\To\Scenario_IndexedDB.zip"

comettrace `
  --input "$acquisition" `
  --output "output\scenario.json" `
  --html-output "output\scenario.html"
```

## Legacy launcher

The backward-compatible launcher remains available:

```powershell
python comet_browser_reconstruct.py `
  --input "C:\Path\To\Acquisition.zip" `
  --output "output\legacy_reconstruction.json"
```

## Evidence-handling reminder

Do not commit real acquisitions or generated reports to the public repository.
Acquisitions and reports can contain prompts, email or calendar content,
account identifiers, uploaded-file URLs, screenshot references, and local host
paths.