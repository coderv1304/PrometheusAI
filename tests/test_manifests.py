"""Static assertions over the Kubernetes manifests.

These are cheap and catch the controls that are easy to lose in a refactor:
default-deny networking, restricted Pod Security, the absence of cluster-admin,
the split between read and write credentials, hardened containers and pinned
images. They do not replace applying the manifests to a real cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "k8s" / "base"
RUNBOOKS = ROOT / "docs" / "runbooks"

NAMESPACES = {"prometheus-ai", "policy", "data", "demo", "monitoring"}
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}


def _load(path: Path) -> list[dict[str, Any]]:
    documents = []
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(document, dict) and document.get("kind"):
            documents.append(document)
    return documents


@pytest.fixture(scope="module")
def docs() -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(BASE.glob("*.yaml")):
        if path.name == "secrets.example.yaml":
            continue
        for document in _load(path):
            items.append((path.name, document))
    return items


def _by_kind(docs, kind: str) -> list[dict[str, Any]]:
    return [doc for _, doc in docs if doc.get("kind") == kind]


def _containers(doc: dict[str, Any]):
    spec = doc["spec"]["template"]["spec"]
    for key in ("initContainers", "containers"):
        yield from spec.get(key, [])


def test_namespaces_enforce_restricted_pod_security(docs) -> None:
    namespaces = {doc["metadata"]["name"]: doc for doc in _by_kind(docs, "Namespace")}
    assert set(namespaces) == NAMESPACES
    for name, ns in namespaces.items():
        labels = ns["metadata"]["labels"]
        assert labels["pod-security.kubernetes.io/enforce"] == "restricted", name
        assert labels["pod-security.kubernetes.io/audit"] == "restricted", name
        assert labels["pod-security.kubernetes.io/warn"] == "restricted", name


def test_every_namespace_has_a_default_deny_policy(docs) -> None:
    policies = _by_kind(docs, "NetworkPolicy")
    for namespace in NAMESPACES:
        matches = [
            policy
            for policy in policies
            if policy["metadata"]["namespace"] == namespace
            and policy["metadata"]["name"] == "default-deny"
        ]
        assert len(matches) == 1, f"{namespace} has no default-deny policy"
        spec = matches[0]["spec"]
        assert spec["podSelector"] == {}
        assert set(spec["policyTypes"]) == {"Ingress", "Egress"}


def test_no_cluster_admin_or_cluster_wide_binding(docs) -> None:
    for doc in _by_kind(docs, "ClusterRoleBinding"):
        assert doc["roleRef"]["name"] != "cluster-admin"
    for doc in _by_kind(docs, "ClusterRole"):
        assert doc["metadata"]["name"] != "cluster-admin"
        for rule in doc.get("rules", []):
            assert "*" not in rule.get("verbs", [])
            assert "*" not in rule.get("resources", [])


def test_agent_service_account_has_no_write_verbs(docs) -> None:
    roles = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
        for doc in _by_kind(docs, "Role")
    }
    bindings = [
        doc
        for doc in _by_kind(docs, "RoleBinding")
        if any(
            subject.get("kind") == "ServiceAccount"
            and subject.get("name") == "prometheus-ai-agent"
            and subject.get("namespace") == "prometheus-ai"
            for subject in doc.get("subjects", [])
        )
    ]
    assert bindings, "the agent has no RoleBinding; it could not even read"
    for binding in bindings:
        role = roles[(binding["metadata"]["namespace"], binding["roleRef"]["name"])]
        for rule in role.get("rules", []):
            assert not (set(rule.get("verbs", [])) & WRITE_VERBS), rule
            assert "*" not in rule.get("resources", [])


def test_policy_writer_is_narrowly_scoped(docs) -> None:
    roles = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
        for doc in _by_kind(docs, "Role")
    }
    writer = roles[("demo", "remediation-writer")]
    for rule in writer["rules"]:
        verbs = set(rule["verbs"])
        assert not (verbs & {"create", "delete", "deletecollection"}), rule
        assert "*" not in rule.get("resources", [])
        assert "*" not in rule.get("apiGroups", [])
        if "deployments" in rule["resources"]:
            assert set(rule["resourceNames"]) == {"api", "web", "worker"}
            assert verbs <= {"get", "patch", "update"}
        if "deployments/scale" in rule["resources"]:
            assert set(rule["resourceNames"]) == {"api", "web", "worker"}
            assert verbs <= {"get", "patch", "update"}


def test_images_are_pinned(docs) -> None:
    for kind in ("Deployment", "StatefulSet", "DaemonSet"):
        for doc in _by_kind(docs, kind):
            for container in _containers(doc):
                image = container["image"]
                assert "@sha256:" in image or (
                    ":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest")
                ), f"unpinned image {image}"


def test_containers_run_hardened(docs) -> None:
    for kind in ("Deployment", "StatefulSet", "DaemonSet"):
        for doc in _by_kind(docs, kind):
            name = doc["metadata"]["name"]
            pod_spec = doc["spec"]["template"]["spec"]
            pod_security = pod_spec.get("securityContext", {})
            assert pod_security.get("runAsNonRoot") is True, name
            assert pod_security.get("seccompProfile", {}).get("type") == "RuntimeDefault", name
            assert "hostNetwork" not in pod_spec, name
            assert "hostPID" not in pod_spec, name
            for container in _containers(doc):
                security = container.get("securityContext", {})
                assert security.get("allowPrivilegeEscalation") is False, name
                assert security.get("readOnlyRootFilesystem") is True, name
                assert security.get("privileged") is not True, name
                assert "ALL" in security.get("capabilities", {}).get("drop", []), name
            for volume in pod_spec.get("volumes", []):
                assert "hostPath" not in volume, name


def test_no_plaintext_secret_is_part_of_the_bundle(docs) -> None:
    assert not _by_kind(docs, "Secret")
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "secrets.example.yaml" not in kustomization["resources"]


def test_kustomization_references_existing_files() -> None:
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text(encoding="utf-8"))
    for resource in kustomization["resources"]:
        assert (BASE / resource).is_file(), resource


def test_agent_uses_the_remote_policy_service(docs) -> None:
    configmaps = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
        for doc in _by_kind(docs, "ConfigMap")
    }
    agent = configmaps[("prometheus-ai", "agent-config")]["data"]
    assert agent["PROMETHEUS_AI_POLICY_MODE"] == "http"
    assert agent["PROMETHEUS_AI_POLICY_URL"].startswith("http://prometheus-ai-policy.policy")
    assert agent["PROMETHEUS_AI_CHECKPOINT_BACKEND"] == "postgres"
    # The database URL is a credential and belongs in the Secret, not here.
    assert "PROMETHEUS_AI_DATABASE_URL" not in agent


def test_policy_service_uses_live_inventory(docs) -> None:
    configmaps = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
        for doc in _by_kind(docs, "ConfigMap")
    }
    policy = configmaps[("policy", "policy-config")]["data"]
    assert policy["PROMETHEUS_AI_INVENTORY_BACKEND"] == "kubectl"
    assert policy["PROMETHEUS_AI_ENABLE_DOCS"] == "false"


def test_runbooks_configmap_is_in_sync(docs) -> None:
    configmaps = [
        doc
        for doc in _by_kind(docs, "ConfigMap")
        if doc["metadata"]["name"] == "runbooks"
    ]
    assert len(configmaps) == 1
    rendered = configmaps[0]["data"]
    expected = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(RUNBOOKS.glob("*.md"))
    }
    assert set(rendered) == set(expected)
    for name, text in expected.items():
        assert rendered[name] == text, f"{name} is stale; rerun the generator"


def test_network_policies_have_explicit_rules(docs) -> None:
    for policy in _by_kind(docs, "NetworkPolicy"):
        if policy["metadata"]["name"] == "default-deny":
            continue
        spec = policy["spec"]
        assert spec.get("ingress") or spec.get("egress"), policy["metadata"]["name"]
