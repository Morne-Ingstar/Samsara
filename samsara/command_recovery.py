"""Catalog recovery evidence; a risk label is never proof of an undo path."""
from __future__ import annotations


def recovery_hint(risk: str, phrase: str, metadata=None) -> tuple[bool, str]:
    """Return a declared undo hint and its provenance, never an authorization.

    False means no verified undo claim, not that the command needs a wait.
    Even a declaration is documentation, not a runtime rollback guarantee.
    """
    metadata = metadata or {}
    verb = (phrase.lower().strip().split() or [""])[0]
    if verb in {"close", "quit", "exit"}:
        return False, "conditional: unsaved content may be lost"
    if risk == "read":
        return False, "not needed: read only"
    value = metadata.get("reversible", metadata.get("reversibility"))
    if value is True or value in ("reversible", "undoable"):
        return True, "declared: reversible (not verified rollback)"
    if value is False or metadata.get("irreversible") is True:
        return False, "declared: no undo"
    return False, "undeclared: no verified recovery"
