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

另外會讀 `account-appids.json`（同目錄，Sideloadly 自己維護，一樣唯讀），
記著每個已登入 Apple ID 本週還剩多少 App ID 額度——免費帳號一週只能註冊 10 個，
額度用完是「這個 Apple ID 發的憑證出問題」最常見的原因之一。隨時可以用
`/accounts` 查目前每個帳號的額度，不用等刷新失敗才知道。

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
| usbmuxd 異常 | 連不上 usbmuxd，或連得上卻一台裝置都看不到（每天最多提醒一次） |
| usbmuxd 恢復 | 異常後又看得到裝置 |
| 監控停擺 | `state.json` 的 `last_run` 超過 `HEARTBEAT_STALE_HOURS` 沒更新 |

同一輪的變化會**併成一則訊息**，不會每個 app 各發一次。

「刷新失敗」如果剛好碰上失敗那個 app 綁定的 Apple ID 本週 App ID 額度是 0，
訊息會多一行提示，建議切去哪個還有額度的已綁定帳號（額度取自 `account-appids.json`，
挑剩最多的那個）。**這只是提示，不是診斷**——Sideloadly 沒公開失敗原因分類，
額度用完只是眾多可能原因之一，需要的話還是手動去 Sideloadly 換帳號重簽。

最後一項是 dead-man's switch：`monitor.py` 每次執行都會更新 `last_run`，`bot.py`
每輪 `getUpdates` 回來時檢查。沒有它，「一切正常」和「監控自己死了」長得一模一樣。

## Telegram 介面

不用記指令：`/menu` 會給一組按鈕，而且 bot 啟動時會呼叫 `setMyCommands`，
所以輸入框旁邊的指令選單也列得出來。

```
/menu           功能選單（按鈕）
/status         每台裝置的連線狀態、上面有哪些 app（誰簽的、剩多久）與問題
/accounts       各 Apple ID 本週 App ID 額度
/log [n]        最近的異常紀錄（預設 15 筆）
/stats [天數]   刷新/失敗次數與平均間隔（預設 7 天）
/restart        重啟 daemon（需確認）
/redeploy       為某個 app 重新部署（需確認；動作跟 /restart 一樣）
/forget         忘記某個裝置或 app，之後不再告警
/forgotten      查看忘記清單，可以復原
/mute [小時]    暫停主動通知（預設 8 小時）
/unmute         解除靜音
/help
```

按鈕和文字指令走同一套 `dispatch()`，行為一致。重啟一定要二次確認——
按鈕會跳出「確定重啟 / 取消」，文字指令則是 60 秒內回 `/confirm`。`/redeploy`
共用同一段確認流程。

靜音只擋主動推送，指令回覆照常。靜音期間的事件**仍會寫進歷史**，解除後用
`/log` 補看。

## 忘記某個裝置或 app（/forget）

有些裝置或 app 就是不會再處理了（裝置賣掉、app 不用了），但只要它還留在
Sideloadly 的資料庫裡，`monitor.py` 就會一直為它發過期/離線告警，`restart.py`
也會一直把它列進「需要重啟」的理由。`/forget`（`/forgotten` 查看與復原）讓
你把這些條目從報表和告警裡拿掉。

**這份清單完全是本專案自己的本機狀態（`ignored.json`），跟 `installations.db`
無關。** 那個資料庫是 Sideloadly 的內部狀態，這個專案從頭到尾唯讀開啟、不寫
入（見下方「架構」一節）——forget 因此不可能、也不會去改 Sideloadly 自己的
資料，只是在讀出來之後多一層本機過濾。忘記一整台裝置會連帶忘記它底下所有
app，不用逐一忘記；忘記單一 app 則不影響同裝置上的其他 app。`common.py` 的
`visible_installs()` / `visible_devices()` 是唯一的過濾點，`build_status_report`、
`monitor.py` 的偵測迴圈、`restart.py` 的自動重啟判斷三處都經過它——少了任何
一處，忘記的東西還是會用某種方式冒出來。

## 重新部署（/redeploy）——其實就是重啟

`/redeploy` 實際上做的事跟 `/restart` 完全一樣：`launchctl kickstart -k` 整顆
daemon。差別只在於它讓你先選一個 app，訊息和 `history.py` 的紀錄會點名「為了
哪個 app」——方便事後回頭看「這次重啟是為了處理誰」，而不是假裝這是一個只
影響單一 app、不會打斷其他裝置刷新的動作。兩者共用同一段 60 秒確認流程與同
一顆 `restart:go` 按鈕，避免同一段重啟邏輯被複製兩份。

### 為什麼沒有「只重簽這一個 app」，也沒有遠端部署（2026-09-06 實測）

**Sideloadly 的命令列介面只有兩個參數**，整個 binary 掃過沒有第三個：

| 給它的參數 | 實測行為 |
| --- | --- |
| `Sideloadly <IPA 路徑>`（加不加 `--silent` 一樣） | 正在跑的那個實例把該 IPA 載進 GUI 表單，視窗跳到前景 |
| `--enqueue <值>` | 值被吃掉，然後什麼都不做（DB 完全沒動） |
| 其他不認得的參數 | 被當成 IPA 路徑去開，開不到就印 `Could not open IPA` |

關鍵是**沒有任何參數可以指定裝置或 Apple ID**。所以「選一個 IPA、指定裝置、
指定 Apple ID、然後部署」不可能從命令列做完——缺的不是還沒找到方法，是
Sideloadly 沒開這個口，最後一哩一定得有人在 Mac 的 GUI 上完成。這也是為什麼
這個專案只做「監控 + 重啟」，不做部署。

`--enqueue` 的值幾乎可以確定是 `installations.enqueue_token`（欄位就叫這個名字，
平時是空的，由 Sideloadly 自己在需要時產生）。隨便餵一個值它會安靜忽略，而我們
造不出有效的 token；唯一的繞法是自己往 `installations.db` 寫入，那正是這個專案
從第一天就拒絕做的事（見下方架構一節）。

實驗方法附記，之後要重驗才不用重新摸索：**第二次啟動 Sideloadly 不會真的開第
二個實例**——它綁 localhost:28811 失敗後，會把命令列參數轉發給正在跑的那個實
例然後自己結束（本地印 `Raised the running instance`，主實例印 `Got raise
request <參數>`）。所以要看到參數被怎麼處理，得先關掉 GUI、改從終端機啟動來接
它的 stdout。裸的 `--enqueue`（後面不接值）會讓行程 panic，這也是它確實是個參數
的證據。實測全程只餵不存在的路徑和假 IPA，資料庫與 App ID 額度都沒有變動。

## 報表排版

報表包在 `<pre>` 裡送出，Telegram 才會用等寬字把欄位對齊；padding 用
`display_width()` 計算，中文字算兩格，否則會歪。

**一份報表講完三件事**：哪裡有問題、每台裝置的連線狀態、每台裝置上有哪些
app（哪個 Apple ID 簽的、還剩多久）。裝置連線狀態原本是獨立的 `/devices`，
但它講的東西 `/status` 本來就有——分成兩個指令只是逼人自己在腦裡把兩張表
拼起來。**沒有 app 的裝置也會列出來**（標成「（沒有 app）」），那是 `/devices`
併進來之後唯一還看得到閒置裝置的地方。

問題排在最前面，健康時就只有一行結論加表格：

```
🟢 一切正常

4 台裝置・6 個 app　最近刷新 3.7 小時前

裝置 A   18 分鐘前連線
  App 1  剩 6.8 天  alice...@icloud.com
  App 2  剩 6.8 天  bobby...@gmail.com

裝置 D   57 分鐘前連線
  （沒有 app）
```

Apple ID 只留本地端前幾個字（`common.short_apple_id`，長度是
`config.APPLE_ID_LOCAL_WIDTH`）：一行要同時塞下 app 名、倒數跟帳號，而
`<pre>` 不換行，太長只會變成要橫向捲，等於把對齊的意義整個抵銷。網域整個
留著，因為 gmail/icloud 本身就是一種辨識。

有狀況時先給摘要，表格內對應的那幾行右側也會標記：

```
🔴 4 個問題

🔴 已過期
  裝置 B - App 1：已過期 2.0 天
❌ 刷新失敗
  裝置 A - App 3：anisette server unreachable（3 次）
⚠ 逾期未刷新
  裝置 A - App 2：剩 2.0 天
📵 裝置離線
  裝置 C：最後連線 3.0 天前

...

裝置 A   19 分鐘前連線
  App 2  剩 2.0 天      alice...@icloud.com  ⚠
  App 1  剩 6.8 天      alice...@icloud.com
  App 3  剩 6.8 天      bobby...@gmail.com   ❌
```

每台裝置內部依剩餘時間排序，快過期的自動浮到上面。**只有「有 app 在跑」的
裝置離線才會進問題區**——閒置裝置離線不算問題，但它仍然會出現在下面的表格裡。

## 終端機用法

```sh
./query.py            # 同 /status
./query.py log
./query.py stats
./query.py forgotten  # 同 /forgotten
./query.py daemon     # launchd 看到的 daemon state
./restart.py          # 只在有問題時重啟
./restart.py --force  # 無視判斷直接重啟
./test_monitor.py -v  # 跑測試（-v 印出實際訊息內容）
```

## 需求

- macOS（要有 Sideloadly.app 及其 `installations.db`，排程也是用 launchd）
- Python 3.9+，全部用標準庫，不用另外 `pip install`

## 安裝與部署

```sh
git clone https://github.com/recallsangel/sideloadly-monitor.git
cd sideloadly-monitor
```

1. 建立 Telegram bot（找 [@BotFather](https://t.me/BotFather) 拿 token），並取得要通知的
   `chat_id`。
2. 設定憑證，二選一：
   - 環境變數 `SIDELOADLY_MONITOR_BOT_TOKEN`、`SIDELOADLY_MONITOR_CHAT_ID`
   - 或在專案根目錄放 `secrets.local.json`（見下方「設定」，這個檔不會進版控）
3. 把 `launchd/` 底下三個範本複製到 `~/Library/LaunchAgents/`，改掉裡面的
   `com.example.*` label 和 `/path/to/sideloadly-monitor` 路徑，然後載入：

   ```sh
   cp launchd/*.plist ~/Library/LaunchAgents/
   # 編輯剛複製的檔案，換成實際路徑與 label
   launchctl load ~/Library/LaunchAgents/com.example.sideloadly-monitor.plist
   launchctl load ~/Library/LaunchAgents/com.example.sideloadly-bot.plist
   launchctl load ~/Library/LaunchAgents/com.example.sideloadly-daily-restart.plist
   ```

4. 傳 `/status` 給 bot 確認有回應。
5. （選用）想用 `/usbmuxd` 的話，加一條 NOPASSWD 規則，見下方「重啟 usbmuxd」。

`restart.py` 預設用 `launchctl kickstart` 重啟 label 為 `io.sideloadly.daemon` 的
Sideloadly daemon（`config.py` 的 `RESTART_LABEL`），如果你的環境 label 不同要一併改。

## 重啟 usbmuxd

`usbmuxd` 是 macOS 負責探索 iOS 裝置的系統服務，Sideloadly 找不找得到裝置，最底層
完全取決於它。它偶爾會卡死，症狀很好認：

- 裝置明明都在同一個 Wi-Fi 上，Sideloadly 卻一台都看不到
- `devices` 表的 `last_seen` 集體停在某個時間點不再前進
- **重啟 Sideloadly daemon 沒有用**——壞的不是 Sideloadly

`monitor.py` 每小時會順手問一次，發現異常就主動推播，並且把它排在問題區塊的**最
前面**——底下那串「裝置離線」很可能全是它的下游結果，先看到兇手才不會跑去修錯的
東西。`/status` 同理。

只有一台裝置在冊時不做「看不到任何裝置」的判定（`USBMUXD_MIN_DEVICES_FOR_ALERT`）：
單台裝置關機或帶出門是常態，全部一起不見才是這個服務的問題。

確認方法是直接問 usbmuxd 看得到幾台（`/usbmuxd` 的確認訊息裡也會先報一次）：

```sh
python3 -c "import common; print(common.usbmux_list_devices())"
```

連得上卻回 `[]`，就是卡死了。唯一的解法是把它砍掉讓 launchd 重新拉起來，這要
root；bot 是一般使用者的 LaunchAgent，沒有 tty 也無從輸入密碼，所以需要一條只
放行這一道指令的規則（參數寫死，不留萬用字元——放行的只有「砍掉名字剛好是
usbmuxd 的行程」，不是「以 root 執行任意 pkill」）：

```sh
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/pkill -x usbmuxd" \
  | sudo tee /etc/sudoers.d/sideloadly-monitor-usbmuxd
sudo chmod 440 /etc/sudoers.d/sideloadly-monitor-usbmuxd
```

裝好之後 `/usbmuxd` 就能用了。沒裝也不會怎樣，`/usbmuxd` 只會把該補的那行回給你，
不會有任何動作。

重啟後 Wi-Fi 裝置要等 Bonjour 重新探索完才會回來，比 USB 慢，所以指令會等到有裝置
出現才回報（最多約 20 秒）。要是等完還是 0 台，通常是裝置那邊沒開「在 Wi-Fi 上顯示
這部裝置」——那個開關只能用傳輸線接上後在 Finder 裡勾，光是連同一個 Wi-Fi 不夠。

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
- `ignored.json` — `/forget` 忘記的裝置/app 清單（見上方「忘記某個裝置或 app」）

## 測試

`test_monitor.py` 會把資料庫複製到暫存目錄後改資料來製造各種狀況，並換掉
`send_message`，所以不會動到真實狀態、也不會發訊息。

## 授權

[MIT License](LICENSE)
