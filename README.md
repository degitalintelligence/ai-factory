# AI Factory / LioBot Core

Status: v0.3 Chief-of-Staff is implemented in this repository and has an
operator-accepted staging candidate recorded on 2026-10-08. The bounded v0.2
engineering guardrails remain mandatory, and production-complete v0.3 still
depends on the remaining live gates in
[docs/V03_IMPLEMENTATION_AND_ACCEPTANCE.md](docs/V03_IMPLEMENTATION_AND_ACCEPTANCE.md).

An operating engine for building software from Telegram requirements: **repository-aware planning → implementation → isolated tests → independent review → repair → GitHub PR → explicit publication or commit-bound Coolify deployment**.

LioBot = AI Factory. This repository is the LioBot core product and its bounded
engineering engine. `telegram-lab` is a test/acceptance harness only; it is not a
second LioBot product. Quant Factory, Kedaya, and other business products remain
separate registered repositories and runtimes.

## Current status

The v0.2 engine remains the guarded execution foundation. On top of it, the
v0.3 Chief-of-Staff layer now ships natural chat intake, `POST /v1/chat`,
`/dashboard`, Decision Inbox, scoped memory, model-run audit, self-improvement
proposal flow, and the shared staff workflow described in
[docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md](docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md).

The current state is an implementation candidate with operator acceptance on a
staging deployment, not a blanket claim that every live gate is complete.
Self-improvement through controlled release/outcome measurement and broader
live channel-parity checks are still tracked work. Telegram remains a channel
adapter and `telegram-lab` remains the acceptance harness.

**v0.4 is not yet a production release.** Live objectives, channel parity,
restart/rollback, a measured self-improvement and a real product pilot need acceptance
on the same candidate SHA. See [the implementation and acceptance runbook](docs/V04_IMPLEMENTATION_AND_ACCEPTANCE.md)
and [the approved implementation plan](docs/AI_FACTORY_V0_4_PLAN.md).
Nonengineering domains provide analysis/drafts; external business actions require
registered tools and explicit policy. The model and approval boundaries remain binding.

## What works

- Persistent PostgreSQL queue, task events, plans, diffs, test reports and review evidence.
- Atomic claims, worker leases/heartbeats, bounded restart recovery, one active task per repository.
- Natural private-chat goals and `POST /v1/chat`, plus `/dashboard` for the shared Decision Inbox, task summaries and evidence drill-down.
- Multiple registered projects with explicit repository, branch, Python/Node test profile and deployment policy.
- Separate OpenRouter models for Lead, Developer and Reviewer. Structured responses, bounded retries, token/call/time budgets and provider-reported cost tracking.
- Scoped tenant/owner memory, durable decisions, improvement proposals, rollback confirmation flow, and model-run audit records.
- Developer tools: inspect, search, replace/write/delete files, review a complete diff, run sandbox checks.
- Deterministic gates: failing/absent tests, generated databases, credentials, symlink escapes, changed source after review, incomplete acceptance mapping and oversized diff cannot be approved by a model.
- Post-publication verification is fail-closed: a PR that does not match the reviewed evidence leaves the task in `failed` with the exact issues, not `pr_created`. A retry re-checks the same PR.
- High-risk plan approval bound to its hash; clarification, cancellation, retry and feedback to an existing open PR. Only an approval decision can resume a gated task; rejecting, asking, or deferring settles it and releases its repository.
- Full target deployment pack when requested: Dockerfile, Compose, environment example, persistent volumes, healthchecks, deployment/backup/rollback runbook.
- Optional deployment of an **existing registered Coolify application** after an explicit command, merged PR, matching reviewed tree, matching full commit SHA and successful existing GitHub checks.
- Private Telegram allowlist, task ownership, optional bearer-protected HTTP API, health/readiness endpoints.

## Upgrade an existing Coolify deployment

Read [docs/OPERATIONS.md](docs/OPERATIONS.md) before switching branches. Keep the existing Compose resource and volume names. The startup migration is additive; old task rows remain intact.

Keep your existing model/API/GitHub/PostgreSQL values. Add:

```dotenv
TELEGRAM_ALLOWED_USER_IDS=YOUR_NUMERIC_USER_ID
SANDBOX_TOKEN=AN_INDEPENDENT_RANDOM_SECRET
```

Generate a secret locally using `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Put it in Coolify, never in a task or Git.

On the Ubuntu Docker host, install the supplied runner-specific AppArmor profile once: `sudo bash scripts/install-sandbox-profile.sh` from this checkout. It permits the runner to create isolated namespaces without disabling the host-wide restrictions.

The new `sandbox` service must become healthy before the control service starts. Its namespace check fails closed if the host cannot provide isolation. Neither service mounts a Docker socket. Do not run generated Python directly in the credential-bearing control container.

## First useful task

```text
/new lab | Siapkan LioBot end-to-end di repository ini. Pertahankan fitur /hello dan /todo. Tambahkan Dockerfile non-root, docker-compose.yaml, .env.example, healthcheck, volume data agar todo tetap ada setelah container dibuat ulang, serta docs/DEPLOYMENT.md yang menjelaskan environment, backup dan rollback. Buat test add/list, isolasi user, persistence, dan kegagalan input. Jangan menaruh data runtime atau token di Git.
```

The bot replies with the exact project, repository and branch. Follow progress with `/status`, `/logs` and `/report`. This command creates a reviewable PR, and does not silently merge or deploy it.

## Project registry

Configure `PROJECTS_JSON` in Coolify (empty retains the default lab):

```json
{
  "lab": {
    "repo": "degitalintelligence/telegram-lab",
    "base_branch": "main",
    "profile": "python",
    "require_deployment": false,
    "install_dependencies": true
  }
}
```

The lab project is an acceptance harness and therefore acknowledges merged PRs
without deploying them. Set require_deployment: true and register coolify_uuid
only for a project that has a real Coolify application.

Dependency installation requires both project `install_dependencies: true` and server `SANDBOX_INSTALL_DEPS=true`. Python installs wheels only; Node uses a committed lockfile and `npm ci --ignore-scripts`. Tests themselves have no network. Projects requiring native build/install scripts need a prebuilt, operator-maintained sandbox image.

To add another product, create its repository with an initial commit, grant the fine-grained GitHub token access and register its alias. Do not give an LLM authority to change this registry. Project policies are snapshotted at task creation; a changed/revoked policy stops pending work.

## Commands

| Command | Purpose |
|---|---|
| `/new <requirement>` | Queue a task in `lab` |
| `/new <alias> \| <requirement>` | Queue a task in a registered project |
| `/projects`, `/tasks` | Repository registry and your recent queue |
| `/status <id>`, `/logs <id>`, `/report <id>` | State, events, and downloadable evidence |
| `/plan <id>` | Inspect the plan and its approval hash |
| `/answer <id> <answer>` | Clarify a blocked requirement; replan |
| `/approve <id> <hash>` | Approve that specific high-risk plan |
| `/cancel <id>` | Request cancellation; cannot undo an already published PR |
| `/retry <id>` | Retry failed/cancelled work; lifetime LLM usage is retained |
| `/feedback <id> <revision>` | Replan, test and review changes on the same open PR |
| `/publish <id> <40-character-merged-SHA>` | Acknowledge a merged PR for a project without a deployment target |
| `/supersede <id> <reason>` | Close a stale merged PR checkpoint so a fresh review can run |
| Review-only requirement | Explicit re-review requests run tests and independent review; a clean result ends in `reviewed` with no mutation or PR. |
| `/deploy <id> <40-character-merged-SHA>` | Explicitly deploy a registered, reviewed release |
| `/deployment <id>` | Reconcile Coolify status; success of business behavior still needs smoke testing |

## Local development and tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
ruff check app sandbox tests scripts
```

PostgreSQL integration uses `TEST_POSTGRES_URL` pointing **only to a disposable test database**; the integration test recreates its tables. CI supplies PostgreSQL 16 and a separate real Docker sandbox smoke test. Unit/integration tests stub external model/GitHub/Coolify calls and incur no API spend.

For local API development, set `WORKER_ENABLED=false`, leave the Telegram token empty and use an `API_TOKEN`. `API_OPERATOR_USER_ID` is required whenever `API_TOKEN` is set: startup fails closed without it, and requests are rejected rather than acting as nobody. Run `uvicorn app.main:app --port 8080`. Workers in a real deployment require PostgreSQL and a healthy sandbox.

Memory is isolated per tenant, and within a tenant a memory owned by a person is readable only by that person. A read with no principal returns shared unowned memory only, and `ROLE_CLEARANCE_JSON` narrows by role on top of that — it never widens across an owner boundary.

## Current limits

This is still a bounded software engine, not an unlimited autonomous team.
Source snapshots are UTF-8, up to 200 KB per file and 4 MB/2,500 files total,
with a 110 KB complete review diff. Binary assets, empty repositories,
monorepo-scale changes, arbitrary shells, networked tests, infrastructure
provisioning and automatically creating Coolify resources are outside this
release. Split larger work into separate tasks. No automatic merge or
production rollback is performed.

Deployment validation covers file structure and review, not an actual Docker build of every target. Target CI and post-deployment smoke tests remain necessary. Reported dollar limits are checked between model calls and can overshoot by one call; if a provider omits cost, `/status` marks the total partial and token/call/time limits still apply. Set an OpenRouter key credit limit for a hard spend ceiling.

See [architecture](docs/ARCHITECTURE.md), [operations and upgrade](docs/OPERATIONS.md), and [acceptance/verification](docs/VERIFICATION.md).

## v0.3 implementation and acceptance

Natural private-chat goals and `POST /v1/chat` now use a bounded, durable skill DAG in the
existing queue. `/dashboard` provides the shared Decision Inbox, task/evidence views and
knowledge actions. See [implementation, operating choices and release acceptance](docs/V03_IMPLEMENTATION_AND_ACCEPTANCE.md)
for the operator-accepted staging record, deployment recovery, rollback confirmation,
budget semantics and the remaining live gates.
