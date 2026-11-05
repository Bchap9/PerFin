# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming ``multipart/form-data`` for ``POST /documents`` (INGESTION.md "Receive").

FastAPI's ``UploadFile`` would spool the whole plaintext file into a
temporary file before the handler runs. The upload route instead feeds the
raw request body through :func:`file_chunks`, which runs
``python-multipart``'s incremental parser and yields the bytes of the single
``file`` part as they arrive; the caller encrypts each chunk straight into
the quarantine area (``app.ingestion.intake.upload.store_stream``). No
plaintext touches the disk and memory stays bounded by the chunk size.

Rules: exactly one part, named ``file`` (any other part, a second file or a
truncated body is ``422 invalid-upload``); the part's ``filename`` is kept
as metadata only (C3); its declared ``Content-Type`` is ignored (the bytes
are sniffed). Part headers are capped by the parser (count and size).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from app.core.errors import ProblemError

FILE_FIELD: Final = "file"
_MAX_FILENAME_BYTES: Final = 1024


def invalid_upload(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-upload", "Invalid upload", detail)


def unsupported_media_type() -> ProblemError:
    return ProblemError(
        415,
        "unsupported-media-type",
        "Unsupported media type",
        "Send the file as multipart/form-data in a part named 'file'.",
    )


def boundary_of(content_type: str | None) -> bytes:
    """The multipart boundary of a ``Content-Type`` header (``415`` if not multipart)."""
    if not content_type:
        raise unsupported_media_type()
    media, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise unsupported_media_type()
    return boundary


@dataclass
class _State:
    header_field: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    disposition: bytes = b""
    in_file: bool = False
    files: int = 0
    parts: int = 0
    finished: bool = False
    filename: str | None = None
    out: list[bytes] = field(default_factory=list)


class MultipartFile:
    """The ``file`` part of a streamed multipart body (see the module doc).

    Iterate :meth:`chunks`; :attr:`filename` is known once the first chunk
    has been produced (the part headers precede its data).
    """

    def __init__(self, content_type: str | None, body: Iterable[bytes]) -> None:
        self._boundary = boundary_of(content_type)
        self._body = body
        self._state = _State()

    @property
    def filename(self) -> str | None:
        return self._state.filename

    def _parser(self) -> MultipartParser:
        state = self._state

        def on_part_begin() -> None:
            state.parts += 1
            state.disposition = b""

        def on_header_field(data: bytes, start: int, end: int) -> None:
            state.header_field += data[start:end]

        def on_header_value(data: bytes, start: int, end: int) -> None:
            state.header_value += data[start:end]

        def on_header_end() -> None:
            if bytes(state.header_field).strip().lower() == b"content-disposition":
                state.disposition = bytes(state.header_value)
            state.header_field.clear()
            state.header_value.clear()

        def on_headers_finished() -> None:
            kind, params = parse_options_header(state.disposition)
            name = params.get(b"name")
            if kind != b"form-data" or name != FILE_FIELD.encode():
                raise invalid_upload("Only one part, named 'file', is accepted.")
            state.files += 1
            if state.files > 1:
                raise invalid_upload("Send one file per request.")
            raw = params.get(b"filename") or b""
            state.filename = raw[:_MAX_FILENAME_BYTES].decode("utf-8", "replace") or None
            state.in_file = True

        def on_part_data(data: bytes, start: int, end: int) -> None:
            if state.in_file and end > start:
                state.out.append(bytes(data[start:end]))

        def on_part_end() -> None:
            state.in_file = False

        def on_end() -> None:
            state.finished = True

        return MultipartParser(
            self._boundary,
            {
                "on_part_begin": on_part_begin,
                "on_header_field": on_header_field,
                "on_header_value": on_header_value,
                "on_header_end": on_header_end,
                "on_headers_finished": on_headers_finished,
                "on_part_data": on_part_data,
                "on_part_end": on_part_end,
                "on_end": on_end,
            },
        )

    def chunks(self) -> Iterator[bytes]:
        """The file's bytes, chunk by chunk; the whole body is consumed and validated."""
        parser = self._parser()
        state = self._state
        try:
            for raw in self._body:
                if not raw:
                    continue
                parser.write(raw)
                if state.out:
                    pending, state.out = state.out, []
                    yield from pending
            parser.finalize()
        except MultipartParseError:
            raise invalid_upload("The multipart body is malformed.") from None
        if state.out:
            pending, state.out = state.out, []
            yield from pending
        if not state.finished:
            raise invalid_upload("The multipart body is incomplete.")
        if state.files != 1:
            raise invalid_upload("No part named 'file' was sent.")
