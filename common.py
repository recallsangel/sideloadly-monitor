from __future__ import annotations

import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config

_TS_RE = re.compile(r"^(?P<base>.*)\.(?P<frac>\d+)(?P<tz>[+-]\d{2}:\d{2})?$")


def notify(title: str, message: str):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": config.CHAT_ID, "text": f"{title}\n{message}"}
    ).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except OSError:
        pass


def connect_readonly() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.SIDELOADLY_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch_installations() -> list[sqlite3.Row]:
    con = connect_readonly()
    try:
        return con.execute(
            """
            SELECT i.id, i.device_udid, d.name AS device_name, i.name AS app_name,
                   i.last_updated, i.last_error AS install_last_error,
                   i.failures_count AS install_failures_count,
                   d.last_seen, d.last_checked, d.last_error AS device_last_error,
                   d.failures_count AS device_failures_count
            FROM installations i
            LEFT JOIN devices d ON d.udid = i.device_udid
            WHERE i.deleted_at IS NULL OR i.deleted_at = '' OR i.deleted_at = '0001-01-01 00:00:00+00:00'
            ORDER BY d.name, i.name
            """
        ).fetchall()
    finally:
        con.close()


def parse_ts(value):
    if not value:
        return None
    # SQLite 省略微秒尾端的 0（例如 .13506 只有 5 位），但 Python 3.9 的
    # fromisoformat 只接受 3 或 6 位微秒，要先補零否則會解析失敗。
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
