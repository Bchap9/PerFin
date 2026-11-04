# SPDX-License-Identifier: AGPL-3.0-or-later
"""EML extractor: headers, body text, attachments as child documents (D3, D4).

Runs only inside the sandbox child. The message is parsed by the standard
library (``email`` with ``policy.default``) from the bytes on stdin.

* **Headers**: ``From``, ``To``, ``Cc``, ``Delivered-To``, ``Reply-To``,
  ``Subject``, ``Date``, ``Message-ID`` and ``Authentication-Results``
  (provenance and the intake policy, D1), bounded in length and count.
* **Body** (D3): the ``text/plain`` body when there is one, otherwise the
  ``text/html`` body converted to text by
  :func:`app.ingestion.extract.htmltext.html_to_text` (no rendering, no
  fetch); remote resources the HTML references are counted, never loaded.
* **Children** (D4): attachments, inline ``cid:`` images and attached
  messages (``message/rfc822``) come back as
  :class:`app.ingestion.sandbox.protocol.ChildDocument` bytes at depth
  ``options.depth + 1``, for the intake to sniff and store like an upload
  (same caps, names as metadata only). A message being extracted at depth
  ``>= options.max_depth`` (a mail that was itself an attachment) is read
  for its text but its attachments are **not** handed back
  (``depth_limit`` warning): nesting stops at one level. Attached messages
  are not descended into here. Caps: :attr:`ExtractOptions.max_children`,
  ``max_child_bytes`` (larger attachments are skipped and reported),
  ``max_children_total_bytes`` (the rest is left out, ``truncated``) and at
  most :data:`MAX_PARTS` MIME parts are visited.
"""

from __future__ import annotations

from collections.abc import Iterator
from email import policy
from email.errors import MessageError
from email.message import EmailMessage, Message
from email.parser import BytesParser
from typing import Final

from app.ingestion.extract.common import ExtractFailure, metadata_name
from app.ingestion.extract.htmltext import html_to_text
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import (
    PAGE_TEXT_MAX,
    ChildDocument,
    EmailHeaders,
    EmailInfo,
    ExtractOptions,
    ExtractResult,
)

EXTRACTOR_VERSION: Final = "eml-1"
MAX_PARTS: Final = 1000
_ADDRESS_MAX: Final = 1000


def _header(msg: Message, name: str, limit: int) -> str | None:
    try:
        value = msg.get(name)
    except (MessageError, ValueError, TypeError, IndexError):
        return None
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _header_list(msg: Message, name: str, limit: int, count: int) -> list[str]:
    out: list[str] = []
    try:
        values = msg.get_all(name) or []
    except (MessageError, ValueError, TypeError, IndexError):
        return out
    for value in values[:count]:
        text = " ".join(str(value).split())[:limit]
        if text:
            out.append(text)
    return out


def _addresses(msg: Message, name: str, count: int) -> list[str]:
    out: list[str] = []
    for value in _header_list(msg, name, _ADDRESS_MAX * 4, count):
        for address in value.split(","):
            address = address.strip()[:_ADDRESS_MAX]
            if address and len(out) < count:
                out.append(address)
    return out


def headers_of(msg: Message) -> EmailHeaders:
    return EmailHeaders(
        from_addr=_header(msg, "From", _ADDRESS_MAX),
        to=_addresses(msg, "To", 200),
        cc=_addresses(msg, "Cc", 200),
        delivered_to=_addresses(msg, "Delivered-To", 50),
        reply_to=_header(msg, "Reply-To", _ADDRESS_MAX),
        subject=_header(msg, "Subject", 2000),
        date=_header(msg, "Date", 200),
        message_id=_header(msg, "Message-ID", 1000),
        authentication_results=_header_list(msg, "Authentication-Results", 2000, 20),
    )


def leaf_parts(msg: Message, budget: list[int]) -> Iterator[Message]:
    """Non-multipart parts, not descending into attached messages, at most MAX_PARTS."""
    budget[0] -= 1
    if budget[0] < 0:
        return
    if msg.get_content_type() == "message/rfc822" or not msg.is_multipart():
        yield msg
        return
    payload = msg.get_payload()
    if not isinstance(payload, list):
        return
    for part in payload:
        if isinstance(part, Message):
            yield from leaf_parts(part, budget)


def _text_of(part: Message) -> str:
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else part.get_payload()
    except (LookupError, MessageError, ValueError, TypeError, AssertionError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _bytes_of(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(inner, Message):
            return inner.as_bytes(policy=policy.default)
        return b""
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def _content_id(part: Message) -> str | None:
    raw = _header(part, "Content-ID", 500)
    return raw.strip("<> ") or None if raw else None


def _is_body(part: Message, body_parts: set[int]) -> bool:
    return id(part) in body_parts


def extract_eml(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except (MessageError, ValueError, TypeError, IndexError) as exc:
        raise ExtractFailure("malformed_email", "the message cannot be parsed") from exc
    if not msg.keys():
        raise ExtractFailure("malformed_email", "the message has no headers")
    headers = headers_of(msg)
    warnings_out: list[str] = []
    plain = html = None
    try:
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
    except (MessageError, ValueError, TypeError, KeyError):
        warnings_out.append("body_unreadable")
    body_parts = {id(p) for p in (plain, html) if p is not None}
    remote = 0
    html_text = ""
    truncated = False
    if html is not None:
        source = _text_of(html)
        if len(source) > options.max_html_chars:
            source = source[: options.max_html_chars]
            truncated = True
            warnings_out.append("html_truncated")
        html_text, remote, cut = html_to_text(source, limit=PAGE_TEXT_MAX)
        truncated = truncated or cut
    if plain is not None:
        body, body_source = _text_of(plain)[:PAGE_TEXT_MAX], "plain"
    elif html is not None:
        body, body_source = html_text, "html"
    else:
        body, body_source = "", "none"

    children: list[ChildDocument] = []
    attachments = inline = 0
    total = 0
    expand = options.depth < options.max_depth
    budget = [MAX_PARTS]
    for part in leaf_parts(msg, budget):
        if part is msg or _is_body(part, body_parts):
            continue
        maintype = part.get_content_maintype()
        cid = _content_id(part)
        disposition = (part.get_content_disposition() or "").lower()
        is_message = part.get_content_type() == "message/rfc822"
        if disposition == "attachment" or part.get_filename() or is_message:
            origin = "attachment"
            attachments += 1
        elif cid and maintype == "image":
            origin = "inline"
            inline += 1
        else:
            continue  # alternative bodies and other unnamed text parts
        if not expand:
            continue
        if len(children) >= options.max_children:
            truncated = True
            if "child_cap" not in warnings_out:
                warnings_out.append("child_cap")
            continue
        content = _bytes_of(part)
        if not content:
            continue
        if len(content) > options.max_child_bytes:
            warnings_out.append("attachment_too_large")
            truncated = True
            continue
        if total + len(content) > options.max_children_total_bytes:
            truncated = True
            if "children_total_cap" not in warnings_out:
                warnings_out.append("children_total_cap")
            continue
        total += len(content)
        children.append(
            ChildDocument(
                origin=origin,  # type: ignore[arg-type]
                name=metadata_name(part.get_filename()),
                declared_type=part.get_content_type()[:128],
                content_id=cid,
                size=len(content),
                depth=options.depth + 1,
                data=content,
            )
        )
    if budget[0] < 0:
        truncated = True
        warnings_out.append("part_cap")
    if not expand and (attachments or inline):
        warnings_out.append("depth_limit")
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=EXTRACTOR_VERSION,
        email=EmailInfo(
            headers=headers,
            body_text=body,
            body_source=body_source,  # type: ignore[arg-type]
            remote_resources_blocked=remote,
            attachments=attachments,
            inline_images=inline,
        ),
        children=children,
        text=body,
        truncated=truncated,
        warnings=warnings_out[:100],
    )


register("eml", extract_eml)
