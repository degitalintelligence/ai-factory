# AI Factory V0.1

General-purpose autonomous software engineering factory.

## Boundary

AI Factory is the software-building system itself. Domain products such as Quant Factory are separate repositories/products created by AI Factory and must not be embedded into this core.

## V0.1 flow

Telegram requirement -> Lead plan -> Developer edits a target repository -> restricted tests -> independent Reviewer -> up to MAX_ITERATIONS -> branch + GitHub pull request.

The first target repository is `degitalintelligence/telegram-lab`.

## Safety boundaries in V0.1

- No production deployment.
- No Docker socket access.
- No access to other repositories unless the GitHub credential grants it.
- Developer commands are allowlisted to pytest and Python compile checks.
- High-risk tasks stop automatically.
- Review/fix loops are capped.
- Human merges the pull request.

## Coolify deployment

Create a Docker Compose resource from this repository.

Set these environment variables in Coolify:

- OPENROUTER_API_KEY
- TELEGRAM_BOT_TOKEN
- GITHUB_TOKEN
- GITHUB_OWNER=degitalintelligence
- LAB_REPO=telegram-lab
- LEAD_MODEL
- DEVELOPER_MODEL
- REVIEWER_MODEL
- POSTGRES_PASSWORD
- DATABASE_URL=postgresql+asyncpg://ai_factory:<same-password>@postgres:5432/ai_factory
- MAX_ITERATIONS=3
- MAX_DEV_STEPS=30

Do not commit secrets.

## GitHub token scope

For V0.1, use a fine-grained token restricted to the lab repository. Minimum practical permissions:

- Contents: Read and write
- Pull requests: Read and write
- Metadata: Read

Do not grant access to production repositories yet.

## Telegram commands

`/start` - help

`/new <requirement>` - create and execute a task

`/status <task_id>` - inspect task status

## First acceptance test

Send:

`/new Tambahkan command /hello yang membalas "Halo, <nama user>!" dan buat test-nya.`

Expected outcome:

1. Lead creates a bounded plan.
2. Developer inspects Telegram Lab and changes code.
3. Tests run in the restricted workspace.
4. Reviewer independently reviews diff + test output.
5. If approved, AI Factory pushes a task branch and creates a PR.
6. You review/merge manually.

## Health check

GET `/health` returns the running version.
