"""Composition root: build the runtime from settings.

Synthetic mode (`model_backend=fake`) never touches a cluster. Every surface
labels it as synthetic so demo output cannot be mistaken for a real run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prometheus_ai.agent.graph import GraphRunner
from prometheus_ai.agent.nodes import GraphDeps
from prometheus_ai.agent.verify import LiveVerifier, SyntheticExecutor, SyntheticVerifier
from prometheus_ai.config import Settings
from prometheus_ai.llm.client import ModelClient, create_model
from prometheus_ai.policy import (
    FakeInventory,
    KubectlExecutor,
    KubectlInventory,
    PolicyEngine,
    SqliteLedger,
)
from prometheus_ai.policy.client import HttpPolicyClient, InProcessPolicyClient, PolicyClient
from prometheus_ai.services.alerts import AlertService, InvestigationManager
from prometheus_ai.storage.base import MemoryRepository, Repository
from prometheus_ai.storage.postgres import PostgresRepository
from prometheus_ai.tools.base import ToolRegistry
from prometheus_ai.tools.factory import build_registry


def _build_inventory(settings: Settings, *, synthetic: bool):
    backend = settings.inventory_backend
    use_fake = backend == "fake" or (backend == "auto" and synthetic)
    if use_fake:
        inventory = FakeInventory()
        for name in ("api", "web", "worker"):
            inventory.add(settings.demo_namespace, name, 1)
        return inventory
    return KubectlInventory()


def _build_local_engine(settings: Settings, *, synthetic: bool):
    inventory = _build_inventory(settings, synthetic=synthetic)
    engine = PolicyEngine(
        allowed_namespaces=set(settings.allowed_namespaces),
        allowed_actions=set(settings.allowed_actions),
        inventory=inventory,
        max_writes_per_hour=settings.max_writes_per_hour,
    )
    if isinstance(inventory, FakeInventory):
        executor = SyntheticExecutor()
        return engine, executor
    ledger_path = Path(".prometheus-ai") / "executions.db"
    executor = KubectlExecutor(
        SqliteLedger(ledger_path),
        dry_run=settings.dry_run,
    )
    return engine, executor


@dataclass
class Services:
    settings: Settings
    repo: Repository
    registry: ToolRegistry
    policy: PolicyClient
    model: ModelClient
    runner: GraphRunner
    manager: InvestigationManager
    alerts: AlertService
    synthetic: bool = False
    engine: PolicyEngine | None = None

    async def close(self) -> None:
        await self.manager.shutdown()
        await self.repo.close()


async def build_services(
    settings: Settings,
    *,
    checkpointer: Any | None = None,
    repo: Repository | None = None,
    runbooks_dir: str | Path | None = None,
    model_override: ModelClient | None = None,
) -> Services:
    synthetic = settings.model_backend == "fake"

    if repo is None:
        if settings.database_url:
            pg = PostgresRepository(settings.database_url)
            await pg.connect()
            repo = pg
        else:
            repo = MemoryRepository()

    registry = build_registry(settings, runbooks_dir=runbooks_dir)
    model = model_override or create_model(settings)

    engine: PolicyEngine | None = None
    if synthetic:
        engine = PolicyEngine(
            allowed_namespaces=set(settings.allowed_namespaces),
            allowed_actions=set(settings.allowed_actions),
            inventory=_build_inventory(settings, synthetic=True),
            max_writes_per_hour=settings.max_writes_per_hour,
        )
        policy: PolicyClient = _SyntheticPolicy(engine, SyntheticExecutor())
        verifier = SyntheticVerifier()
    elif settings.policy_mode == "http":
        # The agent process must not even construct a kubectl write executor:
        # no write credential, no local execution path, no accidental fallback.
        policy = HttpPolicyClient(settings.policy_url, settings.policy_token)
        verifier = LiveVerifier(settings)
    else:
        engine, executor = _build_local_engine(settings, synthetic=False)
        policy = InProcessPolicyClient(engine, executor)
        verifier = LiveVerifier(settings)

    deps = GraphDeps(
        settings=settings,
        repo=repo,
        model=model,
        registry=registry,
        policy=policy,
        verifier=verifier,
        model_backend=model.name,
    )
    runner = GraphRunner(deps, checkpointer=checkpointer)
    manager = InvestigationManager(runner, repo)
    alerts = AlertService(settings, repo, manager)

    return Services(
        settings=settings,
        repo=repo,
        registry=registry,
        policy=policy,
        model=model,
        runner=runner,
        manager=manager,
        alerts=alerts,
        synthetic=synthetic,
        engine=engine,
    )


class _SyntheticPolicy(InProcessPolicyClient):
    """In-process policy with a fake cluster executor; clearly demo-only."""

    def __init__(self, engine: PolicyEngine, executor: SyntheticExecutor) -> None:
        self.engine = engine
        self.executor = executor


def build_inprocess_engine(settings: Settings) -> tuple[PolicyEngine, Any]:
    """Used by the standalone policy service: this process owns the write credential.

    The policy service's inventory choice must not follow the *model* backend.
    A `fake` inventory is a test double; a deployed policy service always needs
    the live Kubernetes lookup so it can reject hallucinated targets.
    """
    return _build_local_engine(settings, synthetic=settings.inventory_backend == "fake")
