"""Claude Code 這一側：找 session、送訊息進它的 inbox、讀它的回覆。

**送訊息的通道**是 Claude Code 自己的跨 session inbox（它的 SendMessage 工具也走
這條）。每個活著的 session 會：

- 在 `~/.claude/sessions/<pid>.json` 登記自己，其中 `messagingSocketPath` 是一個
  Unix socket（例如 `/tmp/cc-socks/<pid>.sock`）。
- 在同目錄的 `<pid>.<hash>.key` 放 `{"peerToken": ...}`，權限 0600。

協定是一行一個 JSON：第一行 `{"type":"auth","token":<peerToken>}`，之後
`{"type":"user","message":{"role":"user","content":<文字>}}`。對方收下就排進自己的
佇列，閒著的話立刻開一輪，忙的話等這一輪結束。它不回任何位元組，所以送出即完成。
這個格式是 Claude Code 自己的 debug log 印出來的注入範例（見 docs/protocol.md），
不是公開 API：升版後若失效，先重看那段 log。

**讀回覆**靠對話紀錄 `~/.claude/projects/<slug>/<session id>.jsonl`。
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Iterable

from agent_bridge import model
from agent_bridge.config import Config

AUTH_FRAME = "auth"
USER_FRAME = "user"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我們的——不會發生在同一使用者，但不該被當成死掉
    return True


def _records(cfg: Config) -> Iterable[dict]:
    """註冊表裡**還活著**的 session。pid 檔會殘留（crash 時沒人清），所以要驗 pid。"""
    directory = cfg.claude_sessions_dir
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # 正在被寫到一半；下次再看
        pid = record.get("pid")
        if isinstance(pid, int) and record.get("sessionId") and _alive(pid):
            yield record


def _status(record: dict) -> str:
    """註冊表的 status 除了 idle/busy 還有 shell（正在跑指令）、waiting 等。
    對傳訊來說只有「現在閒著會立刻處理」與「要排隊」兩種，所以非 idle 都算忙。"""
    raw = record.get("status")
    if not raw:
        return model.UNKNOWN
    return model.IDLE if raw == "idle" else model.BUSY


def peers(cfg: Config) -> list[model.Peer]:
    found = []
    for r in _records(cfg):
        updated_ms = int(r.get("updatedAt") or r.get("startedAt") or 0)
        found.append(model.Peer(
            kind=model.CLAUDE, id=r["sessionId"], name=r.get("name") or "",
            cwd=r.get("cwd") or "", status=_status(r), updated_ms=updated_ms))
    return found


def _record_for(cfg: Config, session_id: str) -> dict:
    for r in _records(cfg):
        if r["sessionId"] == session_id:
            return r
    raise LookupError(f"Claude session {session_id} 沒有在跑（找不到活著的註冊）")


def _token(cfg: Config, pid: int) -> str | None:
    """inbox 的 token。找不到就回 None：某些平台的 inbox 不要求認證。"""
    for key in cfg.claude_sessions_dir.glob(f"{pid}.*.key"):
        try:
            return json.loads(key.read_text())["peerToken"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return None


def frames(token: str | None, text: str) -> bytes:
    lines = []
    if token is not None:
        lines.append({"type": AUTH_FRAME, "token": token})
    lines.append({"type": USER_FRAME,
                  "message": {"role": "user", "content": text}})
    return "".join(json.dumps(x, ensure_ascii=False) + "\n"
                   for x in lines).encode()


def send(cfg: Config, session_id: str, text: str) -> None:
    record = _record_for(cfg, session_id)
    path = record.get("messagingSocketPath")
    if not path:
        raise LookupError(f"Claude session {session_id} 沒有開 inbox socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(cfg.socket_timeout)
        sock.connect(path)
        sock.sendall(frames(_token(cfg, record["pid"]), text))
        # 關寫端讓對方讀到 EOF，確定整批都被讀完才離開。
        sock.shutdown(socket.SHUT_WR)
        try:
            sock.recv(1)
        except socket.timeout:
            pass


def transcript(cfg: Config, session_id: str) -> Path | None:
    matches = list(cfg.claude_projects_dir.glob(f"*/{session_id}.jsonl"))
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def _ms(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
               .timestamp() * 1000)


def replies(cfg: Config, session_id: str) -> list[model.Reply]:
    """主對話（不含 subagent 的 sidechain）裡 assistant 說出口的文字。"""
    path = transcript(cfg, session_id)
    if path is None:
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            message = entry.get("message") or {}
            text = "".join(c.get("text", "") for c in message.get("content") or []
                           if c.get("type") == "text").strip()
            if text:
                out.append(model.Reply(
                    text=text, at_ms=_ms(entry["timestamp"]),
                    final=message.get("stop_reason") == "end_turn"))
    return out


def status(cfg: Config, session_id: str) -> str:
    return _status(_record_for(cfg, session_id))
