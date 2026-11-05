# SPDX-License-Identifier: AGPL-3.0-or-later
"""A private CA for a LAN issuer is trusted only through ``SSL_CERT_FILE``.

``app.core.http.SafeHttpClient`` verifies certificates with
``ssl.create_default_context()``, i.e. OpenSSL's default trust store, which
honours ``SSL_CERT_FILE``. docs/AUTHENTIK.md (step 11) tells operators of an
Authentik behind a private CA to point ``SSL_CERT_FILE`` at a bundle that adds
their CA. This test runs a real TLS server with a synthetic CA: refused
without the bundle, accepted with it, and still bound to the host name.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.http import EgressError, EgressPolicy, IPAddress, SafeHttpClient

pytestmark = pytest.mark.security

HOST = "auth.lan.invalid"  # .invalid never resolves through system DNS


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_pki(directory: Path) -> tuple[Path, Path, Path]:
    """A synthetic CA and a server certificate for ``HOST``; returns PEM paths."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("PerFin test private CA"))
        .issuer_name(_name("PerFin test private CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(HOST))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_path = directory / "private-ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(pem))
    cert_path = directory / "server.pem"
    cert_path.write_bytes(leaf_cert.public_bytes(pem))
    key_path = directory / "server.key"
    key_path.write_bytes(
        leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_path, cert_path, key_path


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"issuer": "lan"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    ca_path, cert_path, key_path = _write_pki(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], ca_path
    finally:
        server.shutdown()
        server.server_close()


def _client(port: int) -> SafeHttpClient:
    # Loopback stands in for the LAN address (allow_loopback is the dev-mode
    # allowance; the address rules are tested in test_egress_ssrf.py).
    policy = EgressPolicy(allowed_hosts=frozenset({f"{HOST}:{port}"}), allow_loopback=True)

    def resolve(_host: str, _port: int) -> list[IPAddress]:
        return [ipaddress.ip_address("127.0.0.1")]

    return SafeHttpClient(policy, resolver=resolve, proxy=None)


def test_private_ca_needs_ssl_cert_file(
    tls_server: tuple[int, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, ca_path = tls_server
    url = f"https://{HOST}:{port}/.well-known/openid-configuration"
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with pytest.raises(EgressError):
        _client(port).get(url)

    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    assert _client(port).get(url).json() == {"issuer": "lan"}


def test_private_ca_does_not_relax_host_name_checks(
    tls_server: tuple[int, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, ca_path = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    other = f"other.lan.invalid:{port}"
    policy = EgressPolicy(allowed_hosts=frozenset({other}), allow_loopback=True)

    def resolve(_host: str, _port: int) -> list[IPAddress]:
        return [ipaddress.ip_address("127.0.0.1")]

    client = SafeHttpClient(policy, resolver=resolve, proxy=None)
    with pytest.raises(EgressError):
        client.get(f"https://{other}/")  # certificate is for HOST only
