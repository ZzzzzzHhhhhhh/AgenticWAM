"""Standalone command line; installing or planning never connects to hardware."""

import argparse
import json
import signal
import sys
import uuid
from pathlib import Path

from agenticwam.config import assemble, load_profile, make_planner
from agenticwam.core.types import Plan, Request


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agenticwam", description="AgenticWAM: 任务无关的动作模型编排框架")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="离线运行固定证据示例, 不使用模型或机器人")
    demo.add_argument("--scenario", choices=("mixed", "plates"), default="mixed")
    demo.add_argument("--output", type=Path, default=Path("agent-runs"))
    inspect = commands.add_parser("inspect", help="汇总一次执行记录")
    inspect.add_argument("directory", type=Path)
    commands.add_parser("plugins", help="列出已安装插件, 不加载插件代码")
    for name in ("plan", "run", "mcp"):
        command = commands.add_parser(name)
        command.add_argument("--profile", type=Path, required=True)
        command.add_argument("--instruction")
        command.add_argument("--output", type=Path, default=Path("agent-runs"))
        command.add_argument("--model", default="gpt-6-astra")
        command.add_argument("--codex", default="codex")
        if name in {"run", "mcp"}:
            command.add_argument("--endpoint")
        if name == "run":
            command.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from agenticwam.examples.demo import run_demo

            result = run_demo(args.output, args.scenario)
        elif args.command == "inspect":
            from agenticwam.evaluation import summarize

            result = summarize(args.directory)
        elif args.command == "plugins":
            from agenticwam.plugins import KINDS, discover

            result = {kind: sorted(discover(kind)) for kind in sorted(KINDS)}
        else:
            from agenticwam.planners.codex import CodexJson

            profile = load_profile(args.profile)
            model = CodexJson(executable=args.codex, model=args.model)
            planner = make_planner(profile, model)
            if args.command == "mcp":
                from agenticwam.service import serve

                return serve(planner, lambda: assemble(profile, model, args.output, endpoint=args.endpoint), profile)
            if args.command == "run" and args.plan:
                plan = Plan.from_dict(json.loads(args.plan.read_text(encoding="utf-8")))
            else:
                instruction = args.instruction if args.instruction is not None else sys.stdin.read()
                plan = planner.plan(Request(uuid.uuid4().hex, instruction))
            if len(plan.steps) > profile["max_steps"] or any(
                step.max_duration_ns > profile["step_timeout_sec"] * 1_000_000_000 for step in plan.steps
            ):
                raise ValueError("saved plan exceeds the profile's limits")
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / f"{plan.plan_id}.json").write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if args.command == "plan":
                result = plan.to_dict()
            else:
                runner = assemble(
                    profile,
                    model,
                    args.output,
                    endpoint=args.endpoint,
                    on_event=lambda e: print(json.dumps(e, ensure_ascii=False), flush=True),
                )
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: runner.cancel())
                result = runner.run(plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "cancelled"} else 0
    except Exception as exc:
        print(f"AgenticWAM: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
