class HandoffPresenter:
    """Formata dados de handoff para inserção no prompt."""

    EMPTY_FIELDS = {
        "handoff_present": "",
        "handoff_id": "",
        "handoff_task": "",
        "handoff_from": "",
        "handoff_context": "",
        "handoff_expected": "",
        "handoff_priority": "",
        "handoff_chain": "",
        "handoff_raw": "",
    }

    @staticmethod
    def present(handoff, from_agent=None):
        """Normaliza campos de handoff para renderização estável no template."""
        if not handoff:
            return dict(HandoffPresenter.EMPTY_FIELDS)
        if isinstance(handoff, dict):
            chain = handoff.get("chain", [])
            priority = handoff.get("priority", "normal")
            return {
                "handoff_present": "1",
                "handoff_id": str(handoff.get("handoff_id") or "").strip(),
                "handoff_task": (handoff.get("task") or "").strip(),
                "handoff_from": (from_agent or "").strip(),
                "handoff_context": (handoff.get("context") or "").strip(),
                "handoff_expected": (handoff.get("expected") or "").strip(),
                "handoff_priority": (
                    str(priority).strip().upper()
                    if priority and str(priority).strip().lower() != "normal"
                    else ""
                ),
                "handoff_chain": " -> ".join(chain) if chain else "",
                "handoff_raw": "",
            }
        return {
            **HandoffPresenter.EMPTY_FIELDS,
            "handoff_present": "1",
            "handoff_raw": str(handoff).strip(),
        }
