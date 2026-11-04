# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build a :class:`~.base.Channel` from a stored channel type and configuration.

The configuration is the decrypted JSON object of ``notification_channel``
(validated by ``app.domain.notifications.schemas`` before it was stored).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.integrations.notifications.base import Channel, ChannelType, DeliveryOptions
from app.integrations.notifications.email import SmtpChannel
from app.integrations.notifications.web import (
    GotifyChannel,
    MatrixChannel,
    NtfyChannel,
    WebhookChannel,
)


def _opt(config: Mapping[str, Any], name: str) -> str | None:
    value = config.get(name)
    return str(value) if value else None


def build_channel(
    channel_type: ChannelType, config: Mapping[str, Any], options: DeliveryOptions
) -> Channel:
    if channel_type is ChannelType.SMTP:
        return SmtpChannel(
            host=str(config["host"]),
            port=int(config["port"]),
            tls="implicit" if config.get("tls") == "implicit" else "starttls",
            sender=str(config["sender"]),
            recipients=tuple(str(r) for r in config["recipients"]),
            username=_opt(config, "username"),
            password=_opt(config, "password"),
            options=options,
        )
    if channel_type is ChannelType.WEBHOOK:
        return WebhookChannel(url=str(config["url"]), secret=str(config["secret"]), options=options)
    if channel_type is ChannelType.NTFY:
        return NtfyChannel(
            server_url=str(config["server_url"]),
            topic=str(config["topic"]),
            token=_opt(config, "token"),
            priority=int(config.get("priority", 3)),
            options=options,
        )
    if channel_type is ChannelType.GOTIFY:
        return GotifyChannel(
            server_url=str(config["server_url"]),
            token=str(config["token"]),
            priority=int(config.get("priority", 5)),
            options=options,
        )
    if channel_type is ChannelType.MATRIX:
        return MatrixChannel(
            homeserver_url=str(config["homeserver_url"]),
            room_id=str(config["room_id"]),
            access_token=str(config["access_token"]),
            options=options,
        )
    raise ValueError(f"unknown channel type {channel_type!r}")  # pragma: no cover
