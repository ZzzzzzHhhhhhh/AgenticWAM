"""Brand migration must preserve saved goals and the deployed robot protocol."""

import hashlib

import pytest

from agenticwam.backends.legacy_wire import WamContext
from agenticwam.backends.vela import VelaContextBackend
from agenticwam.core.types import Context, ContractError, Plan
from agenticwam.examples.demo import make_demo


def test_previous_plan_is_read_without_changing_goals():
    plan, _ = make_demo("plates")
    legacy = plan.to_dict()
    legacy["schema"] = "vela.agent.plan/v1"
    legacy["mission"] = "Place red, yellow, blue and green plates in order."
    legacy["steps"][0]["constraints"] = ["Preserve previously placed objects."]
    converted = Plan.from_dict(legacy).to_dict()
    assert converted == {**legacy, "schema": "agenticwam.plan/v1"}
    assert legacy["schema"] == "vela.agent.plan/v1"


@pytest.mark.parametrize("schema", ["vela.context-plan/v1", "agenticwam.plan/v2", None, {}, []])
def test_migration_does_not_accept_unrecognized_plan_schemas(schema):
    plan, _ = make_demo()
    with pytest.raises(ContractError, match="invalid plan envelope"):
        Plan.from_dict({**plan.to_dict(), "schema": schema})


def test_renamed_context_keeps_deployed_wam_receipts_compatible():
    context = Context("task-1", 1, "Place the red plate on the rack.")
    assert context.to_dict()["schema"] == "agenticwam.context/v1"
    wire = WamContext(context.context_id, context.revision, context.text)
    receipt = {
        "schema": "wam.context/v1",
        "context_id": context.context_id,
        "revision": context.revision,
        "text": context.text,
        "text_sha256": hashlib.sha256(context.text.encode()).hexdigest(),
    }
    wire.verify_receipt(receipt)
    with pytest.raises(ContractError, match="exact task context"):
        wire.verify_receipt({**receipt, "text": "Place a different plate."})


def test_existing_robot_capability_handshake_is_unchanged(monkeypatch):
    backend = VelaContextBackend("http://127.0.0.1:1")
    responses = {
        "/api/v1/capabilities": {"commands": [{"type": "agent.execute"}, {"type": "agent.cancel"}]},
        "/api/v1/snapshot": {
            "agent": {
                "schema": "vela.agent.execution/v1",
                "active": False,
                "cancel_epoch": 7,
                "gripper_signal_supported": True,
            }
        },
    }
    monkeypatch.setattr(backend, "_request", responses.__getitem__)
    assert backend.check_capabilities() == responses["/api/v1/capabilities"]
    assert backend.cancel_epoch == 7
