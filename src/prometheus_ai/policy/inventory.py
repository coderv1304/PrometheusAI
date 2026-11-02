"""Cluster inventory used by the policy engine for live existence checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass
class FakeInventory:
    """In-memory inventory for tests and the demo world."""

    deployments: dict[tuple[str, str], int] = field(default_factory=dict)

    def deployment_exists(self, namespace: str, name: str) -> bool:
        return (namespace, name) in self.deployments

    def replicas(self, namespace: str, name: str) -> int | None:
        return self.deployments.get((namespace, name))

    def add(self, namespace: str, name: str, replicas: int = 1) -> None:
        self.deployments[(namespace, name)] = replicas


class KubectlInventory:
    """Read-only `kubectl get` lookup with a hard timeout and no shell."""

    def __init__(self, *, timeout_seconds: float = 5.0, kubectl: str | None = None) -> None:
        self.kubectl = kubectl or shutil.which("kubectl") or "kubectl"
        self.timeout_seconds = timeout_seconds
        self._cache: dict[tuple[str, str], bool] = {}

    def deployment_exists(self, namespace: str, name: str) -> bool:
        key = (namespace, name)
        if key in self._cache:
            return self._cache[key]
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                [self.kubectl, "get", "deployment", name, "-n", namespace, "-o", "json"],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0:
            self._cache[key] = False
            return False
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False
        exists = payload.get("kind") == "Deployment" and payload.get("metadata", {}).get("name") == name
        self._cache[key] = exists
        return exists
