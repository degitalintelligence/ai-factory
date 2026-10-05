# Operations: Oracle ARM / Ubuntu 24.04 / Coolify

## Upgrade from V0.1

1. Let running V0.1 tasks finish before changing the resource branch; its old background jobs did not have durable leases. Take a PostgreSQL backup and retain the current image/commit for rollback.
2. Keep the same Coolify Compose resource/project. Preserve named volumes `ai_factory_postgres` and `ai_factory_workspaces`; do not delete volumes or recreate the resource under a new project name.
3. Add `TELEGRAM_ALLOWED_USER_IDS` (your numeric user ID, comma-separated for multiple operators) and a newly generated `SANDBOX_TOKEN`. Keep your existing `POSTGRES_PASSWORD`, model IDs, GitHub/OpenRouter/Telegram credentials. Do not put credentials in build arguments.
4. On the Ubuntu Docker host, run `sudo bash scripts/install-sandbox-profile.sh` from this reviewed checkout once. This loads `deploy/apparmor/ai-factory-sandbox` under its own profile name and changes no global sysctl. Then set the reviewed V0.2 branch/commit and redeploy using `docker-compose.yaml`. `workspace-init` adjusts ownership of the existing workspace volume for UID 10001. The app applies additive migrations and keeps V0.1 task rows.
5. Check `postgres`, `sandbox`, then `ai-factory` readiness. The control endpoint `/health` means process alive; `/ready` means DB, worker, Telegram (if enabled) and sandbox are responding.
6. Send `/start`, `/projects`, then a small `/new` acceptance task. Confirm that `/status` identifies the target repo and `/report` contains passing test/review evidence. Confirm the PR in GitHub before merging it.

Existing V0.1 task rows remain historical: they have no stored owner/policy/source digest. Do not use them for automatic feedback/deployment; start a V0.2 task. The operator API can still inspect the old records. Existing Postgres passwords must remain unchanged unless changed inside Postgres too—changing an environment variable does not change credentials in an initialized volume.

Compose uses `DATABASE_HOST` and `DATABASE_PASSWORD`; the application URL-encodes the password instead of interpolating it unsafely into a DSN. Direct local use may still set `DATABASE_URL`.

## Sandbox readiness troubleshooting

The runner needs Linux unprivileged user namespaces. The Compose runner alone has `seccomp=unconfined`, `systempaths=unconfined` and `apparmor=ai-factory-sandbox` to permit nested namespaces and a fresh private `/proc`; Docker's default masked `/proc` paths otherwise make Bubblewrap fail while mounting its PID namespace. The credential-bearing control service keeps the default Docker profiles. No `privileged: true`, host network, host PID namespace, host directory mount or Docker socket is needed.

Ubuntu 24.04 can reject loopback setup with `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` even with the generic unconfined Docker profile. The supplied explicit profile grants `userns` to this runner only. Install it on the host before deployment; Docker intentionally fails if the named profile is absent. On hosts without AppArmor restrictions, the operator may set `SANDBOX_APPARMOR_PROFILE=unconfined`, but Ubuntu should retain the named profile.

Inspect `sandbox` logs and `/health`. On namespace setup failure, leave the service blocked. Check Docker/rootless/user-namespace policies and Ubuntu's AppArmor restrictions for this runner on your host. Do not disable security globally and do not add a direct-host command fallback. If host policy forbids this isolation profile, run the sandbox on a separate appropriately configured Linux machine over a protected connection, using `SANDBOX_URL`/`SANDBOX_TOKEN`. Never expose the runner publicly.

The runner receives only `SANDBOX_TOKEN` and the dependency-download toggle. Do not inject GitHub, OpenRouter, Telegram, production DB or Coolify credentials into it. Test subprocesses receive neither variable.

## Dependency and stack setup

Python default gate: compileall plus pytest (empty test collection fails). Runner image already includes the factory's pinned Python dependencies. For target-specific wheels, set both `SANDBOX_INSTALL_DEPS=true` and project `install_dependencies=true`. Requirements are installed afresh per snapshot, so declare runtime and test dependencies explicitly. Builds from source are disabled.

Node default gate: `npm test`, followed by `npm run build --if-present`. Supply a meaningful test script and a lockfile for `npm ci --ignore-scripts`. No-test shell scripts that return zero must be rejected in review. Frameworks requiring postinstall/native build scripts need a prebuilt sandbox image; the engine does not silently enable them.

Sandbox tests are offline; mock external Telegram/payment/database APIs. Temporary fixtures use pytest `tmp_path` or in-memory databases. A real restart/recreation test must be included for persistent product features, and deployment smoke tests must verify its actual mounted storage.

## Budgets and queue controls

## Model aliases

The v0.3 default uses `bunny-alpha` for Lead, Developer, and Reviewer. The
operator may map it through `MODEL_ALIASES_JSON`; the repository default maps
it to `stealth/space-bunny-alpha`. Alias changes are configuration changes
and require the same review and approval discipline as any model/provider change.

### Structured model output failures

`/report <id>` and `/logs <id>` include `llm_validation` diagnostics for each rejected completion after this update. These identify the schema/role, attempt, and error category: malformed JSON, schema fields, empty content, missing choices, refusal, or truncated output. Report artifacts also record the configured model and output token limit. Raw responses, unknown field names, and input values are deliberately excluded. Earlier failures cannot be reconstructed from these new diagnostics.

The engine sends specific validation feedback on retry and documents the Developer action format explicitly. A single JSON Markdown fence is accepted; prose, action arrays, unknown action names and extra fields remain invalid. A syntactically valid JSON object does not necessarily satisfy the action schema. The engine uses JSON-object mode and local validation; it does not assume that every configured model supports provider-enforced JSON Schema.

After deploying a fix, retry the failed task once and inspect its new diagnostics if it fails again. For `truncated`, inspect the configured output limit and task size before adjusting them; reasoning models may consume output budget before returning JSON. No automatic token-limit increase or model switch is performed. A passing mocked regression suite does not establish live compatibility with a provider: verify one real task through tests, independent review and PR creation.

Defaults: one worker, four review iterations, 36 developer steps per iteration, 8 consecutive stall steps, 150 model calls, 600k aggregate provider-reported tokens, $5 reported cost and one hour per attempt. Developer context is capped separately from the global prompt budget. Budget warnings are emitted at 60%, 80%, and 95%; exhaustion never resets through `/retry`. Choose model IDs or the `bunny-alpha` alias explicitly in Coolify; no silent provider fallback can increase spending.

Each malformed-output retry consumes a call and records returned usage. Network failures can be billed by the provider without returned usage. Reported cost can therefore be partial. Use a provider-level credit limit for a strict dollar ceiling. `/retry` retains call/token/cost totals; reaching the budget requires a deliberately new bounded task or an operator policy change, not an automatic budget reset.

`/cancel` is cooperative; a running model/sandbox request may finish and consume cost. Cancellation does not undo a completed Git push/PR or a deployment. Check the terminal task status. Restart recovery is bounded; after repeated failures inspect logs instead of repeatedly restarting the container.

Keep a single Telegram polling replica. If scaling the worker code into additional processes, disable Telegram in additional control instances; use PostgreSQL (not SQLite), retain shared task workspace storage, and provision enough sandbox capacity. The default sandbox accepts one job at a time; unsupported concurrency receives HTTP 429.

## HTTP operator interface

Set a separate `API_TOKEN` and, when it is set, `API_OPERATOR_USER_ID` is **required**: startup fails closed without it, and every request is rejected with 401 rather than acting as nobody. That numeric identity is what the audit trail records for an approval, deployment, or memory read, and it comes from the server-side credential — the request body cannot name a different actor. Empty token means endpoints fail closed. The decision answer body accepts only `answer`; callers cannot spoof `user_id`. Use `Authorization: Bearer ...` for `/tasks`, `/tasks/{id}`, `/tasks/{id}/events`, `/tasks/{id}/artifacts` and `/v1/memory/search`. POST `/tasks` with `requirement`, `project` and optional `idempotency_key`. Reuse a key only for the same request. POST `/tasks/{id}/{cancel|retry|answer|approve|feedback}` with a JSON `message`. The token grants operator-wide access: do not distribute it to customers.

`ROLE_CLEARANCE_JSON` maps each agent role to one of `public`, `internal`, `confidential`, `restricted` and is validated at startup, so a typo fails closed instead of silently widening or narrowing memory access. It never overrides ownership: a memory owned by a person is readable only by that person, and a read with no principal returns only shared unowned memory.

## Optional Coolify release integration

Create the target application in Coolify once and configure secrets/domains/persistent volumes there. Register its UUID as `coolify_uuid` in the project policy, set `COOLIFY_URL` and a scoped `COOLIFY_TOKEN`, and disable that target's auto-deploy. This release does not automatically provision resources or set production secrets.

After reviewing and merging a generated PR, send `/deploy <task-id> <full-merged-commit-sha>`. The engine checks identity/CI/tree integrity and pins the application to that commit. Subsequent deployments require new explicit approved task releases because the pin remains set. `/deployment <id>` fetches the remote status and verifies the reported commit when supplied.

For a test-only or otherwise non-deployable project, send `/publish <task-id> <full-merged-commit-sha>` instead. The engine performs the same PR, branch, tree and check validation, records the merged commit in the release ledger, transitions the task to `completed`, and does not call Coolify. `/deploy` remains a backward-compatible alias for this acknowledgement when the project policy has `require_deployment: false`.

If the PR was merged but the base branch advanced before release, do not reuse the stale SHA. Send `/supersede <task-id> <reason>`. The engine verifies that the reviewed PR was merged unchanged, that the current base still contains that merge, records an auditable `superseded` terminal state without publishing or deploying, and releases the repository reservation for a fresh bounded review.

Review-only/no-op requests are supported: when the requirement explicitly allows no correction, the engine runs mandatory tests and independent review without forcing a file mutation. A passing result ends in `reviewed`; no commit, push, PR, or deployment is created. If a correction is found, the task fails with evidence and a separate bounded implementation task is required.

If status is `unknown`, inspect Coolify's deployment history before doing anything else; the record intentionally blocks automatic duplicate submission. If a deployment failed, diagnose logs and prepare a corrected new task. Do not treat an HTTP response or queued UUID as successful deployment.

## Backup, retention and rollback

Back up Postgres daily and before upgrades using Coolify's database backup facilities or `pg_dump` executed with credentials supplied securely by the operator. Also back up the workspace volume if you need to recover unpushed changes. Store backups outside the VPS and periodically restore into a disposable instance. Task artifacts are stored in PostgreSQL; text traces can grow with usage, so monitor disk space and add an operator-approved retention policy. This release does not delete old evidence/workspaces automatically.

To roll back the factory release, stop task intake, let/cancel active work, retain a backup, and redeploy the previous image/commit against the existing volume. Added DB columns/tables are compatible with V0.1 reads; V0.1 does not understand new statuses, ownership or recovery. Avoid downgrading while V0.2 tasks are active. A backup restore is a separate deliberate data decision; never delete volumes as a troubleshooting shortcut.

For a target application rollback, use a previously validated immutable image/commit in Coolify and assess migration/data compatibility first. The factory never automatically restores a database or rolls back a stateful release.


### Chief-of-Staff audit evidence and approval notifications

Audit context includes recent owner/tenant-scoped task summaries, up to four event
excerpts, three artifact excerpts, and three model-call metadata records per task.
These are bounded excerpts, not complete `/report` or `/logs` output. Effective
budget limits reflect the current operator configuration and the task plan; they
are not proof of historical configuration. Missing provider cost remains explicit.
Unavailable acceptance proof or root-cause evidence is reported as an evidence gap;
only questions needed to define the objective, target, or safe authority pause intake.
A high-risk analysis still pauses for approval of the exact saved plan. Its message
includes the decision ID, planned steps, risks, and effective budget. Review the
card before replying `setujui keputusan #ID`; approval grants L0/L1 analysis only.
Previously paused tasks retain their saved intent until the owner supplies an answer
and the normal workflow replans. Production acceptance requires live evidence.


### Plan call feasibility

Before approval or staff execution, LioBot counts already consumed task calls,
two calls per unfinished step (output and independent review), and two final
calls (synthesis and review). Completed checkpoints with the same execution hash
are excluded. This is a minimum, not a guarantee of token/cost availability or
provider retries; runtime task/subtask/daily ceilings remain binding.
The planner receives call usage and operator limits. A fresh infeasible plan may
be revised once, only when that extra call and the minimum required skill coverage
fit. The admitted task budget and high-risk approval requirement cannot be
raised or removed by that revision. An infeasible saved or revised plan stops
before asking for execution approval. Saved plans are never silently rewritten.
Approval messages include calls used, minimum remaining calls, and retry headroom.
Shortened descriptions end with an ellipsis at a word boundary; full plans remain
available through the existing plan endpoint/Telegram command. All human-facing
model fields are instructed to use Indonesian; real-provider quality still needs
live validation.


### Structured-output failures during staff planning

A staff plan repair permits exactly one provider attempt (`max_attempts=1`),
matching the call reserved by the feasibility calculation. General structured
calls retain at most three attempts; all attempts remain charged and audited.
Staff intent and plan prompts request compact JSON instead of repeating audit
findings and long evidence references inside control fields. These are output
instructions, not proof of real-provider compatibility or language quality.
Validation artifacts include finish reason, text character count, prompt/output
token counts, and reasoning tokens when reported; raw completion text is not saved.
Budget failure notifications include the specific redacted cause and task-specific
`/report ID` and `/logs ID` commands. The blocked card also records that cause.
Task #24 demonstrated seven planning/repair calls used with four calls still
required under a ten-call limit, following invalid/truncated JSON. It did not
run audit subtasks or establish successful live v0.3 acceptance. Increasing calls
alone cannot establish structured-output compatibility. Inspect the recorded
validation metadata and validate a bounded configured-provider run before claiming
that provider quality or truncation is resolved.


### Intake accounting and named chat-task evidence

Before saving a fresh model-generated staff plan, the engine includes all charged
intent/planning attempts, two calls per unfinished step, and two final calls.
If the model's call estimate is below that minimum, the engine may admit the
minimum plus one retry call, capped by the existing operator call ceiling. The
`plan_budget_accounting` artifact records the estimate, usage, minimum, admitted
limit, operator ceiling, and headroom. This admission correction does not change
operator, token, cost, daily, or subtask limits. Saved/approved task budgets are
never raised. Requests mentioning budget/call/token/currency constraints skip
this correction conservatively; an infeasible task remains blocked. An operator
ceiling below the minimum also remains blocked.

Unscoped staff goals include their owner's unscoped chat-task history alongside
registered project history. Explicit `task #ID`, `tujuan #ID`, or `intent #ID`
references are prioritized before the 30-task SQL limit and context-size limit.
Tenant and owner filters still apply. A project-scoped goal does not gain access
to unscoped chat history or another registered project by naming a task ID.
Task #26 supplied a concrete regression: a model estimate of five calls omitted
the already charged intent call, and its context had omitted chat task #24.
Configured-provider output quality and live completion remain separate acceptance
checks; passing deterministic tests does not establish those outcomes.


### Output/review retry reservations

Fresh model-generated plans may reserve one shared retry per output/review pair
by admitting a three-call subtask estimate, when the existing operator ceiling
allows it. Intake accounting now funds subtask allowances and final calls before
saving a model estimate that is too small. Operator/user-specified/saved limits
remain unchanged; no retry resets usage or increases those limits.
Gateway attempts are capped by both the remaining subtask allowance and available
task calls after reserving downstream work. Output generation preserves one
independent review call; step review preserves later steps and finalization;
final synthesis preserves one final review call. Invalid JSON is never used as
approval. Exhausting the allowed validation attempts records the structured-output
failure instead of initiating another attempt against a depleted subtask.
Task #27 supplied the regression: a two-call subtask used one output call, then
its reviewer returned invalid JSON and had no retry allowance. Tests exercise the
actual JSON gateway with mocked HTTP responses, including valid and repeatedly
invalid reviewer retries. They do not establish real-provider compatibility.

### Bounded factual task explanations

The explicit read-only request `Analisis task #24 saja. Jelaskan satu penyebab
berhenti berdasarkan evidence yang tersedia. Maksimal 150 kata. Jangan melakukan
perubahan.` now uses `FactualOutput`, without priority, alternatives, or decision
fields. The supported conservative contract accepts task/tujuan/intent IDs and
word limits from 30 to 150; other wording continues through the normal planner.
Named evidence is still filtered by tenant, owner, registered project scope and
current policy. Unavailable or unauthorized evidence stops without model calls.
This contract reads only the named task summary and bounded diagnostics, without
repository requests, unrelated tasks, decisions, or memory.

A fixed single-engineering-step plan replaces the model planning call. Intent
resolution, step review, final synthesis/review, leases, immutable checkpoints,
call/token/cost/daily ceilings and fail-closed evaluation remain in place. A valid
run uses five model calls before retries. Fresh call allowance is at most charged
intake calls plus seven, capped by the operator; token/cost defaults remain
120,000 tokens and USD 1, also capped by the operator. Saved plans are never raised
or converted to the new contract. No model or provider change is included.

The rendered factual response must fit the requested word limit, cite supplied
references and receive independent approval. Review checks human prose, rather
than treating required schema keys/enums as language defects. A locally valid
draft and its review are retained as `factual_draft` and
`factual_draft_evaluation`, including rejected drafts; they are not completed
results. ValueError notifications include the redacted failure reason.
Mocked gateway and regression tests do not prove configured-provider output
quality. Deploy the exact merged SHA, then send the request above as a new goal
and inspect its response/report before claiming live acceptance. Existing failed
tasks retain their lifetime budget and saved contracts on retry.

### Preserve the approved factual answer (task #29)

New bounded factual goals use `factual-v2`: one intent call, one answer call,
and one independent review call before retries. The exact approved answer is
published without final synthesis or another model rewrite. The local evidence,
word-limit and credential checks run again before atomic publication, and a saved
completed subtask must carry an approved, issue-free review. Restart between
review and publication reuses that exact output/review; it does not regenerate
the text. This changes the earlier five-call description for new goals only.
Fresh call allowance is at most charged intake calls plus five, capped by the
operator. All token/cost/daily/subtask limits remain binding.

Existing saved `factual-v1` plans keep their original workflow and ceilings;
start a new goal to use v2. Task #29 retained a readable, approved draft, then
failed when its unnecessary final rewrite was rejected for language corruption.
That evidence supports removing this extra generation stage for one-step factual
answers; it does not establish general configured-model language reliability.
Other staff workflows keep final synthesis and independent review.

### Compact staff audit generation (task #31)

Task #31 reached staff execution, then its first skill output hit the unchanged
8,192-token provider limit twice (`finish_reason=length`), once with no returned
content and once with 19,153 characters. It stopped on structured-output
validation exhaustion, not the task call ceiling. These diagnostics do not prove
why the configured provider generated so much output or omitted the first body.

New `StaffOutput` model calls use `CompactStaffOutput`: the same decision fields
and enums, shorter prose fields, at most four evidence refs per finding and a
7,000-character total JSON validation bound. Prompts target 5,000 characters and
three findings unless the objective explicitly asks for four to six. Missing
proof, alternatives, risks, confidence and decision-required flags remain part
of the contract. Old persisted output remains readable through `StaffOutput`;
there is no migration or rewriting of historical evidence. Factual-v2 is unchanged.

The model, provider output-token limit, task/subtask budgets, retry reservations
and independent review gates are unchanged. Truncated responses remain rejected;
there is no JSON salvage or approval fallback. Structured-output exhaustion now
shows its redacted reason in progress notifications. Gateway regression tests
cover a truncated first response followed by a valid compact audit and two
consecutive truncations that must fail without publishing a result.

Deploy the reviewed SHA and rerun the audit as a new goal. Compact contracts do
not establish natural-language quality, real-provider reliability, or general
v0.3 live acceptance; the plan and reviewer may still need quality improvements.
