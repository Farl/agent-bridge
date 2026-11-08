# agent-bridge

讓同一台機器上**正在跑的** Claude Code session 與 Codex thread 能互相傳訊，兩邊都能主動發起。

它只負責傳話：叫醒對方、告訴對方要做什麼、去哪裡看，再把回覆帶回來。要交換的資料
放在各專案本來存放的地方。怎麼分工由 agent 自己決定，這裡不規定流程。

## 安裝

```bash
cd ~/Prototyper/agent-bridge
uv tool install -e .          # agent-bridge 指令會出現在 ~/.local/bin
agent-bridge skills link      # skill 連到 ~/.claude/skills 與 ~/.agents/skills
```

`-e` 是 editable 安裝：改了 repo 裡的程式，不必重裝就會生效。

## 用法

```bash
agent-bridge list
agent-bridge send codex:01a159cf "請幫我 review ~/Prototyper/foo 的 MR !12" --wait
agent-bridge read claude:a4ff6083 --final
```

agent 該怎麼用寫在 [`.claude/skills/agent-bridge/SKILL.md`](.claude/skills/agent-bridge/SKILL.md)，
通道的細節與升版後怎麼重新查證寫在 [`docs/protocol.md`](docs/protocol.md)。

## 開發

```bash
uv run pytest
```

測試用 tmp 目錄搭出假的 Claude 與 Codex 環境（真的 Unix socket、真的 sqlite），
不會碰到機器上正在跑的 session。所有路徑都能用環境變數覆寫，見 `src/agent_bridge/config.py`。
