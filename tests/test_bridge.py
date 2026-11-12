from __future__ import annotations

import threading

import pytest

from agent_bridge import bridge, claude, cli, codex, model
from conftest import TOKEN

CLAUDE_ID = "aaaa1111-0000-0000-0000-000000000000"
CODEX_ID = "bbbb2222-0000-0000-0000-000000000000"


def test_lists_both_kinds_and_hides_codex_subagents(world):
    world.add_claude(CLAUDE_ID, "Claude PM")
    world.add_codex(CODEX_ID, "Codex PM")
    world.add_codex("cccc", "guardian", source='{"subagent":{"other":"guardian"}}')
    addresses = {p.address for p in bridge.all_peers(world.cfg)}
    assert addresses == {f"claude:{CLAUDE_ID}", f"codex:{CODEX_ID}"}


def test_dead_claude_pid_is_not_listed(world):
    world.add_claude(CLAUDE_ID, "ghost", pid=999_999)
    assert claude.peers(world.cfg) == []


def test_claude_status_shell_counts_as_busy(world):
    world.add_claude(CLAUDE_ID, "x", status="shell")
    assert claude.peers(world.cfg)[0].status == model.BUSY


def test_resolve_by_prefix_name_and_ambiguity(world):
    world.add_claude(CLAUDE_ID, "Same")
    world.add_codex(CODEX_ID, "same")
    assert bridge.resolve(world.cfg, "aaaa").address == f"claude:{CLAUDE_ID}"
    assert bridge.resolve(world.cfg, "codex:same").kind == model.CODEX
    with pytest.raises(bridge.ResolveError, match="不只一個"):
        bridge.resolve(world.cfg, "same")
    with pytest.raises(bridge.ResolveError, match="找不到"):
        bridge.resolve(world.cfg, "nobody")


def test_send_to_claude_authenticates_then_sends_user_frame(world):
    inbox = world.add_claude(CLAUDE_ID, "Claude PM")
    bridge.send(world.cfg, f"claude:{CLAUDE_ID}", "hello", wrap=False)
    assert inbox.lines() == [
        {"type": "auth", "token": TOKEN},
        {"type": "user", "message": {"role": "user", "content": "hello"}}]


def test_envelope_carries_reply_address_from_env(world, monkeypatch):
    inbox = world.add_claude(CLAUDE_ID, "Claude PM")
    world.add_codex(CODEX_ID, "Codex PM")
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_ID)
    bridge.send(world.cfg, CLAUDE_ID, "請幫忙 review")
    content = inbox.lines()[1]["message"]["content"]
    assert f"來自 codex:{CODEX_ID} (Codex PM)" in content
    assert f"agent-bridge send codex:{CODEX_ID}" in content
    assert content.endswith("請幫忙 review")


def test_envelope_without_identity_says_no_reply_address():
    assert "沒有回覆地址" in bridge.envelope(None, "hi")


def test_send_to_codex_uses_queue_and_reports_item(world):
    world.add_codex(CODEX_ID, "Codex PM")
    receipt = bridge.send(world.cfg, CODEX_ID, "hi", wrap=False)
    assert receipt.queue_item == "item-1"
    assert world.queued() == [("item-1", CODEX_ID)]


def test_claude_replies_skip_sidechain_and_mark_final(world):
    world.claude_says(CLAUDE_ID, "thinking aloud", "2026-01-01T00:00:00Z", end=False)
    world.claude_says(CLAUDE_ID, "subagent text", "2026-01-01T00:00:01Z", sidechain=True)
    world.claude_says(CLAUDE_ID, "done", "2026-01-01T00:00:02Z")
    got = claude.replies(world.cfg, CLAUDE_ID)
    assert [(r.text, r.final) for r in got] == [("thinking aloud", False), ("done", True)]


def test_wait_for_codex_returns_after_queue_drains_and_final_answer(world):
    world.add_codex(CODEX_ID, "Codex PM")
    receipt = bridge.send(world.cfg, CODEX_ID, "hi", wrap=False)

    def codex_works():
        world.drain_queue()
        world.codex_says(CODEX_ID, "進行中", receipt.sent_ms + 1, final=False)
        world.codex_says(CODEX_ID, "收到", receipt.sent_ms + 2)

    threading.Timer(0.05, codex_works).start()
    got = bridge.wait(world.cfg, receipt)
    assert [r.text for r in got] == ["進行中", "收到"]


def test_wait_ignores_replies_older_than_the_message(world):
    world.add_codex(CODEX_ID, "Codex PM")
    world.codex_says(CODEX_ID, "old answer", 1)
    receipt = bridge.send(world.cfg, CODEX_ID, "hi", wrap=False)
    world.drain_queue()
    with pytest.raises(TimeoutError):
        bridge.wait(world.cfg, receipt, timeout=0.1)


def test_codex_busy_when_latest_turn_in_progress(world):
    world.add_codex(CODEX_ID, "Codex PM", turn="inProgress")
    assert codex.status(world.cfg, CODEX_ID) == model.BUSY


def test_codex_failed_turn_is_reported_and_stops_waiting(world):
    # 額度用完時 Codex 一收到訊息就開一輪並失敗，訊息被吃掉，不能顯示成 idle。
    world.add_codex(CODEX_ID, "Codex PM", turn="failed")
    assert bridge.resolve(world.cfg, CODEX_ID).status == model.FAILED
    receipt = bridge.send(world.cfg, CODEX_ID, "hi", wrap=False)
    world.drain_queue()
    with pytest.raises(RuntimeError, match="失敗"):
        bridge.wait(world.cfg, receipt, timeout=1)


def test_codex_peer_reports_messages_still_queued_ahead(world):
    world.add_codex(CODEX_ID, "Codex PM", turn="inProgress")
    bridge.send(world.cfg, CODEX_ID, "first", wrap=False)
    # 第二則送出前查到的 peer 就看得到前面那一則還在排隊。
    receipt = bridge.send(world.cfg, CODEX_ID, "second", wrap=False)
    assert receipt.peer.queued == 1
    world.drain_queue()
    assert bridge.resolve(world.cfg, CODEX_ID).queued == 0


# --- 排隊訊息：查看與撤回 ------------------------------------------------------

ME = "claude:" + CLAUDE_ID
OTHER = "claude:dddd4444-0000-0000-0000-000000000000"


def _as(monkeypatch, address: str) -> None:
    kind, _, sid = address.partition(":")
    monkeypatch.setenv(bridge.SELF_ENV[kind], sid)


def test_queue_lists_sender_and_order(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, ME)
    bridge.send(world.cfg, CODEX_ID, "第一則")
    world.queue_raw(CODEX_ID, "人在桌面版打的")
    peer, items = bridge.queue(world.cfg, CODEX_ID)
    assert [q.sender for q in items] == [ME, ""]
    assert items[0].text.endswith("第一則") and items[0].created_ms > 0


def test_cancel_all_mine_leaves_others_queued(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, OTHER)
    theirs = bridge.send(world.cfg, CODEX_ID, "別人的").queue_item
    _as(monkeypatch, ME)
    mine = [bridge.send(world.cfg, CODEX_ID, f"我的 {i}").queue_item for i in range(2)]
    result = bridge.cancel(world.cfg, CODEX_ID)
    assert result.cancelled == mine and not result.skipped
    assert world.queued() == [(theirs, CODEX_ID)]


def test_cancel_by_id_refuses_others_unless_any(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    raw = world.queue_raw(CODEX_ID, "不明寄件者")
    _as(monkeypatch, ME)
    result = bridge.cancel(world.cfg, CODEX_ID, item_ids=[raw])
    assert result.cancelled == [] and "--any" in result.skipped[raw]
    assert bridge.cancel(world.cfg, CODEX_ID, item_ids=[raw], any_sender=True).cancelled == [raw]
    assert world.queued() == []


def test_cancel_reports_items_already_taken(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, ME)
    iid = bridge.send(world.cfg, CODEX_ID, "x").queue_item
    world.drain_queue()  # Codex 先一步取走了
    result = bridge.cancel(world.cfg, CODEX_ID, item_ids=[iid])
    assert result.cancelled == [] and "不在佇列" in result.skipped[iid]


def test_cancel_outside_agent_needs_from(world):
    world.add_codex(CODEX_ID, "Codex PM")
    with pytest.raises(RuntimeError, match="--from"):
        bridge.cancel(world.cfg, CODEX_ID)


def test_cancel_with_from_works_outside_agent(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, ME)
    iid = bridge.send(world.cfg, CODEX_ID, "x").queue_item
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert bridge.cancel(world.cfg, CODEX_ID, sender=ME).cancelled == [iid]


def test_send_replace_keeps_only_the_new_message(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, OTHER)
    theirs = bridge.send(world.cfg, CODEX_ID, "別人的").queue_item
    _as(monkeypatch, ME)
    old = bridge.send(world.cfg, CODEX_ID, "舊方向").queue_item
    receipt = bridge.send(world.cfg, CODEX_ID, "新方向", replace_mine=True)
    assert receipt.replaced == [old]
    assert [i for i, _ in world.queued()] == [theirs, receipt.queue_item]


def test_queue_and_cancel_refuse_claude_targets(world, monkeypatch):
    world.add_claude(CLAUDE_ID, "Claude PM")
    _as(monkeypatch, OTHER)
    with pytest.raises(RuntimeError, match="撤不回"):
        bridge.queue(world.cfg, CLAUDE_ID)
    with pytest.raises(RuntimeError, match="撤不回"):
        bridge.send(world.cfg, CLAUDE_ID, "x", replace_mine=True)


def test_cancel_without_daemon_says_so(world, monkeypatch):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, ME)
    world.cfg.codex_control_sock.unlink()
    with pytest.raises(RuntimeError, match="沒在跑"):
        bridge.cancel(world.cfg, CODEX_ID)


def test_cli_queue_and_cancel(world, monkeypatch, capsys):
    world.add_codex(CODEX_ID, "Codex PM")
    _as(monkeypatch, ME)
    monkeypatch.setattr("agent_bridge.config.load", lambda: world.cfg)
    iid = bridge.send(world.cfg, CODEX_ID, "請看 " + "長" * 200).queue_item
    assert cli.main(["queue", CODEX_ID]) == 0
    out = capsys.readouterr().out
    assert f"* {iid}" in out and ME in out and "回覆方式" not in out and "…" in out
    assert cli.main(["cancel", CODEX_ID]) == 1  # 沒說撤哪些，不猜
    assert cli.main(["cancel", CODEX_ID, "--all-mine"]) == 0
    assert world.queued() == []
