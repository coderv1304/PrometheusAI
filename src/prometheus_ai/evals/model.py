"""Deterministic scripted model for T1.

These are hand-authored fixtures, not recorded model output. T1 exists to test
the harness, routing, policy gate and approval binding — not model quality.
"""

from __future__ import annotations

from prometheus_ai.evals.scenarios import Scenario
from prometheus_ai.llm.client import ModelResponse
from prometheus_ai.models import Hypothesis, Proposal


class ScriptedModel:
    name = "scripted-t1"

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls = 0

    async def triage(self, incident, repair_note: str | None = None) -> ModelResponse:
        self.calls += 1
        queries = self.scenario.queries if self.calls == 1 else []
        return ModelResponse(
            hypothesis=Hypothesis(
                category=self.scenario.hypothesis,
                confidence=self.scenario.hypothesis_confidence,
                reasoning=self.scenario.hypothesis_reasoning,
                next_queries=list(queries),
            ),
            tokens_in=100,
            tokens_out=50,
        )

    async def propose(self, incident, repair_note: str | None = None) -> ModelResponse:
        spec = self.scenario.proposal
        proposal = Proposal(
            action=spec.action,
            namespace=spec.namespace,
            target_kind=spec.target_kind,
            target_name=spec.target_name,
            parameters=spec.parameters,
            rationale=spec.rationale,
            confidence=spec.confidence,
            rollback=spec.rollback,
        ).with_hash()
        return ModelResponse(proposal=proposal, tokens_in=120, tokens_out=80)


class MalformedModel:
    """Always returns invalid output; used to prove the repair/escalation path."""

    name = "malformed-t1"

    async def triage(self, incident, repair_note: str | None = None) -> ModelResponse:
        return ModelResponse(error="invalid_json", raw_text="{")

    async def propose(self, incident, repair_note: str | None = None) -> ModelResponse:
        return ModelResponse(error="schema_invalid", raw_text="{}")
