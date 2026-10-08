# v0.3 implementation and release acceptance

This branch adds the Chief-of-Staff workflow to the existing backend, PostgreSQL queue,
model gateway, decision store and engineering sandbox. It does not create another queue
or a generic workflow platform. Production merge, deployment and rollback remain human controlled.

## Operating choices

- Telegram private-chat messages and `POST /v1/chat` call the same conversational core.
  `/new` and `mode=engineering` preserve explicit engineering intake. Natural engineering
  goals hand off to the existing engine only after selecting a registered project.
- `/dashboard` is the operator console. It shows task summaries, plans, cards, knowledge
  and audit; evidence is a drill-down. Supply the configured API token at connection time.
  The token stays in page memory, is never stored in browser persistence, and is sent only
  to same-origin APIs. Serve the console over HTTPS.
- All six registered domains can analyze and draft. Engineering changes use the existing
  isolated implementation/test/review/PR pipeline. Other domains have no external sending,
  financial, publication or production connector. A model cannot add one through a plan.
- Skill `approval_policy=always` and high-risk drafts pause at a digest-bound approval.
  Approval of a recommendation records a decision; it does not authorize external execution.
- Context includes authorized tasks, decisions, owner/shared memory and bounded registered
  repository document excerpts pinned to the branch SHA. Excerpts are explicitly incomplete;
  unavailable sources are not invented. Repository text is data, never execution policy.
- The shared staff workflow uses the intersection of Lead/Developer/Reviewer clearances.
  No confidential Lead-only memory is forwarded to a lower-clearance specialist.
- Plans are a bounded DAG, at most six work items. Independent review and deterministic
  evidence-reference checks both must pass. Results and cards commit with completion.
- Checkpoints preserve evidence and usage. Changed requirements/plans use new subtask
  checkpoints without deleting old ones. Provider attempts reserve task, subtask and tenant
  daily budgets before making a request. Subtask retries do not receive fresh call limits.
- Daily token accounting deliberately retains worst-case reservations even for failed calls.
  This can stop work early, but cannot silently overspend tokens through concurrent workers.
  Dollar accounting uses reported cost and remains marked incomplete when the provider
  omits it. It is not a guarantee of exact invoiced spend; call/token ceilings remain binding.
- At 60/80/95% the task/daily budget warnings are durable. At 80%, staff prompts request
  concise drafts. No cheaper model is substituted; review, provenance and gates remain.
- One configured tenant is served per runtime. Operator identity comes from server-side API
  configuration or the Telegram allowlist, never a request body. Owned work is private.
  Legacy unowned tasks remain available to the configured HTTP operator for historical
  inspection; the upgrade neither adopts nor auto-deploys those tasks.

## Logical entity mapping

The requirement describes logical entities, not a requirement for one table per name.
These are concrete persisted/configured mappings; tenant or identity administration across
multiple organizations would require a separate future registry migration.

| Entity | Storage / authority |
|---|---|
| users, organizations | configured authenticated operator / Telegram allowlist; `TENANT_ID`, owner columns |
| projects | validated `PROJECTS_JSON` and immutable task policy snapshot |
| objectives, plans, tasks | Task requirement, resolved-intent/plan artifacts, plan JSON and Subtask DAG |
| agents, skills, skill_versions | configured roles and versioned `SKILLS`; model-run roles, subtask pinned skill version |
| memory_items | MemoryItem versions, expiry, locks, owner, tenant and MemoryConflict |
| decisions, decision_actions, approvals | Decision state/actor/timestamp/reason plus append-only audit/events; plan SHA |
| evidence, tool_runs | Artifact and existing sandbox/workspace evidence, context references and events |
| model_runs | ModelRun alias/resolved model/prompt digest/schema/usage/retry/latency/outcome |
| budgets | operator ceilings, requested plan budget, lifetime task/subtask counters, DailyBudget reservations |
| improvement_proposals | ImprovementProposal with reproducible evidence, test plan, hypothesis/risk/benefit and rollback |
| events, audit_log | Event timeline and AuditLog; ORM and database reject audit update/delete |

## API and channel behavior

`POST /v1/chat` requires message, optional project alias and an idempotency key. It returns
summary/status/next_action/decision_required/evidence_refs/risk and the durable intent ID.
Read progress with `/v1/overview`, `/v1/intents/{id}/result` and task evidence endpoints.

Natural scoped actions include `setujui keputusan #12`, `tolak keputusan #12: alasan`,
`revisi keputusan #12: perubahan yang dibutuhkan`, `tunda keputusan #12`, and
`jawab tujuan #42: informasi yang kurang`. Ambiguous approval is never authorization.
HTTP decision actions and Telegram `/decide` also support delegation with a registered
operator ID and reason. Execution approval cards cannot be delegated. Delegation closes
the original card and creates a separate question owned by the recipient.

Memory search is GET `/v1/memory/search?role=lead&keys=...`. Write, explicit correction,
lock and retraction are operator actions. Contradictory values remain visible conflicts;
the active value and earlier versions are retained. Conflict state lowers confidence in
context rather than silently resolving the contradiction.

## Deployment ambiguity recovery

An unresolved deployment with no UUID never auto-retries. Inspect Coolify and submit
`POST /v1/deployments/{id}/reconcile` with the exact approved SHA, evidence, and either:

1. A candidate deployment UUID. The service reads the deployment and application and
   verifies application identity and exact commit before attaching it.
2. `no_submission_confirmed=true`. This explicit operator attestation records
   `not_submitted`, marks the task deployment_failed, and releases the repository.

Known UUIDs cannot be replaced or denied. Reconciliation never calls the deploy endpoint.
Repeated identical recovery returns its durable record. Provider fields follow the
[deployment API contract](https://coolify.io/docs/api/endpoints/deployments/get-deployment-by-uuid),
including string application IDs. New work uses a fresh reviewed task after a failed release.

## Self-improvement and rollback

Repeated failures can produce a durable, evidence-backed proposal; they cannot activate
it. Detection is triggered after staff failures or by the explicit detect API. Review the
hypothesis/test plan/risk, then explicitly start the proposal. `SELF_PROJECT` must name an
explicit registered ai-factory alias. Every self-improvement implementation is approval
bound and runs through the existing branch/sandbox/review pipeline. Merge/release is manual.
`SELF_IMPROVEMENT_ENABLED=false` is the intake/execution kill switch; it does not delete
queued work, data or evidence.

After release, POST the measured before/after evidence to `/v1/improvements/{id}/outcome`.
`retain` stores the measured lesson atomically. `rollback` records only rollback_pending
and creates a production rollback decision; it does not claim a rollback happened.
Approve the rollback card, assess migration/data compatibility, create a compatible revert
revision on the registered branch, perform the approved release, and run smoke checks.
Then call `/v1/improvements/{id}/rollback/confirm` with the full revision, evidence and
`compatibility_checked=true`. A deployed task also requires a finished deployment UUID
whose application and exact revision are verified. Only confirmation stores a rollback lesson.

## Upgrade and rollback of this branch

Use the existing Compose volumes. Configure `SELF_PROJECT` explicitly only when needed;
Compose now forwards tenant, daily budgets and the self-improvement kill switch. Startup
adds columns/tables and a unique subtask index. Decision-table upgrades preserve old cards.
Audit append-only triggers are installed in SQLite/PostgreSQL; production database roles
must allow their installation. Do not drop volumes or historical records to fix an upgrade.

Before deployment, back up PostgreSQL and record current image/commit. Reverting the control
image must preserve the additive schema and evidence. Disable self-improvement, pause
workers, assess active orchestration tasks before an older image (which lacks their runner)
is started, and verify DB/data compatibility. A safe code rollback is not a database rollback.

## Acceptance evidence and remaining release gates

Local verification: Ruff check/format and JavaScript syntax check pass; full pytest passes
343 tests with three PostgreSQL integration tests skipped because no local PostgreSQL
runtime is available. GitHub CI run 37297969466 on candidate 3e745e8160c41725638edbe4b1718659db0a4ecc
passed 345 tests including real PostgreSQL concurrency; Docker sandbox isolation/smoke
and Compose/control-image build jobs also passed. Later changes must retain green CI.

Automated coverage lives in `tests/test_v03_staff.py`, existing engineering/security suites,
and the PostgreSQL concurrent-budget test. It exercises the actual leased SQL workflow
with deterministic model replies, including the exact golden objective, two dependent
skills, reviewed output and three shared cards. It also verifies unauthorized evidence,
owner/tenant/role isolation, API/Telegram intake parity, memory conflict resolution,
checkpoint recovery, approval gates, budgets, audit immutability, old-table upgrade,
blocked L3 execution, recurring-failure proposals, deployment recovery and pending rollback.

The mocked golden test proves the contracts and state transitions. It does **not** prove
that the configured real model identifies three distinct important issues in a real repo.
To claim v0.3 done, require all of these release gates on the same candidate:

1. Ruff, full pytest, real PostgreSQL concurrent tests and real Docker sandbox CI pass.
2. Run `FACTORY_URL=<staging> API_TOKEN=<configured outside git> FACTORY_PROJECT=self
   python scripts/v03_acceptance.py` against a registered staging instance using the
   configured bunny-alpha alias. It creates only a bounded analysis intent and never
   bypasses approval. Record task ID, model runs, exact candidate SHA and evidence.
3. Independently check the three findings are distinct, source-backed and useful, and
   compare their task/card state in Telegram and dashboard. Check clarification, revision,
   rejection, defer, memory correction/lock and budget warnings in both channels.
4. Recreate staging workers/containers and confirm evidence/checkpoints and data persist.
5. Verify one approved sandbox self-improvement through review, controlled release,
   before/after measurement and retain or confirmed compatible rollback.
6. Dedi approves production release of the verified SHA. Deployment health and business
   acceptance are distinct from a successful HTTP response or CI badge.

No live provider key, staging endpoint, Telegram session or production credentials were
available in the implementation workspace. Until real-provider/staging/business acceptance
is recorded, this is an implementation candidate, not a claim of production v0.3 completion.

## Release acceptance record — 2026-10-08 (operator-accepted)

Candidate: main at commit 6e64fae, deployed staging build at https://liobot.kreasheet.com
(`GET /v1/health` → `0.3.0`), configured model alias `bunny-alpha`. Operator credentials
lived outside git; all evidence below is durable server-side at the cited task endpoints.

1. Golden acceptance: `scripts/v03_acceptance.py` with `FACTORY_PROJECT=self` produced
   intent #77, `completed` with 5 model runs / 41,432 tokens; three distinct
   source-backed findings (every `evidence_refs` subset of the persisted context
   artifact) and three durable `recommendation` decision cards. The gate-script GOAL was
   realigned to the engine's `evidence_audit_request` trigger with an anti-drift test
   (commit 6e64fae; regression evidence: intent #76).
2. Clarification fail-closed: "Rencanakan penghematan biaya LioBot untuk bulan depan"
   (intent #78) entered `waiting_input` with a real blocking question and no plan; cost
   USD 0.0033, 1 model run.
3. Idempotency: three identical `POST /v1/chat` requests sharing one idempotency key all
   returned intent #79; exactly one task row was created.
4. Channel parity: a free-text Telegram chat message created intent #80 with the
   identical requirement text in the same core state; the server Telegram poller is
   active (an independent read-only `getUpdates` call returns 409 Conflict); the
   dashboard overview and decision rows come from the same store.
5. Approval path: a generic staff plan gated at `awaiting_approval` with
   APPROVAL_REQUIRED decision card #72 bound to
   `plan_sha256=25eee4ee3da01630e46fb1b67e29a8821dea8463528148f4ba1c4675e13188f7`.
   Approval issued via the operator API under Dedi's explicit cleanup instruction
   (audit `decided_by=7596044503`) resumed the task. The deterministic anti-hallucination
   evaluator then rejected the model draft (findings claiming confidence 0.9–0.95 on
   `unverified` evidence, maximum 0.5, plus one unauthorized evidence ref) and the task
   failed closed — no overstated summary was published.
6. Budget fail-closed: intent #80 stopped at `skill_review` with the task token budget
   exhausted (used 77,264; next reserve 50,417; limit 120,000). Lifetime usage was
   preserved and evidence recorded (`/report 80`, `/logs 80`).
7. Persistence: after an operator Coolify restart, tasks #77/#78/#79, the 13 evidence
   artifacts of #77, its three decision cards and `/v1/health` all remained intact.
8. Operator release approval: Dedi accepted the release on 2026-10-08 ("bisa release").

Outstanding at acceptance time (tracked, not silently dropped):

- Release gate 5 — one approved sandbox self-improvement end to end — is not yet
  demonstrated. It is blocked on the OPEN operator decision to register the ai-factory
  self-target alias (AGENTS.md §19). Until then this record covers the gates that do
  not require self-editing.
- Live channel parity was exercised for clarification, plan approval and the budget
  stop. Revision, rejection, defer and memory correction/lock in both live channels
  remain covered by the mocked contract suites only.
- Hardening backlog observed during acceptance, not accepted behaviour: the budget
  estimator omits workflow/retry calls (`plan_budget_accounting`, intent #80);
  `CompactStaffOutput` caps `evidence_refs` at 4 and caused a validation retry
  (intent #80); `waiting_input` tasks may set `decision_required` without a matching
  decision card (intent #78); generic staff drafts overstate confidence on unverified
  refs (intent #79).
