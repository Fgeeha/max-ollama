"""Streaming safeguards: timeouts and one generation per user at a time."""
import time
from unittest.mock import MagicMock

import httpx
import pytest

from bot.utils.ollama import OllamaClient, OllamaTimeoutError


class _FakeStream:
    """Minimal stand-in for httpx's streaming response context manager."""

    def __init__(self, lines, *, raise_on_read=None):
        self._lines = lines
        self._raise_on_read = raise_on_read

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        if self._raise_on_read is not None:
            raise self._raise_on_read
        for line in self._lines:
            yield line


@pytest.fixture
async def client():
    c = OllamaClient("http://localhost:11434")
    c._available_models = [{"name": "m:latest"}]
    c._last_model_check = time.time()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_stalled_stream_raises_timeout(client, monkeypatch):
    """A read timeout from httpx surfaces as OllamaTimeoutError, not a generic error."""
    monkeypatch.setattr(
        client.client, "stream",
        lambda *a, **kw: _FakeStream([], raise_on_read=httpx.ReadTimeout("stalled")),
    )

    with pytest.raises(OllamaTimeoutError):
        async for _ in client.chat_stream("m", [{"role": "user", "content": "hi"}]):
            pass


@pytest.mark.asyncio
async def test_generation_deadline_is_enforced(client, monkeypatch):
    """A stream that keeps producing output forever still hits the hard limit."""
    client.generation_timeout = 0  # every chunk is already past the deadline
    monkeypatch.setattr(
        client.client, "stream",
        lambda *a, **kw: _FakeStream(['{"message":{"content":"x"},"done":false}'] * 5),
    )

    with pytest.raises(OllamaTimeoutError):
        async for _ in client.chat_stream("m", [{"role": "user", "content": "hi"}]):
            pass


@pytest.mark.asyncio
async def test_stream_read_timeout_is_passed_to_httpx(client, monkeypatch):
    """The configured read timeout actually reaches the HTTP layer."""
    captured = {}

    def fake_stream(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeStream(['{"message":{"content":"ok"},"done":true}'])

    monkeypatch.setattr(client.client, "stream", fake_stream)

    async for _ in client.chat_stream("m", [{"role": "user", "content": "hi"}]):
        pass

    assert captured["timeout"].read == client.stream_read_timeout
    assert captured["timeout"].pool is None  # no overall deadline on the stream


@pytest.mark.asyncio
async def test_second_generation_for_same_user_is_refused(monkeypatch):
    """While a user's generation runs, their next message gets a clear refusal."""
    from bot.handlers import chat

    replies = []

    async def fake_answer(event, text, **kwargs):
        replies.append(text)

    monkeypatch.setattr(chat, "answer", fake_answer)

    event = MagicMock()
    event.message.sender.user_id = 777
    event.get_ids.return_value = (1, 777)

    chat._generating.add(777)
    try:
        await chat._process_chat_interaction(
            event,
            stored_user_message="привет",
            payload_content="привет",
        )
    finally:
        chat._generating.discard(777)

    assert len(replies) == 1
    assert "ещё отвечаю" in replies[0]
