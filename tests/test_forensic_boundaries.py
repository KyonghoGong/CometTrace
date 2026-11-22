import unittest
from unittest.mock import patch

from comettrace import engine_v038 as engine
from comettrace import legacy_engine as leg


def workflow_record(payloads):
    steps = [{"status":"WORKFLOW_COMPLETED","items":[{"type":"WORKFLOW_ITEM_TEXT","payload":{"text_payload":p}}]} for p in payloads]
    return leg.ForensicRecord("all_results", [{"backend_uuid":"test","blocks":[{"workflow_block":{"steps":steps}}]}], 42, True, [], [], record_kind="all_results")


class EvidenceBoundaries(unittest.TestCase):
    def test_ancillary_backend_id_cannot_replace_entry_owner(self):
        record = workflow_record([])
        entry = record.value[0]
        entry["context_uuid"] = "different-context"
        entry["upsell_information"] = {"backend_uuid": "different-context"}
        metadata = engine._extract_metadata([record])
        self.assertEqual(metadata["backend_uuid"], entry["backend_uuid"])
        self.assertEqual(metadata["context_uuid"], "different-context")
        self.assertEqual(metadata["evidence"]["backend_uuid"], leg.record_evidence(record, max_items=2))

    def test_prompt_recipient_stops_before_instruction_paragraph(self):
        prompt = "Recipient: test contact\r\n\r\nWrite to another@example.test only in the body."
        with engine._v038_runtime_patches():
            payloads = leg._v18_extract_labeled_payloads_from_text(prompt, "prompt", [], 2)
            self.assertEqual([p["value"] for p in payloads], ["test contact"])
            thread = {"prompt": {"text": prompt}, "task_outcome": {"task_type": "email_draft_send"},
                      "typed_payloads": payloads}
            self.assertEqual(leg.build_structured_actions(thread), [])
            observed = dict(payloads[0], payload_source="observed_form_input")
            self.assertEqual(leg.build_structured_actions({"typed_payloads": [observed]})[0]["value"], "test contact")
            for _ in range(2):
                engine._partition(thread)
                self.assertEqual(thread["confirmed_payloads"], [])
                self.assertEqual([p["value"] for p in thread["candidate_payloads"]], ["test contact"])

    def test_unlabelled_clock_input_does_not_become_calendar_title(self):
        def run(title):
            thread = {"task_outcome": {"task_type": "calendar_create"},
                      "typed_payloads": [{"field": "event_title", "value": title, "payload_source": "prompt"}],
                      "observed_input_artifacts": [{"value": "10:00 AM", "field": "text"}]}
            engine._promote_input(thread)
            self.assertEqual(thread["observed_input_artifacts"][0]["value"], "10:00 AM")
            return [p["value"] for p in thread["typed_payloads"] if p["field"] == "event_title"]
        self.assertEqual(run("Lab meeting"), ["Lab meeting"])
        self.assertEqual(run("10:00 AM"), ["10:00 AM"])

    def test_exact_answer_and_chunks(self):
        chunks = ["Open", " Seoul at https://example.test/news-", "insights.\nKeep ", "this."]
        for full in (True, False):
            with self.subTest(full=full):
                raw = "".join(chunks)
                answer = dict(variant="answer", chunks=chunks, is_streaming=False)
                if full:
                    answer["text"] = raw
                rec = workflow_record([{"variant":"thought","text":"I am still considering the request. "*15}, answer])
                with engine._v038_runtime_patches():
                    fa=leg.extract_final_answer([rec])
                    thread={"final_answer":fa}
                    leg._v27_clean_thread_text_and_urls(thread)
                self.assertEqual(thread["final_answer"]["text"], raw)
                self.assertEqual(fa["relative_order"], 42)

    def test_thought_alone(self):
        rec=workflow_record([{"variant":"thought","text":"I will open the page and review the contents."}])
        with engine._v038_runtime_patches():
            self.assertFalse(leg.extract_final_answer([rec])["available"])

    def test_negative_download_evidence(self):
        for suffix, status in [("Save is waiting for your approval.", "download_confirmation_pending"),
                               ("Saving was blocked by the browser.", "download_failed"),
                               ("", None)]:
            t={"prompt":{"text":"Download the PDF."},"final_answer":{"text":"The final filename is report_Q4.pdf. "+suffix},
               "actions":[{"kind":"workflow_action","tool_name":"browser_downloads"}]}
            with self.subTest(suffix=suffix), engine._v038_runtime_patches():
                out=leg.classify_task_outcome(t)
                self.assertFalse(out["side_effect_completed"])
                if status:self.assertEqual(out["status"],status)
                self.assertIsNone(leg._v23_reported_downloaded_filename(t))

    def test_completed_report_with_duplicate_filename(self):
        t={"prompt":{},"final_answer":{"text":"Done. Downloaded file (exactly one): annual-report (2).pdf (about 1.9 MB)."}}
        with engine._v038_runtime_patches():
            self.assertEqual(leg.classify_task_outcome(t)["status"],"completed_download")
            self.assertEqual(leg._v23_reported_downloaded_filename(t),"annual-report (2).pdf")

    def test_unrelated_history(self):
        thread={"final_answer":{"text":"I saved the file from https://a.example/page"},
                "task_outcome":{"task_type":"file_download","status":"completed_download",
                                "side_effect_completed":True,"reported_downloaded_filename":"shared.pdf"}}
        rows=[dict(id=9,filename="shared.pdf",state=1,interrupt_reason=0,tab_url="https://b.example/page",url_chain=[])]
        with patch.object(engine,"read_chromium_history_downloads",return_value=rows):
            engine.attach_history_download_corroboration({"threads":[thread]}, None)
        self.assertFalse(thread["task_outcome"].get("history_corroborated_complete"))

    def test_compare_identity(self):
        def t(uid):return {"thread_id":uid,"prompt":{"text":"S04_WebResearch_20261105_R01 is my reference code."}}
        left={"threads":[t("stay"),t("lost")]}; right={"threads":[t("stay"),t("new")]}
        with engine._v038_runtime_patches():
            result=leg.compare_reconstructions(left,right)["summary"]
        self.assertEqual([result[k+"_thread_count"] for k in ("persisted","disappeared","appeared")],[1,1,1])

    def test_gmail_partition(self):
        t={"task_outcome":{"task_type":"email_draft_send"},"prompt":{"text":"Compose for subject Evidence Record"},
           "final_answer":{"text":""},"typed_payloads":[{"field":"subject","value":"Evidence Record"},{"field":"subject","value":"Unrelated string"}]}
        with engine._v038_runtime_patches():
            for _ in range(2):
                leg._v28_partition_typed_payloads(t)
                self.assertEqual([p["value"] for p in t["confirmed_payloads"]],["Evidence Record"])
                self.assertEqual([p["value"] for p in t["candidate_payloads"]],["Unrelated string"])


if __name__ == "__main__":
    unittest.main()
