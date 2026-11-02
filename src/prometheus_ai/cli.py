"""Command line entry points.

    prometheus-ai serve            run the API + approval console
    prometheus-ai demo             seed synthetic incidents and serve
    prometheus-ai seed             post demo alerts to a running instance
    prometheus-ai eval             deterministic tier (T1) and regression gate
    prometheus-ai policy-serve     run the standalone policy service
    prometheus-ai approve          approve/reject from a terminal
    prometheus-ai kill-switch      engage or release the global write halt
    prometheus-ai versions         regenerate docs/VERSIONS.md
    prometheus-ai version          print the project version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

from prometheus_ai import __version__

ROOT = Path(__file__).resolve().parents[2]

DEMO_ALERTS = [
    {
        "labels": {
            "alertname": "DemoCrashLooping",
            "namespace": "demo",
            "service": "api",
            "severity": "critical",
            "demo_fault": "crashloop",
        },
        "annotations": {"summary": "api pods are restarting repeatedly; availability is 0/1."},
    },
    {
        "labels": {
            "alertname": "DemoOOMKilled",
            "namespace": "demo",
            "service": "worker",
            "severity": "critical",
            "demo_fault": "oom",
        },
        "annotations": {"summary": "worker was OOMKilled at its memory limit."},
    },
    {
        "labels": {
            "alertname": "DemoLatencyHigh",
            "namespace": "demo",
            "service": "web",
            "severity": "warning",
            "demo_fault": "latency",
        },
        "annotations": {"summary": "web p99 latency is 2.4s and rising."},
    },
    {
        "labels": {
            "alertname": "DemoHostileLogs",
            "namespace": "demo",
            "service": "api",
            "severity": "warning",
            "demo_fault": "injection",
        },
        "annotations": {
            "summary": "api logs contain instructions aimed at the agent (injection test)."
        },
    },
]


# --------------------------------------------------------------------------- helpers


def _demo_payload(alerts: list[dict]) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "fingerprint": f"demo-{alert['labels']['demo_fault']}-{alert['labels']['service']}",
            }
            for alert in alerts
        ],
    }


async def _seed(services, payload: dict) -> list[str]:
    result = await services.alerts.ingest(payload)
    await services.manager.wait_all()
    return result.created


def _build_app(settings, services=None):
    from prometheus_ai.api.app import create_app

    return create_app(settings, services=services)


# --------------------------------------------------------------------------- commands


def cmd_version(_: argparse.Namespace) -> int:
    print(f"prometheus-ai {__version__}")
    print(f"python {platform.python_version()} on {platform.platform()}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from prometheus_ai.config import get_settings

    settings = get_settings()
    uvicorn.run(
        _build_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    import uvicorn

    from prometheus_ai.config import Settings

    settings = Settings(model_backend="fake")
    if settings.model_backend != "fake":
        print("demo requires PROMETHEUS_AI_MODEL_BACKEND=fake", file=sys.stderr)
        return 2

    async def prepare():
        from prometheus_ai.services.bootstrap import build_services

        services = await build_services(settings)
        created = await _seed(services, _demo_payload(DEMO_ALERTS))
        return services, created

    services, created = asyncio.run(prepare())
    print(f"seeded {len(created)} synthetic incidents (no cluster was touched):")
    for incident_id in created:
        print(f"  http://{args.host}:{args.port}/incidents/{incident_id}")
    print()
    print("Synthetic mode: the deterministic demo model and fake tools are active.")
    print("Approve one with the dev token 'dev-approval-token' to see the full path.")
    try:
        uvicorn.run(
            _build_app(settings, services=services),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    finally:
        asyncio.run(services.close())
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    import httpx

    faults = [f.strip() for f in args.fault.split(",") if f.strip()]
    chosen = [
        alert
        for alert in DEMO_ALERTS
        if not faults or alert["labels"]["demo_fault"] in faults
    ]
    if not chosen:
        print(f"no demo alerts match {faults}", file=sys.stderr)
        return 2
    url = args.url.rstrip("/") + "/webhooks/alertmanager"
    token = args.token or os.environ.get("PROMETHEUS_AI_WEBHOOK_TOKEN", "")
    headers = {"X-Webhook-Token": token} if token else {}
    response = httpx.post(url, json=_demo_payload(chosen), headers=headers, timeout=20.0)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    if args.tier.lower() != "t1":
        print(
            "T2 (live model) is not run by this command.\n"
            "  T2 needs Ollama or Bedrock plus a stored live baseline; no live number is\n"
            "  published in this repository. See docs/EVALUATION.md for the protocol.",
            file=sys.stderr,
        )
        return 2

    from prometheus_ai.evals.runner import run_t1
    from prometheus_ai.evals.scenarios import load_scenarios

    scenarios = load_scenarios(args.scenarios_dir)
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s.id in wanted]
        if not scenarios:
            print(f"no scenario matched {sorted(wanted)}", file=sys.stderr)
            return 2

    report, metrics, passed, regressions = asyncio.run(
        run_t1(
            scenarios,
            baseline_path=Path(args.baseline),
            update_baseline=args.update_baseline,
            out_path=Path(args.out),
        )
    )

    print(f"T1 · {report['scenarios']} scenarios · {report['generated_at']}")
    print(f"  root-cause accuracy    {metrics.root_cause_accuracy:7.1%}")
    print(f"  action correctness     {metrics.action_correctness:7.1%}")
    print(f"  tool-call correctness  {metrics.tool_call_correctness:7.1%}")
    print(f"  unsafe-action rate     {metrics.unsafe_action_rate:7.1%}  (gate: 0)")
    print(f"  status accuracy        {metrics.status_accuracy:7.1%}")
    print(f"  p95 latency            {metrics.p95_latency_ms:7.0f} ms")
    print(f"  mean steps             {metrics.mean_steps:7.2f}")
    if args.verbose:
        for row in report["results"]:
            mark = "ok " if row["status"] == row["expected_status"] else "BAD"
            print(
                f"  [{mark}] {row['id']:<26} {row['status']:<18} "
                f"{row['root_cause']:<13} {row['proposed_action'] or '-'}"
            )
            if row["unsafe_reason"]:
                print(f"        unsafe: {row['unsafe_reason']}")
            if row["error"]:
                print(f"        error: {row['error']}")
    print(f"\nreport written to {args.out}")
    if args.update_baseline:
        print(f"baseline updated at {args.baseline}")
    if not passed:
        print("\nGATE FAILED", file=sys.stderr)
        for regression in regressions:
            print(f"  - {regression}", file=sys.stderr)
        return 1
    print("GATE PASSED")
    return 0


def cmd_policy_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from prometheus_ai.config import get_settings
    from prometheus_ai.policy.server import create_policy_app

    settings = get_settings()
    if settings.policy_token == "dev-policy-token":
        print(
            "warning: using the development policy token; set "
            "PROMETHEUS_AI_POLICY_TOKEN before exposing this service",
            file=sys.stderr,
        )
    uvicorn.run(
        create_policy_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    import httpx

    from prometheus_ai.config import get_settings

    settings = get_settings()
    token = args.token or settings.approver_token
    url = args.url.rstrip("/") + f"/api/incidents/{args.incident_id}/decision"
    payload = {
        "decision": args.decision,
        "approver": args.approver,
        "reason": args.reason,
    }
    response = httpx.post(
        url,
        json=payload,
        headers={"X-Approval-Token": token},
        timeout=120.0,
    )
    if response.status_code >= 400:
        print(f"{response.status_code}: {response.text}", file=sys.stderr)
        return 1
    body = response.json()
    print(f"{body.get('id')}: {body.get('status')}")
    if body.get("dry_run"):
        print(f"  dry run: {'passed' if body['dry_run']['ok'] else 'failed'}")
    if body.get("policy_decision"):
        print(f"  policy: {body['policy_decision']['code']}")
    if body.get("execution"):
        print(f"  execution: {body['execution']['status']}")
    if body.get("verification"):
        print(f"  verification: {body['verification']['summary']}")
    return 0


def cmd_kill_switch(args: argparse.Namespace) -> int:
    import httpx

    from prometheus_ai.config import get_settings

    settings = get_settings()
    enabled = args.state == "on"
    response = httpx.post(
        args.url.rstrip("/") + "/api/admin/kill-switch",
        params={"enabled": str(enabled).lower()},
        headers={"X-Approval-Token": args.token or settings.approver_token},
        timeout=20.0,
    )
    if response.status_code >= 400:
        print(f"{response.status_code}: {response.text}", file=sys.stderr)
        return 1
    print(json.dumps(response.json()))
    return 0


def cmd_versions(_: argparse.Namespace) -> int:
    from prometheus_ai.versioning import collect_versions, render_versions_markdown

    data = collect_versions()
    path = ROOT / "docs" / "VERSIONS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_versions_markdown(data), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    for row in data["python_packages"]:
        print(f"  {row['name']:<28} {row['version']}")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prometheus-ai",
        description="Approval-gated incident-response agent for Kubernetes (demo scope).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _serve_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--log-level", default="info")

    serve = sub.add_parser("serve", help="run the API and approval console")
    _serve_args(serve)
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="seed synthetic incidents and serve")
    _serve_args(demo)
    demo.set_defaults(func=cmd_demo)

    seed = sub.add_parser("seed", help="post demo alerts to a running instance")
    seed.add_argument("--url", default="http://127.0.0.1:8000")
    seed.add_argument(
        "--token",
        default=None,
        help="webhook token (default: PROMETHEUS_AI_WEBHOOK_TOKEN)",
    )
    seed.add_argument(
        "--fault",
        default="",
        help="comma-separated faults: crashloop,oom,latency,injection (default: all)",
    )
    seed.set_defaults(func=cmd_seed)

    ev = sub.add_parser("eval", help="run the deterministic evaluation tier")
    ev.add_argument("--tier", default="t1", help="only t1 is implemented offline")
    ev.add_argument(
        "--scenarios-dir",
        default=str(ROOT / "evals" / "scenarios"),
    )
    ev.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run one scenario id (repeatable)",
    )
    ev.add_argument(
        "--baseline",
        default=str(ROOT / "evals" / "baseline.t1.json"),
    )
    ev.add_argument(
        "--out",
        default=str(ROOT / "evals" / "out" / "latest.json"),
    )
    ev.add_argument("--update-baseline", action="store_true")
    ev.add_argument("--verbose", action="store_true")
    ev.set_defaults(func=cmd_eval)

    pol = sub.add_parser("policy-serve", help="run the standalone policy service")
    pol.add_argument("--host", default="127.0.0.1")
    pol.add_argument("--port", type=int, default=8081)
    pol.add_argument("--log-level", default="info")
    pol.set_defaults(func=cmd_policy_serve)

    approve = sub.add_parser("approve", help="approve or reject an incident")
    approve.add_argument("incident_id")
    approve.add_argument("--decision", choices=["approve", "reject"], default="approve")
    approve.add_argument("--approver", default=os.environ.get("USER", "operator"))
    approve.add_argument("--reason", default="")
    approve.add_argument("--url", default="http://127.0.0.1:8000")
    approve.add_argument("--token", default=None)
    approve.set_defaults(func=cmd_approve)

    kill = sub.add_parser("kill-switch", help="engage or release the global write halt")
    kill.add_argument("state", choices=["on", "off"])
    kill.add_argument("--url", default="http://127.0.0.1:8000")
    kill.add_argument("--token", default=None)
    kill.set_defaults(func=cmd_kill_switch)

    versions = sub.add_parser("versions", help="regenerate docs/VERSIONS.md")
    versions.set_defaults(func=cmd_versions)

    version = sub.add_parser("version", help="print versions")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
