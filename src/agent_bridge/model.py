"""兩種 agent 共用的資料形狀。

地址一律寫成 `<kind>:<id>`（例如 `claude:a4ff6083-…`、`codex:01a159cf-…`）。
id 是對方自己產的穩定識別碼：Claude 的 session id、Codex 的 thread id。
名稱會改、pid 會重開，只有 id 適合寫進「回信請寄到這裡」。
"""
from __future__ import annotations

from dataclasses import dataclass

CLAUDE = "claude"
CODEX = "codex"
KINDS = (CLAUDE, CODEX)

# 狀態只分四種，兩邊各自的細節（Claude 的 waitingFor、Codex 的 turn status）
# 由各自的模組翻譯過來。
BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"
# 最近一輪失敗（例如 Codex 額度用完）。送過去的訊息會被這一輪吃掉、不會重排，
# 所以看到它要回頭補送，而不是以為對方閒著在處理。
FAILED = "failed"


@dataclass(frozen=True)
class Peer:
    kind: str
    id: str
    name: str
    cwd: str
    status: str
    updated: str = ""   # 人看的時間字串，排序用 updated_ms
    updated_ms: int = 0
    # 還沒被對方取走的訊息數。只有 Codex 查得到；Claude inbox 看不到，固定為 0。
    queued: int = 0

    @property
    def address(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class Reply:
    """對方說過的一段話。`final` 表示那一輪的最後答覆，而不是過程中的旁白。"""
    text: str
    at_ms: int
    final: bool
