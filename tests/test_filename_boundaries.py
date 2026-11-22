"""A PDF mention, an attributed name and a completion claim are separate evidence."""
import unittest

from comettrace import engine_v038 as engine


class FilenameBoundaries(unittest.TestCase):
    def test_reference_across_a_clause_is_not_the_reported_filename(self):
        for separator in ("; ", ", ", ". ", "。", "\n"):
            for prefix in ("", "I downloaded one PDF. "):
                text = prefix + "The final filename is undecided" + separator + "reference copy is guide.pdf."
                thread = {"prompt": {"text": "Download one PDF"}, "final_answer": {"text": text}}
                with self.subTest(text=text), engine._v038_runtime_patches():
                    self.assertEqual(engine.extract_pdf_filenames_v038(text), ["guide.pdf"])
                    self.assertIsNone(engine._v23_reported_downloaded_filename_v038(thread))
                    outcome = engine.classify_task_outcome_v038(thread)
                    self.assertIsNone(outcome.get("reported_downloaded_filename"))
                    self.assertEqual(bool(outcome["side_effect_completed"]), bool(prefix))

    def test_quotes_delimit_literal_punctuation(self):
        for name in ("Budget, Q4.PDF", "계획; 개정.pdf.pdf", "a.PDF"):
            text = 'I downloaded one file. Filename: "' + name + '".'
            thread = {"final_answer": {"text": text}}
            self.assertEqual(engine.extract_pdf_filenames_v038(text), [name])
            self.assertEqual(engine._v23_reported_downloaded_filename_v038(thread), name)

    def test_unconfirmed_name_does_not_borrow_a_reference(self):
        text = "I downloaded one PDF. The final filename is not confirmed in UI yet; reference copy is guide.pdf."
        self.assertEqual(engine.extract_pdf_filenames_v038(text), ["guide.pdf"])
        self.assertIsNone(engine._v23_reported_downloaded_filename_v038({"final_answer": {"text": text}}))

    def test_bold_download_name_and_explicitly_blocked_save(self):
        text = "I downloaded the document and saved it as **Quarterly Financial Report.PDF**."
        thread = {"prompt": {"text": "Download a PDF"}, "final_answer": {"text": text}}
        self.assertEqual(engine._v23_reported_downloaded_filename_v038(thread), "Quarterly Financial Report.PDF")
        with engine._v038_runtime_patches():
            thread["final_answer"]["text"] = "Saving 계획.PDF was blocked by browser policy."
            result = engine.classify_task_outcome_v038(thread)
            self.assertEqual(result["status"], "download_failed")
            self.assertIs(result["side_effect_completed"], False)
            self.assertIsNone(result.get("reported_downloaded_filename"))
        self.assertIsNone(engine._download_negative_status("Saving plan.pdf succeeded, but login was blocked."))

    def test_a_descriptive_heading_does_not_cancel_an_explicit_value(self):
        text = 'I downloaded one PDF. **Filename:** Plan.v2.PDF.pdf (2 MB).\n**Filename:** Its extension repeats.'
        thread = {"final_answer": {"text": text}}
        self.assertEqual(engine._v23_reported_downloaded_filename_v038(thread), "Plan.v2.PDF.pdf")

    def test_bold_heading_with_a_bold_inline_code_value(self):
        text = "Download complete. **Final downloaded filename:** **\x60plan.v2.PDF\x60**"
        self.assertEqual(engine._v23_reported_downloaded_filename_v038({"final_answer": {"text": text}}), "plan.v2.PDF")
