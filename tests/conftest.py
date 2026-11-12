"""假的 Claude / Codex 世界：真的 Unix socket、真的 sqlite，但全在 tmp 底下。

schema 只建 agent-bridge 會讀的欄位；真實 schema 更寬，但多出來的欄位不影響查詢。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
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
                 codex_control_sock_override=short / "codex-ctl.sock",
                 poll_interval=0.01, wait_timeout=2, socket_timeout=2)
    w = World(cfg, short)
    daemon = FakeDaemon(cfg)
    yield w
    daemon.close()
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
            db.execute("""create table queued_items (id text, thread_id text,
                payload_json text, queue_order int, created_at_ms int)""")

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
            return db.execute("select id, thread_id from queued_items"
                              " order by queue_order").fetchall()

    def queue_raw(self, tid: str, text: str) -> str:
        """不經 agent-bridge、直接排進佇列（例如人在桌面版打的、別的工具送的）。"""
        return _enqueue(self.cfg.codex_queue_db, tid, text)

    def drain_queue(self) -> None:
        with sqlite3.connect(self.cfg.codex_queue_db) as db:
            db.execute("delete from queued_items")


def _enqueue(db_path: Path, tid: str, text: str) -> str:
    with sqlite3.connect(db_path) as db:
        n = db.execute("select coalesce(max(queue_order), 0) + 1 from queued_items").fetchone()[0]
        iid = f"item-{n}"
        payload = {"input": [{"type": "text", "text": text}]}
        db.execute("insert into queued_items values (?,?,?,?,?)",
                   (iid, tid, json.dumps(payload), n, 1_000 * n))
    return iid


def _fake_codex(tmp: Path, codex_home: Path) -> Path:
    """`codex queue` 的替身：把訊息寫進 queue_1.sqlite 並印出與真的一樣的格式。"""
    script = tmp / "fake-codex"
    script.write_text(f"""#!/usr/bin/env python3
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pathlib import Path
from conftest import _enqueue
args = sys.argv[1:]
thread = args[args.index("--thread") + 1]
message = args[args.index("--message") + 1]
iid = _enqueue(Path({str(codex_home / "queue_1.sqlite")!r}), thread, message)
open({str(tmp / "codex-last-message")!r}, "w").write(message)
print(f"Queued message {{iid}} for thread {{thread}}.")
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class FakeDaemon:
    """Codex app-server 控制 socket 的替身：Unix socket 上的 WebSocket＋JSON-RPC。

    只實作 agent-bridge 用到的方法，資料就是 World 的 queue_1.sqlite。
    沒宣告 experimentalApi 就拒絕 queue 方法，跟真的 daemon 一樣。
    """

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, cfg: Config):
        self.db_path = cfg.codex_queue_db
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(cfg.codex_control_sock))
        self.sock.listen(4)
        self.calls: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            head, buf = buf.split(b"\r\n\r\n", 1)
            key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n")
                   if l.lower().startswith(b"sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1(key + self._GUID.encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            experimental = False
            reader = _FrameReader(conn, buf)
            while (msg := reader.next()) is not None:
                method, rid = msg.get("method"), msg.get("id")
                if rid is None:
                    continue
                self.calls.append(method)
                if method == "initialize":
                    experimental = msg["params"]["capabilities"].get("experimentalApi", False)
                    reply = {"result": {}}
                elif not experimental:
                    reply = {"error": {"code": -32600, "message": "experimental required"}}
                else:
                    reply = {"result": self._handle(method, msg["params"])}
                # 先插一則通知，確認客戶端會略過它、只等自己的回應。
                _send_frame(conn, {"method": "thread/queue/changed", "params": {}})
                _send_frame(conn, {"jsonrpc": "2.0", "id": rid, **reply})

    def _handle(self, method, params):
        with sqlite3.connect(self.db_path) as db:
            if method == "thread/queue/list":
                rows = db.execute("select id, payload_json from queued_items where thread_id = ?"
                                  " order by queue_order", (params["threadId"],)).fetchall()
                return {"data": [{"id": i, "clientUserMessageId": i, **json.loads(p)}
                                 for i, p in rows], "nextCursor": None}
            if method == "thread/queue/delete":
                cur = db.execute("delete from queued_items where id = ? and thread_id = ?",
                                 (params["queuedSubmissionId"], params["threadId"]))
                return {"deleted": cur.rowcount > 0}
        raise AssertionError(f"unexpected {method}")


class _FrameReader:
    def __init__(self, conn, buf: bytes):
        self.conn, self.buf = conn, buf

    def _take(self, n):
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def next(self):
        try:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._take(n)))
        except (EOFError, OSError):
            return None
        if b0 & 0x0F == 0x8:
            return None
        return json.loads(data)


def _send_frame(conn, obj):
    data = json.dumps(obj).encode()
    n = len(data)
    size = bytes([n]) if n < 126 else (bytes([126]) + struct.pack(">H", n) if n < 1 << 16
                                       else bytes([127]) + struct.pack(">Q", n))
    conn.sendall(bytes([0x81]) + size + data)
