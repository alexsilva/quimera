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


def _evidence():
    return [
        {
            "id": "E1",
            "source": "quimera/debate/models.py",
            "line_start": 1,
            "line_end": 3,
            "excerpt": "Domain models and strict protocol parsing",
            "claim": "o modulo define o protocolo estrito do debate",
        }
    ]


def _contribution_payload(**overrides):
    payload = {
        "position": "adotar a opcao A",
        "arguments": ["menor risco"],
        "objections": [],
        "proposal": "executar em duas etapas",
        "confidence": 0.8,
        "vote": "propose",
        "critical_objection": False,
        "evidence_ids": ["E1"],
        "evidence": _evidence(),
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
    assert contribution.evidence_ids == ("E1",)
    assert "quimera/debate/models.py:1-3" in contribution.render()


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


def test_contribution_protocol_rejects_missing_or_unknown_evidence():
    with pytest.raises(DebateProtocolError, match="evidence deve conter"):
        contribution_from_response(
            _contribution_payload(evidence=[]),
            debate_id="deb-1",
            round_index=1,
            agent="alpha",
        )

    with pytest.raises(DebateProtocolError, match="referencias desconhecidas"):
        contribution_from_response(
            _contribution_payload(evidence_ids=["E404"]),
            debate_id="deb-1",
            round_index=1,
            agent="alpha",
        )


def test_evidence_accepts_web_source_without_lines():
    contribution = contribution_from_response(
        _contribution_payload(
            evidence=[
                {
                    "id": "E1",
                    "source": "https://docs.python.org/3/library/json.html",
                    "excerpt": "json exposes an API familiar to users of the standard library",
                    "claim": "a doc oficial descreve o modulo json",
                }
            ]
        ),
        debate_id="deb-1",
        round_index=1,
        agent="alpha",
    )

    evidence = contribution.evidence[0]
    assert evidence.kind == "web"
    assert evidence.line_start == 0
    assert evidence.line_end == 0
    assert "<https://docs.python.org/3/library/json.html>" in contribution.render()


@pytest.mark.parametrize("source", ["https://", "http:///caminho/sem/host"])
def test_evidence_rejects_web_source_without_host(source):
    with pytest.raises(DebateProtocolError, match="URL invalida"):
        contribution_from_response(
            _contribution_payload(
                evidence=[
                    {
                        "id": "E1",
                        "source": source,
                        "excerpt": "trecho copiado de algum lugar",
                        "claim": "afirmacao",
                    }
                ]
            ),
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
                "evidence_ids": ["E1"],
                "evidence": _evidence(),
                "work_items": [],
            },
            debate_id="deb-1",
            round_index=1,
            moderator="alpha",
        )


def test_persisted_legacy_synthesis_without_evidence_still_loads():
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
    assert loaded.evidence == ()
    assert loaded.evidence_ids == ()


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
                "evidence_ids": ["E1"],
                "evidence": _evidence(),
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
                        "evidence_ids": ["E1"],
                    }
                ],
            },
            debate_id="deb-1",
            round_index=1,
            moderator="alpha",
        )
