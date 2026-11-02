"""Build a tool registry appropriate for the configured backend."""

from __future__ import annotations

from pathlib import Path

from prometheus_ai.config import Settings
from prometheus_ai.tools.base import ToolRegistry
from prometheus_ai.tools.kubectl import KubectlReadTool
from prometheus_ai.tools.observability import LokiTool, PrometheusTool
from prometheus_ai.tools.runbooks import RunbookSearchTool
from prometheus_ai.tools.synthetic import (
    SyntheticKubectlTool,
    SyntheticLokiTool,
    SyntheticPrometheusTool,
)


def build_registry(settings: Settings, *, runbooks_dir: str | Path | None = None) -> ToolRegistry:
    runbooks = RunbookSearchTool(runbooks_dir or Path("docs/runbooks"))
    if settings.model_backend == "fake":
        tools = [
            SyntheticPrometheusTool(),
            SyntheticLokiTool(),
            SyntheticKubectlTool(),
            runbooks,
        ]
    else:
        tools = [
            PrometheusTool(settings.prometheus_url),
            LokiTool(settings.loki_url),
            KubectlReadTool(),
            runbooks,
        ]
    return ToolRegistry(
        tools,
        max_calls_per_incident=settings.max_tool_calls,
        allowed_namespaces=set(settings.allowed_namespaces),
        timeout_seconds=settings.http_timeout_seconds,
    )
