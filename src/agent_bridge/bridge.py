"""兩邊共用的邏輯：解析地址、加信封、送出、等回覆。

CLI 只負責參數與輸出，所有決定都在這裡，兩種 agent 走完全相同的路。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, replace

from agent_bridge import claude, codex, model
from agent_bridge.config import Config

# 各家在自己的 shell 工具裡塞給子程序的「我是誰」。
SELF_ENV = {model.CLAUDE: "CLAUDE_CODE_SESSION_ID", model.CODEX: "CODEX_THREAD_ID"}
ENVELOPE_TAG = "[agent-bridge]"
# envelope() 第一行的寄件者地址；撤回時靠它判斷是不是自己送的。
_ENVELOPE_SENDER = re.compile(rf"^{re.escape(ENVELOPE_TAG)} 來自 (\S+:\S+)")

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


def sender_of(text: str) -> str:
    """從信封讀出寄件者地址；沒有信封（外部程式、--raw）就是空字串。"""
    m = _ENVELOPE_SENDER.match(text)
    return m.group(1) if m else ""


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
    replaced: list[str] = field(default_factory=list)  # --replace 撤回的舊訊息


def send(cfg: Config, target: str, body: str, wrap: bool = True,
         replace_mine: bool = False) -> Receipt:
    """`replace_mine`：先撤回自己還在對方佇列裡的舊訊息，讓這則成為唯一有效的指示。"""
    peer = resolve(cfg, target)
    replaced: list[str] = []
    if replace_mine:
        replaced = cancel(cfg, peer.address).cancelled
        # 撤回後佇列變短了，警告要看新的排隊數。
        peer = replace(peer, queued=max(0, peer.queued - len(replaced)))
    text = envelope(_sender(cfg), body) if wrap else body
    sent_ms = now_ms()
    if peer.kind == model.CODEX:
        return Receipt(peer, sent_ms, codex.send(cfg, peer.id, text), replaced)
    claude.send(cfg, peer.id, text)
    return Receipt(peer, sent_ms, replaced=replaced)


# --- 排隊中的訊息 ----------------------------------------------------------

def _require_codex(peer: model.Peer) -> None:
    if peer.kind != model.CODEX:
        # Claude inbox 收下就排進對方行程記憶體裡的佇列，協定只有 auth／user 兩種
        # 訊息，外部看不到也撤不回（見 docs/protocol.md）。
        raise RuntimeError(f"{peer.address} 是 Claude session：它的排隊訊息外部看不到，"
                           "也撤不回。只有 Codex thread 支援 queue／cancel／--replace")


def queue(cfg: Config, target: str) -> tuple[model.Peer, list[model.Queued]]:
    peer = resolve(cfg, target)
    _require_codex(peer)
    items = [replace(q, sender=sender_of(q.text)) for q in codex.queue(cfg, peer.id)]
    return peer, items


@dataclass
class CancelResult:
    cancelled: list[str] = field(default_factory=list)
    # 沒撤的：item id → 原因（不是自己送的、已被取走、找不到）。
    skipped: dict[str, str] = field(default_factory=dict)


def cancel(cfg: Config, target: str, item_ids: list[str] | None = None,
           sender: str | None = None, any_sender: bool = False) -> CancelResult:
    """撤回還沒被取走的訊息。

    沒指定 item：撤 `sender`（預設自己）送的全部。指定 item：預設只撤自己送的，
    `any_sender` 才放寬——別人的訊息可能是別的 agent 正在等的工作。
    """
    owner = sender or whoami()
    if not owner and not (item_ids and any_sender):
        raise RuntimeError("不知道你是誰（不在 Claude Code 或 Codex 裡執行）："
                           "用 --from <你的地址> 指定寄件者，或指定 item id 加 --any")
    peer, items = queue(cfg, target)
    result = CancelResult()
    if item_ids:
        chosen = []
        for wanted in item_ids:
            hits = [q for q in items if q.id.startswith(wanted)]
            if len(hits) != 1:
                result.skipped[wanted] = ("不在佇列裡（可能已被取走）" if not hits
                                          else "前綴對到不只一則，請寫完整 id")
            elif not any_sender and hits[0].sender != owner:
                result.skipped[hits[0].id] = (f"不是 {owner} 送的"
                                              f"（寄件者：{hits[0].sender or '不明'}），要撤加 --any")
            else:
                chosen.append(hits[0].id)
    else:
        chosen = [q.id for q in items if q.sender == owner]
    if not chosen:
        return result
    # daemon 在同一把鎖裡判斷能不能刪；deleted=False 就是剛好被取走了。
    for iid, deleted in codex.cancel(cfg, peer.id, chosen).items():
        if deleted:
            result.cancelled.append(iid)
        else:
            result.skipped[iid] = "已被取走，來不及撤回"
    # 改完再列一次確認，不只信 daemon 的回報。
    left = {q.id for q in codex.queue(cfg, peer.id)}
    still = [iid for iid in result.cancelled if iid in left]
    if still:
        raise RuntimeError(f"daemon 回報已撤回，但這幾則還在佇列裡：{', '.join(still)}")
    return result


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
