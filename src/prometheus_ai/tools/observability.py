"""Prometheus and Loki read tools.

Both are time-bounded, result-capped and return text that is marked untrusted:
log lines and label values are attacker-influenced data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from prometheus_ai.models import EvidenceRequest, EvidenceSource
from prometheus_ai.tools.base import ToolContext, ToolError, ToolOutput


class PrometheusTool:
    name = "prometheus_query"
    source = EvidenceSource.METRICS

    def __init__(self, base_url: str, *, timeout: float = 10.0, max_series: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_series = max_series

    async def run(self, request: EvidenceRequest, ctx: ToolContext) -> ToolOutput:
        query = request.query.strip()
        if not query or len(query) > 2000:
            raise ToolError("invalid_query", "PromQL must be 1-2000 characters")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query", params={"query": query}
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise ToolError("prometheus_unavailable", str(exc)) from exc

        if payload.get("status") != "success":
            raise ToolError("prometheus_error", str(payload.get("error", "unknown")))

        result = payload.get("data", {}).get("result", [])[: self.max_series]
        values: list[float] = []
        for series in result:
            value = series.get("value")
            if value:
                try:
                    values.append(float(value[1]))
                except (TypeError, ValueError):
                    continue
        peak = max(values) if values else None
        summary = f"{len(result)} series returned"
        if peak is not None:
            summary += f", peak value {peak:.4g}"
        return ToolOutput(
            summary=summary,
            detail=json.dumps(result, indent=2)[:6000],
            untrusted=True,
        )


class LokiTool:
    name = "loki_logs"
    source = EvidenceSource.LOGS

    def __init__(self, base_url: str, *, timeout: float = 10.0, max_lines: int = 80) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_lines = max_lines

    async def run(self, request: EvidenceRequest, ctx: ToolContext) -> ToolOutput:
        selector = request.query.strip()
        if not selector.startswith("{"):
            raise ToolError("invalid_selector", "Loki selector must start with '{'")
        minutes = min(max(int(request.params.get("minutes", "15")), 1), 60)
        end = datetime.now(UTC)
        start = end - timedelta(minutes=minutes)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/loki/api/v1/query_range",
                    params={
                        "query": selector,
                        "start": f"{int(start.timestamp() * 1e9)}",
                        "end": f"{int(end.timestamp() * 1e9)}",
                        "limit": self.max_lines,
                        "direction": "backward",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise ToolError("loki_unavailable", str(exc)) from exc

        streams = payload.get("data", {}).get("result", [])
        lines: list[str] = []
        for stream in streams:
            for _ts, line in stream.get("values", []):
                lines.append(line)
        lines = lines[: self.max_lines]
        return ToolOutput(
            summary=f"{len(lines)} log lines from the last {minutes}m",
            detail="\n".join(lines) or "(no log lines matched)",
            untrusted=True,
        )
