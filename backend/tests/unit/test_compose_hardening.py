# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks of the reference deployment (SECURITY.md H1, H2, H3, H5).

Parses ``deploy/docker-compose.yml``, ``deploy/.env.example``,
``deploy/Dockerfile``, ``deploy/egress/Dockerfile`` and
``deploy/Caddyfile.example`` and asserts the hardening properties the
security model promises, so a later edit cannot quietly drop one. The live
behaviour (containers start this way, the worker has no route out, the proxy
cannot resolve db or worker) was verified by bringing the stack up; these
tests keep the files honest.
"""

from __future__ import annotations

import ipaddress
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy"

#: PerFin's own containers (the application image and the egress proxy).
APP_SERVICES = ("api", "worker", "migrate", "mcp", "egress")
#: Containers that touch the database or untrusted files; never on a
#: network with a route out or shared with the proxy.
PRIVATE_SERVICES = ("api", "worker", "migrate", "mcp", "db")
APP_UID = "10001:10001"

#: Capabilities a third-party image demonstrably needs, and nothing else.
ALLOWED_CAP_ADD: dict[str, set[str]] = {
    "proxy": {"NET_BIND_SERVICE"},  # caddy binary carries a file capability
    "proxy-init": {"CHOWN"},  # one-shot chown of Caddy's state volumes
    "clamav": {"CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"},  # image drops to clamav
}
#: Services allowed to start as root: one-shot, no network, CHOWN only; and
#: ClamAV, whose image switches to its own user itself.
ROOT_ALLOWED = {"proxy-init", "clamav"}

#: Every secret in .env.example and the services that may receive it through
#: ``env_file``. Everything else must be blanked in that service's
#: ``environment``. A new secret variable fails ``test_every_secret_is_classified``
#: until it is added here.
SECRET_RECIPIENTS: dict[str, set[str]] = {
    "POSTGRES_PASSWORD": set(),
    "POSTGRES_OWNER_PASSWORD": set(),
    "POSTGRES_APP_PASSWORD": set(),
    "PERFIN_DATABASE_URL": {"api", "worker"},
    "PERFIN_MIGRATION_DATABASE_URL": {"migrate"},
    "PERFIN_MASTER_KEY": {"api", "worker"},
    "PERFIN_SECRET_KEY": {"api", "worker"},
    # config_check is not role-aware yet: the worker refuses to start with
    # OIDC configured but no client secret.
    "PERFIN_OIDC_CLIENT_SECRET": {"api", "worker"},
}
_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|_KEY$|DATABASE_URL)")
SCRATCH_DIR = "/tmp"  # noqa: S108 - the container path under test, not a temp file
_INTERPOLATION_DEFAULT = re.compile(r"^\$\{[A-Z0-9_]+:-([^}]*)\}$")


# ---- loaders ----------------------------------------------------------------


@cache
def compose() -> dict[str, Any]:
    data = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def services() -> dict[str, dict[str, Any]]:
    result = compose()["services"]
    assert isinstance(result, dict)
    return result


def service(name: str) -> dict[str, Any]:
    result = services()[name]
    assert isinstance(result, dict)
    return result


def networks_of(name: str) -> set[str]:
    nets = service(name).get("networks", [])
    return set(nets) if isinstance(nets, (list, dict)) else set()


def network_def(name: str) -> dict[str, Any]:
    nets = compose()["networks"]
    definition = nets[name] or {}
    assert isinstance(definition, dict)
    return definition


def environment(name: str) -> dict[str, str]:
    env = service(name).get("environment", {}) or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


@cache
def env_example() -> dict[str, str]:
    """Active (uncommented) assignments in deploy/.env.example."""
    values: dict[str, str] = {}
    for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
            values[match.group(1)] = value
    return values


def interpolation_default(value: str) -> str:
    match = _INTERPOLATION_DEFAULT.match(value.strip())
    assert match, f"expected ${{VAR:-default}}, got {value!r}"
    return match.group(1)


def mounts(name: str) -> list[str]:
    vols = service(name).get("volumes", []) or []
    return [v if isinstance(v, str) else f"{v['source']}:{v['target']}" for v in vols]


# ---- H1: container hardening ------------------------------------------------


def test_compose_has_the_expected_services() -> None:
    assert set(services()) >= {"proxy", "api", "worker", "db", "migrate", "mcp", "egress"}
    for optional, profile in (("mcp", "mcp"), ("egress", "egress"), ("clamav", "av")):
        assert service(optional).get("profiles") == [profile]


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_services_run_unprivileged(name: str) -> None:
    svc = service(name)
    assert svc.get("user") == APP_UID
    assert svc.get("read_only") is True
    assert svc.get("cap_drop") == ["ALL"]
    assert not svc.get("cap_add")
    assert "no-new-privileges:true" in svc.get("security_opt", [])


@pytest.mark.parametrize("name", sorted(set(services())))
def test_every_service_is_hardened(name: str) -> None:
    svc = service(name)
    assert svc.get("privileged") is not True
    assert svc.get("read_only") is True, "read-only root filesystem"
    assert svc.get("cap_drop") == ["ALL"]
    assert set(svc.get("cap_add", []) or []) <= ALLOWED_CAP_ADD.get(name, set())
    security_opt = svc.get("security_opt", [])
    assert "no-new-privileges:true" in security_opt
    # Docker's default seccomp (and AppArmor) profile stays in force.
    assert not any("unconfined" in opt for opt in security_opt)
    assert svc.get("network_mode") not in ("host",)
    assert svc.get("pid") is None
    assert svc.get("ipc") is None
    assert not svc.get("devices")
    assert not any("docker.sock" in m for m in mounts(name))
    user = str(svc.get("user", ""))
    if name not in ROOT_ALLOWED:
        assert user, "explicit non-root user"
        assert not user.startswith(("0", "root"))
    # Resource limits (H1, F8): bounded pids and memory.
    assert 0 < int(svc["pids_limit"]) <= 1024
    assert svc.get("mem_limit")


@pytest.mark.parametrize("name", [n for n in services() if n != "proxy-init"])
def test_scratch_space_is_tmpfs_noexec(name: str) -> None:
    tmpfs = service(name).get("tmpfs", [])
    tmp = [t for t in tmpfs if t.split(":", 1)[0] == SCRATCH_DIR]
    assert tmp, "scratch space must be a tmpfs on a read-only root filesystem"
    options = tmp[0].split(":", 1)[1].split(",")
    assert {"noexec", "nosuid", "nodev"} <= set(options)
    assert any(o.startswith("size=") for o in options)


def test_root_one_shot_has_no_network() -> None:
    init = service("proxy-init")
    assert init.get("network_mode") == "none"
    assert init.get("restart") == "no"
    assert init.get("cap_add") == ["CHOWN"]


def test_dockerfile_runs_as_unprivileged_user() -> None:
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)", text, flags=re.M)
    assert users[-1] == APP_UID
    assert 'ENTRYPOINT ["perfin"]' in text
    assert re.search(r'HEALTHCHECK .*\n?.*CMD \["perfin", "healthcheck"\]', text)
    # Code is owned by root: the runtime user cannot modify it.
    assert "COPY --from=backend --chown=root:root /app /app" in text


def test_egress_dockerfile_runs_as_unprivileged_user() -> None:
    text = (DEPLOY / "egress" / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^USER\s+(\S+)", text, flags=re.M)[-1] == APP_UID
    assert "pip install" not in text


@pytest.mark.parametrize("path", ["Dockerfile", "egress/Dockerfile"])
def test_base_images_are_pinned(path: str) -> None:
    text = (DEPLOY / path).read_text(encoding="utf-8")
    images = re.findall(r"^ARG \w+_IMAGE=(\S+)", text, flags=re.M)
    assert images
    for image in images:
        assert ":" in image.rsplit("/", 1)[-1], f"{image} has no tag"
        assert not image.endswith(":latest"), f"{image} uses :latest"
    assert not re.search(r"^ADD\s+https?://", text, flags=re.M)
    if path == "Dockerfile":
        uv_pin = r"^ARG UV_IMAGE=ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+$"
        assert re.search(uv_pin, text, flags=re.M), "uv must be pinned to an exact version"


def test_dockerignore_is_an_allowlist() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    active = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert active[0] == "*"
    assert "**/.env" in active
    assert "**/*.key" in active


# ---- H2: network exposure ---------------------------------------------------


def test_only_the_proxy_publishes_ports() -> None:
    published = {name for name, svc in services().items() if svc.get("ports")}
    assert published == {"proxy"}


@pytest.mark.parametrize("name", PRIVATE_SERVICES)
def test_private_services_are_on_internal_networks_only(name: str) -> None:
    nets = networks_of(name)
    assert nets, f"{name} must name its networks explicitly"
    for net in nets:
        assert network_def(net).get("internal") is True, f"{name} joins non-internal {net}"


def test_proxy_cannot_reach_worker_or_database() -> None:
    proxy = networks_of("proxy")
    for name in ("worker", "db", "migrate"):
        assert not proxy & networks_of(name), f"proxy shares a network with {name}"


def test_mcp_cannot_reach_the_database_or_documents() -> None:
    assert not networks_of("mcp") & networks_of("db")
    assert not mounts("mcp")
    assert "env_file" not in service("mcp")
    for key in environment("mcp"):
        assert not _SECRET_NAME.search(key), f"mcp receives {key}"


def test_only_the_egress_proxy_has_a_route_out() -> None:
    routable = {
        net for net in compose()["networks"] if network_def(net).get("internal") is not True
    }
    users = {name for name in services() if networks_of(name) & routable}
    assert users == {"proxy", "egress"}
    assert networks_of("egress") & {"egress"}
    assert not networks_of("egress") & networks_of("db")


def test_worker_drop_folder_mounts() -> None:
    worker_mounts = mounts("worker")
    inbox = [m for m in worker_mounts if ":/var/lib/perfin/inbox" in m]
    processed = [m for m in worker_mounts if ":/var/lib/perfin/processed" in m]
    assert len(inbox) == 1
    assert inbox[0].endswith(":ro")
    assert len(processed) == 1
    assert not processed[0].endswith(":ro")
    assert environment("worker")["PERFIN_DROPFOLDER_INBOX"] == "/var/lib/perfin/inbox"
    assert environment("worker")["PERFIN_DROPFOLDER_PROCESSED"] == "/var/lib/perfin/processed"
    assert not [m for m in mounts("api") if "inbox" in m]


def test_api_trusts_forwarded_headers_from_the_proxy_only() -> None:
    proxy_ip = interpolation_default(service("proxy")["networks"]["web"]["ipv4_address"])
    subnet = interpolation_default(network_def("web")["ipam"]["config"][0]["subnet"])
    assert ipaddress.ip_address(proxy_ip) in ipaddress.ip_network(subnet)
    trusted = [ipaddress.ip_network(c) for c in env_example()["PERFIN_TRUSTED_PROXIES"].split(",")]
    assert trusted == [ipaddress.ip_network(f"{proxy_ip}/32")]


# ---- H3: database -----------------------------------------------------------


def test_database_is_not_published_and_uses_scram() -> None:
    db = service("db")
    assert not db.get("ports")
    env = environment("db")
    assert "--auth-host=scram-sha-256" in env["POSTGRES_INITDB_ARGS"]
    assert "--auth-local=scram-sha-256" in env["POSTGRES_INITDB_ARGS"]
    assert env["POSTGRES_HOST_AUTH_METHOD"] == "scram-sha-256"
    assert "password_encryption=scram-sha-256" in db["command"]
    for var in ("POSTGRES_PASSWORD", "POSTGRES_OWNER_PASSWORD", "POSTGRES_APP_PASSWORD"):
        # Required from .env, never a literal in the compose file.
        assert re.fullmatch(rf"\$\{{{var}:\?[^}}]+\}}", env[var]), var
    assert "./db-init:/docker-entrypoint-initdb.d:ro" in mounts("db")
    assert "env_file" not in db


def test_app_connects_as_app_role_and_migrations_as_owner() -> None:
    env = env_example()
    assert env["PERFIN_DATABASE_URL"].startswith(
        "postgresql+psycopg://perfin_app:${POSTGRES_APP_PASSWORD}@db:5432/"
    )
    assert env["PERFIN_MIGRATION_DATABASE_URL"].startswith(
        "postgresql+psycopg://perfin_owner:${POSTGRES_OWNER_PASSWORD}@db:5432/"
    )
    assert env["PERFIN_DB_APP_ROLE"] == "perfin_app"
    for name in ("POSTGRES_PASSWORD", "POSTGRES_OWNER_PASSWORD", "POSTGRES_APP_PASSWORD"):
        assert env[name] == "CHANGE_ME"


def test_migrate_runs_once_before_api_and_worker() -> None:
    migrate = service("migrate")
    assert migrate["command"] == ["migrate"]
    assert migrate.get("restart") == "no"
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("api", "worker"):
        deps = service(name)["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully"
        assert deps["db"]["condition"] == "service_healthy"


# ---- H5: secrets reach only the services that need them ---------------------


def test_every_secret_is_classified() -> None:
    secrets = {name for name in env_example() if _SECRET_NAME.search(name)}
    secrets -= {"PERFIN_MASTER_KEY_FILE"}  # a path, not a secret
    assert secrets == set(SECRET_RECIPIENTS)


def test_secrets_are_placeholders_in_the_example() -> None:
    env = env_example()
    for name in ("PERFIN_MASTER_KEY", "PERFIN_SECRET_KEY"):
        assert env[name] == "CHANGE_ME"
    assert env["PERFIN_OIDC_CLIENT_SECRET"] == ""


@pytest.mark.parametrize("name", sorted(services()))
def test_secrets_are_blanked_where_not_needed(name: str) -> None:
    svc = service(name)
    env = environment(name)
    if "env_file" not in svc:
        # Explicit environment only: no secret may appear in it except the
        # database bootstrap passwords of the db container itself.
        for key, value in env.items():
            if _SECRET_NAME.search(key) and name != "db":
                pytest.fail(f"{name} receives {key}")
            if name == "db" and key in SECRET_RECIPIENTS:
                assert value.startswith("${"), f"{key} must come from .env"
        return
    assert svc["env_file"] == [".env"]
    for secret, recipients in SECRET_RECIPIENTS.items():
        if name in recipients:
            assert secret not in env, f"{name} needs {secret}"
        else:
            assert env.get(secret) == "", f"{name} must blank {secret}"


def test_gitignore_keeps_env_files_out_of_git() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert "deploy/.env" in lines
    assert "!.env.example" in lines


# ---- configuration completeness --------------------------------------------


def test_env_example_documents_every_setting() -> None:
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    documented = {
        name.lower() for name in re.findall(r"^#?\s*PERFIN_([A-Z0-9_]+)=", text, flags=re.M)
    }
    missing = sorted(set(Settings.model_fields) - documented)
    assert missing == [], (
        "add these settings to deploy/.env.example (commented out if compose "
        f"or the image sets them): {missing}"
    )


def test_env_example_has_no_inline_comments() -> None:
    """``docker run --env-file`` and systemd keep ``# ...`` as part of the value."""
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    inline = [line for line in text.splitlines() if re.match(r"^[A-Z0-9_]+=[^#]*\s#", line)]
    assert inline == []


def test_upload_limits_agree() -> None:
    caddy = (DEPLOY / "Caddyfile.example").read_text(encoding="utf-8")
    sizes = re.findall(r"max_size (\d+)MB", caddy)
    assert sorted(int(s) for s in sizes) == [1, 52]
    assert int(env_example()["PERFIN_UPLOAD_MAX_MB"]) < 52


def test_caddyfile_controls() -> None:
    caddy = (DEPLOY / "Caddyfile.example").read_text(encoding="utf-8")
    assert 'Strict-Transport-Security "max-age=63072000; includeSubDomains; preload"' in caddy
    assert "protocols tls1.2 tls1.3" in caddy
    # Prefix matchers: /healthz/, /readyz%2F, /healthz;x ... are refused too (SR-9).
    assert re.search(r"@internal_only path /healthz\* /readyz\* /metrics\*\n", caddy)
    assert re.search(r"handle @internal_only \{\s*respond 404\s*\}", caddy)
    assert "flush_interval -1" in caddy
    assert "admin off" in caddy
    assert "reverse_proxy api:8000" in caddy
    # Logs drop query strings and credentials (H8).
    assert 'request>uri regexp "\\?.*$" "?redacted"' in caddy
    assert "request>headers>Cookie delete" in caddy
    assert "request>headers>Authorization delete" in caddy
