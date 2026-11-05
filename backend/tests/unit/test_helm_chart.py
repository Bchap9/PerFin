# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendered-manifest checks of the Helm chart (SECURITY.md H1, H2, H3, H5).

Renders ``deploy/helm/perfin`` with ``helm template`` and asserts the same
hardening ``test_compose_hardening.py`` asserts for Compose: non-root pods with
a read-only root filesystem, no capabilities, RuntimeDefault seccomp and no
service-account token; per-role secrets exactly as in Compose (the mcp pod
never sees the application secrets); NetworkPolicies that deny by default and
leave the worker and mcp no route out except the allowlist; the database
reachable only from the app pods; and the render-time refusals of insecure
values.

Needs the ``helm`` binary (``PERFIN_HELM`` or ``PATH``); install the pinned
build with ``scripts/install-chart-tools.sh <dir>``. The CI backend job does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - runs the pinned helm binary on repo files
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.mcp.config import FORBIDDEN_ENV, FORBIDDEN_ENV_RE

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "deploy" / "helm" / "perfin"
CI_VALUES = CHART / "ci" / "test-values.yaml"
FULL_VALUES = CHART / "ci" / "full-values.yaml"
KUBE_VERSION = "1.31.0"
APP_UID = 10001

#: Keys of secrets.existingSecret each role receives (as in Compose:
#: test_compose_hardening.SECRET_RECIPIENTS plus the per-service blanks).
ROLE_SECRETS: dict[str, set[str]] = {
    "api": {
        "PERFIN_DATABASE_URL",
        "PERFIN_MASTER_KEY",
        "PERFIN_SECRET_KEY",
        "PERFIN_OIDC_CLIENT_SECRET",
        "PERFIN_SMTP_PASSWORD",
    },
    "worker": {
        "PERFIN_DATABASE_URL",
        "PERFIN_MASTER_KEY",
        "PERFIN_SECRET_KEY",
        "PERFIN_OIDC_CLIENT_SECRET",
        "PERFIN_PRICE_PROVIDER_API_KEY",
    },
    "migrate": {"PERFIN_MIGRATION_DATABASE_URL"},
    "mcp": set(),
}
_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|_KEY$|DATABASE_URL|_TOKEN$)")


# ---- helpers -----------------------------------------------------------------


def helm_binary() -> str:
    found = os.environ.get("PERFIN_HELM") or shutil.which("helm")
    if not found:
        pytest.fail(
            "helm is required for the chart tests: run scripts/install-chart-tools.sh <dir> "
            "and put <dir> on PATH (or set PERFIN_HELM)"
        )
    return found


def run_helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed binary, argument list, no shell
        [helm_binary(), *args], capture_output=True, text=True, check=False, timeout=120
    )


def render_raw(*values_files: Path, sets: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    args = ["template", "perfin", str(CHART), "--kube-version", KUBE_VERSION]
    for path in values_files:
        args += ["-f", str(path)]
    for item in sets:
        args += ["--set", item]
    return run_helm(*args)


def render(*values_files: Path, sets: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    result = render_raw(*values_files, sets=sets)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@cache
def default_docs() -> tuple[dict[str, Any], ...]:
    return tuple(render(CI_VALUES))


@cache
def full_docs() -> tuple[dict[str, Any], ...]:
    return tuple(render(FULL_VALUES))


Docs = tuple[dict[str, Any], ...] | list[dict[str, Any]]


def of_kind(docs: Docs, kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


def component(doc: dict[str, Any]) -> str:
    labels: dict[str, str] = doc["metadata"]["labels"]
    return labels["app.kubernetes.io/component"]


def workloads(docs: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] in ("Deployment", "StatefulSet", "Job")]


def pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = doc["spec"]["template"]["spec"]
    return spec


def workload(docs: tuple[dict[str, Any], ...], name: str) -> dict[str, Any]:
    (found,) = [doc for doc in workloads(docs) if component(doc) == name]
    return found


def policy(docs: tuple[dict[str, Any], ...], name: str) -> dict[str, Any]:
    (found,) = [doc for doc in of_kind(docs, "NetworkPolicy") if doc["metadata"]["name"] == name]
    return found


def secret_refs(container: dict[str, Any]) -> dict[str, str]:
    """env name -> referenced Secret name, for every secretKeyRef."""
    refs: dict[str, str] = {}
    for entry in container.get("env", []):
        ref = entry.get("valueFrom", {}).get("secretKeyRef")
        if ref:
            refs[entry["name"]] = ref["name"]
    return refs


def component_selector(name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "perfin",
        "app.kubernetes.io/instance": "perfin",
        "app.kubernetes.io/component": name,
    }


DNS_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
}


# ---- lint and render ---------------------------------------------------------


@pytest.mark.parametrize("values", [CI_VALUES, FULL_VALUES], ids=["minimal", "full"])
def test_chart_lints_strict(values: Path) -> None:
    result = run_helm("lint", "--strict", "-f", str(values), str(CHART))
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_render_contains_every_role() -> None:
    names = {component(doc) for doc in workloads(full_docs())}
    assert names == {"api", "worker", "mcp", "migrate", "db"}
    assert {component(doc) for doc in workloads(default_docs())} == {"api", "worker", "migrate"}


def test_db_init_scripts_match_compose() -> None:
    """The bundled database runs the very scripts Compose mounts (H3)."""
    compose_dir = ROOT / "deploy" / "db-init"
    chart_dir = CHART / "files" / "db-init"
    compose_files = sorted(p.name for p in compose_dir.glob("*.sh"))
    assert compose_files == sorted(p.name for p in chart_dir.glob("*.sh"))
    for name in compose_files:
        assert (chart_dir / name).read_bytes() == (compose_dir / name).read_bytes(), name


# ---- H1: pod and container hardening -----------------------------------------


@pytest.mark.parametrize("which", ["minimal", "full"])
def test_every_pod_is_hardened(which: str) -> None:
    docs = default_docs() if which == "minimal" else full_docs()
    for doc in workloads(docs):
        name = component(doc)
        spec = pod_spec(doc)
        pod_sc = spec["securityContext"]
        assert spec["automountServiceAccountToken"] is False, name
        assert spec.get("enableServiceLinks") is False, name
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            assert not spec.get(flag), (name, flag)
        assert pod_sc["runAsNonRoot"] is True, name
        assert pod_sc["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for volume in spec.get("volumes", []):
            assert "hostPath" not in volume, (name, volume)
        containers = spec["containers"] + spec.get("initContainers", [])
        for container in containers:
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, name
            assert sc["readOnlyRootFilesystem"] is True, name
            assert sc["capabilities"] == {"drop": ["ALL"]}, name
            assert sc.get("privileged") is not True, name
            assert sc["runAsNonRoot"] is True, name
            assert sc["seccompProfile"] == {"type": "RuntimeDefault"}, name
            assert container["resources"]["limits"]["memory"], name
            assert "/tmp" in {m["mountPath"] for m in container["volumeMounts"]}, name  # noqa: S108
        expected_uid = 70 if name == "db" else APP_UID
        assert pod_sc["runAsUser"] == expected_uid, name
        for container in containers:
            assert container["securityContext"]["runAsUser"] == expected_uid, name


def chart_app_version() -> str:
    """The chart's appVersion, the image tag the default values must deploy."""
    match = re.search(r'^appVersion:\s*"?([^"\s#]+)"?', (CHART / "Chart.yaml").read_text(), re.MULTILINE)
    assert match is not None, "Chart.yaml has no appVersion"
    return match.group(1)


def test_app_pods_run_the_release_image_never_latest() -> None:
    for doc in workloads(default_docs()):
        image = pod_spec(doc)["containers"][0]["image"]
        assert image == f"ghcr.io/bchap9/perfin:{chart_app_version()}", image
    for doc in workloads(full_docs()):
        if component(doc) != "db":
            image = pod_spec(doc)["containers"][0]["image"]
            assert image.startswith("ghcr.io/bchap9/perfin@sha256:")
    result = render_raw(CI_VALUES, sets=("image.tag=latest",))
    assert result.returncode != 0
    assert "not latest" in result.stderr


def test_service_account_token_not_mounted() -> None:
    (account,) = of_kind(default_docs(), "ServiceAccount")
    assert account["automountServiceAccountToken"] is False


# ---- H5 / H3: secrets per role -------------------------------------------------


@pytest.mark.parametrize("role", ["api", "worker", "migrate", "mcp"])
def test_each_role_gets_only_its_secrets(role: str) -> None:
    docs = full_docs()
    container = pod_spec(workload(docs, role))["containers"][0]
    refs = secret_refs(container)
    app_secret_keys = {name for name, secret in refs.items() if secret == "perfin-secrets"}
    assert app_secret_keys == ROLE_SECRETS[role]
    for entry in container.get("env", []):
        if _SECRET_NAME.search(entry["name"]):
            assert "value" not in entry, f"{role}: secret {entry['name']} rendered inline"


def test_mcp_never_receives_application_secrets_or_config() -> None:
    """AI_ACCESS.md M7: `perfin mcp` refuses these; the chart never sends them."""
    container = pod_spec(workload(full_docs(), "mcp"))["containers"][0]
    assert "envFrom" not in container
    names = {entry["name"] for entry in container["env"]}
    assert not {n for n in names if n in FORBIDDEN_ENV or FORBIDDEN_ENV_RE.match(n)}
    assert set(secret_refs(container).values()) == {"perfin-mcp"}
    assert "volumeMounts" in container
    assert {m["name"] for m in container["volumeMounts"]} == {"tmp"}


def test_no_secret_objects_and_config_map_holds_no_secrets() -> None:
    for docs in (default_docs(), full_docs()):
        assert not of_kind(docs, "Secret")
        for config_map in of_kind(docs, "ConfigMap"):
            for key in config_map.get("data", {}):
                if not key.endswith(".sh"):
                    assert not _SECRET_NAME.search(key), key


@pytest.mark.parametrize(
    "key", ["PERFIN_MASTER_KEY", "PERFIN_SMTP_PASSWORD", "PERFIN_DATABASE_URL", "PERFIN_MCP_TOKEN"]
)
def test_secret_shaped_config_is_refused(key: str) -> None:
    result = render_raw(CI_VALUES, sets=(f"config.{key}=synthetic",))
    assert result.returncode != 0
    assert key in result.stderr


@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (("secrets.existingSecret=",), "secrets.existingSecret is required"),
        (("config.PERFIN_PUBLIC_URL=http://perfin.example.com",), "must be an https://"),
        (("config.PERFIN_TRUSTED_PROXIES=0.0.0.0/0",), "must not contain 0.0.0.0/0"),
        (("ingress.enabled=true", "ingress.host=perfin.example.com"), "tlsSecretName is required"),
        (("externalDatabase.networkPeers=null",), "externalDatabase.networkPeers is required"),
        (("mcp.enabled=true",), "mcp.existingSecret is required"),
        (("postgresql.enabled=true",), "postgresql.existingSecret is required"),
        (("config.PERFIN_ROLE=api",), "set by the chart per role"),
    ],
)
def test_insecure_or_incomplete_values_are_refused(sets: tuple[str, ...], message: str) -> None:
    result = render_raw(CI_VALUES, sets=sets)
    assert result.returncode != 0
    assert message in result.stderr


def test_migrate_hook_runs_before_upgrades_as_owner() -> None:
    job = workload(default_docs(), "migrate")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert pod_spec(job)["containers"][0]["args"] == ["migrate"]
    bundled = workload(full_docs(), "migrate")
    # The bundled database does not exist before the first install.
    assert bundled["metadata"]["annotations"]["helm.sh/hook"] == "post-install,pre-upgrade"


def test_bundled_database_is_scram_only_with_the_db_init_scripts() -> None:
    db = workload(full_docs(), "db")
    container = pod_spec(db)["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert env["POSTGRES_HOST_AUTH_METHOD"]["value"] == "scram-sha-256"
    assert "--auth-local=scram-sha-256" in env["POSTGRES_INITDB_ARGS"]["value"]
    assert "password_encryption=scram-sha-256" in container["args"]
    for key in ("POSTGRES_PASSWORD", "POSTGRES_OWNER_PASSWORD", "POSTGRES_APP_PASSWORD"):
        assert env[key]["valueFrom"]["secretKeyRef"]["name"] == "perfin-db"
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts["/docker-entrypoint-initdb.d"]["readOnly"] is True
    config_maps = of_kind(full_docs(), "ConfigMap")
    (init,) = [c for c in config_maps if c["metadata"]["name"].endswith("db-init")]
    assert set(init["data"]) == {"10-perfin-roles.sh", "20-perfin-superuser-local.sh"}


def test_documents_survive_uninstall() -> None:
    for claim in of_kind(default_docs(), "PersistentVolumeClaim"):
        assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_dropfolder_inbox_is_read_only() -> None:
    spec = pod_spec(workload(full_docs(), "worker"))
    mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert mounts["inbox"]["readOnly"] is True
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["inbox"]["persistentVolumeClaim"]["readOnly"] is True


# ---- H2: network isolation ----------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"networkPolicy": {"ingressFrom": []}}, "networkPolicy.ingressFrom must list"),
        ({"networkPolicy": {"ingressFrom": None}}, "networkPolicy.ingressFrom must list"),
        (
            {"networkPolicy": {"ingressFrom": []}, "mcp": {"enabled": True}},
            "networkPolicy.ingressFrom must list",
        ),
        ({"networkPolicy": {"ingressFrom": [{}]}}, "networkPolicy.ingressFrom[0] must be"),
        ({"networkPolicy": {"dns": {"peers": []}}}, "networkPolicy.dns.peers must list"),
        ({"networkPolicy": {"dns": {"peers": None}}}, "networkPolicy.dns.peers must list"),
        (
            {"networkPolicy": {"egressAllowlist": {"worker": [{"ports": [{"port": 443}]}]}}},
            "networkPolicy.egressAllowlist.worker[0].to must list",
        ),
        (
            {"networkPolicy": {"egressAllowlist": {"api": [{"to": [], "ports": [{"port": 443}]}]}}},
            "networkPolicy.egressAllowlist.api[0].to must list",
        ),
        ({"externalDatabase": {"networkPeers": [{}]}}, "externalDatabase.networkPeers[0] must be"),
    ],
    ids=[
        "ingress-empty",
        "ingress-null",
        "ingress-empty-with-mcp",
        "ingress-empty-peer",
        "dns-empty",
        "dns-null",
        "allowlist-without-to",
        "allowlist-empty-to",
        "db-empty-peer",
    ],
)
def test_empty_peer_lists_are_refused(tmp_path: Path, values: dict[str, Any], message: str) -> None:
    """An empty or null NetworkPolicy peer list matches everything (P7-SR-R1-01)."""
    override = tmp_path / "override.yaml"
    override.write_text(yaml.safe_dump(values), encoding="utf-8")
    result = render_raw(CI_VALUES, override)
    assert result.returncode != 0, "the chart rendered a fail-open NetworkPolicy"
    assert message in result.stderr


def test_no_rendered_rule_has_an_empty_peer_list() -> None:
    for docs in (default_docs(), full_docs()):
        for doc in of_kind(docs, "NetworkPolicy"):
            spec = doc["spec"]
            for direction, key in (("ingress", "from"), ("egress", "to")):
                for rule in spec.get(direction) or []:
                    assert rule.get(key), (doc["metadata"]["name"], direction, rule)
                    assert all(isinstance(peer, dict) and peer for peer in rule[key])


def test_default_deny_covers_every_pod_of_the_release() -> None:
    for docs in (default_docs(), full_docs()):
        deny = policy(docs, "perfin-default-deny")
        release_labels = {
            "app.kubernetes.io/name": "perfin",
            "app.kubernetes.io/instance": "perfin",
        }
        assert deny["spec"]["podSelector"] == {"matchLabels": release_labels}
        assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]
        assert "ingress" not in deny["spec"]
        assert "egress" not in deny["spec"]
        for doc in workloads(docs):
            labels = doc["spec"]["template"]["metadata"]["labels"]
            assert labels["app.kubernetes.io/instance"] == "perfin"
            assert labels["app.kubernetes.io/name"] == "perfin"


def test_worker_has_no_ingress_and_egress_only_to_dns_and_db() -> None:
    for docs, db_peer in (
        (default_docs(), {"ipBlock": {"cidr": "10.0.12.5/32"}}),
        (full_docs(), {"podSelector": {"matchLabels": component_selector("db")}}),
    ):
        worker = policy(docs, "perfin-worker")["spec"]
        assert worker["podSelector"] == {"matchLabels": component_selector("worker")}
        assert worker["ingress"] == []
        assert worker["egress"] == [
            {
                "to": [DNS_PEER],
                "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
            },
            {"to": [db_peer], "ports": [{"protocol": "TCP", "port": 5432}]},
        ]


def test_worker_egress_allowlist_is_appended_verbatim() -> None:
    rule = {
        "to": [{"ipBlock": {"cidr": "198.51.100.7/32"}}],
        "ports": [{"protocol": "TCP", "port": 993}],
    }
    docs = render(
        CI_VALUES,
        sets=(
            "networkPolicy.egressAllowlist.worker[0].to[0].ipBlock.cidr=198.51.100.7/32",
            "networkPolicy.egressAllowlist.worker[0].ports[0].protocol=TCP",
            "networkPolicy.egressAllowlist.worker[0].ports[0].port=993",
        ),
    )
    egress = policy(tuple(docs), "perfin-worker")["spec"]["egress"]
    assert egress[-1] == rule
    assert len(egress) == 3


def test_mcp_reaches_only_the_api() -> None:
    mcp = policy(full_docs(), "perfin-mcp")["spec"]
    egress_targets = [peer for rule in mcp["egress"] for peer in rule["to"]]
    assert egress_targets == [DNS_PEER, {"podSelector": {"matchLabels": component_selector("api")}}]
    assert mcp["egress"][1]["ports"] == [{"protocol": "TCP", "port": 8000}]
    for rule in mcp["egress"]:
        for peer in rule["to"]:
            assert (
                peer.get("podSelector", {})
                .get("matchLabels", {})
                .get("app.kubernetes.io/component")
                != "db"
            )


def test_database_reachable_only_from_app_pods() -> None:
    db = policy(full_docs(), "perfin-db")["spec"]
    assert db["egress"] == []
    (rule,) = db["ingress"]
    sources = {
        peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for peer in rule["from"]
    }
    assert sources == {"api", "worker", "migrate"}
    assert all(set(peer) == {"podSelector"} for peer in rule["from"])
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]
    services = of_kind(full_docs(), "Service")
    (service,) = [s for s in services if s["metadata"]["name"].endswith("-db")]
    assert service["spec"]["type"] == "ClusterIP"


def test_migrate_egress_only_to_dns_and_db() -> None:
    migrate = policy(default_docs(), "perfin-migrate")["spec"]
    assert migrate["ingress"] == []
    assert len(migrate["egress"]) == 2
    assert migrate["egress"][1]["to"] == [{"ipBlock": {"cidr": "10.0.12.5/32"}}]


def test_api_ingress_only_from_the_ingress_controller_and_mcp() -> None:
    api = policy(full_docs(), "perfin-api")["spec"]
    (rule,) = api["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]
    assert {"podSelector": {"matchLabels": component_selector("mcp")}} in rule["from"]
    assert len(rule["from"]) == 2
    assert {s["spec"]["type"] for s in of_kind(full_docs(), "Service")} == {"ClusterIP"}


def test_ingress_is_tls_only() -> None:
    (ingress,) = of_kind(full_docs(), "Ingress")
    assert ingress["spec"]["tls"] == [{"hosts": ["perfin.example.com"], "secretName": "perfin-tls"}]
