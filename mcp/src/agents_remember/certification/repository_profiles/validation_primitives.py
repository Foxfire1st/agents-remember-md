"""Shared finding construction for repository profile contract validators."""

from __future__ import annotations

from collections import Counter

from agents_remember.certification.models import RegistryValidationFinding


def _finding(code: str, path: str, detail: str) -> RegistryValidationFinding:
    return RegistryValidationFinding(code=code, path=path, detail=detail)


def _duplicates(values, code, path, findings) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        findings.append(_finding(code, path, "duplicate declarations: " + ", ".join(duplicates)))


def _validate_gate_set(gates, path, findings) -> None:
    if tuple(gates) != tuple(sorted(set(gates))):
        findings.append(
            _finding(
                "semantic-input-gates-not-canonical",
                path,
                "semantic input gate scope must be nonempty, unique, and ordered",
            )
        )
