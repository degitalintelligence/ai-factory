# v0.4 implementation candidate and acceptance

Implementation base: `54061f506d7d6df649d5c5206128335998787e62`.
Version: `0.4.0-rc1`. Dedi authorized implementation; production release remains pending.
The earlier v0.3 operator acceptance is historical evidence, not acceptance of this candidate.

## Implemented behavior

| Plan area | Candidate behavior | Remaining release evidence |
| --- | --- | --- |
| M0 baseline | Authenticated release manifest, configured SHA/model/registry/budget snapshot; outcome denominator separates active/blocked/handoff/open PR tasks | Same-SHA CI and runtime baseline measurements |
| M1 reliability | Atomic clarification question + waiting state, exact-question answer, requirement-version fence; output/review/dependency/final prompt sizing before execution; relevant context slices with required refs retained | Ten bounded live objectives, at least eight accepted with source evidence |
| M2 conversation | Durable owned/project-scoped threads and action receipts; explicit task targeting, ambiguity prompts, status without new DAG, Telegram reply references, draft revision and existing-PR feedback | Live Telegram/dashboard parity, decisions/memory and restart checks |
| M3 self-improvement | Existing proposal/outcome/rollback gates retained; developer distinct read windows count as inspection progress, repeated reads still stop, lifetime limits unchanged | One measured improvement, outcome acceptance and compatible staging rollback |
| M4 product pilot | Existing registry, skill DAG and guarded engineering capability retained | One real product pilot accepted by operator; no pilot outcome fabricated |

Conversation routing is deterministic and bounded. A thread with multiple tasks asks
for a target. Bare “setuju” does not approve, and “lanjut” does not bypass a stop or
reset budgets. Questions are answered explicitly through the clarification endpoint,
`jawab tujuan #ID: ...`, or a scoped reply. A draft revision retains the original and
creates a related analysis intent. No free-form agent/code creation or unregistered
external tool execution is introduced. Threads may be retrieved with
`GET /v1/conversations/{conversation_id}`; the dashboard keeps its selected thread
in page memory and can select a task or start a new thread.

A completed orchestration handoff displays its engineering child's actual status.
A missing/foreign child is unverified, rather than reported as successful. Success
rate is successful terminal tasks divided by successful plus failed terminal tasks;
no terminal denominator returns null. Business acceptance is not inferred from this rate.

## Budget and inspection evidence

`workflow_budget_envelope` records prompts rendered with the same schema helper as
execution, planned input bytes/reservations, mandatory calls, specialist retry slots,
finalization retry headroom and a token heuristic. Unseen outputs use a 7,000-character
ASCII sizing assumption. This is **not a Unicode upper bound, tokenizer, provider
bill or guarantee of completion**. Input byte/4 plus configured output tokens is an
estimate; every actual call still passes the existing full rendered reservation and
actual-usage guards. Daily/task ceilings, provider model, output schema and approvals
are unchanged. Oversized mandatory plans stop before execution approval.

Context selection records selected/omitted refs. Scoped repository audits retain
complete authorized evidence. Required dependency refs survive slicing. Earlier
generated drafts are unverified context, not independent primary evidence.

Developer prompt `developer-v14` allows distinct relevant inspection windows to count
as progress, without forcing an edit in five steps. Unchanged repeated reads remain
bounded; the maximum total steps, time/call/token limits and deterministic review
still apply. This change has behavioral regression coverage; live productivity gain
must be measured before being called an accepted self-improvement.

## Migration and rollback

Startup creates additive `conversations`, `chat_turns`, and `telegram_references`
tables; migration `0005_single_open_clarification` adds a partial unique index for
one open clarification question per task. Existing queue, decisions, events, usage,
artifacts and volumes remain intact. Startup migration is idempotent. Do not remove
or recreate PostgreSQL volumes. Historical duplicate questions can prevent index
creation and must be reconciled explicitly; inspect startup warnings before acceptance.

Rollback staging using the retained previous image, same existing Compose resource,
volume names and database. Stop/drain workers before switching images so no active
lease races two versions. New tables can remain; the old binary ignores them.
Conversation continuity is only offered by v0.4. Capture actual restart/rollback
results and compare persisted task, decision, artifact and usage counts. Schema
additivity is not a substitute for this staging check or an existing backup.

## Exact-candidate staging procedure

1. Verify engine tests including real PostgreSQL concurrency, Docker sandbox smoke,
   Compose and control image build on the candidate SHA. Keep CI links in the report.
2. Stage that image/checkout with `RELEASE_SHA=<full candidate SHA>`, existing approved
   models and registered self-target policy. Preserve current secrets and volumes.
   `GET /v1/release-manifest` is authenticated. `RELEASE_SHA` is operator-configured
   metadata, not cryptographic proof of the deployed image; independently verify it.
3. Supply `FACTORY_URL` (HTTPS), `API_TOKEN` and `FACTORY_PROJECT=self` securely outside
   Git. Inspect first, then explicitly run the frozen read-only ten-objective corpus:

   ```bash
   python scripts/v04_acceptance.py --candidate-sha FULL_SHA --output /tmp/v04-inspection.json
   python scripts/v04_acceptance.py --candidate-sha FULL_SHA --run-objectives --output /tmp/v04-acceptance.json
   ```

   The second command creates real model-consuming tasks under existing limits.
   The collector stops for approval, clarification, failure or timeout. It never
   approves, retries, merges or deploys; remaining cases are retained as not run.
   Review and resolve actual issues, retaining failed attempts and lifetime usage.
4. Independently review claims/evidence for each result. Record `operator_accepted`
   and `operator_review`, with task artifacts, actual calls/tokens/elapsed time,
   schema retries and failure stage from runtime evidence. Require at least eight
   accepted results across all ten frozen cases, on the same candidate.
5. Attach measured evidence to every manual gate: live channel/decision/memory parity,
   restart persistence, measured self-improvement, compatible staging rollback,
   real product pilot and explicit release approval. Fill same-SHA CI evidence.
6. Validate the completed report:

   ```bash
   python scripts/v04_acceptance.py --validate-report /tmp/v04-acceptance.json
   ```

   Incomplete evidence returns nonzero. The report validator checks recorded evidence
   fields; it cannot authenticate an operator's assertions. Store acceptance reports
   securely, outside source control if they contain business context. Only explicit
   operator release authority permits production deployment.

## Evidence and limits of this implementation session

Local verification uses SQLite, mocked model/channel transports and real repository
fixtures. Real PostgreSQL tests execute in CI; sandbox/Compose builds are CI gates.
There are more than twenty conversation scenarios, including owner/tenant/project
isolation, ambiguity, duplicate keys, exact clarification, handoffs, restart and
HTTP/Telegram routing. Mocks do not prove live channel parity.

Unauthenticated runtime health/readiness/manifest probes from this environment
returned HTTP 403. No authorized live credential or staging host was available.
The ten live results, live persistence/rollback, measured outcome and product pilot
are therefore pending. No runtime merge, deployment, budget increase or provider
change was performed. This candidate is reviewable for staging, not fully accepted
v0.4 production readiness and not the complete autonomous cross-domain vision.

## Conversation-first dashboard

The operator dashboard now centers on selecting a task and continuing its scoped
conversation, with recorded state/cause, next action, structured results/evidence,
actual usage, plan and timeline alongside the chat. The task list can be searched
or filtered by attention, active work and available results. Overview exposes only
owned/tenant-scoped conversation references so selecting a task can reopen its history.

Decision/clarification forms are inline dialogs with an exact card target and explicit
submit. Retry on a failed task and PR feedback use existing guarded actions. Suggestions
fill the composer; they do not execute automatically. Approval does not merge/deploy.
Multiple-task ambiguity remains explicit in the conversation. Failure explanations
come from persisted messages/artifacts; no extra model call or invented reasoning is
used. No percentage is guessed. Technical JSON stays in expandable evidence sections.

Lost-response sends reuse the same idempotency key for an unchanged message/target.
Refresh errors retain the last known task view. Tokens stay in page memory; disconnect
clears rendered operator state. Model/source text is rendered as text, and external
links accept HTTP(S) only. Knowledge/proposal/audit features remain in expandable panels.

The browser CI gate (`scripts/dashboard_smoke.cjs`) uses local fixtures to exercise
scoped follow-up, exact clarification, lost-response duplicate protection, structured
results, escaping, selection races, refresh failure, mobile width and disconnect.
This verifies UI behavior, not live runtime/channel acceptance.
