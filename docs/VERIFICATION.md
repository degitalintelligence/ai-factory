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


Task #48 audit consistency regression rejects missing/altered active model IDs,
tenant daily budget limits and usage, a readiness-denying summary alongside a
successful probe, and unsolicited production test execution advice. A model
review approval cannot override these defects; the existing single bounded
repair still requires a fresh independent review. Runtime fields are returned
as exact `observed_values` strings (cost snapshots rounded to six decimal places)
and rendered alongside the reviewed observations. Legacy saved outputs remain
readable with an empty values map. Known contradiction patterns are checked
deterministically; this is not proof of general natural-language entailment.

Pinned source excerpts plus README replace redundant default document excerpts
for scoped audits with selected code paths. Complete context metadata stays in
the evidence artifact; prompts use only refs, source kind, content and freshness
label after the existing authorization filters. Regression checks ensure all
authorized refs survive this compaction. Real provider token savings and the
new answer contract require one bounded post-deployment acceptance audit; local
mock tests do not establish model quality.

Task #49 acceptance adds positive cases for absent actual model execution logs,
real-time/historical budget data and external endpoint proof while settings,
budget snapshots and internal readiness are present. Negation checks now keep
contrast and limitation clauses separate and look for the observed subject;
explicit denial of available settings, budget snapshot/limits or readiness still
fails. This remains a narrow consistency check, not general language entailment.
CI observed_values now have an explicit exact-SHA run/status/test-step contract,
using the same completed-successful-run rule as CI verification. Missing or wrong
SHA CI cannot acquire verified fields. A valid audit regression must traverse
both skills and independent reviews to completion without repair, alongside the
existing rejected-output tests. Real provider acceptance remains required.

Scoped audit plan objectives are short stable descriptions; observation fields
and prompt rules are not copied into the 2,000-character plan step field. Rejected
schema-valid drafts are retained as redacted `staff_rejected_draft` diagnostics
with step/phase and local issues, subject to existing task ownership/report rules.
This diagnostic does not approve the answer or relax publication gates.

Task #50 regression uses the actual rejected initial and repaired outputs in an
anonymized fixture (only referenced evidence retained; operator owner replaced).
Both validate, and the initial answer traverses both skills and independent
reviews to completed with no repair and no rewrite. The production-test advisory
check now uses word-boundary action verbs and negation of the nearest action in
the same clause. Maintaining CI "tanpa menjalankan tes di produksi", runtime
mentions, and missing production evidence are not execution proposals. Positive
production-test directives still fail outside requested scope, even when another
clause says "jangan" or "do not". This is a bounded advisory text check, not a
general language parser or an authorization grant; permission/execution gates
are unchanged. Provider acceptance after deployment remains separate from the
exact-output replay and infrastructure tests.
