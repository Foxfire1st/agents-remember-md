"""CCR-R08 gate 1-4 prerequisite-prefix adapter contracts.

require_green_gate_prefix reuse, refusal, invalidation, and memory-only
repair routes. Split from test_final_full_memory_coherence_certification
(repository file-size hard limit); the shared Gate-5 fixture scaffold is
imported from that module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
)
from agents_remember.errors import CertificationContractError, FinalCertificationError
from agents_remember.memory_quality.final_certification import gate_prefix as gate_prefix_module
from agents_remember.memory_quality.final_certification import (
    require_green_gate_prefix,
)
from test_final_full_memory_coherence_certification import (
    _CODE_TREE,
    _green_prefix,
    _scenario,
)

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))


# Gate 1-4 prerequisite adapter
# ---------------------------------------------------------------------------


def test_green_prefix_adapter_reuses_exact_gate_one_to_four() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    proof = require_green_gate_prefix(
        scenario.admission,
        certificates,
        (),
        code_tree=_CODE_TREE,
    )

    assert tuple(item.gate for item in proof.reusedGateOneToFour) == (1, 2, 3, 4)
    assert len(proof.certificateReusePlanDigest) == 64


def test_green_prefix_adapter_refuses_incomplete_prefix() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)[:3]

    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(
            scenario.admission,
            certificates,
            (),
            code_tree=_CODE_TREE,
        )
    assert caught.value.status == "gate-five-prefix-incomplete"


def test_green_prefix_adapter_refuses_when_code_tree_mismatches_admission() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(
            scenario.admission,
            certificates,
            (),
            code_tree="f" * 40,
        )
    assert caught.value.status == "gate-five-code-candidate-mismatch"


def test_green_prefix_adapter_restarts_at_gate_one_on_code_change() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(
            scenario.admission,
            certificates,
            (CertificateInputChange(changeClass="code", reason="code changed"),),
            code_tree=_CODE_TREE,
        )
    assert caught.value.status == "gate-five-prefix-invalidated"
    assert caught.value.observed["firstGateToRun"] == 1


def test_green_prefix_adapter_reuses_prefix_for_memory_only_repair() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    proof = require_green_gate_prefix(
        scenario.admission,
        certificates,
        (CertificateInputChange(changeClass="memory-onboarding", reason="memory-only repair"),),
        code_tree=_CODE_TREE,
    )

    assert tuple(item.gate for item in proof.reusedGateOneToFour) == (1, 2, 3, 4)


def test_green_prefix_adapter_refuses_stale_certificate_prefix() -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)
    envelope = scenario.admission.semanticEnvelope.model_copy(
        update={"candidateCodeTree": cast(Any, SimpleNamespace(kind="git-tree", value="f" * 40))}
    )
    admission = scenario.admission.model_copy(update={"semanticEnvelope": envelope})

    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(admission, certificates, (), code_tree="f" * 40)
    assert caught.value.status == "gate-five-prefix-stale"
    assert caught.value.observed["refusal"]


def test_green_prefix_adapter_translates_r21_refusal_with_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    def refuse(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise CertificationContractError(
            "r21 refused the prefix",
            ({"code": "stale-gate-certificate", "detail": "current admission moved"},),
        )

    monkeypatch.setattr(gate_prefix_module, "plan_certificate_reuse", refuse)
    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(scenario.admission, certificates, (), code_tree=_CODE_TREE)
    assert caught.value.status == "gate-five-prefix-invalidated"
    assert str(caught.value.observed["refusal"]).startswith("stale-gate-certificate")


def test_green_prefix_adapter_translates_plain_r21_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario()
    certificates = _green_prefix(scenario)

    def refuse(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("r21 exploded")

    monkeypatch.setattr(gate_prefix_module, "plan_certificate_reuse", refuse)
    with pytest.raises(FinalCertificationError) as caught:
        require_green_gate_prefix(scenario.admission, certificates, (), code_tree=_CODE_TREE)
    assert caught.value.status == "gate-five-prefix-invalidated"
    assert caught.value.observed["refusal"] == "r21 exploded"
