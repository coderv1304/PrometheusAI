from __future__ import annotations

import argparse
import asyncio

import uvicorn

from prometheus_ai import cli
from prometheus_ai.services import bootstrap


def test_demo_runs_and_closes_services_on_the_server_loop(monkeypatch) -> None:
    class Manager:
        tasks: list[asyncio.Task] = []

        async def wait_all(self) -> None:
            await asyncio.gather(*self.tasks)

        async def shutdown(self) -> None:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    class Services:
        manager = Manager()
        closed = False

        async def close(self) -> None:
            await self.manager.shutdown()
            self.closed = True

    services = Services()

    async def build_services(_settings):
        return services

    async def seed(_services, _payload):
        task = asyncio.create_task(asyncio.sleep(0))
        services.manager.tasks.append(task)
        await services.manager.wait_all()
        return ["inc_demo"]

    monkeypatch.setattr(bootstrap, "build_services", build_services)
    monkeypatch.setattr(cli, "_seed", seed)
    monkeypatch.setattr(cli, "_build_app", lambda _settings, services=None: object())

    class Server:
        def __init__(self, _config) -> None:
            pass

        async def serve(self) -> None:
            loop = asyncio.get_running_loop()
            assert all(task.get_loop() is loop for task in services.manager.tasks)
            raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn, "Server", Server)

    result = cli.cmd_demo(argparse.Namespace(host="127.0.0.1", port=8000, log_level="info"))

    assert result == 0
    assert services.closed
