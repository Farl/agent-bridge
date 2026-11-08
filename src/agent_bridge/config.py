"""所有路徑與可調參數的唯一出處。

每一項都能用環境變數覆寫：測試靠它把整個世界換成 tmp 目錄，使用者靠它應付
非預設安裝（例如 `CODEX_HOME` 指到別處）。其他模組**只**從這裡拿值，不自己
拼 `~/.claude` 之類的字串。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True)
class Config:
    # Claude Code：每個活著的 session 在 sessions/ 下有 <pid>.json（註冊表）與
    # <pid>.<hash>.key（inbox 的認證 token）；對話紀錄在 projects/*/<id>.jsonl。
    claude_home: Path = field(default_factory=lambda: _env_path(
        "AGENT_BRIDGE_CLAUDE_HOME", Path.home() / ".claude"))
    # Codex：thread 的中繼資料在 state_5.sqlite，逐項歷史在 thread_history_1.sqlite，
    # 待送訊息在 queue_1.sqlite。檔名帶版本號，Codex 升版時可能改名——見 docs/protocol.md。
    codex_home: Path = field(default_factory=lambda: _env_path(
        "CODEX_HOME", Path.home() / ".codex"))
    codex_bin: str = field(default_factory=lambda: os.environ.get(
        "AGENT_BRIDGE_CODEX_BIN", "codex"))
    # 等回覆時多久看一次、最多等多久（秒）。
    poll_interval: float = field(default_factory=lambda: _env_float(
        "AGENT_BRIDGE_POLL_INTERVAL", 5.0))
    wait_timeout: float = field(default_factory=lambda: _env_float(
        "AGENT_BRIDGE_WAIT_TIMEOUT", 1800.0))
    # 連 Claude inbox socket 的逾時（秒）。對方只是收下排進佇列，不會等它做完。
    socket_timeout: float = field(default_factory=lambda: _env_float(
        "AGENT_BRIDGE_SOCKET_TIMEOUT", 5.0))

    @property
    def claude_sessions_dir(self) -> Path:
        return self.claude_home / "sessions"

    @property
    def claude_projects_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def codex_state_db(self) -> Path:
        return self.codex_home / "state_5.sqlite"

    @property
    def codex_history_db(self) -> Path:
        return self.codex_home / "thread_history_1.sqlite"

    @property
    def codex_queue_db(self) -> Path:
        return self.codex_home / "queue_1.sqlite"


def load() -> Config:
    return Config()
