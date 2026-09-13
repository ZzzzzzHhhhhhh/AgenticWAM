"""Verify the public interaction boundary and installed plugin selection."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenticwam import plugins
from agenticwam.config import assemble, load_profile, make_planner
from agenticwam.core.types import ContractError
from agenticwam.examples.demo import make_demo
from agenticwam.service import ContextJobs, build_server


def test_duplicate_plugin_names_are_rejected(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda **kw: [SimpleNamespace(name="x"), SimpleNamespace(name="x")])
    with pytest.raises(ContractError, match="duplicate"):
        plugins.discover("backends")


def test_listing_does_not_load_plugin_code(monkeypatch):
    entry = SimpleNamespace(name="example", load=lambda: pytest.fail("listing must not load code"))

    def entry_points(*, group):
        return [entry] if group == "agenticwam.backends" else []

    monkeypatch.setattr(plugins, "entry_points", entry_points)
    assert plugins.discover("backends")["example"] is entry


def test_selected_plugins_are_used_by_application_composition(monkeypatch, tmp_path):
    selected = []
    planner = SimpleNamespace(plan=lambda r: None)

    def load(kind, name, **kwargs):
        selected.append((kind, name))
        return planner if kind == "planners" else SimpleNamespace()

    monkeypatch.setattr(plugins, "load", load)
    profile = {
        "capability": {"skills": ["language"]},
        "backend": "custom",
        "planner": "custom_planner",
        "monitor": "custom_monitor",
        "compiler": "custom_compiler",
    }
    assert make_planner(profile, None) is planner
    runner = assemble(profile, None, tmp_path)
    assert {kind for kind, _ in selected} == {"backends", "planners", "monitors", "compilers"}
    assert runner.replanner is None


def test_generic_mcp_planning_does_not_construct_backend():
    pytest.importorskip("mcp")
    plan, _ = make_demo()
    profile = {"max_steps": 64, "step_timeout_sec": 300}
    jobs = ContextJobs(lambda: pytest.fail("planning caused execution"), profile)
    server = build_server(SimpleNamespace(plan=lambda request: plan), jobs, profile)

    async def check():
        assert {tool.name for tool in await server.list_tools()} == {
            "agent_capabilities",
            "agent_plan",
            "agent_execute",
            "agent_status",
            "agent_events",
            "agent_cancel",
        }
        result = await server.call_tool("agent_plan", {"instruction": "Follow the task"})
        assert "agenticwam.plan/v1" in str(result)

    asyncio.run(check())


def test_standalone_mcp_stdio_does_not_connect_at_startup(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    profile = Path(__file__).resolve().parents[1] / "src/agenticwam/examples/plates-openwam.json"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agenticwam.cli",
            "mcp",
            "--profile",
            str(profile),
            "--endpoint",
            "http://127.0.0.1:1",
            "--output",
            str(tmp_path),
        ],
    )

    async def check():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert "agent_execute" in {tool.name for tool in (await session.list_tools()).tools}
            result = await session.call_tool("agent_capabilities", {})
            assert not result.isError

    asyncio.run(check())


def test_invalid_profile_rejected_before_assembly(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"capability": {"input_modalities": ["text"]}, "max_replans": -1}))
    with pytest.raises(ContractError):
        load_profile(path)
