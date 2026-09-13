"""Optional provider-neutral MCP interface with one execution owner."""

import asyncio
import json
import threading
import uuid

from vela_agent.core.types import NeedsClarification, Plan, Request


class ContextJobs:
    """One execution owner; request IDs are idempotent for this process lifetime."""

    def __init__(self, runner_factory, profile):
        self.runner_factory, self.profile = runner_factory, profile
        self._lock = threading.Lock()
        self._jobs = {}
        self._closed = False

    def execute(self, value, request_id):
        plan = Plan.from_dict(value)
        if len(plan.steps) > self.profile["max_steps"] or any(
            s.max_duration_ns > self.profile["step_timeout_sec"] * 1_000_000_000 for s in plan.steps
        ):
            raise ValueError("plan exceeds the task profile limits")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id must contain 1..128 characters")
        fingerprint = json.dumps(plan.to_dict(), sort_keys=True)
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP execution service is closing")
            if request_id in self._jobs:
                job = self._jobs[request_id]
                if fingerprint != job["fingerprint"]:
                    raise ValueError("request_id already identifies another plan")
            else:
                if any(job["thread"].is_alive() for job in self._jobs.values()):
                    raise RuntimeError("a plan is running; cancel it or wait for completion")
                if len(self._jobs) >= 256:
                    raise RuntimeError("request ledger is full; restart the MCP service")
                runner = self.runner_factory()
                job = {"fingerprint": fingerprint, "runner": runner, "error": ""}

                def run():
                    try:
                        runner.run(plan)
                    except Exception as exc:
                        job["error"] = str(exc)

                job["thread"] = threading.Thread(target=run, name="context-plan", daemon=True)
                self._jobs[request_id] = job
                job["thread"].start()
        return self.status(request_id)

    def status(self, request_id):
        with self._lock:
            job = self._jobs[request_id]
            state = job["runner"].snapshot()
            if state["status"] == "idle" and job["thread"].is_alive():
                state["status"] = "queued"
            if job["error"]:
                state.update(status="failed", reason=job["error"])
            return {"request_id": request_id, **state}

    def cancel(self, request_id):
        with self._lock:
            self._jobs[request_id]["runner"].cancel()
        return {**self.status(request_id), "cancellation_requested": True}

    def events(self, request_id, after_sequence=0, limit=100):
        with self._lock:
            runner = self._jobs[request_id]["runner"]
        return runner.events(after_sequence, limit)

    def close(self):
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job["runner"].cancel()
        for job in jobs:
            job["thread"].join()


def build_server(planner, jobs, profile):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install velabot-agent[mcp] to use the MCP interface") from exc
    server = FastMCP("Vela Agent")

    @server.tool()
    def agent_capabilities() -> dict:
        """Read task capabilities and limits without connecting to the robot."""
        return {"schema": "vela.agent.capabilities/v1", "profile": profile}

    @server.tool()
    async def agent_plan(instruction: str) -> dict:
        """Translate a mission into atomic goals without robot motion."""
        try:
            plan = await asyncio.to_thread(planner.plan, Request(uuid.uuid4().hex, instruction))
        except NeedsClarification as exc:
            return {"status": "needs_clarification", "question": str(exc)}
        return {"status": "ready", "plan": plan.to_dict()}

    @server.tool()
    def agent_execute(plan: dict, request_id: str) -> dict:
        """Execute an explicitly requested physical task; reuse request_id on retries."""
        return jobs.execute(plan, request_id)

    @server.tool()
    def agent_status(request_id: str) -> dict:
        """Read task progress and outcome without issuing actions."""
        return jobs.status(request_id)

    @server.tool()
    def agent_events(request_id: str, after_sequence: int = 0, limit: int = 100) -> dict:
        """Read ordered execution evidence with a cursor."""
        return jobs.events(request_id, after_sequence, limit)

    @server.tool()
    def agent_cancel(request_id: str) -> dict:
        """Request cancellation; poll status before starting another task."""
        return jobs.cancel(request_id)

    return server


def serve(planner, runner_factory, profile):
    jobs = ContextJobs(runner_factory, profile)
    try:
        build_server(planner, jobs, profile).run(transport="stdio")
    finally:
        jobs.close()
    return 0
