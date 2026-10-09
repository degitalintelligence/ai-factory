"""Bounded staging evidence collector. Never approves, retries, merges or deploys.

Default mode only inspects. --run-objectives opts into 10 durable read-only tasks
under the existing server budgets. Store the report outside source control.
Operator claim review and channel/restart/self-improvement gates are manual evidence.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx

CORPUS = (
    (
        "golden",
        "Audit AI Factory berdasarkan evidence yang tersedia. Tunjukkan tiga masalah paling penting, rekomendasi perbaikan, dan keputusan yang harus saya ambil minggu ini. Nyatakan bukti yang belum tersedia sebagai keterbatasan. Jangan melakukan perubahan.",
    ),
    (
        "models",
        "Audit repository ai-factory: verifikasi konfigurasi model Lead, Developer dan Reviewer dari evidence yang tersedia. Bedakan source defaults, konfigurasi aktif dan bukti pemanggilan model.",
    ),
    (
        "workflow",
        "Audit repository ai-factory: periksa workflow evidence-audit-v1 dan scope repository berdasarkan source yang tersedia. Laporkan observasi, keterbatasan dan rekomendasi.",
    ),
    (
        "budget",
        "Audit repository ai-factory: periksa budget diagnostics, perbedaan estimasi, reservasi dan usage aktual. Jangan menaikkan budget atau mengganti model.",
    ),
    (
        "readiness",
        "Audit repository ai-factory: periksa readiness berdasarkan evidence yang tersedia. Bedakan observasi runtime dari bukti source dan CI.",
    ),
    (
        "tests",
        "Audit repository ai-factory: verifikasi test suite dan CI pada exact base SHA yang dikunci. Jangan mengklaim tests production berdasarkan CI.",
    ),
    (
        "scope",
        "Audit repository ai-factory: periksa workflow dan batas konteks, terutama target repository dan base SHA. Jangan mengambil evidence telegram-lab tanpa referensi eksplisit.",
    ),
    (
        "provenance",
        "Audit repository ai-factory: periksa workflow audit dan provenance evidence. Bedakan observasi langsung, hasil model terdahulu dan bukti yang belum tersedia.",
    ),
    (
        "decisions",
        "Audit repository ai-factory berdasarkan evidence yang tersedia: periksa workflow, budget dan readiness. Berikan maksimal tiga rekomendasi keputusan konkret yang didukung sumber.",
    ),
    (
        "release_limits",
        "Audit repository ai-factory: periksa model, workflow, budget, readiness dan tests. Jelaskan apa yang sudah terbukti dan keterbatasan acceptance live. Jangan melakukan perubahan.",
    ),
)
MANUAL_GATES = (
    "channel_decisions_memory_parity",
    "restart_persistence",
    "measured_self_improvement",
    "compatible_staging_rollback",
    "real_product_pilot",
    "release_approval",
)


def corpus_digest():
    return hashlib.sha256(json.dumps(CORPUS, ensure_ascii=False).encode()).hexdigest()


def validate_report(report):
    """Fail closed on absent acceptance; collector success is not operator acceptance."""
    issues = []
    sha = report.get("candidate_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or report.get("manifest", {}).get("release_sha") != sha:
        issues.append("Candidate SHA and runtime manifest must match")
    if report.get("readiness", {}).get("status") != "ready":
        issues.append("Runtime readiness evidence required")
    if report.get("corpus_sha256") != corpus_digest():
        issues.append("Frozen corpus digest mismatch")
    trials = report.get("trials", [])
    if {r.get("case_id") for r in trials} != {key for key, _ in CORPUS} or len(trials) != 10:
        issues.append("All ten cases, including failures, are required")
    accepted = [
        r
        for r in trials
        if r.get("status") == "completed"
        and r.get("evidence_refs")
        and r.get("operator_accepted") is True
        and r.get("operator_review")
    ]
    if len(accepted) < 8:
        issues.append("At least eight independently operator-accepted, evidence-backed results required")
    if any(r.get("candidate_sha") != sha for r in trials):
        issues.append("Every trial must be tied to the same candidate")
    ci = report.get("ci", {})
    if ci.get("sha") != sha or ci.get("passed") is not True or not ci.get("evidence"):
        issues.append("Successful same-SHA engine/PostgreSQL/sandbox/compose CI evidence required")
    for gate in MANUAL_GATES:
        data = report.get("manual_gates", {}).get(gate, {})
        if data.get("passed") is not True or not data.get("evidence"):
            issues.append(f"Missing measured/manual gate: {gate}")
    return issues


async def collect(client, candidate_sha, *, run_objectives=False, timeout=600):
    manifest = (await checked(client.get("/v1/release-manifest"))).json()
    health = (await checked(client.get("/v1/health"))).json()
    readiness = (await checked(client.get("/ready"))).json()
    report = {
        "candidate_sha": candidate_sha,
        "manifest": manifest,
        "health": health,
        "readiness": readiness,
        "corpus_sha256": corpus_digest(),
        "trials": [],
        "ci": {"sha": candidate_sha, "passed": None, "evidence": []},
        "manual_gates": {key: {"passed": None, "evidence": []} for key in MANUAL_GATES},
        "release_status": "pending_acceptance",
    }
    if not run_objectives:
        return report
    if manifest.get("release_sha") != candidate_sha or readiness.get("status") != "ready":
        raise ValueError("Configured runtime candidate/readiness mismatch; no objectives created")
    project = os.environ.get("FACTORY_PROJECT", "self")
    if manifest.get("projects", {}).get(project, {}).get("repo") != "degitalintelligence/ai-factory":
        raise ValueError("Acceptance project must explicitly target ai-factory")
    for case_id, message in CORPUS:
        start = time.monotonic()
        trial = {
            "case_id": case_id,
            "candidate_sha": candidate_sha,
            "operator_accepted": None,
            "operator_review": "",
            "evidence_refs": [],
        }
        report["trials"].append(trial)
        response = (
            await checked(
                client.post(
                    "/v1/chat",
                    json={
                        "message": message + f" Exact base commit SHA: {candidate_sha}.",
                        "project": project,
                        "idempotency_key": f"v04:{case_id}:{uuid4()}",
                    },
                )
            )
        ).json()
        task_id = response["intent_id"]
        trial.update(intent_id=task_id, conversation_id=response["conversation_id"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = (await checked(client.get(f"/v1/intents/{task_id}/result"))).json()
            task = data["task"]
            if task["status"] in {"completed", "failed", "cancelled", "waiting_input", "awaiting_approval"}:
                result = data.get("result") or {}
                trial.update(
                    status=task["status"],
                    evidence_refs=sorted(
                        {
                            ref
                            for finding in result.get("findings", [])
                            for ref in finding.get("evidence_refs", [])
                        }
                        | {
                            ref
                            for check in result.get("audit_checks", [])
                            for ref in check.get("evidence_refs", [])
                        }
                    ),
                    evidence_endpoint=f"/v1/tasks/{task_id}/evidence",
                    task=task,
                )
                break
            await asyncio.sleep(5)
        else:
            trial["status"] = "timeout"
        trial["elapsed_seconds"] = round(time.monotonic() - start, 3)
        if trial["status"] != "completed":
            # Stop when operator attention is required; never hide unfinished cases.
            # Remaining cases are explicitly recorded as not run, not successful.
            for pending, _ in CORPUS[len(report["trials"]) :]:
                report["trials"].append(
                    {
                        "case_id": pending,
                        "candidate_sha": candidate_sha,
                        "status": "not_run",
                        "operator_accepted": None,
                    }
                )
            break
    report["acceptance_issues"] = validate_report(report)
    return report


async def checked(awaitable):
    response = await awaitable
    response.raise_for_status()
    return response


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--run-objectives", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-report", type=Path)
    args = parser.parse_args()
    if args.validate_report:
        issues = validate_report(json.loads(args.validate_report.read_text()))
        print(json.dumps({"ready": not issues, "issues": issues}, indent=2))
        raise SystemExit(bool(issues))
    url, token = os.environ.get("FACTORY_URL", ""), os.environ.get("API_TOKEN", "")
    if not url.startswith("https://") or not token or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
        raise SystemExit("Configure HTTPS FACTORY_URL, API_TOKEN outside git and exact --candidate-sha")
    async with httpx.AsyncClient(
        base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as client:
        report = await collect(client, args.candidate_sha, run_objectives=args.run_objectives)
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(output + "\n")
    else:
        print(output)


if __name__ == "__main__":
    asyncio.run(main())
