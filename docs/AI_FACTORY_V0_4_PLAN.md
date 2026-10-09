# AI Factory / LioBot — Review main dan rencana v0.4

Tanggal review: 8 Oktober 2026. Implementasi kemudian diotorisasi Dedi melalui permintaan menyelesaikan v0.4.
Status terkini: implementation candidate; acceptance live dan persetujuan release belum selesai.
Lihat `V04_IMPLEMENTATION_AND_ACCEPTANCE.md` untuk perubahan, verifikasi dan gate terbuka.
Repository: https://github.com/degitalintelligence/ai-factory
Baseline main: `48dce5206a9de8fbf36950f8a2fd6bd828695972`.
Tujuan v0.4: Dedi bisa melanjutkan pekerjaan lewat percakapan, mendapat hasil yang terverifikasi dalam budget, dan menyetujui satu perbaikan diri yang manfaatnya terbukti.

## 1. Kesimpulan review

v0.3 sudah memiliki fondasi Chief of Staff: intake Telegram/API, bounded skill DAG, Decision Inbox, memory dengan provenance, model-run audit, engineering pipeline, dan proposal improvement. Enam domain terdaftar; domain selain engineering masih berupa analysis/draft, bukan connector bisnis yang mengeksekusi tindakan eksternal.

Dokumen release mencatat penerimaan operator pada 8 Oktober untuk candidate `6e64fae27e3eab0fa233237cf9384f469fb2ada0`. Main saat review sudah maju menjadi `48dce520...`, dengan commit pencatatan acceptance. Dokumen tersebut menyebut golden intent #77 selesai, 5 model runs / 41.432 tokens, tiga findings dan decision cards; persistence setelah restart juga dicatat. Ini bukti dalam repo, bukan pembacaan ulang task runtime pada sesi review ini.

CI main terbaru: https://github.com/degitalintelligence/ai-factory/actions/runs/37742744982. Hasil run success; hasil lokal dan cakupan CI dirangkum di bagian verifikasi.

Fondasi ada, tetapi penerimaan v0.3 tidak sama dengan seluruh gate selesai. Self-improvement sampai controlled release dan pengukuran outcome belum dibuktikan; beberapa aksi lintas kanal masih hanya punya bukti mocked test.

## 2. Temuan yang menentukan prioritas

| Temuan | Bukti source | Implikasi |
|---|---|---|
| Intake natural belum menjadi percakapan berkelanjutan yang eksplisit | `app/chat.py`: aksi decision/clarification memakai regex dan ID; pesan lainnya membuat intent. `ChatRequest` tidak mempunyai conversation_id atau reply_to_intent_id | “lanjut yang tadi” berisiko menjadi task baru atau ambigu. Retrieval history bukan pengikat sesi/otorisasi |
| waiting_input belum selalu membuat clarification card | `app/staff.py`: missing_information dan proyek engineering yang kosong langsung transition waiting_input; `app/main.py` menganggap state ini decision_required | Status task dan Inbox dapat memberi pengalaman yang tidak konsisten |
| Budget calls sebagian sudah diperbaiki; problem token perlu baseline live | `plan_budget_accounting`, admission/repair dan lifetime budget sudah ada. Release record intent #80: used 77.264, next reserve 50.417, limit 120.000 | Jangan sekadar menaikkan kuota atau menulis ulang allocator. Ukur rendered context, reservation dan token aktual sebelum memperbaiki admission |
| Confidence repair sudah ada, namun bukti live generik masih lemah | `repair_confidence`, `repair_review`, deterministic reference/confidence checks; release intent #79 ditolak | Guard bekerja, tetapi task gagal tetap belum memberi hasil berguna. Perbaiki evidence selection dan repair, jangan menurunkan evaluator |
| Compact output sengaja dibatasi | `CompactFinding`: 1–4 evidence refs; JSON output maksimal 7.000 karakter | Retry bukan alasan otomatis memperbesar output. Uji pemilihan refs, kebutuhan claim coverage dan error diagnostics |
| Self-improvement end-to-end belum terbukti | Acceptance record dan `AGENTS.md` §19: self-target registry masih OPEN | Perlu verifikasi registry runtime; jangan menganggap alias self untuk audit sudah memberi otorisasi self-edit |
| Metrik success rate belum memisahkan hasil akhir dari task aktif | `/v1/metrics`: completed/reviewed/deployed dibagi semua tasks, termasuk pekerjaan aktif/menunggu; orchestration handoff juga dapat completed | Angka ini belum cukup untuk menilai keberhasilan bisnis atau kualitas coding |
| Dokumen status tertinggal | README masih judul V0.2; AGENTS sebagian menyebut v0.3 belum implemented, sementara acceptance mencatat operator release | Risiko planning berulang dan roadmap dianggap gap implementasi. Sinkronkan status tanpa menutupi gate yang tertunda |

Review ini fokus arsitektur, intake, workflow staff, schema, acceptance, dan verifikasi; bukan audit keamanan menyeluruh atau bukti konfigurasi VPS terkini.

## 3. Milestone yang direkomendasikan

### M0 — Baseline dan tutup utang acceptance v0.3

Deliverables:
- Manifest versi: exact main SHA, deployed SHA, model alias/resolved model, registry snapshot, gate yang selesai/tertunda.
- Sinkronkan README/AGENTS/status requirement dengan implementation dan acceptance record.
- Bekukan corpus regression: intent #77–#80 dari evidence runtime yang berizin, engineering Task #50–#62 yang fixtures-nya sudah tersedia, plus variasi percakapan.
- Pisahkan terminal completion, engineering handoff, blocked, budget stop, dan output yang diterima operator pada metrik.

Acceptance:
- Dashboard/report tidak menyebut child engineering selesai hanya karena parent selesai handoff.
- CI, real PostgreSQL concurrency, sandbox smoke dan Compose/build pass pada candidate yang sama.
- Live revision/rejection/defer dan memory correction/lock diuji di Telegram dan dashboard.
- Baseline tokens, calls, elapsed time, schema retries dan failure_stage dicatat per workflow, tanpa mereset lifetime usage.

### M1 — Reliability dan evidence/budget

Deliverables:
- Planning admission memakai estimasi prompt yang benar-benar dirender, dependency outputs, review, synthesis dan retry yang dibatasi. Estimasi berbeda dari reservation dan actual usage.
- Budget envelope ditampilkan sebelum eksekusi; tidak ada widening kuota, perubahan model atau pengurangan gate secara diam-diam.
- Context dipilih per step/skill dengan evidence manifest. Reference harus authorized, fresh dan relevan. Ringkasan turunan tidak dipromosikan menjadi bukti primer.
- Error schema menunjuk field dan batas yang dilanggar; bounded repair memilih evidence refs paling relevan dan menghapus klaim yang tidak didukung.
- waiting_input mempunyai clarification card durable yang terhubung ke task, dapat dijawab lintas kanal, dan ditutup/resolved secara idempotent.

Acceptance:
- Replay workflow yang menyerupai #80 bisa ditolak sebelum eksekusi jika tidak muat, atau selesai dalam budget setelah optimasi; tidak mati mendadak karena tahap review yang tidak diperhitungkan.
- Klaim tanpa bukti tetap ditolak setelah repair. Tidak ada silent confidence clamp untuk menyelamatkan klaim salah.
- Duplicate clarification, restart, dan jawaban melalui dua kanal tidak menggandakan kartu/task/continuation.
- Bandingkan biaya total termasuk retry dan failed calls, bukan hanya harga nominal model.

### M2 — Continuity percakapan

Deliverables:
- Conversation/thread terikat tenant, owner dan project, dengan hubungan ke task, hasil, blocking question dan decision.
- Routing membedakan diskusi/informasi, goal baru, follow-up, klarifikasi, revisi dan keputusan. Pertanyaan sederhana tidak otomatis membuat DAG penuh.
- API menyediakan conversation_id dan reply_to_intent_id; Telegram mengikat reply ke task/card bila tersedia. “Lanjut yang tadi” hanya otomatis bila target tunggal, authorized dan jelas.
- Bila ada beberapa task relevan, minta memilih target. Persetujuan tetap terikat objek dan plan digest; percakapan tidak menjadi sumber otorisasi tambahan.
- Progress/result disajikan sebagai percakapan dengan next action, evidence drill-down dan child-task link.

Acceptance:
- “Kenapa gagal?”, “jawab pertanyaan tadi”, dan “revisi rekomendasi kedua” melanjutkan objek yang benar dalam thread yang jelas.
- “Setuju” pada dua kartu terbuka meminta disambiguation; tidak mengeksekusi keputusan.
- Pergantian proyek dan user tidak membocorkan context atau memilih task lain.
- Restart mempertahankan hubungan percakapan; duplicate delivery tidak menggandakan aksi.
- Dataset minimal 20 skenario conversation memiliki expected target dan expected authority; semua kasus isolation/approval harus lolos.

### M3 — Satu self-improvement yang terukur

Deliverables:
- Baca registry runtime dan verifikasi self-target khusus ai-factory, exact base SHA, project policy, budget dan approval. Bila belum disetujui, siapkan konfigurasi usulan untuk Dedi.
- Pilih satu defect sempit dari M1 yang punya regression dan benefit measurement jelas.
- Jalankan observe → proposal → approval → branch/sandbox → tests → independent review → PR → controlled merge/release → smoke → outcome → retained lesson.
- Retain, reject atau rollback ditentukan dari bukti before/after. Production rollback tetap compatible, approved dan confirmed.

Acceptance:
- Satu proposal punya provenance lengkap dari failure sampai outcome, exact SHAs dan evidence.
- Perubahan meningkatkan metrik yang ditetapkan sebelum eksekusi tanpa melemahkan review/security/budget.
- Kill switch dan gagal release tidak menghapus historical tasks/evidence.
- Jalur rollback diuji pada staging; tidak mengklaim production rollback terjadi jika hanya proposal/decision dibuat.

### M4 — Pilot penggunaan nyata, kemudian kandidat rilis

Gunakan engineering sebagai use case pertama untuk menyelesaikan satu task kecil pada produk nyata yang dipilih Dedi, setelah sandbox test profile/dependency/policy dikonfirmasi. Kandidat bisnis: LolosATS, karena tujuan pengguna saat ini adalah menambah readiness untuk scale. Jangan memilih issue, mengubah repo, mengirim pesan atau deploy pada sesi perencanaan ini.

Domain non-engineering boleh satu pilot draft dengan data yang berizin. Connector aksi eksternal, WhatsApp, payment dan autonomous publication ditunda ke milestone sesudah kebutuhan dan authority jelas.

Acceptance:
- Parent intent, child engineering, reviewed PR, decision dan deployment state bisa diikuti melalui percakapan yang sama.
- Hasil diukur terhadap acceptance produk, bukan hanya status completed/CI.
- Dedi dapat approve/reject/revise dengan bukti tanpa harus memburu raw logs.

## 4. Definition of Done v0.4 (target usulan)

- Semua gate safety/ownership/idempotency lolos; nol unauthorized action atau evidence leak dalam corpus uji.
- Minimal 10 bounded live objectives dengan fixture/data dan acceptance yang dibekukan; target ≥8 menghasilkan output/PR yang diterima setelah review. Tampilkan semua failures dan alasan, tidak pilih hanya run sukses. Sampel ini release smoke, belum estimasi statistik reliabilitas produksi.
- Minimal 20 conversation cases lolos expected routing dan authority.
- Minimal satu self-improvement end-to-end dengan outcome terukur dan rollback staging.
- Token/call/cost/latency dibandingkan baseline M0 pada input/model/budget yang sebanding. Target optimasi token awal 25% adalah hipotesis; quality/security tidak boleh turun dan target disahkan setelah baseline.
- Tidak ada gap acceptance wajib yang disembunyikan oleh label released.
- Dedi menyetujui exact production SHA; migration additive, backup dan rollback compatibility tersedia.

## 5. Urutan delivery dan scope

Urutan: M0 → M1 → M2 → M3 → M4. Pecah tiap defect atau kontrak menjadi PR kecil dengan regression yang bermakna; jangan satu PR besar v0.4. M1 sebelum M2 karena percakapan yang nyaman tidak menyelesaikan workflow yang sering gagal.

Estimasi awal 2–4 minggu pengerjaan aktif jika endpoint staging, runtime evidence, operator decisions dan dependency testing tersedia. Ini planning range, bukan janji deadline; gate live/provider dapat mengubah durasi. Tidak memerlukan penambahan Redis/vector DB/microservice atau pergantian orkestrator untuk scope ini.

Deferred: marketplace agent, banyak connector sekaligus, multi-organization SaaS onboarding, self-modifying permissions, auto-merge/production deploy, model free sebagai solusi tunggal. Model alternatif bisa dievaluasi terpisah memakai corpus yang sama, total retry/cost dan output acceptance; hasil model murah tidak diasumsikan setara.

Keputusan awal yang perlu ditentukan sebelum implementasi terkait: konfirmasi self-target policy runtime; pilih satu pilot produk; sepakati budget live acceptance yang terbatas. Rekomendasi pertama adalah PR kecil clarification-card parity, sambil mengumpulkan baseline token yang menyerupai intent #80.

## 6. Evidence links

- Baseline commit: https://github.com/degitalintelligence/ai-factory/commit/48dce5206a9de8fbf36950f8a2fd6bd828695972
- Acceptance: https://github.com/degitalintelligence/ai-factory/blob/48dce5206a9de8fbf36950f8a2fd6bd828695972/docs/V03_IMPLEMENTATION_AND_ACCEPTANCE.md
- Chat: https://github.com/degitalintelligence/ai-factory/blob/48dce5206a9de8fbf36950f8a2fd6bd828695972/app/chat.py
- Workflow: https://github.com/degitalintelligence/ai-factory/blob/48dce5206a9de8fbf36950f8a2fd6bd828695972/app/staff.py
- Schema: https://github.com/degitalintelligence/ai-factory/blob/48dce5206a9de8fbf36950f8a2fd6bd828695972/app/staff_schemas.py
- Metrics: https://github.com/degitalintelligence/ai-factory/blob/48dce5206a9de8fbf36950f8a2fd6bd828695972/app/api_v03.py
- CI: https://github.com/degitalintelligence/ai-factory/actions/runs/37742744982

## 7. Verifikasi sesi review

- Full pytest lokal: **546 passed, 3 skipped**, 38,92 detik. Tiga skips adalah PostgreSQL integration tanpa TEST_POSTGRES_URL; satu collection warning untuk class TestReport, bukan kegagalan test.
- Ruff check: pass. Ruff format check: **57 files already formatted**.
- CI exact main SHA: engine, sandbox dan compose masing-masing **success**.
- PostgreSQL/Docker live bukan dijalankan lokal pada sesi ini; CI menjadi bukti lingkungan tersebut. Tidak ada source repo, PR, merge, deploy atau runtime policy diubah.
