class DelegatePresenter:
    """Formata dados de delegação para inserção no prompt."""

    ROLE_LABELS = {
        "planner": "planejador",
        "executor": "executor",
        "reviewer": "revisor",
        "verifier": "verificador",
        "synthesizer": "sintetizador",
    }

    ROLE_CONTRACTS = {
        "planner": "Planeje a abordagem, decomponha o trabalho e não aplique mudanças.",
        "executor": "Implemente a tarefa, valide o resultado e reporte evidências.",
        "reviewer": "Revise, aponte riscos e não edite o código.",
        "verifier": "Verifique evidências, rode/analise validações e confirme se os critérios foram atendidos.",
        "synthesizer": "Sintetize os resultados, resolva conflitos e produza uma conclusão final.",
    }

    EMPTY_FIELDS = {
        "delegation_present": "",
        "delegation_id": "",
        "delegation_request": "",
        "delegation_from": "",
        "delegation_context": "",
        "delegation_role": "",
        "delegation_role_contract": "",
        "delegation_access_list": "",
        "delegation_expected": "",
        "delegation_priority": "",
        "delegation_chain": "",
        "delegation_raw": "",
    }

    @staticmethod
    def _format_access_list(value) -> str:
        if not isinstance(value, list):
            return ""
        entries = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in entries)

    @staticmethod
    def present(delegation, from_agent=None):
        """Normaliza campos de delegação para renderização estável no template."""
        if not delegation:
            return dict(DelegatePresenter.EMPTY_FIELDS)
        if isinstance(delegation, dict):
            chain = delegation.get("chain", [])
            priority = delegation.get("priority", "normal")
            role = str(delegation.get("role") or "").strip()
            return {
                "delegation_present": "1",
                "delegation_id": str(delegation.get("delegation_id") or "").strip(),
                "delegation_request": (delegation.get("task") or "").strip(),
                "delegation_from": (from_agent or "").strip(),
                "delegation_context": (delegation.get("context") or "").strip(),
                "delegation_role": DelegatePresenter.ROLE_LABELS.get(role, ""),
                "delegation_role_contract": DelegatePresenter.ROLE_CONTRACTS.get(role, ""),
                "delegation_access_list": DelegatePresenter._format_access_list(
                    delegation.get("access_list")
                ),
                "delegation_expected": (delegation.get("expected") or "").strip(),
                "delegation_priority": (
                    str(priority).strip().upper()
                    if priority and str(priority).strip().lower() != "normal"
                    else ""
                ),
                "delegation_chain": " -> ".join(chain) if chain else "",
                "delegation_raw": "",
            }
        return {
            **DelegatePresenter.EMPTY_FIELDS,
            "delegation_present": "1",
            "delegation_raw": str(delegation).strip(),
        }
