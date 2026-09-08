from __future__ import annotations

import html
import json
import os
import plistlib
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import ignore

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


def visible_installs() -> list[Install]:
    """套用本地 /forget 清單。報表和 monitor/restart 的判斷一律要用這個，
    不要直接用 fetch_installs()，否則忘記的裝置/app 還是會觸發告警或自動重啟。"""
    ignored_devices, ignored_installs = ignore.ignored_keys()
    return [
        i
        for i in fetch_installs()
        if i.device_udid not in ignored_devices
        and (i.device_udid, i.app_name) not in ignored_installs
    ]


def visible_devices() -> list[Device]:
    ignored_devices, _ = ignore.ignored_keys()
    return [d for d in fetch_devices() if d.udid not in ignored_devices]


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

SEV_USBMUXD = "🔌 usbmuxd 異常"
SEV_EXPIRED = "🔴 已過期"
SEV_FAILED = "❌ 刷新失敗"
SEV_OVERDUE = "⚠ 逾期未刷新"
SEV_DEVICE_OFFLINE = "📵 裝置離線"

# 問題區塊由重到輕排序。usbmuxd 擺第一：它一壞，下面的「裝置離線」全都是它的
# 下游結果，先看到它才不會跑去修錯的東西（重啟 daemon 對它完全沒用）。
SEVERITY_ORDER = [
    SEV_USBMUXD,
    SEV_EXPIRED,
    SEV_FAILED,
    SEV_OVERDUE,
    SEV_DEVICE_OFFLINE,
]


def short_apple_id(apple_id: str | None) -> str:
    """報表欄位用的短版 Apple ID：本地端截短，網域整個留著。

    一行要同時塞下 app 名、到期倒數跟帳號，而報表是包在 <pre> 裡送出的——
    Telegram 不會換行，只會變成要橫向捲，等於把表格對齊的意義整個抵銷。
    帳號通常前幾個字就分得出來，網域留著是因為 gmail/icloud 本身就是辨識點。"""
    if not apple_id:
        return ""
    local, _, domain = apple_id.partition("@")
    if len(local) > config.APPLE_ID_LOCAL_WIDTH:
        local = local[: config.APPLE_ID_LOCAL_WIDTH] + "..."
    return f"{local}@{domain}" if domain else local


def build_status_report() -> str:
    """一份報表講完三件事：哪裡有問題、每台裝置的連線狀態、每台裝置上有哪些
    app（誰簽的、還剩多久）。原本 /devices 是獨立一份，但它講的東西這裡本來
    就有——分成兩個指令只是逼人自己在腦裡把兩張表拼起來。"""
    installs = visible_installs()
    devices = visible_devices()
    if not installs and not devices:
        return "沒有任何裝置資料。"

    by_device: dict[str, list[Install]] = {}
    for inst in installs:
        by_device.setdefault(inst.device_udid, []).append(inst)
    device_by_udid = {d.udid: d for d in devices}

    # 先挑出問題，健康時整份報表就只有一行標題加表格。
    problems: dict[str, list[str]] = {}
    markers: dict[str, str] = {}

    for inst in installs:
        if inst.expired:
            problems.setdefault(SEV_EXPIRED, []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "🔴"
        elif inst.overdue:
            problems.setdefault(SEV_OVERDUE, []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "⚠"
        if inst.failing:
            problems.setdefault(SEV_FAILED, []).append(
                f"{inst.label}：{inst.last_error or '未知錯誤'}"
                f"（{inst.failures_count} 次）"
            )
            markers[inst.id] = "❌"

    for udid in by_device:
        device = device_by_udid.get(udid)
        # 只有「有 app 在跑」的裝置離線才算問題，閒置裝置離線不算——但下面的
        # 表格仍會把它列出來，這是 /devices 併進來之後唯一還看得到它的地方。
        if device and device.offline:
            problems.setdefault(SEV_DEVICE_OFFLINE, []).append(
                f"{device.name}：最後連線 {device.seen_text()}"
            )

    # 上面那串「裝置離線」有可能全都是 usbmuxd 卡死的下游結果。問一下它，是的話
    # 就把真正的兇手擺到最前面——否則看到滿螢幕裝置離線，只會跑去重啟 daemon，
    # 而那對這種狀況完全沒用。
    usbmux_issue, _ = usbmux_health(len(devices))
    if usbmux_issue:
        problems.setdefault(SEV_USBMUXD, []).append(usbmux_issue)
        problems[SEV_USBMUXD].append("重啟 daemon 沒用，要用 /usbmuxd 重啟它")

    lines = []
    if problems:
        count = sum(len(v) for v in problems.values())
        severe = any(h in problems for h in (SEV_USBMUXD, SEV_EXPIRED, SEV_FAILED))
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
    summary = f"{len(devices)} 台裝置・{len(installs)} 個 app"
    if latest:
        summary += (
            "　最近刷新 "
            + human_delta((datetime.now(timezone.utc) - latest).total_seconds())
            + "前"
        )
    lines.append(summary)
    lines.append("")

    # installs 指到的裝置在 devices 表裡查不到時（資料不一致），還是要列出來，
    # 否則這些 app 會整組從報表消失——那是最不該被安靜吃掉的一種狀況。
    orphan_udids = [udid for udid in by_device if udid not in device_by_udid]

    # 三個欄位共用同一組寬度，整份報表才對得齊：名稱、到期倒數、Apple ID。
    device_names = [d.name for d in devices] + [
        by_device[udid][0].device_name for udid in orphan_udids
    ]
    name_width = 2 + max(
        max((display_width(n) for n in device_names), default=0),
        max((2 + display_width(i.app_name) for i in installs), default=0),
    )
    expiry_width = 2 + max(
        (display_width(i.expiry_short()) for i in installs), default=0
    )
    account_width = 2 + max(
        (display_width(short_apple_id(i.apple_id)) for i in installs), default=0
    )

    def render_device(device: Device, group: list[Install]):
        header = pad(device.name, name_width) + device.seen_label()
        if device.offline:
            header += "  📵"
        lines.append(header.rstrip())
        if device.last_error:
            lines.append(f"    裝置錯誤：{device.last_error}")
        if not group:
            lines.append("  （沒有 app）")
        for inst in sorted(group, key=lambda i: i.seconds_to_expiry or -1e9):
            row = (
                "  "
                + pad(inst.app_name, name_width - 2)
                + pad(inst.expiry_short(), expiry_width)
                + pad(short_apple_id(inst.apple_id), account_width)
            )
            marker = markers.get(inst.id)
            if marker:
                row += marker
            lines.append(row.rstrip())
        lines.append("")

    for device in devices:
        render_device(device, by_device.get(device.udid, []))

    for udid in orphan_udids:
        group = by_device[udid]
        render_device(
            Device(
                udid=udid,
                name=group[0].device_name,
                last_seen=None,
                last_error="裝置不在 devices 表裡",
                failures_count=0,
            ),
            group,
        )

    mute = mute_remaining()
    if mute:
        lines.append(f"🔇 靜音中，剩 {human_delta(mute.total_seconds())}")

    return "\n".join(lines).rstrip()


def status_action_keyboard() -> dict | None:
    """有問題的 app 各配一顆按鈕，一鍵發動重新部署——動作其實是重啟整個
    daemon（見 bot.py 的 redeploy 說明），這裡只負責點名是哪個 app 促成的。"""
    problems = [i for i in visible_installs() if i.expired or i.overdue or i.failing]
    if not problems:
        return None
    return {
        "inline_keyboard": [
            [{"text": f"🔁 {p.label}", "callback_data": f"redeploy:{p.id}"}]
            for p in problems[: config.PICKER_MAX_BUTTONS]
        ]
    }


def build_ignored_report() -> str:
    devices = ignore.list_ignored_devices()
    installs = ignore.list_ignored_installs()
    if not devices and not installs:
        return "目前沒有忘記任何裝置或 app。"

    lines = [f"🙈 已忘記 {len(devices)} 台裝置、{len(installs)} 個 app："]
    if devices:
        lines.append("")
        lines.append("裝置（整台都不再告警）：")
        lines.extend(f"  · {d.name}" for d in devices)
    if installs:
        lines.append("")
        lines.append("app：")
        lines.extend(f"  · {i.device_name} - {i.app_name}" for i in installs)
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


# ---------------------------------------------------------------- usbmuxd

def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    """收滿 size 個 byte，對方先斷線就回 None。"""
    if size < 0:
        return None
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def usbmux_list_devices() -> list[dict] | None:
    """問 usbmuxd 現在看得到哪些 iOS 裝置。

    回 None 是「連不上或講不通」，回 [] 是「連得上，但它真的說一台都沒有」——
    後者正是卡死的樣子，兩者一定要分開。
    """
    request = plistlib.dumps(
        {
            "MessageType": "ListDevices",
            "ClientVersionString": config.USBMUXD_CLIENT_NAME,
            "ProgName": config.USBMUXD_CLIENT_NAME,
            # 低於 3 的話 usbmuxd 只會回 USB 裝置，Wi-Fi 的一律看不到。
            "kLibUSBMuxVersion": 3,
        }
    )
    # 表頭是 4 個小端 uint32：總長度、協定版本 1、訊息類型 8（plist）、tag。
    header = struct.pack("<IIII", 16 + len(request), 1, 8, 1)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(config.USBMUXD_TIMEOUT_SECONDS)
            sock.connect(config.USBMUXD_SOCKET)
            sock.sendall(header + request)
            reply_header = _recv_exactly(sock, 16)
            if reply_header is None:
                return None
            payload = _recv_exactly(sock, struct.unpack("<I", reply_header[:4])[0] - 16)
        if payload is None:
            return None
        return plistlib.loads(payload).get("DeviceList", [])
    except (OSError, ValueError, struct.error, plistlib.InvalidFileException):
        return None


def usbmux_pid() -> int | None:
    result = subprocess.run(
        ["pgrep", "-x", config.USBMUXD_PROCESS_NAME], capture_output=True, text=True
    )
    pids = result.stdout.split() if result.returncode == 0 else []
    return int(pids[0]) if pids else None


def describe_usbmux_devices(devices: list[dict]) -> str:
    """把 usbmuxd 回的裝置清單講成人話，UDID 盡量換成 Sideloadly 裡的名字。"""
    if not devices:
        return "目前看到 0 台裝置"
    try:
        names = {d.udid: d.name for d in fetch_devices()}
    except (sqlite3.Error, OSError):
        names = {}
    labels = []
    for device in devices:
        props = device.get("Properties", {})
        udid = props.get("SerialNumber") or ""
        kind = "USB" if props.get("ConnectionType") == "USB" else "Wi-Fi"
        labels.append(f"{names.get(udid) or udid[:8] or '未知裝置'}（{kind}）")
    return f"目前看到 {len(devices)} 台裝置：" + "、".join(labels)


def usbmux_health(known_device_count: int) -> tuple[str | None, list[dict] | None]:
    """usbmuxd 現在健不健康。回 (問題描述或 None, 它回報的裝置清單或 None)。

    只問一次 socket，讓 /status 和 monitor 共用同一份判斷——兩邊講的話不一致
    的話，人只會更困惑。
    """
    devices = usbmux_list_devices()
    if devices is None:
        return "連不上 usbmuxd（負責探索裝置的系統服務，可能沒在跑）", None
    if not devices and known_device_count >= config.USBMUXD_MIN_DEVICES_FOR_ALERT:
        return (
            f"usbmuxd 還活著，但一台裝置都看不到"
            f"（Sideloadly 在冊 {known_device_count} 台）",
            devices,
        )
    return None, devices


def perform_usbmuxd_restart() -> tuple[bool, str]:
    """砍掉 usbmuxd 讓 launchd 重新拉起來，再確認它真的開始回報裝置。

    只砍不重開是故意的：這個服務設了 KeepAlive，launchd 會自己補上來，這也是
    唯一不必知道它在 launchd 裡叫什麼名字的做法（macOS 26 已經沒有對應的
    plist 檔可以查了）。
    """
    old_pid = usbmux_pid()
    result = subprocess.run(
        ["sudo", "-n", "/usr/bin/pkill", "-x", config.USBMUXD_PROCESS_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # sudo 沒過（沒裝 NOPASSWD 規則）和 pkill 沒砍到東西都是 exit 1，只能靠
        # stderr 分辨：pkill 找不到行程時是安靜的，sudo 一定會抱怨。
        stderr = result.stderr.strip()
        if stderr:
            if "sudo" in stderr.lower() or "password" in stderr.lower():
                return False, (
                    "沒有免密碼 sudo 權限，不能重啟 usbmuxd。\n"
                    "請在 Mac 上執行一次（會問你的登入密碼）：\n\n"
                    f"echo '{config.SUDOERS_RULE}' | "
                    f"sudo tee {config.SUDOERS_PATH} && "
                    f"sudo chmod 440 {config.SUDOERS_PATH}"
                )
            return False, stderr
        if old_pid is not None:
            # 它明明還在跑，pkill 卻說沒砍到——這種矛盾不該默默往下走。
            return False, f"pkill 沒有砍到 usbmuxd（pid {old_pid}），狀態不明。"
        # usbmuxd 本來就沒在跑，繼續往下等 launchd 把它拉起來。

    restarted = None
    for _ in range(config.USBMUXD_VERIFY_ATTEMPTS):
        time.sleep(config.USBMUXD_VERIFY_INTERVAL_SECONDS)
        new_pid = usbmux_pid()
        if new_pid is None or new_pid == old_pid:
            continue  # launchd 還沒把它拉回來
        devices = usbmux_list_devices()
        if devices is None:
            continue  # 起來了但還沒開始接受連線
        restarted = (new_pid, devices)
        if devices:
            break  # 裝置已經回來了，不用再等滿

    if restarted is None:
        return False, (
            "已送出重啟指令，但 usbmuxd 沒有在時限內回到可以回答的狀態。"
        )

    new_pid, devices = restarted
    summary = f"usbmuxd 已重啟（pid {old_pid} → {new_pid}），{describe_usbmux_devices(devices)}"
    if not devices:
        summary += (
            "。\nWi-Fi 裝置要等 Bonjour 重新探索，再等一下用 /status 看看；"
            "還是 0 台的話，代表裝置那邊沒開「在 Wi-Fi 上顯示」，得先用線接一次。"
        )
    return True, summary
