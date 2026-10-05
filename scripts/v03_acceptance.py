"""Bounded live L0/L1 golden test. No automatic approval, merge, or deployment.

Run against a configured staging instance: FACTORY_URL, API_TOKEN and optional
FACTORY_PROJECT. The server uses its configured provider and persists full evidence.
This is an acceptance gate, not a synthetic source seeder.
"""

import asyncio
import os
import time
from uuid import uuid4

import httpx

GOAL = "Audit AI Factory. Tunjukkan tiga masalah paling penting, rekomendasi perbaikan, dan keputusan yang harus saya ambil minggu ini."


async def main():
    url = os.environ.get("FACTORY_URL", "").rstrip("/")
    token = os.environ.get("API_TOKEN", "")
    if not url or not token:
        raise SystemExit("FACTORY_URL and API_TOKEN are required; configure them outside source control")
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as client:
        response = await client.post(
            "/v1/chat",
            json={
                "message": GOAL,
                "project": os.environ.get("FACTORY_PROJECT", ""),
                "idempotency_key": f"acceptance:{uuid4()}",
            },
        )
        response.raise_for_status()
        task_id = response.json()["intent_id"]
        print(f"Golden acceptance intent #{task_id}; track /dashboard and /v1/tasks/{task_id}/evidence")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = await client.get(f"/v1/intents/{task_id}/result")
            response.raise_for_status()
            data = response.json()
            status = data["task"]["status"]
            if status in {"failed", "cancelled", "waiting_input", "awaiting_approval"}:
                raise SystemExit(
                    f"Acceptance requires operator attention: {status}; inspect intent #{task_id}. No automated gate bypass."
                )
            if status == "completed":
                result = data["result"]
                if not result or len(result["findings"]) != 3:
                    raise SystemExit(
                        "Golden acceptance failed: three grounded findings are required. Supply actual source evidence; do not manufacture findings."
                    )
                response = await client.get(f"/v1/tasks/{task_id}/evidence")
                response.raise_for_status()
                context = next(a["content"] for a in reversed(response.json()) if a["kind"] == "context")
                import json

                refs = {item["ref"] for item in json.loads(context)}
                if any(
                    not f["evidence_refs"] or not set(f["evidence_refs"]) <= refs for f in result["findings"]
                ):
                    raise SystemExit("Golden acceptance failed: unauthorized evidence reference")
                response = await client.get("/v1/overview")
                response.raise_for_status()
                cards = [d for d in response.json()["decisions"] if d["task_id"] == task_id]
                if len(cards) != 3:
                    raise SystemExit("Golden acceptance failed: three shared decision cards required")
                print(
                    f"PASS: completed intent #{task_id}, three reviewed source-backed findings and three durable decision cards. Independently review claim quality and verify Telegram/dashboard parity before release."
                )
                return
            await asyncio.sleep(5)
        raise SystemExit(
            f"Acceptance timeout for intent #{task_id}; budget/history preserved, no automatic retry"
        )


if __name__ == "__main__":
    asyncio.run(main())
