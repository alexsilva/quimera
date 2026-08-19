import json

import pytest

from quimera.debate.models import (
    DebateProtocolError,
    WorkItem,
    contribution_from_response,
    synthesis_from_response,
    synthesis_from_dict,
    validate_work_items,
)


def _contribution_payload(**overrides):
    payload = {
        "position": "adotar a opcao A",
        "arguments": ["menor risco"],
        "objections": [],
        "proposal": "executar em duas etapas",
        "confidence": 0.8,
        "vote": "propose",
        "critical_objection": False,
        "work_items": [],
    }
    payload.update(overrides)
    return payload


def test_contribution_protocol_accepts_strict_json_object():
    contribution = contribution_from_response(
        json.dumps(_contribution_payload()),
        debate_id="deb-1",
        round_index=1,
        agent="alpha",
    )

    assert contribution.position == "adotar a opcao A"
    assert contribution.confidence == 0.8
    assert contribution.critical_objection is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"position": ["nao e texto"]},
        {"arguments": "nao e lista"},
        {"confidence": "0.8"},
        {"confidence": 1.2},
        {"critical_objection": "false"},
    ],
)
def test_contribution_protocol_rejects_ambiguous_types(overrides):
    with pytest.raises(DebateProtocolError):
        contribution_from_response(
            _contribution_payload(**overrides),
            debate_id="deb-1",
            round_index=1,
            agent="alpha",
        )


def test_synthesis_protocol_requires_boolean_consensus():
    with pytest.raises(DebateProtocolError, match="consensus_reached"):
        synthesis_from_response(
            {
                "summary": "sintese",
                "verdict": "veredito",
                "agreements": [],
                "disagreements": [],
                "critical_objections": [],
                "confidence": 0.5,
                "consensus_reached": "false",
                "work_items": [],
            },
            debate_id="deb-1",
            round_index=1,
            moderator="alpha",
        )


def test_persisted_synthesis_loads_without_removed_legacy_fields():
    loaded = synthesis_from_dict(
        {
            "debate_id": "deb-old",
            "round_index": 1,
            "moderator": "alpha",
            "summary": "sintese antiga",
            "verdict": "veredito antigo",
            "agreements": [],
            "disagreements": [],
            "critical_objections": [],
            "confidence": 0.5,
            "consensus_reached": False,
            "work_items": [],
        }
    )

    assert loaded is not None


def test_workflow_validation_rejects_unknown_dependencies_and_cycles():
    with pytest.raises(DebateProtocolError, match="desconhecidos"):
        validate_work_items((WorkItem("T1", "um", "um", dependencies=("T2",)),))

    with pytest.raises(DebateProtocolError, match="ciclo"):
        validate_work_items(
            (
                WorkItem("T1", "um", "um", dependencies=("T2",)),
                WorkItem("T2", "dois", "dois", dependencies=("T1",)),
            )
        )


def test_workflow_protocol_rejects_unsafe_item_identity():
    with pytest.raises(DebateProtocolError, match="id invalido"):
        synthesis_from_response(
            {
                "summary": "sintese",
                "verdict": "plano",
                "agreements": [],
                "disagreements": [],
                "critical_objections": [],
                "confidence": 0.5,
                "consensus_reached": False,
                "work_items": [
                    {
                        "id": "../../task",
                        "title": "item",
                        "description": "item",
                        "task_type": "general",
                        "assigned_to": "alpha",
                        "dependencies": [],
                        "acceptance_criteria": [],
                        "priority": "medium",
                    }
                ],
            },
            debate_id="deb-1",
            round_index=1,
            moderator="alpha",
        )
