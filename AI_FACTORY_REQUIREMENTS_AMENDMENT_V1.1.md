# AI Factory / LioBot — Requirements Amendment v1.1

**Status:** Addendum wajib untuk AI_FACTORY_FULL_REQUIREMENTS.md yang sudah dibagikan sebelumnya  
**Tanggal berlaku:** 2026-10-04  
**Pemilik keputusan:** Dedi / PT Pohon Keahlian Digital  
**Repository engine:** degitalintelligence/ai-factory  
**Repository uji:** degitalintelligence/telegram-lab  
**Model standar:** stealth/space-bunny-alpha untuk Lead, Developer, dan Reviewer

Dokumen lama tetap menjadi baseline historis. Amendment ini memiliki prioritas lebih tinggi untuk istilah, batas produk, dan requirement yang bertentangan. Tujuannya adalah menjaga pekerjaan developer yang sudah berjalan tanpa membuat arsitektur ganda.

## 0. Keputusan pengikat

1. LioBot = AI Factory. Keduanya satu produk, bukan dua aplikasi atau dua roadmap.
2. Repository ai-factory adalah engine dan core product tempat LioBot dibangun.
3. Chief of Staff, orchestrator, context/memory, agent roles, tools, approvals, audit, dan channel adapters berada di dalam LioBot/AI Factory.
4. telegram-lab adalah laboratorium pengujian dan acceptance harness, bukan produk LioBot final.
5. Telegram untuk /new, /status, /logs, dan command lain adalah operator/test interface. Interface final boleh Telegram native chat, dashboard, atau keduanya, tetapi state dan keputusan harus sama.
6. Quant Factory dan factory spesialis lain dapat dibangun atau dikendalikan oleh LioBot, tetapi tetap memiliki boundary runtime, credential, data, dan deployment sendiri.

## 1. Prioritas dokumen

Jika terdapat perbedaan, gunakan urutan berikut:

1. Instruksi eksplisit terbaru dari Dedi.
2. Amendment ini.
3. AI_FACTORY_FULL_REQUIREMENTS.md versi 1.1.
4. Baseline lama dan catatan implementasi.
5. Asumsi developer.

Jangan memilih tafsir diam-diam. Konflik yang memengaruhi arsitektur atau risiko harus ditandai sebagai DECISION_REQUIRED untuk Dedi.

## 2. Terminologi resmi

| Wording yang harus dianggap obsolete | Wording resmi |
|---|---|
| AI Factory membangun LioBot sebagai aplikasi terpisah | AI Factory adalah engine dan produk LioBot itu sendiri |
| LioBot berada di luar AI Factory | LioBot dibangun dan dijalankan dari core ai-factory |
| telegram-lab adalah LioBot | telegram-lab adalah test harness dan acceptance surface |
| Bot Telegram sekarang adalah interface final | Bot Telegram sekarang adalah adapter/operator interface untuk testing |
| Chief of Staff berada di luar AI Factory | Chief of Staff adalah orchestrator utama di dalam LioBot |
| Agent boleh mengedit dirinya langsung di production | Self-improvement harus melewati task, branch, eval, review, dan approval |
| Semua data organisasi dimasukkan ke setiap prompt | Agent menerima context slice yang relevan dan permission-aware |

Nama class, table, endpoint, atau environment variable yang sudah dipakai tidak harus diganti sekaligus. Yang wajib konsisten adalah makna, boundary, dokumentasi, dan acceptance criteria.

## 3. Aturan kompatibilitas: jangan mengulang pekerjaan

- Perubahan yang sudah dibuat pada telegram-lab tidak perlu dihapus atau di-rewrite.
- Test, fixture, command, migration, dan acceptance scenario di telegram-lab dipertahankan sebagai contract test/acceptance test LioBot.
- PR yang sedang berjalan tetap boleh diselesaikan sesuai workflow repository.
- Jangan membuat LioBot kedua di dalam telegram-lab.
- Core product baru, self-building, memory/context, decision inbox, governance, dan channel abstraction masuk ke ai-factory.
- Jika perubahan mencampur test harness dan core product, pisahkan bertahap melalui adapter atau contract test; jangan membatalkan hasil yang aman.
- Klasifikasikan pekerjaan lama sebagai core product, channel adapter, test harness, fixture, atau documentation.

## 4. Product hierarchy yang benar

Dedi berkomunikasi dengan satu produk:

- LioBot / AI Factory
  - Chief of Staff / orchestrator
  - Context and Memory Plane
  - Engineering roles: Lead, Developer, Reviewer, Security
  - Product, Marketing, Admin/Ops, Finance, Sales/CS roles
  - Specialist factory connectors, termasuk Quant Factory
  - Policy, approvals, audit, cost, dan risk controls
  - Channel adapters: Telegram, dashboard, API
- telegram-lab
  - test harness, smoke test, acceptance test, dan operator test surface

Agent tidak boleh menganggap Telegram sebagai source of truth. Source of truth adalah state dan evidence pada core LioBot. Dashboard dan Telegram hanya adapter ke contract yang sama.

## 5. Self-building LioBot

LioBot harus dapat menemukan gap, mengusulkan perbaikan, membuat pekerjaan engineering, dan menyiapkan perubahan yang bisa direview. Ini bukan izin untuk mengubah production tanpa kontrol manusia.

Loop wajib:

1. Observe failure, gap, atau pola berulang.
2. Diagnose dengan evidence.
3. Usulkan improvement dan expected benefit.
4. Buat task type self_improvement di ai-factory.
5. Kerjakan di isolated branch/workspace.
6. Jalankan test, eval, security check, dan regression.
7. Lakukan independent review.
8. Buat decision card untuk Dedi.
9. Tunggu approve, reject, ask, atau defer yang eksplisit.
10. Merge/deploy hanya sesuai policy.
11. Ukur before/after, simpan learning, dan rollback bila terjadi regresi.

Area yang dapat di-self-improve melalui gate normal:

- Prompt, role contract, output schema, retry, routing, skill, evaluator, parser, connector, adapter.
- Retrieval, indexing, freshness check, citation/provenance, dashboard, notification, docs, runbook, dan test fixture.
- Workflow non-sensitive yang tidak mengubah permission, credential, hard budget limit, atau production data.

Area yang selalu membutuhkan persetujuan eksplisit Dedi:

- Permission, allowlist, sandbox boundary, network egress, secret/credential scope.
- Budget hard limit, model policy, provider switch, merge ke main, release, deployment target, production rollback.
- External message, publication, payment/transfer, purchase, legal action.
- Penghapusan evidence, database, volume, workspace, audit log, atau historical memory.
- Penurunan test/review/approval gate atau perubahan yang dapat menimbulkan irreversible production effect.

Setiap self-improvement wajib menyimpan masalah, evidence, hipotesis, scope, baseline, test/eval/security result, independent review, before/after quality-reliability-latency-cost, risk, blast radius, rollback plan, expiry/review date, dan keputusan Dedi. Tidak boleh ada self-edit hanya berdasarkan model merasa lebih baik.

## 6. Dedi-first communication

Dedi tidak boleh dipaksa membaca raw log untuk memahami pekerjaan. Setiap update penting menjawab:

- Apa yang terjadi?
- Mengapa dilakukan?
- Apa evidence atau link artefaknya?
- Apa yang kurang atau berisiko?
- Apa opsi dan rekomendasi LioBot?
- Apakah Dedi harus bertindak sekarang?
- Apa akibat approve, reject, ask, atau defer?

Gunakan message type berikut:

- UPDATE
- NEED_INFO
- DECISION_REQUIRED
- APPROVAL_REQUIRED
- WARNING
- BLOCKED
- COMPLETED
- LEARNING_PROPOSAL

Decision card minimal harus memiliki: decision_id, task_id, type, title, situation, why_now, options, impact setiap opsi, risk setiap opsi, recommendation, evidence, missing_information, expiry, rollback, dan required_action.

Dedi boleh menjawab dengan bahasa natural atau tombol: approve, reject, ask, defer. Jawaban harus dipetakan ke state yang sama dan diaudit.

## 7. Decision Inbox

LioBot wajib memiliki satu inbox keputusan yang menampilkan:

- Semua item yang menunggu tindakan Dedi.
- Prioritas, expiry, risk level, dan affected project.
- Recommendation dan alternatif.
- Evidence yang dapat dibuka tanpa mencari raw log.
- State: open, approved, rejected, needs-info, deferred, expired.
- Siapa yang memutuskan, kapan, dan perubahan state sesudah keputusan.

Telegram dan dashboard membaca inbox yang sama. Tidak boleh ada keputusan yang hanya tersimpan di chat Telegram.

## 8. Context dan Memory Plane

LioBot perlu mengetahui data organisasi tanpa memberikan seluruh data mentah ke setiap agent.

Setiap memory item wajib memiliki source/reference, project atau business scope, owner, created/updated time, freshness/expiry, confidence, sensitivity, provenance, version, correction/retraction path, permission/role, dan link ke evidence asli.

Aturan retrieval:

- Orchestrator mengambil relevant context slice, bukan dump seluruh database.
- Agent hanya menerima data sesuai permission dan tujuan task.
- Retrieval menunjukkan source, timestamp, dan confidence jika memengaruhi keputusan.
- Stale, contradictory, atau unverifed information harus diberi label.
- Dedi dapat mengoreksi, mencabut, atau mengunci memory; semua koreksi diaudit.
- Secret dan credential tidak boleh masuk prompt, log, artifact, atau memory biasa.

## 9. Interface dan channel policy

- Telegram native chat boleh menjadi interface tercepat untuk Dedi.
- Dashboard dipakai untuk inbox, filter, evidence, diff, history, dan banyak task.
- Telegram dan dashboard adalah channel adapter, bukan dua backend.
- Action yang sama harus ada di contract: create task, ask, approve, reject, defer, cancel, retry, inspect, report.
- Bila satu channel gagal, keputusan dapat dilanjutkan di channel lain tanpa kehilangan state.
- telegram-lab digunakan untuk menguji adapter dan acceptance scenario; keterbatasan lab bukan batas desain produk.
- Raw log tetap tersedia untuk debugging, tetapi bukan primary UX Dedi.

## 10. Governance dan safety

- Setiap tool invocation membawa actor, task, project, policy decision, dan correlation id.
- Sandbox isolation, network policy, credential boundary, budget limit, rate limit, dan audit log tetap aktif.
- Agent tidak boleh mengubah policy yang mengontrol agent itu sendiri tanpa decision request dan independent review.
- Merge dan production deploy selalu human-controlled.
- Self-building memiliki kill switch dan rollback.
- Penghapusan data, artifact, atau log material memerlukan approval.
- Structured-output failure memakai retry/fallback yang terukur, bukan infinite retry.
- stealth/space-bunny-alpha adalah default model saat ini melalui configuration ter-audit, bukan nilai yang tersebar atau hard-coded.

## 11. Prioritas implementasi

### P0 — tanpa membatalkan pekerjaan lama

1. Tandai telegram-lab sebagai test harness pada docs dan code comments.
2. Konsistenkan istilah LioBot/AI Factory sebagai satu produk.
3. Tetapkan event/state contract dan Decision Request contract.
4. Tetapkan Decision Inbox sebagai source of truth.
5. Tambahkan task type self_improvement dengan branch, eval, review, approval, merge, deploy, measurement, dan rollback fields.
6. Pastikan semua model configuration menunjuk ke stealth/space-bunny-alpha.

### P1 — fondasi intelligence

1. Context/Memory Plane dengan provenance, freshness, permission, correction, dan retrieval slice.
2. Independent review dan evidence bundle yang dapat dibuka Dedi.
3. Evaluation/regression registry untuk mengukur self-improvement.
4. Metrics untuk cost, latency, reliability, risk, dan learning.

### P2 — kemudahan komunikasi

1. Telegram adapter dengan update singkat dan decision card.
2. Dashboard inbox untuk decision, evidence, diff, history, dan status lintas project.
3. Sinkronisasi state dua arah tanpa state khusus channel.
4. Notification policy agar Dedi tahu apa yang perlu diketahui, bukan dibanjiri log.

## 12. Acceptance criteria amendment

- Dokumentasi dan implementasi menyebut LioBot = AI Factory secara konsisten.
- ai-factory diperlakukan sebagai core product/engine.
- telegram-lab tetap dapat menjalankan test dan acceptance flow tanpa dianggap produk final.
- Pekerjaan lama di telegram-lab tetap berjalan dan tidak dihapus karena perubahan terminologi.
- LioBot dapat membuat task self_improvement terisolasi dengan evidence, eval, dan review.
- Self-improvement yang menyentuh policy, credential, budget, production, merge, atau deploy berhenti pada approval Dedi.
- Dedi menerima decision card dengan situasi, pilihan, risiko, rekomendasi, evidence, dan tindakan.
- Approval/reject/ask/defer dari Telegram atau dashboard memutakhirkan state yang sama.
- Retrieval membawa provenance, freshness, sensitivity, permission, dan correction path.
- Raw log, secret, dan credential bukan primary decision surface.
- Semua perubahan penting dapat diaudit dan memiliki rollback/stop path.

## 13. Instruksi copy-paste untuk developer

Amendment v1.1 overrides conflicting wording in the previous AI Factory requirements.

LioBot = AI Factory. Build the core product in degitalintelligence/ai-factory.
Chief of Staff/orchestrator, memory/context, agents, tools, approvals, and audit
are inside that one product.

degitalintelligence/telegram-lab is a test harness and acceptance surface only.
Keep existing Telegram commands, tests, fixtures, and migrations if they work;
do not delete or redo them. Do not build a second final LioBot inside telegram-lab.
Classify its work as test harness, adapter, fixture, or contract test.

LioBot must improve itself through a gated self-improvement task:
observe, diagnose, proposal, isolated branch, tests/evals/security, independent
review, Dedi decision, controlled merge/deploy, measurement, rollback/learning.
Never allow uncontrolled self-editing or direct production mutation.

Dedi receives concise decision cards, not raw logs. Every approval/decision states
situation, why now, options, recommendation, evidence, risk, rollback, and the
exact action: approve, reject, ask, or defer. Telegram and dashboard share one
state and one decision inbox.

Use permission-aware context/memory with source, freshness, provenance,
sensitivity, version, correction/retraction, and audit trail. Give agents only
the relevant context slice. Keep credentials, hard budgets, sandbox policy,
merge, and production deploy human-controlled.

## 14. Final source of truth

- Dokumen lama tetap menjadi baseline dan riwayat pekerjaan.
- Full requirements v1.1 yang sudah diperbarui memuat koreksi normatif yang sama.
- Amendment ini wajib dibaca setelah dokumen lama oleh developer yang sudah terlanjur bekerja.
- Jika masih ada ambiguitas yang berisiko, buat DECISION_REQUIRED untuk Dedi.
- Perubahan terminologi bukan alasan untuk menghapus hasil kerja yang sudah lulus test.

