from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from docurandom.private_github import (
    PrivateGitHubError,
    clear_private_github_token,
    load_private_github_token,
    private_github_request,
    private_github_token_url,
    save_private_github_token,
)


class PrivateGitHubTests(unittest.TestCase):
    def test_token_link_prefills_read_only_owner_scope(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        parsed = urlsplit(private_github_token_url())
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "github.com")
        self.assertEqual(parsed.path, "/settings/personal-access-tokens/new")
        self.assertEqual(query["target_name"], ["JustHari01"])
        self.assertEqual(query["contents"], ["read"])

    def test_token_is_protected_at_rest_and_can_be_cleared(self) -> None:
        token = "github_pat_private_donut_read_only_123456789"
        with tempfile.TemporaryDirectory(prefix="DocuRandomGitHubSecret_") as temp:
            path = Path(temp) / "github.dat"
            with (
                mock.patch(
                    "docurandom.private_github._protect_bytes",
                    side_effect=lambda value: b"protected:" + value[::-1],
                ),
                mock.patch(
                    "docurandom.private_github._unprotect_bytes",
                    side_effect=lambda value: value.removeprefix(b"protected:")[::-1],
                ),
            ):
                save_private_github_token(token, path)
                self.assertNotIn(token.encode("ascii"), path.read_bytes())
                self.assertEqual(load_private_github_token(path), token)
                clear_private_github_token(path)
                self.assertFalse(path.exists())

    def test_private_request_rejects_non_github_hosts(self) -> None:
        token = "github_pat_private_donut_read_only_123456789"
        with self.assertRaises(PrivateGitHubError):
            private_github_request("https://example.test/releases", token)

    def test_private_request_adds_authorization_only_for_github_api(self) -> None:
        token = "github_pat_private_donut_read_only_123456789"
        request = private_github_request(
            "https://api.github.com/repos/JustHari01/donutbrowser-local/releases",
            token,
        )
        self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")
        self.assertEqual(request.get_header("Cache-control"), "no-store")

    def test_rejects_whitespace_in_token(self) -> None:
        with self.assertRaises(PrivateGitHubError):
            private_github_request(
                "https://api.github.com/user",
                "github_pat_bad token_123456789",
            )


if __name__ == "__main__":
    unittest.main()
