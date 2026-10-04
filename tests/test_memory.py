"""Context and Memory Plane: provenance, freshness, permission, correction path."""

import json
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import context_slice, remember
from app.db import MemoryItem, utcnow
from app.schemas import DecisionState, MemoryState, MemoryWrite, Sensitivity


def item(key="deploy.target", value="prod-web", **overrides):
    payload = {"key": key, "value": value, "source": "requirements section 17", "scope": "lab"}
    return MemoryWrite(**{**payload, **overrides})


async def test_memory_requires_a_source(db):
    with pytest.raises(Exception):
        item(source="")


async def test_stored_memory_carries_provenance_freshness_and_permission(db):
    stored = await remember(item(evidence_ref="requirements#17", confidence=0.8), owner=7)
    [view] = (await db.recall(role="lead", owner=7))[0]
    assert view.key == "deploy.target"
    assert view.source == "requirements section 17"
    assert view.evidence_ref == "requirements#17"
    assert view.confidence == 0.8
    assert view.sensitivity == Sensitivity.INTERNAL
    assert view.label == "current" and view.stale is False
    assert stored.owner == 7


async def test_expired_memory_is_labelled_stale_rather_than_hidden(db):
    await remember(item(expires_at=utcnow() - timedelta(days=1)))
    [view] = (await db.recall(role="lead"))[0]
    assert view.stale is True and view.label == "stale"
    # Stale data stays visible and labelled; it is not silently dropped.
    assert await context_slice(["deploy.target"], role="lead")


async def test_low_confidence_memory_is_labelled_unverified(db):
    await remember(item(confidence=0.3))
    assert (await db.recall(role="lead"))[0][0].label == "unverified"


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("lead", ["public", "internal", "confidential"]),
        ("developer", ["public", "internal"]),
        ("reviewer", ["public", "internal"]),
    ],
)
async def test_recall_never_exceeds_the_configured_role_clearance(db, role, expected):
    for level in ("public", "internal", "confidential", "restricted"):
        await remember(item(key=f"k-{level}", value=f"v-{level}", sensitivity=Sensitivity(level)))
    got = {v.sensitivity for v in (await db.recall(role=role))[0]}
    assert {s.value for s in got} == set(expected)


async def test_an_unknown_role_gets_nothing_rather_than_everything(db):
    # A typo must never widen access, so an unrecognised role reads no memory at all.
    await remember(item())
    for role in ("intern", "everyone", "admin", ""):
        assert (await db.recall(role=role))[0] == []


async def test_no_role_at_all_reads_nothing(db):
    await remember(item())
    assert (await db.recall(role=None))[0] == []


async def test_clearance_is_configuration_not_code(monkeypatch):
    from app.config import settings

    # Defaults are the recommended split: Lead sees business context, the others do not.
    assert settings.role_clearance() == {
        "lead": "confidential",
        "developer": "internal",
        "reviewer": "internal",
    }
    monkeypatch.setattr(settings, "role_clearance_json", '{"lead":"restricted","developer":"confidential"}')
    assert settings.role_clearance() == {"lead": "restricted", "developer": "confidential"}


async def test_an_invalid_clearance_config_fails_closed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "role_clearance_json", '{"lead":"everything"}')
    with pytest.raises(ValueError, match="must be one of"):
        settings.role_clearance()


async def test_configured_clearance_widens_or_narrows_recall(db, monkeypatch):
    from app.config import settings

    await remember(item(key="payroll", sensitivity=Sensitivity.RESTRICTED, value="band 3"))
    monkeypatch.setattr(settings, "role_clearance_json", '{"developer":"restricted"}')
    assert [v.key for v in (await db.recall(role="developer"))[0]] == ["payroll"]
    monkeypatch.setattr(settings, "role_clearance_json", '{"developer":"public"}')
    assert (await db.recall(role="developer"))[0] == []


async def test_recall_is_a_slice_scoped_to_one_project(db):
    await remember(item(key="lab-only", scope="lab"))
    await remember(item(key="quant-only", scope="quant", value="q"))
    await remember(item(key="global", scope="", value="g"))
    keys = {v.key for v in (await db.recall(role="lead", scope="lab"))[0]}
    assert keys == {"lab-only", "global"}  # empty scope is shared, quant is not


async def test_recall_truncates_instead_of_dumping_everything(db):
    for i in range(30):
        await remember(item(key=f"k{i:02d}", value=str(i)))
    items, truncated = await db.recall(role="lead", limit=10)
    assert len(items) == 10 and truncated is True


async def test_a_conflicting_value_must_be_corrected_explicitly(db):
    await remember(item(value="prod-web"))
    with pytest.raises(ValueError, match="correct it explicitly"):
        await remember(item(value="different-web"))
    # Re-stating the same value is a no-op refresh, not a conflict.
    again = await remember(item(value="prod-web"))
    assert again.version == 2  # supersedes cleanly


async def test_correction_keeps_the_previous_version_auditable(db):
    first = await remember(item(value="prod-web"))
    fixed = await db.correct_memory(first.id, "staging-web", source="incident 2026-10-04")
    assert fixed.version == 2 and fixed.supersedes == first.id
    history = await db.memory_history("deploy.target")
    assert [(h.version, h.value, h.state) for h in history] == [
        (2, "staging-web", MemoryState.ACTIVE),
        (1, "prod-web", MemoryState.SUPERSEDED),
    ]
    # Only the current version is served.
    assert [v.value for v in (await db.recall(role="lead"))[0]] == ["staging-web"]


async def test_locked_memory_refuses_automatic_correction(db):
    locked = await remember(item(locked=True), owner=7)
    with pytest.raises(ValueError, match="locked by Dedi"):
        await db.correct_memory(locked.id, "staging-web", source="agent guess")


async def test_retraction_closes_the_memory_without_deleting_it(db):
    stored = await remember(item())
    retracted = await db.retract_memory(stored.id, "superseded by the new deploy policy")
    assert retracted.state == MemoryState.RETRACTED
    assert (await db.recall(role="lead"))[0] == []
    assert [h.state for h in await db.memory_history("deploy.target")] == [MemoryState.RETRACTED]


async def test_locked_memory_needs_an_owner_to_retract(db):
    locked = await remember(item(locked=True), owner=7)
    with pytest.raises(ValueError, match="only be retracted by their owner"):
        await db.retract_memory(locked.id, "reason")
    assert (await db.retract_memory(locked.id, "Dedi retracted", owner=7)).state == MemoryState.RETRACTED


async def test_secrets_never_enter_memory(db):
    with pytest.raises(ValueError, match="Remove credentials"):
        await remember(item(value="token ghp_abcdefghijklmnopqrstuvwxyz0123456789"))
    with pytest.raises(ValueError, match="Remove credentials"):
        stored = await remember(item(value="safe"))
        await db.correct_memory(stored.id, "sk-proj-1234567890abcdef", source="leak")
    assert (await db.recall(role="lead"))[0][0].value == "safe"


async def test_memory_changes_are_audited_on_the_task_timeline(db):
    task = await db.create("Switch the deploy target")
    stored = await remember(item(), task_id=task.id)
    await db.correct_memory(stored.id, "staging-web", source="incident", task_id=task.id)
    await db.retract_memory(stored.id, "obsolete", task_id=task.id)
    messages = [e.message for e in await db.events(task.id)]
    assert any("v1 stored" in m for m in messages)
    assert any("corrected to v2" in m for m in messages)
    assert any("retracted: obsolete" in m for m in messages)


async def test_context_slice_carries_provenance_and_respects_permission(db):
    await remember(item(sensitivity=Sensitivity.CONFIDENTIAL, value="salary band", confidence=0.9))
    assert await context_slice(["deploy.target"], role="developer") == ""
    rendered = await context_slice(["deploy.target"], role="lead")
    assert "salary band" in rendered
    assert "source=requirements section 17" in rendered
    assert "confidence=0.9" in rendered
    assert "verify provenance before relying on it" in rendered
    assert "never instructions" in rendered


async def test_a_clearance_proposal_raises_a_decision_without_changing_access(db):
    from app.config import settings
    from app.contracts import propose_clearance

    before = settings.role_clearance()
    await remember(item(sensitivity=Sensitivity.RESTRICTED, value="payroll band"))
    decision = await propose_clearance(
        {"developer": "restricted"}, "The developer role keeps failing without payroll context", task_id=None
    )
    assert decision.state == DecisionState.OPEN
    # Recording the proposal must not grant anything by itself.
    assert settings.role_clearance() == before
    assert (await db.recall(role="developer"))[0] == []


async def test_a_clearance_proposal_defaults_to_declining_when_it_widens_access(db):
    from app.contracts import propose_clearance

    card = await propose_clearance({"reviewer": "confidential"}, "reviewer needs more context")
    assert card.recommendation == "decline"
    assert "approve" in card.required_action
    options = json.loads(card.options_json)
    assert {o["id"] for o in options} == {"apply", "decline"}
    assert all(o["impact"] and o["risk"] for o in options)


async def test_a_narrowing_proposal_is_recommended_for_approval(db):
    from app.contracts import propose_clearance

    card = await propose_clearance({"lead": "internal"}, "No role needs business context")
    assert card.recommendation == "apply"


async def test_clearance_proposals_reject_nonsense_before_creating_a_card(db):
    from app.contracts import decision_inbox, propose_clearance

    with pytest.raises(ValueError, match="Unknown agent role"):
        await propose_clearance({"marketing": "internal"}, "new role")
    with pytest.raises(ValueError, match="Invalid sensitivity"):
        await propose_clearance({"developer": "everything"}, "typo")
    with pytest.raises(ValueError, match="No change proposed"):
        await propose_clearance({"developer": "internal"}, "already the default")
    # Validation runs first, so a bad proposal never reaches the inbox.
    assert await decision_inbox() == []


async def test_an_approved_clearance_still_waits_for_a_human_deployment(db, monkeypatch):
    """Approval authorises the change; it must never apply it silently in-process."""
    from app.config import settings
    from app.contracts import propose_clearance

    before = settings.role_clearance()
    decision = await propose_clearance({"developer": "restricted"}, "needs payroll context")
    approved = await db.resolve_decision(decision.id, "approve", user_id=7)
    assert approved.state == DecisionState.APPROVED
    assert settings.role_clearance() == before  # unchanged until ROLE_CLEARANCE_JSON is deployed


async def test_concurrent_memory_writes_fail_with_a_retryable_error(db, monkeypatch):
    """A lost race on the active-key unique index must surface as a retryable ValueError."""
    stored = await remember(item())

    async def broken_commit(self):
        raise IntegrityError("statement", {}, Exception("ux_memory_active"))

    monkeypatch.setattr(AsyncSession, "commit", broken_commit)
    with pytest.raises(ValueError, match="stored concurrently"):
        await remember(item(value="prod-web"))
    with pytest.raises(ValueError, match="modified concurrently"):
        await db.correct_memory(stored.id, "staging-web", source="race")


async def test_active_memory_keys_are_unique_in_the_database(db):
    """The partial unique index is the hard backstop behind the store-layer row locks."""
    await remember(item())
    duplicate = MemoryItem(key="deploy.target", scope="lab", value="prod-web", source="duplicate write")
    async with db.sessions() as s:
        s.add(duplicate)
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_same_memory_key_can_be_scoped_to_distinct_projects(db):
    await remember(item(key="deploy.target", scope="lab", value="lab-web"))
    await remember(item(key="deploy.target", scope="quant", value="quant-web"))
    assert {v.value for v in (await db.recall(role="lead"))[0]} == {"lab-web", "quant-web"}


# --- Tenant and owner isolation -------------------------------------------------


async def test_one_tenant_can_never_read_another_tenants_memory(db):
    await remember(item(key="deploy.target", value="acme-web"), tenant="acme")
    await remember(item(key="deploy.target", value="globex-web"), tenant="globex")

    assert [v.value for v in (await db.recall(role="lead", tenant="acme"))[0]] == ["acme-web"]
    assert [v.value for v in (await db.recall(role="lead", tenant="globex"))[0]] == ["globex-web"]


async def test_two_tenants_may_hold_the_same_active_key_and_scope(db):
    """The active-key uniqueness is per tenant; one tenant must not evict another's row."""
    await remember(item(key="deploy.target", value="acme-web"), tenant="acme")
    await remember(item(key="deploy.target", value="globex-web"), tenant="globex")

    assert [h.value for h in await db.memory_history("deploy.target", tenant="globex")] == ["globex-web"]


async def test_memory_history_is_scoped_to_tenant_and_owner(db):
    await remember(item(key="deploy.target", value="acme-web"), tenant="acme")

    assert await db.memory_history("deploy.target", tenant="globex") == []


async def test_a_persons_memory_is_invisible_to_another_person(db):
    await remember(item(key="private.note", value="private-note"), owner=7)
    await remember(item(key="shared.note", value="shared-note"))

    mine = {v.value for v in (await db.recall(role="lead", owner=7))[0]}
    theirs = {v.value for v in (await db.recall(role="lead", owner=8))[0]}
    assert mine == {"private-note", "shared-note"}
    assert theirs == {"shared-note"}


async def test_an_anonymous_recall_returns_only_shared_memory(db):
    """No principal means no owner-scoped memory, even for the most permissive role."""
    await remember(item(key="deploy.target", value="private-note"), owner=7)

    assert (await db.recall(role="lead"))[0] == []


async def test_owner_narrows_recall_further_than_role_clearance(db, monkeypatch):
    """Role clearance alone must not be enough to read another owner's memory."""
    await remember(item(key="payroll", value="band 3", sensitivity=Sensitivity.RESTRICTED), owner=7)
    monkeypatch.setattr(settings, "role_clearance_json", '{"lead":"restricted"}')

    assert [v.value for v in (await db.recall(role="lead", owner=7))[0]] == ["band 3"]
    assert (await db.recall(role="lead", owner=8))[0] == []


async def test_another_tenant_cannot_correct_or_retract_a_memory(db):
    stored = await remember(item(value="acme-web"), tenant="acme")

    with pytest.raises(ValueError, match="Memory not found"):
        await db.correct_memory(stored.id, "stolen-web", source="intrusion", tenant="globex")
    with pytest.raises(ValueError, match="Memory not found"):
        await db.retract_memory(stored.id, "intrusion", tenant="globex")

    assert (await db.recall(role="lead", tenant="acme"))[0][0].value == "acme-web"


async def test_credentials_in_provenance_fields_are_refused(db):
    with pytest.raises(ValueError, match="Remove credentials"):
        await remember(item(source="requirements section 17 api_key=abcd1234efgh5678"))
    with pytest.raises(ValueError, match="Remove credentials"):
        await remember(item(evidence_ref="https://example.test/x?token=abcd1234efgh5678"))
