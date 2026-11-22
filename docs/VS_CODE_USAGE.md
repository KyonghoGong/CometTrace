# Run CometTrace in VS Code

Open the folder containing `pyproject.toml` and `src/`. Install Microsoft's
Python and Python Debugger extensions. Create the virtual environment and
install the pinned CCL reader and CometTrace using the [README](../README.md).
Run **Python: Select Interpreter** and choose that environment.

## Run and Debug

Press F5 and select a configuration to use the selected Python interpreter.
The same commands are available under **Tasks: Run Task**. Windows tasks run
the workspace's `.venv/Scripts/python.exe`. Create that environment under
the project root as described in the README. Test Explorer discovers the
tests from the project root.

To run from a Windows PowerShell terminal opened in that folder:

```powershell
New-Item -ItemType Directory -Force output | Out-Null
.\.venv\Scripts\python.exe -m comettrace --input "C:\path\to\acquisition.zip" --output output/case.json --html-output output/case.html
```

Use a different output basename for each acquisition or stage.

| Configuration | Input and output |
| --- | --- |
| Verify engine integrity | Prints the expected and actual SHA-256 of both installed engines. |
| Show CLI help | Lists arguments for the default v0.38 engine. |
| Run unit tests | Discovers the public tests in `tests/`. |
| Reconstruct an acquisition | Prompts for an acquisition ZIP or directory and an output basename. |
| Reconstruct one stage | Also prompts for the stage identifier. |
| Compare two snapshots | Prompts for separate before and after inputs. |
| Compare two stages | Prompts for one acquisition and the stage identifier for each side. |
| Run frozen v0.37 | Prompts for a legacy acquisition using the `keyval-store` database. |

The first three configurations run with an installed environment and no case
files. For a reconstruction or comparison, enter paths directly at the prompts,
including spaces without surrounding quotes. Output defaults to
`output/reconstruction.json` and `output/reconstruction.html` under the
workspace. Choose a different basename to retain results across runs.
Use a copied acquisition in a private directory. For supported inputs, stage
handling and reference filters, see [example commands](../examples/commands.md).

## Check an installation or acquisition

Run these commands in a terminal using the selected environment:

```sh
python -m pip check
python tools/verify_release.py
python -m comettrace --input "path/to/acquisition.zip" --list-stages
```

For reference-filtered runs, inspect `threads` and
`source.database_selection` in the JSON report. A filter can match zero
threads. Missing databases and missing requested stores produce CLI errors.
