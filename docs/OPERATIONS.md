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

Set a separate `API_TOKEN` and, when using HTTP approvals, set `API_OPERATOR_USER_ID` to the numeric operator identity recorded in the audit trail. Empty token means endpoints fail closed. The decision answer body accepts only `answer`; callers cannot spoof `user_id`. Use `Authorization: Bearer ...` for `/tasks`, `/tasks/{id}`, `/tasks/{id}/events` and `/tasks/{id}/artifacts`. POST `/tasks` with `requirement`, `project` and optional `idempotency_key`. Reuse a key only for the same request. POST `/tasks/{id}/{cancel|retry|answer|approve|feedback}` with a JSON `message`. The token grants operator-wide access: do not distribute it to customers.

## Optional Coolify release integration

Create the target application in Coolify once and configure secrets/domains/persistent volumes there. Register its UUID as `coolify_uuid` in the project policy, set `COOLIFY_URL` and a scoped `COOLIFY_TOKEN`, and disable that target's auto-deploy. This release does not automatically provision resources or set production secrets.

After reviewing and merging a generated PR, send `/deploy <task-id> <full-merged-commit-sha>`. The engine checks identity/CI/tree integrity and pins the application to that commit. Subsequent deployments require new explicit approved task releases because the pin remains set. `/deployment <id>` fetches the remote status and verifies the reported commit when supplied.

For a test-only or otherwise non-deployable project, send `/publish <task-id> <full-merged-commit-sha>` instead. The engine performs the same PR, branch, tree and check validation, records the merged commit in the release ledger, transitions the task to `completed`, and does not call Coolify. `/deploy` remains a backward-compatible alias for this acknowledgement when the project policy has `require_deployment: false`.

If the PR was merged but the base branch advanced before release, do not reuse the stale SHA. Send `/supersede <task-id> <reason>`. The engine verifies that the reviewed PR was merged unchanged, that the current base still contains that merge, records an auditable `superseded` terminal state without publishing or deploying, and releases the repository reservation for a fresh bounded review.

If status is `unknown`, inspect Coolify's deployment history before doing anything else; the record intentionally blocks automatic duplicate submission. If a deployment failed, diagnose logs and prepare a corrected new task. Do not treat an HTTP response or queued UUID as successful deployment.

## Backup, retention and rollback

Back up Postgres daily and before upgrades using Coolify's database backup facilities or `pg_dump` executed with credentials supplied securely by the operator. Also back up the workspace volume if you need to recover unpushed changes. Store backups outside the VPS and periodically restore into a disposable instance. Task artifacts are stored in PostgreSQL; text traces can grow with usage, so monitor disk space and add an operator-approved retention policy. This release does not delete old evidence/workspaces automatically.

To roll back the factory release, stop task intake, let/cancel active work, retain a backup, and redeploy the previous image/commit against the existing volume. Added DB columns/tables are compatible with V0.1 reads; V0.1 does not understand new statuses, ownership or recovery. Avoid downgrading while V0.2 tasks are active. A backup restore is a separate deliberate data decision; never delete volumes as a troubleshooting shortcut.

For a target application rollback, use a previously validated immutable image/commit in Coolify and assess migration/data compatibility first. The factory never automatically restores a database or rolls back a stateful release.
