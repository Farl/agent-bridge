"""假的 Claude / Codex 世界：真的 Unix socket、真的 sqlite，但全在 tmp 底下。

schema 只建 agent-bridge 會讀的欄位；真實 schema 更寬，但多出來的欄位不影響查詢。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from agent_bridge.config import Config

TOKEN = "0" * 32


class Inbox:
    """模擬 Claude session 的 inbox：收下一條連線的全部內容。"""

    def __init__(self, path: str):
        self.path = path
        self.received = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return  # 測試沒送東西給它就收工了
        with conn:
            while chunk := conn.recv(4096):
                self.received += chunk

    def lines(self) -> list[dict]:
        self.thread.join(timeout=5)
        return [json.loads(x) for x in self.received.decode().splitlines()]

    def close(self):
        self.sock.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    # Unix socket 路徑上限約 104 bytes，macOS 的 tmp_path 太長，另開短目錄。
    short = Path(tempfile.mkdtemp(prefix="ab-", dir="/tmp"))
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    (claude_home / "sessions").mkdir(parents=True)
    codex_home.mkdir()
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config(claude_home=claude_home, codex_home=codex_home,
                 codex_bin=str(_fake_codex(tmp_path, codex_home)),
                 poll_interval=0.01, wait_timeout=2, socket_timeout=2)
    w = World(cfg, short)
    yield w
    for inbox in w.inboxes:
        inbox.close()


class World:
    def __init__(self, cfg: Config, short: Path):
        self.cfg = cfg
        self.short = short
        self.inboxes: list[Inbox] = []
        self._init_codex()

    # --- Claude -----------------------------------------------------------
    def add_claude(self, sid: str, name: str, status: str = "idle",
                   pid: int | None = None) -> Inbox:
        pid = pid or os.getpid()
        inbox = Inbox(str(self.short / f"{sid[:6]}.sock"))
        self.inboxes.append(inbox)
        sessions = self.cfg.claude_sessions_dir
        (sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": sid, "cwd": "/w", "name": name,
            "status": status, "updatedAt": 1000,
            "messagingSocketPath": inbox.path}))
        (sessions / f"{pid}.abc.key").write_text(json.dumps({"peerToken": TOKEN}))
        return inbox

    def claude_says(self, sid: str, text: str, ts: str, end: bool = True,
                    sidechain: bool = False) -> None:
        path = self.cfg.claude_projects_dir / "-w" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "type": "assistant", "timestamp": ts, "isSidechain": sidechain,
                "message": {"role": "assistant",
                            "stop_reason": "end_turn" if end else "tool_use",
                            "content": [{"type": "text", "text": text}]}}) + "\n")

    # --- Codex ------------------------------------------------------------
    def _init_codex(self):
        with sqlite3.connect(self.cfg.codex_state_db) as db:
            db.execute("""create table threads (id text, name text, title text,
                cwd text, source text, archived int, updated_at_ms int)""")
        with sqlite3.connect(self.cfg.codex_history_db) as db:
            db.execute("""create table thread_turns (thread_id text, turn_id text,
                rollout_ordinal int, status text)""")
            db.execute("""create table thread_items (thread_id text, item_type text,
                item_json text, created_at_ms int, rollout_ordinal int)""")
        with sqlite3.connect(self.cfg.codex_queue_db) as db:
            db.execute("create table queued_items (id text, thread_id text)")

    def add_codex(self, tid: str, name: str, source: str = "vscode",
                  updated_ms: int = 2000, turn: str = "completed") -> None:
        with sqlite3.connect(self.cfg.codex_state_db) as db:
            db.execute("insert into threads values (?,?,?,?,?,0,?)",
                       (tid, name, "", "/c", source, updated_ms))
        with sqlite3.connect(self.cfg.codex_history_db) as db:
            db.execute("insert into thread_turns values (?,?,1,?)", (tid, "t1", turn))

    def codex_says(self, tid: str, text: str, at_ms: int, final: bool = True) -> None:
        item = {"type": "agentMessage", "text": text,
                "phase": "final_answer" if final else "commentary"}
        with sqlite3.connect(self.cfg.codex_history_db) as db:
            db.execute("insert into thread_items values (?,?,?,?,?)",
                       (tid, "agentMessage", json.dumps(item), at_ms, at_ms))

    def queued(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.cfg.codex_queue_db) as db:
            return db.execute("select id, thread_id from queued_items").fetchall()

    def drain_queue(self) -> None:
        with sqlite3.connect(self.cfg.codex_queue_db) as db:
            db.execute("delete from queued_items")


def _fake_codex(tmp: Path, codex_home: Path) -> Path:
    """`codex queue` 的替身：把訊息寫進 queue_1.sqlite 並印出與真的一樣的格式。"""
    script = tmp / "fake-codex"
    script.write_text(f"""#!/usr/bin/env python3
import sqlite3, sys, json
args = sys.argv[1:]
thread = args[args.index("--thread") + 1]
message = args[args.index("--message") + 1]
db = sqlite3.connect({str(codex_home / "queue_1.sqlite")!r})
db.execute("insert into queued_items values (?, ?)", ("item-1", thread))
db.commit()
open({str(tmp / "codex-last-message")!r}, "w").write(message)
print(f"Queued message item-1 for thread {{thread}}.")
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script
