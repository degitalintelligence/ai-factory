# AGENTS.md — AI Factory / LioBot Agent Instructions

> Single source of operational rules for this repository.
> Product identity: LioBot = AI Factory.
> This repository is the LioBot core engine. Read this file before changing code.
>
> **Requirement authority:** `docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md` is the current product
> and engineering requirement. It amends `docs/AI_FACTORY_FULL_REQUIREMENTS (2).md` and
> `docs/AI_FACTORY_REQUIREMENTS_AMENDMENT_V1.1.md` and is read before them. Where V0.3 and an
> earlier document disagree, V0.3 wins. See §3 for the full precedence order.

## 0. Project at a glance

- Product: LioBot / AI Factory — one product.
- Repository: degitalintelligence/ai-factory.
- Role: control plane, orchestration engine, policy/gates, worker, evidence store, sandbox client, and channel adapters.
- Test repository: degitalintelligence/telegram-lab.
- Important boundary: telegram-lab is a test laboratory and acceptance harness only.
- Current baseline: implemented V0.3 Chief of Staff on the guarded V0.2 engine;
  the repository records operator acceptance. V0.4 is an implementation candidate,
  authorized by Dedi, with live release gates still pending. Consult
  `docs/V04_IMPLEMENTATION_AND_ACCEPTANCE.md`; never infer live acceptance from CI.
- Runtime: Python 3.12, FastAPI, async SQLAlchemy, Pydantic, PostgreSQL for multi-worker operation, SQLite only for local/unit tests.
- Channels today: Telegram control interface and optional authenticated HTTP API.
- Execution: control service, disposable worker leases, and a separate credential-free sandbox service.
- Release: GitHub PR publication, non-deploy publication acknowledgement, and explicit full-commit-SHA Coolify deployment.
- Current model convention: Lead, Developer, and Reviewer use the configured provider model
  `deepseek/deepseek-v4-flash-0731` unless Dedi explicitly approves a tested configuration
  change. If an alias is used, resolve it through environment configuration only; never
  hard-code it in business logic.

Generated caches, local databases, runtime workspaces, credentials, and Docker
volumes are not source files.

## 1. Mandatory pre-work

Before writing or changing code, every agent must:

1. Read this AGENTS.md completely.
2. Read README.md.
3. Read relevant sections of docs/ARCHITECTURE.md, docs/VERIFICATION.md, and docs/OPERATIONS.md.
4. Read `docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md`, then the amendment
   `docs/AI_FACTORY_REQUIREMENTS_AMENDMENT_V1.1.md`, then the supplied product requirements.
   V0.3 is authoritative for product identity, product boundary, capability levels,
   decision categories, memory model, and acceptance criteria.
5. Inspect the current module and test structure before choosing a file.
6. Search for existing state transitions, schemas, gates, and tests before adding a duplicate.
7. Establish the current branch, HEAD SHA, working-tree changes, and target project policy.
8. Identify whether the request changes control-plane code, sandbox code, a channel adapter, project registry, or deployment policy.
9. Define observable acceptance criteria and evidence for every criterion.
10. If the requirement is ambiguous, STOP and ask Dedi. Do not invent business rules, permissions, provider behavior, or deployment authority.

A model response is not evidence. Evidence is a deterministic test result,
structured event, persisted artifact, diff, review record, or verified external
reference.

---

## 2. Binding product decisions

These decisions are locked unless Dedi explicitly changes them.

### 2.1 Product identity

- LioBot and AI Factory are one product.
- Chief of Staff, orchestrator, skills, agents, context/memory, policies, tools, approvals, audit, and channel adapters belong inside LioBot.
- The engine must eventually identify a gap, create a self-improvement task, implement it through its own guarded workflow, evaluate it, request Dedi's decision, and retain the learning.
- Self-building never means uncontrolled self-editing or direct production mutation.

### 2.2 Repository boundary

- ai-factory is the LioBot core repository.
- telegram-lab is a test target, regression fixture, and acceptance harness.
- Existing work in telegram-lab must be preserved. Do not delete, rewrite, or promote it into the final product merely to satisfy terminology.
- Do not build a separate final LioBot inside telegram-lab.
- If LioBot needs to modify its own source, register ai-factory explicitly in the project registry with its own policy and branch. Never silently repoint the default lab alias.
- Quant Factory, Kedaya, and other business products remain separate repositories, runtimes, databases, workspaces, and credentials. LioBot may build or coordinate them through explicit connectors.

### 2.3 Human control

- Merge to a protected branch is human-controlled.
- Production deployment is human-controlled.
- Permission, credential, sandbox, budget hard limit, network policy, external communication, payment, deletion, and irreversible data actions require explicit approval.
- The engine may prepare a plan, branch, PR, deployment pack, or decision card. It must not infer approval from model confidence.

---

## 3. Source of truth and precedence

When documents or prompts conflict, use this order:

1. The latest explicit instruction from Dedi.
2. docs/LIOBOT_CORE_V0_3_REQUIREMENTS.md, the current product and engineering requirement.
3. docs/AI_FACTORY_REQUIREMENTS_AMENDMENT_V1.1.md, where V0.3 is silent.
4. This AGENTS.md for repository operation and safety.
5. docs/AI_FACTORY_FULL_REQUIREMENTS (2).md, including its normative correction section.
6. README.md and docs.
7. Existing implementation and historical notes.
8. Agent assumptions last.

The old wording that AI Factory builds LioBot as an application in a target
repository is obsolete. A target repository may be telegram-lab for proof and
acceptance, but LioBot itself is the ai-factory product.

If a conflict could change architecture, data, security, cost, or deployment,
create a DECISION_REQUIRED item for Dedi instead of choosing silently.

---

## 4. Scope rules

### 4.1 Current release

The bounded V0.2 engine remains mandatory. It supports repository-aware
planning, bounded implementation, isolated tests, independent review, evidence,
PR publication, acknowledgement of merged PRs for non-deployable targets, and
explicit deployment of an existing registered Coolify target.

It is not yet an unlimited autonomous company, unrestricted shell agent,
automatic production deployer, or generic workflow engine.

V0.3 (`LIOBOT_CORE_V0_3_REQUIREMENTS.md`) moves the product to a Chief of Staff
and orchestration engine. Intent resolution, context assembly, skill registry,
scoped memory, improvement proposal lifecycle, budget warning, `/v1` APIs, and
the shared dashboard/inbox are now implemented in this repository and covered
by tests plus the staged acceptance record. Do not describe unsatisfied release
gates, broader live acceptance, or later milestones as already complete.

Implement V0.3 in the milestone order given in that document (M1 core
orchestration, M2 decision experience, M3 memory and evaluator, M4
self-improvement). Do not start M4 before M1 to M3 pass their acceptance test.
The V0.2 guardrails in this file remain mandatory throughout: removing or
weakening one requires an improvement proposal and regression test, not a
refactor.

### 4.2 Work that belongs in this repository

- Task intake, idempotency, ownership, leases, recovery, and queue state.
- Lead planning, role contracts, structured model calls, budgets, and retries.
- Bounded repository tools, workspace hygiene, source digests, and diffs.
- Sandbox submission, isolation checks, test artifacts, deterministic gates, and review.
- GitHub branch/commit/PR reconciliation.
- Approval records, decision state, task events, audit evidence, and deployment records.
- Channel adapters that translate Telegram, HTTP, or future dashboard actions into core contracts.
- Context/memory, decision inbox, skill registry, and self-improvement workflow when implemented according to the roadmap.

### 4.3 Work that does not belong here by default

- Business application logic for telegram-lab, Kedaya, Quant Factory, or another product.
- Production credentials, customer data, payment execution, external messaging, or direct database mutations in target products.
- A second task queue or orchestrator inside a target repository.
- Ad hoc scripts that bypass policy, evidence, sandbox, or approval gates.
- Generic abstractions without an active user story and acceptance test.


---

## 5. Architecture invariants

### 5.1 Control plane

The control service owns configuration, task intake, policy snapshots, model
calls, GitHub credentials, Telegram credentials, Coolify credentials, task
state, and durable evidence. It runs FastAPI, the Telegram poller, and the
worker pool.

The control plane must never execute generated target code directly.

### 5.2 Persistence

- PostgreSQL is required for deployed multi-worker operation.
- SQLite is allowed only for local development and tests that explicitly use it.
- Startup schema changes are additive and idempotent.
- Existing task rows, events, artifacts, workspaces, and deployment records are retained.
- Queue claims use locking and lease fencing. A stale worker cannot overwrite a newer worker.
- Restart recovery resumes at a durable stage boundary. It may repeat a bounded development step, but it must not duplicate a task, branch, PR, or deployment.
- One active task per repository is the default safety policy.

### 5.3 Worker and workspace

- A task receives an isolated deterministic branch and workspace.
- Do not delete an existing workspace, reset it destructively, or mix two tasks in one checkout.
- Preserve unrelated user changes when inspecting a worktree.
- A source digest is captured before testing and again before commit.
- Untracked files are part of the complete review diff.
- Review and publication use the complete diff, not only tracked files.

### 5.4 Sandbox boundary

The sandbox is a separate service, not a convenience subprocess.

- The sandbox receives a bounded source snapshot and approved commands only.
- It receives no Git metadata, control checkout, database credential, GitHub token, OpenRouter key, Telegram token, Coolify token, Docker socket, or host directory.
- It runs non-root with dropped capabilities, read-only root filesystem, resource limits, and fresh filesystem/PID/network/IPC/user namespaces.
- Bubblewrap user-namespace setup is mandatory. There is no insecure direct-host fallback.
- Test commands are offline. Dependency installation is an explicit operator/project-policy opt-in and is limited to the supported wheel/npm-ci path.
- The runner-specific AppArmor profile is for the runner only. Do not weaken global host restrictions or add privileged mode.
- If isolation cannot be established, fail closed and report the blocker.

Never move generated code execution into the credential-bearing control
container to work around a sandbox failure.

### 5.5 External product isolation

A registered project policy is explicit configuration, not model-generated
authority. Each project has its own repository, base branch, profile, deployment
requirement, dependency policy, and optional Coolify target. Policy is snapshotted
at task creation; revoking or changing a policy stops pending work that no longer
matches.

No product repository shares LioBot's database, workspace volume, secrets, or
deployment credentials.

---

## 6. Model roles and structured output

### 6.1 Model policy

- Configure Lead, Developer, and Reviewer explicitly through environment variables.
- Current standard is provider model `deepseek/deepseek-v4-flash-0731` for
  Lead, Developer, and Reviewer. An alias is optional, but any alias must stay
  environment-configured and must never be hard-coded in business logic.
- Record the alias and the resolved provider model for every call.
- Do not silently switch provider, model, temperature, output limit, or fallback path.
- A fallback model is permitted only when policy explicitly allows it, and the
  fallback must be recorded in the call audit.
- Any model change requires a bounded regression task, cost/risk assessment, and Dedi approval when it affects spending, data, or safety.
- Record role, model, call count, token usage, reported cost, timeout, and validation failure category.
- Provider-reported cost may be partial. Call, token, and wall-clock limits still apply.
- Use provider credit limits for a hard spend ceiling; application limits are not a billing guarantee.
- Validate every structured response locally with Pydantic before using it.
- Retry diagnostics may include schema/category feedback, but must not echo secrets or raw sensitive model output.

### 6.2 Lead

Lead receives the requirement, project policy, and bounded repository context.
Lead produces a plan, not source code.

A valid plan has numbered observable acceptance criteria, constraints, suggested
tests, implementation steps, risks, blocking questions, risk level,
deployment_required, and persistence_required.

Lead must not write files, run target commands, push, merge, or deploy.

### 6.3 Developer and specialist roles

Developer actions use the bounded DeveloperAction contract. Supported actions are
list_files, read_file, search, write_file, replace_text, delete_file,
run_command, git_diff, and finish.

Rules:

- Use a flat validated action object. Do not return a tool_calls wrapper, action array, or invented action name.
- Inspect relevant files before editing.
- Use repository-relative paths only.
- Never read credential paths, Git internals, runtime databases, or unrelated workspaces.
- Never push, merge, deploy, alter project registry, or change security policy from a sandbox task.
- Do not declare success before inspecting the complete diff and running required tests.
- A finish action must summarize changed files, tests, limitations, and remaining risk.

### 6.4 Independent Reviewer

The Reviewer is independent from the Developer iteration. It receives the
requirement, plan, baseline provenance, complete diff, deterministic test report,
standalone test evidence, source digest, hygiene findings, and feedback.

The Reviewer must provide exactly one evidence entry for every numbered acceptance
criterion. An AI reviewer cannot override a deterministic gate. Approval with
unresolved issues or missing evidence is invalid.

---

## 7. Skill system and future domains

A skill is a capability contract, not a prompt pasted into a role.

Every skill must declare:

- stable id and version;
- domain and purpose;
- inputs and outputs;
- allowed tools;
- data and permission scope;
- risk level and approval policy;
- deterministic evaluator or evidence contract;
- tests and regression fixtures;
- rollback or disable behavior;
- owner and change history.

The model may propose a new skill, but it cannot install, activate, grant tools
to, or widen the permission of a skill without the normal task, review, and
approval gates.

Initial capability is Engineering. The architecture must support Product,
Marketing, Admin/Ops, Finance, Sales/Customer Success, and specialist factory
skills later. A domain skill may prepare drafts and recommendations before it
is allowed to perform real external action. Each domain needs its own staging
acceptance and least-privilege connector.

Do not build all future domain schema, a generic workflow engine, or an
unbounded agent marketplace before an active skill has a user story and test.

---

## 8. LioBot self-building contract

### 8.1 Required loop

Self-improvement must follow this sequence:

~~~text
observe gap or recurring failure
  -> diagnose with evidence
  -> propose improvement and expected benefit
  -> create self_improvement task in ai-factory
  -> isolated branch and workspace
  -> implement bounded change
  -> mandatory tests, evaluation, security checks
  -> independent review
  -> decision card for Dedi
  -> explicit approve, reject, ask, or defer
  -> controlled merge/deploy under policy
  -> before/after measurement and retained learning
  -> rollback or disable on regression
~~~

### 8.2 Allowed self-improvement

With normal gates, LioBot may improve prompts, role contracts, schemas,
retry/routing logic, skills, evaluators, parsers, connectors, adapters,
retrieval, memory indexing, freshness checks, dashboard/Telegram presentation,
notification wording, observability, documentation, runbooks, and test fixtures.

The change must be isolated, reviewable, measurable, and reversible.

### 8.3 Always human-controlled

Dedi approval is mandatory for permission, allowlist, sandbox boundary, network
egress, credential scope, model policy, budget hard limits, provider changes,
merge, deployment target, production rollback, external messages, publication,
payment/transfer, legal action, deletion of evidence/data/workspaces, or any
change that weakens a gate or creates an irreversible production effect.

### 8.4 Self-improvement evidence

Every self-improvement task stores:

- problem statement and occurrence evidence;
- hypothesis and expected benefit;
- exact files/components in scope;
- baseline behavior;
- tests, evaluation, security result, and independent review;
- before/after quality, reliability, latency, token/cost, and failure mode;
- risk, blast radius, rollback/disable plan, and review date;
- Dedi decision and resulting state.

No self-edit is accepted because a model says it is better.


---

## 9. Context, memory, and Dedi communication

These are product requirements for the core LioBot roadmap. Do not pretend they
are fully implemented in V0.2; add them through explicit contracts and tests.

### 9.1 Context and memory

A memory item must be able to retain source/reference, project or business
scope, owner, timestamps, freshness/expiry, confidence, sensitivity,
provenance, version, correction/retraction, permission, and evidence link.

The orchestrator retrieves relevant context slices, not a raw database dump.
Agents receive only data authorized for the task. Stale, contradictory, or
unverified information is marked. Secrets and credentials never enter ordinary
memory, prompts, logs, artifacts, or review text.

### 9.2 Dedi-first communication

Dedi must not need raw logs to understand progress. Each important update states:

- what happened;
- why it happened;
- evidence or artifact link;
- missing information and risk;
- available options;
- LioBot recommendation;
- whether Dedi must act now;
- consequence of approve, reject, ask, or defer.

Standard message types are UPDATE, NEED_INFO, DECISION_REQUIRED,
APPROVAL_REQUIRED, WARNING, BLOCKED, COMPLETED, and LEARNING_PROPOSAL.

V0.3 adds the decision categories `risk_escalation`, `clarification_needed`,
`blocked`, `recommendation`, `learning_proposal`, and `incident`. They are
roadmap until the Decision Inbox categories ship; do not invent them in the
current message set.

### 9.3 Decision Request and Decision Inbox

A decision request minimally contains decision_id, task_id, type, title,
situation, why_now, options, impact, risk, recommendation, evidence,
missing_information, expiry, rollback, and required_action.

Telegram, dashboard, and future adapters must read and mutate one durable
Decision Inbox. The same item must have the same state and evidence in every
channel. Natural language answers such as approve, reject, ask, and defer must
resolve to an exact scoped, idempotent, auditable action.

No decision may live only in a Telegram message.

---

## 10. Task lifecycle and idempotency

The durable lifecycle is:

~~~text
received
-> planning
-> waiting_input (optional)
-> awaiting_approval (optional)
-> developing
-> testing
-> reviewing
-> publishing
-> pr_created
-> completed (for non-deployable projects after /publish)
-> superseded (stale merged PR closed before fresh review)
-> reviewed (review-only task completed without a correction or PR)
-> deployment_pending (optional)
-> deploying
-> deployed / deployment_unknown / deployment_failed
-> completed / failed / cancelled
~~~

Rules:

- Intake enters planning only after project alias and policy validation.
- Blocking questions enter waiting_input; do not continue by guessing.
- High-risk plans enter awaiting_approval and bind approval to the exact plan hash.
- Development enters testing only after a nonempty diff and Developer finish.
- Testing enters reviewing only after mandatory test evidence is persisted.
- Rejected review may return to developing only while budget and iteration limits remain.
- Publishing requires Reviewer approval and zero deterministic gate issues.
- Review-only tasks may end in `reviewed` only after mandatory tests and independent review; they must not invent a mutation or PR.
- PR publication reconciles an existing branch/PR after lost responses; it does not create duplicates.
- Deployment requires a human command with a full 40-character merged SHA.
- deployment_unknown is not auto-retried.
- Terminal states do not change without a new explicit action or event.
- Duplicate Telegram/API requests are idempotent.
- Retry retains lifetime calls, tokens, cost, and recovery history.

---

## 11. Telegram, API, and adapter rules

Telegram is a channel adapter, not domain logic. Future dashboard, Slack,
WhatsApp, or API adapters must use the same task, event, artifact, approval,
and decision contracts.

Current Telegram commands:

| Command | Purpose |
|---|---|
| /start | help and access check |
| /new | create a task |
| /projects | list registered projects |
| /tasks | list operator tasks |
| /status | task status and budgets |
| /plan | plan and approval hash |
| /logs | task events and diagnostics |
| /report | evidence report |
| /answer | answer a blocking question |
| /approve | approve exact high-risk plan hash |
| /cancel | cooperative cancellation |
| /retry | bounded retry with retained lifetime budget |
| /feedback | revise an existing open PR |
| /publish | acknowledge a merged PR for a non-deployable project |
| /supersede | close a stale merged PR before fresh review; no release is recorded |
| /deploy | explicit deployment by full merged SHA |
| /deployment | reconcile a deployment |

Telegram security:

- private chats only;
- numeric allowlist;
- fail closed if a bot token exists without an allowlist;
- verify task ownership for user-level commands;
- reject group messages and unauthorized access;
- deduplicate repeated updates;
- do not reveal task details to unauthorized users.

HTTP operator endpoints are Bearer-protected when API_TOKEN is set. An empty
token disables the operator endpoints. Never put tokens in URLs, requirements,
logs, or model prompts. Do not expose the sandbox endpoint publicly.

---

## 12. Security and non-negotiable boundaries

- Secrets belong in Coolify/operator secret management, never in Git, task requirements, prompts, artifacts, fixtures, or documentation.
- Redact API keys, bot tokens, database URLs/passwords, Authorization headers, and recognizable credential patterns before logging or publication.
- The control container may hold provider credentials; the sandbox may not.
- Do not mount Docker socket, host filesystem, host PID/network, or production database into the sandbox.
- Do not use privileged mode, arbitrary capability additions, or direct-host execution fallback.
- File tools reject traversal, symlink escapes, Git internals, sensitive environment paths, oversized files, and runtime artifacts.
- Git uses temporary askpass and clean remote URLs; tokens never enter remote URLs or command arguments.
- Project registry and deployment policy are operator-owned. The model cannot modify them.
- No external message, payment, data deletion, or production mutation without explicit approval and audit record.
- A security bypass is a P0 blocker even if feature tests pass.

---

## 13. Evidence and deterministic gates

Before PR publication, all must be true:

- complete diff is nonempty and includes untracked files;
- mandatory tests exist and exit zero;
- no timeout, test artifact, generated database, credential, traversal, or symlink violation;
- source digest is unchanged during testing and matches the reviewed commit;
- every acceptance criterion has exactly one satisfied evidence entry;
- Reviewer approved with no unresolved issue;
- deployment files are complete when deployment was requested;
- task policy and branch/repository identity still match.

Deterministic gates cannot be overridden by Lead, Developer, Reviewer, or any
model. If a gate fails, report the exact blocker and stop publication.

For deployment-required tasks, validate Dockerfile, Compose, non-root app user,
enabled healthchecks, restart policies, no unsafe host mounts, declared
persistent volumes, environment placeholders, and backup/rollback/health


---

## 14. Code conventions

- Python 3.12.
- Type hints for new public functions and Pydantic models for external and LLM boundaries.
- Async I/O for FastAPI, database, HTTP, Telegram, GitHub, and Coolify calls.
- Keep control-plane code, sandbox code, and provider clients separated.
- Reuse existing store, schema, security, workspace, gate, and deployment services before adding new ones.
- Do not duplicate state transition logic in Telegram or HTTP handlers.
- Keep model prompt and response parsing deterministic and locally validated.
- Use Ruff for lint and formatting. Do not suppress a rule without a narrow reason.
- Avoid broad Any, hidden globals, unbounded recursion, unbounded subprocesses, or silent exception swallowing.
- Commands must have an allowlist, timeout, output limit, and deterministic result record.
- File paths are repository-relative and validated centrally.
- Never persist raw provider responses when they may contain secrets or private source.
- Keep migrations additive and idempotent. Never delete historical task or evidence rows as a shortcut.
- New behavior needs a regression test and, when relevant, a real sandbox or integration test.
- Documentation must describe behavior that actually exists; label roadmap behavior as roadmap.

Do not mix refactors with security-sensitive or state-machine changes unless the
refactor is necessary for the requested behavior and has its own regression
coverage.

---

## 15. Project registry and target repositories

The registry is operator configuration. A project policy includes repository,
base branch, profile, dependency-install permission, deployment requirement, and
optional Coolify UUID.

Rules:

- Alias must be validated before task creation.
- Repository must be owner/name format.
- Profile is currently python or node.
- Policy is snapshotted into the task.
- A target checkout is never treated as trusted merely because its owner is familiar.
- telegram-lab remains the default proof target until an explicit product policy says otherwise.
- To self-build LioBot, register ai-factory as a separate explicit alias and use a dedicated self-improvement task type.
- Do not allow the model to edit PROJECTS_JSON, deployment UUIDs, branch policy, allowlists, or credentials.
- One active task per repository prevents concurrent branches from corrupting a shared workspace.

---

## 16. Environment and secrets

Tracked environment templates may contain names and safe placeholders only.
Never commit real values.

Required operator values normally include:

- POSTGRES_PASSWORD or a direct disposable DATABASE_URL;
- OPENROUTER_API_KEY;
- TELEGRAM_BOT_TOKEN;
- TELEGRAM_ALLOWED_USER_IDS;
- GITHUB_TOKEN;
- LEAD_MODEL, DEVELOPER_MODEL, REVIEWER_MODEL;
- SANDBOX_TOKEN;
- API_TOKEN when the operator API is needed.

Optional values include PROJECTS_JSON, Coolify URL/token, resource budgets,
worker settings, and dependency-install policy.

Use a separate strong random SANDBOX_TOKEN. It must not equal any provider or
database secret. Keep secrets out of build arguments, task text, model context,
Git remotes, logs, screenshots, and PR bodies.

Changing an environment variable does not change a password already stored in an
initialized PostgreSQL volume. Treat upgrades and credential rotation as an
explicit operations task.

---

## 17. Local development contract

From the repository root:

~~~bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app sandbox tests scripts
ruff format --check app sandbox tests scripts
python -m pytest -q
~~~

For a local API without a worker, set WORKER_ENABLED=false, leave the Telegram
token empty, set a disposable API_TOKEN if needed, and run:

~~~bash
uvicorn app.main:app --port 8080
~~~

PostgreSQL integration uses TEST_POSTGRES_URL pointing only to a disposable
database. Tests must not use the production or persistent deployment database.
The real sandbox smoke test requires Docker/Linux and the runner profile; if it
cannot run locally, use the CI job and record that limitation.

Compose validation:

~~~bash
docker compose config --quiet
docker build --target control -t ai-factory-control:test
docker build --target sandbox -t ai-factory-sandbox:test
~~~

Do not run generated target code directly from the host to claim sandbox
verification. Use scripts/sandbox_smoke.py or the real CI sandbox job.

---

## 18. Required verification by change type

### Any Python or schema change

- Ruff check.
- Ruff format check.
- Full pytest.
- Relevant targeted tests.
- Confirm no secret, runtime database, cache, or generated artifact enters the diff.

### Queue, worker, state, or database change

- Existing historical task migration is applied twice.
- Concurrent claim, lease fencing, and recovery tests pass.
- Cancellation, idempotency, duplicate update, and terminal-state tests pass.
- PostgreSQL integration passes when available.
- No destructive migration or loss of evidence.

### LLM, prompt, schema, or model change

- Valid response test.
- Malformed JSON, fenced JSON, unknown fields, missing fields, refusal, truncation, timeout, and provider error tests.
- Budget and usage accounting tests.
- One real bounded task on the configured provider before claiming compatibility.
- No silent model/provider fallback.

### Sandbox or security change

- Traversal, symlink, Git-internal, credential-path, oversize, unauthorized-token, and network-off tests.
- Real Docker runner smoke test.
- Namespace failure must fail closed.
- No insecure fallback, privileged container, host mount, or Docker socket.

### GitHub or deployment change

- Lost-response and reconciliation test.
- Exact branch, commit, tree, PR, and check identity test.
- Duplicate PR and deployment prevention.
- deployment_unknown manual reconciliation test.
- Compose structural gate and staging smoke test when applicable.

### Telegram, API, or decision change

- Unauthorized access and ownership tests.
- Duplicate request and idempotency test.
- Same state and evidence contract across adapters.
- Error messages do not leak secrets or private task data.
- Decision request is actionable without raw log hunting.


---

## 19. Open decisions and unresolved items

Record new ambiguity here or in a linked decision artifact. Do not hide it in
model prompts or code comments.

| Topic | Status | Rule |
|---|---|---|
| LioBot versus AI Factory identity | RESOLVED | They are one product. |
| telegram-lab role | RESOLVED | Test harness and acceptance target only. |
| Merge and production deployment | RESOLVED | Human-controlled. |
| V0.3 versus amendment V1.1 authority | RESOLVED | `LIOBOT_CORE_V0_3_REQUIREMENTS.md` is current and wins; V1.1 applies only where V0.3 is silent. |
| Default model | RESOLVED | Provider model `deepseek/deepseek-v4-flash-0731` for Lead, Developer, Reviewer through config. Record the configured name and the resolved provider model per call. |
| V0.3 capability levels L0 to L3 | RESOLVED | L0 read, L1 plan/draft in isolated workspace, L2 non-production change and staging deploy per project policy, L3 production, external message, financial transaction, credential. L3 always requires Dedi approval. An agent must not skip a level because it feels confident. |
| V0.3 minimum data model and `/v1` API | IMPLEMENTED CANDIDATE | Core `/v1` chat, overview, intent result, memory and improvement endpoints plus the dashboard are implemented and covered by tests; broader conversation continuity and live acceptance remain roadmap. Business logic must remain usable without Telegram. |
| Final Dedi interface | RESOLVED | Telegram private natural chat and the same-origin operator dashboard share the existing core inbox/state. User authorized autonomous v0.3 design decisions; see V03_IMPLEMENTATION_AND_ACCEPTANCE.md. |
| Context/memory backend | RESOLVED | Existing SQL memory with tenant/owner/role filtering, provenance, versioning, conflict records and withdrawal checks on checkpoint recovery. No additional memory service. |
| Memory tenant isolation key | RESOLVED | Dedi chose a real `tenant` column as the hard boundary plus `owner` as a real filter within one tenant. Every read, write, correction, retraction, and history lookup filters on tenant. A row owned by a person is visible only to that owner, unowned rows are shared inside the tenant, and a read with no principal returns shared memory only. Role clearance applies after the owner filter and never widens across it. The active-key uniqueness is `(tenant, key, scope)`; `owner` stays outside it, so one tenant holds one active value per key and scope. A tenant registry/mapping is still OPEN. |
| Skill registry and Team Planner implementation | IMPLEMENTED CANDIDATE | Bounded six-skill DAG in the existing queue. Engineering uses the verified engine; other skills analyze/draft only. Release acceptance still requires real-provider/staging evidence. |
| Credential scanner false positives | RESOLVED | Explicit budget detection requires an amount bound to budget vocabulary (`app/engineering_budget.py`). Fixture credentials in `tests/` and `docs/` are exempted only by an explicit per-line `# credential-fixture` marker (`app/workspace.py`); every other path stays fail-closed. Fixed directly per Dedi's 2026-10-08 decision because the scanner blocked all self-improvement tasks; effective after redeploy. |
| ai-factory self-target registry alias | RESOLVED | Dedi approved on 2026-10-08: alias `self` -> `degitalintelligence/ai-factory`, profile `python`, base `main`, `require_deployment=false`, `install_dependencies=false` (operator defaults), set via `PROJECTS_JSON` + `SELF_PROJECT=self` in Coolify. Self-improvement intake stays fail closed until those env values are active; merge and deploy of self-changes remain human-controlled. |
| Budget admission cost re-fund | RESOLVED | On the adjustable path (fresh or retry readmission without an explicit budget), admission re-funds `max_cost_usd` like calls/tokens: floor = max(Lead estimate, default PlanBudget $1.0), capped at the operator ceiling. Explicit budgets stay binding and lifetime spend above the funded floor still blocks. Fixed 2026-10-08 per Dedi's decision after self-improvement tasks #83/#84/#85 died at shrinking Lead cost estimates ($0.2/$0.1/$0.05) while calls/tokens were always re-funded; regression tests in `tests/test_engineering_budget.py`; effective after redeploy. |
| Domain skills beyond Engineering | ROADMAP | One domain at a time, with least-privilege connectors and staging evidence. |

When an open decision blocks safe execution, create DECISION_REQUIRED and stop
the affected branch of work. Do not resolve it by code convention.

---

## 20. Anti-over-engineering rules

Agents must not:

- create a microservice merely because a module may grow;
- create a second queue, orchestrator, memory store, or approval system;
- add Redis, a vector database, a generic workflow engine, or a package monorepo without a measured need and approved design;
- give every agent unrestricted database or repository access;
- make telegram-lab the final product;
- make the model decide its own permissions, model budget, deployment target, or project registry;
- add future domain schema before the active user story requires it;
- bypass deterministic gates because a model review passed;
- auto-merge, auto-deploy, or auto-rollback stateful production;
- delete workspaces, evidence, volumes, or historical records to make a test pass.

Prefer the smallest change that preserves existing contracts, evidence, and the
rollback path.

---

## 21. When you finish work

Before reporting completion:

1. Run the verification required by the change type.
2. Inspect git diff, including untracked files, and confirm no unrelated user work was overwritten.
3. Confirm task state, evidence, branch, commit, and PR identity where applicable.
4. Update README/docs/AGENTS only when behavior or operating procedure changed.
5. Record new open decisions and limitations.
6. Report exactly:
   - what changed;
   - why;
   - tests and exact results;
   - files and artifacts;
   - security or migration implications;
   - remaining risk;
   - whether human approval is required.
7. Do not claim a PR, merge, deployment, or business acceptance unless the corresponding evidence exists.
8. Do not push, merge, deploy, rotate credentials, or delete data without explicit authorization.

---

## 22. Historical compatibility

- V0.1 task rows remain historical and may lack owner, policy snapshot, source digest, or recovery metadata.
- Do not silently reassign or auto-deploy legacy tasks. Start a new V0.2 task for new work.
- Existing telegram-lab PRs and tests remain useful acceptance evidence.
- Existing Compose resources and named volumes must be preserved during upgrades.
- Additive migrations are preferred; a rollback must consider database compatibility before changing the running image.

---

*Last reviewed: 2026-10-04. Owner: Dedi Setiadi.*

*Amendment 2026-10-04: recorded `LIOBOT_CORE_V0_3_REQUIREMENTS.md` as the current
requirement with precedence over amendment V1.1; resolved the then-current `bunny-alpha`
alias against provider model `stealth/space-bunny-alpha`; recorded V0.3 capability levels
L0 to L3 as binding; labelled the V0.3 data model, `/v1` API, and Decision Inbox
categories as roadmap; opened a memory tenant-isolation decision. No runtime behaviour
changed.*

*Amendment 2026-10-09: Dedi approved replacing the retired `bunny-alpha` standard with
provider model `deepseek/deepseek-v4-flash-0731` for Lead, Developer, and Reviewer.
Historical acceptance records may still mention the retired alias; current config and
operator documentation should follow the new model standard.*


