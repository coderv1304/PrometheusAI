"""Dependency and environment capture for `docs/VERSIONS.md`.

The lockfile remains the source of truth for transitives; this report records
what was actually installed when the artefact was built.
"""

from __future__ import annotations

import importlib.metadata as md
import platform

from prometheus_ai import __version__
from prometheus_ai.util import utcnow

_PACKAGES = (
    "fastapi",
    "httpx",
    "jinja2",
    "langgraph",
    "langgraph-checkpoint-postgres",
    "markdown",
    "psycopg",
    "pydantic",
    "pydantic-settings",
    "pyyaml",
    "uvicorn",
)


def collect_versions() -> dict:
    python_packages = []
    for name in _PACKAGES:
        try:
            python_packages.append({"name": name, "version": md.version(name)})
        except md.PackageNotFoundError:
            continue

    return {
        "prometheus_ai": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "python_packages": sorted(python_packages, key=lambda item: item["name"]),
    }


def render_versions_markdown(data: dict) -> str:
    lines = [
        "# Versions",
        "",
        f"- prometheus-ai: `{data['prometheus_ai']}`",
        f"- Python: `{data['python']}`",
        f"- Platform: `{data['platform']}`",
        f"- Generated: `{data['generated_at']}`",
        "",
        "## Python packages",
        "",
        "| Package | Version |",
        "| --- | --- |",
    ]
    for row in data["python_packages"]:
        lines.append(f"| {row['name']} | {row['version']} |")
    if not data["python_packages"]:
        lines.append("| (none detected) | - |")
    lines += [
        "",
        "> `uv.lock` is the source of truth for transitive dependencies. "
        "Run `prometheus-ai versions` after dependency changes.",
        "",
    ]
    return "\n".join(lines)
