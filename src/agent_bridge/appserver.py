"""Codex app-server daemon 的最小 JSON-RPC 客戶端。

`codex queue` 只會加，要列出或撤回排隊訊息得走 daemon 的 experimental 方法
`thread/queue/list`、`thread/queue/delete`。改由 daemon 自己刪，它會在同一把鎖
裡判斷訊息是否已被取走，也會通知掛著的 UI；直接改 sqlite 做不到這兩件事。

控制 socket 講的是 Unix socket 上的 WebSocket（`codex app-server proxy` 只轉送
位元組，不替你做 WebSocket），所以這裡自己做握手與文字 frame。只用到
「送一則、等對應 id 的回應」，不處理分段 frame 以外的進階功能。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
from pathlib import Path

_TEXT, _CLOSE, _PING = 0x1, 0x8, 0x9
CLIENT_NAME = "agent-bridge"


class AppServerError(RuntimeError):
    pass


class AppServer:
    def __init__(self, sock_path: Path, timeout: float):
        if not sock_path.exists():
            raise AppServerError(f"Codex daemon 沒在跑（找不到 {sock_path}）")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(sock_path))
        self._buf = b""
        self._next_id = 0
        self._handshake()
        # experimentalApi：queue 系列方法只對宣告要用 experimental 的連線開放。
        self.call("initialize", {"clientInfo": {"name": CLIENT_NAME, "version": "0"},
                                 "capabilities": {"experimentalApi": True}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        try:
            self._frame(_CLOSE, b"")
        except OSError:
            pass
        self._sock.close()

    def call(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self._recv()
            if msg.get("id") != rid:
                continue  # 通知（例如 thread/queue/changed）不是我們要的回應
            if "error" in msg:
                raise AppServerError(f"{method} 失敗：{msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}

    # --- WebSocket ---------------------------------------------------------
    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self._buf:
            self._fill()
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if status.split()[1:2] != ["101"]:
            raise AppServerError(f"Codex daemon 拒絕連線：{status}")

    def _send(self, obj: dict) -> None:
        self._frame(_TEXT, json.dumps(obj, ensure_ascii=False).encode())

    def _frame(self, opcode: int, data: bytes) -> None:
        n = len(data)
        if n < 126:
            size = bytes([0x80 | n])
        elif n < 1 << 16:
            size = bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            size = bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)  # 客戶端送出的 frame 依規範一定要遮罩
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes([0x80 | opcode]) + size + mask + body)

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise AppServerError("Codex daemon 中斷了連線")
        self._buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv(self) -> dict:
        message = b""
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4) if b1 & 0x80 else b""
            data = self._take(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == _CLOSE:
                raise AppServerError("Codex daemon 關閉了連線")
            if opcode == _PING:
                self._frame(0xA, data)
                continue
            if opcode in (_TEXT, 0x0):
                message += data
                if b0 & 0x80:  # FIN：一則訊息收完
                    return json.loads(message)
