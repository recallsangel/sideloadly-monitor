"""事件歷史。state.json 只存當下值，這裡留下時間序列，
才回答得出「這週失敗幾次」、「平均刷新間隔多久」。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import common
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    device  TEXT,
    app     TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""

# kind 一覽：refresh / failure / recovery / overdue / expired
#            device_offline / device_online / restart / monitor_stale

KIND_LABELS = {
    "refresh": "刷新完成",
    "failure": "刷新失敗",
    "recovery": "錯誤解除",
    "overdue": "逾期未刷新",
    "expired": "已過期",
    "device_offline": "裝置離線",
    "device_online": "裝置回線",
    "restart": "重啟 daemon",
    "monitor_stale": "監控停擺",
}


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.EVENTS_DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def record(kind: str, device: str | None = None, app: str | None = None,
           detail: str | None = None):
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO events (ts, kind, device, app, detail) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), kind, device, app, detail),
            )
    finally:
        con.close()


def recent(limit: int = 15, kinds: list[str] | None = None) -> list[sqlite3.Row]:
    con = _connect()
    try:
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            return con.execute(
                f"SELECT * FROM events WHERE kind IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        return con.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        con.close()


def build_log_report(limit: int = 15) -> str:
    rows = recent(limit, kinds=["failure", "recovery", "overdue", "expired",
                                "device_offline", "device_online", "restart",
                                "monitor_stale"])
    if not rows:
        return "沒有任何異常紀錄。"

    lines = [f"最近 {len(rows)} 筆異常："]
    for row in rows:
        ts = common.parse_ts(row["ts"])
        when = common.human_delta((datetime.now(timezone.utc) - ts).total_seconds()) + "前" if ts else "?"
        target = " / ".join(x for x in (row["device"], row["app"]) if x)
        label = KIND_LABELS.get(row["kind"], row["kind"])
        line = f"· {when} {label}"
        if target:
            line += f" — {target}"
        lines.append(line)
        if row["detail"]:
            lines.append(f"    {row['detail']}")
    return "\n".join(lines)


def build_stats_report(days: int = 7) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = _connect()
    try:
        counts = con.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind ORDER BY n DESC",
            (since,),
        ).fetchall()
        refreshes = con.execute(
            "SELECT ts, device, app FROM events WHERE kind = 'refresh' AND ts >= ? "
            "ORDER BY device, app, ts",
            (since,),
        ).fetchall()
    finally:
        con.close()

    if not counts:
        return f"最近 {days} 天沒有任何紀錄（監控可能剛啟用）。"

    lines = [f"最近 {days} 天統計："]
    for row in counts:
        label = KIND_LABELS.get(row["kind"], row["kind"])
        lines.append(f"  {label}: {row['n']} 次")

    # 每個 app 連續兩次刷新之間的平均間隔。
    gaps: dict[tuple[str, str], list[float]] = {}
    previous: dict[tuple[str, str], datetime] = {}
    for row in refreshes:
        key = (row["device"] or "?", row["app"] or "?")
        ts = common.parse_ts(row["ts"])
        if ts is None:
            continue
        if key in previous:
            gaps.setdefault(key, []).append((ts - previous[key]).total_seconds())
        previous[key] = ts

    if gaps:
        lines.append("")
        lines.append("平均刷新間隔：")
        for (device, app), values in sorted(gaps.items()):
            avg = sum(values) / len(values)
            lines.append(
                f"  {device} - {app}: {common.human_delta(avg)}（{len(values)} 次間隔）"
            )

    return "\n".join(lines)
