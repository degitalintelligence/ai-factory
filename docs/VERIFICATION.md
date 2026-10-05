# Acceptance and verification

## Automated gates

| Area | Evidence |
|---|---|
| Durable queue | Idempotency, per-repo serialization, lease expiry/recovery, stale-worker fencing, cancellation and lifetime budgets |
| V0.1 compatibility | Additive migration applied twice with an existing historical task |
| Review integrity | New/untracked files in complete diff; changed source cannot commit; every criterion needs unique evidence |
| Security | Traversal, symlink, env/Git access rejection; token redaction; Telegram private-chat allowlist and task ownership; HTTP auth |
| Orchestration | Actual temporary Git repository + simulated model/GitHub calls through planning, code, checks, review and publication |
| Recovery | Lost response after push resumes publication without recoding, duplicate push or duplicate PR |
| Feedback | Second tested commit updates the same PR; high-risk/question plans pause before code execution |
| Release | Full SHA, merged reviewed head/tree, CI checks, pinned registered application, at-most-once submission and ambiguous-response handling |
| Sandbox | Upload validation, command routing, source/test-artifact detection, dependency opt-in, offline test policy and fail-closed HTTP behavior |
| Real infrastructure CI | PostgreSQL 16 concurrent claims; Docker-built runner filesystem/environment/network isolation and real passing/failing pytest workloads |

Local tests do not consume real provider tokens or contact Telegram, GitHub or Coolify. The orchestration suite mocks these boundaries while exercising actual SQL state and Git diffs/commits. PostgreSQL integration is skipped unless a dedicated `TEST_POSTGRES_URL` is configured. Real sandbox smoke verification lives in `scripts/sandbox_smoke.py` and the separate CI job.

## Live acceptance after installation

1. Confirm `/ready` and the runner namespace healthcheck.
2. From an unauthorized Telegram account/group, verify that task creation is denied.
3. From the operator account, create a small task, note its exact target repo/branch, and verify a complete tested PR. Resending the same Telegram update must not duplicate it.
4. Restart the factory during a task. Verify one recovered task, bounded recovery counter and a single PR.
5. Introduce a failing test in a disposable target task. Verify no PR is created even if an AI review says approved.
6. Add `/feedback` to an open PR; verify the PR receives a new reviewed commit and no second PR.
7. Request a stateful product with a deployment pack. Review the volume path, environment placeholders, health behavior and backup/rollback procedure. Build it in target CI.
8. On a registered staging Coolify app, deploy the exact merged SHA. Check reported commit, actual app health, persistent data after recreation and the user-visible acceptance criteria before using production.

A passing unit suite or generated Compose file does not establish live model quality, ARM image build compatibility, target application behavior or a successful production deployment. Record those results on the actual staging installation.

## Task #47 audit quality regression

`tests/test_repository_audit_evidence.py` exercises pinned source reads, active
model settings overriding repository defaults, tenant-isolated daily budget
snapshots, readiness availability, exact-SHA CI filtering, per-topic rendering
and backwards-compatible historical output loading. It rejects the Task #47
style documentation-only answer even when the model reviewer approves, and
accepts a complete five-topic report with the exact reviewed checks preserved.
It also rejects successful CI from a different SHA, failed test steps, irrelevant
source references, missing topics, instruction leakage and claims of test success
based only on source definitions. Readiness and GitHub are mocked; these tests do
not establish production readiness or live model response quality.

Live acceptance after release: repeat the Task #47 read-only chat audit. Expect
one result for each requested topic with explicit verification kind and refs.
Check active role/model settings, budget snapshot time/counters, direct readiness
observation and exact-commit CI evidence. If a source or probe is unavailable, the
result must report that limitation rather than invent success or an unrelated
manual ARM verification requirement. Inspect `/report` for the pinned code and
observation refs. Merge and deployment remain separate operator actions.
