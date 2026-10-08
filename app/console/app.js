"use strict";
const STATUS_LABELS = {
  received: "Tujuan diterima",
  planning: "Menyusun rencana",
  developing: "Mengerjakan",
  testing: "Menguji hasil",
  reviewing: "Meninjau bukti",
  publishing: "Menyiapkan PR",
  waiting_input: "Butuh jawabanmu",
  awaiting_approval: "Butuh persetujuan",
  failed: "Berhenti karena masalah",
  pr_created: "PR siap ditinjau",
  completed: "Hasil tersedia",
  reviewed: "Review selesai",
  cancelled: "Dibatalkan",
  superseded: "Digantikan",
  deployed: "Deployment tercatat",
  deploying: "Sedang deploy",
  deployment_pending: "Deployment menunggu",
  deployment_unknown: "Deployment belum pasti",
  deployment_failed: "Deployment gagal",
  handoff_unverified: "Handoff belum terverifikasi",
};
function taskState(task) {
  return task.effective_status || task.status;
}
function attention(task) {
  return [
    "waiting_input",
    "awaiting_approval",
    "failed",
    "deployment_failed",
    "deployment_unknown",
    "handoff_unverified",
    "pr_created",
  ].includes(taskState(task));
}
function finished(task) {
  return ["completed", "reviewed", "deployed", "pr_created"].includes(
    taskState(task),
  );
}
function statusLabel(task) {
  return (
    STATUS_LABELS[taskState(task)] || `Status tercatat: ${taskState(task)}`
  );
}
function taskMatches(task, query, filter) {
  const match =
    `${task.id} ${task.requirement} ${task.project} ${statusLabel(task)}`
      .toLowerCase()
      .includes(query.toLowerCase());
  return (
    match &&
    (filter === "all" ||
      (filter === "attention" && attention(task)) ||
      (filter === "done" && finished(task)) ||
      (filter === "active" &&
        !attention(task) &&
        !finished(task) &&
        !["cancelled", "superseded"].includes(taskState(task))))
  );
}
function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}
function artifactMap(items) {
  const result = {};
  for (const item of items) {
    try {
      result[item.kind] = JSON.parse(item.content);
    } catch {
      result[item.kind] = item.content;
    }
  }
  return result;
}
function guidance(task, artifacts = {}) {
  const state = taskState(task);
  const failure = artifacts.staff_failure || artifacts.failure || {};
  const recorded =
    typeof failure === "object"
      ? failure.message || failure.error || failure.reason
      : failure;
  const cause =
    recorded ||
    task.summary ||
    task.last_message ||
    "Belum ada penjelasan yang tersimpan.";
  if (state === "waiting_input")
    return {
      title: "LioBot menunggu jawabanmu",
      cause,
      next: "Jawab pertanyaan yang tercatat pada kartu klarifikasi. Jawaban akan memperbarui rencana.",
    };
  if (state === "awaiting_approval")
    return {
      title: "Rencana menunggu keputusanmu",
      cause,
      next: "Baca rencana dan konsekuensinya, lalu pilih keputusan. Pekerjaan belum boleh melewati gate ini.",
    };
  if (["failed", "deployment_failed"].includes(state))
    return {
      title: "Pekerjaan berhenti",
      cause,
      next: "Tinjau penyebab dan evidence di bawah. Lu bisa mempersempit tujuan dalam pekerjaan baru; retry harus dipilih secara eksplisit dan tetap memakai budget seumur task.",
    };
  if (state === "pr_created")
    return {
      title: "PR tersedia untuk ditinjau",
      cause,
      next: "Buka PR atau minta revisi pada PR yang sama. Merge dan deploy masih keputusan operator.",
    };
  if (state === "handoff_unverified")
    return {
      title: "Status engineering belum dapat dipastikan",
      cause,
      next: "Verifikasi referensi handoff. Parent yang selesai bukan bukti engineering selesai.",
    };
  if (state === "deployment_unknown")
    return {
      title: "Hasil deployment belum pasti",
      cause,
      next: "Rekonsiliasi deployment sebelum mengulang tindakan. Tidak ada retry otomatis.",
    };
  if (finished(task))
    return {
      title: "Ada hasil yang bisa lu tinjau",
      cause,
      next: "Baca hasil dan bukti pendukung. Status selesai tidak otomatis membuktikan manfaat bisnis.",
    };
  if (["cancelled", "superseded"].includes(state))
    return {
      title: "Pekerjaan ini tidak berjalan",
      cause,
      next: "Buat tujuan baru bila pekerjaan masih diperlukan.",
    };
  return {
    title: statusLabel(task),
    cause,
    next: "Pantau pembaruan yang tersimpan. Persentase progres tidak ditebak dari jumlah tahap.",
  };
}
if (typeof module !== "undefined")
  module.exports = {
    taskState,
    attention,
    finished,
    taskMatches,
    safeLink,
    artifactMap,
    guidance,
    statusLabel,
  };
if (typeof document !== "undefined") initDashboard();
function initDashboard() {
  const el = (id) => document.getElementById(id);
  let token = "",
    epoch = 0,
    conversationId = null,
    selectedId = null,
    overview = { tasks: [], decisions: [], projects: [] },
    busy = false,
    detailSequence = 0,
    selectionSequence = 0,
    pendingSend = null,
    dialogCallback = null,
    detailCache = null;
  const nf = new Intl.NumberFormat("id-ID");
  function note(value, error = false) {
    el("status").textContent = value;
    el("status").classList.toggle("error", error);
  }
  function text(tag, value, parent, cls) {
    const node = document.createElement(tag);
    node.textContent = value ?? "";
    if (cls) node.className = cls;
    parent.append(node);
    return node;
  }
  function button(parent, label, callback, cls) {
    const node = text("button", label, parent, cls);
    node.type = "button";
    node.onclick = async () => {
      node.disabled = true;
      try {
        await callback();
      } catch (error) {
        note(error.message, true);
      } finally {
        node.disabled = false;
      }
    };
    return node;
  }
  function details(parent, label) {
    const node = document.createElement("details");
    text("summary", label, node);
    parent.append(node);
    return node;
  }
  function list(parent, items) {
    const ul = text("ul", "", parent);
    for (const item of items || [])
      text("li", typeof item === "string" ? item : JSON.stringify(item), ul);
  }
  function link(parent, label, url) {
    const safe = safeLink(url);
    if (!safe) return;
    const a = text("a", label, parent);
    a.href = safe;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  }
  function formatTime(value) {
    if (!value) return "Waktu belum tercatat";
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? "Waktu belum tercatat"
      : date.toLocaleString("id-ID");
  }
  function activeTask() {
    return overview.tasks.find((t) => t.id === selectedId);
  }
  function clearNode(id) {
    el(id).replaceChildren();
  }
  function sessionReset() {
    epoch++;
    token = "";
    conversationId = null;
    selectedId = null;
    pendingSend = null;
    busy = false;
    overview = { tasks: [], decisions: [], projects: [] };
    detailCache = null;
    detailSequence++;
    selectionSequence++;
    for (const id of [
      "tasks",
      "decisions",
      "task-detail",
      "conversation",
      "summary",
      "knowledge",
      "conflicts",
      "proposals",
      "metrics",
    ])
      clearNode(id);
    for (const id of [
      "message",
      "query",
      "memory-value",
      "memory-source",
      "memory-key",
      "memory-scope",
      "action-value",
      "action-source",
      "action-error",
    ])
      el(id).value = "";
    for (const id of [
      "action-error",
      "action-title",
      "action-context",
      "thread-target",
      "decision-scope",
      "decision-count",
    ])
      el(id).textContent = "";
    el("task-search").value = "";
    el("action-delegate").value = "";
    el("project").replaceChildren();
    const initialProject = text("option", "Lintas proyek", el("project"));
    initialProject.value = "";
    dialogCallback = null;
    el("action-dialog").close();
    el("workspace").hidden = true;
    el("access").hidden = false;
    el("disconnect").hidden = true;
    note("Koneksi ditutup. Masukkan token untuk membuka state operator.");
  }
  async function api(path, method = "GET", body) {
    const requestEpoch = epoch;
    const response = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    if (requestEpoch !== epoch)
      throw Error("Koneksi sudah berubah; respons lama diabaikan.");
    let data;
    try {
      data = await response.json();
    } catch {
      throw Error(
        "Server tidak mengirim respons yang bisa dibaca. Coba perbarui.",
      );
    }
    if (requestEpoch !== epoch)
      throw Error("Koneksi sudah berubah; respons lama diabaikan.");
    if (!response.ok) {
      if (response.status === 401) sessionReset();
      throw Error(
        typeof data.detail === "string"
          ? data.detail
          : `Permintaan ditolak (${response.status}). Tidak ada keberhasilan yang dikonfirmasi.`,
      );
    }
    return data;
  }
  function openAction(
    {
      title,
      context,
      label = "Alasan / pertanyaan",
      required = true,
      value = "",
      source = false,
      delegate = false,
      submit = "Simpan keputusan",
    },
    callback,
  ) {
    el("action-title").textContent = title;
    el("action-context").textContent = context;
    el("action-label").firstChild.textContent = label;
    el("action-value").value = value;
    el("action-value").required = required;
    el("action-source-label").hidden = !source;
    el("action-source").required = source;
    el("action-source").value = "";
    el("action-delegate-label").hidden = !delegate;
    el("action-delegate").required = delegate;
    el("action-delegate").value = "";
    el("action-error").textContent = "";
    el("action-submit").textContent = submit;
    dialogCallback = callback;
    el("action-dialog").showModal();
    el("action-value").focus();
  }
  el("action-form").onsubmit = async (event) => {
    event.preventDefault();
    el("action-submit").disabled = true;
    try {
      await dialogCallback({
        value: el("action-value").value.trim(),
        source: el("action-source").value.trim(),
        delegate: Number(el("action-delegate").value),
      });
      el("action-dialog").close();
      await refresh();
    } catch (error) {
      el("action-error").textContent = error.message;
    } finally {
      el("action-submit").disabled = false;
    }
  };
  for (const id of ["action-close", "action-cancel"])
    el(id).onclick = () => el("action-dialog").close();
  function renderSummary() {
    clearNode("summary");
    const tasks = overview.tasks;
    const open = overview.decisions.filter((d) => d.state === "open");
    for (const [count, label, filter] of [
      [open.length, "keputusan terbuka", "attention"],
      [
        tasks.filter(
          (t) =>
            !attention(t) &&
            !finished(t) &&
            !["cancelled", "superseded"].includes(taskState(t)),
        ).length,
        "sedang berjalan",
        "active",
      ],
      [tasks.filter(finished).length, "hasil tersedia", "done"],
    ]) {
      const b = button(el("summary"), "", () => {
        el("task-filter").value = filter;
        renderTasks();
        if (label === "keputusan terbuka") {
          el("decisions").scrollIntoView({
            behavior: "smooth",
            block: "center",
          });
        }
      });
      text("strong", nf.format(count), b);
      text("span", label, b);
    }
  }
  function renderTasks() {
    clearNode("tasks");
    const tasks = overview.tasks.filter((t) =>
      taskMatches(t, el("task-search").value, el("task-filter").value),
    );
    if (!tasks.length)
      text(
        "p",
        "Belum ada pekerjaan di tampilan ini. Mulai tujuan baru atau ubah filter.",
        el("tasks"),
        "empty",
      );
    for (const task of tasks) {
      const card = button(
        el("tasks"),
        "",
        () => selectTask(task.id),
        "task-card" + (task.id === selectedId ? " selected" : ""),
      );
      card.setAttribute("aria-pressed", String(task.id === selectedId));
      const meta = text("div", "", card, "task-meta");
      text("small", `#${task.id} · ${task.project || "Lintas proyek"}`, meta);
      text(
        "span",
        statusLabel(task),
        card,
        "badge" +
          (attention(task) ? " attention" : "") +
          (["failed", "deployment_failed"].includes(taskState(task))
            ? " failed"
            : ""),
      );
      text("strong", task.requirement, card);
      text("small", formatTime(task.updated_at), card);
    }
  }
  function renderDecisions() {
    clearNode("decisions");
    const task = activeTask();
    const targetIds = task ? [task.id, task.handoff_task_id] : [];
    const cards = overview.decisions.filter(
      (d) => d.state === "open" && (!task || targetIds.includes(d.task_id)),
    );
    el("decision-count").textContent = cards.length;
    el("decision-scope").textContent = task
      ? `Keputusan terkait pekerjaan #${task.id}${task.handoff_task_id ? ` dan engineering #${task.handoff_task_id}` : ""}.`
      : "Semua keputusan terbuka. Pilih pekerjaan untuk fokus.";
    if (!cards.length)
      text(
        "p",
        task
          ? "Tidak ada keputusan terbuka untuk pekerjaan ini."
          : "Tidak ada keputusan terbuka.",
        el("decisions"),
        "empty",
      );
    for (const d of cards) {
      const card = text("article", "", el("decisions"), "decision-card");
      text(
        "span",
        `#${d.id} · ${d.risk_level} · ${d.project || "Lintas proyek"}`,
        card,
        "badge",
      );
      text("h3", d.title, card);
      text("p", d.situation, card);
      if (d.priority) text("p", `Prioritas: ${d.priority}`, card);
      if (d.why_now) text("p", `Mengapa sekarang: ${d.why_now}`, card);
      if (d.recommendation)
        text("p", `Rekomendasi tercatat: ${d.recommendation}`, card);
      if (d.required_action)
        text("p", `Keputusan yang diminta: ${d.required_action}`, card);
      const more = details(card, "Pilihan, konsekuensi & bukti");
      for (const option of d.options || []) {
        text(
          "p",
          `${option.label || option.id}${option.impact ? ` — ${option.impact}` : ""}`,
          more,
        );
      }
      if (d.missing_information)
        text("p", `Informasi kurang: ${d.missing_information}`, more);
      if (d.impact?.length) {
        text("h3", "Dampak", more);
        list(more, d.impact);
      }
      if (d.risk?.length) {
        text("h3", "Risiko", more);
        list(more, d.risk);
      }
      if (d.rollback) text("p", `Rollback: ${d.rollback}`, more);
      if (d.expires_at) text("p", `Tenggat: ${formatTime(d.expires_at)}`, more);
      list(more, d.evidence);
      const actions = text("div", "", card, "actions");
      if (d.category === "clarification_needed") {
        button(actions, "Jawab pertanyaan", () =>
          openAction(
            {
              title: `Jawab klarifikasi #${d.id}`,
              context: d.missing_information || d.situation,
              label: "Jawabanmu",
              submit: "Kirim jawaban",
            },
            ({ value }) =>
              api(`/v1/decisions/${d.id}/clarification`, "POST", {
                answer: value,
              }),
          ),
        );
      } else
        for (const [answer, label] of [
          ["approve", "Setujui"],
          ["request_changes", "Minta revisi"],
          ["reject", "Tolak"],
          ["ask", "Tanya"],
          ["defer", "Tunda"],
          ["delegate", "Delegasi"],
        ])
          button(actions, label, () =>
            openAction(
              {
                title: `${label} keputusan #${d.id}`,
                context: `${d.situation}\n${d.required_action || ""}\nKeputusan ini hanya berlaku pada kartu #${d.id}; tidak otomatis merge atau deploy.`,
                required: ["ask", "request_changes", "delegate"].includes(
                  answer,
                ),
                delegate: answer === "delegate",
                submit: label,
              },
              ({ value, delegate }) =>
                api(`/v1/decisions/${d.id}/action`, "POST", {
                  answer,
                  reason: value,
                  ...(answer === "delegate" ? { delegate_to: delegate } : {}),
                }),
            ),
          );
    }
  }
  function renderConversation(data) {
    clearNode("conversation");
    for (const turn of data.turns || []) {
      bubble("Kamu", turn.message, "user");
      bubble(
        "LioBot",
        turn.response.summary || "Respons tercatat.",
        "",
        turn.response,
      );
    }
    if (!(data.turns || []).length)
      text(
        "p",
        "Belum ada percakapan tersimpan untuk pekerjaan ini. Lu bisa mulai follow-up di bawah.",
        el("conversation"),
        "empty",
      );
    el("conversation").scrollTop = el("conversation").scrollHeight;
  }
  function bubble(speaker, message, cls = "", response) {
    const node = text("div", "", el("conversation"), "bubble " + cls);
    text("span", speaker, node, "speaker");
    text("span", message, node);
    if (response?.candidates?.length)
      for (const id of response.candidates)
        button(node, `Pilih pekerjaan #${id}`, () => selectTask(id));
    return node;
  }
  function updateTarget() {
    const task = activeTask();
    el("thread-target").textContent = task
      ? `Follow-up #${task.id} · ${task.project || "Lintas proyek"}${conversationId ? " · riwayat tersimpan" : ""}`
      : "Tujuan baru · belum ada pekerjaan dipilih";
    el("composer-hint").textContent = task
      ? "Pesan dikaitkan dengan pekerjaan ini. Tujuan lain? Klik + Baru."
      : "Pesan baru membuat tujuan. Pilih pekerjaan untuk menindaklanjuti.";
    clearNode("suggestions");
    for (const [label, message] of task
      ? [
          ["Lihat progres", "status"],
          ["Kenapa berhenti?", "kenapa gagal"],
          ["Lihat hasil", "hasilnya"],
          ["Minta revisi", "revisi hasil: "],
        ]
      : [
          [
            "Audit proyek",
            "Audit repository proyek ini. Tunjukkan tiga masalah paling penting berdasarkan evidence yang tersedia. Jangan melakukan perubahan.",
          ],
          ["Susun rencana", "Bantu susun rencana untuk "],
        ])
      button(el("suggestions"), label, () => {
        el("message").value = message;
        el("message").focus();
      });
  }
  async function selectTask(id) {
    const task = overview.tasks.find((t) => t.id === id);
    if (!task) {
      note("Pekerjaan ini tidak tersedia di scope operator saat ini.", true);
      return;
    }
    const sequence = ++selectionSequence;
    selectedId = id;
    conversationId = task.conversation_id || null;
    el("project").value = task.project || "";
    pendingSend = null;
    renderTasks();
    renderDecisions();
    updateTarget();
    clearNode("conversation");
    text("p", "Membuka percakapan…", el("conversation"), "empty");
    if (conversationId) {
      try {
        const data = await api(`/v1/conversations/${conversationId}`);
        if (sequence !== selectionSequence) return;
        renderConversation(data);
      } catch (error) {
        if (sequence === selectionSequence) {
          clearNode("conversation");
          text(
            "p",
            `Riwayat belum berhasil dibuka: ${error.message}`,
            el("conversation"),
            "empty",
          );
        }
      }
    } else if (sequence === selectionSequence) {
      clearNode("conversation");
      text(
        "p",
        "Task ini belum memiliki riwayat chat dashboard. Follow-up baru akan disimpan.",
        el("conversation"),
        "empty",
      );
    }
    if (sequence === selectionSequence) await loadDetail();
  }
  function contextBlock(parent, title, value) {
    const block = text("div", "", parent, "context-block");
    text("h3", title, block);
    if (value) text("p", value, block);
    return block;
  }
  function renderDetail(
    task,
    artifacts = {},
    events = [],
    parentResult = null,
    loadError = "",
  ) {
    const root = el("task-detail");
    root.replaceChildren();
    text("h3", `#${task.id} · ${task.project || "Lintas proyek"}`, root);
    text("p", task.requirement, root);
    const guide = guidance(task, artifacts);
    const state = contextBlock(root, "SAAT INI", guide.title);
    state.lastChild.className = "big-status";
    contextBlock(root, "YANG TERCATAT / PENYEBAB", guide.cause);
    contextBlock(root, "LANGKAH BERIKUTNYA", guide.next);
    if (task.handoff_task_id)
      text(
        "p",
        `Tujuan ini diteruskan ke engineering #${task.handoff_task_id}. Progres dan evidence engineering ditampilkan di bawah.`,
        root,
        "muted",
      );
    if (loadError)
      text(
        "p",
        `Sebagian detail belum berhasil dibuka: ${loadError}. State ringkas terakhir tetap ditampilkan.`,
        root,
        "muted",
      );
    const result = artifacts.staff_result || parentResult?.result;
    if (result) {
      const block = contextBlock(root, "HASIL YANG TERSIMPAN", result.summary);
      for (const finding of result.findings || []) {
        const node = text("div", "", block, "finding");
        text("h3", finding.title, node);
        text("p", finding.situation, node);
        text("p", `Rekomendasi: ${finding.recommendation}`, node);
        if (finding.risk) text("p", `Risiko: ${finding.risk}`, node);
        const proof = details(node, "Bukti & alternatif");
        list(proof, finding.evidence_refs);
        if (finding.alternative) text("p", finding.alternative, proof);
      }
      if (result.missing_information?.length) {
        text("h3", "Belum diketahui", block);
        list(block, result.missing_information);
      }
      if (result.audit_checks?.length) {
        const checks = details(block, "Apa yang sudah / belum diverifikasi");
        for (const check of result.audit_checks)
          text(
            "p",
            `${check.topic} · ${check.verification}: ${check.observation}`,
            checks,
          );
      }
      if (result.next_action)
        text("p", `Saran hasil: ${result.next_action}`, block);
    } else if (finished(task) && !task.pr_url)
      contextBlock(
        root,
        "HASIL",
        "Belum ada hasil analisis terstruktur yang tersedia. Periksa evidence; status selesai saja bukan hasil bisnis.",
      );
    const taskData = artifacts.__task || task;
    const usage = contextBlock(root, "PENGGUNAAN TERCATAT");
    const grid = text("div", "", usage, "usage");
    for (const [label, value] of [
      ["Panggilan model", nf.format(taskData.llm_calls || 0)],
      ["Token aktual", nf.format(taskData.tokens || 0)],
      [
        "Biaya dilaporkan",
        `$${Number(taskData.cost_usd || 0).toFixed(4)}${taskData.cost_incomplete ? " · parsial" : ""}`,
      ],
      ["Diperbarui", formatTime(taskData.updated_at)],
    ]) {
      const item = text("div", label, grid);
      text("strong", value, item);
    }
    const envelope = artifacts.workflow_budget_envelope;
    if (envelope) {
      text(
        "p",
        `Estimasi sisa saat preflight: ${nf.format(envelope.estimated_tokens || 0)} token. Limit tercatat: ${nf.format(envelope.limits?.max_total_tokens || 0)}. Estimasi berbeda dari reservasi dan usage aktual.`,
        usage,
      );
    }
    const actions = text("div", "", root, "actions");
    if (task.pr_url) link(actions, "Buka PR ↗", task.pr_url);
    if (taskState(task) === "pr_created")
      button(actions, "Minta revisi PR", () =>
        openAction(
          {
            title: `Revisi PR task #${task.handoff_task_id || task.id}`,
            context:
              "Masukkan perubahan yang diminta. PR yang sama akan melewati test dan review ulang.",
            label: "Perubahan yang diminta",
            submit: "Kirim revisi",
          },
          ({ value }) =>
            api(
              `/v1/tasks/${task.handoff_task_id || task.id}/feedback`,
              "POST",
              { message: value },
            ),
        ),
      );
    if (taskState(task) === "failed")
      button(actions, "Retry eksplisit", () =>
        openAction(
          {
            title: `Retry task #${task.handoff_task_id || task.id}`,
            context:
              "Retry mempertahankan usage seumur task. Penyebab awal bisa tetap menghalangi pekerjaan; ini tidak menaikkan budget atau memberi persetujuan eksekusi.",
            label: "Catatan retry",
            required: false,
            submit: "Retry dengan batas yang sama",
          },
          ({ value }) =>
            api(`/v1/tasks/${task.handoff_task_id || task.id}/retry`, "POST", {
              message: value,
            }),
        ),
      );
    if (
      taskState(task) === "waiting_input" &&
      !overview.decisions.some(
        (d) =>
          d.state === "open" &&
          d.category === "clarification_needed" &&
          [task.id, task.handoff_task_id].includes(d.task_id),
      )
    )
      button(actions, "Jawab klarifikasi task", () =>
        openAction(
          {
            title: `Jawab task #${task.id}`,
            context: guide.cause,
            label: "Jawabanmu",
            submit: "Kirim jawaban",
          },
          ({ value }) =>
            api(`/v1/tasks/${task.handoff_task_id || task.id}/answer`, "POST", {
              message: value,
            }),
        ),
      );
    const plan = taskData.plan || task.plan;
    if (plan) {
      const node = details(root, "Rencana & kriteria keberhasilan");
      text("p", plan.objective, node);
      list(node, plan.success_criteria || plan.acceptance_criteria);
      list(
        node,
        (plan.steps || []).map((step) =>
          typeof step === "string" ? step : `${step.skill}: ${step.objective}`,
        ),
      );
      if (plan.risks?.length) {
        text("h3", "Risiko", node);
        list(node, plan.risks);
      }
    }
    const timeline = details(root, "Jejak pekerjaan · pembaruan tersimpan");
    const ol = text("ol", "", timeline, "timeline");
    for (const event of [...events].reverse()) {
      const row = text("li", event.message, ol);
      text("time", `${event.kind} · ${formatTime(event.created_at)}`, row);
    }
    if (!events.length)
      text("p", "Belum ada event yang berhasil dibuka.", timeline);
    const raw = details(root, "Evidence teknis · data asli");
    const publicArtifacts = { ...artifacts };
    delete publicArtifacts.__task;
    text("pre", JSON.stringify(publicArtifacts, null, 2), raw);
  }
  async function loadDetail() {
    const task = activeTask();
    if (!task) return;
    const seq = ++detailSequence;
    const id = task.handoff_task_id || task.id;
    renderDetail(task);
    const requests = await Promise.allSettled([
      api(`/v1/tasks/${id}/evidence`),
      api(`/tasks/${id}/events`),
      api(`/v1/tasks/${id}`),
      ...(task.handoff_task_id ? [api(`/v1/intents/${task.id}/result`)] : []),
    ]);
    if (seq !== detailSequence || selectedId !== task.id) return;
    const arts =
      requests[0].status === "fulfilled" ? artifactMap(requests[0].value) : {};
    if (requests[2].status === "fulfilled") {
      arts.__task = requests[2].value;
      if (arts.__task.pr_url) task.pr_url = arts.__task.pr_url;
    }
    const errors = requests
      .filter((r) => r.status === "rejected")
      .map((r) => r.reason.message)
      .join("; ");
    detailCache = {
      taskId: task.id,
      artifacts: arts,
      events: requests[1].status === "fulfilled" ? requests[1].value : [],
      parentResult:
        requests[3]?.status === "fulfilled" ? requests[3].value : null,
      error: errors,
    };
    renderDetail(
      task,
      arts,
      detailCache.events,
      detailCache.parentResult,
      errors,
    );
  }
  async function refresh() {
    if (!token || busy) return;
    busy = true;
    try {
      const data = await api("/v1/overview");
      const same = JSON.stringify(data) === JSON.stringify(overview);
      overview = data;
      const selected = el("project").value;
      const currentProjects = JSON.stringify(
        [...el("project").options].map((o) => o.value),
      );
      const values = ["", ...data.projects.map((p) => p.id)];
      if (JSON.stringify(values) !== currentProjects) {
        el("project").replaceChildren();
        const all = text("option", "Lintas proyek", el("project"));
        all.value = "";
        for (const p of data.projects) {
          const option = text("option", `${p.id} · ${p.repo}`, el("project"));
          option.value = p.id;
        }
        el("project").value = selected;
      }
      renderSummary();
      if (!same) {
        renderTasks();
        renderDecisions();
      }
      if (selectedId && activeTask()) {
        updateTarget();
        await loadDetail();
      }
      note(
        `Terakhir diperbarui ${new Date().toLocaleTimeString("id-ID")}. ${data.summary}`,
      );
      await refreshSupport();
    } catch (error) {
      note(`Pembaruan gagal: ${error.message}`, true);
      throw error;
    } finally {
      busy = false;
    }
  }
  async function refreshSupport() {
    const outcomes = await Promise.allSettled([
      api("/v1/improvement-proposals"),
      api("/v1/metrics"),
      api("/v1/memory/conflicts"),
    ]);
    if (!token) return;
    const failed = outcomes.filter((r) => r.status === "rejected");
    if (failed.length)
      note(
        `State pekerjaan tersedia; ${failed.length} panel pendukung gagal dimuat. Tekan Perbarui untuk mencoba lagi.`,
        true,
      );
    if (outcomes[1].status === "fulfilled")
      el("metrics").textContent = JSON.stringify(outcomes[1].value, null, 2);
    if (outcomes[0].status === "fulfilled") {
      clearNode("proposals");
      for (const proposal of outcomes[0].value) {
        const card = text("article", "", el("proposals"));
        text("h3", `#${proposal.id} · ${proposal.status}`, card);
        text("p", proposal.brief.problem, card);
        text("p", `Hipotesis: ${proposal.brief.hypothesis}`, card);
        list(card, proposal.brief.test_plan);
        list(card, proposal.brief.evidence);
        text(
          "p",
          `Risiko: ${proposal.brief.risk}; rollback: ${proposal.brief.rollback_plan}`,
          card,
        );
        if (!proposal.task_id)
          button(card, "Mulai proposal dengan approval gate", () =>
            openAction(
              {
                title: `Mulai proposal #${proposal.id}`,
                context:
                  "Membuat task perbaikan dengan gate yang sudah berlaku. Tidak otomatis merge atau deploy.",
                required: false,
                submit: "Buat task proposal",
              },
              () =>
                api(
                  `/v1/improvement-proposals/${proposal.id}/start`,
                  "POST",
                  {},
                ),
            ),
          );
      }
      if (!outcomes[0].value.length)
        text("p", "Belum ada usulan perbaikan.", el("proposals"), "empty");
    }
    if (outcomes[2].status === "fulfilled") {
      clearNode("conflicts");
      for (const conflict of outcomes[2].value) {
        const card = text("article", "", el("conflicts"));
        text("h3", `Konflik #${conflict.id}`, card);
        text(
          "p",
          `${conflict.proposed_value} · Source: ${conflict.source}`,
          card,
        );
        button(card, "Pilih nilai yang benar", () =>
          openAction(
            {
              title: "Selesaikan konflik knowledge",
              context: "Nilai final akan disimpan dengan source keputusan.",
              label: "Nilai final",
              value: conflict.proposed_value,
              source: true,
            },
            ({ value, source }) =>
              api(`/v1/memory/${conflict.memory_id}/correction`, "POST", {
                value,
                source,
              }),
          ),
        );
      }
      if (!outcomes[2].value.length)
        text("p", "Tidak ada konflik terbuka.", el("conflicts"), "empty");
    }
  }
  function newThread() {
    selectionSequence++;
    detailSequence++;
    selectedId = null;
    conversationId = null;
    pendingSend = null;
    detailCache = null;
    clearNode("conversation");
    const empty = text("div", "", el("conversation"), "chat-empty");
    text("h3", "Mulai dari hasil yang lu butuhkan.", empty);
    text(
      "p",
      "Jelaskan tujuan dan proyeknya. LioBot akan menyusun rencana; kalau ada hal yang belum jelas, pertanyaannya muncul di sini.",
      empty,
      "muted",
    );
    clearNode("task-detail");
    text(
      "p",
      "Pilih pekerjaan untuk melihat progres dan hasil, atau kirim tujuan baru.",
      el("task-detail"),
      "empty",
    );
    renderTasks();
    renderDecisions();
    updateTarget();
    el("message").focus();
  }
  el("connect").onsubmit = async (event) => {
    event.preventDefault();
    const value = el("token").value;
    sessionReset();
    token = value;
    el("token").value = "";
    el("connect").querySelector("button").disabled = true;
    try {
      await refresh();
      if (!token) return;
      el("access").hidden = true;
      el("workspace").hidden = false;
      el("disconnect").hidden = false;
      newThread();
    } catch (error) {
      sessionReset();
      note(`Tidak berhasil terhubung: ${error.message}`, true);
    } finally {
      el("connect").querySelector("button").disabled = false;
    }
  };
  el("disconnect").onclick = sessionReset;
  el("refresh").onclick = () => refresh().catch(() => {});
  el("new-thread").onclick = newThread;
  el("project").onchange = newThread;
  el("task-search").oninput = renderTasks;
  el("task-filter").onchange = renderTasks;
  el("chat").onsubmit = async (event) => {
    event.preventDefault();
    if (!token) return;
    const base = {
      message: el("message").value.trim(),
      project: el("project").value,
      ...(conversationId ? { conversation_id: conversationId } : {}),
      ...(selectedId ? { reply_to_intent_id: selectedId } : {}),
    };
    if (!base.message) return;
    const fingerprint = JSON.stringify(base);
    if (!pendingSend || pendingSend.fingerprint !== fingerprint)
      pendingSend = {
        fingerprint,
        body: { ...base, idempotency_key: crypto.randomUUID() },
      };
    const request = pendingSend;
    const sequence = selectionSequence;
    el("send").disabled = true;
    note("Mengirim pesan…");
    try {
      const result = await api("/v1/chat", "POST", request.body);
      pendingSend = null;
      if (sequence !== selectionSequence) {
        note(
          "Pesan diterima pada percakapan sebelumnya. Pilih pekerjaan untuk melihatnya.",
        );
        await refresh();
        return;
      }
      conversationId = result.conversation_id;
      selectedId = result.intent_id || selectedId;
      el("message").value = "";
      bubble("Kamu", base.message, "user");
      bubble("LioBot", result.summary, "", result);
      await refresh().catch((error) =>
        note(`Pesan diterima; pembaruan state gagal: ${error.message}`, true),
      );
      updateTarget();
      if (conversationId) {
        try {
          renderConversation(await api(`/v1/conversations/${conversationId}`));
        } catch (error) {
          note(
            `Pesan diterima; riwayat belum berhasil dimuat: ${error.message}`,
            true,
          );
        }
      }
    } catch (error) {
      note(
        `Pesan belum dikonfirmasi: ${error.message}. Teks tetap tersedia; kirim ulang pesan yang sama memakai ID permintaan yang sama.`,
        true,
      );
    } finally {
      el("send").disabled = false;
    }
  };
  el("memory").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const result = await api(
        `/v1/memory/search?keys=${encodeURIComponent(el("query").value)}&role=lead`,
      );
      clearNode("knowledge");
      for (const item of result.items) {
        const card = text("article", "", el("knowledge"));
        text("h3", `${item.key} · v${item.version} · ${item.label}`, card);
        text("p", item.value, card);
        text(
          "p",
          `Source: ${item.source}; evidence: ${item.evidence_ref}; confidence: ${item.confidence}`,
          card,
        );
        button(card, "Koreksi", () =>
          openAction(
            {
              title: "Koreksi knowledge",
              context:
                "Nilai sebelumnya tetap memiliki riwayat; masukkan source koreksi.",
              label: "Nilai pengganti",
              value: item.value,
              source: true,
            },
            ({ value, source }) =>
              api(`/v1/memory/${item.id}/correction`, "POST", {
                value,
                source,
              }),
          ),
        );
        for (const [action, label] of [
          ["lock", "Lock"],
          ["retract", "Retract"],
        ])
          button(card, label, () =>
            openAction(
              {
                title: `${label} knowledge`,
                context: `${item.key} · ${item.evidence_ref}`,
                label: "Alasan",
              },
              ({ value }) =>
                api(`/v1/memory/${item.id}/${action}`, "POST", {
                  reason: value,
                }),
            ),
          );
      }
      if (!result.items.length)
        text(
          "p",
          "Tidak ditemukan knowledge pada scope yang diizinkan.",
          el("knowledge"),
          "empty",
        );
    } catch (error) {
      note(error.message, true);
    }
  };
  el("write-memory").onsubmit = async (event) => {
    event.preventDefault();
    try {
      await api("/v1/memory", "POST", {
        key: el("memory-key").value,
        value: el("memory-value").value,
        source: el("memory-source").value,
        scope: el("memory-scope").value,
      });
      await refresh();
      note("Knowledge tersimpan dengan provenance.");
    } catch (error) {
      note(error.message, true);
    }
  };
  setInterval(() => {
    if (token && !el("action-dialog").open && !document.hidden)
      refresh().catch(() => {});
  }, 15000);
}
