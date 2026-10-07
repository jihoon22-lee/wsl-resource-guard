from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .safe_read import read_text


def default_config_path() -> Path:
    override = os.environ.get("WRG_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".config/wsl-resource-guard/config.toml"


def default_secrets_path() -> Path:
    override = os.environ.get("WRG_SECRETS")
    return Path(override).expanduser() if override else Path.home() / ".config/wsl-resource-guard/secrets.json"


QUIET_HOURS_RE = re.compile(r"^([01]?\d|2[0-3])(:[0-5]\d)?-([01]?\d|2[0-3])(:[0-5]\d)?$")

# key: (kind, minimum, maximum, 설명) — kind는 int/float/bool/quiet
CONFIG_RULES: dict[str, tuple[str, float, float, str]] = {
    "warning_available_gib": ("float", 0.1, 1024, "Warning: 가용 RAM 임계값 (GiB)"),
    "critical_available_gib": ("float", 0.1, 1024, "Critical: 가용 RAM 임계값 (GiB)"),
    "warning_psi_some_avg60": ("float", 0.1, 100, "Warning: PSI some 60초 평균 (%)"),
    "critical_psi_full_avg60": ("float", 0.1, 100, "Critical: PSI full 60초 평균 (%)"),
    "critical_psi_full_low_ram_avg60": ("float", 0.1, 100, "Critical: 낮은 RAM에서의 PSI full (%)"),
    "critical_psi_full_low_ram_available_gib": ("float", 0.1, 1024, "PSI 복합 판정의 낮은 RAM 기준 (GiB)"),
    "warning_swap_out_mib_per_minute": ("float", 1, 102400, "Warning: swap-out 속도 (MiB/분)"),
    "warning_swap_out_max_available_gib": ("float", 0.1, 1024, "swap-out 경보의 가용 RAM 상한 (GiB)"),
    "warning_disk_free_percent": ("float", 1, 95, "Warning: 디스크 여유 (%)"),
    "critical_disk_free_percent": ("float", 0.5, 95, "Critical: 디스크 여유 (%)"),
    "session_observe_rss_gib": ("float", 0.5, 1024, "세션 관찰 시작 RSS (GiB)"),
    "session_warning_rss_gib": ("float", 0.5, 1024, "Warning: 세션 RSS (GiB)"),
    "mcp_observe_rss_gib": ("float", 0.5, 1024, "MCP 관찰 시작 RSS (GiB)"),
    "mcp_warning_rss_gib": ("float", 0.5, 1024, "Warning: MCP RSS (GiB)"),
    "stale_session_hours": ("float", 1, 720, "오래된 세션 표시 기준 (시간)"),
    "interval_seconds": ("int", 5, 600, "샘플링 주기 (초)"),
    "history_interval_seconds": ("int", 10, 86400, "이력 기록 주기 (초)"),
    "retention_days": ("int", 1, 365, "이력 보존 일수"),
    "reminder_cooldown_seconds": ("int", 60, 604800, "Warning 재알림 간격 (초)"),
    "critical_reminder_seconds": ("int", 60, 86400, "Critical 재알림 간격 (초)"),
    "recovery_sustain_seconds": ("int", 10, 3600, "회복 판정 유지 시간 (초)"),
    "recovery_notifications": ("bool", 0, 0, "회복 알림 사용"),
    "disk_refresh_seconds": ("int", 15, 3600, "디스크 측정 주기 (초)"),
    "process_warning_sustain_seconds": ("int", 10, 3600, "세션/MCP 경보 지속 시간 (초)"),
    "warning_available_sustain_seconds": ("int", 5, 600, "가용 RAM 경보 지속 시간 (초)"),
    "warning_psi_sustain_seconds": ("int", 5, 600, "PSI 경보 지속 시간 (초)"),
    "critical_psi_sustain_seconds": ("int", 5, 600, "Critical PSI 지속 시간 (초)"),
    "warning_swap_out_sustain_seconds": ("int", 5, 600, "swap-out 경보 지속 시간 (초)"),
    "alert_quiet_hours": ("quiet", 0, 0, "Warning 재알림 억제 시간대 HH[:MM]-HH[:MM] (빈 값=해제)"),
    "email_heartbeat_enabled": ("bool", 0, 0, "정기 이메일 heartbeat 사용"),
    "email_heartbeat_minute": ("int", 0, 59, "heartbeat 발송 분"),
    "windows_toast_enabled": ("bool", 0, 0, "Windows 토스트 알림 사용"),
    "gmail_enabled": ("bool", 0, 0, "Gmail 알림 사용"),
    "discord_enabled": ("bool", 0, 0, "Discord 알림 사용"),
    "webhook_enabled": ("bool", 0, 0, "범용 webhook 알림 사용 (URL은 secrets.json의 webhook_url)"),
    "host_memory_refresh_seconds": ("int", 60, 3600, "호스트 vmmem 수집 주기 (초)"),
    "push_enabled": ("bool", 0, 0, "등록한 기기로 브라우저 푸시 알림"),
    "weekly_report_enabled": ("bool", 0, 0, "주간 리포트 발송 (Gmail·Discord·webhook)"),
    "weekly_report_weekday": ("int", 0, 6, "주간 리포트 요일 (0=월 … 6=일)"),
    "weekly_report_hour": ("int", 0, 23, "주간 리포트 발송 시각 (시)"),
}

# Keys the dashboard may change (through the guard daemon): numeric
# thresholds and intervals with fixed ranges. Channel switches and text
# values stay terminal-only so a web session cannot silence alerting.
WEB_EDITABLE_KINDS = ("int", "float")

# Settings view grouping: ordered (panel title, keys). Every CONFIG_RULES key
# must appear exactly once — test_config.py asserts the coverage.
CONFIG_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("메모리·PSI 경보", (
        "warning_available_gib",
        "critical_available_gib",
        "warning_psi_some_avg60",
        "critical_psi_full_avg60",
        "critical_psi_full_low_ram_avg60",
        "critical_psi_full_low_ram_available_gib",
        "warning_swap_out_mib_per_minute",
        "warning_swap_out_max_available_gib",
        "warning_available_sustain_seconds",
        "warning_psi_sustain_seconds",
        "critical_psi_sustain_seconds",
        "warning_swap_out_sustain_seconds",
    )),
    ("디스크", (
        "warning_disk_free_percent",
        "critical_disk_free_percent",
    )),
    ("세션·MCP", (
        "session_observe_rss_gib",
        "session_warning_rss_gib",
        "mcp_observe_rss_gib",
        "mcp_warning_rss_gib",
        "stale_session_hours",
        "process_warning_sustain_seconds",
    )),
    ("알림", (
        "reminder_cooldown_seconds",
        "critical_reminder_seconds",
        "recovery_sustain_seconds",
        "recovery_notifications",
        "alert_quiet_hours",
        "email_heartbeat_enabled",
        "email_heartbeat_minute",
        "windows_toast_enabled",
        "gmail_enabled",
        "discord_enabled",
        "webhook_enabled",
        "push_enabled",
        "weekly_report_enabled",
        "weekly_report_weekday",
        "weekly_report_hour",
    )),
    ("수집·보존", (
        "interval_seconds",
        "history_interval_seconds",
        "retention_days",
        "disk_refresh_seconds",
        "host_memory_refresh_seconds",
    )),
)

# 변경 후에도 왼쪽 값이 오른쪽보다 커야 하는 관계
CONFIG_RELATIONS = (
    ("warning_available_gib", "critical_available_gib"),
    ("warning_disk_free_percent", "critical_disk_free_percent"),
    ("session_warning_rss_gib", "session_observe_rss_gib"),
    ("mcp_warning_rss_gib", "mcp_observe_rss_gib"),
)

# Keys that `wrg config set` does not edit but a hand-written file may contain.
STRUCTURED_KEYS = {
    "project_roots": "path_list",
    "disk_drives": "drive_list",
    "wsl_vhd_path": "text",
    "toast_app_id": "text",
    "state_dir": "path",
    "email_heartbeat_interval_seconds": "legacy_int",
}


def parse_config_value(key: str, raw: str):
    """Parse a `wrg config set` argument (always a string) for a CONFIG_RULES key."""
    kind, low, high, _ = CONFIG_RULES[key]
    if kind == "bool":
        if raw.lower() in ("true", "on", "1", "yes"):
            return True
        if raw.lower() in ("false", "off", "0", "no"):
            return False
        raise ValueError("true/false 값이 필요합니다.")
    if kind == "quiet":
        if raw and not QUIET_HOURS_RE.fullmatch(raw):
            raise ValueError("HH[:MM]-HH[:MM] 형식이 필요합니다. 예: 23-08, 23:30-07:15")
        return raw
    try:
        value = int(raw) if kind == "int" else float(raw)
    except ValueError:
        raise ValueError("숫자 값이 필요합니다.") from None
    if not low <= value <= high:
        raise ValueError(f"{low:g}~{high:g} 범위가 필요합니다.")
    return value


def coerce_config_value(key: str, value: Any):
    """Validate a value already typed by TOML; raise ValueError with a short reason."""
    if key in CONFIG_RULES:
        kind, low, high, _ = CONFIG_RULES[key]
        if kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError("true/false 값이 필요합니다.")
        if kind == "quiet":
            if isinstance(value, str) and (not value or QUIET_HOURS_RE.fullmatch(value)):
                return value
            raise ValueError("HH[:MM]-HH[:MM] 형식의 문자열이 필요합니다.")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("숫자 값이 필요합니다.")
        # Check bounds before conversion: TOML integers may overflow a float.
        if not low <= value <= high:
            raise ValueError(f"{low:g}~{high:g} 범위가 필요합니다.")
        if kind == "int":
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("정수 값이 필요합니다.")
            return int(value)
        return float(value)
    kind = STRUCTURED_KEYS[key]
    if kind == "path_list":
        if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
            return value
        raise ValueError("비어 있지 않은 경로 문자열 목록이 필요합니다.")
    if kind == "drive_list":
        if isinstance(value, list) and all(
            isinstance(item, str) and re.fullmatch("[A-Za-z]", item) for item in value
        ):
            return [item.upper() for item in value]
        raise ValueError('드라이브 문자 목록이 필요합니다. 예: ["C", "D"]')
    if kind == "path":
        if isinstance(value, str) and value:
            return value
        raise ValueError("비어 있지 않은 경로 문자열이 필요합니다.")
    if kind == "legacy_int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("정수 값이 필요합니다.")
    if isinstance(value, str):
        return value
    raise ValueError("문자열 값이 필요합니다.")


def _toml_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def check_relations(settings: "Settings", key: str, value) -> None:
    """Raise ValueError when setting key=value breaks a CONFIG_RELATIONS pair."""
    for bigger, smaller in CONFIG_RELATIONS:
        if key not in (bigger, smaller):
            continue
        left = value if key == bigger else getattr(settings, bigger)
        right = value if key == smaller else getattr(settings, smaller)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and left <= right:
            raise ValueError(f"{bigger}({left:g})는 {smaller}({right:g})보다 커야 합니다.")


def write_config_value(path: Path, key: str, value) -> None:
    """Set one root-level key in config.toml, preserving the rest of the file.

    Replaces the first root-level `key =` line only. A regex substitution
    would also hit a same-named key inside a [table] or a multiline string,
    and a naive append lands inside the last table instead of the root scope.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key} = {_toml_literal(value)}"
    lines = text.splitlines(keepends=True)
    # TOML allows the same bare key to be written quoted: "key" or 'key'.
    key_line = re.compile(rf"""(?:{re.escape(key)}|"{re.escape(key)}"|'{re.escape(key)}')\s*=""")
    in_table = False
    replaced = False
    for index, existing in enumerate(lines):
        stripped = existing.lstrip()
        if not stripped.strip() or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_table = True
            continue
        if not in_table and key_line.match(stripped):
            lines[index] = line + "\n"
            replaced = True
            break
    if not replaced:
        insert_at = next(
            (i for i, existing in enumerate(lines) if existing.lstrip().startswith("[")),
            len(lines),
        )
        lines.insert(insert_at, line + "\n")
    new_text = "".join(lines)
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"설정 파일이 손상되어 있습니다: {exc}") from None
    if parsed.get(key) != value:
        raise ValueError(f"{key} 치환 결과를 확인할 수 없습니다. {path}를 직접 수정하세요.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(slots=True)
class Settings:
    interval_seconds: int = 15
    history_interval_seconds: int = 60
    retention_days: int = 14
    reminder_cooldown_seconds: int = 21600
    critical_reminder_seconds: int = 900
    # Warning reminder suppression window, e.g. "23-08" or "23:30-07:15".
    # State transitions and critical alerts are never suppressed.
    alert_quiet_hours: str = ""
    email_heartbeat_enabled: bool = False
    email_heartbeat_minute: int = 0
    # Retained for compatibility with existing configuration files. Heartbeats
    # are scheduled by email_heartbeat_minute instead of elapsed seconds.
    email_heartbeat_interval_seconds: int = 3600
    recovery_notifications: bool = True
    recovery_sustain_seconds: int = 120
    project_roots: list[str] = field(default_factory=lambda: ["~/projects"])

    warning_available_gib: float = 4.0
    critical_available_gib: float = 2.0
    warning_available_sustain_seconds: int = 30
    warning_psi_some_avg60: float = 2.0
    critical_psi_full_avg60: float = 20.0
    critical_psi_full_low_ram_avg60: float = 1.0
    critical_psi_full_low_ram_available_gib: float = 4.0
    warning_psi_sustain_seconds: int = 300
    critical_psi_sustain_seconds: int = 30
    warning_swap_out_mib_per_minute: float = 256.0
    warning_swap_out_max_available_gib: float = 6.0
    warning_swap_out_sustain_seconds: int = 30
    process_warning_sustain_seconds: int = 120
    session_observe_rss_gib: float = 4.0
    session_warning_rss_gib: float = 6.0
    mcp_observe_rss_gib: float = 3.0
    mcp_warning_rss_gib: float = 5.0
    stale_session_hours: float = 48.0

    disk_drives: list[str] = field(default_factory=lambda: ["C", "D", "E"])
    disk_refresh_seconds: int = 60
    host_memory_refresh_seconds: int = 300
    warning_disk_free_percent: float = 20.0
    critical_disk_free_percent: float = 10.0
    wsl_vhd_path: str = ""

    windows_toast_enabled: bool = True
    gmail_enabled: bool = True
    discord_enabled: bool = True
    webhook_enabled: bool = False
    push_enabled: bool = True
    weekly_report_enabled: bool = True
    weekly_report_weekday: int = 0
    weekly_report_hour: int = 9
    toast_app_id: str = "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"

    state_dir: str = "~/.local/state/wsl-resource-guard"
    config_path: Path = field(default_factory=default_config_path)
    secrets_path: Path = field(default_factory=default_secrets_path)
    # Why a file value was ignored; the default was kept in its place.
    load_warnings: list[str] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        config_path = (path or default_config_path()).expanduser()
        settings = cls(config_path=config_path, secrets_path=default_secrets_path())
        if not config_path.exists():
            return settings
        raw = tomllib.loads(read_text(config_path, max_bytes=1024 * 1024))
        defaults = cls()
        for key, value in raw.items():
            if key not in CONFIG_RULES and key not in STRUCTURED_KEYS:
                settings.load_warnings.append(f"{key}: 알 수 없는 설정이라 무시합니다.")
                continue
            try:
                setattr(settings, key, coerce_config_value(key, value))
            except ValueError as exc:
                settings.load_warnings.append(f"{key}: {exc} 기본값 {getattr(defaults, key)!r}을 사용합니다.")
        for bigger, smaller in CONFIG_RELATIONS:
            if getattr(settings, bigger) <= getattr(settings, smaller):
                for key in (bigger, smaller):
                    setattr(settings, key, getattr(defaults, key))
                settings.load_warnings.append(f"{bigger}는 {smaller}보다 커야 합니다. 두 값 모두 기본값을 사용합니다.")
        return settings

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()

    def load_secrets(self) -> dict[str, str]:
        path = self.secrets_path
        try:
            data: Any = json.loads(read_text(path, max_bytes=1024 * 1024))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if isinstance(value, (str, int, float))}


def load_daemon_settings(path: Path | None = None) -> Settings:
    """Settings for `wrg run`. Never raises: the guard keeps monitoring on defaults."""
    try:
        return Settings.load(path)
    except (OSError, ValueError) as exc:
        # Includes TOML syntax, UTF-8 decoding, and parser numeric limits.
        settings = Settings(config_path=(path or default_config_path()).expanduser())
        settings.load_warnings.append(f"설정 파일을 읽지 못해 모든 기본값을 사용합니다: {type(exc).__name__}: {exc}")
        return settings
