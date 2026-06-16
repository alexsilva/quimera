class DelegatePresenter:
    """Formata dados de delegação para inserção no prompt."""

    EMPTY_FIELDS = {
        "delegation_present": "",
        "delegation_id": "",
        "delegation_request": "",
        "delegation_from": "",
        "delegation_context": "",
        "delegation_expected": "",
        "delegation_priority": "",
        "delegation_chain": "",
        "delegation_raw": "",
    }

    @staticmethod
    def present(delegation, from_agent=None):
        """Normaliza campos de delegação para renderização estável no template."""
        if not delegation:
            return dict(DelegatePresenter.EMPTY_FIELDS)
        if isinstance(delegation, dict):
            chain = delegation.get("chain", [])
            priority = delegation.get("priority", "normal")
            return {
                "delegation_present": "1",
                "delegation_id": str(delegation.get("delegation_id") or "").strip(),
                "delegation_request": (delegation.get("task") or "").strip(),
                "delegation_from": (from_agent or "").strip(),
                "delegation_context": (delegation.get("context") or "").strip(),
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
