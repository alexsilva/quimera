"""Testes da integração de bug context no PromptBuilder."""

from __future__ import annotations

from unittest.mock import patch

from quimera.bugs import BugEvidenceRef, BugReport, BugStore, make_bug_fingerprint
from quimera.prompt import PromptBuilder


class _DummyContextManager:
    def load(self) -> str:
        return ""


def test_prompt_builder_includes_bug_context_when_open_bugs_exist(tmp_path):
    session_id = "sessao-bugs"
    logs_dir = tmp_path / "data" / "logs"
    store = BugStore(logs_dir)
    try:
        summary = "Saída operacional colada na linha do prompt"
        fingerprint = make_bug_fingerprint(session_id, "prompt_line_collision", summary)
        store.file(
            BugReport(
                id=f"bug_{fingerprint[:12]}",
                session_id=session_id,
                category="prompt_line_collision",
                summary=summary,
                severity="high",
                confidence=0.91,
                fingerprint=fingerprint,
                evidence_refs=[
                    BugEvidenceRef(
                        kind="render_jsonl",
                        path="/tmp/render.jsonl",
                        line=99,
                    )
                ],
            )
        )
    finally:
        store.close()

    builder = PromptBuilder(
        context_manager=_DummyContextManager(),
        session_state={
            "workspace_tmp_root": str(tmp_path),
        },
    )
    section = builder._build_evidence_section({"session_id": session_id}, session_id)
    assert '<bug_context title="Bugs Operacionais Abertos">' in section
    assert "[prompt_line_collision]" in section


def test_prompt_builder_ignores_bug_store_failures(tmp_path):
    builder = PromptBuilder(
        context_manager=_DummyContextManager(),
        session_state={"workspace_tmp_root": str(tmp_path)},
    )
    with patch("quimera.prompt.BugStore", side_effect=OSError("perm denied")):
        section = builder._build_evidence_section({"session_id": "sessao-bugs"}, "sessao-bugs")
    assert section == ""
