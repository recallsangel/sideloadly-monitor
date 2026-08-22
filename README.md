# sideloadly-monitor

盯著 [Sideloadly](https://sideloadly.io/) 的側載 app 有沒有按時重簽，出事就用 Telegram 通知。

免費 Apple ID 簽的憑證只有 7 天，過期 app 就打不開。Sideloadly daemon 平常會自己
刷新，但它失敗時是安靜的——這個專案就是補上那層告警。

## 架構

三個 launchd job，共用同一份 `common.py`：

| Job | 頻率 | 做什麼 |
| --- | --- | --- |
| `com.patrickchen.sideloadly-monitor` | 每小時 | `monitor.py` 比對資料庫，把變化彙整成一則通知 |
| `com.patrickchen.sideloadly-bot` | 常駐 | `bot.py` 處理 Telegram 指令，兼任監控看門狗 |
| `com.patrickchen.sideloadly-daily-restart` | 每天 04:00 | `restart.py` 只在真有問題時重啟 daemon |

資料來源是 Sideloadly 自己的 sqlite（**唯讀開啟**，不寫入）：
`~/Library/Application Support/sideloadly/installations.db`

## 過期判定

不用寫死的天數，直接讀資料庫裡 Sideloadly 自己的設定：

- `known_ttl`（憑證有效天數，目前 7）→ `last_updated + known_ttl` 就是**過期時間**
- `refresh_at_hours`（該刷新的時數，目前 96）→ 超過就算**逾期**，另加
  `OVERDUE_GRACE_HOURS` 寬限，避免刷新稍慢就告警

欄位是 0/NULL 時才退回 `config.py` 的 `DEFAULT_*` 預設值。

## 告警項目

| 事件 | 觸發條件 |
| --- | --- |
| 刷新完成 | `last_updated` 變了 |
| 刷新失敗 | `failures_count` 增加，或 `last_error` 出現新內容 |
| 錯誤已解除 | 原本有錯，現在乾淨了 |
| 逾期未刷新 | 超過 `refresh_at_hours` + 寬限（每天最多提醒一次） |
| 已過期 | 超過 `known_ttl`（每天最多提醒一次） |
| 裝置離線 | `devices.last_seen` 超過 `DEVICE_OFFLINE_HOURS` |
| 裝置回線 | 離線後又出現 |
| 監控停擺 | `state.json` 的 `last_run` 超過 `HEARTBEAT_STALE_HOURS` 沒更新 |

同一輪的變化會**併成一則訊息**，不會每個 app 各發一次。

最後一項是 dead-man's switch：`monitor.py` 每次執行都會更新 `last_run`，`bot.py`
每輪 `getUpdates` 回來時檢查。沒有它，「一切正常」和「監控自己死了」長得一模一樣。

## Telegram 指令

```
/status         各 app 的過期倒數與錯誤
/devices        各裝置連線狀態
/log [n]        最近的異常紀錄（預設 15 筆）
/stats [天數]   刷新/失敗次數與平均間隔（預設 7 天）
/restart        重啟 daemon，需在 60 秒內傳 /confirm
/mute [小時]    暫停主動通知（預設 8 小時）
/unmute         解除靜音
/help
```

靜音只擋主動推送，指令回覆照常。靜音期間的事件**仍會寫進歷史**，解除後用
`/log` 補看。

## 終端機用法

```sh
./query.py            # 同 /status
./query.py devices    # 同 /devices
./query.py log
./query.py stats
./query.py daemon     # launchd 看到的 daemon state
./restart.py          # 只在有問題時重啟
./restart.py --force  # 無視判斷直接重啟
./test_monitor.py -v  # 跑測試（-v 印出實際訊息內容）
```

## 設定

`config.py` 上方是設定值，敏感資料走環境變數或 `secrets.local.json`（不進版控）：

```json
{ "bot_token": "...", "chat_id": "..." }
```

對應環境變數：`SIDELOADLY_MONITOR_BOT_TOKEN`、`SIDELOADLY_MONITOR_CHAT_ID`。

## 產生的檔案（都不進版控）

- `state.json` — 上輪快照 + `last_run` 心跳
- `events.db` — 事件歷史，`/log` 和 `/stats` 的來源
- `mute_until.txt` — 靜音到期時間，存在才算靜音
- `bot_offset.txt` — Telegram update offset

## 測試

`test_monitor.py` 會把資料庫複製到暫存目錄後改資料來製造各種狀況，並換掉
`send_message`，所以不會動到真實狀態、也不會發訊息。
