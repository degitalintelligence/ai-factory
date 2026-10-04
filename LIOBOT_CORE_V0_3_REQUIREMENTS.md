# LioBot Core v0.3 — Product & Engineering Requirements

**Status:** Amendement wajib terhadap `AI_FACTORY_FULL_REQUIREMENTS.md` dan `AI_FACTORY_REQUIREMENTS_AMENDMENT_V1.1.md`  
**Produk:** LioBot  
**Engine internal:** AI Factory  
**Versi target:** v0.3  
**Owner/approver:** Dedi  
**Tanggal:** 4 Oktober 2026

---

## 1. Tujuan dan posisi produk

Dokumen ini mengubah fokus AI Factory dari **task runner untuk coding** menjadi **LioBot sebagai Chief of Staff dan orchestration engine**.

Fondasi v0.2 tetap berlaku: sandbox, branch isolation, evidence, independent review, approval manusia, audit trail, dan deployment terkendali. Dokumen ini menambahkan lapisan kecerdasan, konteks, koordinasi, dan komunikasi keputusan di atas fondasi tersebut.

### LioBot

LioBot adalah antarmuka utama Dedi untuk:

- menyampaikan tujuan bisnis atau teknis dengan bahasa natural;
- memperoleh status lintas proyek dan lintas fungsi;
- menemukan masalah, lubang, risiko, dan peluang;
- meminta rencana dan opsi;
- mengawasi banyak agent/skill;
- menerima decision card yang ringkas dan actionable;
- approve, reject, request changes, atau defer;
- membangun dan memperbaiki dirinya secara terkontrol.

### AI Factory

AI Factory adalah engine internal yang menjalankan:

```text
Intent → Context → Clarification → Plan → Team/Skill → Execute → Verify → Review → Decision → Action → Learn
```

AI Factory bukan produk yang harus dipahami langsung oleh Dedi. Detail engine dan raw logs ditampilkan hanya melalui drill-down.

### Telegram dan dashboard

Telegram dan dashboard adalah adapter komunikasi ke state yang sama. Telegram saat ini boleh dipakai sebagai test harness, tetapi domain logic tidak boleh bergantung pada Telegram.

---

## 2. Masalah yang harus diselesaikan

Implementasi saat ini terasa seperti task queue karena:

1. setiap `/new` berdiri sendiri dan konteks lintas tugas belum kuat;
2. planner belum memilih team/skill berdasarkan tujuan;
3. hasil masih berupa status teknis, bukan rekomendasi keputusan;
4. memory organisasi, SOP, dan keputusan belum menjadi context aktif;
5. self-improvement masih berupa branch/test, belum menjadi loop yang dipimpin LioBot;
6. failure budget/structured output belum selalu dipulihkan secara cerdas;
7. Dedi belum memiliki satu inbox keputusan yang konsisten.

---

## 3. Sasaran v0.3

Untuk pesan seperti:

> “Audit kondisi AI Factory dan beri tahu apa yang perlu saya putuskan minggu ini.”

LioBot wajib mampu:

1. memahami tujuan dan konteks yang relevan;
2. bertanya hanya bila ambiguitas mengubah hasil;
3. menyusun rencana dan menjelaskan trade-off;
4. memilih skill/agent yang tepat;
5. menjalankan pekerjaan dalam batas permission dan budget;
6. mengumpulkan evidence dan melakukan review;
7. mengubah hasil menjadi decision card;
8. meminta approval pada titik yang tepat;
9. melaksanakan keputusan yang disetujui;
10. mencatat outcome untuk pembelajaran berikutnya.

### Definisi “cukup canggih”

LioBot dianggap memenuhi sasaran bila dapat menjawab:

> “Apa tiga hal paling penting yang perlu saya putuskan hari ini, mengapa sekarang, apa rekomendasinya, dan apa risikonya?”

Jawaban harus berbasis data/evidence, bukan generalisasi kosong.

---

## 4. Prinsip produk

1. **Decision-oriented:** Dedi menerima keputusan yang perlu diambil, bukan dump log.
2. **Context-aware:** jawaban mempertimbangkan tujuan, riwayat, SOP, data, dan keputusan sebelumnya.
3. **Evidence-first:** setiap klaim penting memiliki sumber, waktu, dan tingkat keyakinan.
4. **Human-controlled authority:** kecerdasan boleh berkembang, kewenangan tidak boleh meluas sendiri.
5. **Reversible by default:** aksi berisiko tinggi memiliki rollback atau approval.
6. **One state, many interfaces:** Telegram dan dashboard membaca inbox dan state yang sama.
7. **Model policy:** seluruh agent v0.3 menggunakan model alias `bunny-alpha` sebagai default. Perubahan model wajib melalui konfigurasi dan audit log.
8. **No cosmetic intelligence:** fitur baru harus meningkatkan kualitas atau kecepatan keputusan.

---

## 5. Capability dan kewenangan

| Level | Kemampuan | Approval |
|---|---|---|
| L0 | Membaca, merangkum, mencari, menganalisis | Tidak perlu selama read-only |
| L1 | Menyusun rencana, draft, test, branch, rekomendasi | Tidak perlu untuk workspace terisolasi |
| L2 | Mengubah code/data non-produksi, membuat PR, deployment staging | Sesuai policy proyek |
| L3 | Produksi, pesan eksternal, transaksi finansial, credential | Selalu approval Dedi |

Agent tidak boleh melompati level karena merasa yakin.

### Skill awal

Registry harus mendukung:

- Engineering: repository, coding, testing, review, release;
- Product/Research: requirement, research, prioritization, experiment;
- Marketing: campaign, content, funnel, competitor monitoring;
- Admin/Ops: SOP, task, vendor, schedule, follow-up;
- Finance: cashflow, reporting, invoice, anomaly detection;
- Sales/CS: lead, follow-up, customer response, escalation.

Engineering adalah skill pertama yang diverifikasi ketat, bukan batas akhir produk.

---

## 6. Core orchestration flow

### 6.1 Intake dan intent resolution

Pesan masuk diproses menjadi:

- `objective`;
- `desired_outcome`;
- `scope`;
- `constraints`;
- `urgency`;
- `risk_level`;
- `requested_authority`;
- `missing_information`.

Jika informasi kurang, LioBot harus memilih: lanjut dengan asumsi yang ditampilkan, bertanya maksimal tiga pertanyaan penentu, atau menolak dengan input minimum yang diperlukan.

### 6.2 Context assembly

Engine mengambil context berdasarkan relevansi dan permission:

- profil dan preferensi Dedi;
- tujuan perusahaan/proyek;
- keputusan terdahulu;
- task, PR, deployment, dan incident aktif;
- SOP, AGENTS, dan requirements;
- data terhubung yang diizinkan;
- memory dengan provenance.

Setiap context item memiliki `source`, `scope`, `owner`, `created_at`, dan `confidence`.

### 6.3 Planning

Planner wajib menghasilkan:

- objective dan success criteria;
- assumptions;
- work breakdown;
- skill/agent;
- dependencies;
- budget dan deadline;
- risk register;
- approval gates;
- rollback plan.

Planner harus menantang requirement yang kontradiktif, terlalu luas, atau tidak memiliki definisi selesai.

### 6.4 Team dan skill selection

Registry setiap agent/skill minimal memuat:

- capability;
- input/output schema;
- tools yang boleh dipakai;
- risk level;
- model policy;
- budget policy;
- evaluator;
- owner dan versi.

### 6.5 Execution dan verification

Executor menjalankan subtask sesuai dependency graph. Setiap subtask menghasilkan status, output terstruktur, evidence, tool calls, biaya/token, error/recovery, dan escalation bila perlu.

Hasil tidak boleh dipublikasikan hanya karena agent menyatakan selesai. Wajib tersedia test/evaluation, diff/artifact, security/policy check, independent review, unresolved findings, dan confidence level.

Kriteria yang baru dapat diverifikasi setelah PR/deployment ditandai `post_publication_criteria`, bukan menyebabkan review prematur gagal.

---

## 7. Decision Inbox

Decision Inbox adalah sumber kebenaran tunggal untuk semua item yang menunggu Dedi.

### Kategori

- `approval_required`;
- `risk_escalation`;
- `clarification_needed`;
- `blocked`;
- `recommendation`;
- `learning_proposal`;
- `incident`.

### Decision card

```text
Judul
Situasi
Mengapa sekarang
Rekomendasi utama
Opsi alternatif
Risiko dan dampak
Evidence
Rollback
Keputusan yang diminta
Deadline/urgency
```

Action minimal: Approve, Reject, Request changes, Ask follow-up, Defer, Delegate.

Telegram dan dashboard harus menampilkan item, status, action, dan audit trail yang sama. Raw logs hanya melalui drill-down.

---

## 8. Memory dan knowledge layer

### Jenis memory

- `identity`: profil dan preferensi komunikasi;
- `strategy`: visi, prioritas, KPI, trade-off;
- `project`: requirement, milestone, dependency;
- `operational`: SOP dan checklist;
- `decision`: keputusan dan alasan;
- `episodic`: percakapan/event relevan;
- `skill`: capability, playbook, evaluator;
- `lesson`: outcome dan pembelajaran.

### Aturan

- memory memiliki owner dan scope;
- memory private tidak bocor antar user/tenant;
- konflik memory ditandai, bukan diam-diam ditimpa;
- Dedi dapat melihat, mengoreksi, menghapus, atau mengunci memory;
- penggunaan memory penting dapat ditelusuri ke sumbernya.

---

## 9. Self-building dan self-improvement

LioBot boleh memperbaiki dirinya, tetapi tidak boleh mengubah production secara diam-diam.

### Trigger

Repeated failure, budget waste, evaluator score rendah, koreksi Dedi, missing skill, workaround manual berulang, security finding, atau outcome yang menyimpang dari prediksi.

### Improvement proposal

Wajib memuat masalah berbukti, root-cause hypothesis, perubahan, expected improvement, risk, test/evaluation plan, rollback, budget, dan approval yang diperlukan.

### Siklus

```text
Detect gap → Proposal → Dedi approve scope → Isolated branch → Implement
→ Test/evaluate → Independent review → Dedi approve merge/deploy
→ Observe outcome → Rollback atau retain
```

Perubahan authority, credential, approval policy, budget ceiling, atau production data selalu memerlukan approval eksplisit Dedi.

---

## 10. Budget, model, dan recovery

### Model policy

- default semua agent: `bunny-alpha`;
- alias dikonfigurasi, bukan di-hard-code dalam business logic;
- fallback model hanya jika policy mengizinkan dan harus tercatat;
- setiap call mencatat model, prompt version, token, latency, cost, dan task.

### Budget policy

- budget per task, subtask, dan global;
- warning pada 60%, 80%, dan 95%;
- automatic degradation: ringkas context, kurangi paralelisme, atau minta approval tambahan;
- task exhausted tidak boleh diam-diam di-reset melalui retry;
- retry baru harus memiliki attempt/audit trail yang jelas.

### Recovery

Jika structured output gagal: validasi schema, repair dengan context minimal, fallback parser hanya untuk output non-kritis, lalu escalation yang menjelaskan input/output yang dibutuhkan.

Pesan ke Dedi harus berisi tindakan berikutnya, bukan hanya `RuntimeError`.

---

## 11. Data model minimum

Entitas minimum:

`users`, `organizations`, `projects`, `objectives`, `plans`, `tasks`, `agents`, `skills`, `skill_versions`, `memory_items`, `decisions`, `decision_actions`, `evidence`, `approvals`, `tool_runs`, `model_runs`, `budgets`, `improvement_proposals`, `events`, dan `audit_log`.

Semua entitas memiliki `id`, `created_at`, `updated_at`, `created_by`, `scope`, serta status lifecycle bila relevan.

---

## 12. API minimum

Business logic harus dapat dipakai tanpa Telegram.

- `POST /v1/intents` — menerima objective natural language;
- `GET /v1/intents/{id}` — status intent dan plan;
- `POST /v1/intents/{id}/clarification` — menjawab pertanyaan;
- `POST /v1/plans/{id}/approve` — menyetujui plan;
- `GET /v1/decisions` — inbox keputusan;
- `POST /v1/decisions/{id}/action` — action decision;
- `GET /v1/tasks/{id}` — status dan evidence;
- `GET /v1/memory/search` — pencarian memory berpermission;
- `POST /v1/improvements` — improvement proposal;
- `GET /v1/health` dan `GET /ready` — health/readiness.

Response ke Dedi minimal memiliki `summary`, `next_action`, `decision_required`, `evidence_refs`, `risk`, dan `status`.

---

## 13. Security dan governance

- deny-by-default untuk tools dan credentials;
- explicit capability dan scope per tool;
- secret tidak boleh masuk prompt, log, artifact, atau memory;
- production action memerlukan approval;
- tenant/user isolation wajib diuji;
- prompt injection dari repository, email, dokumen, atau web adalah untrusted input;
- sandbox v0.2 tetap wajib;
- approval, rejection, override, deploy, dan rollback diaudit;
- event governance penting menggunakan append-only audit log.

---

## 14. Non-functional requirements

### Reliability

- state durable setelah restart;
- webhook dan approval action idempotent;
- task menggantung memiliki timeout/escalation;
- retry tidak menggandakan side effect.

### Performance

- acknowledgement intake maksimal 5 detik;
- Decision Inbox maksimal 3 detik pada beban normal;
- progress event tersedia untuk task lebih lama dari 10 detik.

### Observability

- correlation ID dari intent sampai outcome;
- metrics: success rate, review rejection, budget burn, retry rate, time-to-decision, human override, rollback;
- structured logs dengan redaction;
- status pengguna tidak bergantung pada raw log.

### Testability

Unit, integration, sandbox, adapter contract, evaluator, dan regression test wajib tersedia.

---

## 15. Acceptance criteria v0.3

### Orchestration

- [ ] satu intent menghasilkan plan berisi objective, dependencies, risk, budget, dan approval gates;
- [ ] planner memilih minimal dua skill untuk objective lintas fungsi;
- [ ] task dapat dipantau tanpa membuka database/log server.

### Decision Inbox

- [ ] Telegram dan dashboard identik;
- [ ] Dedi dapat approve/reject/request changes/defer;
- [ ] keputusan menyimpan actor, timestamp, alasan, dan outcome;
- [ ] raw log bukan format utama.

### Context dan memory

- [ ] memory scoped dan ber-owner;
- [ ] klaim penting memiliki evidence reference;
- [ ] konflik memory ditandai;
- [ ] Dedi dapat koreksi dan lock memory.

### Recovery dan budget

- [ ] structured-output failure menghasilkan repair/escalation yang dapat dipahami;
- [ ] warning muncul sebelum exhausted;
- [ ] task exhausted tidak dapat dihidupkan kembali diam-diam;
- [ ] task baru memiliki audit trail terpisah.

### Self-improvement

- [ ] failure berulang dapat menjadi improvement proposal;
- [ ] proposal memiliki evidence, test plan, risk, dan rollback;
- [ ] implementasi berjalan di branch/sandbox terisolasi;
- [ ] merge/deploy memerlukan approval Dedi;
- [ ] outcome setelah perubahan diukur.

### Security

- [ ] tidak ada secret di prompt/log/artifact;
- [ ] tool permission deny-by-default lulus test;
- [ ] user/tenant isolation lulus test;
- [ ] production action tanpa approval ditolak.

### Golden test

Input:

> “Audit AI Factory. Tunjukkan tiga masalah paling penting, rekomendasi perbaikan, dan keputusan yang harus saya ambil minggu ini.”

Output wajib memuat tiga temuan berbasis evidence, prioritas, rekomendasi, risiko, decision cards, status pekerjaan terkait, dan tidak ada klaim tanpa source.

---

## 16. Milestone

### M1 — Core orchestration

Intent schema, context assembly, plan schema, skill registry awal, durable state machine, dan progress events.

### M2 — Decision experience

Decision Inbox, decision card, Telegram/dashboard adapter, approval actions, audit trail, dan notification policy.

### M3 — Memory dan evaluator

Scoped memory, provenance, conflict handling, quality evaluator, model/budget observability.

### M4 — Self-improvement

Failure detection, improvement proposal, isolated self-build, independent review, approval, measurement, dan rollback.

Jangan memulai M4 sebelum M1–M3 lulus acceptance test.

---

## 17. Aturan untuk developer

1. Jangan memperlakukan LioBot sebagai kumpulan command Telegram.
2. Jangan menaruh business logic di adapter Telegram.
3. Jangan menambah agent tanpa capability schema, permission, evaluator, dan budget policy.
4. Jangan menghapus guardrail v0.2 tanpa proposal dan test.
5. Jangan menyatakan selesai tanpa evidence.
6. Jangan mengirim raw logs sebagai jawaban utama kepada Dedi.
7. Jangan mereset budget/task history untuk melewati safety guard.
8. Setiap perubahan memiliki regression test.
9. Setiap fitur harus menjawab: keputusan apa yang menjadi lebih baik atau lebih cepat?
10. Jika requirement ambigu, tampilkan asumsi atau bertanya—jangan mengarang.

---

## 18. Definition of Done

LioBot Core v0.3 selesai bila seluruh acceptance criteria lulus, Telegram dan dashboard memakai state yang sama, production action memiliki approval dan audit trail, failure memiliki recovery/escalation jelas, self-improvement berjalan melalui proposal → isolated test → review → approval, dokumentasi deployment/rollback tersedia, dan Dedi dapat berkomunikasi dalam bahasa natural tanpa memahami detail internal AI Factory.

**Target akhir:** Dedi tidak lagi mengelola antrean task. Dedi mengelola keputusan; LioBot mengelola konteks, rencana, koordinasi, eksekusi, evidence, dan pembelajaran dalam batas kewenangan yang disetujui.
