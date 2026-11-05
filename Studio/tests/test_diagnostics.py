from __future__ import annotations

import unittest

from docurandom.diagnostics import format_exception_report, sanitize_diagnostic_text


class DiagnosticTests(unittest.TestCase):
    def test_sanitizer_redacts_browser_credentials(self) -> None:
        raw = (
            "http://127.0.0.1:51080/mcp/abcdefghijklmnop "
            "Authorization: Bearer secret-token "
            'access_token="access-value" refresh_token=refresh-value '
            "password='hidden' cookie=session=abc"
        )

        cleaned = sanitize_diagnostic_text(raw)

        self.assertNotIn("abcdefghijklmnop", cleaned)
        self.assertNotIn("secret-token", cleaned)
        self.assertNotIn("access-value", cleaned)
        self.assertNotIn("refresh-value", cleaned)
        self.assertNotIn("hidden", cleaned)
        self.assertNotIn("session=abc", cleaned)
        self.assertGreaterEqual(cleaned.count("<REDACTED>"), 6)

    def test_exception_report_keeps_stack_and_metadata(self) -> None:
        try:
            raise RuntimeError("render failed")
        except RuntimeError as exc:
            report = format_exception_report(
                "render-worker",
                exc,
                metadata={"workspace_id": "workspace-1", "job_id": "job-9"},
            )

        self.assertIn("context=render-worker", report)
        self.assertIn("RuntimeError: render failed", report)
        self.assertIn("workspace_id='workspace-1'", report)
        self.assertIn("test_exception_report_keeps_stack_and_metadata", report)


if __name__ == "__main__":
    unittest.main()
