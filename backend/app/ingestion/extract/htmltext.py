# SPDX-License-Identifier: AGPL-3.0-or-later
"""A strict HTML-to-text converter on the standard library (SECURITY.md D3).

Used for HTML mail bodies inside the sandbox child. It is a tokenizer pass
(``html.parser.HTMLParser``), not a renderer: no DOM is built, no CSS is
applied, no script runs, and **nothing is fetched** - the converter has no
I/O at all. What it does:

* keeps text, turning block elements (``p``, ``div``, ``br``, ``tr``, ``li``,
  headings, ...) into line breaks and table cells into tab-separated text;
* drops the content of ``script``, ``style``, ``head``, ``title``,
  ``template``, ``noscript``, ``iframe``, ``object``, ``svg``, ``math`` and
  similar containers, and every comment, doctype and processing instruction;
* drops ``img`` elements (an image's ``alt`` text is kept), counting the ones
  whose source is remote (``http:``, ``https:``, ``//`` or anything other
  than ``cid:``/``data:``) as blocked remote resources, along with remote
  stylesheets, backgrounds and frames: tracking pixels are never loaded;
* keeps link text but not the target;
* resolves character references (``convert_charrefs``) and strips control
  characters; output is capped by the caller's character limit.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

SKIP_CONTENT: Final = frozenset(
    {
        "script",
        "style",
        "head",
        "title",
        "template",
        "noscript",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "svg",
        "math",
        "canvas",
        "audio",
        "video",
        "select",
        "button",
        "textarea",
    }
)
BLOCK: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "ul",
    }
)
VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
        "param",
    }
)
#: Attributes that would make a renderer fetch something.
FETCH_ATTRS: Final = frozenset({"src", "background", "poster", "data", "srcset", "href"})
FETCHING_TAGS: Final = frozenset(
    {
        "img",
        "link",
        "iframe",
        "frame",
        "embed",
        "object",
        "video",
        "audio",
        "source",
        "script",
        "input",
        "body",
        "table",
        "td",
        "th",
        "image",
    }
)
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES: Final = re.compile(r"[ \t\u00a0]+")
_CSS_URL: Final = re.compile(r"url\(", re.IGNORECASE)


def _is_remote(value: str) -> bool:
    target = value.strip().lower()
    if not target:
        return False
    return not target.startswith(("cid:", "data:", "#", "mailto:"))


class _Converter(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.skip: list[str] = []
        self.remote = 0
        self.truncated = False

    # -- output ------------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self.truncated or not text:
            return
        room = self.limit - self.size
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.parts.append(text)
        self.size += len(text)

    def _count_remote(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            styled = name == "style" and _CSS_URL.search(value) is not None
            fetched = name in FETCH_ATTRS and tag in FETCHING_TAGS and _is_remote(value)
            if styled or fetched:
                self.remote += 1

    # -- parser callbacks ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._count_remote(tag, attrs)
        if self.skip:
            if tag in SKIP_CONTENT and tag not in VOID:
                self.skip.append(tag)
            return
        if tag in SKIP_CONTENT and tag not in VOID:
            self.skip.append(tag)
            return
        if tag == "br":
            self._emit("\n")
        elif tag == "img":
            alt = next((v for k, v in attrs if k == "alt" and v), None)
            if alt:
                self._emit(f" {alt} ")
        elif tag in ("td", "th"):
            self._emit("\t")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in BLOCK:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            elif tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self._emit(data)
        elif self.skip[-1] == "style":
            # Stylesheets fetch through url(...) and @import.
            self.remote += len(_CSS_URL.findall(data)) + data.lower().count("@import")


def html_to_text(html: str, *, limit: int = 400_000) -> tuple[str, int, bool]:
    """``(text, remote_resources_blocked, truncated)`` for an HTML document (module doc)."""
    parser = _Converter(limit)
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):  # malformed markup: keep what was read
        pass
    raw = _CONTROL.sub("", "".join(parser.parts))
    lines = [_SPACES.sub(" ", line).strip() for line in raw.replace("\r", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip(), parser.remote, parser.truncated
