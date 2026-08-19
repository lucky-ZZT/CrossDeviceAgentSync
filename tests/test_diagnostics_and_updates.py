import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import app_diagnostics
import app_release_checker
import app_updater
import upstream_update_checker


class DiagnosticsAndUpdatesTests(unittest.TestCase):
    def test_application_log_redacts_sensitive_fields_and_records_errors(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            diagnostics = app_diagnostics.AppDiagnostics("test-app", "1.0")
            diagnostics.event("scan", selected_count=2, token="secret-token", payload="private-body")
            diagnostics.error("failed", RuntimeError("boom"), "traceback-text", auth="private-auth")
            text = diagnostics.path.read_text(encoding="utf-8")
            diagnostics.close()

        self.assertIn("application_start", text)
        self.assertIn("traceback-text", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("private-body", text)
        self.assertNotIn("private-auth", text)

    def test_application_release_check_compares_versions_without_installing(self):
        release = {
            "tag_name": "v1.2.0",
            "name": "CrossDeviceAgentSync 1.2.0",
            "body": "Reviewed release notes",
            "html_url": "https://github.com/example/cdas/releases/tag/v1.2.0",
            "published_at": "2026-08-13T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "CrossDeviceAgentSync-v1.2.0.exe",
                "size": 1234,
                "browser_download_url": "https://example.invalid/app.exe",
            }],
        }
        with mock.patch.dict(os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project"}), mock.patch.object(
            app_release_checker, "_latest_release", return_value=release
        ) as request:
            result = app_release_checker.check_latest_release("1.0.2")

        request.assert_called_once_with("owner/project")
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "1.2.0")
        self.assertEqual(result["release_notes"], "Reviewed release notes")
        self.assertEqual(result["assets"][0]["name"], "CrossDeviceAgentSync-v1.2.0.exe")
        self.assertEqual(result["artifact"]["name"], "CrossDeviceAgentSync-v1.2.0.exe")

    def test_application_release_check_rejects_missing_repository(self):
        with mock.patch.dict(os.environ, {"CDAS_GITHUB_REPOSITORY": ""}):
            self.assertFalse(app_release_checker.is_configured())
            with self.assertRaisesRegex(ValueError, "仓库地址尚未配置"):
                app_release_checker.check_latest_release("1.0.2")

    def test_application_release_page_is_available_without_api_check(self):
        with mock.patch.dict(os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project"}):
            self.assertTrue(app_release_checker.is_configured())
            self.assertEqual(
                app_release_checker.release_page_url(),
                "https://github.com/owner/project/releases",
            )

    def test_application_release_check_uses_atom_feed_before_api(self):
        atom = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>tag:github.com,2008:Repository/1/v1.3.0</id>
            <updated>2026-08-13T00:00:00Z</updated>
            <link rel="alternate" href="https://github.com/owner/project/releases/tag/v1.3.0"/>
            <title>CrossDeviceAgentSync v1.3.0</title>
            <content type="html">&lt;h2&gt;Fixed&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;No API limit&lt;/li&gt;&lt;/ul&gt;</content>
          </entry>
        </feed>"""
        with mock.patch.dict(os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project"}), mock.patch.object(
            app_release_checker, "_request_text", return_value=atom
        ) as request, mock.patch.object(app_release_checker, "_request_json") as api:
            result = app_release_checker.check_latest_release("1.0.2")

        request.assert_called_once_with("https://github.com/owner/project/releases.atom")
        api.assert_not_called()
        self.assertEqual(result["latest_version"], "1.3.0")
        self.assertTrue(result["update_available"])
        self.assertFalse(result["assets_known"])
        self.assertIn("No API limit", result["release_notes"])

    def test_application_release_result_marks_a_newer_version_for_manual_download(self):
        release = {
            "tag_name": "v1.0.4",
            "name": "CrossDeviceAgentSync v1.0.4",
            "body": "Manual download only",
            "html_url": "https://github.com/owner/project/releases/tag/v1.0.4",
            "published_at": "2026-08-13T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [],
        }
        with mock.patch.dict(os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project"}), mock.patch.object(
            app_release_checker, "_latest_release", return_value=release
        ):
            result = app_release_checker.check_latest_release("1.0.3")

        self.assertTrue(result["update_available"])
        self.assertEqual(result["release_url"], release["html_url"])

    def test_application_release_check_uses_saved_result_when_network_fails(self):
        release = {
            "tag_name": "v1.0.4",
            "name": "CrossDeviceAgentSync v1.0.4",
            "body": "Cached release",
            "html_url": "https://github.com/owner/project/releases/tag/v1.0.4",
            "published_at": "2026-08-13T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [],
            "assets_known": False,
            "source": "atom",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project", "LOCALAPPDATA": temporary}
        ), mock.patch.object(
            app_release_checker, "_latest_release", side_effect=[release, RuntimeError("TLS connection reset")]
        ):
            first = app_release_checker.check_latest_release("1.0.3")
            cached = app_release_checker.check_latest_release("1.0.3")

        self.assertFalse(first["using_cached_release"])
        self.assertTrue(cached["using_cached_release"])
        self.assertEqual(cached["latest_version"], "1.0.4")
        self.assertIn("TLS", cached["network_error"])

    def test_application_updater_downloads_only_after_checksum_verification(self):
        payload = b"verified release executable"
        checksum = __import__("hashlib").sha256(payload).hexdigest().upper()
        release = {
            "artifact": {
                "name": "CrossDeviceAgentSync-v1.0.4.exe",
                "download_url": "https://example.invalid/app.exe",
                "checksum_url": "https://example.invalid/SHA256SUMS.txt",
            }
        }

        def download(url, destination):
            destination.write_bytes(
                f"{checksum} *CrossDeviceAgentSync-v1.0.4.exe\n".encode("ascii")
                if url.endswith("SHA256SUMS.txt") else payload
            )

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            app_updater, "_download", side_effect=download
        ):
            executable = app_updater.download_and_verify(release, Path(temporary))
            self.assertEqual(executable.read_bytes(), payload)
            self.assertEqual(app_updater.sha256_file(executable), checksum)

    def test_application_updater_rejects_a_bad_checksum_without_retaining_executable(self):
        release = {
            "artifact": {
                "name": "CrossDeviceAgentSync-v1.0.4.exe",
                "download_url": "https://example.invalid/app.exe",
                "checksum_url": "https://example.invalid/SHA256SUMS.txt",
            }
        }

        def download(url, destination):
            destination.write_bytes(
                b"0" * 64 + b" *CrossDeviceAgentSync-v1.0.4.exe\n"
                if url.endswith("SHA256SUMS.txt") else b"unexpected"
            )

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            app_updater, "_download", side_effect=download
        ):
            with self.assertRaisesRegex(ValueError, "校验失败"):
                app_updater.download_and_verify(release, Path(temporary))
            self.assertEqual(list(Path(temporary).glob("*.exe")), [])

    def test_application_updater_starts_a_separate_replacement_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "CrossDeviceAgentSync.exe"
            downloaded = root / "downloaded.exe"
            current.write_bytes(b"old")
            downloaded.write_bytes(b"new")
            with mock.patch.object(app_updater, "update_root", return_value=root), mock.patch.object(
                app_updater.subprocess, "Popen"
            ) as process:
                script = app_updater.schedule_replacement(current, downloaded, process_id=4321)

            self.assertTrue(script.is_file())
            contents = script.read_text(encoding="utf-8")
            self.assertIn("Move-Item -LiteralPath $DownloadedExe -Destination $CurrentExe -Force", contents)
            self.assertIn("Start-Process -FilePath $CurrentExe", contents)
            process.assert_called_once()

    def test_application_release_check_explains_when_atom_and_api_are_limited(self):
        error = urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"CDAS_GITHUB_REPOSITORY": "owner/project", "LOCALAPPDATA": temporary}
        ), mock.patch.object(
            app_release_checker, "_request_text", side_effect=urllib.error.URLError("atom unavailable")
        ), mock.patch.object(
            app_release_checker, "_request_json", side_effect=error
        ):
            with self.assertRaisesRegex(RuntimeError, "暂时无法完成更新检查"):
                app_release_checker.check_latest_release("1.0.2")

    def test_maintainer_update_check_creates_review_report_and_handoff_prompt(self):
        responses = {}
        for upstream in upstream_update_checker.UPSTREAMS:
            base = f"https://api.github.com/repos/{upstream['owner']}/{upstream['repo']}"
            responses[base] = {"default_branch": "main", "html_url": f"https://github.com/{upstream['owner']}/{upstream['repo']}"}
            responses[f"{base}/commits/main"] = {
                "sha": "f" * 40,
                "commit": {"committer": {"date": "2026-08-12T00:00:00Z"}},
            }
            responses[f"{base}/releases/latest"] = {"tag_name": "v9.0", "html_url": f"{base}/release"}
            responses[f"{base}/compare/{upstream['reviewed_commit']}...{'f' * 40}"] = {
                "status": "ahead",
                "ahead_by": 2,
                "files": [{"filename": "src/sqlite/session_restore.rs"}],
                "commits": [{"commit": {"message": "Improve SQLite session restore"}}],
            }

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ), mock.patch.object(
            upstream_update_checker,
            "_request_json",
            side_effect=lambda url, timeout=15: responses[url],
        ):
            report = upstream_update_checker.check_updates()
            prompt = upstream_update_checker.handoff_prompt(report)
            report_path = Path(report["latest_path"])

            self.assertTrue(report_path.is_file())
            stored = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["results"]), 2)
            self.assertEqual(stored["latest_path"], str(report_path))
            self.assertTrue(all(item["ahead_by"] == 2 for item in report["results"]))
            self.assertTrue(all("建议复核" in item["recommendation"] for item in report["results"]))
            self.assertIn(str(report_path), prompt)
            self.assertIn("一次性统一审查", prompt)
            self.assertIn("Codex Provider Sync", prompt)
            self.assertIn("Codex ReHome", prompt)
            self.assertIn("已审查基线", prompt)
            self.assertIn("只重新封装一次 EXE", prompt)
            self.assertIn("若不值得升级", prompt)
            self.assertIn("不要为了版本号而改代码或重打包", prompt)
            self.assertIn("不能只回复‘已更新’", prompt)
            self.assertIn("两个上游项目各自更新了什么", prompt)
            self.assertIn("哪些变化未采用及原因", prompt)
            self.assertIn("新 EXE 路径、版本号和 SHA-256", prompt)
            self.assertIn("未产生新 EXE", prompt)
            self.assertIn("reviewed_commit", prompt)
            self.assertIn("检查失败的项目不得推进基线", prompt)

            persisted_prompt = upstream_update_checker.handoff_prompt(stored)
            self.assertIn(str(report_path), persisted_prompt)

    def test_update_assessment_ignores_documentation_only_changes(self):
        recommendation, reasons = upstream_update_checker._assess(
            ["docs/readme.md"],
            [{"commit": {"message": "Update screenshots"}}],
        )
        self.assertEqual(recommendation, "暂不需要更新")
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()
