# Reconstruction architecture

The console command and `python -m comettrace` enter through `cli.main`. The schema 0.38 coordinator in `engine_v038.py` resolves inputs, runs each selected stage and writes either a single report, a stage bundle or a before/after comparison. The compatibility module also exports the existing engine callables. The `--engine v037` route delegates to the frozen paper-associated module.

| Module under `v038/` | Responsibility |
|---|---|
| `stages.py` | Locate each LevelDB and sibling History, select the requested stage and retain stage-local file metadata. |
| `database.py` | Enumerate databases and stores, rank candidates by observed keys and validate explicit selections. |
| `records.py` | Identify key types, group task-owned records and scope shared sidebar entries to each entry's UUID. |
| `workflow.py` | Walk nested workflow items, recover the prompt and select an eligible verbatim final answer. |
| `modes.py` | Interpret the owning entry's backend fields, requested skill and observed tool events. |
| `payloads.py` | Attribute form and action fields, then partition confirmed and candidate payloads. |
| `downloads.py` | Interpret PDF mentions, attributed filenames and download state within the thread. |
| `history.py` | Read a stage's Chromium History and check filename, thread time window and download state. |
| `runtime.py` | Bind the handlers that frozen reconstruction functions look up in their own module. |

## Following a reported value

For a thread's prompt or answer, start at the evidence reference in JSON. Locate the stage, database and object store, then the source file and LevelDB Sequence. Inspect the key, backend UUID, context and JSON value path. Multiple turns can share a context and a physical record. Read the selected entry and the recorded scope before attributing the text to a particular turn. List-only previews can supply partial text. A completed workflow answer or eligible completed answer markdown can supply full text.

The coordinator uses explicit calls for database selection, stage discovery and History corroboration. During reconstruction, the frozen module calls other functions through its own globals. The 26 entries in `runtime._bindings()` identify these lookups, including one regular expression. They cover classification, prompts and answers, record grouping, action payloads, PDF handling, download outcomes and comparison identity. An exception or a nested synchronous call restores the bindings active at entry. Concurrent analyses require separate CLI processes.

Deletion interpretation, most enrichment layers and JSON/HTML rendering remain in `legacy_engine.py`. When changing a handler, test both its direct result and its behavior through the CLI. Verify the frozen file, coordinator and module hashes with `python -m comettrace.provenance` and `python tools/verify_release.py`.
