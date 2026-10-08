from pathlib import Path
import json
import unittest
from unittest.mock import MagicMock, patch

from wsl_resource_guard.config import Settings
from wsl_resource_guard.notifications import Notifier

_shared = None


def setUpModule() -> None:
    # Never read the real /var/lib/wrg-shared: once a phone is subscribed,
    # these tests would otherwise push to it.
    import tempfile
    from unittest.mock import patch as _patch
    global _shared
    _shared = tempfile.TemporaryDirectory()
    patcher = _patch("wsl_resource_guard.webpush.DEFAULT_SHARED", Path(_shared.name))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(_shared.cleanup)


class NotificationTests(unittest.TestCase):
    def test_construction_failures_do_not_skip_later_channels_or_expose_secrets(self):
        from wsl_resource_guard.notifications import NotificationResult
        notifier = Notifier(Settings(windows_toast_enabled=False, webhook_enabled=True))
        notifier.secrets = {
            "gmail_user": "sender@example.test\nprivate-marker",
            "gmail_to": "receiver@example.test", "gmail_app_password": "fixture",
            "webhook_url": "http://[broken-private-marker",
        }
        with patch.object(notifier, "_send_push", return_value=NotificationResult("push", True, "sent")):
            results = notifier.send("fixture", "body", "warning")
        by_channel = {r.channel: r for r in results}
        for channel in ("gmail", "webhook"):
            self.assertFalse(by_channel[channel].sent)
            self.assertFalse(by_channel[channel].skipped)
            self.assertNotIn("private-marker", by_channel[channel].detail)
        self.assertTrue(by_channel["push"].sent)

    def test_unexpected_channel_exception_isolated_but_shutdown_is_not(self):
        notifier = Notifier(Settings(windows_toast_enabled=False, push_enabled=False))
        notifier.secrets = {}
        for error in (UnicodeError("private-marker"), RuntimeError("private-marker")):
            with self.subTest(error=type(error).__name__), patch.object(notifier, "_send_gmail", side_effect=error):
                results = notifier.send("fixture", "body", "warning")
                self.assertEqual([r.channel for r in results], ["gmail", "discord"])
                self.assertFalse(results[0].sent)
                self.assertNotIn("private-marker", results[0].detail)
        with patch.object(notifier, "_send_gmail", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                notifier.send("fixture", "body", "warning")

    def test_gmail_contains_plain_text_and_html_alternatives(self) -> None:
        notifier = Notifier(Settings())
        notifier.secrets = {
            "gmail_user": "sender@example.com",
            "gmail_to": "receiver@example.com",
            "gmail_app_password": "test-password",
        }
        with patch("wsl_resource_guard.notifications.smtplib.SMTP_SSL") as smtp:
            result = notifier._send_gmail(
                "WSL report",
                "plain report",
                "warning",
                "<html><body>HTML report</body></html>",
            )
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertTrue(result.sent)
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content_type(), "text/plain")
        self.assertEqual(message.get_body(preferencelist=("html",)).get_content_type(), "text/html")

    def test_webhook_posts_json_payload(self) -> None:
        notifier = Notifier(Settings())
        notifier.secrets = {"webhook_url": "https://hooks.example.test/alert"}
        response = MagicMock()
        response.status = 204
        request_ctx = MagicMock()
        request_ctx.__enter__.return_value = response
        with patch("wsl_resource_guard.notifications.urllib.request.urlopen", return_value=request_ctx) as urlopen:
            result = notifier._send_webhook("WSL 자원", "가용 RAM 부족", "warning")
        self.assertTrue(result.sent)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["severity"], "warning")
        self.assertEqual(payload["title"], "WSL 자원")
        self.assertIn("가용 RAM 부족", payload["message"])

    def test_webhook_rejects_non_http_url(self) -> None:
        notifier = Notifier(Settings())
        notifier.secrets = {"webhook_url": "file:///etc/passwd"}
        result = notifier._send_webhook("t", "m", "warning")
        self.assertFalse(result.sent)
        self.assertIn("http", result.detail)

    def test_send_includes_webhook_only_when_enabled(self) -> None:
        settings = Settings(webhook_enabled=True, windows_toast_enabled=False,
                            gmail_enabled=False, discord_enabled=False, push_enabled=False)
        notifier = Notifier(settings)
        notifier.secrets = {"webhook_url": "https://hooks.example.test/alert"}
        response = MagicMock()
        response.status = 200
        request_ctx = MagicMock()
        request_ctx.__enter__.return_value = response
        with patch("wsl_resource_guard.notifications.urllib.request.urlopen", return_value=request_ctx):
            results = notifier.send("t", "m", "warning")
        self.assertEqual([result.channel for result in results], ["webhook"])

        disabled = Notifier(Settings(webhook_enabled=False, windows_toast_enabled=False,
                                     gmail_enabled=False, discord_enabled=False, push_enabled=False))
        disabled.secrets = {"webhook_url": "https://hooks.example.test/alert"}
        self.assertEqual(disabled.send("t", "m", "warning"), [])

    def test_windows_toast_failure_includes_stderr_detail(self) -> None:
        notifier = Notifier(Settings())
        failed = MagicMock()
        failed.returncode = 1
        failed.stderr = "경로가 없습니다".encode("cp949")
        with patch("wsl_resource_guard.notifications.Path") as path:
            path.return_value.exists.return_value = True
            with patch("wsl_resource_guard.notifications.subprocess.run",
                       return_value=failed) as run:
                result = notifier._send_windows_toast("t", "m")
        self.assertFalse(result.sent)
        self.assertIn("PowerShell exit 1", result.detail)
        self.assertIn("경로가 없습니다", result.detail)
        # stderr must actually reach the process, not DEVNULL.
        self.assertIs(run.call_args.kwargs["capture_output"], True)

    def test_windows_toast_unwraps_clixml_errors(self) -> None:
        notifier = Notifier(Settings())
        failed = MagicMock()
        failed.returncode = 1
        failed.stderr = ('#< CLIXML<Objs><S>앱 ID를 찾을 수 없습니다</S></Objs>'
                         ).encode("utf-8")
        with patch("wsl_resource_guard.notifications.Path") as path:
            path.return_value.exists.return_value = True
            with patch("wsl_resource_guard.notifications.subprocess.run",
                       return_value=failed):
                result = notifier._send_windows_toast("t", "m")
        self.assertIn("앱 ID를 찾을 수 없습니다", result.detail)
        self.assertNotIn("CLIXML", result.detail)

    def test_toast_failure_clixml_prefers_error_stream_and_decodes(self) -> None:
        notifier = Notifier(Settings())
        failed = MagicMock()
        failed.returncode = 1
        failed.stderr = (
            '#< CLIXML\r\n<Objs Version="1.1.0.1">'
            '<S S="verbose">loading modules_x000D__x000A_</S>'
            '<S S="Error">New-Object : Cannot find type &lt;Toast&gt;_x000D__x000A_</S>'
            '<S S="Error">At line:1 char:1_x000D__x000A_</S></Objs>'
        ).encode("utf-8")
        with patch("wsl_resource_guard.notifications.Path") as path:
            path.return_value.exists.return_value = True
            with patch("wsl_resource_guard.notifications.subprocess.run",
                       return_value=failed):
                result = notifier._send_windows_toast("t", "m")
        self.assertEqual(
            result.detail,
            "PowerShell exit 1: New-Object : Cannot find type <Toast> At line:1 char:1",
        )

    def test_channel_status_reports_webhook(self) -> None:
        notifier = Notifier(Settings(webhook_enabled=True))
        notifier.secrets = {"webhook_url": "https://hooks.example.test/alert"}
        self.assertEqual(notifier.channel_status()["webhook"], "configured")
        notifier.secrets = {}
        self.assertEqual(notifier.channel_status()["webhook"], "not configured")

    def test_disabled_channel_reports_disabled_even_with_credentials(self) -> None:
        notifier = Notifier(Settings(gmail_enabled=False))
        notifier.secrets = {
            "gmail_user": "sender@example.com",
            "gmail_to": "receiver@example.com",
            "gmail_app_password": "test-password",
        }
        self.assertEqual(notifier.channel_status()["gmail"], "disabled")


    def test_unconfigured_channels_are_skipped_not_failed(self) -> None:
        notifier = Notifier(Settings(windows_toast_enabled=False, webhook_enabled=True))
        notifier.secrets = {}
        results = notifier.send("t", "m", "warning")
        self.assertEqual([(r.channel, r.sent, r.skipped) for r in results],
                         [("gmail", False, True), ("discord", False, True), ("webhook", False, True),
                          ("push", False, True)])

    def test_misconfigured_webhook_is_still_a_failure(self) -> None:
        notifier = Notifier(Settings())
        notifier.secrets = {"discord_webhook_url": "https://example.test/not-discord"}
        result = notifier._send_discord("t", "m", "warning")
        self.assertFalse(result.sent)
        self.assertFalse(result.skipped)


if __name__ == "__main__":
    unittest.main()
