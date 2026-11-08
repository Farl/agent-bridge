# 通訊協定與查證方法

agent-bridge 依賴的都是 Claude Code 與 Codex 的**內部**機制，不是公開 API。
這份文件記錄每一條通道是什麼、怎麼找到的、升版壞掉時怎麼重新查證。

查證時的版本：Claude Code 2.1.319、Codex CLI 0.162.1（daemon 0.167.0），2026-11-08，macOS。

## 外部 → Claude Code：跨 session inbox

Claude Code 的 SendMessage 工具（session 之間互傳）走的就是這條。

**發現**：每個活著的 session 在 `~/.claude/sessions/` 有兩個檔：

| 檔案 | 內容 |
|---|---|
| `<pid>.json` | `sessionId`、`name`、`cwd`、`status`、`messagingSocketPath`（例如 `/tmp/cc-socks/<pid>.sock`）、`peerProtocol`、`peerFeatures` |
| `<pid>.<hash>.key` | `{"peerToken": "<32 hex>", ...}`，權限 0600 |

pid 檔在 crash 時可能殘留，要用 `kill -0 <pid>` 驗活。`claude agents --json`
（官方指令）也會列出同一批 session，可拿來交叉檢查。

`status` 看過的值：`idle`、`busy`、`shell`（正在跑指令）。

**送**：連上 socket，一行一個 JSON：

```
{"type":"auth","token":"<peerToken>"}
{"type":"user","message":{"role":"user","content":"<文字>"}}
```

對方不回任何位元組。閒著就立刻開一輪，忙著就排隊。收到的訊息在對方那裡顯示成
「Another Claude session sent a message」。

**怎麼找到的**：把 `claude` 執行檔跑 `strings`，搜尋 `messagingSocketPath` 與
`[uds-messaging]`。它的 debug log 會印出注入範例，原文大致是
`{ echo '{"type":"auth","token":"'"$CLAUDE_CODE_MESSAGING_TOKEN"'"}'; echo '{"type":"user",...}'; } | socat - UNIX-CONNECT:<sock>`。
session 自己的 token 也放在環境變數 `CLAUDE_CODE_MESSAGING_TOKEN`。

**重新查證**：

```bash
strings -n 8 "$(readlink -f "$(which claude)")" | grep -F '[uds-messaging]' | head
```

**讀回覆**：對話紀錄 `~/.claude/projects/<cwd slug>/<sessionId>.jsonl`。
`type=assistant`、`isSidechain=false` 的 `message.content[].text` 是說出口的話；
`stop_reason=end_turn` 是那一輪的最後一段。

**自己是誰**：Claude Code 給 shell 的環境變數 `CLAUDE_CODE_SESSION_ID`。

## 外部 → Codex：daemon 佇列

**送**：官方 CLI `codex queue --thread <id> --message <text>`，輸出
`Queued message <item id> for thread <thread id>.`。訊息進 daemon 的佇列，thread
閒著就立刻處理，忙著就等這一輪結束。

**發現與讀**（sqlite，在 `$CODEX_HOME`，預設 `~/.codex`，一律唯讀開）：

| 檔案 | 表 | 用途 |
|---|---|---|
| `state_5.sqlite` | `threads` | `id`、`name`／`title`、`cwd`、`source`、`archived`、`updated_at_ms` |
| `thread_history_1.sqlite` | `thread_turns` | 每輪 `status`：`inProgress`、`completed`、`interrupted`、`failed` |
| 同上 | `thread_items` | `item_type=agentMessage` 的 `item_json.text`，`phase` 是 `commentary` 或 `final_answer` |
| `queue_1.sqlite` | `queued_items` | 還沒被取走的訊息（`id` 就是 queue 回的 item id） |

`source` 以 `{"subagent"` 開頭的是 subagent 或 guardian review，不是對話對象。

檔名帶版本號，升版時可能變成 `state_6.sqlite` 之類，要改 `config.py`。

**自己是誰**：Codex 給 shell 的環境變數 `CODEX_THREAD_ID`。

**重新查證**：

```bash
codex queue --help
sqlite3 -readonly ~/.codex/state_5.sqlite .tables
```

## 沒採用的方案

| 方案 | 為什麼不用 |
|---|---|
| `claude -p --resume <id>` | 對正在跑的 session 會另開一份副本，不是送進原本那個 |
| tmux `send-keys` | 只適用在 tmux 裡的 CLI session，桌面版沒有 terminal |
| Claude Code channels（MCP 推播） | session 啟動時要帶參數，已經在跑的 session 用不了 |
| Codex app-server JSON-RPC（WebSocket） | 能做，但 `codex queue` 已經包好，而且是官方 CLI |
