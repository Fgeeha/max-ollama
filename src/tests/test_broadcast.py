"""Broadcast pacing and retries."""
from unittest.mock import AsyncMock

import pytest

from bot.handlers import admin


@pytest.mark.asyncio
async def test_transient_failure_is_retried(monkeypatch):
    """A single hiccup should not cost a user their message."""
    monkeypatch.setattr(admin, "BROADCAST_RETRY_DELAY", 0)
    send = AsyncMock(side_effect=[RuntimeError("timeout"), None])
    monkeypatch.setattr(admin.bot, "send_message", send)

    assert await admin._send_broadcast(1, "привет") is True
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_persistent_failure_is_reported_not_raised(monkeypatch):
    """One unreachable user must not abort the whole broadcast."""
    monkeypatch.setattr(admin, "BROADCAST_RETRY_DELAY", 0)
    send = AsyncMock(side_effect=RuntimeError("blocked"))
    monkeypatch.setattr(admin.bot, "send_message", send)

    assert await admin._send_broadcast(1, "привет") is False
    assert send.await_count == admin.BROADCAST_RETRIES


@pytest.mark.asyncio
async def test_broadcast_text_is_escaped(monkeypatch):
    """Admin text with angle brackets must not break the HTML message."""
    monkeypatch.setattr(admin, "BROADCAST_RETRY_DELAY", 0)
    sent = []
    monkeypatch.setattr(
        admin.bot, "send_message",
        AsyncMock(side_effect=lambda **kw: sent.append(kw["text"])),
    )

    from html import escape
    await admin._send_broadcast(1, f"📢 {escape('<b>тест</b>')}")

    assert "&lt;b&gt;" in sent[0]
