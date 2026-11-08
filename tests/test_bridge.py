from __future__ import annotations

import threading

import pytest

from agent_bridge import bridge, claude, codex, model
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
