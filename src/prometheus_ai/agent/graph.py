"""LangGraph wiring.

    receive -> triage -> hypothesize <-> gather_evidence
            -> propose_fix -> [interrupt: approval_gate]
            -> policy_check -> execute -> verify -> postmortem -> END
    reject / denial / failure / verify-failure -> escalate -> postmortem -> END

The interrupt is a real graph interrupt: the checkpoint is the source of truth,
and resuming requires a decision carrying the proposal's action hash.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from prometheus_ai.agent import nodes
from prometheus_ai.agent.nodes import GraphDeps
from prometheus_ai.agent.state import IncidentState
from prometheus_ai.config import Settings
from prometheus_ai.models import Incident
from prometheus_ai.util import utcnow


def _guarded(fn, deps: GraphDeps, node_name: str):
    """Wall-clock bound per node; a timeout escalates instead of hanging."""

    async def wrapper(state: IncidentState) -> dict:
        try:
            return await asyncio.wait_for(
                fn(state, deps), timeout=deps.settings.node_timeout_seconds
            )
        except TimeoutError:
            incident = await deps.repo.get_incident(state["incident_id"])
            if incident is not None:
                await deps.repo.save_incident(
                    incident.model_copy(
                        update={
                            "status": "escalated",
                            "error": f"node {node_name} timed out after "
                            f"{deps.settings.node_timeout_seconds}s",
                            "updated_at": utcnow(),
                        }
                    )
                )
            return {
                "status": "escalated",
                "error": f"node {node_name} timed out",
            }

    return wrapper


def build_graph(deps: GraphDeps, checkpointer: Any | None = None):
    graph = StateGraph(IncidentState)

    graph.add_node("triage", _guarded(nodes.triage, deps, "triage"))
    graph.add_node("hypothesize", _guarded(nodes.hypothesize, deps, "hypothesize"))
    graph.add_node("gather_evidence", _guarded(nodes.gather_evidence, deps, "gather_evidence"))
    graph.add_node("propose_fix", _guarded(nodes.propose_fix, deps, "propose_fix"))
    graph.add_node("approval_gate", _guarded(nodes.approval_gate, deps, "approval_gate"))
    graph.add_node("policy_check", _guarded(nodes.policy_check, deps, "policy_check"))
    graph.add_node("execute", _guarded(nodes.execute, deps, "execute"))
    graph.add_node("verify", _guarded(nodes.verify, deps, "verify"))
    graph.add_node("escalate", _guarded(nodes.escalate, deps, "escalate"))
    graph.add_node("postmortem", _guarded(nodes.postmortem, deps, "postmortem"))

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "hypothesize")
    graph.add_edge("gather_evidence", "hypothesize")

    graph.add_conditional_edges(
        "hypothesize",
        lambda state: _route_after_hypothesis(state, deps),
        {
            "gather_evidence": "gather_evidence",
            "propose_fix": "propose_fix",
            "escalate": "escalate",
        },
    )
    graph.add_conditional_edges(
        "propose_fix",
        lambda state: "escalate"
        if state.get("status") == "escalated"
        else "approval_gate",
        {"approval_gate": "approval_gate", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "approval_gate",
        lambda state: (
            "policy_check"
            if (state.get("approval") or {}).get("decision") == "approve"
            else "escalate"
        ),
        {"policy_check": "policy_check", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "policy_check",
        lambda state: "execute" if (state.get("policy") or {}).get("allowed") else "escalate",
        {"execute": "execute", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "execute",
        lambda state: (
            "verify"
            if (state.get("execution") or {}).get("status")
            in {"executed", "dry_run", "skipped_duplicate"}
            else "escalate"
        ),
        {"verify": "verify", "escalate": "escalate"},
    )
    graph.add_edge("verify", "postmortem")
    graph.add_edge("escalate", "postmortem")
    graph.add_edge("postmortem", END)

    return graph.compile(checkpointer=checkpointer)


def _route_after_hypothesis(state: IncidentState, deps: GraphDeps) -> str:
    if state.get("status") == "escalated":
        return "escalate"
    hypothesis = state.get("hypothesis") or {}
    queries = hypothesis.get("next_queries") or []
    if not queries:
        return "propose_fix"
    if int(state.get("rounds", 0)) >= deps.settings.max_rehypothesis:
        return "propose_fix"
    if int(state.get("new_calls", 1)) <= 0:
        return "propose_fix"
    if deps.registry.remaining(state["incident_id"]) <= 0:
        return "propose_fix"
    return "gather_evidence"


class GraphRunner:
    def __init__(self, deps: GraphDeps, checkpointer: Any | None = None) -> None:
        self.deps = deps
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = build_graph(deps, self.checkpointer)

    def _config(self, incident_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": incident_id}}

    async def start(self, incident: Incident) -> dict[str, Any]:
        state: IncidentState = {
            "incident_id": incident.id,
            "alert": incident.model_dump(mode="json"),
            "evidence": [],
            "rounds": 0,
            "new_calls": 1,
            "tool_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "status": "received",
            "synthetic": self.deps.model_backend == "demo-deterministic",
        }
        return await self.graph.ainvoke(state, self._config(incident.id))

    async def resolve(
        self, incident_id: str, *, decision: str, approver: str, reason: str = ""
    ) -> dict[str, Any]:
        payload = {
            "decision": decision,
            "approver": approver,
            "reason": reason,
            "action_hash": await self._proposal_hash(incident_id),
        }
        return await self.graph.ainvoke(Command(resume=payload), self._config(incident_id))

    async def _proposal_hash(self, incident_id: str) -> str:
        incident = await self.deps.repo.get_incident(incident_id)
        if incident is None or incident.proposal is None:
            return ""
        return incident.proposal.action_hash

    async def snapshot(self, incident_id: str) -> dict[str, Any]:
        state = await self.graph.aget_state(self._config(incident_id))
        return state.values if state else {}

    @staticmethod
    def pending_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
        interrupts = result.get("__interrupt__") or ()
        if not interrupts:
            return None
        value = getattr(interrupts[0], "value", None)
        return value if isinstance(value, dict) else None


async def create_checkpointer(
    settings: Settings, stack: AsyncExitStack
) -> Any:
    if settings.checkpoint_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("checkpoint_backend=postgres requires PROMETHEUS_AI_DATABASE_URL")
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_url)
        )
        await saver.setup()
        return saver
    return InMemorySaver()
