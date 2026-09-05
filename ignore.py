"""本地的「忘記」清單：裝置或 app 進了這裡，報表和告警都會跳過它。

跟 installations.db 完全無關——那個檔案是 sideloadly 自己的內部狀態，這個專案
只唯讀開它（見 common.connect_readonly 的說明），forget 因此不可能寫回那邊，
只能是本專案自己記一份「哪些我不想再聽到」的清單，過濾在讀出來之後那一層。
裝置本身被忘記時，底下所有 app 也一併跳過，不必逐一忘記。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass
class IgnoredDevice:
    udid: str
    name: str
    since: str


@dataclass
class IgnoredInstall:
    device_udid: str
    device_name: str
    app_name: str
    since: str


def _load() -> dict:
    if not config.IGNORED_PATH.exists():
        return {"devices": [], "installs": []}
    try:
        data = json.loads(config.IGNORED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"devices": [], "installs": []}
    data.setdefault("devices", [])
    data.setdefault("installs", [])
    return data


def _save(data: dict):
    config.IGNORED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def list_ignored_devices() -> list[IgnoredDevice]:
    return [IgnoredDevice(**d) for d in _load()["devices"]]


def list_ignored_installs() -> list[IgnoredInstall]:
    return [IgnoredInstall(**i) for i in _load()["installs"]]


def ignored_keys() -> tuple[set[str], set[tuple[str, str]]]:
    """一次讀檔，回傳 (被忘記的裝置 udid 集合, 被忘記的 (device_udid, app_name)
    集合)。要在迴圈裡判斷一整批 install/device 的呼叫方（common.visible_installs
    / visible_devices）用這個，不要對每一筆都各呼叫一次 is_*_ignored——那樣是
    N 次重新讀檔加解析 JSON。"""
    data = _load()
    return (
        {d["udid"] for d in data["devices"]},
        {(i["device_udid"], i["app_name"]) for i in data["installs"]},
    )


def is_device_ignored(udid: str) -> bool:
    devices, _ = ignored_keys()
    return udid in devices


def is_install_ignored(device_udid: str, app_name: str) -> bool:
    """裝置整台被忘記時，底下的 app 一起算忘記，不用個別再忘記一次。"""
    devices, installs = ignored_keys()
    return device_udid in devices or (device_udid, app_name) in installs


def ignore_device(udid: str, name: str) -> bool:
    """回傳是否真的新增了；已經忘記過就回 False，不重複寫入。"""
    data = _load()
    if any(d["udid"] == udid for d in data["devices"]):
        return False
    data["devices"].append(
        {"udid": udid, "name": name, "since": datetime.now(timezone.utc).isoformat()}
    )
    _save(data)
    return True


def ignore_install(device_udid: str, device_name: str, app_name: str) -> bool:
    data = _load()
    if any(
        i["device_udid"] == device_udid and i["app_name"] == app_name
        for i in data["installs"]
    ):
        return False
    data["installs"].append(
        {
            "device_udid": device_udid,
            "device_name": device_name,
            "app_name": app_name,
            "since": datetime.now(timezone.utc).isoformat(),
        }
    )
    _save(data)
    return True


def unignore_device(udid: str) -> bool:
    data = _load()
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d["udid"] != udid]
    if len(data["devices"]) == before:
        return False
    _save(data)
    return True


def unignore_install(device_udid: str, app_name: str) -> bool:
    data = _load()
    before = len(data["installs"])
    data["installs"] = [
        i
        for i in data["installs"]
        if not (i["device_udid"] == device_udid and i["app_name"] == app_name)
    ]
    if len(data["installs"]) == before:
        return False
    _save(data)
    return True
