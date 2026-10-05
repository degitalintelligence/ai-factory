# Code Review — 2026-10-04

**Scope:** full read-only review of the LioBot / AI Factory control plane, written against
commit `b12dcfdf074f2f07e7cc2d1ff149c70252f705ef` ("Merge pull request #3 from
degitalintelligence/fix/lio-main-alignment").

**Re-verified against `956ada5` ("Merge pull request #4 from
degitalintelligence/fix/self-task-control-loop").** Ten commits landed on `main` between the
review commit and this document's merge. Every blocker and high finding was re-checked
against the new tip:

| File | Changed in `b12dcfd..956ada5` | Findings affected |
|---|---|---|
| `app/store.py` | no | B1, B2, B3, B4, H1, H2, H3 — all unchanged |
| `app/db.py` | no | B2, H3, H8, M7 — all unchanged |
| `app/security.py` | no | H6 — unchanged |
| `app/gates.py` | no | B5, H9 — unchanged |
| `app/main.py` | no | H4 — unchanged |
| `app/telegram_control.py` | no | — |
| `app/deployment.py` | no | H8 — unchanged |
| `app/orchestrator.py` | yes (repository context only) | B5 region untouched; see below |
| `app/config.py` | yes (new budget ceilings) | H5, H7 regions untouched |
| `app/agents.py` | yes (developer loop budgets) | M4 untouched |
| `tests/test_control_guards.py` | new file | 2 tests, unrelated scope |

The new commits bound the developer loop (`max_developer_stall_steps`,
`max_developer_context_chars`, `self_task_context_chars`) and compact self-improvement
context. `tests/test_control_guards.py` covers self-task context compactness and stall
detection only. **No finding in this document is resolved, mitigated, or obsoleted by
`956ada5`.** Findings citing `app/config.py:20` and `:24`, and `app/store.py` / `app/gates.py`
line numbers, were confirmed to still hold at the new tip.

**Reviewer stance:** documentation only. No source file was created, modified, or deleted.
`LIOBOT_CORE_V0_3_REQUIREMENTS.md` was present as an untracked file and was read as
context, not as part of any reviewed commit; it is intentionally excluded from the commit
that carries this document.

---

## 0. Remediation status — 2026-10-04

Python 3.12.10 was installed, `.venv` created from `requirements-dev.txt`, and the suite was
executed. **All five blockers, H1, H3 through H9, and M1 through M7 are fixed and committed.
Every behavioural fix carries regression tests; H9 is a wording-only change with no behaviour
to test.** The findings below are retained unchanged as the original
record; this section is the current state.

| ID | Status | Commit | Regression coverage |
|---|---|---|---|
| B1 | FIXED | `3aeeb66` | `test_intake_rolls_back_when_the_brief_cannot_be_stored`; claim guard rejects `branch IS NULL` |
| B2 | FIXED | `3aeeb66` | `test_queued_task_already_reserves_its_repository` plus same-repo / other-repo / blocking-state matrix |
| B3 | FIXED | `7de1edb` | `test_a_non_approval_outcome_releases_the_gated_task_and_its_repository` (reject/ask/defer) |
| B4 | FIXED | `7de1edb` | `test_a_generic_decision_never_resumes_an_approval_task` |
| B5 | FIXED | `bdc2ea3` | `test_post_publication_failure_is_not_reported_as_a_pass`, `test_post_publication_failure_reconciles_the_same_pr_on_retry` |
| H1 | FIXED | `7de1edb` | `test_decision_and_task_transition_roll_back_together`, `test_repeated_identical_answer_does_not_duplicate_audit_events` |
| H2 | PARTIAL — see below | `3aeeb66` | `test_idempotency_key_binds_the_full_request` |
| H3 | FIXED — Dedi's decision recorded below | this branch | 7 tenant/owner tests in `tests/test_memory.py` |
| H4 | FIXED | this branch | `test_api_refuses_a_valid_token_with_no_principal`, `test_startup_refuses_an_api_token_without_a_principal` |
| H5 | FIXED | this branch | `test_project_repo_cannot_traverse_out_of_the_registry` (7 parametrized cases) |
| H6 | FIXED | this branch | `test_common_credential_shapes_are_redacted` (6 shapes), `test_ordinary_prose_is_not_mangled_by_redaction`, `test_credentials_in_provenance_fields_are_refused` |
| H7 | FIXED | this branch | `test_startup_refuses_invalid_role_clearance` |
| M1 | FIXED | `cefd723` | `tests/test_worker.py`: heartbeat renews a long lease, cancellation interrupts the running job, wall-clock timeout fails the task and releases the lease, notify forwarding (with and without a chat), loop executes queued work then stops, a failing cycle is recorded in `last_error`, stopping mid-run leaves a recoverable checkpoint |
| M2 | FIXED | `cefd723` | Partial unique index `ux_decision_open_approval`; `test_a_repeated_approval_request_returns_the_same_open_card`, `test_the_database_refuses_a_second_open_approval_for_one_task`, `test_a_resolved_card_frees_the_task_for_one_new_approval`, `test_a_lost_insert_race_returns_the_winners_card`; `test_postgres_concurrent_approval_requests_produce_one_card` (CI, PostgreSQL) |
| M3 | FIXED | `cefd723` | `test_cancel_is_idempotent_across_repeated_channel_requests` now asserts exactly one `cancel` event after a repeated cancel |
| M4 | FIXED | `cefd723` | `tests/test_model_runs.py`: per-attempt role/alias/resolved model/prompt version/prompt hash/outcome/tokens/cost; provider errors recorded without response bodies; unreported cost marked `cost_incomplete`; calls outside a task context persist nothing |
| M5 | FIXED | `cefd723` | `tests/test_context_assembly.py`: memory reaches the lead prompt with id/version/source/evidence/confidence and a trust-boundary header, tenant and owner isolation, character budget truncation, baseline `memory_slice_sha256`/`context_sha256` |
| M6 | FIXED | `cefd723` | `tests/test_budget.py`: plan budget can only lower the operator ceiling, warnings at 60/80/95 fire exactly once, degradation plan names the remedy, `budget_status()` reports the binding envelope, retry retains lifetime usage, token reserve alone can exhaust |
| M7 | FIXED | `cefd723` | `tests/test_migration_ledger.py`: ordered ledger with per-step SQL checksums, a second startup neither re-applies nor duplicates, checksum drift is reported and left untouched, a v0.1 database gains the ledger without losing rows |
| H8 | FIXED | this branch | `test_deploy_transitions_task_to_deployment_pending_then_deploying`, `test_ambiguous_submission_leaves_task_deployment_unknown`, `test_reconciliation_maps_coolify_outcomes_to_task_states`, `test_deployed_task_is_terminal_against_later_reconciliation`, `test_unprovable_commits_stay_deployment_unknown`, `test_cancel_rejects_deployment_states` (5 states), `test_in_flight_deployment_reserves_the_repository` |
| H9 | FIXED | this branch | Wording-only (see note below); full suite re-run green |

Verification at the tip of this branch:

```text
ruff check app sandbox tests scripts          -> All checks passed
ruff format --check app sandbox tests scripts -> 45 files already formatted
python -m pytest -q                           -> 317 passed, 3 skipped
```

The 3 skips are deliberate, not failures: the two PostgreSQL integration tests
(`TEST_POSTGRES_URL` only, run in CI) and the symlink-escape test on Windows hosts without
`SeCreateSymbolicLinkPrivilege`, which still runs in the Linux CI sandbox job.

The same remediation branch also fixed a deployment incident on 2026-10-04: Coolify/Compose
forward `API_OPERATOR_USER_ID` as an empty string, which pydantic refused to parse and
crashed `Settings()` import in the sandbox healthcheck (`container ... is unhealthy`).
Commit `64248a9` treats a blank principal as unset; fail-closed semantics are unchanged
(`validate_runtime()` still refuses `API_TOKEN` without a principal). Merged to `main` as
`f4dc777`.

**H3 follows Dedi's explicit decision: `owner` becomes a real filter and a `tenant` column is
added.** `tenant` is the hard boundary and is applied to every read, write, correction,
retraction, and history lookup. `owner` narrows further inside one tenant: a row owned by a
person is visible only to that owner, an unowned row is shared within the tenant, and a read
with no principal returns only unowned shared memory. Role clearance cannot reach across
the owner boundary — it is applied after the owner filter, so a permissive role still cannot
read another person's memory. The partial unique index is now
`ON memory_items (tenant, key, scope) WHERE state = 'active'`, so two tenants may hold the
same active key while `owner` deliberately stays outside the index: one tenant still has one
active value per `(key, scope)`, and a differing value must be corrected explicitly.

The additive migration adds `tenant` to an existing `memory_items` table with
`DEFAULT 'default'`, so historical rows become tenant `default` instead of being dropped or
left null. `init_db()` is idempotent and the upgrade test runs it twice.

**H4 fails closed at both layers.** `Settings.validate_runtime()` now refuses to start when
`API_TOKEN` is set without `API_OPERATOR_USER_ID`, and `authorize()` independently returns
401 for that case so the request cannot be authorized even if startup validation is bypassed.
The principal continues to come from the server-side credential, never from the request body.

**H6 broadens redaction and extends it to provenance.** Added GitLab, Slack, AWS access key
ID and temporary key, Google API key, DigitalOcean, JWT, generic
`api_key/secret/token/password/access_key` assignment, `Authorization: Bearer|Basic|Token`,
and PEM public key patterns. The generic assignment pattern requires a value of at least eight
non-space characters so ordinary prose is not mangled, which is asserted by
`test_ordinary_prose_is_not_mangled_by_redaction`. `remember()` and `correct_memory()` now
reject credentials in `value`, `source`, and `evidence_ref`, not only in the value.

**H2 is intentionally partial.** The idempotency comparison now binds `requirement`,
`project`, `chat_id`, `user_id`, `kind`, and the serialized self-improvement brief. It
deliberately does **not** compare `policy_json`: the policy snapshot is server-side state
that may legitimately change between two identical retries, and a stale task is stopped by
the existing policy-revocation check rather than by refusing the retry. This is a deliberate
narrowing of finding H2 and is recorded here rather than silently dropped.

**H8 wires the deployment lifecycle onto the task.** `TaskStatus` now defines
`deployment_pending`, `deploying`, `deployed`, `deployment_unknown`, `deployment_failed`,
plus the publication outcomes `completed`, `superseded`, and `reviewed`. Because status is a
`String(32)` column, adding enum values is additive and needs no DDL. `DeploymentService`
advances the parent task in the same transaction as the deployment record:
`deployment_pending` on a validated submission, `deploying` after Coolify accepts it, and
`deployment_unknown` when the outcome is ambiguous. Reconciliation maps Coolify statuses
through an explicit table (`finished → deployed`, `failed/cancelled/error →
deployment_failed`, `unverified_commit/commit_mismatch → deployment_unknown`), and
`_advance_task()` only moves a task that is still in a reconcilable state, so terminal
`deployed`/`deployment_failed` are never reopened and `deployment_unknown` still cannot
auto-retry — only an explicit `/deployment` reconciliation moves it (`AGENTS.md` §10).
The at-most-once guarantee no longer depends on task status plus an `IntegrityError`:
`deploy()` checks the durable deployment record first and refuses a second submission with
"already requested". In-flight deployment states join the shared reserved set, so they block
a second task on the same repository, while `deployed`/`deployment_failed` release the
reservation. `cancel()` refuses deployment states with direction to `/deployment` and
refuses to undo a terminal deployment.

**H9 aligns the wording with the evidence.** The `deployment_issues` gate docstring now
states it is a static structure-and-policy gate that proves nothing about a real image
build, boot, or health check, and the published PR body says the same: deployment files are
only statically checked, nothing was built or booted, and a live deployment is a separate
explicit operator action reconciled with `/deployment`. `README.md` and the operator
contract messages already described this honestly and were left unchanged.

**Not verified locally.** Docker is unavailable on the review machine, so the real sandbox
smoke test, AppArmor profile checks, namespace probes, Compose validation, and both image
builds were not executed. The PostgreSQL concurrency proof — including the advisory-lock
serialization that B2 depends on — also only runs in CI. Per `AGENTS.md` §18, these remain
required CI gates before this work is treated as production-verified.

---

## 1. Verification status — read this first

> **Superseded by section 0.** This section records the state of the original review pass,
> when no Python interpreter was available. Ruff and pytest have since been run; see
> section 0 for current results. The reasoning below is kept as the original record.

The following required checks from `AGENTS.md` §17–18 were **not executed**:

| Check | Status | Reason |
|---|---|---|
| `ruff check app sandbox tests scripts` | NOT RUN | No Python interpreter on the review machine |
| `ruff format --check app sandbox tests scripts` | NOT RUN | Same |
| `python -m pytest -q` | NOT RUN | Same |
| Real Docker sandbox smoke test | NOT RUN | No Docker daemon available |

`python` resolved only to the Microsoft Store alias stub
(`C:\Users\Dedi\AppData\Local\Microsoft\WindowsApps\python.exe`), and `docker` was not
present on `PATH`. No `.venv` exists in the repository.

**Consequence:** every finding below is derived from static reading, not from execution.
A model response is not evidence (`AGENTS.md` §1). Before acting on this document, the
suite must be run on a machine with Python 3.12 and the pinned dev requirements. Findings
marked *unverified by execution* may in principle be already covered by a test that was
read incorrectly; findings marked *verified by reading* were confirmed against the exact
line numbers cited.

---

## 2. Overall assessment

The V0.2 foundation is genuinely strong. The following are real, load-bearing
implementations and should not be disturbed casually:

- **Lease fencing with CAS** — `store.update()` refuses writes from an owner without a
  valid lease (`app/store.py:161-172`), and `store.check()` re-verifies before every step.
- **PostgreSQL queue serialization** — advisory transaction lock plus
  `SELECT ... FOR UPDATE SKIP LOCKED` (`app/store.py:190-198`).
- **Publication checkpointing** — the approved commit and review digest are made durable
  *before* any push or PR request (`app/orchestrator.py:198-202` at `956ada5`; `169-173` at
  `b12dcfd`), so a lost response is
  reconciled rather than duplicated.
- **Sandbox boundary** — Bubblewrap with user/IPC/PID/net/UTS/cgroup namespaces,
  `--cap-drop ALL`, `--clearenv`, and a `/health` that performs a real namespace probe.
- **Deterministic gate implementations** — `app/gates.py` correctly computes diff,
  test, evidence, and deployment problems.

The problem is not in the components. It is in the **seams between components**: between
two commits, between two transactions, between a gate's verdict and its effect, between a
database column and the query that should filter on it, and between a configuration
default and the code path that consumes it.

Findings B1–B5 are blockers: each one either loses durable state silently, permits a
concurrency the architecture explicitly forbids, or lets a mandatory gate be bypassed.

---

## 3. Blockers

### B1. `Store.create()` is not atomic and can strand a task permanently

*Verified by reading: `app/store.py:98-128`.*

`create()` performs **two separate commits**. The first (`s.add(task)` → `await
s.commit()`, line 111) persists the task row. Only afterwards does it set `task.branch`
(line 121), add the `received` event (line 122), add the self-improvement brief (lines
123-126), and commit again (line 127).

A crash, timeout, or unhandled exception between lines 111 and 127 leaves a persisted task
in status `received` with `branch = None`, no `received` event, and — for self-improvement
— no durable brief. This contradicts `AGENTS.md` §2.5 ("Task intake, idempotency,
ownership, leases, recovery").

The stranded state is unrecoverable by design: `claim()` filters only on
`Task.status == "received"` (line 222) and never checks that `branch` is populated. Such a
task will be claimed and then fail mid-pipeline with no repair path.

**Fix:** use `await s.flush()` to obtain the generated id, then a single `commit()`.
Add a `Task.branch.isnot(None)` predicate to the `claim()` selection as defence in depth.

### B2. Repository reservation can be bypassed — and a test enshrines the bypass

*Verified by reading: `app/db.py:37-40`, `app/store.py:85-97`, `app/store.py:219-226`,
`tests/test_store.py:24-32`.*

```python
ACTIVE = {"planning", "developing", "testing", "reviewing", "publishing"}
REPOSITORY_BLOCKING = ACTIVE | {"waiting_input", "awaiting_approval", "pr_created"}
```

`received` is intentionally in neither set, which creates two distinct holes:

- **Creation side.** `create()` rejects a new task only when another task is in
  `REPOSITORY_BLOCKING` (lines 85-93). Any number of tasks can therefore sit in `received`
  for the same repository.
- **Claim side.** Line 219 computes `busy` as `Task.lease_until > now, Task.status.in_(ACTIVE)`.
  An unleased `pr_created` task is **not busy**: it belongs to `REPOSITORY_BLOCKING` (so it
  blocks new creation) but not to `ACTIVE` (so it does not block claiming).

Result: tasks A and B are both created in `received`; A is claimed, completes, and reaches
`pr_created`; A releases its lease; **B is then claimed while A's pull request is still
open.** This is exactly the concurrency that `AGENTS.md` §5.2 forbids ("One active task per
repository is the default safety policy").

The sharpest part: `tests/test_store.py:24-32` is named
`test_serializes_same_repo_but_allows_other_repos`, but its final assertion codifies the
unsafe behaviour as expected:

```python
await db.update(a.id, "worker-a", status="pr_created", lease_owner=None, lease_until=None)
assert (await db.claim("worker-c")).id == b.id   # B claimed while A's PR is open
```

A future reader scanning test names would conclude repository serialization is enforced.

**Fix:** include `pr_created` (and any other reservable state) in the `busy` set used by
`claim()`, or introduce a dedicated `REPOSITORY_RESERVED` set used by both `create()` and
`claim()`. Rewrite `tests/test_store.py:24` so it asserts `claim()` returns `None` in this
scenario, and add a creation-side twin.

### B3. Rejected decisions never release the task or the repository

*Verified by reading: `app/store.py:504-509`.*

```python
if task_id and target == DecisionState.APPROVED:
    task = await self.get(task_id)
    if task and task.status == "awaiting_approval" and task.plan_json:
        await self.resume(task_id, "approve", plan_hash(task.plan_json)[:12])
```

Only `APPROVED` has any effect on the linked task. `REJECTED`, `NEEDS_INFO`, and `DEFERRED`
change the decision row and nothing else. A rejected plan therefore leaves the task parked
in `awaiting_approval` indefinitely, and because `awaiting_approval` is in
`REPOSITORY_BLOCKING`, **the repository stays locked** — no new task for that repo can be
intake. Recovery requires a manual `/cancel`, which is not discoverable from the decision
card.

This directly contradicts `AGENTS.md` §10, which requires blocking questions to enter
`waiting_input` and terminal outcomes to settle.

### B4. Decision `kind` is never validated before auto-resuming a task

*Verified by reading: `app/store.py:504-509`; card creation at `app/store.py:361-385`.*

The resume block does not check `decision.kind == DecisionMessageType.APPROVAL_REQUIRED`.
Any approved decision of **any** kind that carries a `task_id` will resume an
`awaiting_approval` task. `ensure_task_approval_decision` sets the kind correctly at
creation time, but the consuming side ignores it.

The plan-hash guard still applies, so this is not a full bypass — but the approval path is
specified to be bound to an exact plan hash via an `APPROVAL_REQUIRED` card, and this route
reaches the same effect while skipping that classification.

**Fix:** gate the resume on `decision.kind == DecisionMessageType.APPROVAL_REQUIRED` and
reject a task-linked decision of any other kind at creation.

### B5. The post-publication gate does not gate anything

*Verified by reading: `app/orchestrator.py:183-197` at `956ada5` (`153-167` at `b12dcfd`; the
code is byte-identical and was only shifted by the repository-context changes).*

```python
published = post_publication_issues(...)
await store.artifact(task_id, "post_publication", json.dumps({"pr_url": url, "issues": published}))
note = (...)
await transition("pr_created", f"Passed gates. PR: {url}\n{summary}{note}", pr_url=url)
```

Issues are computed, persisted as an artifact, and rendered into an operator note — then the
task transitions to `pr_created` regardless, and the message is prefixed **"Passed gates."**
even when `published` is non-empty.

`AGENTS.md` §13 requires that all gate conditions hold *before* PR publication and states
that deterministic gates cannot be overridden. Here the strongest verification in the
system is advisory only.

Note the asymmetry in test coverage: `tests/test_gates.py` proves
`post_publication_issues()` correctly detects tampered digests, wrong commits, unreadable
PRs, and missing body sections — but `tests/test_engine.py` never drives the engine into a
state where the function returns an issue. The gate *function* is well tested; its *effect*
is untested and, as written, absent.

**Fix:** do not transition to `pr_created` when issues exist; move the task to an explicit
verification-failure state that preserves the issue text. Do not emit "Passed gates." when
verification failed.

---

## 4. High findings

### H1. Decision state and task resume are not crash-consistent

*Verified by reading: `app/store.py:470-509`.*

The decision state is committed at line 501; the `async with self.sessions()` block exits at
line 503. The task resume at lines 506-509 then runs as a **separate operation with its own
session**. A crash between them leaves a durably `approved` decision attached to a task
still in `awaiting_approval`. There is no reconciliation job, outbox, or compensating
transaction.

Worth noting: the system is *one retry* away from self-healing. A repeated identical answer
hits lines 475-478 (`decision.state != OPEN` and `== target`), which sets
`task_id = decision.task_id` and falls through to the same resume block. But nothing
performs that retry or verifies it happened, so the inconsistent state can persist
indefinitely and is invisible to the operator.

### H2. Idempotency key comparison covers only three fields

*Verified by reading: `app/store.py:77-82` and the recovery path at `app/store.py:117`.*

Both comparison sites check only `requirement`, `project`, and `user_id`. Not compared:
`kind`, `brief`, `chat_id`, and the resolved policy (`policy.repo`, `policy.base_branch`,
`install_dependencies`, `require_deployment`, `coolify_uuid`).

Consequence: reusing a Telegram `idempotency_key` of `telegram:<update_id>` with a
**different `kind` or a different `brief`** silently returns the original task instead of
raising "Idempotency key already belongs to a different request". For self-improvement
this conflates "the same request" with "a different change proposal that happened to reuse
a key".

**Fix:** compare the full canonical request payload, including kind, brief, chat_id, and
the policy snapshot.

### H3. Memory has no tenant or organization isolation

*Verified by reading: `app/store.py:582-599` (`recall`), `app/store.py:698-707`
(`memory_history`), `app/db.py:155` (owner column), `app/db.py:200-211` (unique index).*

`recall()` has signature `(keys=None, role=None, scope=None, limit=25)` — there is **no
user, owner, tenant, or organization parameter at all**. Filtering is by free-text `scope`,
role clearance, and sensitivity only. The `owner` column exists (`db.py:155`) but is never
used as a recall filter.

`memory_history(key)` filters on `MemoryItem.key == key` **and nothing else** — no scope, no
owner. A correction history for `deploy.target` therefore returns every version written by
every owner in every scope, interleaved and ordered only by `version DESC`.

Additional design tension: the partial unique index is
`ON memory_items (key, scope) WHERE state = 'active'` (`db.py:208-209`) — **`owner` is not
part of the index**. Two different users cannot both hold an active `deploy.target` in the
same scope; the second write collides. Whether that is intended is not documented and not
tested.

This is a prerequisite for the v0.3 requirement of scoped organizational memory and
tenant/user isolation.

### H4. Approval can be recorded with no principal

*Verified by reading: `app/config.py:47`, `app/main.py:63-68`, `app/store.py:490`,
`docker-compose.yaml`, `.env.example`.*

```python
api_operator_user_id: int | None = Field(default=None, ge=1)

def authorize(authorization: str = Header(default="")):
    ...
    return settings.api_operator_user_id
```

With a valid `API_TOKEN` and no `API_OPERATOR_USER_ID`, an approval resolves with
`user_id=None`, and `decision.decided_by` is set to `NULL`. This defeats the audit
attribution required by `AGENTS.md` §9.3 and §12. `docs/OPERATIONS.md` §"HTTP operator
interface" advises setting the variable "when using HTTP approvals" — but nullable is the
default, so the unsafe path is the default path.

Two aggravating factors:
- `docker-compose.yaml` does not pass `API_OPERATOR_USER_ID` to the `ai-factory` service.
- `.env.example` does not mention `ROLE_CLEARANCE_JSON` at all.

Telegram is unaffected — `telegram_control.py` supplies a concrete
`update.effective_user.id`.

`tests/test_api_telegram.py:181-195` exercises this path via
`monkeypatch.setattr(settings, "api_operator_user_id", 42)`, proving the wiring works *when
a principal is supplied* — not that one is required.

**Fix:** make the principal mandatory whenever an API token is configured; fail closed at
`validate_runtime()`.

### H5. Repository regex permits `.` and `..`

*Verified by reading: `app/config.py:20` versus `app/config.py:24`.*

```python
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
    raise ValueError("repo must be owner/name")
...
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", self.base_branch) or ".." in self.base_branch:
    raise ValueError("invalid base branch")
```

Both character classes admit `.`, so `../repo`, `owner/..`, and `./repo` all validate. Note
the contrast one line below: `base_branch` **does** explicitly reject `..` — the author was
aware of this class of bug and guarded one field but not the other. `policy.repo` flows into
`Task.repo` and from there into GitHub API calls and clone operations.

### H6. Redaction is narrower than the data it protects

*Verified by reading: `app/security.py:6-13`, `app/store.py:514-523`,
`app/store.py:601-620`.*

`SECRET_PATTERNS` contains exactly five entries: PEM private keys, GitHub tokens, `sk-…`,
Telegram bot tokens, and `scheme://user:pass@host`. There is **no** pattern for a generic
`Authorization: Bearer …` header, AWS `AKIA…`, GCP JSON keys, Azure tokens, Slack
`xoxb-…`, or Stripe `sk_live_…`. `AGENTS.md` §12 requires redacting "recognizable
credential patterns"; no test asserts behaviour for any family outside the five.

More concretely, `remember()` validates only the value:

```python
if redact(item.value) != item.value:
    raise ValueError("Remove credentials before storing this as memory")
```

`item.source` and `item.evidence_ref` are stored unvalidated (they are `String(300)` at
`db.py:153-154`), and `_view()` returns both verbatim to every caller. A token pasted into
a source citation is stored and served.

The existing test `tests/test_memory.py:166-172` places its secrets in `value` only; the
`source="leak"` in its second case is a benign string. The test name therefore asserts a
property the code does not have.

### H7. `role_clearance_json` is never validated at startup

*Verified by reading: `app/config.py:110-128`.*

`validate_runtime()` calls `self.projects()` (line 111) and validates Telegram and worker
settings, but **never** `self.role_clearance()`. Malformed `ROLE_CLEARANCE_JSON` passes
startup and fails lazily at the first `recall()` (`app/store.py:588`). This weakens the
fail-closed posture that `AGENTS.md` §5.4 requires for security-relevant configuration.

### H8. Task lifecycle has no deployment states

*Verified by reading: `app/db.py:22-32`, `app/deployment.py`.*

`TaskStatus` defines `received`, `planning`, `waiting_input`, `awaiting_approval`,
`developing`, `testing`, `reviewing`, `publishing`, `pr_created`, `failed`, `cancelled`.

It does **not** define `completed`, `deployment_pending`, `deploying`, `deployed`,
`deployment_failed`, or `deployment_unknown` — all of which are required by the lifecycle in
`AGENTS.md` §10 and by `LIOBOT_CORE_V0_3_REQUIREMENTS.md`. Meanwhile `app/deployment.py`
happily persists those states in the `deployments` table.

Consequence: a deployment never changes the parent task's status. A task that reached
`pr_created` and was successfully deployed still reads as `pr_created` forever. There is no
task-level representation of deployment outcome, and no health-check acknowledgement tied to
the task lifecycle.

### H9. Sandbox and deployment static checks are weaker than their wording

*Verified by reading: `app/gates.py`, `sandbox/server.py`, `README.md`.*

These are largely honest, but two phrasings overstate what is checked:

- The persistence gate requires a named volume to be declared in Compose. It does not
  verify that the volume actually stores the intended application data.
- Host-mount checks do not model the real deployment threat model (a container that can
  read a host path it should not).

`README.md` and `AGENTS.md` already state that real deployment acceptance requires a
smoke test. The gap is that static checks are the only automated signal, so "deployment
files present" can read as "deployment verified" to an operator scanning a decision card.

---

## 5. Medium findings

### M1. `app/worker.py` has no test coverage

No test exercises `WorkerPool.supervise()` or its loop: cancellation polling, wall-clock
timeout enforcement, and lease heartbeats are unverified. `docs/VERIFICATION.md` lists
recovery and timeout evidence as expected, so this is a documented gap.

### M2. Duplicate approval cards are possible

`ensure_task_approval_decision` (`app/store.py:361-385`) performs a select-then-insert with
no uniqueness constraint on `(task_id, kind, open state)`. Concurrent workers can create
duplicate open approval cards for one task.

### M3. Cancellation is state-idempotent but not side-effect-idempotent

`Store.cancel()` appends a new cancellation event on every call. `tests/test_contracts.py:30-35`
calls cancel twice and asserts both succeed with final status `cancelled`, but never counts
events. The test name claims idempotency that the side effects do not honour.

### M4. The model is the single point of failure for correctness evidence

Reviewer evidence is free-form text per acceptance criterion (`app/schemas.py`,
`app/agents.py`). There is no artifact ID or deterministic source reference attached to each
criterion, so "exactly one evidence entry per criterion" is checked structurally but not
semantically. `app/llm.py` records call/token/cost per task, but there is no immutable
`model_run` record per call — no prompt version, latency, or per-call cost trail.

### M5. `context_slice()` is defined but never used in execution

The context helper exists in `app/contracts.py` and is tested, but it is not invoked from
`app/orchestrator.py`, `app/agents.py`, or any LLM prompt assembly. The memory plane is
therefore not an active input to task execution; it is a standalone store.

### M6. Budget enforcement has only a hard stop

`app/llm.py` enforces call count, aggregate tokens, reported cost, and wall clock. There is
no warning or notification at 60%, 80%, or 95% utilization, no per-subtask or global
budget envelope, and no automatic degradation or recovery plan when the budget is
approaching exhaustion — all of which `LIOBOT_CORE_V0_3_REQUIREMENTS.md` asks for.

### M7. Startup migration is additive but is not a migration framework

`init_db()` (`app/db.py`, around lines 190-217) adds missing columns and creates new tables
idempotently, which satisfies `AGENTS.md` §5.2 for additive startup changes. It has no
version table, no ordered upgrade scripts, and no downgrade path. Historical V0.1 rows are
preserved as required. This is acceptable for V0.2 but will need a real migration tool
before the v0.3 schema expansion.

---

## 6. Gap against `LIOBOT_CORE_V0_3_REQUIREMENTS.md`

The untracked v0.3 requirements move the product from "coding task runner" to "Chief of
Staff / orchestration engine". The current implementation satisfies the v0.2 foundation but
does not implement the v0.3 layer at all.

Entirely absent from `app/db.py`:

- `users`, `organizations`, `tenants`, `objectives`, `intents`
- `plans` as a first-class entity (plan is currently a JSON blob on the task)
- `agents`, `skills`, `skill_versions`
- `evidence`, `approvals` as tables (evidence is an artifact string; approval is a task
  column plus a decision row)
- `tool_runs`, `model_runs`
- `budgets`
- `improvement_proposals` (self-improvement is a task *kind*, not a proposal lifecycle)
- append-only `audit_log`

Absent from the API surface (`app/main.py` uses v0.2 paths):

| Required endpoint | Status |
|---|---|
| `POST /v1/intents` | missing |
| `GET /v1/intents/{id}` | missing |
| `POST /v1/intents/{id}/clarification` | missing |
| `POST /v1/plans/{id}/approve` | partial — `/tasks/{id}/approve` exists |
| `GET /v1/decisions` | present in v0.2 shape |
| `POST /v1/decisions/{id}/action` | partial — `/decisions/{id}` exists |
| `GET /v1/tasks/{id}` | present in v0.2 shape |
| `GET /v1/memory/search` | missing |
| `POST /v1/improvements` | missing |
| `GET /v1/health` | present as `/health` |
| `GET /ready` | present |

Also missing: an agent/skill registry (roles are hard-coded in `app/agents.py`); a
`DECISION_REQUIRED` type for clearance changes that reaches config (currently
`propose_clearance()` creates a decision, but no mechanism carries an approved decision into
a deployed configuration change); and a decision note/reason field on the decision answer
contract.

**Terminology conflict requiring your decision:** the amendment V1.1 specifies
`stealth/space-bunny-alpha` for Lead, Developer, and Reviewer. The v0.3 requirements
document refers to `bunny-alpha`. `AGENTS.md` §6.1 and §19 currently record the former as
resolved. If v0.3 is authoritative, the model naming needs an explicit amendment rather
than a silent rename.

---

## 7. Tests that assert less than their names claim

*Verified by reading.* These are documented as coverage-debt items, not as regressions.

| Test | What the name promises | What it asserts |
|---|---|---|
| `tests/test_store.py:24` | same-repo tasks are serialized | Asserts a same-repo sibling **is** claimed while the first task's PR is open (see B2) |
| `test_post_publication_gate_passes_for_complete_evidence` | complete evidence passes the gate | Requires exactly **one issue string** in the result |
| `test_model_cannot_override_failed_or_missing_tests` | failed **and missing** tests both block | Parametrized only over exit codes `[1, 2, 5, 124]`; never constructs an empty/absent result list |
| `test_every_criterion_requires_unique_evidence` | evidence must be unique per criterion | Bare `assert quality_issues(...)` with no message check — any non-empty list passes |
| `test_secrets_never_enter_memory` | secrets cannot enter memory | Both cases place the secret in `value`; `source` and `evidence_ref` are never exercised |
| `test_api_decision_records_the_answering_operator` | the operator is recorded | Monkeypatches `api_operator_user_id = 42`; the `None` default is never exercised (see H4) |
| `test_postgres_concurrent_claim_recovery_and_unique_keys` | unique keys are exercised | All tasks are created without an `idempotency_key`; no unique constraint is ever tested |
| `test_sensitive_self_improvement_reports_that_approval_is_required` | approval is required | Asserts `needs_approval=True` only; never proves development is blocked afterwards |
| `test_cancel_is_idempotent_across_repeated_channel_requests` | cancel is idempotent | Asserts final status only; a new event is appended on each call (see M3) |

---

## 8. Recommended remediation order

> **Status 2026-10-04:** steps 1–5 (all blockers plus H2's deliberate `policy_json`
> exclusion), the hardening batch (H3–H7), and the M1–M7 medium batch (`cefd723`, hotfix
> `64248a9`) are complete and committed. H8 and H9 are also fixed on this branch: the task
> now carries the full deployment lifecycle, and deployment wording matches the static
> evidence. The remaining open items are the open decisions in section 9.

Each step is independently reviewable and reversible. Steps 1–5 are the blockers.

1. **Make `create()` single-transaction.** Use `flush()` for the id, one `commit()`. Add
   `branch IS NOT NULL` to the `claim()` predicate. *(B1)*
2. **Close the repository reservation hole.** Introduce one shared reserved-state set used
   by both `create()` and `claim()`; rewrite `tests/test_store.py:24` to assert the safe
   outcome and add a creation-side twin. *(B2)*
3. **Make decision resolution atomic and kind-checked.** Move the task transition into the
   same transaction as the decision row; require
   `kind == APPROVAL_REQUIRED` before resuming; give `REJECTED`, `NEEDS_INFO`, and `DEFERRED`
   explicit task effects so the repository is always released. *(B3, B4, H1)*
4. **Make the post-publication gate fail closed.** Block the `pr_created` transition when
   issues exist and preserve the issue text; suppress the "Passed gates." prefix on
   failure. *(B5)*
5. **Complete the idempotency comparison.** Include kind, brief, chat_id, and the policy
   snapshot in both comparison sites. *(H2)*

Then, as hardening: require the API principal when a token is configured and pass it plus
`ROLE_CLEARANCE_JSON` through Compose *(H4)*; reject `.`/`..` in the repo regex *(H5)*;
validate `role_clearance()` at startup *(H7)*; extend redaction to bearer/AWS/GCP/Slack/
Stripe patterns and validate memory `source`/`evidence_ref` *(H6)*.

Recommended regression tests, in priority order — all target confirmed gaps:

| # | Test | File |
|---|---|---|
| 1 | `create()` failure between commits leaves nothing claimable | `tests/test_store.py` |
| 2 | `claim()` returns `None` while a same-repo sibling is `pr_created` | `tests/test_store.py` |
| 3 | Idempotency key reuse with a different kind/brief/chat_id raises | `tests/test_store.py` |
| 4 | Only an `APPROVAL_REQUIRED` decision resumes a task; reject/ask/defer settle it | `tests/test_decisions.py` |
| 5 | Decision + task transition are atomic, or an identical re-answer heals without a duplicate event | `tests/test_store.py` |
| 6 | `recall()` and `memory_history()` isolate by owner and scope | `tests/test_memory.py` |
| 7 | Engine does **not** reach `pr_created` when post-publication verification fails | `tests/test_engine.py` |
| 8 | Secrets in memory `source`/`evidence_ref` are rejected; extra `redact()` families | `tests/test_memory.py`, `tests/test_security_workspace.py` |
| 9 | `Project` rejects `../repo`, `owner/..`, `./repo`; startup rejects bad clearance JSON | `tests/test_security_workspace.py` |
| 10 | No `Decision` row can end with `decided_by IS NULL` | `tests/test_api_telegram.py` |

---

## 9. Open decisions for Dedi

These cannot be resolved from the code and require an explicit decision per `AGENTS.md` §3:

1. **Is v0.3 authoritative over amendment V1.1?** Specifically the model naming
   (`bunny-alpha` vs `stealth/space-bunny-alpha`) and whether the v0.3 Chief-of-Staff scope
   supersedes the V0.2 bounded-engine framing in `README.md`.
2. **Repository reservation semantics:** should a task with an open PR block a new task on
   the same repository indefinitely, or should the rule be "one task per repository per
   base branch"? B2's fix depends on this answer.
3. **Memory isolation model:** is `scope` (free text) the intended tenant boundary, or
   should `owner` become a real filter with a tenant/organization column? H3 cannot be
   fixed correctly without knowing which.
4. **`ai-factory` self-target registry alias** — still OPEN per `AGENTS.md` §19. Not
   addressable from this review.

---

## 10. Reviewer's closing note

> **Updated 2026-10-04.** The single most important next action named below — running the
> verification suite — has been done. All five blockers and H1 are fixed and committed; see
> section 0. The original note is preserved after this line.

No merge, deployment, or credential change is requested or implied by this document. No
production state was inspected. The single most important next action is not any individual
fix — it is to **run the verification suite on a Python 3.12 environment**, since this
review could not execute a single test and that gap affects the confidence of every finding
above.