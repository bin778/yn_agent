import unittest
from pathlib import Path

from scripts.validate_phase0 import approval_is_valid, automated_errors, file_sha256
from scripts.validate_phase1 import automated_errors as phase1_errors


class Phase0ArtifactsTest(unittest.TestCase):
    def test_automated_phase0_contracts(self):
        self.assertEqual([], automated_errors())

    def test_human_approval_requires_identity_and_date(self):
        self.assertFalse(approval_is_valid({"status": "pending"}))
        self.assertFalse(approval_is_valid({"status": "approved"}))
        self.assertFalse(
            approval_is_valid(
                {"status": "approved", "reviewer": "reviewer-1", "reviewed_at": None}
            )
        )

    def test_korean_bar_pdf_is_pinned(self):
        self.assertEqual(
            "01fdc401c50dd71d7d106379b73b8e3ab90b200dcf41a2d03bd3165be6154967",
            file_sha256(
                Path(__file__).resolve().parents[1]
                / "_shared/knowledge/source_docs/korean_bar_violation_examples_2023.pdf"
            ),
        )
        self.assertTrue(
            approval_is_valid(
                {
                    "status": "approved",
                    "reviewer": "reviewer-1",
                    "reviewed_at": "2026-09-08",
                }
            )
        )

    def test_phase1_contracts(self):
        self.assertEqual([], phase1_errors())


if __name__ == "__main__":
    unittest.main()
