"""Read-only cloud-model ablation on recorded images; never imports a robot backend.

Images are sent to the configured Codex model. No robot commands are issued.
Historical model judgments are context, not independent physical ground truth.
"""

import argparse
import json
import re
import time
from pathlib import Path

from agenticwam.core.types import Plan
from agenticwam.monitors.vision import VisionVerifier
from agenticwam.planners.codex import CodexJson


def observation(directory, row):
    metadata = row["observation"]
    identifier = metadata["observation_id"]
    if re.fullmatch(r"[a-f0-9]{32}", identifier) is None:
        raise ValueError("unsupported observation identifier")
    images = [directory / f"{identifier}-{i}.jpg" for i in range(len(metadata["image_order"]))]
    if not images or any(not image.is_file() for image in images):
        raise ValueError("recorded camera images are unavailable")
    return {"images": [str(image.resolve()) for image in images], "metadata": metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path, help="JSON with a verification object")
    parser.add_argument("--step-id", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--compact-instructions", action="store_true", help="Test compact task-specific instructions")
    parser.add_argument("--repetitions", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    plan = Plan.from_dict(json.loads((args.journal / "plan.json").read_text()))
    rules = json.loads(args.rules.read_text())["verification"]
    all_rows = [json.loads(line) for line in (args.journal / "events.jsonl").read_text().splitlines()]
    rows = [row for row in all_rows if row.get("step_id") == args.step_id and "observation" in row]
    step = next(step for step in plan.steps if step.step_id == args.step_id)
    before_row = next(row for row in rows if row["event"] == "precondition_checked")
    first_row = next(row for row in rows if row["event"] == "verification" and row["verdict"]["verdict"] == "succeeded")
    second_row = next(
        row for row in rows if row["event"] == "completion_check" and row["sequence"] > first_row["sequence"]
    )
    before = observation(args.journal, before_row)
    afters = [observation(args.journal, row) for row in (first_row, second_row)]
    history = [row for row in rows if row["sequence"] < first_row["sequence"]]
    completed_ids = {
        row["step_id"]
        for row in all_rows
        if row["event"] == "step_succeeded" and row["sequence"] < before_row["sequence"]
    }
    completed = tuple(step for step in plan.steps if step.step_id in completed_ids)
    remaining = tuple(step for step in plan.steps if step.step_id not in completed_ids)
    report = {
        "mode": "recorded-image replay; no physical execution",
        "step_id": args.step_id,
        "model": args.model,
        "compact_instructions": args.compact_instructions,
        "temporal_gap_seconds": (
            afters[1]["metadata"]["server_monotonic_ns"] - afters[0]["metadata"]["server_monotonic_ns"]
        )
        / 1e9,
        "trials": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(args.repetitions):
        variants = ["stateless_sequential", "memory_sequential", "memory_window"]
        # Reverse order on alternating rounds to reduce simple warm-cache/order effects.
        if repetition % 2:
            variants.reverse()
        for variant in variants:
            model = CodexJson(executable=args.codex, model=args.model, compact_instructions=args.compact_instructions)
            verifier = VisionVerifier(model, rules, memory_entries=0 if variant.startswith("stateless") else 3)
            verifier.set_context(plan, completed, remaining)
            verifier.memory.begin(step)
            for row in history:
                verifier.memory.remember(row["event"], [observation(args.journal, row)], [row["verdict"]])
            trial = {"variant": variant, "repetition": repetition, "calls": [], "verdicts": []}
            started = time.monotonic()
            if variant == "memory_window":
                trial["verdicts"] = verifier.verify_many(step, before, afters)
                trial["calls"].append(model.last_call_metrics)
            else:
                for after in afters:
                    trial["verdicts"].append(verifier.verify(step, before, after))
                    trial["calls"].append(model.last_call_metrics)
            trial["elapsed_seconds"] = time.monotonic() - started
            report["trials"].append(trial)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "variant": variant,
                        "seconds": trial["elapsed_seconds"],
                        "verdicts": [value["verdict"] for value in trial["verdicts"]],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
