"""`agent-bridge` 指令。只處理參數與輸出；邏輯在 bridge.py。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from agent_bridge import bridge, config, model, skills

REPO = Path(__file__).resolve().parents[2]


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M") if ms else "-"


def _print_replies(replies) -> None:
    for r in replies:
        tag = "final" if r.final else "note"
        print(f"--- {_when(r.at_ms)} [{tag}]\n{r.text}\n")


def cmd_list(cfg, args) -> int:
    peers = bridge.all_peers(cfg, include_subagents=args.all)
    if args.kind:
        peers = [p for p in peers if p.kind == args.kind]
    if args.json:
        print(json.dumps([{**p.__dict__, "address": p.address} for p in peers],
                         ensure_ascii=False, indent=2))
        return 0
    me = bridge.whoami()
    for p in peers:
        mark = "*" if p.address == me else " "
        backlog = f"  排隊 {p.queued}" if p.queued else ""
        print(f"{mark} {p.address}  {p.status:<7} {_when(p.updated_ms)}  "
              f"{p.name or '(未命名)'}  [{p.cwd}]{backlog}")
    return 0


def cmd_whoami(cfg, args) -> int:
    me = bridge.whoami()
    print(me or "（不在 Claude Code 或 Codex 裡執行，沒有回覆地址）")
    return 0 if me else 1


def cmd_send(cfg, args) -> int:
    body = sys.stdin.read() if args.message == "-" else args.message
    receipt = bridge.send(cfg, args.target, body, wrap=not args.raw,
                          replace_mine=args.replace)
    if receipt.replaced:
        print(f"已撤回你先前排隊的 {len(receipt.replaced)} 則：{' '.join(receipt.replaced)}")
    print(f"已送到 {receipt.peer.address}（{receipt.peer.name or '未命名'}，"
          f"目前 {receipt.peer.status}）")
    # peer 是送出前查的，所以 queued 是排在這則前面的數量。
    ahead = receipt.peer.queued
    if ahead >= cfg.backlog_warn:
        print(f"注意：前面還有 {ahead} 則在排隊，這則要等它們處理完才會被讀到。"
              "要修正方向時，與其追加，不如合併成一則完整的指示，用 --replace 送"
              "（會先撤回你自己還沒被讀的舊訊息）。", file=sys.stderr)
    if receipt.peer.status == model.FAILED:
        print("注意：對方上一輪失敗了（例如額度用完），這則多半也會失敗而被丟掉，"
              "恢復後要重送。", file=sys.stderr)
    if not args.wait:
        return 0
    print("等待回覆中…", file=sys.stderr)
    try:
        _print_replies(bridge.wait(cfg, receipt, timeout=args.timeout))
    except TimeoutError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


def cmd_read(cfg, args) -> int:
    peer = bridge.resolve(cfg, args.target)
    replies = bridge.backend(peer.kind).replies(cfg, peer.id)
    if args.final:
        replies = [r for r in replies if r.final]
    _print_replies(replies[-args.n:])
    return 0


PREVIEW_CHARS = 80


def _preview(text: str) -> str:
    # 信封頭兩行（寄件者、回覆方式）另外顯示，預覽只看正文。
    body = text.split("\n\n", 1)[1] if bridge.sender_of(text) else text
    flat = " ".join(body.split())
    return flat if len(flat) <= PREVIEW_CHARS else flat[:PREVIEW_CHARS] + "…"


def cmd_queue(cfg, args) -> int:
    peer, items = bridge.queue(cfg, args.target)
    if args.json:
        print(json.dumps([q.__dict__ for q in items], ensure_ascii=False, indent=2))
        return 0
    if not items:
        print(f"{peer.address}（{peer.name or '未命名'}）沒有排隊中的訊息")
        return 0
    me = bridge.whoami()
    print(f"{peer.address}（{peer.name or '未命名'}）排隊 {len(items)} 則，依處理順序：")
    for q in items:
        mine = "*" if me and q.sender == me else " "
        print(f"{mine} {q.id}  {_when(q.created_ms)}  {q.sender or '(寄件者不明)'}\n"
              f"    {_preview(q.text)}")
    return 0


def cmd_cancel(cfg, args) -> int:
    if not (args.items or args.all_mine or args.sender):
        print("agent-bridge: 要指定撤哪些：item id、--all-mine 或 --from <地址>",
              file=sys.stderr)
        return 1
    result = bridge.cancel(cfg, args.target, item_ids=args.items or None,
                           sender=args.sender, any_sender=args.any)
    for iid in result.cancelled:
        print(f"已撤回 {iid}")
    for iid, why in result.skipped.items():
        print(f"沒撤 {iid}：{why}", file=sys.stderr)
    if not result.cancelled and not result.skipped:
        print("沒有符合的排隊訊息")
    return 0 if not result.skipped else 3


def cmd_skills_link(cfg, args) -> int:
    for line in skills.link(Path(args.repo).expanduser().resolve()) or ["連結都已就緒，沒有變更"]:
        print(line)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-bridge",
        description="同一台機器上 Claude Code session 與 Codex thread 之間的雙向傳訊")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="列出可以傳訊的 session／thread")
    s.add_argument("--kind", choices=["claude", "codex"])
    s.add_argument("--all", action="store_true", help="連 Codex subagent 也列")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("whoami", help="印出自己的回覆地址")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("send", help="送訊息（自動附上寄件者與回覆方式）")
    s.add_argument("target", help="地址、id 前綴或名稱，例如 codex:01a159cf")
    s.add_argument("message", help="訊息內容；寫 - 代表從 stdin 讀")
    s.add_argument("--wait", action="store_true", help="等對方處理完並印出回覆")
    s.add_argument("--timeout", type=float, help="--wait 最多等幾秒")
    s.add_argument("--raw", action="store_true", help="不加信封")
    s.add_argument("--replace", action="store_true",
                   help="先撤回自己還在對方佇列裡的舊訊息，再送這則（只限 Codex）")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("queue", help="列出對方還沒取走的排隊訊息（只限 Codex）")
    s.add_argument("target")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("cancel", help="撤回還沒被取走的排隊訊息（只限 Codex；預設只能撤自己送的）")
    s.add_argument("target")
    s.add_argument("items", nargs="*", help="要撤的 item id（可寫前綴，見 queue）")
    s.add_argument("--all-mine", action="store_true", help="撤回自己送的全部")
    s.add_argument("--from", dest="sender", metavar="ADDR",
                   help="以這個地址當寄件者（不在 agent 裡執行時用）；沒給 item 時撤它送的全部")
    s.add_argument("--any", action="store_true",
                   help="指定的 item 不是自己送的也撤（例如替人清掉卡住的佇列）")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("read", help="讀對方最近說的話")
    s.add_argument("target")
    s.add_argument("-n", type=int, default=3, help="最近幾段（預設 3）")
    s.add_argument("--final", action="store_true", help="只看每輪的最後答覆")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("skills", help="把 skill 連到 Claude Code 與 Codex 的掃描路徑")
    s.add_argument("action", choices=["link"])
    s.add_argument("--repo", default=str(REPO),
                   help="哪個 repo 的 .claude/skills（預設 agent-bridge 自己）；"
                        "其他 repo 也用同一套連結規則，不必各抄一份")
    s.set_defaults(func=cmd_skills_link)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(config.load(), args)
    except (LookupError, RuntimeError, OSError) as e:
        print(f"agent-bridge: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
