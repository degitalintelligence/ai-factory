/* Browser contracts against local fixtures, never production or live model APIs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "..", "app", "console");
const tasks = [
  {
    id: 77,
    project: "self",
    requirement: "Audit model dan budget AI Factory",
    status: "failed",
    summary: "Budget tidak cukup untuk panggilan berikutnya.",
    llm_calls: 5,
    tokens: 41432,
    cost_usd: 0.12,
    cost_incomplete: true,
    conversation_id: "thread_77",
    updated_at: "2026-10-09T00:00:00Z",
  },
  {
    id: 78,
    project: "lab",
    requirement: "Siapkan rencana fitur todo",
    status: "waiting_input",
    summary: "Siapa pengguna fitur ini?",
    conversation_id: "thread_78",
    updated_at: "2026-10-09T00:05:00Z",
  },
  {
    id: 79,
    project: "self",
    requirement: "Review <img src=x onerror=alert(1)>",
    status: "completed",
    summary: "Audit selesai; ada rekomendasi yang bisa ditinjau.",
    conversation_id: "thread_79",
    updated_at: "2026-10-09T00:08:00Z",
  },
];
let decision = {
  id: 12,
  task_id: 78,
  project: "lab",
  title: "Siapa pengguna fitur ini?",
  state: "open",
  category: "clarification_needed",
  situation: "Target pengguna perlu ditentukan.",
  missing_information: "Siapa pengguna fitur ini?",
  recommendation: "answer",
  risk_level: "low",
  options: [],
  evidence: ["task:78"],
};
const mutations = [];
const errors = [];
let abortChat = true;
let failDetail = false;
let failOverview = false;
let delayHistory = false;
async function main() {
  const server = http.createServer((req, res) => {
    const name =
      req.url === "/dashboard" ? "index.html" : req.url.split("/").pop();
    if (!["index.html", "app.js", "style.css"].includes(name)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.setHeader(
      "Content-Type",
      name.endsWith(".js")
        ? "text/javascript"
        : name.endsWith(".css")
          ? "text/css"
          : "text/html",
    );
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  let page;
  try {
    browser = await chromium.launch({
      headless: true,
      channel: "chromium",
      args: ["--no-sandbox"],
    });
  } catch (error) {
    await new Promise((resolve) => server.close(resolve));
    throw error;
  }
  try {
    page = await browser.newPage({ viewport: { width: 1536, height: 1100 } });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/*", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const p = url.pathname;
      if (!p.startsWith("/v1/") && !p.startsWith("/tasks/"))
        return route.continue();
      const json = (body, status = 200) =>
        route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify(body),
        });
      if (req.method() === "POST") {
        const body = req.postDataJSON();
        mutations.push({ path: p, body });
        if (p === "/v1/chat") {
          if (abortChat) {
            abortChat = false;
            return route.abort("failed");
          }
          return json({
            intent_id: 77,
            conversation_id: "thread_77",
            summary: "Statut tercatat: failed",
            status: "failed",
          });
        }
        if (p === "/v1/decisions/12/clarification") {
          decision = { ...decision, state: "approved" };
          tasks[1].status = "received";
          return json(tasks[1]);
        }
        return json({});
      }
      if (p === "/v1/overview")
        return failOverview
          ? json({ detail: "Fixture unavailable" }, 503)
          : json({
              tasks,
              decisions: [decision],
              projects: [
                { id: "self", repo: "owner/ai-factory" },
                { id: "lab", repo: "owner/lab" },
              ],
              summary: "1 keputusan terbuka; 1 pekerjaan perlu ditinjau.",
            });
      if (p === "/v1/metrics") return json({ calls: 5, tokens: 41432 });
      if (["/v1/improvement-proposals", "/v1/memory/conflicts"].includes(p))
        return json([]);
      if (p.startsWith("/v1/conversations/")) {
        if (delayHistory && p.endsWith("77"))
          await new Promise((r) => setTimeout(r, 200));
        return json({
          turns: [
            {
              message: p.endsWith("78")
                ? "Rencanakan fitur todo"
                : "Audit AI Factory",
              response: {
                summary: p.endsWith("78")
                  ? "Siapa pengguna fitur ini?"
                  : "Budget tidak cukup. Persempit tujuan audit.",
              },
            },
          ],
        });
      }
      if (p.endsWith("/evidence")) {
        if (failDetail) return json({ detail: "Evidence unavailable" }, 503);
        return json(
          p.includes("/79/")
            ? [
                {
                  kind: "staff_result",
                  content: JSON.stringify({
                    summary: "Temuan berdasarkan source repository.",
                    findings: [
                      {
                        title: "Budget perlu diperjelas",
                        situation: "Estimasi berbeda dari usage aktual.",
                        recommendation: "Pisahkan label estimasi dan aktual.",
                        risk: "Belum diverifikasi live.",
                        evidence_refs: ["repo:self:app/staff.py"],
                      },
                    ],
                    missing_information: ["Bukti runtime belum tersedia"],
                  }),
                },
              ]
            : [
                {
                  kind: "staff_failure",
                  content: JSON.stringify({
                    message: "Budget tidak cukup untuk panggilan berikutnya.",
                    stage: "execution",
                  }),
                },
              ],
        );
      }
      if (p.endsWith("/events"))
        return json([
          {
            kind: "failed",
            message: "Budget tidak cukup untuk panggilan berikutnya.",
            created_at: "2026-10-09T00:00:00Z",
          },
        ]);
      const id = Number(p.split("/").pop());
      return json(tasks.find((t) => t.id === id) || {});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/dashboard`);
    await page.locator("#token").fill("fixture-operator");
    await page.locator("#connect button").click();
    await page.locator("#workspace").waitFor({ state: "visible" });
    await page.getByRole("button", { name: /Audit model dan budget/ }).click();
    await page.getByText("Pekerjaan berhenti", { exact: true }).waitFor();
    assert.match(
      await page.locator("#task-detail").innerText(),
      /Budget tidak cukup/,
    );
    assert.match(
      await page.locator("#conversation").innerText(),
      /Persempit tujuan/,
    );
    assert.equal(
      mutations.length,
      0,
      "Reading/selecting must not mutate state",
    );
    await page.locator("#message").fill("status");
    await page.locator("#send").click();
    await page.getByText(/Pesan belum dikonfirmasi/).waitFor();
    assert.equal(await page.locator("#message").inputValue(), "status");
    await page.locator("#send").click();
    await page.waitForFunction(
      () => document.getElementById("message").value === "",
    );
    assert.equal(
      mutations[0].body.idempotency_key,
      mutations[1].body.idempotency_key,
      "Lost-response retry must reuse key",
    );
    assert.equal(mutations[1].body.reply_to_intent_id, 77);
    assert.equal(mutations[1].body.project, "self");
    await page
      .getByRole("button", { name: /Siapkan rencana fitur todo/ })
      .click();
    await page
      .locator("#decisions")
      .getByRole("button", { name: "Jawab pertanyaan" })
      .click();
    await page.locator("#action-value").fill("Pemilik todo masing-masing");
    await page.locator("#action-submit").click();
    await page.locator("#action-dialog").waitFor({ state: "hidden" });
    assert.deepEqual(mutations[2], {
      path: "/v1/decisions/12/clarification",
      body: { answer: "Pemilik todo masing-masing" },
    });
    await page.getByRole("button", { name: /Review <img/ }).click();
    await page
      .getByText("Temuan berdasarkan source repository.", { exact: true })
      .waitFor();
    assert.equal(
      await page.locator("img").count(),
      0,
      "Untrusted model/source text must not become HTML",
    );
    assert.match(
      await page.locator("#task-detail").innerText(),
      /Bukti runtime belum tersedia/,
    );
    failDetail = true;
    await page.getByRole("button", { name: /Audit model dan budget/ }).click();
    await page.getByText(/Sebagian detail belum berhasil dibuka/).waitFor();
    assert.match(
      await page.locator("#task-detail").innerText(),
      /Pekerjaan berhenti/,
    );
    failDetail = false;
    delayHistory = true;
    await page.getByRole("button", { name: /Audit model dan budget/ }).click();
    await page
      .getByRole("button", { name: /Siapkan rencana fitur todo/ })
      .click();
    await page.waitForTimeout(300);
    assert.match(await page.locator("#thread-target").innerText(), /#78/);
    assert.match(await page.locator("#conversation").innerText(), /fitur todo/);
    delayHistory = false;
    failOverview = true;
    await page.locator("#refresh").click();
    await page.getByText(/Pembaruan gagal/).waitFor();
    assert.equal(
      await page.locator(".task-card").count(),
      3,
      "Refresh errors preserve last known state",
    );
    failOverview = false;
    await page.getByRole("button", { name: /Audit model dan budget/ }).click();
    if (process.env.DASHBOARD_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.DASHBOARD_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({
        path: path.join(
          process.env.DASHBOARD_SCREENSHOT_DIR,
          "dashboard-desktop.png",
        ),
        fullPage: true,
      });
    }
    if (process.env.DASHBOARD_LOG_PREVIEW === "true")
      console.log(
        "DASHBOARD_DESKTOP_JPEG=" +
          (await page.screenshot({ type: "jpeg", quality: 50 })).toString(
            "base64",
          ),
      );
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
      "Mobile must not overflow",
    );
    if (process.env.DASHBOARD_SCREENSHOT_DIR)
      await page.screenshot({
        path: path.join(
          process.env.DASHBOARD_SCREENSHOT_DIR,
          "dashboard-mobile.png",
        ),
        fullPage: true,
      });
    if (process.env.DASHBOARD_LOG_PREVIEW === "true")
      console.log(
        "DASHBOARD_MOBILE_JPEG=" +
          (await page.screenshot({ type: "jpeg", quality: 50 })).toString(
            "base64",
          ),
      );
    await page.locator("#disconnect").click();
    assert.equal(await page.locator("#workspace").isVisible(), false);
    assert.equal(await page.locator("#conversation").innerText(), "");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(errors, []);
    console.log(
      "Dashboard browser smoke passed: targeting, clarification, duplicate-send recovery, results, XSS, race, refresh errors, mobile, disconnect.",
    );
  } catch (error) {
    if (page && process.env.DASHBOARD_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.DASHBOARD_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({
        path: path.join(
          process.env.DASHBOARD_SCREENSHOT_DIR,
          "dashboard-failure.png",
        ),
        fullPage: true,
      });
    }
    throw error;
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
