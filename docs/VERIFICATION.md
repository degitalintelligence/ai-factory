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

## Task #52 Engineering budget regression

The anonymized Task #52 fixture retains the real Lead plan (20 calls, 30,000
tokens, $0.50), requirement, bounded source context and locked lab SHA. Regression
replays its exact `used=13088, next_reserve=22402, limit=30000` store rejection.
Fresh plan admission allocates implementation and independent review within
operator ceilings before saving the plan. A real Git/SQL orchestration regression
then traverses Developer, sandbox test evidence, independent review, deterministic
gates and one PR using mocked external boundaries. It verifies budget flow, not
the implementation of `/todo count` itself.

Negative tests retain explicit budgets, saved plan bytes/lifetime usage, low
operator limits and exhausted reported cost. High-risk approval binds the
admitted plan hash and resume preserves that exact plan. UTF-8 and output
allowances affect admission through the same reservation helper used by actual
gateway calls. Real provider completion still requires a new bounded lab task
after deployment; historical #52 must not be retried expecting a budget reset.

Task #53 regression reproduces the exact `calls=20/20` failure and verifies fresh
admission funds one configured Developer iteration plus review attempts. A scripted
24-action regression runs the real Developer loop and workspace tools against a
temporary Git repository, recovers exact-anchor failures, rejects a tracked runtime
artifact, repairs an unsupported async test pattern, inspects a fresh diff and
reaches independent review/publication in 26 accounted calls. Models, sandbox test
responses and GitHub remain mocked; this is not a replay of unavailable model
outputs or proof of live `/todo count` behavior. Additional tests keep the final
review call, constrain gateway retries, require a successful diff after mutations
and keep long replacement payloads out of history while exposing the error in the
first 600 trace characters. Original runtime-data, test-failure, source-integrity
and review gates remain mandatory. Real provider acceptance requires a fresh lab
task after release; #53 keeps its saved budget and history.

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

Task #54 regression retains the supplied sandbox report (14 passing tests with a
`todos.db` artifact issue) and first finish action. Scripted model actions exercise
the real Developer loop, SQL state, workspace/Git, mandatory tests, review and
publication gates; model, sandbox responses and GitHub are mocked. A controller
finish checkpoint collects the latest complete diff without another model call.
Artifact issues reject publication even when the model approves; a second bounded
iteration repairs test storage before a single PR is allowed. Failed diff hygiene
checks still block handoff. This is not an actual provider replay or live lab test.

Task #55 regression captures the supplied Telegram metadata and last-step tool
sequence. Only steps 21--30 were supplied, and mutation payloads were omitted; the
regression uses an equivalent scripted tail with a synthetic earlier prefix. Real
Developer tools, Git, SQL and publication gates exercise 30 authoring calls plus
Lead and Reviewer. A correct final repair proceeds through mandatory checks and
a single PR; an incorrect final repair fails even with an approving review.
Unsafe/empty/read-only exhaustion still blocks handoff. Shell environment commands
remain rejected with fixture guidance. Models, sandbox execution and GitHub are
mocked; this does not certify the actual Task #55 patch or live model competence.

Task #56 regression retains the supplied repository/SHA, usage, test-artifact and
Reviewer-conflict strings. It exercises two real orchestration iterations with
workspace/Git/SQL and publication gates: an approving Reviewer cannot override
the `todos.db` sandbox issue, the next Developer receives one deterministic
temporary-storage repair instruction, and only a clean second test/review may
produce one PR. Unrelated review issues remain. Models, sandbox execution and
GitHub are mocked; the regression does not claim the failed task branch was valid
or replay the provider. The Reviewer prompt independently treats deletion of a
tracked runtime database as corrective source hygiene.

Task #57 regression retains the supplied target repository, exact base SHA,
usage, failure stage and tool-step sequence. After a successful source mutation,
six stale exact-match replacements fail, then a read of that same file is treated
as bounded recovery progress instead of triggering the generic eight-step stall.
The first exact-match failure also returns a bounded current-file excerpt, while
replacement payloads remain compacted from traces. Repeated or unrelated reads
still hit the existing stall guard. The scripted actions are an equivalent replay;
omitted provider payloads and live model behavior are not certified.

Task #59 regression retains the supplied target/SHA, `33/34` call exhaustion,
three-iteration policy, test result and `todos.db` artifact failure. Fresh
Engineering admission now funds one full Developer iteration plus a seven-action
repair baseline and one independent review for every remaining iteration, with a
small schema-retry margin. Runtime allocation records each iteration's effective
step limit and reserves those later calls before authoring. A deterministic diff
gate also rejects Python test functions removed without an equivalent definition,
even when pytest and the model review say approved. The regression verifies budget
math and gates; it does not replay omitted model payloads or certify live quality.

Task #62 regression retains the supplied target/SHA, final `49/50` call reserve,
three-iteration failure stage, ambiguous `matches=2` replacements and tracked
`todos.db` blocker. The Developer loop now treats repeated failed anchors on one
path as a bounded stall even when the payload changes. After a sandbox command
reports a tracked runtime artifact, unrelated edits and test reruns are rejected
until the model explicitly deletes that artifact; the controller never deletes it
silently. Python writes also reject newly duplicated module-level definitions. A
safe nonempty diff from the final stalled iteration still enters the unchanged
mandatory test and independent-review gates without another Developer call. The
fixture is an equivalent deterministic replay; it does not certify the failed
task branch or live provider behavior.
