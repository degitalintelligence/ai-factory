from app.agents import normalize_lead_plan
from app.schemas import LeadPlan


def test_publication_only_acceptance_is_deferred_even_when_model_omits_index():
    plan = LeadPlan(
        objective="Add a regression test",
        acceptance_criteria=[
            "The callback is covered by an offline test",
            "The PR body records the test evidence and PR URL",
        ],
    )

    normalized = normalize_lead_plan(plan)

    assert normalized.post_publication_criteria == [2]
