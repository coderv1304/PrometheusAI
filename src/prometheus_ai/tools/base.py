"""Typed read-only tools and the budget-enforcing registry.

The registry is the only path from the agent to a tool. It enforces the hard
per-incident call budget, suppresses duplicate calls, applies timeouts, marks
output as untrusted and redacts before anything is stored or sent to a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from prometheus_ai.llm.redact import redact
from prometheus_ai.models import Evidence, EvidenceRequest, EvidenceSource
from prometheus_ai.util import new_id, sha256_json, sha256_text, truncate


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolContext:
    incident_id: str
    namespace: str
    service: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutput:
    summary: str
    detail: str
    untrusted: bool


class ReadTool(Protocol):
    name: str
    source: EvidenceSource

    async def run(self, request: EvidenceRequest, ctx: ToolContext) -> ToolOutput: ...


AuditHook = Callable[..., None]


class ToolRegistry:
    def __init__(
        self,
        tools: list[ReadTool],
        *,
        max_calls_per_incident: int = 6,
        allowed_namespaces: set[str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._max_calls = max_calls_per_incident
        self._allowed_namespaces = allowed_namespaces or {"demo"}
        self._timeout = timeout_seconds
        self._counts: dict[str, int] = {}
        self._seen: dict[str, set[str]] = {}
        self._cache: dict[str, Evidence] = {}

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def calls_used(self, incident_id: str) -> int:
        return self._counts.get(incident_id, 0)

    def remaining(self, incident_id: str) -> int:
        return max(0, self._max_calls - self.calls_used(incident_id))

    async def collect(
        self, requests: list[EvidenceRequest], ctx: ToolContext
    ) -> list[Evidence]:
        results: list[Evidence] = []
        for request in requests:
            if self.remaining(ctx.incident_id) <= 0:
                results.append(
                    self._error_evidence(
                        request, "budget_exhausted", "tool-call budget reached (max 6)"
                    )
                )
                continue
            results.append(await self.call(request, ctx))
        return results

    async def call(self, request: EvidenceRequest, ctx: ToolContext) -> Evidence:
        tool = self._tools.get(request.tool)
        if tool is None:
            return self._error_evidence(request, "unknown_tool", f"unknown tool {request.tool!r}")

        if request.tool == "kubectl_read":
            ns = request.params.get("namespace", ctx.namespace)
            if ns not in self._allowed_namespaces or ns != ctx.namespace:
                return self._error_evidence(
                    request, "namespace_denied", f"namespace {ns!r} denied for kubectl_read"
                )

        key = sha256_json(
            {"incident": ctx.incident_id, "tool": request.tool, "query": request.query,
             "params": request.params}
        )
        if key in self._seen.setdefault(ctx.incident_id, set()):
            cached = self._cache.get(key)
            if cached is not None:
                return cached.model_copy(
                    update={"summary": f"{cached.summary} (duplicate call suppressed)"}
                )
            return self._error_evidence(request, "duplicate_call", "duplicate call suppressed")
        self._seen[ctx.incident_id].add(key)

        self._counts[ctx.incident_id] = self.calls_used(ctx.incident_id) + 1
        try:
            output = await asyncio.wait_for(tool.run(request, ctx), timeout=self._timeout)
        except TimeoutError:
            evidence = self._error_evidence(
                request, "tool_timeout", f"{request.tool} timed out after {self._timeout}s"
            )
        except ToolError as exc:
            evidence = self._error_evidence(request, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - tool failure must not crash the graph
            evidence = self._error_evidence(request, "tool_error", f"{type(exc).__name__}: {exc}")
        else:
            detail = truncate(redact(output.detail).text, 6000)
            evidence = Evidence(
                id=new_id("ev"),
                source=tool.source,
                query=request.query,
                summary=truncate(redact(output.summary).text, 500),
                detail=detail,
                untrusted=output.untrusted,
                tool_call_id=key[:16],
                raw_hash=sha256_text(output.detail),
            )
        self._cache[key] = evidence
        return evidence

    def _error_evidence(
        self, request: EvidenceRequest, code: str, message: str
    ) -> Evidence:
        return Evidence(
            id=new_id("ev"),
            source=EvidenceSource.KUBERNETES
            if request.tool == "kubectl_read"
            else EvidenceSource.METRICS,
            query=request.query,
            summary=f"{code}: {message}",
            detail=f"tool_error code={code} message={message}",
            untrusted=False,
            tool_call_id=sha256_json(request.model_dump())[:16],
            raw_hash=sha256_text(message),
        )
