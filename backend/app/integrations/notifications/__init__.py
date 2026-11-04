# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound notification channels (docs/security/notifications.md).

* :mod:`.smtp`: the instance SMTP relay (password reset mail, A11) and the
  message builder every mail uses.
* :mod:`.base`: the channel protocol, the delivery outcome codes and the
  connection policy (:class:`~.base.DeliveryOptions`).
* :mod:`.email`: a household SMTP channel (STARTTLS or implicit TLS, always
  verified, connection vetted like any other egress).
* :mod:`.web`: webhook (HMAC-SHA256 signed), ntfy, Gotify and Matrix over
  :class:`app.core.http.SafeHttpClient`.
* :mod:`.registry`: builds a channel from its stored type and configuration.

Channels only ever send a :class:`~.base.Notification`: a fixed title and
one generic sentence chosen by ``app.domain.notifications.routing``.
"""
