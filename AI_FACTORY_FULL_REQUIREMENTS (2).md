# AI Factory — Full Product & Engineering Requirements

**Status:** Baseline untuk eksekusi developer  
**Version:** 1.1  
**Tanggal:** 2026-10-04  
**Owner:** Dedi / PT Pohon Keahlian Digital  
**Repository engine:** degitalintelligence/ai-factory  
**Current target:** degitalintelligence/telegram-lab  
**Current model:** stealth/space-bunny-alpha untuk Lead, Developer, Reviewer

---

## 1. Ringkasan produk

LioBot adalah produk AI dan AI Factory adalah nama engine internalnya. LioBot menerima objective manusia, memahami repository atau business context, membuat rencana, membentuk tim/role/skill, mengeksekusi pekerjaan melalui tool yang dibatasi, menghasilkan evidence, menguji hasil, melakukan review independen, meminta approval manusia pada tindakan berisiko, lalu menerbitkan artefak, PR, atau deployment yang dapat diaudit.

LioBot bukan hanya coding bot. Coding adalah capability pertama karena batas verifikasinya paling jelas. Arsitektur harus mendukung:

- Engineering Factory: requirement, design, coding, testing, security review, release preparation.
- Product/Research Factory: discovery, PRD, competitor research, specifications, experiments.
- Marketing Factory: positioning, offers, copy, campaign plans, content, reporting.
- Admin/Ops Factory: SOP, recurring work, reconciliation preparation, checklists, reports.
- Finance Factory: budgeting, cash-flow analysis, invoice/reconciliation preparation, variance analysis.
- Sales/Customer Success Factory: lead qualification, response drafts, follow-ups, escalation.
- Specialist Factory: Quant Factory, Kedaya agents, dan produk domain lain.

Core loop:

~~~text
Objective → Context & policy → Plan → Team/skills → Tool-bounded work
→ Evidence/tests → Independent review → Human approval
→ Publish/action/deploy → Feedback, learning and audit
~~~

LioBot adalah produk/orchestrator utama. AI Factory adalah engine produksi di dalamnya. Quant Factory, Kedaya, dan produk lain tetap berada di repository, runtime, database, credential, dan lifecycle terpisah, tetapi dapat dikendalikan atau dibangun melalui LioBot.

---

## 2. Keputusan yang dikunci

1. LioBot = AI Factory sebagai satu produk. AI Factory adalah engine internal; Chief of Staff/orchestrator, memory, skills, agents, tools, approvals, dan audit adalah bagian dari LioBot.
2. LioBot harus mampu membangun, menguji, mereview, dan memperbaiki bagian dirinya sendiri melalui branch/PR/evaluation/human approval.
3. `telegram-lab` adalah test laboratory/acceptance target saja, bukan produk LioBot final dan bukan roadmap produk.
4. Quant Factory harus tetap produk terpisah dan tidak boleh berbagi repository, database, workspace, volume, atau credential dengan LioBot.
4. Semua role saat ini menggunakan model stealth/space-bunny-alpha. Config boleh tetap dipisah per role untuk future routing, tetapi tidak boleh ada silent fallback atau model switch tersembunyi.
5. Merge branch utama dan production deployment tetap human-controlled.
6. AI Factory hanya boleh deploy ke existing registered Coolify application. Tidak boleh provisioning, menghapus resource, atau menghapus volume secara otomatis.
7. PostgreSQL dan workspaces adalah durable state. Upgrade harus additive dan mempertahankan evidence/task lama.
8. Sandbox harus fail closed. Generated code tidak boleh dijalankan langsung di control container.
9. Credential tidak boleh masuk repository, prompt artifact, task workspace, sandbox, command argument, atau public endpoint.
10. Telegram private chat dengan numeric allowlist adalah operator channel utama. HTTP API optional dan bearer-protected.
11. Source, task, evidence, policy, dan credential antarproject harus terisolasi.

---

## 3. Baseline V0.2

### 3.1 Kapabilitas yang sudah ada

- PostgreSQL durable queue dan additive migration.
- Task events, plan, diff, tests, review, gates, deployment records.
- Atomic claim, worker lease, heartbeat, bounded recovery, per-repository serialization.
- Idempotency, cancellation, retry, feedback terhadap PR yang sama, lifetime budgets.
- Project registry melalui PROJECTS_JSON.
- Lead → Developer → isolated tests → independent Reviewer → deterministic gates → PR.
- High-risk approval berdasarkan plan hash.
- Clarification, answer, approve, retry, feedback, cancel.
- Git branch/commit/push/PR dengan source digest dan base SHA verification.
- Bubblewrap sandbox terpisah, namespace isolation, non-root, capability drop, read-only root, resource limits.
- No Docker socket, no control secrets, no host checkout mount.
- Telegram allowlist, private-chat enforcement, task ownership.
- Optional bearer-protected operator API.
- /health dan /ready.
- Explicit commit-bound Coolify deployment setelah merged SHA, matching tree, CI checks, registered target, dan operator command.
- Compose services: workspace-init, postgres, sandbox, ai-factory.
- Persistent PostgreSQL dan workspace volumes.

### 3.2 Gap yang harus ditutup

Task #7 membuktikan tiga gap:

- Reviewer tidak menerima baseline provenance cukup untuk membuktikan bahwa perilaku belum diuji di main.
- Full suite dijalankan, tetapi test baru tidak selalu dijalankan secara standalone.
- Reviewer meminta metadata PR sebelum PR dibuat; pre-publication gate dan post-publication evidence belum dipisahkan.

Requirement P0: evidence yang dapat dibuktikan oleh engine harus dihasilkan engine secara deterministic, bukan diasumsikan oleh model.

---

## 4. Tujuan dan non-goals

### 4.1 Tujuan

1. Mengubah objective manusia menjadi pekerjaan yang terencana, terukur, dan dapat direview.
2. Memungkinkan satu operator mengelola beberapa produk tanpa memberi AI akses produksi berlebihan.
3. Membuktikan bahwa AI Factory dapat membangun LioBot end-to-end dari satu requirement.
4. Menyediakan audit trail lengkap: input, policy, model, tool, evidence, approval, commit, PR, deployment.
5. Menambahkan skill baru tanpa menulis ulang queue, budget, approval, evidence, dan security layer.
6. Menahan tindakan irreversible sampai ada approval manusia.

### 4.2 Non-goals

- Autonomous merge ke main.
- Autonomous production deployment.
- Provisioning atau penghapusan Coolify resource.
- Menjalankan generated code di control container.
- Akses Docker socket, host filesystem, production DB, atau production credential.
- Menjadi marketplace atau runtime untuk semua produk target.
- Menjamin live model quality hanya berdasarkan test lokal.
- Arbitrary shell, binary asset, empty repository, atau monorepo-scale work tanpa perluasan policy yang direview.
- Pembayaran, transfer dana, pengiriman pesan eksternal, penghapusan massal, atau tax filing tanpa approval.

---

## 5. Arsitektur target

~~~text
Telegram / Operator API
        |
        v
Control Plane (FastAPI)
  auth, intake, policy, state machine, budget, approvals
        |
        +-- PostgreSQL
        |     tasks, leases, events, artifacts, approvals, deployments
        |
        +-- Worker Pool
        |     Lead, Planner, Developer, Specialist, Reviewer
        |
        +-- LLM Gateway
        |     Bunny Alpha, structured output, retries, usage/cost
        |
        +-- Workspace Manager
        |     isolated checkout, bounded snapshot, source digest
        |
        +-- Sandbox Service
        |     build/test commands, no credentials, no host mounts
        |
        +-- Integration Adapters
        |     GitHub, Coolify, Telegram, future CRM/storage/email
        |
        +-- Evidence/Audit Layer
              report, redaction, hashes, export
~~~

### 5.1 Control plane

Control plane mengelola authentication, project resolution, task state, worker leases, LLM gateway, sandbox requests, evidence, gates, GitHub, Coolify, dan Telegram/API. Control plane tidak mengeksekusi target code.

### 5.2 Worker

Worker harus disposable dan restart-safe:

- state penting disimpan di PostgreSQL/workspace;
- claim atomic dan lease fenced;
- heartbeat selama model, test, dan external API call;
- recovery bounded;
- satu task aktif per repository secara default;
- retry mempertahankan lifetime budget;
- lost response direconcile;
- cancellation cooperative;
- error dikategorikan dan di-redact.

---

## 6. Model dan role

### 6.1 Policy

~~~dotenv
LEAD_MODEL=stealth/space-bunny-alpha
DEVELOPER_MODEL=stealth/space-bunny-alpha
REVIEWER_MODEL=stealth/space-bunny-alpha
~~~

Rules:

- Tidak ada silent fallback.
- Status/report menampilkan model dan role tanpa API key.
- Catat model, role, timeout, output limit, usage, dan cost.
- Cost kosong dari provider ditandai partial; call/token/time limit tetap aktif.
- Provider credit limit digunakan sebagai hard spending control.
- Response divalidasi lokal dengan Pydantic sebelum dipakai.
- Raw response tidak disimpan jika berpotensi memuat secret/input sensitif.
- Retry menyebut error category/field, tidak menyalin raw output.

### 6.2 Lead

Input: requirement, policy, repository context. Output: LeadPlan.

Wajib menghasilkan:

- objective;
- acceptance criteria observable dan bernomor;
- constraints;
- suggested tests;
- implementation steps;
- risks;
- blocking questions saja;
- risk level;
- deployment_required;
- persistence_required.

Lead tidak menulis source dan tidak menjalankan command.

### 6.3 Team Planner

Capability P1. Input: LeadPlan, skill registry, project policy. Output: execution graph.

Wajib menentukan:

- role dan skill;
- urutan/dependency;
- input/output stage;
- approval gates;
- evidence wajib;
- budget per stage.

### 6.4 Developer/Specialist

Developer action schema tetap bounded:

~~~json
{"action":"list_files"}
{"action":"read_file","path":"README.md"}
{"action":"search","content":"handler"}
{"action":"write_file","path":"tests/test_regression.py","content":"..."}
{"action":"replace_text","path":"app.py","old_text":"...","content":"..."}
{"action":"run_command","command":"python -m pytest -q"}
{"action":"git_diff"}
{"action":"finish","note":"..."}
~~~

Rules:

- flat JSON object;
- exact field names;
- no tool_calls/arguments wrapper;
- no action array;
- inspect before edit;
- no credential access;
- no arbitrary shell beyond allowlist;
- no push/deploy from sandbox;
- finish hanya setelah complete diff diperiksa.

### 6.5 Independent Reviewer

Reviewer menerima requirement, plan, baseline provenance, complete diff, deterministic test evidence, standalone test evidence, source digest, hygiene issues, dan feedback. Reviewer harus mengembalikan satu evidence entry per criterion. Reviewer tidak boleh override deterministic gate.

---

## 7. Skill system

Skill adalah modul capability dengan kontrak, bukan sekadar prompt.

Minimum manifest:

~~~yaml
id: engineering.regression_test
version: 1.0.0
domain: engineering
description: Add one verified regression test without changing production behavior
inputs:
  - requirement
  - repository_snapshot
  - acceptance_criteria
outputs:
  - diff
  - test_evidence
  - review_evidence
allowed_tools:
  - repository.read
  - repository.search
  - repository.write_test
  - sandbox.run_tests
forbidden_tools:
  - production.database.write
  - payment.execute
  - deploy.production
approval_policy: low_risk_test_only
budget:
  max_steps: 30
  max_calls: 30
evidence:
  required:
    - baseline_review
    - standalone_test
    - full_suite
    - diff_scope
~~~

Setiap skill wajib memiliki:

- unique ID dan semantic version;
- owner/domain;
- input/output schemas;
- allowed/forbidden tools;
- data classification;
- risk level dan approval policy;
- deterministic validators;
- evidence contract;
- budget;
- failure/retry policy;
- unit/integration tests;
- changelog dan compatibility notes.

Skill registry hanya dapat diubah oleh operator atau reviewed PR di AI Factory. Skill tidak boleh mendaftarkan credential sendiri atau mengubah global policy.

### 7.1 Skill awal

Engineering:

- repository reconnaissance;
- requirement decomposition;
- architecture/design;
- implementation;
- regression test;
- full test;
- security review;
- Docker/Compose preparation;
- deployment documentation;
- PR preparation;
- release verification.

Product/research:

- problem framing;
- research dengan source citation;
- PRD/specification;
- acceptance criteria;
- experiment plan;
- decision memo.

Marketing:

- positioning/persona;
- offer/funnel;
- copy variants;
- content calendar;
- campaign brief;
- performance report;
- experiment recommendation.

Admin/Ops:

- SOP extraction;
- checklist;
- recurring task plan;
- data cleanup proposal;
- report;
- reconciliation preparation;
- escalation detection.

Finance:

- cash-flow classification;
- budget/forecast;
- variance analysis;
- invoice preparation;
- management report.

Finance skill tidak boleh mengeksekusi payment, transfer, tax filing, atau bank action.

Sales/CS:

- lead classification;
- response draft;
- follow-up queue;
- product/FAQ answer dari approved catalog;
- escalation;
- scoped CRM update.

---

## 8. Task lifecycle

### 8.1 States

~~~text
received
→ planning
→ waiting_input (optional)
→ awaiting_approval (optional)
→ developing
→ testing
→ reviewing
→ publishing
→ pr_created
→ deployment_pending (optional)
→ deploying
→ deployed / deployment_unknown / deployment_failed
→ completed / failed / cancelled
~~~

### 8.2 Rules

- received → planning hanya jika alias dan policy valid.
- planning → waiting_input untuk blocking question.
- planning → awaiting_approval untuk high-risk plan.
- developing → testing setelah Developer finish dan diff nonempty.
- testing → reviewing setelah mandatory test report tersimpan.
- reviewing → developing jika rejected dan budget masih tersedia.
- reviewing → publishing hanya jika Reviewer approved dan deterministic gates zero issue.
- publishing → pr_created setelah commit, push, dan PR reconciliation.
- pr_created → deploying hanya dengan command manusia dan merged full SHA.
- deployment_unknown tidak auto-retry.
- terminal state tidak berubah tanpa event baru.

### 8.3 Idempotency

- Duplicate Telegram update tidak membuat duplicate task.
- idempotency_key mengembalikan task existing untuk request yang sama.
- Lost response setelah push/PR direconcile.
- Lost deployment response menjadi unknown.

---

## 9. Evidence dan review integrity

### 9.1 Evidence per iteration

Wajib disimpan:

- plan dan plan hash;
- baseline source context, branch, base SHA;
- file inventory;
- complete diff termasuk untracked files;
- source digest sebelum test;
- command, exit code, timeout, output ringkas;
- standalone test command/result untuk test baru;
- full suite command/result;
- lint/format/Compose checks bila relevan;
- source digest sesudah test;
- hygiene/security issues;
- independent review JSON;
- gate result;
- commit SHA;
- PR body/URL bila publish;
- deployment evidence bila explicit deploy.

### 9.2 P0 fix Task #7

1. Baseline provenance otomatis: simpan file/context yang benar-benar diperiksa dari base branch, termasuk test files relevan.
2. Standalone test evidence: identifikasi test baru dan jalankan node test secara terpisah bila dapat ditentukan.
3. Review contract: reviewer menilai pre-publication evidence sebelum PR dibuat. Criterion tentang PR body/URL diverifikasi setelah publication, bukan dipakai sebagai alasan untuk menolak pre-publication code.

### 9.3 Deterministic gates

Model tidak boleh override:

- test absent, failed, atau timeout;
- pytest no-tests;
- source berubah saat test;
- diff kosong/terlalu besar;
- secret, credential, runtime DB;
- traversal/symlink/Git internal escape;
- acceptance criterion tanpa evidence;
- untracked file hilang dari diff;
- deployment files kurang ketika deployment diminta;
- base branch moved;
- branch/PR tree mismatch;
- CI failure;
- unauthorized repository/project;
- expired lease atau cancelled task.

---

## 10. Project registry dan isolation

Contoh policy:

~~~json
{
  "lab": {
    "repo": "degitalintelligence/telegram-lab",
    "base_branch": "main",
    "profile": "python",
    "require_deployment": true,
    "install_dependencies": true,
    "coolify_uuid": ""
  }
}
~~~

Registry operator-managed dan policy disnapshot pada task. Future fields:

- approved test/build commands;
- deployment target/environment;
- data classification;
- allowed domains;
- required reviewers;
- budget/timeout;
- production action policy;
- owner/team.

Isolation:

- workspace task-scoped;
- repo hanya dari policy, bukan model;
- GitHub token scoped;
- revoked/changed policy menghentikan pending task incompatible;
- target source tidak dibaca melalui project lain;
- LioBot/Quant Factory/Kedaya punya repo/deployment boundary masing-masing.

---

## 11. LioBot end-to-end requirement

Ini acceptance target utama untuk membuktikan AI Factory sebagai engine.

### 11.1 Scope

AI Factory menyiapkan LioBot di repository target yang terdaftar, bukan mengedit AI Factory sebagai target application.

Wajib:

- mempertahankan fitur existing termasuk /hello dan /todo;
- Dockerfile production-ready dan non-root;
- docker-compose.yaml untuk app dan persistence yang diperlukan;
- .env.example tanpa secret;
- named volume untuk todo/user state;
- healthcheck/readiness;
- restart policy;
- user isolation;
- persistence setelah restart/recreation;
- invalid input/error behavior;
- offline tests dengan Telegram boundary mock;
- PostgreSQL integration test bila memakai PostgreSQL;
- docs/DEPLOYMENT.md berisi environment, startup, health, backup, restore, migration, logs, rollback;
- tidak ada runtime DB, token, .env, credential, atau generated secret di Git;
- PR hanya setelah semua test, review, dan hygiene gate lulus.

### 11.2 Acceptance criteria

1. /hello tetap mengirim response yang disepakati.
2. /todo add <text> membuat todo milik pengirim.
3. /todo atau /todo list hanya menampilkan todo pengirim.
4. User A tidak dapat membaca/mengubah todo User B.
5. Data bertahan setelah process restart dan container recreation dengan volume sama.
6. Empty/invalid input menghasilkan validasi tanpa crash.
7. Unit test tidak memanggil Telegram API nyata.
8. Full test dan persistence/restart test lulus.
9. Image berjalan non-root dan hanya expose port diperlukan.
10. Compose healthcheck menunggu dependency benar.
11. Backup/restore/rollback docs dapat diikuti operator lain.
12. PR body menyebut behavior, test command, hasil, limitation, deployment notes.

Example request:

~~~text
/new lab | Siapkan LioBot end-to-end. Pertahankan semua fitur existing. Pastikan /hello dan /todo berjalan. Tambahkan persistence setelah restart/recreation, user isolation, invalid input handling, Dockerfile non-root, docker-compose.yaml, named volume, healthcheck, .env.example, test offline dengan mock Telegram, persistence test, dan docs/DEPLOYMENT.md berisi environment, backup, restore, migration, health, log, dan rollback. Jangan commit token, database runtime, credential, atau secret. Buat PR hanya jika seluruh test dan review gate lulus.
~~~

---

## 12. Telegram dan HTTP API

### 12.1 Telegram commands

| Command | Fungsi |
|---|---|
| /start | Help/status; unauthorized tetap ditolak |
| /new <requirement> | Task pada project default lab |
| /new <alias> \| <requirement> | Task pada project tertentu |
| /projects | Registry yang boleh digunakan |
| /tasks | Task operator |
| /status <id> | Status, iteration, calls, tokens, cost, branch, PR |
| /plan <id> | Plan dan hash |
| /logs <id> | Event dan diagnostics |
| /report <id> | Evidence lengkap |
| /answer <id> <text> | Menjawab clarification |
| /approve <id> <hash> | Approve exact high-risk plan |
| /retry <id> | Retry dengan lifetime budget retained |
| /feedback <id> <revision> | Rework pada PR yang sama |
| /cancel <id> | Cooperative cancel |
| /deploy <id> <full SHA> | Explicit commit-bound deployment |
| /deployment <id> | Reconcile deployment |

Security:

- private chat only;
- numeric allowlist;
- fail closed bila token ada tapi allowlist kosong;
- task ownership check;
- group messages ditolak;
- unauthorized response tidak bocorkan detail;
- duplicate update deduplication.

Telegram adalah adapter, bukan domain logic. Future Slack/WhatsApp/Web adapter harus memakai core TaskRequest/TaskEvent yang sama.

### 12.2 HTTP API

Jika API_TOKEN aktif, semua endpoint wajib Bearer auth. Token kosong berarti fail closed. Jangan taruh token di URL/query/log. Tambahkan rate limit dan body limit sebelum public exposure.

Minimum:

~~~text
GET  /health
GET  /ready
GET  /tasks
POST /tasks
GET  /tasks/{id}
GET  /tasks/{id}/events
GET  /tasks/{id}/artifacts
POST /tasks/{id}/cancel
POST /tasks/{id}/retry
POST /tasks/{id}/answer
POST /tasks/{id}/approve
POST /tasks/{id}/feedback
~~~

---

## 13. Security

### 13.1 Sandbox

Wajib:

- Bubblewrap user namespace explicit;
- filesystem/PID/network/IPC isolation;
- cleared child environment;
- no host checkout, Git metadata, Docker socket, or control credential;
- non-root;
- cap_drop ALL;
- read-only root dan bounded tmpfs;
- pids/CPU/memory/time limits;
- network off saat tests;
- network hanya optional saat dependency setup;
- wheel-only pip dan npm install scripts disabled;
- scoped AppArmor profile ai-factory-sandbox;
- systempaths unconfined hanya pada sandbox runner;
- no privileged;
- fail closed bila isolation gagal;
- no direct-host fallback.

### 13.2 Source/file safety

- reject traversal, symlink escape, Git internal path, env/credential path;
- bounded UTF-8 snapshot;
- file count/size/context/diff limits;
- no binary tanpa design khusus;
- generated database, secret, token, private key, .env block publication;
- log redaction defense-in-depth;
- no automatic reset --hard, force push, workspace deletion, volume deletion.

---

## 14. Data and persistence

Minimum logical entities:

- tasks: requirement, project, policy snapshot, owner, status, branch, base/head SHA, budget, iteration, PR/deployment refs;
- task_events: lifecycle/tool/rejection/error;
- task_artifacts: plan, baseline, diff, tests, standalone_tests, review, gates, PR/deployment evidence;
- deployments: target UUID, requested SHA, status, remote ID, reported SHA, ambiguity;
- skills: ID/version/policy/evidence contract;
- approvals: approver, hash, time, scope, expiry;
- budgets: stage/provider reservations and actual usage.

Rules:

- PostgreSQL required in production/multi-worker;
- SQLite local only;
- migrations additive/idempotent;
- V0.1 rows retained;
- no database reset on upgrade;
- artifact redaction and size bound;
- retention explicit and operator-approved;
- backup Postgres before upgrade/daily where applicable;
- workspace backup separate.

---

## 15. Budget dan resource

Current defaults:

~~~dotenv
MAX_ITERATIONS=4
MAX_DEV_STEPS=60
MAX_LLM_CALLS=150
MAX_TOTAL_TOKENS=600000
MAX_COST_USD=5.0
MAX_OUTPUT_TOKENS=8192
LLM_TIMEOUT_SECONDS=120
TASK_TIMEOUT_SECONDS=3600
WORKER_CONCURRENCY=1
LEASE_SECONDS=90
MAX_RECOVERIES=2
~~~

Requirements:

- reserve call before provider request;
- record actual usage;
- limits checked before next call;
- retries consume budget;
- missing provider cost is partial, not unlimited;
- one-call overshoot documented;
- sandbox resource limit;
- one active task per repo by default;
- retry does not reset budget;
- repeated failure should recommend smaller task or policy change;
- future usage dashboard per task/project/role/model/period.

---

## 16. GitHub workflow

Before coding:

- resolve repo from policy;
- capture base branch/current SHA;
- create deterministic task branch;
- capture baseline context and source digest;
- inspect existing relevant tests;
- preserve unrelated user worktree changes.

Before PR:

- complete diff includes untracked files;
- standalone and full tests pass;
- source digest unchanged during tests;
- reviewer approved each criterion;
- gates zero issue;
- commit and digest stored;
- remote branch equals approved SHA;
- create/reconcile exactly one PR;
- PR body includes requirement, plan, evidence, tests, review, digest, commit, limitations.

Merge remains human-controlled.

Feedback must verify the existing PR is open, same repo/task, create a new reviewed commit on same branch, and never create duplicate PR.

---

## 17. Coolify deployment

Preconditions:

- explicit /deploy task full merged SHA;
- PR merged;
- SHA is full 40-character SHA;
- merged tree equals reviewed tree;
- successful existing GitHub checks;
- policy has registered coolify_uuid;
- repository/branch matches policy;
- secrets already exist in Coolify;
- no resource/volume recreation.

Behavior:

- pin exact commit;
- submit at most once;
- store remote ID/status;
- reconcile unknown manually;
- verify reported commit;
- do not infer business acceptance from Coolify finished;
- run post-deploy smoke test;
- stateful rollback requires human compatibility assessment.

Existing deployment constraints:

- use same Coolify Compose resource;
- preserve normalized PostgreSQL/workspace volumes;
- preserve passwords and existing credential;
- no public PostgreSQL/sandbox port;
- sandbox internal only;
- app internal port 8080 is sufficient.

---

## 18. Observability

/health means process alive. /ready must validate PostgreSQL, worker heartbeat, Telegram updater if enabled, sandbox health, and no fatal configuration error.

Logs must include task ID, project/repo, stage/iteration, event type, duration, exit code/timeout, role/model, redacted error category.

Logs must not include API keys, bot token, database password, authorization headers, raw secret-bearing output, or unnecessary private source.

Future metrics:

- throughput/latency per stage;
- rejection reasons;
- structured-output failure by model/role;
- test failure;
- PR rate;
- deployment success/unknown/failure;
- recovery;
- token/cost;
- queue age;
- sandbox resource usage.

---

## 19. Test requirements

### 19.1 Unit

- schemas/action validation;
- structured-output parsing/retry/privacy;
- policy validation;
- command routing;
- allowlist/ownership;
- budget;
- gates/secret detection;
- path/symlink safety;
- deployment preconditions;
- idempotency.

### 19.2 Integration

- PostgreSQL migrations twice;
- concurrent claims;
- stale lease fencing;
- recovery;
- repository serialization;
- full orchestrator with mocked provider/GitHub/Coolify;
- lost push/PR response reconciliation;
- feedback same PR;
- deployment unknown reconciliation.

### 19.3 Real sandbox CI

- build runner;
- AppArmor profile;
- namespace isolation/private procfs;
- no control environment;
- offline passing/failing pytest;
- artifact/source hygiene;
- resource/timeout behavior.

### 19.4 Live staging

1. /ready healthy.
2. Unauthorized Telegram denied.
3. Small task creates one targeted PR.
4. Duplicate message does not duplicate task/PR.
5. Restart during task recovers boundedly.
6. Failing test prevents PR even if model approves.
7. /feedback updates same PR.
8. LioBot persistence survives restart/recreation.
9. Explicit staging deployment pins requested SHA.
10. User-visible smoke test passes.

---

## 20. Roadmap

### P0 — Evidence and review correctness

Deliver:

- baseline provenance;
- standalone new-test execution;
- pre-PR vs post-PR evidence contract;
- review context fixes;
- report sections per iteration;
- live Bunny Alpha validation;
- regression tests for Task #7 failure mode.

Exit:

- one real small task reaches one PR;
- report proves baseline, standalone test, full suite, diff, review, gates;
- no duplicate PR;
- failures actionable without raw-output leak.

### P1 — Skill registry and Team Planner

Deliver:

- versioned skill manifest;
- input/output schemas;
- tool policy;
- Team Planner;
- execution DAG;
- budget allocation;
- reusable evidence contract;
- skill registry tests.

Exit:

- existing Engineering flow remains compatible;
- skill cannot acquire unregistered tool/credential;
- operator can inspect team/skills;
- old tasks remain readable.

### P2 — Domain skills

Deliver one domain at a time: Product/Research, Marketing, Admin/Ops, Finance, Sales/CS.

Each domain requires a real staging task, least-privilege connector, deterministic evidence, and human approval before real external action.

### P3 — Operator console and scale

- web dashboard;
- timeline/artifact viewer;
- approval queue;
- usage/cost dashboard;
- registry UI;
- multi-worker scale;
- Slack/WhatsApp/Web adapters.

Dashboard may improve visibility but cannot bypass core gates.

---

## 21. Developer execution sequence

### PR-A — Close Task #7 evidence gap

1. Reproduce Task #7 in disposable workspace.
2. Add baseline provenance artifact.
3. Add standalone test node detection/execution.
4. Separate pre-publication review criteria from post-publication PR evidence.
5. Add deterministic tests for all three.
6. Run full suite, PostgreSQL integration, lint/format, real sandbox CI.
7. Open PR only after gates.

### PR-B — LioBot proof

1. Submit LioBot requirement through Telegram/API.
2. Verify target repo, branch, and policy.
3. Let AI Factory change only telegram-lab.
4. Verify /hello, /todo, persistence, isolation, invalid input.
5. Verify Docker/Compose/env/health/docs generated by AI Factory.
6. Review CI and PR.
7. Human merge.
8. Stage deploy with exact merged SHA.
9. Run post-deploy smoke and record evidence.

### PR-C — Skill registry

1. Add schemas and migration.
2. Add operator-controlled registry.
3. Build Team Planner using existing LLM gateway/gates.
4. Preserve task compatibility.
5. Migrate Engineering skill first.

### PR-D onward — Domain skills

One domain per PR, with staging acceptance and explicit approval path.

---

## 22. Configuration reference

Required:

~~~dotenv
DATABASE_HOST=postgres
DATABASE_PASSWORD=<Coolify secret>
OPENROUTER_API_KEY=<Coolify secret>
GITHUB_TOKEN=<scoped secret>
TELEGRAM_BOT_TOKEN=<Coolify secret>
TELEGRAM_ALLOWED_USER_IDS=<numeric IDs>
SANDBOX_TOKEN=<independent random secret>
SANDBOX_APPARMOR_PROFILE=ai-factory-sandbox
LEAD_MODEL=stealth/space-bunny-alpha
DEVELOPER_MODEL=stealth/space-bunny-alpha
REVIEWER_MODEL=stealth/space-bunny-alpha
PROJECTS_JSON=<operator-managed JSON>
~~~

Optional:

~~~dotenv
API_TOKEN=<separate operator API secret>
COOLIFY_URL=<Coolify URL>
COOLIFY_TOKEN=<scoped Coolify token>
SANDBOX_INSTALL_DEPS=false
WORKER_CONCURRENCY=1
~~~

Never put secrets in Git, task requirements, fixtures, PR body, report, or Docker build args.

---

## 23. Definition of Done

A milestone is done only when:

- code is in a reviewed PR;
- tests are reproducible and actual results recorded;
- CI passes;
- security and deterministic gates pass;
- migrations/backward compatibility verified;
- docs updated;
- no secret/runtime artifact committed;
- target repository boundary verified;
- human merge decision explicit;
- staging deployment, if requested, uses exact merged SHA;
- post-deploy smoke recorded;
- rollback path documented;
- limitations visible in report;
- model claim is never the only evidence.

Success is not the number of generated PRs. Success is the percentage of tasks reaching a correct, reviewable, reproducible outcome with minimal human intervention while preserving human control over irreversible actions.


---

## 24. Normative correction: LioBot self-building dan komunikasi Dedi

Bagian ini adalah aturan terbaru dan mengesampingkan bagian sebelumnya jika ada konflik.

### 24.1 Identitas produk

- Nama produk yang Dedi gunakan: **LioBot**.
- Nama repository/engine teknis saat ini: **ai-factory**.
- LioBot adalah AI Factory yang utuh, bukan aplikasi target di luar AI Factory.
- Chief of Staff/orchestrator berada di dalam LioBot.
- 'telegram-lab' adalah test laboratory/acceptance target saja, bukan produk LioBot final dan bukan roadmap produk.
- Telegram yang sedang dipakai untuk /new, /status, /logs, dan /retry adalah interface testing/operator untuk AI Factory V0.2. Itu bukan definisi final interface LioBot.

### 24.2 LioBot harus mampu membangun dirinya sendiri

Self-building tidak berarti LioBot boleh mengubah production secara liar. Self-building berarti LioBot dapat menemukan gap, mengusulkan perubahan, mengerjakan perubahan melalui AI Factory internal, menguji perubahan, dan meminta approval Dedi sebelum perubahan dipromosikan.

Loop wajib:

~~~text
Observe → Detect gap → Diagnose → Propose improvement
→ Create self-improvement task → Implement in isolated branch
→ Run evaluation/regression/security tests → Independent review
→ Show Dedi decision card → Approve/Reject
→ Merge/deploy only after explicit human control
→ Measure outcome → Retain or rollback
~~~

Contoh gap yang harus dapat dideteksi:

- model sering mengembalikan structured output invalid;
- reviewer kekurangan evidence;
- task berulang kali gagal pada jenis requirement tertentu;
- tool tidak tersedia untuk pekerjaan yang sering diminta;
- skill menghasilkan output yang tidak konsisten;
- biaya atau waktu meningkat tanpa peningkatan hasil;
- Dedi berulang kali mengoreksi pola yang sama;
- LioBot tidak mengetahui data penting atau sumber data sudah stale.

### 24.3 Self-improvement yang diizinkan

LioBot boleh mengusulkan dan membangun:

- perubahan prompt/role contract;
- skill baru atau versi skill;
- evaluator dan regression test baru;
- parser/structured-output handling;
- connector dan adapter baru;
- UI/dashboard/Telegram presentation;
- documentation dan runbook;
- memory indexing/retrieval improvement;
- observability dan reporting;
- non-sensitive workflow optimization.

LioBot tidak boleh mempromosikan sendiri tanpa approval:

- perubahan allowlist atau permission;
- perubahan sandbox boundary;
- pemberian credential baru;
- perubahan budget hard limit;
- bypass review/approval gate;
- perubahan deployment target;
- penghapusan evidence, database, volume, atau workspace;
- pengiriman pesan eksternal;
- pembayaran/transfer;
- perubahan production data yang irreversible;
- merge ke main atau production deployment.

### 24.4 Self-improvement evidence gate

Setiap self-improvement task wajib menghasilkan:

- gap yang diamati;
- bukti task/event/feedback yang memicu gap;
- hypothesis perubahan;
- expected improvement metric;
- affected roles/skills/projects;
- risk classification;
- before/after evaluation;
- regression suite result;
- security and permission diff;
- cost/time impact;
- rollback plan;
- exact commit/PR;
- decision request untuk Dedi.

Improvement hanya boleh dipromosikan jika:

- evaluation benchmark tidak menurun;
- regression suite lulus;
- tidak ada deterministic gate issue;
- permission boundary tidak melebar secara implisit;
- Dedi menyetujui scope yang tepat;
- hasil setelah release dapat diukur.

Memory tidak boleh menjadi “learning” hanya karena model mengucapkan sesuatu. Learning harus berasal dari source/evidence, explicit Dedi feedback, approved decision, atau evaluation result.

### 24.5 Context dan memory plane

Karena LioBot adalah produk utama, LioBot harus memiliki Context/Memory Plane terpusat namun permission-aware.

Memory domain minimum:

- identity dan preference Dedi;
- company/business context;
- goals dan strategy;
- project/task state;
- SOP dan policy;
- approved decisions;
- metrics dan reports;
- agent outputs dan evidence;
- failures, feedback, and lessons;
- connector metadata dan freshness.

Setiap memory item wajib memiliki:

- source;
- owner/business scope;
- created_at/updated_at;
- freshness/expiry;
- confidence;
- sensitivity/classification;
- provenance link;
- version;
- correction/retraction path.

LioBot tidak boleh memasukkan semua data mentah ke prompt. LioBot harus menggunakan retrieval/context gateway yang:

- memilih konteks relevan;
- menghormati project/business permission;
- memberi provenance;
- menandai data stale/conflicting;
- menghindari credential dan data yang tidak relevan;
- mendukung Dedi mengoreksi atau mencabut memory.

Specialist agent hanya menerima context slice yang diperlukan untuk tugasnya, bukan seluruh memory Dedi.

### 24.6 Komunikasi LioBot dengan Dedi

Prinsip utama: Dedi tidak boleh dipaksa membaca log teknis untuk mengetahui keadaan sistem.

Semua komunikasi penting harus diringkas menjadi salah satu jenis:

- **Update:** apa yang sedang dilakukan;
- **Need information:** informasi apa yang kurang;
- **Decision required:** keputusan apa yang harus dibuat;
- **Approval required:** tindakan apa yang menunggu approval;
- **Warning:** risiko, konflik, atau batasan;
- **Blocked:** mengapa pekerjaan berhenti;
- **Completed:** hasil, evidence, dan next step;
- **Learning proposal:** perubahan terhadap diri LioBot yang diusulkan.

LioBot wajib proaktif menyatakan:

1. Apa yang sedang dikerjakan.
2. Mengapa pekerjaan itu dikerjakan.
3. Apa yang sudah selesai.
4. Apa yang masih kurang atau berlubang.
5. Risiko dan dampaknya.
6. Apa pilihan yang tersedia.
7. Rekomendasi LioBot.
8. Apa yang harus Dedi approve, reject, jawab, atau tunda.
9. Apa yang terjadi setelah keputusan Dedi.

### 24.7 Decision Request contract

Setiap hal yang memerlukan keputusan Dedi harus dibuat sebagai Decision Request terstruktur:

~~~yaml
id: decision-123
type: approval | rejection | clarification | escalation | learning_proposal
title: Short decision title
summary: What happened and why it matters
context:
  project: lab
  task_id: 7
  affected_scope: ...
gap_or_risk:
  - ...
options:
  - id: A
    label: ...
    impact: ...
recommendation:
  option: A
  reason: ...
cost: ...
risk: low | medium | high
deadline: ...
evidence:
  - artifact_id: ...
actions:
  approve: ...
  reject: ...
  ask_question: ...
  defer: ...
expires_at: ...
~~~

Dedi harus dapat menjawab dengan bahasa natural, misalnya:

- “Approve pilihan A.”
- “Tolak, jangan deploy.”
- “Tanya developer kenapa test ini gagal.”
- “Lanjutkan tapi jangan ubah production.”
- “Simpan sebagai keputusan untuk RTB.”

LioBot harus mengubah jawaban itu menjadi action yang exact, scoped, idempotent, dan tercatat.

### 24.8 Decision inbox

LioBot harus memiliki satu inbox lintas channel:

- Needs your decision;
- Blocked;
- Risks/warnings;
- Completed;
- Learning proposals;
- Recent activity.

Telegram native chat dan dashboard hanyalah dua adapter dari inbox yang sama. Status, evidence, approval, dan rejection tidak boleh berbeda antar-channel.

Dashboard minimum:

- ringkasan hari ini;
- keputusan yang menunggu;
- pekerjaan yang blocked;
- pekerjaan yang selesai;
- learning/self-improvement proposals;
- filter business/project/agent;
- evidence dan diff yang dapat dibuka;
- action buttons Approve, Reject, Ask, Defer.

Telegram minimum:

- natural language;
- inline approve/reject/defer/ask;
- concise progress;
- deep link ke evidence/dashboard;
- command fallback tetap tersedia untuk operasi teknis.

### 24.9 Prioritas UX LioBot

Prioritas bukan jumlah command, tetapi **decision clarity**.

Dalam satu pesan/card, Dedi harus bisa menjawab:

~~~text
Apa yang terjadi?
Kenapa saya perlu tahu?
Apa yang kurang?
Apa risikonya?
Apa rekomendasinya?
Saya harus klik/jawab apa?
~~~

Jika tidak ada tindakan dari Dedi, LioBot harus menyatakan “tidak ada tindakan yang diperlukan”. Jika ada tindakan, hanya tampilkan tindakan yang benar-benar diperlukan.

### 24.10 Acceptance criteria LioBot sebagai AI Factory

1. LioBot dapat menerima objective natural language.
2. LioBot dapat memilih skill/agent/factory yang relevan.
3. LioBot dapat mengambil context lintas project sesuai permission.
4. LioBot dapat menyatakan data/context yang missing sebelum pekerjaan dimulai.
5. LioBot dapat membuat task untuk memperbaiki dirinya sendiri.
6. Self-improvement berjalan melalui isolated branch, tests, evaluation, review, dan approval.
7. LioBot tidak dapat mengubah permission, credential, guardrail, merge, atau production tanpa approval.
8. LioBot menghasilkan Decision Request yang jelas dan actionable.
9. Dedi dapat approve/reject/ask/defer melalui natural language atau dashboard.
10. Telegram dan dashboard membaca state/inbox/evidence yang sama.
11. Dedi tidak perlu membaca raw worker/model logs untuk mengambil keputusan.
12. Setiap learning/improvement mempunyai source, evaluation, version, dan rollback path.
13. Failed self-improvement tidak merusak runtime LioBot.
14. 'telegram-lab' tetap diperlakukan sebagai test harness, bukan LioBot final.

### 24.11 Urutan pembangunan setelah requirement ini

1. Pertahankan AI Factory V0.2 sebagai core execution engine.
2. Ubah terminology dan documentation: LioBot adalah product; AI Factory adalah internal engine.
3. Tambahkan Context/Memory Plane dan permission-aware retrieval.
4. Tambahkan Decision Request dan Decision Inbox.
5. Tambahkan self-improvement task type dan evaluation registry.
6. Tambahkan self-building loop dengan approval gate.
7. Baru setelah itu buat dashboard/native chat adapter yang nyaman untuk Dedi.
8. Gunakan 'telegram-lab' hanya untuk regression/acceptance testing seluruh loop.

