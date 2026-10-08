"""Regression pin for the ``evidence_refs`` cap of 4 on ``CompactFinding``.

Guarded invariant: ``CompactFinding.evidence_refs`` accepts 1-4 references and
``CompactStaffOutput`` enforces the same bound through its nested findings.
Silently raising, dropping, or loosening that cap is a regression: provider
generation would copy entire context reference lists into a finding.

Boundary table (intended behaviour):

* 1-4 refs accepted
* 5 refs rejected on ``evidence_refs``
* 6 and 10 refs rejected on ``evidence_refs``

The mutation-sensitivity self-check at the end of this module builds local
subclasses of ``CompactFinding`` with a raised cap and with no cap at all, and
proves the shared assertion helper fails on them. The mutants exist only to
show these assertions would catch a broken production schema; nothing outside
this test module is modified.
"""

from typing import get_args

import pytest
from pydantic import Field, ValidationError

from app.staff_schemas import CompactFinding, CompactStaffOutput


def _finding_payload(refs):
    """Minimal valid ``CompactFinding`` payload; ``refs`` is an int or a list."""
    if isinstance(refs, int):
        refs = [f"task:{i}" for i in range(refs)]
    return {
        "title": "Evidence pipeline drops audit samples",
        "situation": "Recorded samples are missing from the generated audit output.",
        "priority": "high",
        "why_now": "Unverified findings block the pending decision.",
        "recommendation": "Add a regression gate for the evidence cap.",
        "alternative": "Defer the audit entirely.",
        "risk": "Unverified conclusions reach the decision inbox.",
        "evidence_refs": list(refs),
        "confidence": 0.7,
        "decision_required": True,
    }


def _staff_output_payload(refs):
    return {
        "summary": "One finding needs a decision this week.",
        "findings": [_finding_payload(refs)],
        "next_action": "Review the decision cards",
    }


def _rejection_locs(model_cls, count):
    """Return the ``ValidationError`` locations for ``count`` refs, else None.

    Returns ``None`` when the model accepts ``count`` references, which is the
    observable signal that the cap is no longer enforced.
    """
    try:
        model_cls.model_validate(_finding_payload(count))
    except ValidationError as exc:
        return [tuple(error["loc"]) for error in exc.errors()]
    return None


def _assert_cap_enforced(model_cls, cap=4):
    """Assert exactly ``cap`` refs are accepted and ``cap + 1`` / 10 rejected.

    Raises ``AssertionError`` when the cap is raised, lowered, or dropped, so
    the same helper doubles as the mutation-sensitivity probe for the local
    mutant subclasses defined at the bottom of this module.
    """
    accepted = model_cls.model_validate(_finding_payload(cap))
    assert len(accepted.evidence_refs) == cap, (
        f"{model_cls.__name__} kept {len(accepted.evidence_refs)} of {cap} evidence_refs"
    )
    for overflow in (cap + 1, 10):
        locs = _rejection_locs(model_cls, overflow)
        assert locs is not None, (
            f"{model_cls.__name__} accepted {overflow} evidence_refs; the cap of {cap} is not enforced"
        )
        assert any(loc and loc[-1] == "evidence_refs" for loc in locs), (
            f"{model_cls.__name__} rejected {overflow} refs for an unrelated reason: {locs}"
        )


def _declared_max_length(field):
    """Return the declared list maximum length, or None when uncapped."""
    maxima = [
        meta.max_length for meta in field.metadata if isinstance(getattr(meta, "max_length", None), int)
    ]
    return max(maxima) if maxima else None


def test_compact_finding_accepts_exactly_four_evidence_refs():
    _assert_cap_enforced(CompactFinding)
    finding = CompactFinding.model_validate(_finding_payload(4))
    assert len(finding.evidence_refs) == 4


def test_compact_finding_rejects_five_evidence_refs():
    _assert_cap_enforced(CompactFinding)
    locs = _rejection_locs(CompactFinding, 5)
    assert locs is not None, "five evidence_refs must be rejected"
    assert any(loc and loc[-1] == "evidence_refs" for loc in locs), locs


def test_compact_finding_rejects_ten_evidence_refs():
    _assert_cap_enforced(CompactFinding)
    locs = _rejection_locs(CompactFinding, 10)
    assert locs is not None, "ten evidence_refs must be rejected"
    assert any(loc and loc[-1] == "evidence_refs" for loc in locs), locs


@pytest.mark.parametrize("overflow", [5, 6, 10])
def test_compact_finding_rejects_overflow_evidence_refs(overflow):
    with pytest.raises(ValidationError) as caught:
        CompactFinding.model_validate(_finding_payload(overflow))
    assert any(error["loc"][-1] == "evidence_refs" for error in caught.value.errors())


def test_evidence_refs_constraint_metadata_is_max_four():
    field = CompactFinding.model_fields["evidence_refs"]
    assert _declared_max_length(field) == 4, (
        f"expected a declared maximum list length of 4, saw {_declared_max_length(field)} in {field.metadata}"
    )


def test_compact_staff_output_declares_compact_finding_items():
    items = get_args(CompactStaffOutput.model_fields["findings"].annotation)
    assert items and items[0] is CompactFinding, items


def test_compact_staff_output_accepts_nested_finding_with_four_refs():
    output = CompactStaffOutput.model_validate(_staff_output_payload(4))
    assert len(output.findings) == 1
    assert len(output.findings[0].evidence_refs) == 4, "nested refs must not be truncated"


def test_compact_staff_output_rejects_nested_finding_with_five_refs():
    with pytest.raises(ValidationError) as caught:
        CompactStaffOutput.model_validate(_staff_output_payload(5))
    errors = caught.value.errors()
    locs = [tuple(error["loc"]) for error in errors]
    assert any("evidence_refs" in loc for loc in locs), locs
    assert any(error["type"] != "missing" for error in errors), errors


def test_cap_helper_passes_on_real_schema():
    _assert_cap_enforced(CompactFinding)


def test_cap_helper_detects_raised_cap_mutant():
    # Mutant exists only to prove the helper is sensitive to a raised cap.
    raised = _RaisedCapFinding.model_validate(_finding_payload(5))
    assert len(raised.evidence_refs) == 5, "mutant must genuinely accept five refs"
    with pytest.raises(AssertionError):
        _assert_cap_enforced(_RaisedCapFinding)


def test_cap_helper_detects_uncapped_mutant():
    # Mutant exists only to prove the helper is sensitive to a dropped cap.
    uncapped = _UncappedFinding.model_validate(_finding_payload(10))
    assert len(uncapped.evidence_refs) == 10, "mutant must genuinely accept ten refs"
    with pytest.raises(AssertionError):
        _assert_cap_enforced(_UncappedFinding)


def test_evidence_refs_lists_are_per_instance():
    first = CompactFinding.model_validate(_finding_payload(4))
    second = CompactFinding.model_validate(_finding_payload(4))
    assert first.evidence_refs is not second.evidence_refs


class _RaisedCapFinding(CompactFinding):
    """Test-only mutant with the cap raised to 5; never used in production."""

    evidence_refs: list[str] = Field(min_length=1, max_length=5)


class _UncappedFinding(CompactFinding):
    """Test-only mutant with no list-length constraint; never used in production."""

    evidence_refs: list[str] = Field(min_length=1)
