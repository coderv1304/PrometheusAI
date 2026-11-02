"""Read-only kubectl tool.

`kubectl get secrets` is not representable: operations are an enum, namespaces
are checked by the registry, and argv is built without a shell.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Literal

from prometheus_ai.models import EvidenceRequest, EvidenceSource
from prometheus_ai.tools.base import ToolContext, ToolError, ToolOutput
from prometheus_ai.util import is_safe_slug

Operation = Literal["get_pods", "describe", "events", "logs"]
_ALLOWED_OPS = {"get_pods", "describe", "events", "logs"}


class KubectlReadTool:
    name = "kubectl_read"
    source = EvidenceSource.KUBERNETES

    def __init__(
        self, *, timeout: float = 8.0, kubectl: str | None = None, max_chars: int = 6000
    ) -> None:
        self.kubectl = kubectl or shutil.which("kubectl") or "kubectl"
        self.timeout = timeout
        self.max_chars = max_chars

    def build_argv(
        self, request: EvidenceRequest, ctx: ToolContext
    ) -> list[str]:
        op = request.params.get("op", "get_pods")
        if op not in _ALLOWED_OPS:
            raise ToolError("operation_denied", f"operation {op!r} is not read-only allowlisted")
        target = request.params.get("name") or ctx.service
        if not is_safe_slug(target):
            raise ToolError("invalid_target", "target name is not a DNS-1123 label")

        if op == "get_pods":
            return [self.kubectl, "get", "pods", "-n", ctx.namespace, "-l", f"app={target}", "-o", "wide"]
        if op == "describe":
            return [self.kubectl, "describe", "deployment", target, "-n", ctx.namespace]
        if op == "events":
            return [self.kubectl, "get", "events", "-n", ctx.namespace, "--sort-by=.lastTimestamp"]
        return [self.kubectl, "logs", f"deployment/{target}", "-n", ctx.namespace, "--tail=80"]

    async def run(self, request: EvidenceRequest, ctx: ToolContext) -> ToolOutput:
        argv = self.build_argv(request, ctx)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError("kubectl_timeout", f"kubectl timed out after {self.timeout}s") from None
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        if proc.returncode != 0:
            raise ToolError("kubectl_failed", err.strip()[:500] or f"exit {proc.returncode}")
        lines = [line for line in out.splitlines() if line.strip()]
        return ToolOutput(
            summary=f"kubectl {request.params.get('op', 'get_pods')}: {len(lines)} lines",
            detail=out[: self.max_chars],
            untrusted=True,
        )
