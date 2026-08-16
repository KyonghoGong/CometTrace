# Example Commands

Replace the example paths with your own. The `--target-reference` values must
match the reference code actually embedded in the acquisition, or the filtered
report will be empty.

## Profile-wide inventory

```powershell
comettrace --input ".\dataset\Profile10\Default" --output output\inventory.json --html-output output\inventory.html
```

## Known Browser Control case

```powershell
comettrace --input ".\dataset\Profile10\Default" --target-reference "S08_Calendar_20260527" --output output\browser_calendar.json --html-output output\browser_calendar.html
```

## Known Computer mode case

```powershell
comettrace --input ".\dataset\Profile10\Default" --target-reference "Computer_Calendar_20260602" --output output\computer_calendar.json --html-output output\computer_calendar.html
```

## Deletion comparison

```powershell
comettrace --before ".\snapshots\before.zip" --after ".\snapshots\after.zip" --output output\delete_comparison.json --html-output output\delete_comparison.html
```

## Direct LevelDB folder with blob folder

```powershell
comettrace --input ".\dataset\https_www.perplexity.ai_0.indexeddb.leveldb" --blob-input ".\dataset\https_www.perplexity.ai_0.indexeddb.blob" --output output\reconstruction.json --html-output output\reconstruction.html
```

## Single scenario acquisition (as validated)

```powershell
comettrace --input "..\CometDataset\S04_IndexedDB.zip" --output output\S04.json --html-output output\S04.html
```