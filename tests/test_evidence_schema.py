"""Public schema controls use invented identifiers, file names and per-entry events."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from comettrace import engine_v038 as e
from comettrace import legacy_engine as leg


def record(entry):
    return leg.ForensicRecord('["cache","all_results","owner"]', [dict(backend_uuid="owner", **entry)],
                              7, True, [], [], record_kind="all_results")


def workflow(text, variant="answer", status="WORKFLOW_COMPLETED", tool=None):
    step={"status":status, "items":[{"payload":{"text_payload":{"variant":variant,"text":text}}}]}
    if tool:
        step["tool_name"]=tool
    return {"workflow_block":{"steps":[step]}}


class EvidenceSchema(unittest.TestCase):
    def test_search_backend_and_quoted_tool_words(self):
        r=record({"mode":"COPILOT","search_mode":"SEARCH","query_str":"say browser_click ASI",
                  "blocks":[workflow("sample","thought",tool="search_web")]})
        cls=e.classify_group_v038([r])
        self.assertEqual(cls["execution_mode"],"non_browser_agent")
        self.assertEqual(set(cls["observed_tool_events"]),{"search_web"})
        self.assertEqual(cls["observed_browser_tools"],[])
        self.assertIsNone(cls["ui_selection"]["value"])
        study = e.classify_group_v038([record({"mode": "COPILOT", "search_mode": "STUDY",
                                              "display_model": "pplx_study"})])
        self.assertEqual(study["execution_mode"], "non_browser_agent")
        self.assertEqual(study["observed_backend"]["search_mode"], "STUDY")
        self.assertEqual(e.classify_group_v038([record({"blocks":[workflow("a","thought")]})])["execution_mode"],"unknown")

    def test_asi_browser_invocation_and_requested_skill_are_independent(self):
        block=workflow("answer",tool="browser_open")
        for prompt, tagged in [("ordinary request",False),("[Skill](https://www.perplexity.ai/computer/skills?skillId=control-local-browser) read",True)]:
            cls=e.classify_group_v038([record({"mode":"ASI","search_mode":"ASI","query_str":prompt,"blocks":[block]})])
            self.assertEqual(cls["browser_control_skill_in_query_prefix"],tagged)
            self.assertEqual(cls["observed_browser_tools"],["browser_open"])
            self.assertEqual(cls["ui_selection"]["status"],"not_established")
            self.assertEqual(cls["observed_tool_events"]["browser_open"]["sequence"],7)

    def test_legacy_backend_signal_and_foreign_nested_workflow(self):
        child={"backend_uuid":"another","status":"WORKFLOW_COMPLETED",**workflow("x",tool="browser_open")["workflow_block"]}
        block={"workflow_block":{"steps":[{"items":[{"payload":{"workflow_payload":child}}]}]}}
        cls=e.classify_group_v038([record({"search_mode":"BROWSER_AGENT","blocks":[block]})])
        self.assertEqual(cls["execution_mode"],"browser_control")
        self.assertEqual(cls["observed_browser_tools"],[])

    def test_unknown_text_and_malformed_workflow_never_become_final(self):
        for block in (workflow("Private planning text " * 6,"reasoning"),
                      {"workflow_block":{"steps":123}}, {"new_payload":{"answer":"Unrecognized " * 8}}):
            fa=e._exact_final([record({"status":"COMPLETED","query_str":"task","blocks":[block]})])
            self.assertFalse(fa["available"])
            self.assertIsNone(fa["text"])
        self.assertEqual(e._exact_final([record({"blocks":[workflow("Done.")]})])["text"],"Done.")

    def test_markdown_answer_requires_completion(self):
        block={"intended_usage":"ask_text","markdown_block":{"answer":"Exact.\r\n"}}
        for extra in [{},{"text_completed":False},{"final":False,"status":"COMPLETED"}]:
            self.assertFalse(e._exact_final([record({"blocks":[block],**extra})])["available"])
        self.assertEqual(e._exact_final([record({"blocks":[block],"status":"COMPLETED"})])["text"],"Exact.\r\n")

    def test_pdf_names_preserve_labelled_and_delimited_spelling(self):
        for name in ("cv.PDF","月次  予算表.Pdf","a.pdf.pdf","Annual overview (2).PDF","_a.PDF"):
            txt="I downloaded one file. The final filename is "+name+"."
            self.assertEqual(e.extract_pdf_filenames_v038(txt),[name])
            self.assertEqual(e._v23_reported_downloaded_filename_v038({"final_answer":{"text":txt}}),name)
        self.assertEqual(e.extract_pdf_filenames_v038('open "Short name.PDF" later'),["Short name.PDF"])
        self.assertEqual(e.extract_pdf_filenames_v038("data.csv or app.pdf.exe"),[])

    def test_short_preview_is_entry_scoped_and_labelled(self):
        uid="12345678-1111-4111-a111-123456789012"
        row={"uuid":uid,"context_uuid":uid,"link":"/computer/tasks/"+uid,"answer_preview":"네."}
        rec=record({}); rec.value=row; rec.record_kind="thread_list_entry_scoped"
        t=e.build_promoted_computer_thread_v038(uid,{"entries":[row],"records":[rec],"source_kinds":{"thread_list_sidebar"}},[])
        self.assertEqual((t["final_answer"]["text"],t["final_answer"]["answer_scope"],t["final_answer"]["relative_order"]),("네.","list_preview_only",7))

    def test_history_failure_differs_from_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/"History"
            report={}
            e.attach_history_download_corroboration(report,None)
            self.assertEqual(report["source"]["history_read_status"],"not_present")
            f.write_bytes(b"malformed database")
            e.attach_history_download_corroboration(report,f)
            self.assertEqual(report["source"]["history_read_status"],"read_failed")
            self.assertEqual(report["diagnostics"][-1]["code"],"history_read_failed")

    def test_database_error_is_diagnostic(self):
        with patch.object(e,"discover_query_databases",side_effect=OSError("unreadable")):
            with self.assertRaisesRegex(RuntimeError,"database_discovery_failed"):
                e.resolve_database_selection(Path("."),None,"auto")

    def test_readable_empty_download_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "History"
            with closing(sqlite3.connect(history)) as conn:
                conn.execute("CREATE TABLE downloads (id, guid, current_path, target_path, start_time, "
                             "received_bytes, total_bytes, state, danger_type, interrupt_reason, end_time, tab_url, mime_type)")
                conn.execute("CREATE TABLE downloads_url_chains (id, chain_index, url)")
            report = {}
            e.attach_history_download_corroboration(report, history)
            self.assertEqual(report["source"]["history_read_status"], "read_empty")
            self.assertEqual(report["diagnostics"], [])

    def test_stage_role_survives_renaming_the_subject(self):
        for subject in ("Sample", "異なる名前", "Experiment42"):
            for folder, role in ((subject, "stage_snapshot"), ("after_" + subject, "initial_collection")):
                location = Path(folder) / "IndexedDB" / leg.TARGET_LEVELDB_NAME
                self.assertEqual(e._infer_stage_id_and_role(location, 2), (folder, role))

    def test_auto_store_diagnostics_distinguish_schema_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            stage = e.DiscoveredStage("stage", "single_snapshot", path, None, None, ".", 0)
            for count, unreadable, expected in ((0, 0, "empty_store"), (2, 0, "unrecognized_thread_keys"),
                                                (0, 1, "records_unreadable")):
                dbs = [dict(name=name, has_target_store=True, record_count=count, unreadable_records=unreadable,
                            recognized_thread_keys=0, perplexity_cache_record_count=0,
                            is_pplx_query_cache=False, is_legacy_keyval_store=False) for name in ("alpha", "beta")]
                with patch.object(e, "discover_query_databases", return_value=dbs), \
                     patch.object(leg, "extract_records_from_leveldb", return_value={}), \
                     patch.object(leg, "reconstruct_browser_threads", return_value={"threads": []}):
                    report = e.run_stage_v038(path, path, stage, [stage])
                codes = {d["code"] for d in report["diagnostics"]}
                self.assertEqual(codes, {expected, "multiple_databases", "no_reconstructed_threads"})
