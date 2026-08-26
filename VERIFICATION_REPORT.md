# job-apply-os — Báo cáo verify (đối chiếu 2 PDF + code + schema)

Ngày verify: 2026-07-31. Nguồn: 2 file PDF ("Tối ưu hóa kiến trúc hệ thống" — 88 trang, "Hoàn thành pipeline và push lên GitHub" — 158 trang) + đọc trực tiếp `services/`, `db/migrations/`, `docker-compose*.yml`, và chạy thử code trong sandbox (không có Docker/root nên không dựng được Postgres thật — xem mục Test ở cuối).

Sơ đồ đã đánh dấu lại: `diagrams/jobos_master_status_v2.mmd` (nguồn) và `diagrams/jobos_master_status_v2.html` (mở bằng trình duyệt là xem được ngay, không cần cài gì).

> **Cập nhật 2026-07-31 (round 2):** đã code thật các phần "[~] có code chưa nối dây" (trừ submit ở L7) + 4 bug đã liệt kê + ATS discovery đa nền tảng. Xem mục **9** ở cuối file cho chi tiết đầy đủ, danh sách file thay đổi, và cách test lại trên máy m.

---

## 1. Vấn đề trong PDF "Tối ưu hóa kiến trúc hệ thống" — đã sửa chưa?

| # | Vấn đề Claude nêu | Trạng thái | Bằng chứng |
|---|---|---|---|
| 1 | Browser Lock giữ xuyên qua thời gian chờ người (deadlock) | **Sửa theo hướng khác** | Không tách segment A/B như đề xuất, nhưng thực chất hoá bằng `lease_expires_at` + `FOR UPDATE SKIP LOCKED` trong `browser_queue_worker.py`, watchdog reap lease hết hạn. Chưa xác nhận có tách "thả lock giữa chừng khi chờ người duyệt" đúng như thiết kế segment A/B. |
| 2 | Approval token không bind nội dung (approve v1, submit v2) | **Chưa xác nhận đầy đủ** | `approval_service_v1.py` đã có sha256 hash + TTL + one-time + `hmac.compare_digest` + lockout — tốt hơn thiết kế gốc trong sơ đồ. Nhưng công thức cụ thể `HMAC(job_id, action, sha256(resume+cover+form))` mà PDF đề xuất — chưa thấy bằng chứng token bind vào nội dung tài liệu cụ thể. **Rủi ro còn treo lơ lửng, nên kiểm tra lại thủ công.** |
| 3 | QA revise loop không giới hạn → đốt tiền vô hạn | **Cải thiện một phần** | `v_documents_pending_qa` (migration 034) đã lọc `qa_status='revise'`... nhưng thực tế đọc code thấy view này **VẪN CHỨA** `qa_status='revise'` trong điều kiện WHERE và **KHÔNG có** `NOT EXISTS(...revision_of...)` — nghĩa là cơ chế chặn vòng lặp revise vô hạn ở tầng DB view **CHƯA được áp dụng đúng như bàn trong PDF 2**. `revision_round` có track nhưng chưa thấy chặn cứng "≤2" trong `orchestrator_v1.py`. → **Cần làm lại đúng như đã bàn.** |
| 4 | L4 over-engineering (RAG stack cho có 30-40 file của 1 người) | **Không theo hướng PDF đề xuất, nhưng đi hướng khác hợp lý** | PDF đề xuất gộp về 1 file `profile.yaml`. Thực tế code build hẳn 1 pipeline DB mới (`profile_documents → profile_document_sections → profile_evidence_units → profile_assets → profile_capabilities`) kèm lớp an toàn `jobos_safety.py` (word-boundary, downgrade-only) mà PDF ban đầu không hề có. Đây là hướng đi **nặng hơn** đề xuất của PDF nhưng bù lại **an toàn hơn nhiều** — có test pass 13/13. Đánh giá: chấp nhận được, nhưng đúng là còn phức tạp hơn mức cần thiết cho 1 người dùng. |
| 5 | n8n sai công cụ cho state machine sống lâu | **ĐÃ SỬA ĐÚNG HƯỚNG** | `orchestrator_v1.py` là state machine Python thật, dùng Postgres làm nguồn sự thật (`pipeline_steps`/`pipeline_transitions`/`pipeline_events` — migration 035). n8n container **vẫn còn khai báo trong `docker-compose.yml`** dù không dùng nữa — nên gỡ bỏ hẳn để khỏi gây nhầm lẫn. |
| 6 | LinkedIn automation = rủi ro ban vĩnh viễn | **THIẾT KẾ ĐÃ THAY ĐỔI** | Production hiện có user-triggered LinkedIn browser discovery trên profile đã đăng nhập thủ công, cùng FakeMouse/CapSolver helper được giữ lại. Không được mô tả repo là “không có LinkedIn automation”; application submit vẫn nằm sau Human Approval Bus riêng. |
| 7 | Prompt injection qua JD/company site | **Đã có phần chặn** | `company_research_v1.py` có `validate()` chặn claim tự nguồn (self-sourced) và bắt buộc `supporting_quote`. Nhưng chưa thấy tài liệu/khẳng định rõ ràng "agent xử lý nội dung ngoài không được cầm tool có tác dụng phụ" ở tầng OpenClaw config (`.openclaw/openclaw.json` — nằm ngoài repo này, m nói sẽ đưa sau). **Chưa verify được phần OpenClaw permission tách lớp quyền.** |
| 8 | Gmail watch() hết hạn sau 7 ngày, thiếu verify OIDC | **Không áp dụng nữa** | Toàn bộ hướng Gmail Pub/Sub + Tailscale Funnel đã bị cắt theo đúng quyết định trong PDF (không tìm thấy code liên quan). Nhưng hướng thay thế (cron polling 60s) **cũng chưa được xây** — Gmail intake hiện tại = 0%. |
| 9 | Thiếu observability / trace_id | **CHƯA SỬA** | Không tìm thấy `trace_id` xuyên suốt các script. Có 36-40 chỗ `except Exception` chỉ `print()` rồi bỏ qua — rất khó debug khi lỗi xảy ra âm thầm. |
| 10 | Secrets trong Docker volume `.openclaw` | **Chưa thể verify** | `.openclaw` chưa có trong repo này theo đúng như m nói — bỏ qua phần này theo yêu cầu. |
| 11 | Idempotency key `UNIQUE(company_id, job_id)` chống nộp trùng | **PARTIAL** | `applications` có dedup theo `jd_hash` lúc **intake**, nhưng không thấy idempotency check riêng ngay **trước khi submit thật** như PDF đề xuất (đây là 2 việc khác nhau — dedup đầu vào không chống được trường hợp retry sau timeout gây nộp trùng ở bước submit). |
| 12 | 5 lỗi bảo mật cụ thể phát hiện sau (C1/C2/H1-H4/M1/M4/L1-L4...) — xem PDF 2 | Xem bảng mục 2 bên dưới | — |

---

## 2. Vấn đề/bug cụ thể trong PDF "Hoàn thành pipeline và push lên GitHub"

### Đã sửa thật (có bằng chứng code + **test tự động PASS**)

- **C1 — model không được nâng cấp evidence_strength**: sửa trong `jobos_safety.py` (`apply_downgrades`, downgrade cho cả `direct_lab_use` lẫn `project_use`, không chỉ 1 cái như bug cũ).
- **C2 — regex match kiểu substring** (`"lab" in "collaborate"`): sửa bằng word-boundary regex `_wb()` trong `jobos_safety.py`.
- **H2 — `parse_docx` mất hết heading structure**: đã vá, đọc `para.style.name`, giữ `# Heading` + `@@FIELD@@`.
- **H3 — `do_not_overclaim_rules` lẫn enum trần** (`direct_lab_use` lọt vào rules): đã sửa bằng `as_overclaim_rule()` trong `jobos_safety.py`, có self-test ngay trong file.
- Cả 4 nhóm test trên **PASS 100% khi tôi chạy `test_safety_regression.py` thật trong sandbox** (13/13 assertion) — đây là bằng chứng mạnh nhất, không phải chỉ đọc code suông.
- **Company research self-source/supporting_quote guard**: theo PDF 2 thì việc này **bị hoãn** ("để sau"), nhưng khi tôi đọc code thật thì `validate()` trong `company_research_v1.py` **đã có** cả 2 cơ chế này rồi — tức là đã làm thêm **sau thời điểm** cuộc trò chuyện trong PDF 2 kết thúc. Tin tốt.
- **`ON CONFLICT` partial-index bug** (cost_controller, message_reply): đã sửa đúng, có `WHERE component_run_id IS NOT NULL` / xử lý riêng nhánh `external_id NULL`.
- **`docs_generated` chọn document mới nhất thay vì document PASS**: đã sửa, `ORDER BY (qa_status='pass') DESC, created_at DESC`.
- **`current_step` bị 2 script cùng ghi đè (race)**: đã sửa, `analyze_job_fit_v1.py` giờ chỉ đọc, chỉ orchestrator ghi (trừ 1 chỗ set literal lúc INSERT dòng mới — chấp nhận được).
- **pgvector chưa có index, `embedding` kiểu bare `vector`**: đã sửa ở migration `039_vector_index.sql`, `vector(768)` + ivfflat, tôi verify migration này parse đúng cú pháp SQL.

### Chưa sửa / còn treo — cần làm tiếp

- **Migration numbering hỗn loạn** (`007/007_fixed/007a`, `024/024a`, `025/025a/025b`, hai bản `030_*`, ba bản `031_*`): **vẫn y nguyên**, chưa gộp thành baseline. Nguy hiểm thật sự: `031_profile_asset_audits_structured_workflow.sql` cố `CREATE INDEX` trên cột không tồn tại ở schema thật (do bảng `profile_asset_audits` đã được tạo trước đó ở migration 027 với schema khác) — **nếu m từng cài lại DB từ đầu (fresh install), bước này sẽ lỗi**. Cần dọn dẹp sớm.
- **`v_profile_asset_deepseek_review` vẫn hardcode literal** `audit_version = 'deepseek_structured_asset_audit_v1_2026_04_27'` — đúng bug PDF 2 nói, **chưa sửa**. Nếu chạy lại audit với version string mới, view này im lặng trả về 0 dòng — rất dễ tưởng nhầm là "không có gì cần duyệt".
- **`model_routing_policies`** vẫn để `deepseek-r1:14b` + `local_only=true` cho việc `profile_grounding_overclaim_auditor` — đúng là nguyên nhân từng khiến 1 lần audit chạy hơn 6 tiếng (RAM 16GB bị thrash). PDF 2 nói sẽ đổi sang `deepseek-chat` (cloud) nhưng **migration/code chưa phản ánh việc này** — có thể đã đổi thủ công ngoài migration (không kiểm tra được từ code).
- **QA revise loop chưa thật sự chặn** (xem mục 1.3 ở trên) — ưu tiên sửa vì đây là rủi ro tốn tiền thật.
- **`allowed_domains` không được `browser_queue_worker.py` enforce** — chỉ `autofill_agent_v1.py` gọi `check_domain()`. Nghĩa là whitelist domain **không phải lớp chặn thống nhất** như sơ đồ mô tả — có lỗ hổng ở đúng điểm chạm OpenClaw thật sự.
- **`promote_clean_facts_status_only_DO_NOT_USE.py`** vẫn nằm ngay trong `services/profile-ingestion/` (chưa dọn vào thư mục deprecated) — vẫn là "bom nổ chậm" nếu ai đó chạy nhầm.
- **VERSION const vẫn hardcode string tay**, không tự hash theo source code như PDF 2 định làm — rủi ro "sửa logic mà quên bump version" vẫn còn nguyên.
- **~36-40 chỗ `except Exception` nuốt lỗi** — chưa dọn.
- **Mật khẩu DB hardcode lặp lại ở ~36 file** — chưa có module config dùng chung (`services/common/` mới chỉ có `jobos_safety.py`).
- **L8 reply truth-checker**: đúng như PDF 2 dự đoán, **vẫn chưa viết** — L8 gửi tin nhắn hoàn toàn không qua bước kiểm tra sự thật như L6.

---

## 3. Đối chiếu sơ đồ (L0 → L9) — đã đánh dấu lại

File: `diagrams/jobos_master_status_v2.mmd` / `.html` (mở file `.html` bằng trình duyệt là thấy ngay, có chú thích màu ở góc trên). Quy ước:

- `[x]` = đã verify chạy thật (đọc code + có bằng chứng thực thi, ví dụ test pass hoặc `--help` chạy được)
- `[~]` = có code nhưng CHƯA nối dây tự động, hoặc chỉ chạy được qua CLI thủ công
- Không nhãn = 0 dòng code, chưa xây
- **Mờ + nét đứt** = đã quyết định BỎ hoặc bị thiết kế khác thay thế (vẫn giữ trên sơ đồ để nhớ lý do)

Tóm tắt số lượng theo layer (đếm node, không tính LEGEND):

| Layer | Đã xong `[x]` | Có code, chưa nối dây `[~]` | Chưa xây | Đã bỏ / thay thế |
|---|---|---|---|---|
| L0 | 1 (Raw Files) | 0 | 2 (TG, EM) | 1 (LinkedIn) |
| L1 | 2 (APRV, RULE) | 2 (COST, INTAKE) | 1 (WH) | 1 (n8n-là-orchestrator) |
| L2A | 6 | 1 (interviews) | 0 | 1 (profile_facts cũ) |
| L2B | 3 | 1 (screenshots) | 0 | 0 |
| L3 | 3 (BQ, BLock, WD) | 1 (Browser Controller whitelist) | 0 | 1 (OpenClaw-Docker) |
| L4 | 8 (FTC, PARSE, CHUNK, VEC, CLM, BRGEN, PPB, SAFE-mới) | 2 (FG, STALE) | 0 | 2 (DED, Conflict Resolver) |
| L5 | 1 (FIT) | 4 (FGO, SEA, HTTP, SAVE) | 0 (RR/SG coi như missing) | 0 |
| L6 | 3 (RES, QA, QGO) | 4 (WORK, COV, SHA, RTRY, HREV) | 0 | 0 |
| L7 | 4 (AF, SEN, FSG, PAUSE) | 1 (SUB — có chủ đích) | 0 | 0 |
| L8 | 3 (MCL, CTX, REP) | 2 (SGO, NR) | 2 (SMTP, BST) | 0 |
| L9 | 0 | 0 | 3 (IGO thực chất chết, PREP, PPKG) | 0 |

**Đọc nhanh: phần lõi nộp đơn xin việc (profile → fit → viết doc → verify → autofill draft) đã chạy được thật, có test xác nhận. Phần vòng ngoài (Telegram UX, Gmail intake, gửi tin nhắn ra ngoài, interview prep) gần như chưa xây — đúng như 2 PDF đã dự đoán, đây chính là hướng ưu tiên tiếp theo.**

---

## 4. Những chỗ đã quyết định BỎ trong code (giữ trên sơ đồ, mờ đi)

1. **n8n làm orchestrator** — thay bằng Python state machine (`orchestrator_v1.py`). *Lưu ý: container n8n vẫn chạy trong `docker-compose.yml`, nên dọn hẳn.*
2. **LinkedIn automation** — quyết định cũ “bỏ hẳn” không còn phản ánh production. Hiện repo giữ bounded user-triggered LinkedIn discovery và helper FakeMouse/CapSolver; không tự động login và final Submit vẫn là privileged human-approved action.
3. **profile_facts / candidate_profile_facts (pipeline atom-fact cũ)** — deprecated ở migration 026, thay bằng `profile_assets`.
4. **Semantic Dedup (bảng `candidate_fact_dedup_*`)** — thuộc pipeline cũ, không có tương đương trong pipeline mới.
5. **Conflict Resolver** — bỏ đúng như PDF đề xuất, thay bằng review tay qua `approve_drafts.sql`.
6. **OpenClaw chạy trong Docker** (`docker-compose.openclaw.yml`) — thực tế OpenClaw chạy **native** trên máy (không phải container), tốt nhất dưới một OS user riêng với browser profile/tab riêng. File compose này giờ là fallback containerized setup, không phải đường chính.

---

## 5. Những chỗ đã CẢI THIỆN so với sơ đồ gốc (không có trong PDF gốc)

- **`jobos_safety.py`** — lớp an toàn dùng chung hoàn toàn mới, không hề có trong sơ đồ/PDF gốc. Đây là điểm sáng nhất: word-boundary regex, quy tắc "chỉ được hạ cấp, không được nâng cấp" độ mạnh bằng chứng, và validate câu do-not-overclaim. Có bộ test riêng, chạy PASS 100%.
- **`profile_assets` pipeline** thay thế atom-fact — nặng hơn `profile.yaml` mà PDF1 đề xuất, nhưng bù lại theo dõi được `tier`/`ownership` (built/contributed/studied/exposed) đúng tinh thần PDF1 muốn (dù không đúng cấu trúc file phẳng như PDF1 gợi ý).
- **Approval Service** — an toàn hơn thiết kế gốc: sha256 hash (không lưu token thô), `hmac.compare_digest` chống timing attack, cơ chế khoá sau N lần thử sai (`attempt_count`/`max_attempts`) — chi tiết này PDF gốc không hề nhắc tới.
- **Error-recovery (migration 036)** — phân biệt lỗi tạm thời (transient, ví dụ Ollama sập) với lỗi vĩnh viễn, tránh việc 1 lỗi mạng đẩy cả application vào trạng thái `error` chết cứng.
- **Company research `validate()`** — chặn claim tự nguồn (company tự nói về rủi ro của chính mình) + bắt buộc trích dẫn — vượt cả những gì PDF 2 đã bàn tới lúc kết thúc hội thoại.
- **Autofill an toàn cứng** — xác nhận rõ ràng: không có bất kỳ hàm nào trong `autofill_agent_v1.py` có khả năng bấm nút Submit. Đây đúng là "draft-only first" như sơ đồ mô tả, làm nghiêm túc chứ không phải chỉ ghi chú.

---

## 6. Đề xuất cải thiện theo hướng innovation (chưa làm, nên cân nhắc)

Xếp theo độ ưu tiên/đòn bẩy (impact vs effort), dựa trên đúng logic 2 PDF đã phân tích:

1. **Telegram digest 1 lần/ngày (batch approve)** — đây là điểm PDF1 nói rõ nhất: 2 nút thắt cổ chai lớn nhất (con người duyệt, browser lock) không phải vấn đề kỹ thuật mà là vấn đề UX duyệt. Hiện tại 100% chưa xây (chỉ có CLI). Đây là việc có đòn bẩy cao nhất để hệ thống thật sự "tự động" theo đúng nghĩa m muốn.
2. **Discovery qua ATS API công khai (Greenhouse/Lever/Ashby)** — thêm một đường intake công khai, không cần key, rate-limit nhẹ. Nó là option bổ sung, không phải thay thế duy nhất cho mọi cách lấy JD. Hiện tại hệ thống chỉ nhận JD qua `--jd-file` thủ công — chưa có nguồn job tự động nào cả.
3. **Dọn migration numbering** — `pg_dump --schema-only` ra 1 file baseline, xoá hết chuỗi `_fixed/a/b`, đánh số lại tuần tự. Rủi ro thật: cài lại từ đầu sẽ vỡ ở migration 031.
4. **Fix `v_profile_asset_deepseek_review`** dùng `DISTINCT ON (profile_asset_id) ORDER BY created_at DESC` thay vì hardcode `audit_version` — nếu không sửa, mỗi lần đổi model audit là hệ thống "mù" im lặng.
5. **Enforce QA revise cap thật sự** ở cả DB view lẫn `orchestrator_v1.py` (`max_revise = 2` cứng) — đúng như PDF1 cảnh báo "đốt tiền vô hạn".
6. **`allowed_domains` phải được enforce tại đúng điểm chạm OpenClaw** (trong `browser_queue_worker.py`, không chỉ trong `autofill_agent_v1.py`) — đây là lỗ hổng an toàn thật, không chỉ là thiếu sót thẩm mỹ.
7. **Trace_id + structured logging** xuyên suốt pipeline (`trace_id, stage, duration_ms, tokens_in, tokens_out, cost`) — hiện tại debug gần như "bằng niềm tin" đúng như PDF1 nói, vì có 36-40 chỗ nuốt lỗi âm thầm.
8. **Token duyệt bind nội dung** — implement đúng công thức PDF1 đề xuất: `HMAC(job_id, action, sha256(resume+cover+form))`, để tránh trường hợp duyệt bản v1 nhưng hệ thống nộp bản v2 sau vòng revise.
9. **Idempotency check ngay trước submit** (`UNIQUE(company_id, job_id)`), tách biệt với dedup lúc intake — chống nộp trùng khi retry sau timeout.
10. **Dọn dead weight**: xoá/comment `n8n` khỏi `docker-compose.yml`, xoá hoặc đánh dấu rõ `docker-compose.openclaw.yml` là không dùng, dọn `promote_clean_facts_status_only_DO_NOT_USE.py` vào thư mục deprecated.
11. **Module config DB dùng chung** trong `services/common/` — giảm rủi ro khi đổi mật khẩu (hiện lặp ở ~36 file).
12. **Wire nốt L8 truth-checker** trước khi bật tính năng tự động trả lời tin nhắn — nếu không, tin nhắn gửi ra ngoài không được kiểm tra sự thật như CV/cover letter, không nhất quán về nguyên tắc an toàn của cả hệ thống.
13. **Xoá `triggers_l9`** (cờ chết, không ai đọc) hoặc bắt tay xây L9 thật — hiện tại là code half-baked gây rối, nên chọn 1 trong 2.

---

## 7. Về OpenClaw / `.openclaw`

Theo đúng yêu cầu, tôi **bỏ qua** việc thêm `.openclaw` (openclaw.json, config, workspace) vào lúc này. Ghi nhận: `docker-compose.openclaw.yml` hiện có trong repo nhưng theo PDF 2, OpenClaw thực tế chạy **native trên máy** (không phải Docker) — nên file compose này giờ chỉ là fallback. Hướng an toàn nên dùng là: (1) chạy native dưới một OS user riêng, (2) tách browser profile/tab cho agent, (3) nếu không tách được thì mới dùng container fallback. `.gitignore` giờ đã chặn `.openclaw/` và workspace/config local để tránh lộ secrets khi đưa folder này vào repo.

---

## 8. Kết quả chạy thử / test thật trong sandbox

Không có Docker và không có quyền root trong môi trường chạy việc này, nên **không dựng được Postgres+pgvector thật** để chạy end-to-end toàn bộ pipeline. Đã làm mọi việc có thể làm được mà không cần DB:

- **Cài `services/profile-ingestion/requirements.txt`, `document-generation/requirements.txt`, `browser-controller/requirements.txt`** vào venv sạch — **cài thành công, không lỗi dependency**.
- **`python3 -m py_compile`** trên toàn bộ 38 file Python trong `services/` — **100% compile OK**, không lỗi cú pháp.
- **`test_safety_regression.py`** (bài test không cần DB, không cần Ollama) — **chạy thật, PASS 13/13** assertion, xác nhận các bug C1/C2/H3 nêu trong PDF 2 đã thật sự được vá trong code, không chỉ là lời hứa.
- **Chạy `--help`** trên 10 script chính (`orchestrator_v1.py`, `approval_service_v1.py`, `cost_controller_v1.py`, `analyze_job_fit_v1.py`, `company_research_v1.py`, `generate_documents_v1.py`, `verify_document_truth_v1.py`, `autofill_agent_v1.py`, `message_reply_v1.py`, `browser_queue_worker.py`) — **tất cả chạy OK**. Riêng `watchdog.py` và `ingest_files.py` không có cờ `--help`, chạy thẳng và dừng đúng chỗ mong đợi: cố kết nối Postgres tại `127.0.0.1:5433` rồi báo `Connection refused` — nghĩa là code đọc đúng config, chỉ thiếu DB thật, không phải lỗi code.
- **Parse cú pháp SQL bằng `pglast`** (dùng grammar PostgreSQL thật qua `libpg_query`, không cần server) trên toàn bộ 45 file trong `db/migrations/` + 4 file `.sql` trong `services/profile-ingestion/` — **100% parse OK, 0 lỗi cú pháp**. (`approve_drafts.sql`/`verify.sql` báo lỗi ở lệnh `\echo` — đây là lệnh riêng của `psql`, không phải SQL chuẩn, không phải bug thật.)

### Để m tự chạy thật 100% trên máy (có Docker):

```bash
cd job-apply-os
docker compose up -d postgres      # chỉ cần postgres, bỏ n8n nếu đã quyết định không dùng
# apply migrations theo đúng thứ tự file (xem cảnh báo mục 6.3 về migration 031)
for f in db/migrations/*.sql; do
  docker exec -i jobos-postgres psql -U jobos -d job_apply_os < "$f"
done
python3 test_safety_regression.py                 # test không cần DB, nên chạy trước
python3 services/profile-ingestion/ingest_files.py # ingest 41 file thật trong data/profile_raw/
python3 services/orchestrator/orchestrator_v1.py --help
```

**Kết luận test**: code ở mức "chạy được" (deps sạch, syntax sạch, entrypoint đúng, logic an toàn cốt lõi có test pass thật) — cái còn thiếu duy nhất để xác nhận 100% là một lần chạy full end-to-end với Postgres thật, việc này cần Docker nên phải làm trên máy của m, không làm được trong sandbox này.

---

## 9. Round 2 — đã code thật những gì

### 9.1 4 bug đã yêu cầu sửa

1. **Migration numbering nguy hiểm** — `db/migrations/031_profile_asset_audits_structured_workflow.sql` sửa lại tại chỗ: file gốc giả định `profile_asset_audits` chưa tồn tại và cố `CREATE INDEX` trên 2 cột (`audit_status`, `recommended_action`) không hề có trên bảng thật (bảng thật được tạo ở migration 027 với schema khác hẳn) — trên 1 lần cài fresh, bước này sẽ **lỗi thật** (`column "audit_status" does not exist`) và làm hỏng transaction. Đã sửa để idempotent, khớp đúng schema 027. Thêm `db/migrations/README.md` giải thích rõ lịch sử các số trùng (007/007a, 024/024a, 025/025a/025b, hai bản 030) — không đổi tên các file đó vì có thể đã chạy trên DB của m rồi, đổi tên không sửa được gì mà chỉ gây hiểu lầm; đã kiểm tra từng cặp không có xung đột thứ tự nào khác ngoài 031.
2. **`v_profile_asset_deepseek_review` hardcode `audit_version`** — sửa trong migration mới `041_wiring_fixes_and_gates.sql`: đổi sang `DISTINCT ON (profile_asset_id, audit_type) ... ORDER BY created_at DESC`, lấy audit mới nhất bất kể version string. `v_profile_asset_approval_candidates` và `v_profile_asset_deepseek_audit_summary` tự động ăn theo vì chúng build trên view này.
3. **QA revise-loop chưa chặn cứng** — `v_documents_pending_qa` trước đây còn nhận cả `qa_status='revise'`, khiến 1 document đã chạm `--max-rounds` (mặc định 2, đã có sẵn trong `verify_document_truth_v1.py`) bị đọc lại vô thời hạn mỗi lần `--pending` chạy, tốn API call vô ích. Đã sửa: chỉ còn `qa_status IS NULL` + `NOT EXISTS` (con đã tồn tại thì không đọc lại cha). Retry cap 2 lần bản thân nó đã đúng trong code cũ (`verify_document_truth_v1.py --max-rounds 2`), chỉ có cái view đọc hàng chờ là bị sai.
4. **`allowed_domains` chưa được `browser_queue_worker.py` enforce** — trước đây chỉ `autofill_agent_v1.py` gọi `check_domain()`. Đã thêm `check_domain()`/`load_allowed_domains()` trực tiếp vào `browser_queue_worker.py`, gọi từ `require_url()` — đây là chokepoint DUY NHẤT mọi task (fetch JD, snapshot, fill form) đều đi qua trước khi chạm OpenClaw, nên giờ whitelist domain áp dụng cho toàn bộ L3, không chỉ nhánh autofill.

### 9.2 Nối dây các phần "[~] có code, chưa tự động" (trừ submit L7)

- **Cost Controller (L1)** — `orchestrator_v1.py` giờ gọi `cost_controller_v1.py check --task full_pipeline --increment` ngay trước bước fit-analysis (chi tiêu LLM đầu tiên của mỗi job). Bị chặn budget = coi như lỗi tạm thời, application đứng yên ở `screened` chờ ngày mai/tăng budget, không báo lỗi cứng.
- **STALE gate (L4)** — thêm 2 cặp trigger Postgres (`profile_assets`, `profile_capabilities`) tự động đánh `profile_briefs.is_stale = true` mỗi khi có asset/capability mới hoặc đổi status. Trước đây cờ này không bao giờ tự bật lại sau khi brief được generate.
- **FGO ask_user 60-75 điểm (L5)** — đây là gap nghiêm trọng nhất tìm thấy: `ask_user` và `approve_research` trước đây bị xử lý y hệt nhau, job điểm 60-75 chạy thẳng luôn không hề hỏi ai. Giờ có step mới `awaiting_fit_review`, orchestrator tạo `approval_request` (loại `fit_review`, TTL 48h — đúng công thức PDF1 đề xuất "TTL 24-48h, không phải 15 phút, con người ngủ"), in sẵn lệnh approve/deny, và chỉ tiếp tục khi có quyết định.
- **Research Router (L5)** — `orchestrator_v1.py` giờ **gọi thật** `company_research_v1.py` ở bước `fit_analyzed`, trước khi sinh tài liệu. Best-effort: research fail không chặn resume. Nhân tiện phát hiện và sửa 1 bug thật trong `company_research_v1.py` — dòng gọi `openclaw_agent(..., session_id=session_id)` sẽ crash `TypeError` (hàm không nhận tham số đó) cộng thêm biến `_uuid` không tồn tại — nghĩa là trước đây script này **chưa từng chạy được** dù code "đã xong" theo báo cáo cũ.
- **Cover Letter tự động (L6)** — `orchestrator_v1.py` giờ sinh cả resume lẫn cover letter tự động ở bước `fit_analyzed`, cover letter là best-effort (fail không chặn resume). **Short Answer cố tình KHÔNG tự động hoá** — nó cần `--question` là câu hỏi thật lấy từ form đơn ứng tuyển thật (qua L3/L7), tự động hoá giả câu hỏi sẽ phá vỡ nguyên tắc "chỉ trả lời câu hỏi có thật", nên vẫn chạy tay qua CLI, đây là quyết định thiết kế chứ không phải bỏ sót.
- **SGO approve-send gate (L8)** — `message_reply_v1.py` trước đây chỉ IN ra dòng gợi ý "chạy approval_service_v1.py --type send_message" (mà `--type` thực ra không phải cách gọi đúng của script đó). Giờ `cmd_draft` tự tạo `approval_request` thật (loại `send_message`, TTL 48h) ngay khi lưu draft, in token thật sẵn sàng dùng.

### 9.3 ATS API Discovery đa nền tảng — `services/discovery/ats_discovery_v1.py`

Module mới, adapter riêng cho từng nền tảng, dùng API đọc công khai không cần key:

| Nền tảng | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{slug}/postings` |
| Recruitee | `{slug}.recruitee.com/api/offers/` |
| Workable | `apply.workable.com/api/v1/widget/accounts/{slug}` |
| Breezy | `{slug}.breezy.hr/json` |

Job mới rơi thẳng vào `applications` ở `current_step='intake'` (dedup bằng `jd_hash`, y hệt cơ chế `orchestrator_v1.py` đã có) — nghĩa là No-LLM Filter, cost gate, fit gate... tất cả áp dụng luôn không cần sửa gì thêm. Migration `042_ats_discovery.sql` thêm bảng `ats_companies` (danh sách công ty cần poll) và `ats_discovery_runs` (log mỗi lần poll).

**Quan trọng — KHÔNG seed sẵn công ty nào.** Tôi không có network ra ngoài trong sandbox này để verify slug thật (đã thử curl 5 endpoint, tất cả bị chặn bởi allowlist), nên đoán slug rồi nhét vào DB rủi ro hơn để trống — 1 slug sai có thể trùng tên với công ty khác trên platform khác mà tôi không biết. M tự thêm công ty thật bằng cách xem URL trang tuyển dụng của họ (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, v.v.):

```bash
# Test 1 công ty trước, không ghi DB:
python services/discovery/ats_discovery_v1.py test --platform greenhouse --slug <slug-thật>

# Thêm công ty rồi poll tất cả:
python services/discovery/ats_discovery_v1.py add --company "Tên công ty" --platform greenhouse --slug <slug> --apply
python services/discovery/ats_discovery_v1.py poll --apply
```

**KHÔNG hỗ trợ**: Workday, iCIMS, Taleo, SuccessFactors — các nền tảng này không có API JSON công khai theo pattern slug đơn giản (thường cần POST theo từng tenant, không tài liệu hoá công khai). Thêm công ty dùng các nền tảng này bằng `orchestrator_v1.py intake --jd-file` như trước, đừng tin 1 adapter đoán mò cho chúng.

### 9.4 Mở rộng field-mapping Autofill (L7, không đụng submit)

`FIELD_PATTERNS` trong `autofill_agent_v1.py` mở rộng từ 19 lên 34 pattern — thêm pronouns, county, education (trường/bằng cấp/chuyên ngành/ngày tốt nghiệp), current/desired title, years of experience, referral source, X/Twitter, other URL. Thêm bảng `file_uploads` mới trong plan — nhận diện nút "Upload resume/CV/cover letter" và báo cáo (không bấm, không thể tự động — cần file thật). Migration 041 seed thêm các field placeholder tương ứng trong `applicant_identity`/`sensitive_answers` (mặc định `FILL_ME`/chưa approve) để field mới báo "NO VALUE AVAILABLE" đúng cách thay vì rơi vào "UNRECOGNISED".

Cơ chế match vẫn dựa trên label text (không phải DOM structure riêng từng ATS), nên việc mở rộng pattern này áp dụng ngay cho **mọi** nền tảng, không cần code riêng từng cái.

### 9.5 Danh sách file thay đổi

```
sửa:  db/migrations/031_profile_asset_audits_structured_workflow.sql
sửa:  services/approval/approval_service_v1.py           (+fit_review type)
sửa:  services/autofill/autofill_agent_v1.py              (+field patterns, +file_uploads)
sửa:  services/browser-controller/browser_queue_worker.py (+domain whitelist)
sửa:  services/messaging/message_reply_v1.py               (+auto approval)
sửa:  services/orchestrator/orchestrator_v1.py             (+cost gate, +fit review, +research, +cover letter)
sửa:  services/research/company_research_v1.py             (fix crash bug)
mới:  db/migrations/041_wiring_fixes_and_gates.sql
mới:  db/migrations/042_ats_discovery.sql
mới:  db/migrations/README.md
mới:  services/discovery/ats_discovery_v1.py
mới:  services/discovery/requirements.txt
```

### 9.6 Đã test lại toàn bộ (round 2)

- `python3 -m py_compile` trên toàn bộ 39 file Python (38 cũ + 1 mới) — **100% OK**.
- `pglast` (grammar PostgreSQL thật) trên toàn bộ 47 file migration (45 cũ + 2 mới) — **100% parse OK, 0 lỗi**.
- Phát hiện và tự sửa 1 bug thật của chính tôi khi viết migration 041: `CREATE OR REPLACE VIEW` chèn cột `audit_version` vào giữa danh sách cột thay vì cuối cùng — Postgres không cho phép đổi vị trí cột đã tồn tại khi replace view, đã dời cột này xuống cuối.
- `test_safety_regression.py` — vẫn **PASS 13/13**, xác nhận không phá logic an toàn cũ.
- Chạy `--help` trên cả 8 script đã sửa/thêm (`orchestrator_v1.py`, `approval_service_v1.py`, `company_research_v1.py`, `message_reply_v1.py`, `browser_queue_worker.py`, `autofill_agent_v1.py`, `ats_discovery_v1.py`, `cost_controller_v1.py`) — tất cả OK.
- Chạy thật `ats_discovery_v1.py test --platform greenhouse --slug stripe` — network bị chặn trong sandbox (403, đúng như dự đoán do allowlist), nhưng lỗi được bắt gọn bằng `DiscoveryError` và thoát exit code 1 sạch sẽ, không crash traceback — **cần m tự chạy lại lệnh này trên máy có network thật để xác nhận format JSON từng platform khớp 100%**, vì tôi build code này từ tài liệu công khai chứ không verify được response thật.
- Test đơn vị `match_field()`/`html_to_text()` của autofill và discovery bằng dữ liệu giả lập (không cần DB) — đúng như kỳ vọng (field mới -> "missing" đúng cách, HTML -> text sạch).

### 9.7 Vẫn cần m tự làm trên máy thật

1. Apply 2 migration mới (`041`, `042`) vào DB thật: `docker exec -i jobos-postgres psql -U jobos -d job_apply_os -f db/migrations/041_wiring_fixes_and_gates.sql` rồi `042`.
2. `ats_discovery_v1.py test` với slug thật để xác nhận format JSON — nếu 1 platform nào đó trả JSON khác tài liệu công khai (hay đổi), báo lại để tôi vá adapter đó.
3. Điền `applicant_identity` (bỏ `FILL_ME`, set `approved=true`) và `sensitive_answers` (set `approved_by_user=true`) — kể cả với các trường mới thêm — nếu không thì autofill vẫn báo "thiếu", đúng thiết kế.
4. Thêm công ty thật vào `ats_companies` rồi `poll --apply`.
5. Rotate `POSTGRES_PASSWORD`/mật khẩu DB mặc định trước khi để repo public, như báo cáo lần trước đã nhắc — chưa đụng vào việc này lần này (ngoài phạm vi yêu cầu).
