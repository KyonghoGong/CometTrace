"""Unit and structural regression tests for CometTrace v0.38."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from comettrace import ENGINE_SHA256
from comettrace import engine_v038 as v38
from comettrace import legacy_engine as leg
from comettrace.provenance import compute_engine_sha256


class TestProvenanceIntegrity(unittest.TestCase):
    def test_legacy_engine_remains_frozen(self) -> None:
        self.assertEqual(compute_engine_sha256(), ENGINE_SHA256)
        self.assertEqual(
            compute_engine_sha256(),
            "f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80",
        )


class TestFixEFilenameExtraction(unittest.TestCase):
    def test_preserves_repeated_pdf_extension(self) -> None:
        text = "I downloaded one\nfile. The final filename is Monthly.Plan-1.pdf.pdf (about 2 MB)."
        self.assertEqual(v38.extract_pdf_filenames_v038(text), ["Monthly.Plan-1.pdf.pdf"])
        rep = v38._v23_reported_downloaded_filename_v038({"final_answer": {"text": text}})
        self.assertEqual(rep, "Monthly.Plan-1.pdf.pdf")

    def test_standard_and_edge_cases(self) -> None:
        self.assertEqual(
            v38.extract_pdf_filenames_v038("Final downloaded filename: **Monthly.Plan-1\n.pdf**"),
            ["Monthly.Plan-1.pdf"],
        )
        self.assertEqual(
            v38.extract_pdf_filenames_v038("Saved 4-Sample_Project-AI-Guide.pdf and triple.doc.pdf.pdf.pdf."),
            ["4-Sample_Project-AI-Guide.pdf", "triple.doc.pdf.pdf.pdf"],
        )
        self.assertEqual(v38.extract_pdf_filenames_v038("No files here at all."), [])
        self.assertIsNone(
            v38._v23_reported_downloaded_filename_v038({"final_answer": {"text": "No files here."}})
        )


class TestFixDDownloadClassification(unittest.TestCase):
    def _make_thread(self, prompt: str, final: str, actions: list | None = None) -> dict:
        return {
            "thread_id": "test-thread",
            "classification": {"execution_mode": "computer_mode", "interaction_type": "agentic"},
            "prompt": {"text": prompt},
            "final_answer": {"text": final, "available": bool(final)},
            "actions": actions or [],
            "urls": [],
            "typed_payloads": [],
            "metadata": {"mode": "ASI", "search_mode": "ASI"},
            "plan": [],
            "timeline": [],
            "reasoning": {"items": []},
        }

    def test_new_s06_classified_as_completed_download(self) -> None:
        t = self._make_thread(
            prompt=(
                'S06_Download_20261105_R01. Use browser control to find and download the official PDF '
                'document for "Sample planning". Open the official Sample Guide source page, '
                'download exactly one PDF file, and tell me the final downloaded filename.'
            ),
            final=(
                "I downloaded one\nfile. The final filename is Monthly.Plan-1.pdf.pdf (about 2 MB).\n"
                "- Source page: I opened Sample Guide's official publication page https://example.test/planning\n"
                "- How the download happened: I saved the file from that tab."
            ),
            actions=[{"kind": "workflow_action", "tool_name": "browser_downloads", "label": "다운로드 확인 중"}],
        )
        with v38._v038_runtime_patches():
            out = leg.classify_task_outcome(t)
        self.assertEqual(out["task_type"], "file_download")
        self.assertEqual(out["status"], "completed_download")
        self.assertTrue(out["side_effect_completed"])
        self.assertEqual(out["classification_basis"]["scores"]["file_download"], 10)
        self.assertIn("Monthly.Plan-1.pdf.pdf", out["downloaded_filename_candidates"])

    def test_failed_and_interrupted_and_pending_distinguished(self) -> None:
        t_fail = self._make_thread(
            prompt="Download the official PDF Sample planning.",
            final="I failed to download Monthly.Plan-1.pdf due to a network error.",
            actions=[{"kind": "workflow_action", "tool_name": "browser_downloads"}],
        )
        t_int = self._make_thread(
            prompt="Download the official PDF Sample planning.",
            final="The download was cancelled before finishing Monthly.Plan-1.pdf.",
            actions=[{"kind": "workflow_action", "tool_name": "browser_downloads"}],
        )
        t_pend = self._make_thread(
            prompt="Download the official PDF Sample planning.",
            final="I found Monthly.Plan-1.pdf. I need to confirm the download with you before proceeding.",
            actions=[{"kind": "workflow_action", "label": "BROWSER_AGENT_CONFIRMATION"}],
        )
        with v38._v038_runtime_patches():
            o_fail = leg.classify_task_outcome(t_fail)
            o_int = leg.classify_task_outcome(t_int)
            o_pend = leg.classify_task_outcome(t_pend)
        self.assertEqual(o_fail["task_type"], "file_download")
        self.assertEqual(o_fail["status"], "download_failed")
        self.assertFalse(o_fail["side_effect_completed"])
        self.assertEqual(o_int["status"], "download_interrupted")
        self.assertFalse(o_int["side_effect_completed"])
        self.assertEqual(o_pend["status"], "download_confirmation_pending")
        self.assertFalse(o_pend["side_effect_completed"])

    def test_page_open_not_misclassified_as_download(self) -> None:
        t_open = self._make_thread(
            prompt="Open https://en.wikipedia.org/wiki/Alan_Turing and confirm the page title. Do not download any files.",
            final="I opened the Alan Turing Wikipedia page and confirmed the page title is Alan Turing.",
            actions=[{"kind": "workflow_action", "tool_name": "browser_navigate"}],
        )
        with v38._v038_runtime_patches():
            out = leg.classify_task_outcome(t_open)
        self.assertEqual(out["task_type"], "page_open")


class TestFixCMultiThreadListIsolation(unittest.TestCase):
    def test_sidebar_list_not_grouped_into_first_uuid_and_entries_isolated(self) -> None:
        curie_uuid = "00000000-0000-4000-8000-000000000001"
        curie_ctx = "00000000-0000-4000-8000-000000000002"
        nist_uuid = "00000000-0000-4000-8000-000000000003"
        nist_ctx = "00000000-0000-4000-8000-000000000004"

        sidebar_val = {
            "pages": [
                {
                    "threads": [
                        {
                            "uuid": curie_uuid,
                            "context_uuid": curie_ctx,
                            "link": f"/computer/tasks/{curie_uuid}",
                            "status": "completed",
                            "title": "Navigate from Example Museum to Sample Exhibit",
                            "answer_preview": 'I got from "Example Museum" to "Sample Exhibit" in the sample session.',
                        },
                        {
                            "uuid": nist_uuid,
                            "context_uuid": nist_ctx,
                            "link": f"/computer/tasks/{nist_uuid}",
                            "status": "completed",
                            "title": "Download Sample planning PDF",
                            "answer_preview": (
                                "Done. Here's the summary:\nOfficial source page: [Sample Planning Guide]"
                                "(https://example.test/guides) " + ("x" * 200)
                            ),
                        },
                    ]
                }
            ]
        }
        raw_rec = {
            "key": '<IdbKey ["pplx-query-cache-fictional","graphql-sidebar-v2","initial-pages-with-teammates","acc","sidebar-snapshot",false]>',
            "ldb_seq_no": 123,
            "is_live": True,
            "source_files": [{"source_file": "000007.log", "source_type": "log", "offset": 100, "state": "Live"}],
            "source_types": ["log"],
            "value": sidebar_val,
        }
        with v38._v038_runtime_patches():
            frec = leg.normalize_records([raw_rec])[0]
            self.assertEqual(frec.record_kind, "thread_list_sidebar")
            groups, globals_ = leg.group_records([frec])
            self.assertEqual(groups, {})
            self.assertEqual(len(globals_), 1)
            cands = leg.collect_asi_thread_list_candidates_v3([frec])
            self.assertIn(curie_uuid, cands)
            self.assertIn(nist_uuid, cands)
            curie_thread = leg.build_promoted_computer_thread(curie_uuid, cands[curie_uuid], [])
            self.assertIn("Example Museum", curie_thread["final_answer"]["text"])
            self.assertNotIn("Sample Guide", curie_thread["final_answer"]["text"])
            self.assertEqual(curie_thread["final_answer"]["answer_scope"], "list_preview_only")
            self.assertEqual(curie_thread["metadata"]["context_uuid"], curie_ctx)


class TestFixAMultiStageZipDiscoveryAndScoping(unittest.TestCase):
    def test_discovers_all_stages_in_chronological_order_and_scopes_temporal_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            zpath = td_path / "S09_multi.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("S09_02/U/Default/IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb/000823.log", b"u")
                zf.writestr("S09_02/D/Default/IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb/000842.log", b"d")
                zf.writestr("S09_02/R/Default/IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb/000859.log", b"r")
            ext = td_path / "ext"
            with zipfile.ZipFile(zpath, "r") as zf:
                zf.extractall(ext)
            stages = v38.discover_leveldb_stages(ext)
            self.assertEqual([s.stage_id for s in stages], ["U", "D", "R"])
            self.assertEqual([s.stage_role for s in stages], ["pre_deletion", "post_deletion", "post_restart"])

            d_stage = v38.select_stage(stages, "D")
            ctx = v38.build_scoped_source_file_temporal_context(zpath, ext, d_stage.leveldb_path, single_stage_archive=False)
            self.assertEqual(ctx["source_file_count"], 1)
            self.assertEqual(list(ctx["by_source_file"].keys()), ["000842.log"])
            self.assertTrue(ctx["stage_scoped"])


class TestFixEDownstreamV24V25Consistency(unittest.TestCase):
    def test_v24_v25_preserve_repeated_pdf_extension(self) -> None:
        thread = {
            "thread_id": "00000000-0000-4000-8000-000000000005",
            "prompt": {"text": "Download Sample planning PDF"},
            "final_answer": {
                "available": True,
                "text": "I downloaded one\nfile. The final filename is Monthly.Plan-1.pdf.pdf (about 2 MB).",
            },
            "task_outcome": {
                "task_type": "file_download",
                "status": "completed_download",
                "downloaded_filename_candidates": ["Monthly.Plan-1.pdf.pdf"],
                "classification_basis": {
                    "trace_markers": {"pdf_filename_candidates": ["Monthly.Plan-1.pdf.pdf"]}
                },
            },
            "payload_audit": {},
        }
        with v38._v038_runtime_patches():
            self.assertEqual(leg._v24_reported_downloaded_filename(thread), "Monthly.Plan-1.pdf.pdf")
            leg._v24_update_download_filename_views(thread)
            leg._v25_clean_legacy_pdf_trace_fields(thread)
            self.assertEqual(thread["task_outcome"]["reported_downloaded_filename"], "Monthly.Plan-1.pdf.pdf")
            self.assertEqual(thread["task_outcome"]["downloaded_filename_candidates"], ["Monthly.Plan-1.pdf.pdf"])
            self.assertEqual(thread["payload_audit"]["reported_downloaded_filename"], "Monthly.Plan-1.pdf.pdf")
            self.assertEqual(thread["payload_audit"]["nearby_pdf_candidates"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
