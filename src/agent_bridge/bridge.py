"""兩邊共用的邏輯：解析地址、加信封、送出、等回覆。

CLI 只負責參數與輸出，所有決定都在這裡，兩種 agent 走完全相同的路。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from agent_bridge import claude, codex, model
from agent_bridge.config import Config

# 各家在自己的 shell 工具裡塞給子程序的「我是誰」。
SELF_ENV = {model.CLAUDE: "CLAUDE_CODE_SESSION_ID", model.CODEX: "CODEX_THREAD_ID"}
ENVELOPE_TAG = "[agent-bridge]"

_BACKENDS = {model.CLAUDE: claude, model.CODEX: codex}


def now_ms() -> int:
    return int(time.time() * 1000)


def backend(kind: str):
    return _BACKENDS[kind]


def all_peers(cfg: Config, include_subagents: bool = False) -> list[model.Peer]:
    found = claude.peers(cfg) + codex.peers(cfg, include_subagents=include_subagents)
    return sorted(found, key=lambda p: p.updated_ms, reverse=True)


def whoami() -> str | None:
    """呼叫者自己的地址；不在任何 agent 裡（例如人在終端機）就是 None。"""
    for kind, var in SELF_ENV.items():
        value = os.environ.get(var)
        if value:
            return f"{kind}:{value}"
    return None


class ResolveError(LookupError):
    pass


def resolve(cfg: Config, target: str) -> model.Peer:
    """把使用者寫的目標變成一個確定的 peer。

    接受 `kind:id`、`kind:id 前綴`、`kind:名稱`，或省略 kind 的 id 前綴／名稱。
    名稱比對不分大小寫且要完整相同；對到不只一個就報錯並列出候選，不猜。
    """
    kind, _, key = target.partition(":")
    if kind not in model.KINDS:
        kind, key = "", target
    pool = [p for p in all_peers(cfg, include_subagents=True)
            if not kind or p.kind == kind]
    exact = [p for p in pool if p.id == key]
    if exact:
        return exact[0]
    matches = [p for p in pool
               if p.id.startswith(key) or p.name.casefold() == key.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ResolveError(f"找不到「{target}」。先跑 `agent-bridge list` 看有哪些。")
    listing = "\n".join(f"  {p.address}  {p.name}" for p in matches)
    raise ResolveError(f"「{target}」對到不只一個，請用完整地址：\n{listing}")


def envelope(sender: model.Peer | str | None, body: str) -> str:
    """訊息開頭說明是誰寄的、怎麼回。收件者不必認得 agent-bridge 也知道怎麼回信。"""
    if sender is None:
        head = f"{ENVELOPE_TAG} 來自外部程式（沒有回覆地址）"
        return f"{head}\n\n{body}"
    if isinstance(sender, model.Peer):
        address, label = sender.address, f" ({sender.name})" if sender.name else ""
    else:
        address, label = sender, ""
    head = (f"{ENVELOPE_TAG} 來自 {address}{label}\n"
            f"回覆方式：在 shell 執行 agent-bridge send {address} \"<你的回覆>\"")
    return f"{head}\n\n{body}"


def _sender(cfg: Config) -> model.Peer | str | None:
    me = whoami()
    if me is None:
        return None
    try:
        return resolve(cfg, me)
    except ResolveError:
        return me  # 自己還沒出現在清單裡（例如剛開的 thread），地址仍然有效


@dataclass(frozen=True)
class Receipt:
    peer: model.Peer
    sent_ms: int
    queue_item: str = ""   # 只有 Codex 有


def send(cfg: Config, target: str, body: str, wrap: bool = True) -> Receipt:
    peer = resolve(cfg, target)
    text = envelope(_sender(cfg), body) if wrap else body
    sent_ms = now_ms()
    if peer.kind == model.CODEX:
        return Receipt(peer, sent_ms, codex.send(cfg, peer.id, text))
    claude.send(cfg, peer.id, text)
    return Receipt(peer, sent_ms)


def replies_since(cfg: Config, peer: model.Peer, since_ms: int) -> list[model.Reply]:
    return [r for r in backend(peer.kind).replies(cfg, peer.id) if r.at_ms >= since_ms]


def _settled(cfg: Config, receipt: Receipt) -> bool:
    peer = receipt.peer
    if peer.kind == model.CODEX and receipt.queue_item and \
            codex.pending(cfg, receipt.queue_item):
        return False
    current = backend(peer.kind).status(cfg, peer.id)
    if current == model.BUSY:
        return False
    if current == model.FAILED:
        raise RuntimeError(f"{peer.address} 這一輪失敗了，訊息沒有被處理，之後要重送")
    return any(r.final for r in replies_since(cfg, peer, receipt.sent_ms))


def wait(cfg: Config, receipt: Receipt, timeout: float | None = None) -> list[model.Reply]:
    """等對方處理完這則訊息，回傳那之後它說的話。逾時拋 TimeoutError。"""
    deadline = time.monotonic() + (cfg.wait_timeout if timeout is None else timeout)
    while not _settled(cfg, receipt):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等 {receipt.peer.address} 回覆逾時")
        time.sleep(cfg.poll_interval)
    return replies_since(cfg, receipt.peer, receipt.sent_ms)
