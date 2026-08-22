from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config

_TS_RE = re.compile(r"^(?P<base>.*)\.(?P<frac>\d+)(?P<tz>[+-]\d{2}:\d{2})?$")
_STATE_RE = re.compile(r"^\s*state = (\S+)", re.MULTILINE)

# sideloadly 用 Go 的 zero time 當「沒有值」，不是 NULL。
ZERO_TS_PREFIX = "0001-01-01"


# ---------------------------------------------------------------- Telegram

def send_message(text: str, chat_id: str | None = None):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    for chunk in _chunk(text):
        data = urllib.parse.urlencode(
            {"chat_id": chat_id or config.CHAT_ID, "text": chunk}
        ).encode()
        try:
            urllib.request.urlopen(url, data=data, timeout=10)
        except OSError as exc:
            print(f"sendMessage 失敗: {exc}", file=sys.stderr, flush=True)


def _chunk(text: str) -> list[str]:
    """依行界切成不超過 Telegram 上限的段落。"""
    if len(text) <= config.MESSAGE_CHUNK_LIMIT:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > config.MESSAGE_CHUNK_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def notify(title: str, message: str, force: bool = False):
    """主動推送。靜音期間會被丟掉（bot 回覆請直接用 send_message）。"""
    if not force and mute_remaining() is not None:
        return
    send_message(f"{title}\n{message}")


# ------------------------------------------------------------------- 靜音

def mute_remaining() -> timedelta | None:
    """回傳剩餘靜音時間，未靜音則 None。"""
    if not config.MUTE_PATH.exists():
        return None
    until = parse_ts(config.MUTE_PATH.read_text().strip())
    if until is None:
        return None
    remaining = until - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        config.MUTE_PATH.unlink(missing_ok=True)
        return None
    return remaining


def set_mute(hours: float) -> datetime:
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    config.MUTE_PATH.write_text(until.isoformat())
    return until


def clear_mute():
    config.MUTE_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------- 時間處理

def parse_ts(value):
    if not value or str(value).startswith(ZERO_TS_PREFIX):
        return None
    # SQLite 省略微秒尾端的 0（例如 .13506 只有 5 位），舊版 Python 的
    # fromisoformat 只接受 3 或 6 位微秒，先補零以免解析失敗。
    match = _TS_RE.match(value)
    if match:
        frac = match.group("frac")[:6].ljust(6, "0")
        value = f"{match.group('base')}.{frac}{match.group('tz') or ''}"
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def age_days(ts) -> float | None:
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400


def age_hours(ts) -> float | None:
    days = age_days(ts)
    return None if days is None else days * 24


def human_delta(seconds: float) -> str:
    """把秒數講成人看得懂的長度，例如 3.2 小時 / 1.4 天。"""
    seconds = abs(seconds)
    if seconds < 60:
        return "不到 1 分鐘"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分鐘"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小時"
    return f"{seconds / 86400:.1f} 天"


# ------------------------------------------------------------------- 資料

@dataclass
class Install:
    id: str
    device_udid: str
    device_name: str
    app_name: str
    version: str | None
    last_updated: datetime | None
    expires_at: datetime | None
    refresh_due_at: datetime | None
    last_error: str | None
    failures_count: int
    last_failure_at: datetime | None

    @property
    def seconds_to_expiry(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds()

    @property
    def expired(self) -> bool:
        secs = self.seconds_to_expiry
        return secs is not None and secs <= 0

    @property
    def overdue(self) -> bool:
        """已超過 sideloadly 認定的刷新時間（含寬限）。"""
        if self.refresh_due_at is None:
            return self.last_updated is None
        deadline = self.refresh_due_at + timedelta(hours=config.OVERDUE_GRACE_HOURS)
        return datetime.now(timezone.utc) > deadline

    @property
    def failing(self) -> bool:
        return bool(self.last_error) or self.failures_count > 0

    @property
    def label(self) -> str:
        return f"{self.device_name} - {self.app_name}"

    def expiry_text(self) -> str:
        secs = self.seconds_to_expiry
        if secs is None:
            return "無刷新紀錄"
        if secs <= 0:
            return f"已過期 {human_delta(secs)}"
        return f"{human_delta(secs)}後過期"


@dataclass
class Device:
    udid: str
    name: str
    last_seen: datetime | None
    last_error: str | None
    failures_count: int

    @property
    def offline(self) -> bool:
        hours = age_hours(self.last_seen)
        return hours is None or hours > config.DEVICE_OFFLINE_HOURS

    def seen_text(self) -> str:
        if self.last_seen is None:
            return "從未連線"
        return f"{human_delta((datetime.now(timezone.utc) - self.last_seen).total_seconds())}前"


def connect_readonly() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.SIDELOADLY_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _to_install(row: sqlite3.Row) -> Install:
    last_updated = parse_ts(row["last_updated"])
    ttl_days = row["known_ttl"] or config.DEFAULT_KNOWN_TTL_DAYS
    refresh_hours = row["refresh_at_hours"] or config.DEFAULT_REFRESH_AT_HOURS
    return Install(
        id=str(row["id"]),
        device_udid=row["device_udid"],
        device_name=row["device_name"] or row["device_udid"],
        app_name=row["app_name"],
        version=row["version"],
        last_updated=last_updated,
        expires_at=last_updated + timedelta(days=ttl_days) if last_updated else None,
        refresh_due_at=last_updated + timedelta(hours=refresh_hours) if last_updated else None,
        last_error=row["last_error"] or None,
        failures_count=row["failures_count"] or 0,
        last_failure_at=parse_ts(row["last_failure_at"]),
    )


def fetch_installs() -> list[Install]:
    con = connect_readonly()
    try:
        rows = con.execute(
            """
            SELECT i.id, i.device_udid, d.name AS device_name, i.name AS app_name,
                   i.version, i.last_updated, i.known_ttl, i.refresh_at_hours,
                   i.last_error, i.failures_count, i.last_failure_at
            FROM installations i
            LEFT JOIN devices d ON d.udid = i.device_udid
            WHERE i.deleted_at IS NULL OR i.deleted_at = '' OR i.deleted_at = ?
            ORDER BY d.name, i.name
            """,
            (f"{ZERO_TS_PREFIX} 00:00:00+00:00",),
        ).fetchall()
    finally:
        con.close()
    return [_to_install(row) for row in rows]


def fetch_devices() -> list[Device]:
    con = connect_readonly()
    try:
        rows = con.execute(
            "SELECT udid, name, last_seen, last_error, failures_count FROM devices ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [
        Device(
            udid=row["udid"],
            name=row["name"] or row["udid"],
            last_seen=parse_ts(row["last_seen"]),
            last_error=row["last_error"] or None,
            failures_count=row["failures_count"] or 0,
        )
        for row in rows
    ]


# ------------------------------------------------------------------- 報表

def build_status_report() -> str:
    installs = fetch_installs()
    if not installs:
        return "沒有任何裝置資料。"

    devices = {d.udid: d for d in fetch_devices()}
    by_device: dict[str, list[Install]] = {}
    for inst in installs:
        by_device.setdefault(inst.device_udid, []).append(inst)

    lines = []
    for udid, group in by_device.items():
        device = devices.get(udid)
        lines.append(f"[{group[0].device_name}]")
        if device:
            offline_flag = "  ⚠ 離線" if device.offline else ""
            lines.append(f"  最後連線: {device.seen_text()}{offline_flag}")
            if device.last_error:
                lines.append(
                    f"  裝置錯誤: {device.last_error} (failures={device.failures_count})"
                )

        for inst in sorted(group, key=lambda i: i.seconds_to_expiry or -1):
            if inst.expired:
                icon = "🔴"
            elif inst.overdue:
                icon = "⚠"
            else:
                icon = "✅"
            lines.append(f"  {icon} {inst.app_name}: {inst.expiry_text()}")
            if inst.last_updated:
                lines.append(
                    f"     最後刷新 {human_delta((datetime.now(timezone.utc) - inst.last_updated).total_seconds())}前"
                )
            if inst.last_error:
                lines.append(
                    f"     錯誤: {inst.last_error} (failures={inst.failures_count})"
                )
        lines.append("")

    mute = mute_remaining()
    if mute:
        lines.append(f"🔇 靜音中，剩 {human_delta(mute.total_seconds())}")

    return "\n".join(lines).rstrip()


def build_device_report() -> str:
    devices = fetch_devices()
    if not devices:
        return "沒有任何裝置資料。"
    lines = []
    for device in devices:
        icon = "⚠" if device.offline else "✅"
        lines.append(f"{icon} {device.name}: 最後連線 {device.seen_text()}")
        if device.last_error:
            lines.append(f"   錯誤: {device.last_error} (failures={device.failures_count})")
    return "\n".join(lines)


# ---------------------------------------------------------------- daemon

def daemon_state() -> str | None:
    """回傳 launchd 對 daemon 的 state 字串，服務不存在時回 None。"""
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{config.RESTART_LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = _STATE_RE.search(result.stdout)
    return match.group(1) if match else "unknown"


def perform_restart(verify: bool = True) -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{config.RESTART_LABEL}"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "kickstart 失敗"
    if not verify:
        return True, f"已重啟 {config.RESTART_LABEL}"

    for _ in range(config.RESTART_VERIFY_ATTEMPTS):
        time.sleep(config.RESTART_VERIFY_INTERVAL_SECONDS)
        if daemon_state() == "running":
            return True, f"已重啟 {config.RESTART_LABEL}，daemon 回到 running"
    return False, (
        f"已送出重啟指令，但 daemon 沒回到 running（state={daemon_state()}）"
    )
