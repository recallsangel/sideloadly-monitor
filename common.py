from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
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

def api(method: str, **params) -> dict | None:
    """呼叫 Telegram Bot API。dict/list 參數自動轉 JSON，None 直接略過。"""
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    payload = {}
    for key, value in params.items():
        if value is None:
            continue
        payload[key] = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
    try:
        with urllib.request.urlopen(
            url, data=urllib.parse.urlencode(payload).encode(), timeout=15
        ) as resp:
            return json.loads(resp.read())
    except OSError as exc:
        print(f"{method} 失敗: {exc}", file=sys.stderr, flush=True)
        return None


def send_message(text: str, chat_id: str | None = None, reply_markup=None):
    """送純文字。過長會依行界切段，按鈕只掛在最後一段。"""
    chunks = _chunk(text, config.MESSAGE_CHUNK_LIMIT)
    for index, chunk in enumerate(chunks):
        api(
            "sendMessage",
            chat_id=chat_id or config.CHAT_ID,
            text=chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def send_report(text: str, chat_id: str | None = None, reply_markup=None):
    """送報表。包成 <pre> 讓 Telegram 用等寬字，欄位才對得齊。"""
    # 先切段再包標籤，否則長訊息會把 <pre> 切成兩半變成壞掉的 HTML。
    chunks = _chunk(text, config.REPORT_CHUNK_LIMIT)
    for index, chunk in enumerate(chunks):
        api(
            "sendMessage",
            chat_id=chat_id or config.CHAT_ID,
            text=f"<pre>{html.escape(chunk)}</pre>",
            parse_mode="HTML",
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def answer_callback(callback_id: str, text: str | None = None):
    """按鈕按下後一定要回應，否則 Telegram 會一直轉圈。"""
    api("answerCallbackQuery", callback_query_id=callback_id, text=text)


def _chunk(text: str, limit: int) -> list[str]:
    """依行界切成不超過 Telegram 上限的段落。"""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
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


def display_width(text: str) -> int:
    """中文字在等寬字型佔兩格，用字元數 padding 會對不齊。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


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
    apple_id: str | None
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
        """通知用的完整說法。"""
        secs = self.seconds_to_expiry
        if secs is None:
            return "無刷新紀錄"
        if secs <= 0:
            return f"已過期 {human_delta(secs)}"
        return f"{human_delta(secs)}後過期"

    def expiry_short(self) -> str:
        """表格用的短版，欄位標題已經說明是到期倒數。"""
        secs = self.seconds_to_expiry
        if secs is None:
            return "無紀錄"
        if secs <= 0:
            return f"已過期 {human_delta(secs)}"
        return f"剩 {human_delta(secs)}"


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

    def seen_label(self) -> str:
        return "從未連線" if self.last_seen is None else f"{self.seen_text()}連線"


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
        apple_id=row["apple_id"] or None,
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
                   i.version, i.apple_id, i.last_updated, i.known_ttl, i.refresh_at_hours,
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


@dataclass
class AccountQuota:
    apple_id: str
    remaining: int
    nearest_ttl: datetime | None


def fetch_account_quotas() -> dict[str, AccountQuota]:
    """讀 sideloadly 自己維護的 account-appids.json，key 是 apple_id。

    這個檔案跟 installations.db 一樣是 sideloadly 的內部狀態、沒有公開格式，
    所以任何欄位缺漏或格式不對都當作「讀不到」處理，不讓這個輔助功能弄壞主流程。
    """
    if not config.ACCOUNT_APPIDS_PATH.exists():
        return {}
    try:
        raw = json.loads(config.ACCOUNT_APPIDS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    quotas = {}
    for apple_id, info in raw.items():
        if not isinstance(info, dict) or "Remaining" not in info:
            continue
        quotas[apple_id] = AccountQuota(
            apple_id=apple_id,
            remaining=info.get("Remaining", 0),
            nearest_ttl=parse_ts(info.get("NearestTtl")),
        )
    return quotas


def suggest_alternate_account(
    current_apple_id: str | None, quotas: dict[str, AccountQuota]
) -> AccountQuota | None:
    """目前這個帳號額度用完時，挑一個額度還沒用完、剩最多的其他已綁定帳號。

    只挑本地資料看得到的線索（App ID 週配額），不代表一定能解決那次失敗——
    Sideloadly 沒公開失敗原因的分類，這只是提示，不是診斷。
    """
    candidates = [
        q for aid, q in quotas.items() if aid != current_apple_id and q.remaining > 0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.remaining)


# ------------------------------------------------------------------- 報表

# 問題區塊由重到輕排序。
SEVERITY_ORDER = ["🔴 已過期", "❌ 刷新失敗", "⚠ 逾期未刷新", "📵 裝置離線"]


def build_status_report() -> str:
    installs = fetch_installs()
    if not installs:
        return "沒有任何裝置資料。"

    devices = {d.udid: d for d in fetch_devices()}
    by_device: dict[str, list[Install]] = {}
    for inst in installs:
        by_device.setdefault(inst.device_udid, []).append(inst)

    # 先挑出問題，健康時整份報表就只有一行標題加表格。
    problems: dict[str, list[str]] = {}
    markers: dict[str, str] = {}

    for inst in installs:
        if inst.expired:
            problems.setdefault(SEVERITY_ORDER[0], []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "🔴"
        elif inst.overdue:
            problems.setdefault(SEVERITY_ORDER[2], []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "⚠"
        if inst.failing:
            problems.setdefault(SEVERITY_ORDER[1], []).append(
                f"{inst.label}：{inst.last_error or '未知錯誤'}"
                f"（{inst.failures_count} 次）"
            )
            markers[inst.id] = "❌"

    for udid in by_device:
        device = devices.get(udid)
        # 只回報有 app 在跑的裝置，閒置裝置離線不算問題。
        if device and device.offline:
            problems.setdefault(SEVERITY_ORDER[3], []).append(
                f"{device.name}：最後連線 {device.seen_text()}"
            )

    lines = []
    if problems:
        count = sum(len(v) for v in problems.values())
        severe = SEVERITY_ORDER[0] in problems or SEVERITY_ORDER[1] in problems
        lines.append(f"{'🔴' if severe else '⚠'} {count} 個問題")
        lines.append("")
        for heading in SEVERITY_ORDER:
            items = problems.get(heading)
            if not items:
                continue
            lines.append(heading)
            lines.extend(f"  {item}" for item in items)
        lines.append("")
    else:
        lines.append("🟢 一切正常")
        lines.append("")

    latest = max((i.last_updated for i in installs if i.last_updated), default=None)
    summary = f"{len(installs)} 個 app · {len(by_device)} 台裝置"
    if latest:
        summary += (
            "　最近刷新 "
            + human_delta((datetime.now(timezone.utc) - latest).total_seconds())
            + "前"
        )
    lines.append(summary)
    lines.append("")

    # 名稱欄寬度取所有裝置名與縮排後 app 名的最大值，整份報表共用同一欄。
    name_width = 2 + max(
        max(display_width(g[0].device_name) for g in by_device.values()),
        max(2 + display_width(i.app_name) for i in installs),
    )
    # 問題標記統一貼在倒數欄之後，讓每行的標記垂直對齊。
    marker_col = name_width + 2 + max(
        display_width(i.expiry_short()) for i in installs
    )

    for udid, group in by_device.items():
        device = devices.get(udid)
        header = pad(group[0].device_name, name_width)
        if device:
            header += device.seen_label()
            if device.offline:
                header += "  📵"
        lines.append(header.rstrip())
        if device and device.last_error:
            lines.append(f"    裝置錯誤：{device.last_error}")

        for inst in sorted(group, key=lambda i: i.seconds_to_expiry or -1e9):
            row = "  " + pad(inst.app_name, name_width - 2) + inst.expiry_short()
            marker = markers.get(inst.id)
            if marker:
                row = pad(row, marker_col) + marker
            lines.append(row.rstrip())
        lines.append("")

    mute = mute_remaining()
    if mute:
        lines.append(f"🔇 靜音中，剩 {human_delta(mute.total_seconds())}")

    return "\n".join(lines).rstrip()


def build_device_report() -> str:
    devices = fetch_devices()
    if not devices:
        return "沒有任何裝置資料。"

    offline = [d for d in devices if d.offline]
    lines = [
        f"⚠ {len(offline)} 台離線 / 共 {len(devices)} 台"
        if offline
        else f"🟢 {len(devices)} 台裝置全部在線",
        "",
    ]

    name_width = 2 + max(display_width(d.name) for d in devices)
    for device in devices:
        row = pad(device.name, name_width) + device.seen_label()
        if device.offline:
            row += "  📵"
        lines.append(row)
        if device.last_error:
            lines.append(
                f"  {' ' * name_width}錯誤：{device.last_error}"
                f"（{device.failures_count} 次）"
            )
    return "\n".join(lines)


def build_account_report() -> str:
    quotas = fetch_account_quotas()
    if not quotas:
        return "讀不到帳號額度資料（account-appids.json 不存在或格式看不懂）。"

    accounts = sorted(quotas.values(), key=lambda q: q.remaining)
    low = [q for q in accounts if q.remaining <= config.LOW_QUOTA_THRESHOLD]

    lines = [
        f"⚠ {len(low)} 個帳號額度偏低 / 共 {len(accounts)} 個"
        if low
        else f"🟢 {len(accounts)} 個帳號額度都還夠用",
        "",
    ]

    name_width = 2 + max(display_width(q.apple_id) for q in accounts)
    now = datetime.now(timezone.utc)
    for q in accounts:
        row = pad(q.apple_id, name_width) + f"剩 {q.remaining} / {config.WEEKLY_APPID_QUOTA}"
        if q.remaining <= 0:
            row += "  ❌"
        elif q.remaining <= config.LOW_QUOTA_THRESHOLD:
            row += "  ⚠"
        lines.append(row)

        if q.nearest_ttl is None:
            reset_text = "無資料"
        elif q.nearest_ttl <= now:
            reset_text = "應已釋放（下次使用時更新）"
        else:
            reset_text = f"{human_delta((q.nearest_ttl - now).total_seconds())}後"
        lines.append(f"  {' ' * name_width}下一個額度釋放：{reset_text}")

    lines.append("")
    lines.append("額度是這週能再註冊幾個 App ID，不是帳號本身壞了。")
    lines.append("重簽會不會用到新 App ID 沒有公開規則，額度用完時保守起見")
    lines.append("換去還有額度的帳號比較保險。")
    return "\n".join(lines).rstrip()


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
