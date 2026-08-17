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

    chat._generating[777] = None
    try:
        await chat._process_chat_interaction(
            event,
            stored_user_message="привет",
            payload_content="привет",
        )
    finally:
        chat._generating.pop(777, None)

    assert len(replies) == 1
    assert "ещё отвечаю" in replies[0]


@pytest.mark.asyncio
async def test_keep_alive_and_options_reach_ollama(client, monkeypatch):
    """Operator settings must actually appear in the request payload."""
    client.keep_alive = "10m"
    client.options = {"temperature": 0.3, "num_ctx": 8192}
    captured = {}

    def fake_stream(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _FakeStream(['{"message":{"content":"ok"},"done":true}'])

    monkeypatch.setattr(client.client, "stream", fake_stream)

    async for _ in client.chat_stream("m", [{"role": "user", "content": "hi"}]):
        pass

    assert captured["keep_alive"] == "10m"
    assert captured["options"] == {"temperature": 0.3, "num_ctx": 8192}


@pytest.mark.asyncio
async def test_unset_options_are_not_sent(client, monkeypatch):
    """With nothing configured, the model's own defaults apply."""
    captured = {}

    def fake_stream(*args, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _FakeStream(['{"message":{"content":"ok"},"done":true}'])

    monkeypatch.setattr(client.client, "stream", fake_stream)

    async for _ in client.chat_stream("m", [{"role": "user", "content": "hi"}]):
        pass

    assert "options" not in captured
    assert "keep_alive" not in captured


@pytest.fixture
async def openai_client():
    c = OllamaClient("http://localhost:4000", api_style="openai")
    c._available_models = [{"name": "m:latest", "size": 0}]
    c._last_model_check = time.time()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_openai_style_chat_stream_parses_sse_and_usage(openai_client, monkeypatch):
    """LiteLLM/OpenAI SSE deltas must map onto the same chunk shape chat.py expects."""
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        openai_client.client, "stream",
        lambda *a, **kw: _FakeStream(lines),
    )

    chunks = [
        c async for c in openai_client.chat_stream("m", [{"role": "user", "content": "hi"}])
    ]

    text = "".join(c["message"]["content"] for c in chunks if not c["done"])
    assert text == "Hello"
    final = chunks[-1]
    assert final["done"] is True
    assert final["prompt_eval_count"] == 5
    assert final["eval_count"] == 2


@pytest.mark.asyncio
async def test_openai_style_list_models_maps_id_to_name(monkeypatch):
    """OpenAI-style /v1/models has no size/family; only the id survives as "name"."""
    c = OllamaClient("http://localhost:4000", api_style="openai")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gpt-4o-mini"}, {"id": "llava"}]}

    async def fake_get(path):
        assert path == "/v1/models"
        return _Resp()

    monkeypatch.setattr(c.client, "get", fake_get)
    models = await c.list_models()
    await c.close()

    assert [m["name"] for m in models] == ["gpt-4o-mini", "llava"]


def test_openai_style_messages_with_images_use_content_parts():
    """A flat "images" field must become OpenAI's content-parts list, not be dropped."""
    converted = OllamaClient._to_openai_messages([
        {"role": "user", "content": "what's this", "images": ["Zm9v"]},
    ])

    assert converted[0]["content"][0] == {"type": "text", "text": "what's this"}
    assert converted[0]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,Zm9v"


def test_runtime_passes_every_configured_limit():
    """Guard against a setting being declared but never wired to the client."""
    from bot.config import settings
    from bot.runtime import ollama_client

    assert ollama_client.timeout == settings.OLLAMA_TIMEOUT
    assert ollama_client.stream_read_timeout == settings.OLLAMA_STREAM_READ_TIMEOUT
    assert ollama_client.generation_timeout == settings.OLLAMA_GENERATION_TIMEOUT
    assert ollama_client.keep_alive == settings.OLLAMA_KEEP_ALIVE
