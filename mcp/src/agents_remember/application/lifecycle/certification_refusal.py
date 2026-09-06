"""Public rendering of complete typed certification admission findings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agents_remember.errors import CertificationContractError


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return value


def certification_admission_refusal(
    operation: str, error: CertificationContractError
) -> dict[str, object]:
    """Preserve expected/observed facts without dropping all but the first failure."""
    return {
        "ok": False,
        "operation": operation,
        "state": "refused",
        "status": "certification-admission-refused",
        "detail": str(error),
        "findings": [_json_value(finding) for finding in error.findings],
        "gateStarts": 0,
    }
