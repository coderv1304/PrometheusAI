"""Idempotent execution of approved actions.

Idempotency key = sha256(incident_id + action_hash). The ledger is durable so a
crash between apply and graph checkpoint cannot apply the same change twice.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Protocol

from prometheus_ai.models import ActionRequest, DryRunResult, ExecutionResult
from prometheus_ai.util import sha256_text, utcnow


class ExecutionLedger(Protocol):
    def already_executed(self, key: str) -> bool: ...

    def record(self, key: str, incident_id: str, action_hash: str, result: str) -> None: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def already_executed(self, key: str) -> bool:
        return key in self._keys

    def record(self, key: str, incident_id: str, action_hash: str, result: str) -> None:
        self._keys.add(key)


class SqliteLedger:
    """Durable single-node ledger. The unique key is the safety invariant."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    idempotency_key TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    action_hash TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def already_executed(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM executions WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return row is not None

    def record(self, key: str, incident_id: str, action_hash: str, result: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO executions "
                "(idempotency_key, incident_id, action_hash, result, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, incident_id, action_hash, result, utcnow().isoformat()),
            )


class CommandRunner(Protocol):
    async def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]: ...


async def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        timeout=timeout,
    )


class KubectlExecutor:
    """Builds argv lists only; there is no shell, so names cannot inject commands."""

    def __init__(
        self,
        ledger: ExecutionLedger,
        *,
        dry_run: bool = True,
        timeout_seconds: float = 30.0,
        kubectl: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.kubectl = kubectl or shutil.which("kubectl") or "kubectl"

    def idempotency_key(self, incident_id: str, action_hash: str) -> str:
        return sha256_text(f"{incident_id}:{action_hash}")

    def build_argv(self, action: ActionRequest, *, dry_run: bool) -> list[str]:
        argv = [self.kubectl, "-n", action.namespace]
        target = f"deployment/{action.target_name}"
        if action.action == "rollout_restart":
            argv += ["rollout", "restart", target]
        elif action.action == "rollout_undo":
            argv += ["rollout", "undo", target]
        elif action.action == "scale":
            argv += ["scale", target, f"--replicas={int(action.parameters['replicas'])}"]
        else:  # pragma: no cover - policy engine rejects this first
            raise ValueError(f"refusing to build command for {action.action!r}")
        if dry_run:
            argv.append("--dry-run=server")
        return argv

    async def preview(
        self, *, action: ActionRequest, runner: CommandRunner | None = None
    ) -> DryRunResult:
        """Ask the API server to validate the action without persisting anything.

        This never consults or writes the idempotency ledger: a preview is a
        read-equivalent preflight, and its failure must not consume the one
        allowed attempt for an incident.
        """
        runner = runner or _run_process
        argv = self.build_argv(action, dry_run=True)
        try:
            proc = await runner(argv, self.timeout_seconds)
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            return DryRunResult(
                action_hash=action.action_hash,
                ok=False,
                command=argv,
                error=f"{type(exc).__name__}: {exc}",
            )
        stdout = _decode(proc.stdout)
        stderr = _decode(proc.stderr)
        ok = proc.returncode == 0
        return DryRunResult(
            action_hash=action.action_hash,
            ok=ok,
            command=argv,
            stdout=stdout,
            stderr=stderr,
            error="" if ok else (stderr.strip()[:2000] or f"kubectl exited {proc.returncode}"),
        )

    async def execute(
        self, *, incident_id: str, action: ActionRequest, runner: CommandRunner | None = None
    ) -> ExecutionResult:
        key = self.idempotency_key(incident_id, action.action_hash)
        if self.ledger.already_executed(key):
            return ExecutionResult(
                action_hash=action.action_hash,
                status="skipped_duplicate",
                idempotency_key=key,
                stdout="idempotency key already recorded; no write issued",
                finished_at=utcnow(),
            )

        runner = runner or _run_process
        dry = self.build_argv(action, dry_run=True)
        try:
            preview = await runner(dry, self.timeout_seconds)
            if preview.returncode != 0:
                return ExecutionResult(
                    action_hash=action.action_hash,
                    status="failed",
                    command=dry,
                    stdout=_decode(preview.stdout),
                    stderr=_decode(preview.stderr),
                    idempotency_key=key,
                    finished_at=utcnow(),
                )
            if self.dry_run:
                return ExecutionResult(
                    action_hash=action.action_hash,
                    status="dry_run",
                    command=dry,
                    stdout=_decode(preview.stdout),
                    stderr=_decode(preview.stderr),
                    idempotency_key=key,
                    finished_at=utcnow(),
                )

            real = self.build_argv(action, dry_run=False)
            applied = await runner(real, self.timeout_seconds)
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            return ExecutionResult(
                action_hash=action.action_hash,
                status="failed",
                idempotency_key=key,
                stderr=str(exc),
                finished_at=utcnow(),
            )

        result = ExecutionResult(
            action_hash=action.action_hash,
            status="executed" if applied.returncode == 0 else "failed",
            command=real,
            stdout=_decode(applied.stdout),
            stderr=_decode(applied.stderr),
            idempotency_key=key,
            finished_at=utcnow(),
        )
        if result.status == "executed":
            self.ledger.record(key, incident_id, action.action_hash, result.status)
        return result


async def _run_process(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"command timed out after {timeout}s") from None
    return subprocess.CompletedProcess(
        argv, proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    )


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
