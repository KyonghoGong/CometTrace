"""Coordinate schema 0.38 reconstruction and expose the historical engine API."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import redirect_stdout
from io import StringIO
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import legacy_engine as leg
from .provenance import module_file_hashes, modules_digest
from .v038.database import (
    discover_query_databases,
    select_database,
)
from .v038.downloads import (
    _DOWNLOAD_COMPLETION_PATTERNS_V38,
    _DOWNLOAD_FAILURE_PATTERNS_V38,
    _DOWNLOAD_INTERRUPTED_PATTERNS_V38,
    _DOWNLOAD_NEGATION_OR_FAILURE_PATTERNS,
    _PDF_END,
    _PDF_LABEL,
    _PDF_LABELED,
    _PDF_QUOTED,
    PDF_FILENAME_RE_V38,
    _download_negative_status,
    _has_any_regex,
    _has_positive_download_completion_v038,
    _join_wrapped_pdf_extension,
    _named_pdfs,
    _v21_current_thread_signals_v038,
    _v23_reported_downloaded_filename_v038,
    _v23_update_download_outcome_v038,
    _v38_cleanup_pdf_filename,
    classify_task_outcome_v038,
    extract_pdf_filenames_v038,
)
from .v038.history import (
    _chrome_time_to_utc_iso,
    corroborate_downloads,
    read_chromium_history_downloads,
)
from .v038.modes import classify_group_v038
from .v038.payloads import (
    _build_structured_actions,
    _make_payload,
    _partition,
    _promote_input,
    _requested_recipient_only,
)
from .v038.records import (
    MULTI_THREAD_LIST_KINDS,
    SINGLE_THREAD_KEY_MARKERS,
    _iter_sidebar_or_recent_task_entries,
    _make_scoped_entry_record,
    _v07_records_for_thread_v038,
    build_promoted_computer_thread_v038,
    collect_asi_thread_list_candidates_v038,
    extract_unique_computer_task_ids,
    group_records_v038,
    infer_record_kind_v038,
    primary_thread_id_from_key_v038,
)
from .v038.runtime import _v038_runtime_patches
from .v038.outputs import check_destinations, save_reports, staged_files
from .v038.stages import (
    DiscoveredStage,
    _find_sibling_history,
    _infer_stage_id_and_role,
    _max_ldb_log_number,
    build_scoped_source_file_temporal_context,
    discover_leveldb_stages,
    select_stage,
)
from .v038.workflow import (
    _clean_thread,
    _core_entries,
    _dict_items,
    _exact_final,
    _extract_metadata,
    _extract_prompt,
    _workflow_steps,
    _workflow_text_items,
)

SCHEMA_VERSION_V038 = "0.38"
ENGINE_REVISION_V038 = "v0.38.6"


def resolve_database_selection(
    leveldb_path: Path,
    blob_path: Path | None,
    requested_database: str | None,
    object_store_name: str = leg.DEFAULT_STORE,
) -> tuple[str, dict[str, Any]]:
    """Delegate to the stage-specific evidence handler using the configured reader."""
    return select_database(
        leveldb_path,
        blob_path,
        requested_database,
        object_store_name,
        discover_query_databases=discover_query_databases,
    )


def attach_history_download_corroboration(report: dict[str, Any], history_path: Path | None) -> None:
    """Delegate to the stage-specific evidence handler using the configured reader."""
    return corroborate_downloads(
        report, history_path, read_chromium_history_downloads=read_chromium_history_downloads
    )


def compute_v038_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _engine_provenance() -> dict[str, str]:
    """Identify the installed bytes that produced this stage or bundle."""
    return {
        "engine_revision": ENGINE_REVISION_V038,
        "schema_version": SCHEMA_VERSION_V038,
        "base_legacy_engine_schema": "0.37",
        "base_legacy_engine_sha256": hashlib.sha256(Path(leg.__file__).read_bytes()).hexdigest(),
        "patched_engine_file": "src/comettrace/engine_v038.py",
        "patched_engine_sha256": compute_v038_sha256(),
        "modules_sha256": modules_digest(module_file_hashes(Path(__file__).parent)),
    }


def run_stage_v038(
    original_input: Path,
    root_path: Path,
    stage: DiscoveredStage,
    all_stages: list[DiscoveredStage],
    explicit_blob_input: Path | None = None,
    database: str = "auto",
    store: str = leg.DEFAULT_STORE,
    include_computer: bool = True,
    target_reference: str | None = None,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:
    blob_path = explicit_blob_input.expanduser().resolve() if explicit_blob_input else stage.blob_path
    if blob_path is not None and not blob_path.exists():
        raise FileNotFoundError(f"Blob input path does not exist: {blob_path}")

    selected_db, db_selection = resolve_database_selection(
        leveldb_path=stage.leveldb_path,
        blob_path=blob_path,
        requested_database=database,
        object_store_name=store,
    )

    with _v038_runtime_patches():
        source_file_temporal_context = build_scoped_source_file_temporal_context(
            original_input=original_input,
            root_path=root_path,
            leveldb_path=stage.leveldb_path,
            single_stage_archive=(len(all_stages) == 1),
        )
        extracted = leg.extract_records_from_leveldb(
            leveldb_path=stage.leveldb_path,
            blob_path=blob_path,
            database_name=selected_db,
            object_store_name=store,
        )
        extracted["source_file_temporal_context"] = source_file_temporal_context

        if dump_records_dir is not None:
            dump_records_dir.mkdir(parents=True, exist_ok=True)
            leg.write_json(
                dump_records_dir / "perplexity_live_records.json", {"records": extracted["live_records"]}
            )
            leg.write_json(
                dump_records_dir / "perplexity_dead_records.json", {"records": extracted["dead_records"]}
            )
            leg.write_json(
                dump_records_dir / "perplexity_ldb_records.json", {"records": extracted["ldb_records"]}
            )
            leg.write_json(
                dump_records_dir / "perplexity_log_records.json", {"records": extracted["log_records"]}
            )
            leg.write_json(
                dump_records_dir / "perplexity_unmatched_records.json",
                {"records": extracted["unmatched_records"]},
            )
            leg.write_json(
                dump_records_dir / "perplexity_source_file_temporal_context_v38.json",
                source_file_temporal_context,
            )

        report = leg.reconstruct_browser_threads(
            extracted=extracted,
            input_label=str(original_input),
            browser_only=not include_computer,
        )
        before_reference = len(report.get("threads", []))
        if target_reference:
            report = leg.filter_report_by_target_reference(report, target_reference)

        summary = report.get("payload_attribution_summary_v24") or {}
        for field in ("confirmed_payload", "candidate_payload"):
            summary[field + "_count"] = sum(len(t.get(field + "s") or []) for t in report.get("threads", []))

        # Attach optional same-stage Chromium History downloads corroboration
        attach_history_download_corroboration(report, stage.history_path)

    # Stamp explicit v0.38 schema & provenance distinct from frozen v0.37
    report["schema_version"] = SCHEMA_VERSION_V038
    report["engine_provenance"] = _engine_provenance()
    src = report.setdefault("source", {})
    src["database_selection"] = db_selection
    diagnostics = report.setdefault("diagnostics", [])

    def note(code, message):
        diagnostics.append({"code": code, "message": message})

    stores = [d for d in db_selection["available_databases"] if d["has_target_store"]]
    selected = next(d for d in stores if d["name"] == selected_db)
    if db_selection["selection_mode"] == "auto" and len(stores) > 1:
        note(
            "multiple_databases",
            f"Selected {selected_db!r}. Other databases with store {store!r}: "
            + ", ".join(d["name"] for d in stores if d["name"] != selected_db)
            + ". Use --database to inspect each.",
        )
    if selected.get("unreadable_records"):
        note("records_unreadable", f"{selected['unreadable_records']} record value(s) could not be decoded.")
    if not selected["record_count"] and not selected.get("unreadable_records"):
        note(
            "empty_store",
            f"Object store {store!r} contains no readable records and reported no decoding failures.",
        )
    if selected["record_count"] and selected.get("recognized_thread_keys") == 0:
        note(
            "unrecognized_thread_keys",
            "Records exist but none has a recognized thread or sidebar key. Coverage is uncertain.",
        )
    if not include_computer:
        omitted_browser = [
            g["group_id"]
            for g in report.get("skipped", [])
            if any(
                label.startswith("tool:browser_") or label == "legacy_browser_tool_event"
                for label in g.get("markers", [])
            )
        ]
        note(
            "browser_only_scope",
            f"Legacy scope omits Computer/ASI expansion, including ASI browser activity. "
            f"{len(report.get('skipped', []))} grouped task(s) skipped, {len(omitted_browser)} with observed browser tools; "
            "list-only promotion disabled. Omit --browser-only for full scope.",
        )
        diagnostics[-1]["omitted_browser_tool_groups"] = omitted_browser
    if not report.get("threads"):
        if target_reference and before_reference:
            note("reference_no_match", f"No reconstructed thread matches reference {target_reference!r}.")
        else:
            note(
                "no_reconstructed_threads",
                "The selected store and analysis scope yielded no reconstructable thread.",
            )
    if any((t.get("final_answer") or {}).get("unsupported_variants") for t in report.get("threads", [])):
        note(
            "unsupported_answer_variant",
            "Some entries have an unfamiliar workflow text variant and no supported completed answer.",
        )
    src["stage_selection"] = {
        "selected_stage": stage.to_metadata(),
        "total_stages_discovered": len(all_stages),
        "all_discovered_stages": [s.to_metadata() for s in all_stages],
        "stages_merged": False,
    }
    return report


def run_input_v038(
    input_path: Path,
    blob_input: Path | None = None,
    database: str = "auto",
    store: str = leg.DEFAULT_STORE,
    include_computer: bool = True,
    stage_selector: str | None = None,
    all_stages: bool = False,
    target_reference: str | None = None,
    dump_records_dir: Path | None = None,
) -> tuple[dict[str, Any], list[tuple[DiscoveredStage, dict[str, Any]]]]:
    """Run v0.38 reconstruction on a single-stage or multi-stage acquisition.

    Returns ``(primary_report, stage_reports)``.
    - For single-stage inputs or explicit ``stage_selector``: ``primary_report``
      is the standard single-stage report, and ``stage_reports`` has 1 item.
    - For multi-stage inputs with ``all_stages=True`` (or default when no
      ``stage_selector`` is supplied): each stage is reconstructed independently
      in chronological order (NEVER merged), and ``primary_report`` is a
      ``multi_stage_bundle`` index summarizing all stages.
    """
    input_path = Path(input_path).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root_path = input_path
    try:
        if leg.is_zip_path(input_path):
            temp_dir = tempfile.TemporaryDirectory(prefix="comet_reconstruct_")
            root_path = Path(temp_dir.name)
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(root_path)

        stages = discover_leveldb_stages(root_path)

        if stage_selector:
            chosen = select_stage(stages, stage_selector)
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=chosen,
                all_stages=stages,
                explicit_blob_input=blob_input,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=dump_records_dir,
            )
            return rep, [(chosen, rep)]

        if len(stages) == 1 and not all_stages:
            chosen = stages[0]
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=chosen,
                all_stages=stages,
                explicit_blob_input=blob_input,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=dump_records_dir,
            )
            return rep, [(chosen, rep)]

        # Multi-stage archive without explicit --stage (or explicit --all-stages):
        # Process every stage independently — NEVER merge stages!
        stage_reports: list[tuple[DiscoveredStage, dict[str, Any]]] = []
        stage_summaries: list[dict[str, Any]] = []
        for st in stages:
            st_dump = (dump_records_dir / st.stage_id) if dump_records_dir else None
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=st,
                all_stages=stages,
                explicit_blob_input=None,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=st_dump,
            )
            stage_reports.append((st, rep))
            stage_summaries.append(
                {
                    "stage_id": st.stage_id,
                    "stage_role": st.stage_role,
                    "rel_leveldb_path": st.rel_leveldb_posix,
                    "database": (rep.get("source") or {}).get("database"),
                    "extraction_summary": rep.get("extraction_summary"),
                    "summary": rep.get("summary"),
                    "source_file_temporal_context": (rep.get("source") or {}).get(
                        "source_file_temporal_context"
                    ),
                }
            )

        bundle = {
            "tool": "comet-browser-reconstructor",
            "report_kind": "multi_stage_acquisition_bundle",
            "schema_version": SCHEMA_VERSION_V038,
            "engine_provenance": _engine_provenance(),
            "source": {
                "input": str(input_path),
                "total_stages": len(stages),
                "stages_merged": False,
                "note": "Each discovered stage was reconstructed independently; stages are never merged.",
            },
            "stages": stage_summaries,
        }
        return bundle, stage_reports
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def build_parser_v038() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CometTrace v0.38 — Reconstruct Perplexity Comet Browser/Computer behavior from IndexedDB LevelDB artifacts."
    )
    parser.add_argument(
        "--engine",
        choices=["v038", "v037", "legacy"],
        default="v038",
        help="Select v0.38 patched engine (default) or frozen v0.37 legacy engine.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="LevelDB folder, profile/scenario folder, or ZIP containing Perplexity IndexedDB.",
    )
    parser.add_argument(
        "--blob-input", type=Path, default=None, help="Optional matching IndexedDB blob folder."
    )
    parser.add_argument(
        "--before", type=Path, default=None, help="Before snapshot zip/folder for deletion comparison."
    )
    parser.add_argument(
        "--after", type=Path, default=None, help="After snapshot zip/folder for deletion comparison."
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reconstruction.json"), help="JSON report output path."
    )
    parser.add_argument("--html-output", type=Path, default=None, help="Optional HTML report output path.")
    parser.add_argument(
        "--database",
        default="auto",
        help="IndexedDB database name, or 'auto' to detect keyval-store / pplx-query-cache-* automatically.",
    )
    parser.add_argument(
        "--store", default=leg.DEFAULT_STORE, help="IndexedDB object store name (default: keyval)."
    )
    parser.add_argument("--stage", default=None, help="Select a stage identifier reported by --list-stages.")
    parser.add_argument("--before-stage", help="Select the stage used on the before side.")
    parser.add_argument("--after-stage", help="Select the stage used on the after side.")
    parser.add_argument(
        "--all-stages",
        action="store_true",
        help="Independently reconstruct all stages in a multi-stage ZIP/folder.",
    )
    parser.add_argument(
        "--list-stages", action="store_true", help="List all discovered LevelDB stages in --input and exit."
    )
    parser.set_defaults(include_computer=True)
    parser.add_argument(
        "--include-computer",
        dest="include_computer",
        action="store_true",
        help="Include detected Computer/ASI tasks (default).",
    )
    parser.add_argument(
        "--browser-only",
        dest="include_computer",
        action="store_false",
        help="Legacy scope: skip Computer/ASI expansion, including ASI browser tools. Search records may remain.",
    )
    parser.add_argument(
        "--dump-records-dir",
        type=Path,
        default=None,
        help="Optional directory to write raw parsed record dumps.",
    )
    parser.add_argument(
        "--target-reference", default=None, help="Optional reference code/keyword for case-study filtering."
    )
    return parser


def _print_diagnostics(stages):
    for stage, report in stages:
        for item in report.get("diagnostics", []):
            print(f"[{item['code']}] {stage.stage_id}: {item['message']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser_v038()
    args = parser.parse_args(argv)
    paths = [args.output] + ([args.html_output] if args.html_output else [])
    if not args.list_stages:
        try:
            check_destinations(paths)
        except ValueError as exc:
            parser.error(str(exc))

    if args.engine in {"v037", "legacy"}:
        # Delegate directly to untouched frozen legacy_engine CLI
        forwarded: list[str] = []
        raw_argv = argv if argv is not None else __import__("sys").argv[1:]
        skip_next = False
        for i, tok in enumerate(raw_argv):
            if skip_next:
                skip_next = False
                continue
            if tok == "--engine":
                skip_next = True
                continue
            if tok.startswith("--engine="):
                continue
            forwarded.append(tok)
        import sys

        old_argv = sys.argv
        try:
            messages = StringIO()
            with staged_files(paths) as pending:
                sys.argv = [old_argv[0], *forwarded, "--output", str(pending[0])]
                if args.html_output:
                    sys.argv.extend(["--html-output", str(pending[1])])
                with redirect_stdout(messages):
                    leg.main()
            text = messages.getvalue()
            for temp_path, destination in zip(pending, paths):
                text = text.replace(str(temp_path), str(destination))
            print(text, end="")
        finally:
            sys.argv = old_argv
        return

    if args.list_stages:
        if not args.input:
            raise SystemExit("--list-stages requires --input.")
        inp = args.input.expanduser().resolve()
        temp_dir = None
        root = inp
        try:
            if leg.is_zip_path(inp):
                temp_dir = tempfile.TemporaryDirectory(prefix="comet_reconstruct_")
                root = Path(temp_dir.name)
                with zipfile.ZipFile(inp, "r") as zf:
                    zf.extractall(root)
            stages = discover_leveldb_stages(root)
            print(json.dumps([s.to_metadata() for s in stages], indent=2, ensure_ascii=False))
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        return

    if args.before or args.after:
        if not args.before or not args.after:
            raise SystemExit("Both --before and --after are required for snapshot comparison mode.")

        def snapshot(path, selector, side):
            if not selector and path.is_dir() and len(discover_leveldb_stages(path)) > 1:
                parser.error(f"Multiple stages in {side} input. Select --{side}-stage.")
            report, stages = run_input_v038(
                input_path=path,
                database=args.database,
                store=args.store,
                include_computer=args.include_computer,
                stage_selector=selector,
                target_reference=args.target_reference,
            )
            _print_diagnostics(stages)
            if "threads" not in report:
                parser.error(f"Multiple stages in {side} input. Select --{side}-stage.")
            return report

        before = snapshot(args.before, args.before_stage or args.stage, "before")
        after = snapshot(args.after, args.after_stage or args.stage, "after")
        with _v038_runtime_patches():
            comparison = leg.compare_reconstructions(before, after)
        comp = {
            "tool": "comet-browser-reconstructor",
            "schema_version": SCHEMA_VERSION_V038,
            "mode": "before_after_comparison",
            "before_reconstruction": before,
            "after_reconstruction": after,
            "snapshot_comparison": comparison,
        }
        save_reports(comp, args.output, args.html_output)
        print(f"Wrote comparison report to {args.output}")
        return

    if not args.input:
        raise SystemExit("Provide --input <path> (or --before and --after).")

    primary, stage_reports = run_input_v038(
        input_path=args.input,
        blob_input=args.blob_input,
        database=args.database,
        store=args.store,
        include_computer=args.include_computer,
        stage_selector=args.stage,
        all_stages=args.all_stages,
        target_reference=args.target_reference,
        dump_records_dir=args.dump_records_dir,
    )

    _print_diagnostics(stage_reports)
    if primary.get("report_kind") == "multi_stage_acquisition_bundle":
        save_reports(primary, args.output, args.html_output, stage_reports)
        print(f"Wrote multi-stage bundle ({len(stage_reports)} independent stages) to {args.output}")
    else:
        save_reports(primary, args.output, args.html_output)
        print(f"Wrote v0.38 report to {args.output}")
