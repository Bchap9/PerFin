# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming multipart parsing for uploads (``app.ingestion.intake.multipart``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.errors import ProblemError
from app.ingestion.intake.multipart import MultipartFile, boundary_of

BOUNDARY = "synthetic-boundary-1234"
CT = f"multipart/form-data; boundary={BOUNDARY}"


def body(*parts: tuple[str, str | None, bytes], close: bool = True) -> bytes:
    out = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += (
            (
                f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    if close:
        out += f"--{BOUNDARY}--\r\n".encode()
    return out


def chunked(data: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


@pytest.mark.parametrize("size", [1, 7, 64, 100_000])
def test_file_part_is_streamed_in_any_chunking(size: int) -> None:
    payload = bytes(range(256)) * 50 + b"\r\n--not-the-boundary\r\n"
    upload = MultipartFile(CT, chunked(body(("file", "a/b/c.csv", payload)), size))
    assert b"".join(upload.chunks()) == payload
    assert upload.filename == "a/b/c.csv"


def test_chunks_arrive_before_the_body_ends() -> None:
    payload = b"x" * 10_000
    seen: list[int] = []

    def source() -> Iterator[bytes]:
        for piece in chunked(body(("file", "f", payload)), 1000):
            seen.append(len(piece))
            yield piece

    stream = MultipartFile(CT, source()).chunks()
    first = next(stream)
    assert first
    assert len(seen) < 5  # not buffered to the end


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (body(("other", None, b"x")), "named 'file'"),
        (body(("file", "a", b"1"), ("file", "b", b"2")), "one file"),
        (body(("file", "a", b"123"), close=False), "incomplete"),
        (f"--{BOUNDARY}--\r\n".encode(), "No part"),
        (b"garbage without any boundary", "malformed"),
    ],
)
def test_invalid_bodies(raw: bytes, detail: str) -> None:
    upload = MultipartFile(CT, iter([raw]))
    with pytest.raises(ProblemError) as caught:
        b"".join(upload.chunks())
    assert caught.value.status == 422
    assert detail in str(caught.value.problem_detail)


@pytest.mark.parametrize(
    "content_type",
    [None, "", "application/json", "multipart/form-data", "multipart/mixed; boundary=x"],
)
def test_only_multipart_form_data_with_a_boundary(content_type: str | None) -> None:
    with pytest.raises(ProblemError) as caught:
        boundary_of(content_type)
    assert caught.value.status == 415
