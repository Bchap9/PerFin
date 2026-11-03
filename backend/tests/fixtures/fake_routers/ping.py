# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned and root routers for discovery tests."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["test"])
root_router = APIRouter()


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"pong": "v1"}


@root_router.get("/rootping")
def rootping() -> dict[str, str]:
    return {"pong": "root"}
