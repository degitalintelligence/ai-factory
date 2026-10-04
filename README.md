# AI Factory V0.2

An operating engine for building software from Telegram requirements: **repository-aware planning → implementation → isolated tests → independent review → repair → GitHub PR → explicit publication or commit-bound Coolify deployment**.

LioBot = AI Factory. This repository is the LioBot core product and its bounded
engineering engine. `telegram-lab` is a test/acceptance harness only; it is not a
second LioBot product. Quant Factory, Kedaya, and other business products remain
separate registered repositories and runtimes.

## LioBot Core v0.3 direction

The v0.2 engine is the guarded execution foundation. The v0.3 requirements are
tracked in [docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md](docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md)
and are delivered by milestone, not by weakening the v0.2 gates.

M1 currently adds channel-neutral intent/plan API aliases, a bounded skill
registry, explicit plan assumptions/dependencies/skills/budget/approval gates,
configurable `bunny-alpha` model alias resolution, and actionable budget
warnings. Context/memory, Decision Inbox, and self-improvement contracts already
present in v0.2 remain the source of truth for later M2–M4 work. Telegram is
still only an adapter and `telegram-lab` remains the acceptance harness.


## What works

- Persistent PostgreSQL queue, task events, plans, diffs, test reports and review evidence.
- Atomic claims, worker leases/heartbeats, bounded restart recovery, one active task per repository.
- Multiple registered projects with explicit repository, branch, Python/Node test profile and deployment policy.
- Separate OpenRouter models for Lead, Developer and Reviewer. Structured responses, bounded retries, token/call/time budgets and provider-reported cost tracking.
- Developer tools: inspect, search, replace/write/delete files, review a complete diff, run sandbox checks.
- Deterministic gates: failing/absent tests, generated databases, credentials, symlink escapes, changed source after review, incomplete acceptance mapping and oversized diff cannot be approved by a model.
- High-risk plan approval bound to its hash; clarification, cancellation, retry and feedback to an existing open PR.
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

For local API development, set `WORKER_ENABLED=false`, leave the Telegram token empty and use an `API_TOKEN`. Run `uvicorn app.main:app --port 8080`. Workers in a real deployment require PostgreSQL and a healthy sandbox.

## Current limits

V0.2 is a bounded software engine, not an unlimited autonomous team. Source snapshots are UTF-8, up to 200 KB per file and 4 MB/2,500 files total, with a 110 KB complete review diff. Binary assets, empty repositories, monorepo-scale changes, arbitrary shells, networked tests, infrastructure provisioning and automatically creating Coolify resources are outside this release. Split larger work into separate tasks. No automatic merge or production rollback is performed.

Deployment validation covers file structure and review, not an actual Docker build of every target. Target CI and post-deployment smoke tests remain necessary. Reported dollar limits are checked between model calls and can overshoot by one call; if a provider omits cost, `/status` marks the total partial and token/call/time limits still apply. Set an OpenRouter key credit limit for a hard spend ceiling.

See [architecture](docs/ARCHITECTURE.md), [operations and upgrade](docs/OPERATIONS.md), and [acceptance/verification](docs/VERIFICATION.md).
