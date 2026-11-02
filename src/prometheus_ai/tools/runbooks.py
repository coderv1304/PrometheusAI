"""Local runbook search.

Deliberately simple and dependency-free for v1: heading chunks plus keyword
scoring. Every runbook is treated as untrusted (stale or tampered docs must not
be able to redirect the agent).
"""

from __future__ import annotations

import re
from pathlib import Path

from prometheus_ai.models import EvidenceRequest, EvidenceSource
from prometheus_ai.tools.base import ToolContext, ToolOutput

_TOKEN = re.compile(r"[a-z0-9]{3,}")


class RunbookSearchTool:
    name = "runbook_search"
    source = EvidenceSource.RUNBOOK

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._chunks = self._load()

    def _load(self) -> list[tuple[str, str, str]]:
        chunks: list[tuple[str, str, str]] = []
        if not self.directory.exists():
            return chunks
        for path in sorted(self.directory.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            heading = path.stem
            body: list[str] = []
            for line in text.splitlines():
                if line.startswith("## ") and body:
                    chunks.append((path.name, heading, "\n".join(body).strip()))
                    heading, body = line[3:].strip(), []
                elif line.startswith("# "):
                    continue
                else:
                    body.append(line)
            if body:
                chunks.append((path.name, heading, "\n".join(body).strip()))
        return chunks

    async def run(self, request: EvidenceRequest, ctx: ToolContext) -> ToolOutput:
        query_tokens = set(_TOKEN.findall(request.query.lower()))
        scored: list[tuple[float, str, str, str]] = []
        for source, heading, text in self._chunks:
            haystack = f"{heading} {text}".lower()
            doc_tokens = set(_TOKEN.findall(haystack))
            overlap = len(query_tokens & doc_tokens)
            if overlap:
                scored.append((overlap / max(len(query_tokens), 1), source, heading, text))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:2]
        if not top:
            return ToolOutput(summary="no runbook matched", detail="(none)", untrusted=True)
        detail = "\n\n".join(
            f"# {heading} ({source}, score={score:.2f})\n{text[:1800]}"
            for score, source, heading, text in top
        )
        return ToolOutput(
            summary=f"{len(top)} runbook section(s) matched",
            detail=detail,
            untrusted=True,
        )
