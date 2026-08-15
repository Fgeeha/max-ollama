"""Splitting long model answers into messages MAX will accept.

MAX rejects any text of 4000 characters or more, and model answers routinely
run longer. Before this, the send simply failed inside a bare ``except`` and the
user was left with a truncated message and no error.
"""
import pytest

from bot.utils.text import MAX_MESSAGE_CHARS, split_message


def test_short_text_is_one_piece():
    assert split_message("привет") == ["привет"]


def test_empty_text_yields_nothing():
    assert split_message("") == []


@pytest.mark.parametrize("size", [MAX_MESSAGE_CHARS - 1, MAX_MESSAGE_CHARS, 12_000])
def test_every_piece_fits_the_limit(size):
    pieces = split_message("а" * size)
    assert pieces
    assert all(len(p) <= MAX_MESSAGE_CHARS for p in pieces)


def test_nothing_is_lost_or_duplicated():
    text = "\n\n".join(f"Абзац {i}. " + "слово " * 60 for i in range(40))
    pieces = split_message(text)

    assert len(pieces) > 1
    assert "".join(pieces).replace("\n", "") == text.replace("\n", "")


def test_splits_on_paragraph_boundary_when_possible():
    first = "Первый абзац." + " x" * 1000
    second = "Второй абзац." + " y" * 1000
    pieces = split_message(f"{first}\n\n{second}")

    assert len(pieces) == 2
    assert pieces[0].strip().startswith("Первый абзац.")
    assert pieces[1].strip().startswith("Второй абзац.")


def test_falls_back_to_line_then_hard_cut():
    """A single unbroken blob still gets split rather than dropped."""
    pieces = split_message("x" * (MAX_MESSAGE_CHARS * 2 + 5))

    assert len(pieces) == 3
    assert all(len(p) <= MAX_MESSAGE_CHARS for p in pieces)
    assert sum(len(p) for p in pieces) == MAX_MESSAGE_CHARS * 2 + 5


def test_code_block_is_not_split_mid_line_when_avoidable():
    lines = [f"line {i} " + "z" * 50 for i in range(200)]
    pieces = split_message("\n".join(lines))

    assert len(pieces) > 1
    # Every piece should start at a line boundary, not mid-word
    for piece in pieces[1:]:
        assert piece.startswith("line ")


@pytest.mark.asyncio
async def test_long_answer_is_delivered_in_full(monkeypatch):
    """A 10k answer reaches the user completely, not truncated at the limit."""
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers import chat

    sent: list[str] = []

    async def fake_answer(event, text, **kwargs):
        assert len(text) < 4000, "кусок не должен превышать лимит MAX"
        sent.append(text)

    edited: list[str] = []

    async def fake_edit(message_id, text, **kwargs):
        assert len(text) < 4000
        edited.append(text)

    monkeypatch.setattr(chat, "answer", fake_answer)
    monkeypatch.setattr(chat.bot, "edit_message", AsyncMock(side_effect=fake_edit))

    answer_text = "\n\n".join(f"Абзац {i}. " + "текст " * 80 for i in range(30))
    await chat._deliver(MagicMock(), answer_text, "placeholder-mid")

    delivered = "".join(edited + sent)
    assert delivered.replace("\n", "") == answer_text.replace("\n", "")
    assert len(edited) == 1, "первый кусок заменяет сообщение стрима"
    assert len(sent) >= 1, "остальные приходят отдельными сообщениями"
