"""`agent-bridge` 指令。只處理參數與輸出；邏輯在 bridge.py。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from agent_bridge import bridge, config, skills

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
        print(f"{mark} {p.address}  {p.status:<7} {_when(p.updated_ms)}  "
              f"{p.name or '(未命名)'}  [{p.cwd}]")
    return 0


def cmd_whoami(cfg, args) -> int:
    me = bridge.whoami()
    print(me or "（不在 Claude Code 或 Codex 裡執行，沒有回覆地址）")
    return 0 if me else 1


def cmd_send(cfg, args) -> int:
    body = sys.stdin.read() if args.message == "-" else args.message
    receipt = bridge.send(cfg, args.target, body, wrap=not args.raw)
    print(f"已送到 {receipt.peer.address}（{receipt.peer.name or '未命名'}，"
          f"目前 {receipt.peer.status}）")
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
    s.set_defaults(func=cmd_send)

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
