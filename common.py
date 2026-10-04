from __future__ import annotations

import html
import http.client
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
from pathlib import Path

import config
import forget

_STATE_RE = re.compile(r"^\s*state = (\S+)", re.MULTILINE)

# sideloadly 用 Go 的 zero time 當「沒有值」，不是 NULL。
ZERO_TS_PREFIX = "0001-01-01"


# ---------------------------------------------------------------- Telegram

def api(method: str, http_timeout: float = 15, **params) -> dict | None:
    """呼叫 Telegram Bot API。dict/list 參數自動轉 JSON，None 直接略過。

    http_timeout 是這次 HTTP 請求的逾時秒數，跟 Telegram 自己的 timeout 參數
    （getUpdates 的 long polling 秒數）是兩回事，所以不能叫 timeout。
    失敗只印到 stderr 並回 None。
    """
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
            url, data=urllib.parse.urlencode(payload).encode(), timeout=http_timeout
        ) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # OSError 涵蓋連線錯誤與 HTTP 錯誤碼；ValueError 是回應不是 JSON；
        # HTTPException（例如讀到一半斷線的 IncompleteRead）不是 OSError，要另外接。
        print(f"{method} 失敗: {exc}", file=sys.stderr, flush=True)
        return None


def send_message(text: str, reply_markup=None):
    """送純文字。過長會依行界切段，按鈕只掛在最後一段。"""
    _send_chunks(_chunk(text, config.MESSAGE_CHUNK_LIMIT), reply_markup)


def send_report(text: str, reply_markup=None):
    """送報表。包成 <pre> 讓 Telegram 用等寬字，欄位才對得齊。"""
    # 先切段再包標籤，否則長訊息會把 <pre> 切成兩半變成壞掉的 HTML。
    chunks = [
        f"<pre>{html.escape(chunk)}</pre>"
        for chunk in _chunk(text, config.REPORT_CHUNK_LIMIT)
    ]
    _send_chunks(chunks, reply_markup, parse_mode="HTML")


def _send_chunks(chunks: list[str], reply_markup, **params):
    for index, chunk in enumerate(chunks):
        api(
            "sendMessage",
            chat_id=config.CHAT_ID,
            text=chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
            **params,
        )


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


def notify(title: str, message: str):
    """主動推送。靜音期間會被丟掉（bot 回覆請直接用 send_message）。"""
    if mute_remaining() is not None:
        return
    send_message(f"{title}\n{message}")


# ------------------------------------------------------------------- 靜音

def mute_remaining() -> timedelta | None:
    """回傳剩餘靜音時間，未靜音則 None。"""
    if not config.MUTE_UNTIL_PATH.exists():
        return None
    until = parse_ts(config.MUTE_UNTIL_PATH.read_text().strip())
    if until is None:
        return None
    remaining = until - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        config.MUTE_UNTIL_PATH.unlink(missing_ok=True)
        return None
    return remaining


def set_mute(hours: float) -> datetime:
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    config.MUTE_UNTIL_PATH.write_text(until.isoformat())
    return until


def clear_mute():
    config.MUTE_UNTIL_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------- 時間處理

def parse_ts(value):
    # Sideloadly 的時間字串微秒位數不固定（例如 .13506 只有 5 位），
    # account-appids.json 則是 Z 結尾；Python 3.11 起的 fromisoformat 都吃得下。
    if not value or str(value).startswith(ZERO_TS_PREFIX):
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def age_hours(ts) -> float | None:
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


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


def ago(ts: datetime) -> str:
    """「3.2 小時前」這種說法。"""
    return f"{human_delta((datetime.now(timezone.utc) - ts).total_seconds())}前"


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
        """這一列上有錯誤紀錄。

        不等於「現在正在失敗」——Sideloadly 要到下一次刷新成功才會清掉它，所以
        旗標會一路亮到下一個刷新窗口。要問「現在還在失敗嗎」用 failing_now。
        告警與「錯誤已解除」的判定要的是這個原始事實，不是 failing_now。
        """
        return bool(self.last_error) or self.failures_count > 0

    @property
    def stale_failure(self) -> bool:
        """舊帳：旗標還亮著，但沒有證據顯示現在還在失敗。

        daemon 每分鐘 tick 一次，真的卡在重試的話 last_failure_at 會一直被推新；
        它不動，表示 daemon 根本沒在重試，重啟也不會讓它重試。
        """
        if not self.failing:
            return False
        # 失敗之後又刷新成功了。Sideloadly 這時本來就該清掉旗標，這裡不依賴它做到。
        if (
            self.last_updated
            and self.last_failure_at
            and self.last_updated > self.last_failure_at
        ):
            return True
        age = age_hours(self.last_failure_at)
        # 沒有時間戳就沒有「現在還在失敗」的證據，當舊帳處理。monitor 的告警與
        # /status 仍然看得到這個錯誤，只是不會拿它當重啟的理由。
        if age is None:
            return True
        return age > config.FAILURE_STALE_HOURS

    @property
    def failing_now(self) -> bool:
        """重啟 daemon 有機會幫上忙的那一種失敗：最近還在發生。"""
        return self.failing and not self.stale_failure

    @property
    def label(self) -> str:
        return f"{self.device_name} - {self.app_name}"

    def failure_text(self) -> str:
        """錯誤內容加上它有多舊。同一個旗標會亮到下一次刷新成功為止，少了時間
        就分不出「剛剛還在失敗」跟「兩天前的舊帳」。"""
        text = f"{self.last_error or '未知錯誤'}（{self.failures_count} 次"
        if self.last_failure_at:
            text += f"，{ago(self.last_failure_at)}"
        if self.stale_failure:
            text += "，等下次刷新才會清"
        return text + "）"

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

    @property
    def offline(self) -> bool:
        hours = age_hours(self.last_seen)
        return hours is None or hours > config.DEVICE_OFFLINE_HOURS

    def seen_text(self) -> str:
        return "從未連線" if self.last_seen is None else ago(self.last_seen)


def connect_readonly() -> sqlite3.Connection:
    """唯讀開 Sideloadly 的資料庫。那是它的內部狀態，這個專案只讀不寫——
    /forget 這類要「改」的東西，都只記在本專案自己的檔案裡。"""
    con = sqlite3.connect(f"file:{config.SIDELOADLY_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


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

    installs = []
    for row in rows:
        last_updated = parse_ts(row["last_updated"])
        ttl_days = row["known_ttl"] or config.DEFAULT_KNOWN_TTL_DAYS
        refresh_hours = row["refresh_at_hours"] or config.DEFAULT_REFRESH_AT_HOURS
        installs.append(
            Install(
                id=str(row["id"]),
                device_udid=row["device_udid"],
                device_name=row["device_name"] or row["device_udid"],
                app_name=row["app_name"],
                version=row["version"],
                apple_id=row["apple_id"] or None,
                last_updated=last_updated,
                expires_at=last_updated + timedelta(days=ttl_days) if last_updated else None,
                refresh_due_at=(
                    last_updated + timedelta(hours=refresh_hours) if last_updated else None
                ),
                last_error=row["last_error"] or None,
                failures_count=row["failures_count"] or 0,
                last_failure_at=parse_ts(row["last_failure_at"]),
            )
        )
    return installs


def fetch_devices() -> list[Device]:
    con = connect_readonly()
    try:
        rows = con.execute(
            "SELECT udid, name, last_seen, last_error FROM devices ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [
        Device(
            udid=row["udid"],
            name=row["name"] or row["udid"],
            last_seen=parse_ts(row["last_seen"]),
            last_error=row["last_error"] or None,
        )
        for row in rows
    ]


def visible_installs() -> list[Install]:
    """套用本地 /forget 清單。報表和 monitor/restart 的判斷一律要用這個，
    不要直接用 fetch_installs()，否則忘記的裝置/app 還是會觸發告警或自動重啟。"""
    forgotten_devices, forgotten_installs = forget.forgotten_keys()
    return [
        i
        for i in fetch_installs()
        if i.device_udid not in forgotten_devices
        and (i.device_udid, i.app_name) not in forgotten_installs
    ]


def visible_devices() -> list[Device]:
    forgotten_devices, _ = forget.forgotten_keys()
    return [d for d in fetch_devices() if d.udid not in forgotten_devices]


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
            remaining=info["Remaining"],
            nearest_ttl=parse_ts(info.get("NearestTtl")),
        )
    return quotas


# ------------------------------------------------------------------- 報表

# 問題區塊的標題，由重到輕排序。monitor 的通知用同一組標題，兩邊才不會各說各話。
EXPIRED_HEADING = "🔴 已過期"
FAILING_HEADING = "❌ 刷新失敗"
OVERDUE_HEADING = "⚠ 逾期未刷新"
OFFLINE_HEADING = "📵 裝置離線"
SEVERITY_ORDER = [EXPIRED_HEADING, FAILING_HEADING, OVERDUE_HEADING, OFFLINE_HEADING]


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
            problems.setdefault(EXPIRED_HEADING, []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "🔴"
        elif inst.overdue:
            problems.setdefault(OVERDUE_HEADING, []).append(
                f"{inst.label}：{inst.expiry_short()}"
            )
            markers[inst.id] = "⚠"
        if inst.failing:
            problems.setdefault(FAILING_HEADING, []).append(
                f"{inst.label}：{inst.failure_text()}"
            )
            markers[inst.id] = "❌"

    for udid in by_device:
        device = device_by_udid.get(udid)
        # 只有「有 app 在跑」的裝置離線才算問題，閒置裝置離線不算——但下面的
        # 表格仍會把它列出來，這是 /devices 併進來之後唯一還看得到它的地方。
        if device and device.offline:
            problems.setdefault(OFFLINE_HEADING, []).append(
                f"{device.name}：最後連線 {device.seen_text()}"
            )

    lines = []
    if problems:
        count = sum(len(v) for v in problems.values())
        severe = EXPIRED_HEADING in problems or FAILING_HEADING in problems
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
        summary += f"　最近刷新 {ago(latest)}"
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
        seen = f"{ago(device.last_seen)}連線" if device.last_seen else "從未連線"
        header = pad(device.name, name_width) + seen
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
            ),
            group,
        )

    mute = mute_remaining()
    if mute:
        lines.append(f"🔇 靜音中，剩 {human_delta(mute.total_seconds())}")

    return "\n".join(lines).rstrip()


def status_action_keyboard() -> dict | None:
    """有問題的 app 各配一顆按鈕，一鍵發動重新部署——動作其實是重啟整個
    daemon（見 bot.py 的 _start_restart_confirm），這裡只負責點名是哪個 app 促成的。"""
    # failing_now 不是 failing：舊帳按了也沒用，這顆按鈕做的是重啟整顆 daemon，
    # 而重啟既清不掉錯誤旗標也不會讓刷新提早。真的想手動重來一次仍然可以走
    # /redeploy，那支列的是全部的 app，不只有問題的那些。
    problems = [
        i for i in visible_installs() if i.expired or i.overdue or i.failing_now
    ]
    if not problems:
        return None
    return {
        "inline_keyboard": [
            [{"text": f"🔁 {p.label}", "callback_data": f"redeploy:{p.id}"}]
            for p in problems[: config.PICKER_MAX_BUTTONS]
        ]
    }


def build_forgotten_report() -> str:
    devices = forget.forgotten_devices()
    installs = forget.forgotten_installs()
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
        lines.extend(f"  · {i.label}" for i in installs)
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
        ["launchctl", "print", f"gui/{os.getuid()}/{config.DAEMON_LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = _STATE_RE.search(result.stdout)
    return match.group(1) if match else "unknown"


def perform_restart() -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{config.DAEMON_LABEL}"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "kickstart 失敗"

    for _ in range(config.RESTART_VERIFY_ATTEMPTS):
        time.sleep(config.RESTART_VERIFY_INTERVAL_SECONDS)
        if daemon_state() == "running":
            return True, f"已重啟 {config.DAEMON_LABEL}，daemon 回到 running"
    return False, (
        f"已送出重啟指令，但 daemon 沒回到 running（state={daemon_state()}）"
    )


def rotate_log(path: Path) -> str | None:
    """launchd 寫的日誌超過上限就留一份尾巴、然後原地清空。

    一定要「原地清空」而不是改名：launchd 開著這個檔的 fd，改名之後行程會
    繼續往改名後的那個 inode 寫，新建的檔案永遠是空的。清空可以，因為 launchd
    是用 O_APPEND 開的，下一次寫入會自己回到檔頭接上。

    沒超過上限或檔案不存在回 None，做了事才回一句話。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= config.LOG_MAX_BYTES:
        return None

    keep = path.with_name(path.name + ".1")
    try:
        with path.open("rb") as src:
            src.seek(max(0, size - config.LOG_KEEP_BYTES))
            tail = src.read()
        keep.write_bytes(tail)
        with path.open("r+b") as dst:
            dst.truncate(0)
    except OSError as exc:
        return f"{path.name} 輪替失敗: {exc}"
    return (
        f"{path.name} 已清空（{_size_text(size)}），"
        f"最後 {_size_text(len(tail))} 留在 {keep.name}"
    )


def _size_text(num_bytes: int) -> str:
    """檔案大小講成人話。用不到 1 MB 的門檻跑測試時，整數 MB 會顯示成 0。"""
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1048576:.0f} MB"
