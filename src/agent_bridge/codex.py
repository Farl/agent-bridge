"""Codex 這一側：找 thread、排訊息進它的佇列、讀它的回覆。

**送訊息**用官方 CLI `codex queue --thread <id> --message <text>`。它把訊息寫進
共用 app-server daemon 的佇列；thread 閒著就立刻開一輪，忙著就等這一輪結束。
Codex 桌面版、`codex` TUI、`codex exec` 都掛在同一個 daemon 上，所以一條路通吃。

**讀**直接開 daemon 的 sqlite（唯讀）：
- `state_5.sqlite` 的 `threads`：名稱、cwd、來源、更新時間。
- `thread_history_1.sqlite` 的 `thread_turns`（每輪狀態）與 `thread_items`
  （每一項，`agentMessage` 的 `phase` 分 `commentary` 旁白與 `final_answer`）。
- `queue_1.sqlite` 的 `queued_items`：還沒被取走的訊息。

**列出、撤回排隊訊息**走 daemon 的 JSON-RPC（`thread/queue/list`、
`thread/queue/delete`，見 appserver.py），不直接改 sqlite：daemon 自己判斷訊息
是否已被取走，也會通知掛著的 UI。

這些表不是公開 API，Codex 升版可能改 schema 或換檔名（檔名帶版本號），失效時
先看 docs/protocol.md 的查證方法。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing

from agent_bridge import model
from agent_bridge.appserver import AppServer
from agent_bridge.config import Config

# subagent（含 guardian review）是 thread 底下的分身，不是能對話的對象。
_SUBAGENT_SOURCE_PREFIX = '{"subagent"'
_BUSY_TURN = "inProgress"
_FAILED_TURN = "failed"
_TURN_STATUS = {_BUSY_TURN: model.BUSY, _FAILED_TURN: model.FAILED}
_FINAL_PHASE = "final_answer"


def _connect(path) -> sqlite3.Connection:
    # mode=ro：絕不寫 daemon 的資料庫；WAL 模式下唯讀連線仍看得到最新內容。
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _turn_status(cfg: Config, thread_ids: list[str]) -> dict[str, str]:
    if not thread_ids or not cfg.codex_history_db.exists():
        return {}
    marks = ",".join("?" * len(thread_ids))
    with closing(_connect(cfg.codex_history_db)) as db:
        rows = db.execute(
            f"""select thread_id, status from thread_turns t
                where thread_id in ({marks}) and rollout_ordinal = (
                  select max(rollout_ordinal) from thread_turns
                  where thread_id = t.thread_id)""", thread_ids).fetchall()
    return {tid: _TURN_STATUS.get(st, model.IDLE) for tid, st in rows}


def queued_counts(cfg: Config, thread_ids: list[str]) -> dict[str, int]:
    """每個 thread 還有幾則訊息在排隊。積壓代表對方忙不過來，後送的會晚到。"""
    if not thread_ids or not cfg.codex_queue_db.exists():
        return {}
    marks = ",".join("?" * len(thread_ids))
    with closing(_connect(cfg.codex_queue_db)) as db:
        rows = db.execute(
            f"""select thread_id, count(*) from queued_items
                where thread_id in ({marks}) group by thread_id""",
            thread_ids).fetchall()
    return dict(rows)


def peers(cfg: Config, include_subagents: bool = False,
          limit: int = 30) -> list[model.Peer]:
    if not cfg.codex_state_db.exists():
        return []
    where = "archived = 0"
    if not include_subagents:
        where += f" and source not like '{_SUBAGENT_SOURCE_PREFIX}%'"
    with closing(_connect(cfg.codex_state_db)) as db:
        rows = db.execute(
            f"""select id, coalesce(nullif(name,''), nullif(title,''), ''),
                       cwd, updated_at_ms
                from threads where {where}
                order by updated_at_ms desc limit ?""", (limit,)).fetchall()
    ids = [r[0] for r in rows]
    status = _turn_status(cfg, ids)
    queued = queued_counts(cfg, ids)
    return [model.Peer(kind=model.CODEX, id=tid, name=name, cwd=cwd or "",
                       status=status.get(tid, model.UNKNOWN),
                       updated_ms=int(ms or 0), queued=queued.get(tid, 0))
            for tid, name, cwd, ms in rows]


def send(cfg: Config, thread_id: str, text: str) -> str:
    """回傳 Codex 給這則訊息的 queue item id。"""
    done = subprocess.run(
        [cfg.codex_bin, "queue", "--thread", thread_id, "--message", text],
        capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"codex queue 失敗：{(done.stderr or done.stdout).strip()}")
    # 輸出形如 "Queued message <item> for thread <thread>."
    words = done.stdout.split()
    return words[2] if len(words) > 2 else ""


def pending(cfg: Config, item_id: str) -> bool:
    """這則訊息是否還在佇列裡沒被取走。"""
    if not cfg.codex_queue_db.exists():
        return False
    with closing(_connect(cfg.codex_queue_db)) as db:
        return db.execute("select 1 from queued_items where id = ?",
                          (item_id,)).fetchone() is not None


def replies(cfg: Config, thread_id: str) -> list[model.Reply]:
    if not cfg.codex_history_db.exists():
        return []
    with closing(_connect(cfg.codex_history_db)) as db:
        rows = db.execute(
            """select item_json, created_at_ms from thread_items
               where thread_id = ? and item_type = 'agentMessage'
               order by rollout_ordinal""", (thread_id,)).fetchall()
    out = []
    for raw, at_ms in rows:
        item = json.loads(raw)
        if item.get("text"):
            out.append(model.Reply(text=item["text"], at_ms=int(at_ms or 0),
                                   final=item.get("phase") == _FINAL_PHASE))
    return out


def status(cfg: Config, thread_id: str) -> str:
    return _turn_status(cfg, [thread_id]).get(thread_id, model.UNKNOWN)



def _created_ms(cfg: Config, item_ids: list[str]) -> dict[str, int]:
    # JSON-RPC 的回應沒有時間，從 sqlite 唯讀補上。
    if not item_ids or not cfg.codex_queue_db.exists():
        return {}
    marks = ",".join("?" * len(item_ids))
    with closing(_connect(cfg.codex_queue_db)) as db:
        return dict(db.execute(
            f"select id, created_at_ms from queued_items where id in ({marks})",
            item_ids).fetchall())


def _text(submission: dict) -> str:
    return "\n".join(part.get("text", "") for part in submission.get("input", [])
                     if part.get("type") == "text")


def queue(cfg: Config, thread_id: str) -> list[model.Queued]:
    """還沒被取走的訊息，依處理順序。寄件者留空，由 bridge 從信封讀。"""
    with AppServer(cfg.codex_control_sock, cfg.socket_timeout) as srv:
        items, cursor = [], None
        while True:
            page = srv.call("thread/queue/list",
                            {"threadId": thread_id, "cursor": cursor})
            items += page.get("data", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
    created = _created_ms(cfg, [i["id"] for i in items])
    out = []
    for item in items:
        out.append(model.Queued(id=item["id"], sender="", text=_text(item),
                                created_ms=int(created.get(item["id"]) or 0)))
    return out


def cancel(cfg: Config, thread_id: str, item_ids: list[str]) -> dict[str, bool]:
    """撤回訊息。回傳每則是否真的撤掉；False 表示它已經被取走（或本來就不在）。"""
    with AppServer(cfg.codex_control_sock, cfg.socket_timeout) as srv:
        return {iid: bool(srv.call("thread/queue/delete",
                                   {"threadId": thread_id, "queuedSubmissionId": iid}
                                   ).get("deleted"))
                for iid in item_ids}
