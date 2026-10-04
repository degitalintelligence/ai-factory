from app.schemas import LeadPlan, ReviewResult


def test_lead_plan_validation():
    plan = LeadPlan(
        objective="Add hello command",
        acceptance_criteria=["Replies to /hello"],
        constraints=[],
        suggested_tests=["pytest"],
        risk="low",
    )
    assert plan.risk == "low"


def test_review_result_validation():
    review = ReviewResult(approved=False, summary="Missing test", issues=["Add test"])
    assert review.approved is False
