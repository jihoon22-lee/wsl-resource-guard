from __future__ import annotations

import base64
from dataclasses import dataclass
import re
from datetime import datetime
from email.message import EmailMessage
import html
import json
from pathlib import Path
import smtplib
import ssl
import subprocess
import urllib.error
import urllib.request

from .config import Settings


@dataclass(slots=True)
class NotificationResult:
    channel: str
    sent: bool
    detail: str
    # Enabled in config.toml but without credentials: nothing was attempted.
    skipped: bool = False


# Details of skipped results; also used to drop such entries saved by older versions.
SKIPPED_DETAILS = frozenset({"credentials not configured", "webhook not configured", "webhook URL not configured",
                             "no subscriptions", "cryptography unavailable"})


_CLIXML_STRING = re.compile(r'<S(?:\s+S="([^"]*)")?[^>]*>(.*?)</S>', re.S)
_CLIXML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _clixml_text(text: str) -> str:
    """Readable message from PowerShell CLIXML stderr.

    Prefers the Error stream over verbose/warning/progress strings, decodes
    the _xHHHH_ control-character escapes and XML entities, and collapses
    the resulting line breaks.
    """
    strings = _CLIXML_STRING.findall(text)
    errors = [body for stream, body in strings if stream.lower() == "error"]
    bodies = errors or [body for _, body in strings]
    joined = "".join(bodies)
    joined = _CLIXML_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), joined)
    return " ".join(html.unescape(joined).split())


class Notifier:
    def __init__(self, settings: Settings, shared_dir: Path | None = None):
        from .webpush import DEFAULT_SHARED
        self.settings = settings
        self.secrets = settings.load_secrets()
        self.shared_dir = shared_dir or DEFAULT_SHARED

    def channel_status(self) -> dict[str, str]:
        gmail_ready = all(self.secrets.get(key) for key in ("gmail_user", "gmail_app_password", "gmail_to"))
        discord_ready = bool(self.secrets.get("discord_webhook_url"))
        webhook_ready = bool(self.secrets.get("webhook_url"))

        def remote(enabled: bool, ready: bool) -> str:
            if not enabled:
                return "disabled"
            return "configured" if ready else "not configured"

        from .webpush import subscription_count
        push = ("disabled" if not self.settings.push_enabled
                else "configured" if subscription_count(self.shared_dir, self.settings.state_path)
                else "unsubscribed")
        return {
            "push": push,
            "windows_toast": "enabled" if self.settings.windows_toast_enabled else "disabled",
            "gmail": remote(self.settings.gmail_enabled, gmail_ready),
            "discord": remote(self.settings.discord_enabled, discord_ready),
            "webhook": remote(self.settings.webhook_enabled, webhook_ready),
        }

    def send(
        self,
        title: str,
        message: str,
        severity: str,
        channels: set[str] | None = None,
        html_message: str | None = None,
    ) -> list[NotificationResult]:
        wanted = channels or {"toast", "gmail", "discord", "webhook", "push"}
        results: list[NotificationResult] = []
        deliveries = (
            ("toast", self.settings.windows_toast_enabled, self._send_windows_toast, (title, message)),
            ("gmail", self.settings.gmail_enabled, self._send_gmail, (title, message, severity, html_message)),
            ("discord", self.settings.discord_enabled, self._send_discord, (title, message, severity)),
            ("webhook", self.settings.webhook_enabled, self._send_webhook, (title, message, severity)),
            ("push", self.settings.push_enabled, self._send_push, (title, message, severity)),
        )
        for channel, enabled, deliver, args in deliveries:
            if channel not in wanted or not enabled:
                continue
            try:
                results.append(deliver(*args))
            except Exception as exc:
                # Include message/request construction in the channel boundary.
                # Exception text can contain addresses or credentials. A broken
                # channel must not stop the next channel or sample persistence.
                results.append(NotificationResult(channel, False, type(exc).__name__))
        return results

    def _send_push(self, title: str, message: str, severity: str) -> NotificationResult:
        from . import webpush
        ready, _ = webpush.available()
        if not ready:
            return NotificationResult("push", False, "cryptography unavailable", skipped=True)
        body = " ".join(message.split())[:280]
        try:
            sent, attempted, detail = webpush.send_all(
                self.shared_dir, self.settings.state_path, None,
                {"title": title[:120], "body": body, "severity": severity,
                 "tag": "wrg-alert", "url": "/#overview"},
                urgency="high" if severity == "critical" else "normal")
        except (OSError, ValueError) as exc:
            return NotificationResult("push", False, f"{type(exc).__name__}: {exc}"[:200])
        if not attempted:
            return NotificationResult("push", False, "no subscriptions", skipped=True)
        return NotificationResult("push", sent > 0, f"{sent}/{attempted} sent · {detail}"[:300])

    def _send_windows_toast(self, title: str, message: str) -> NotificationResult:
        powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if not powershell.exists():
            return NotificationResult("windows_toast", False, "powershell.exe is unavailable")
        safe_title = html.escape(title[:120])
        safe_message = html.escape(message[:800])
        safe_app_id = self.settings.toast_app_id.replace("'", "''")
        script = f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{safe_title}</text><text>{safe_message}</text></binding></visual></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app_id}').Show($toast)
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            completed = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return NotificationResult("windows_toast", False, type(exc).__name__)
        if completed.returncode == 0:
            return NotificationResult("windows_toast", True, "sent")
        # stderr holds the failure reason. powershell.exe writes it as UTF-16
        # (BOM or NUL bytes) or in the console code page (cp949 on Korean
        # Windows); plain UTF-8 decoding is tried before cp949.
        detail = f"PowerShell exit {completed.returncode}"
        raw = completed.stderr or b""
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:64]:
            text = raw.decode("utf-16", errors="replace")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp949", errors="replace")
        # Errors may arrive wrapped in CLIXML; unwrap the message payloads,
        # otherwise keep the last non-empty line.
        if "CLIXML" in text:
            text = _clixml_text(text) or text
        else:
            text = text.splitlines()[-1].strip() if text.strip() else ""
        if text:
            detail += f": {text[:200]}"
        return NotificationResult("windows_toast", False, detail)

    def _send_gmail(
        self,
        title: str,
        message: str,
        severity: str,
        html_message: str | None = None,
    ) -> NotificationResult:
        user = self.secrets.get("gmail_user", "").strip()
        password = self.secrets.get("gmail_app_password", "").replace(" ", "")
        recipients = [item.strip() for item in self.secrets.get("gmail_to", "").split(",") if item.strip()]
        if not (user and password and recipients):
            return NotificationResult("gmail", False, "credentials not configured", skipped=True)
        email = EmailMessage()
        email["From"] = user
        email["To"] = ", ".join(recipients)
        email["Subject"] = f"[{severity.upper()}] {title}"
        email.set_content(message)
        if html_message:
            email.add_alternative(html_message, subtype="html")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
                smtp.login(user, password)
                smtp.send_message(email)
        except smtplib.SMTPAuthenticationError as exc:
            return NotificationResult("gmail", False, f"authentication failed ({exc.smtp_code})")
        except (OSError, smtplib.SMTPException) as exc:
            return NotificationResult("gmail", False, type(exc).__name__)
        return NotificationResult("gmail", True, "sent")

    def _send_discord(self, title: str, message: str, severity: str) -> NotificationResult:
        webhook = self.secrets.get("discord_webhook_url", "").strip()
        if not webhook:
            return NotificationResult("discord", False, "webhook not configured", skipped=True)
        if not webhook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            return NotificationResult("discord", False, "webhook URL is not a Discord webhook")
        colors = {"normal": 0x3BA55D, "warning": 0xFAA61A, "critical": 0xED4245, "recovery": 0x3BA55D}
        payload = {
            "username": "WSL Resource Guard",
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": title[:256],
                    "description": message[:4000],
                    "color": colors.get(severity, 0x5865F2),
                }
            ],
        }
        request = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "wsl-resource-guard/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            return NotificationResult("discord", False, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return NotificationResult("discord", False, type(exc).__name__)
        return NotificationResult("discord", 200 <= status < 300, f"HTTP {status}")

    def _send_webhook(self, title: str, message: str, severity: str) -> NotificationResult:
        url = self.secrets.get("webhook_url", "").strip()
        if not url:
            return NotificationResult("webhook", False, "webhook URL not configured", skipped=True)
        if not url.startswith(("http://", "https://")):
            return NotificationResult("webhook", False, "webhook URL must use http(s)")
        payload = {
            "source": "wsl-resource-guard",
            "severity": severity,
            "title": title[:256],
            "message": message[:4000],
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "wsl-resource-guard/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            return NotificationResult("webhook", False, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return NotificationResult("webhook", False, type(exc).__name__)
        return NotificationResult("webhook", 200 <= status < 300, f"HTTP {status}")
