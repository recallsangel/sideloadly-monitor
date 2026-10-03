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
class ForgottenDevice:
    udid: str
    name: str
    since: str


@dataclass
class ForgottenInstall:
    device_udid: str
    device_name: str
    app_name: str
    since: str

    @property
    def label(self) -> str:
        return f"{self.device_name} - {self.app_name}"


def _load() -> dict:
    try:
        data = json.loads(config.FORGOTTEN_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("devices", [])
    data.setdefault("installs", [])
    return data


def _save(data: dict):
    config.FORGOTTEN_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def forgotten_devices() -> list[ForgottenDevice]:
    return [ForgottenDevice(**d) for d in _load()["devices"]]


def forgotten_installs() -> list[ForgottenInstall]:
    return [ForgottenInstall(**i) for i in _load()["installs"]]


def forgotten_keys() -> tuple[set[str], set[tuple[str, str]]]:
    """一次讀檔，回傳 (被忘記的裝置 udid 集合, 被忘記的 (device_udid, app_name)
    集合)。common.visible_installs / visible_devices 用它整批過濾，不要對每一筆
    都各讀一次檔。"""
    data = _load()
    return (
        {d["udid"] for d in data["devices"]},
        {(i["device_udid"], i["app_name"]) for i in data["installs"]},
    )


def forget_device(udid: str, name: str) -> bool:
    """回傳是否真的新增了；已經忘記過就回 False，不重複寫入。"""
    data = _load()
    if any(d["udid"] == udid for d in data["devices"]):
        return False
    data["devices"].append(
        {"udid": udid, "name": name, "since": datetime.now(timezone.utc).isoformat()}
    )
    _save(data)
    return True


def forget_install(device_udid: str, device_name: str, app_name: str) -> bool:
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


def unforget_device(udid: str) -> bool:
    data = _load()
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d["udid"] != udid]
    if len(data["devices"]) == before:
        return False
    _save(data)
    return True


def unforget_install(device_udid: str, app_name: str) -> bool:
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
