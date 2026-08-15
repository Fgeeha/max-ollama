"""Text helpers for fitting model output into messenger limits."""

# MAX rejects text of 4000 characters or more; leave room for the "..." suffix
# the streaming code appends while a reply is still growing.
MAX_MESSAGE_CHARS = 3900


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into pieces that fit the messenger limit.

    Prefers to break on a blank line, then on a line break, and only cuts
    mid-line when a single line is itself longer than the limit. No characters
    are dropped: joining the pieces reproduces the input.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    rest = text

    while len(rest) > limit:
        window = rest[:limit]

        # Break as late as possible, on the most natural boundary available.
        # The break characters stay with the piece before them, so the next
        # piece starts on real content rather than a stray newline.
        paragraph = window.rfind("\n\n")
        line = window.rfind("\n")
        if paragraph > 0:
            cut = paragraph + 2
        elif line > 0:
            cut = line + 1
        else:
            cut = limit

        pieces.append(rest[:cut])
        rest = rest[cut:]

    if rest:
        pieces.append(rest)

    return pieces
