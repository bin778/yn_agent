"""Translate draft headings and bold spans into native Slack rich text."""

from __future__ import annotations

import re
from collections.abc import Iterator

# Ordinary punctuation, numbered lists, and unpaired markers remain text.
_MARKUP = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<heading>\S[^\n]*)|(?<![\\*])\*\*(?P<bold>[^*\n]+)\*\*(?!\*)", re.MULTILINE)
_BLOCK_CHARS = 1400


def _spans(text: str) -> Iterator[tuple[str, bool]]:
    end = 0
    for match in _MARKUP.finditer(text):
        if match.start() > end:
            yield text[end:match.start()], False
        heading = match.group("heading")
        if heading is not None:
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", heading)
            yield re.sub(r"\*\*([^*\n]+)\*\*", r"\1", heading), True
        else:
            yield match.group("bold"), True
        end = match.end()
    if end < len(text):
        yield text[end:], False


def plain_draft_title(text: str) -> str:
    return "".join(part for part, _ in _spans(text))


def draft_body_blocks(text: str) -> Iterator[dict]:
    """Split rendered text without breaking an inline style across blocks."""
    elements = []
    remaining = _BLOCK_CHARS
    for part, bold in _spans(text):
        offset = 0
        while offset < len(part):
            fragment = part[offset:offset + remaining]
            element = {"type": "text", "text": fragment}
            if bold:
                element["style"] = {"bold": True}
            elements.append(element)
            offset += len(fragment)
            remaining -= len(fragment)
            if not remaining:
                yield {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}
                elements, remaining = [], _BLOCK_CHARS
    if elements:
        yield {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}
