#!/usr/bin/env python3
"""重啟 Sideloadly daemon。

每天 4am 由 launchd 呼叫，但只在真的有問題時才動手——無條件重啟會在
daemon 正在刷新時把它打斷。加 --force 可以無視判斷直接重啟。
"""
import sys

import common
import history


def restart_reasons() -> list[str]:
    reasons = []

    state = common.daemon_state()
    if state != "running":
        reasons.append(f"daemon 不在執行中（state={state}）")

    for inst in common.fetch_installs():
        if inst.expired:
            reasons.append(f"{inst.label} {inst.expiry_text()}")
        elif inst.overdue:
            reasons.append(f"{inst.label} 逾期未刷新（{inst.expiry_text()}）")
        if inst.failing:
            reasons.append(
                f"{inst.label} 有錯誤：{inst.last_error or '未知'} "
                f"(failures={inst.failures_count})"
            )

    return reasons


def main():
    force = "--force" in sys.argv[1:]
    reasons = restart_reasons()

    if not force and not reasons:
        print("一切正常，不重啟。")
        return

    if force:
        print("--force：略過判斷直接重啟。")
    else:
        print("需要重啟的原因：")
        for reason in reasons:
            print(f"  - {reason}")

    ok, message = common.perform_restart()
    print(message)

    detail = "; ".join(reasons) if reasons else "force"
    history.record("restart", detail=f"{'ok' if ok else 'fail'}: {message} | {detail}")

    body = message if force else message + "\n\n原因：\n" + "\n".join(
        f"· {r}" for r in reasons
    )
    if ok:
        common.notify("Sideloadly Daemon 已重啟", body)
    else:
        common.notify("Sideloadly Daemon 重啟失敗", body)


if __name__ == "__main__":
    main()
