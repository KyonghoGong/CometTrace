# Supported acquisition schemas

Verification covers retained Windows Comet 148 and 156 acquisitions. The CLI inspects stored structures to choose its readers. Browser version alone cannot identify the database, backend, UI setting or available fields.

| Observed evidence | Interpretation |
|---|---|
| Legacy `keyval-store` | The object store holds Perplexity cache entries. The frozen engine remains available through `--engine v037`. |
| Account-scoped `pplx-query-cache-*` | The 0.38 engine ranks readable stores using retained keys. Inspect `source.database_selection` and select `--database` explicitly when necessary. |
| `COPILOT/SEARCH`, `COPILOT/STUDY` | The retained entry supports the search or study category, including entries with provider-labelled thought text. |
| `COPILOT/BROWSER_AGENT` | Legacy Browser Agent backend. The record may include browser tool input/output blocks. |
| `ASI/ASI`, `browser_*` tool steps | Backend and tool events are separate observations. Search+BC, Computer and Computer+BC may share backend fields. The UI selection can remain `not_established`. |
| Completed workflow answer, completed answer markdown | Eligible for full-answer recovery. Ownership, completion, nesting and ambiguity checks apply. |
| Shared sidebar or recent-task list | Per-entry UUID/context scoping. An explicit answer preview carries `list_preview_only`. |

Read JSON diagnostics and stderr. An unsupported answer variant can yield an unavailable answer. A readable empty store, unfamiliar key layout or unmatched reference can return exit 0 with an empty thread list. An unavailable database or store fails the command. Multi-stage comparisons require a unique selection on each side. The historical `--browser-only` filter can exclude ASI browser actions and list-only entries. Omit it for the full supported scope.
