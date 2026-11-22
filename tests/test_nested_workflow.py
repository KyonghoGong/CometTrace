import unittest

from comettrace import engine_v038 as engine
from comettrace import legacy_engine as legacy


def step(payload, status="WORKFLOW_COMPLETED"):
    return {"status": status, "items": [{"payload": payload}]}


def record(steps):
    entry = {"backend_uuid": "owner", "blocks": [{"workflow_block": {"steps": steps}}]}
    return legacy.ForensicRecord("all_results", [entry], 71, True, [], [], record_kind="all_results")


class NestedWorkflowTests(unittest.TestCase):
    def test_completed_nested_answer_preserves_original_bytes_and_path(self):
        raw = "I've  kept — 두 칸\r\n\nthird line.  "
        for source in ({"text": raw}, {"chunks": [raw[:3], raw[3:11], raw[11:]]}):
            with self.subTest(source=source):
                leaf = step({"text_payload": {"variant": "answer", **source}})
                inner = {"status": "WORKFLOW_COMPLETED", "steps": [leaf]}
                intermediate = {"status": "WORKFLOW_COMPLETED", "steps": [step({"workflow_payload": inner})]}
                thread = {"final_answer": engine._exact_final([record([step({"workflow_payload": intermediate})])])}
                engine._clean_thread(thread)
                answer = thread["final_answer"]
                self.assertEqual(answer["text"].encode("utf-8"), raw.encode("utf-8"))
                self.assertEqual(answer["relative_order"], 71)
                self.assertEqual(answer["raw_value_path"], "[0].blocks[0].workflow_block"
                                 + ".steps[0].items[0].payload.workflow_payload" * 2
                                 + ".steps[0].items[0].payload.text_payload." + next(iter(source)))

    def test_outer_answer_precedes_children_and_ineligible_children_do_not_win(self):
        def child(text, **overrides):
            return step({"workflow_payload": {"status": "WORKFLOW_COMPLETED", "steps": [
                step({"text_payload": {"variant": "answer", "text": text}})], **overrides}})

        exact = "Published answer"
        outer = step({"text_payload": {"variant": "answer", "text": exact}})
        self.assertEqual(engine._exact_final([record([child("much longer " * 8), outer])])["text"], exact)
        self.assertFalse(engine._exact_final([record([child("first"), child("second")])])["available"])
        for other in (child("not complete", status="IN_PROGRESS"), child("foreign", backend_uuid="other"),
                      step({"text_payload": {"variant": "answer", "is_streaming": True, "text": "partial"}})):
            self.assertEqual(engine._exact_final([record([other, child(exact)])])["text"], exact)
