"""Rich-text format converters for mapped text fields.

Minimal, dependency-free converters between ``plain``, ``html`` and
``markdown``. All conversions round-trip through plain text. These are
intentionally conservative: correctness over fidelity. When adapters declare
their native text formats (Phase 2+), configs set ``source_format`` /
``target_format`` and these converters handle the rest.
"""

from __future__ import annotations

import html as html_mod
import re
from html.parser import HTMLParser

KNOWN_FORMATS = {"plain", "html", "markdown"}


class _HTMLToText(HTMLParser):
    _SKIP = {"script", "style"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _html_to_plain(value: str) -> str:
    parser = _HTMLToText()
    parser.feed(value)
    return parser.text()


def _plain_to_html(value: str) -> str:
    escaped = html_mod.escape(value)
    return "<p>" + escaped.replace("\n", "<br>\n") + "</p>"


_MD_PATTERNS = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),  # bold
    (re.compile(r"\*(.+?)\*"), r"\1"),  # italic
    (re.compile(r"`(.+?)`"), r"\1"),  # code
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),  # headings
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),  # bullets
    (re.compile(r"\[(.+?)\]\(.+?\)"), r"\1"),  # links -> label
]


def _markdown_to_plain(value: str) -> str:
    out = value
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return re.sub(r"\n{2,}", "\n", out)  # collapse blank lines


def _plain_to_markdown(value: str) -> str:
    return value  # plain is valid markdown; no conversion needed


_TO_PLAIN = {"plain": lambda v: v, "html": _html_to_plain, "markdown": _markdown_to_plain}
_FROM_PLAIN = {"plain": lambda v: v, "html": _plain_to_html, "markdown": _plain_to_markdown}


def convert_text(value, from_format: str, to_format: str):
    """Convert a text value between known formats; non-strings pass through."""
    if not isinstance(value, str) or from_format == to_format:
        return value
    if from_format not in KNOWN_FORMATS or to_format not in KNOWN_FORMATS:
        raise ValueError(f"unknown text format(s): {from_format!r} -> {to_format!r}")
    return _FROM_PLAIN[to_format](_TO_PLAIN[from_format](value))
