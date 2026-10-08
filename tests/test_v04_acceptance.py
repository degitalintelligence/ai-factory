"""Acceptance cannot become green merely because a collector received HTTP 200."""

import json

import httpx
import pytest

from scripts.v04_acceptance import CORPUS, MANUAL_GATES, collect, corpus_digest, validate_report


def reviewed_report():
    sha = "a" * 40
    return {
        "candidate_sha": sha,
        "manifest": {"release_sha": sha},
        "readiness": {"status": "ready"},
        "corpus_sha256": corpus_digest(),
        "trials": [
            {
                "case_id": key,
                "candidate_sha": sha,
                "status": "completed",
                "evidence_refs": ["repo:self:source"],
                "operator_accepted": True,
                "operator_review": "Checked claim against cited source",
            }
            for key, _ in CORPUS
        ],
        "ci": {"sha": sha, "passed": True, "evidence": ["same-SHA CI run"]},
        "manual_gates": {
            key: {"passed": True, "evidence": ["measured staging evidence"]} for key in MANUAL_GATES
        },
    }


@pytest.mark.parametrize("missing", ["operator_review", "same_sha", "all_cases", "ci", *MANUAL_GATES])
def test_incomplete_live_acceptance_cannot_claim_ready(missing):
    report = reviewed_report()
    if missing == "operator_review":
        for trial in report["trials"]:
            trial["operator_accepted"] = None
    elif missing == "same_sha":
        report["manifest"]["release_sha"] = "b" * 40
    elif missing == "all_cases":
        report["trials"].pop()
    elif missing == "ci":
        report["ci"]["passed"] = None
    else:
        report["manual_gates"][missing]["passed"] = None
    assert validate_report(report)


def test_all_gates_require_frozen_corpus_and_keep_failed_cases():
    report = reviewed_report()
    report["trials"][0]["status"] = "failed"
    report["trials"][1]["status"] = "failed"
    assert not validate_report(report)
    report["trials"][2]["status"] = "failed"
    assert validate_report(report)


async def test_default_inspection_never_submits_tasks():
    requests = []

    def server(req):
        requests.append(req)
        return httpx.Response(200, json={"release_sha": "a" * 40, "status": "ready"})

    async with httpx.AsyncClient(
        base_url="https://factory.invalid", transport=httpx.MockTransport(server)
    ) as client:
        report = await collect(client, "a" * 40)
    assert len(requests) == 3 and all(r.method == "GET" for r in requests)
    assert report["release_status"] == "pending_acceptance" and validate_report(report)


async def test_wrong_runtime_revision_fails_before_task_submission():
    methods = []

    def server(req):
        methods.append(req.method)
        return httpx.Response(200, json={"release_sha": "b" * 40, "status": "ready"})

    async with httpx.AsyncClient(
        base_url="https://factory.invalid", transport=httpx.MockTransport(server)
    ) as client:
        with pytest.raises(ValueError, match="candidate/readiness"):
            await collect(client, "a" * 40, run_objectives=True)
    assert all(method == "GET" for method in methods)


async def test_failed_objective_is_retained_and_never_auto_approved_or_retried(monkeypatch):
    def server(req):
        if req.url.path == "/v1/release-manifest":
            return httpx.Response(
                200,
                json={
                    "release_sha": "a" * 40,
                    "projects": {"self": {"repo": "degitalintelligence/ai-factory"}},
                },
            )
        if req.method == "POST":
            assert req.url.path == "/v1/chat"
            body = json.loads(req.content)
            assert "a" * 40 in body["message"]
            return httpx.Response(202, json={"intent_id": 1, "conversation_id": "thread_1"})
        if req.url.path.endswith("/result"):
            return httpx.Response(200, json={"task": {"status": "awaiting_approval"}, "result": None})
        return httpx.Response(200, json={"status": "ready"})

    async with httpx.AsyncClient(
        base_url="https://factory.invalid", transport=httpx.MockTransport(server)
    ) as client:
        report = await collect(client, "a" * 40, run_objectives=True)
    assert report["trials"][0]["status"] == "awaiting_approval"
    assert all(t["status"] == "not_run" for t in report["trials"][1:])
    assert len(report["trials"]) == 10 and report["acceptance_issues"]
