# job-apply-os

Pipeline cá nhân, chạy local, tự động hoá việc apply job: tìm job (ATS discovery),
chấm điểm fit, sinh resume/cover letter có kiểm chứng bằng evidence, autofill form
(chỉ điền, không tự bấm submit), và soạn nháp trả lời tin nhắn nhà tuyển dụng.
LLM mặc định chạy local qua Ollama; mọi Python LLM/embedding stage cũng có thể
đổi sang API tương thích OpenAI bằng token, theo global hoặc từng stage.

Giả định: bạn chạy trên **WSL2 (Ubuntu)** nếu là máy Windows, hoặc native
Ubuntu/Linux nếu đó là máy chạy JobOS. Toàn bộ hướng dẫn dưới đây chạy trong
shell Bash (WSL/macOS/Linux). Không hỗ trợ chạy trực tiếp trên PowerShell/CMD.

### Chọn máy chạy đúng vai trò

JobOS có thể chạy toàn bộ trên Ubuntu GPU host, dùng token API, hoặc dùng tunnel
SSH tới Ollama trên máy khác. Runbook đầy đủ cho `/dev/nvidia`, Docker GPU và
systemd Ollama: [docs/ubuntu_gpu.md](docs/ubuntu_gpu.md).

## 1. Yêu cầu

- WSL2 + Ubuntu (Windows) hoặc macOS/Linux, có Bash
- Docker Desktop (bật WSL2 integration nếu dùng Windows)
- Python 3.10+
- Git
- [Ollama](https://ollama.com/download) — chạy local LLM, cài trực tiếp trong
  WSL/máy, không qua Docker
- (Tuỳ chọn) [OpenClaw](https://github.com/khal3d/openclaw) nếu muốn dùng lớp
  browser automation (L3/L7) — xem `docs/openclaw.md`

## 2. Clone & cấu hình môi trường

```bash
git clone https://github.com/your-account/job-apply-os.git job-apply-os
cd job-apply-os
cp .env.example .env
```

Mở `.env`, đổi các giá trị đánh dấu `CHANGE_ME`. Xem chú thích trong
`.env.example` — mỗi biến có ghi rõ dùng để làm gì và lấy ở đâu (không có
API key trả phí nào bắt buộc; các key Telegram/Gmail/Google chỉ cần nếu
bạn bật thêm tính năng đó trong OpenClaw).

## 3. Khởi động Postgres + n8n

```bash
docker compose up -d postgres n8n
```

Postgres expose ở `127.0.0.1:${POSTGRES_HOST_PORT}` (mặc định `5433`), n8n ở
`http://localhost:5678`.

## 4. Chạy migrations

```bash
python scripts/migration_lint.py   # kiểm tra tĩnh, không cần DB — xem db/migrations/README.md
chmod +x scripts/apply_migrations.sh
./scripts/apply_migrations.sh
```

Script này chạy toàn bộ `db/migrations/*.sql` theo thứ tự tên file (không có
bảng theo dõi migration — xem `db/migrations/README.md` để hiểu vì sao số thứ
tự có vài chỗ trùng/lặp có chủ đích). Cần `psql` trong PATH; trên WSL/Ubuntu:
`sudo apt install postgresql-client`.

## 5. Cài Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

File `requirements.txt` ở root chỉ gộp 4 file `services/*/requirements.txt`
lại — không cần cài từng cái riêng.

### Binary hệ thống cho OCR (poppler, tesseract)

`services/profile-ingestion/ocr_profile_files.py` gọi `pdftotext`, `pdfinfo`,
`pdftoppm` (từ poppler) và `tesseract` — đây là binary hệ thống, **pip không
cài được**. Trên WSL/Ubuntu:

```bash
sudo apt update
sudo apt install poppler-utils tesseract-ocr
```

Không có bước này, mọi script OCR sẽ thoát ngay với lỗi "binary not found"
(chủ đích — tránh silent data corruption, xem comment trong
`services/browser-controller/requirements.txt`).

## 6. Chọn LLM backend: Ollama local hoặc token API

```bash
# cài Ollama trong WSL theo hướng dẫn tại ollama.com/download, sau đó:
ollama pull qwen3:8b
ollama pull qwen3:4b
ollama pull deepseek-r1:14b
ollama pull nomic-embed-text
```

**Local mặc định:** Ollama phải đang chạy (`ollama serve`, hoặc daemon tự chạy
nền) ở `http://127.0.0.1:11434` trước khi chạy service sinh nội dung.

**Token API:** thay vì Ollama, đặt `JOBOS_LLM_BACKEND=api`,
`JOBOS_LLM_API_BASE`, `JOBOS_LLM_API_KEY`, và (nếu cần)
`JOBOS_LLM_API_MODEL` trong `.env` không được commit. Gateway chung tại
`services/common/llm_gateway.py` dùng OpenAI-compatible chat/completions và
embeddings; từng role có thể override, ví dụ `JOBOS_DOCGEN_LLM_BACKEND=api`.
Xem danh sách tên role và ví dụ đầy đủ trong `.env.example`. Dùng
`JOBOS_*_LLM_API_STYLE=deepseek` với DeepSeek (base URL không thêm `/v1`), và
để role `embed` dùng provider có embeddings, ví dụ OpenAI. Vì vậy có thể dùng
DeepSeek cho analysis/coordinator và một provider khác cho CV, verification,
và embeddings mà không sửa code.

### Ubuntu NVIDIA/Ollama GPU doctor

Trước khi chạy Ollama trên GPU Ubuntu, dùng doctor chỉ-đọc này. Nó không đọc
`.env`, browser profile hay API key; output có thể đưa Gemini để phân loại đúng
lỗi host driver, Docker runtime hoặc Ollama service:

```bash
bash scripts/ubuntu_ollama_gpu_doctor.sh
# chỉ khi chấp nhận pull CUDA image public để test Docker GPU:
bash scripts/ubuntu_ollama_gpu_doctor.sh --docker-smoke
```

Hai lựa chọn loại trừ nhau:

```bash
# A. Khuyến nghị: Ollama native systemd trên Ubuntu GPU host.
sudo systemctl restart ollama

# B. Ollama container GPU: yêu cầu host nvidia-smi và Docker smoke test pass.
docker compose -f docker-compose.yml -f docker-compose.ollama-gpu.yml up -d ollama
docker exec -it jobos-ollama ollama pull qwen3:8b
```

Không chạy A và B đồng thời: cả hai cùng dùng `127.0.0.1:11434`. Xem
`docs/ubuntu_gpu.md` để cài NVIDIA Container Toolkit, xử lý Secure Boot/DKMS,
kiểm tra `ollama ps`, và dùng SSH tunnel an toàn từ Ubuntu client tới Windows
GPU workstation.

## 7. OpenClaw (tuỳ chọn — chỉ cần cho L3 browser runtime / L7 autofill)

```bash
# tự tạo token private nếu chưa có, rồi render 4 agent/workspace
python scripts/setup_openclaw_jobos.py --mode native --force --generate-gateway-token
```

Script tạo config riêng, bốn agent/workspace (`main`, `resume`,
`cover_letter`, `repo_coordinator`), remote CDP profile và policy tool theo
least privilege, không chạy model/browser. Với Docker fallback, dùng
`python scripts/setup_openclaw_jobos.py --mode docker --force` trước khi chạy
Compose. Chi tiết provider DeepSeek/OpenAI, container và policy: xem
`docs/openclaw.md`.

## 8. Kiểm tra đã setup đúng chưa

```bash
# test không phụ thuộc DB — chạy được ngay sau bước 5
python -m pytest test_safety_regression.py -v

# kiểm tra schema, profile-context gates và LLM config; không gọi model/ghi DB
python services/orchestrator/pipeline_preflight_v1.py --json

# test có phụ thuộc DB thật — chạy sau bước 3+4
python services/orchestrator/orchestrator_v1.py --help
python services/discovery/ats_discovery_v1.py test --platform greenhouse --slug example-company
```

## 9. Chạy pipeline

Entry point chính là `services/orchestrator/orchestrator_v1.py` — xem
`--help` để biết các lệnh (`advance`, `approve`, `deny`, v.v). Toàn bộ các
bước tốn tiền/nhạy cảm (fit-review khi điểm biên, gửi tin nhắn, generate
research) đều dừng lại chờ approval qua `services/approval/approval_service_v1.py`
trước khi tiếp tục — không có bước nào tự động submit đơn (L7 chỉ dừng ở
draft, xem `VERIFICATION_REPORT.md`).

Nếu preflight báo thiếu `base_fit_check_support`, không auto-approve dữ liệu.
Review/approve evidence trước, rồi build các pack deterministic:

```bash
python services/profile-ingestion/prepare_profile_for_pipeline_v1.py status
python services/profile-ingestion/prepare_profile_for_pipeline_v1.py build --apply
python services/orchestrator/pipeline_preflight_v1.py --json
```

## 9.1 — Cửa sổ dán JD (khuyến nghị, không cần browser)

Nếu bạn tự tìm job, đây là đường nhanh và không phụ thuộc LinkedIn/CDP/OpenClaw.
Form desktop nhận công ty, chức danh, URL, nguồn, địa điểm, seniority, deadline,
salary range, ghi chú và toàn bộ JD. Nó lưu vào `applications` chuẩn, dedupe theo
hash JD, chạy screen miễn phí nếu chọn, rồi database tự queue market-demand cho cả
job bị loại lẫn job phù hợp. Nút analysis là opt-in vì có thể dùng API/LLM; nó chỉ
chạy đến cổng duyệt của bạn và không submit đơn.

```bash
# Ubuntu desktop only; cài một lần nếu Python thiếu Tk.
sudo apt install python3-tk
source .venv/bin/activate
python scripts/jobos_intake_app.py
```

Không chạy `launch_jobos_browser.py`, không cần Chrome, và không cần login LinkedIn
cho workflow này.

## 9.2 — Job search theo profile + LinkedIn user intake (tuỳ chọn)

`services/discovery/profile_job_search_v1.py` chỉ dùng capability/tool/competency
đã `approved` trong database. Nó sinh link tìm kiếm LinkedIn để **bạn tự mở**
và xếp hạng các job đã intake một cách giải thích được; nó không scrape, không
đăng nhập, không scroll kết quả và không apply tự động.

```bash
python services/discovery/profile_job_search_v1.py terms
python services/discovery/profile_job_search_v1.py queries --keyword "security engineer"
python services/discovery/linkedin_intake_v1.py import --file data/linkedin_jobs.json --apply
python services/discovery/profile_job_search_v1.py rank --keyword "security engineer"
```

File import là JSON array (hoặc `{ "jobs": [...] }`) gồm `company`, `title`,
`url`, `jd_text` (và tuỳ chọn `location`, `work_mode`). Một URL LinkedIn do bạn
tự dán cũng có thể đi qua browser queue ở chế độ chỉ đọc (`queue-fetch` rồi
`ingest-task`). Tất cả đều vào bảng `applications` hiện có, nên vẫn đi qua fit,
company research, document evidence và approval như mọi nguồn khác.

### LinkedIn browser executor (profile riêng, tuỳ chọn)

Để JobOS tự tìm một số JD và lưu thử, mở Chrome profile riêng của JobOS. Bạn
đăng nhập LinkedIn bằng tay một lần trong cửa sổ đó; JobOS không sao chép
password/cookie từ browser thường dùng. Chrome phải giữ mở trong lúc worker
chạy.

```bash
python scripts/launch_jobos_browser.py
python scripts/setup_openclaw_jobos.py --mode native --force --generate-gateway-token
# Keep this terminal running; it starts the private OpenClaw runtime.
python scripts/start_openclaw_jobos.py gateway
# In another terminal, confirm the gateway and CDP browser are reachable.
python services/browser-controller/browser_queue_worker.py --health

# Explicitly user-initiated, read-only; maximum is 5 JD per task.
python services/discovery/linkedin_intake_v1.py queue-discovery \
  --keywords "cybersecurity analyst" \
  --location "New Jersey, United States" \
  --max-results 3
python services/browser-controller/browser_queue_worker.py --once
```

The discovery executor may search only the supplied terms and open at most the
requested number of result detail pages. It stores only validated
company/title/LinkedIn-job-URL/full-JD records in `applications`, then the
usual market-demand and application gates run. It never solves CAPTCHA,
changes LinkedIn preferences, creates alerts, saves jobs, messages anyone,
fills forms, or applies.

Cover letter lấy company context từ `company_research_cache` chỉ khi cache còn
hiệu lực và có URL nguồn. Mỗi đoạn dùng context đó phải lưu URL trong evidence
map; URL lạ hoặc claim company không có nguồn bị loại trước khi lưu document.

### Resume Word từ template local

Sau khi truth checker pass, pipeline copy Word template riêng, chỉ tailor 12
project-bullet slots, 5 skill-category rows, và subtitle nằm giữa project name
với GitHub link. Education, experience, certifications, project name, dates,
GitHub links, font và spacing được preserve; không tự shrink font để nhét
trang. Template và output là local/ignored:

```bash
python services/document-generation/render_verified_resume_v1.py \
  --application-id APPLICATION_UUID
```

Mở DOCX này trong Word và dùng Print/Save as PDF sau khi review. JobOS không tự
in PDF từ LibreOffice, vì cùng một DOCX có thể paginate khác Word/macOS. Xem
`docs/resume_template_contract.md`.

Resume rules are enforced, not just prompted: only the six pre-approved project
blocks already present in the template can be selected; their title, dates, and
GitHub links cannot change. A primary bullet (slots 1, 3, …) is limited to 200
characters and its optional secondary bullet to 105 characters; the secondary
cannot exist on its own. Each block can cite only that same project's approved
profile asset, and skills are capped at five evidence-backed ranked rows. A new
project requires a deliberately prepared block in the Word template and an
approved asset-title alias before it can be used.

The editable header subtitle (for example, `CAROECT-D — [subtitle] | GitHub`)
is separately audited. A proposed subtitle must provide the exact previous
subtitle, a literal JD quote, a literal quote from its project asset, a reason
the change is more accurate/relevant, and a before/after rationale covering
every substantive changed term. The verifier checks both quotes and rejects the
whole document if the change is not supported. See
`docs/resume_template_contract.md`.

Project bullets receive the same JD/profile audit: they need an exact old
bullet, literal JD and profile-asset quotes, and a term-by-term rationale.
Unsupported tools or invented experience are blocked. Cover letters are the
more context-rich artifact: the generator combines fit requirements and gaps,
approved evidence, and sourced company context. When company research exists,
it requires at least one candidate-asset-backed, URL-backed company-specific
paragraph with a literal company-research quote; it may be persuasive, but
cannot invent a company fact or professional experience.

### Project profile form (source of truth)

Before tailoring, record the verified facts for the six approved projects in a
private local registry. The form captures immutable template identity plus
aliases, skills, tools, JD keywords, allowed facts, boundaries, and evidence
locations. It later maps parsed profile/repository data conservatively; unclear
records stay unmapped rather than contaminating a project.

```bash
source .venv/bin/activate
python scripts/jobos_project_profile_app.py
```

The JSON is local/ignored at `data/project-registry/project_profiles.json`.
See `docs/project_profile_registry.md` for the mapping contract.

### Market-demand intelligence → project backlog

Mỗi JD đã intake được database queue ngay từ lúc lưu, **trước** filter/fit.
Worker LLM đọc từng chunk, nhận diện cả tool/skill/framework/standard/
qualification mới không nằm trong catalogue, rồi code chỉ lưu item nào có
**literal excerpt** đối chiếu được với JD. Vì queue độc lập với state machine,
job `filtered_out`, `fit_rejected` và job phù hợp đều đóng góp market data.
Role đã fit dùng `role_family`; job bị filter quá sớm được nhóm theo
`job_title` chuẩn hoá thay vì bị rơi vào nhóm chung. Sau khi cấu hình LLM
gateway, chạy worker/backfill:

```bash
python services/discovery/market_demand_intelligence_v1.py process --apply
# Retry only JDs whose prior extraction failed because a provider was unavailable:
python services/discovery/market_demand_intelligence_v1.py process --retry-failed --apply
python services/discovery/market_demand_intelligence_v1.py demands --role-family soc_dfir
python services/discovery/market_demand_intelligence_v1.py gaps
python services/discovery/market_demand_intelligence_v1.py ideas --apply
```

`process --apply` can be run manually, or as the profile-gated worker below
after setting the role/global API values in untracked `.env`. It does not
start by default, so adding an API key never creates unexpected spend:

```bash
docker compose --profile market-intelligence up -d --build market-intelligence-worker
```

`demands` cho count posting/company và danh sách công ty; `gaps` chỉ là các
requirements chưa có trong profile capabilities đã approved; `ideas` tạo
backlog project ở trạng thái `proposed`, với scope/evidence goal để bạn chọn
và sửa trước khi build. Nó không được dùng để nói rằng bạn đã có skill đó.

## 9.2 — Pointer telemetry (research/ergonomics only)

Để đo chuyển động chuột của chính bạn, không điều khiển chuột:

```bash
pip install pynput
python services/pointer-dynamics/record_pointer_trace.py --output ~/pointer-trace.csv
python services/pointer-dynamics/fit_pointer_regimes.py ~/pointer-trace.csv --output ~/pointer-regimes.json
```

Estimator tạo nhiều regime theo thời gian: drift dùng Theil–Sen median slope,
diffusion dùng MAD của Brownian innovations sau khi trừ drift local. Không có
global mean và không có code replay/cursor automation; trace là dữ liệu hành vi
nhạy cảm, không commit vào git.

## 9.3 — Repo audit agent riêng

`repo-audit` tách inventory GitHub (read-only), worker test offline trong
container, và coordinator model đọc **report JSON**. Các worker không share
chain-of-thought; chúng chỉ share logs, exit codes, finding và evidence.

GitHub metadata/audit là một nguồn evidence **tách khỏi profile chunks**. Một
repository phát hiện được không tự chứng minh bạn là tác giả hoặc là kinh
nghiệm làm việc: sau import bạn phải xác nhận ownership, kiểm tra asset được
tạo, rồi approve asset đó. Chỉ asset đã approve mới có thể đi vào L6
resume/cover-letter.

```bash
# Public inventory; với repo private, đặt GH_TOKEN (fine-grained, contents:read)
python services/repo-audit/repo_inventory_v1.py --github-user YOUR_GITHUB --write-manifest data/repo-manifest.json

# Chỉ mount các bản copy repo đã chọn; container không có network/credentials
export JOBOS_REPO_AUDIT_INPUT=/absolute/path/to/selected-repo-copies
docker compose -f docker-compose.repo-audit.yml run --rm repo-audit \
  --repo /input/project-a --check python_compile

# Import metadata vào evidence store (dry-run nếu bỏ --apply)
python services/repo-audit/repository_evidence_v1.py import-inventory \
  --manifest data/repo-manifest.json --apply

# Xem source_id, sau đó xác nhận ownership và tạo asset cần review
python services/repo-audit/repository_evidence_v1.py review
python services/repo-audit/repository_evidence_v1.py confirm-ownership \
  --source-id REPOSITORY_SOURCE_ID --actor candidate --apply
python services/repo-audit/repository_evidence_v1.py build-asset \
  --source-id REPOSITORY_SOURCE_ID --role-family software_engineering --apply

# Sau khi review nội dung/evidence: cho phép L6 dùng asset
python services/repo-audit/repository_evidence_v1.py approve-asset \
  --asset-id PROFILE_ASSET_ID --actor candidate --apply
```

Nếu có report JSON từ worker, attach report vào source tương ứng bằng
`import-audit --source-id REPOSITORY_SOURCE_ID --report /reports/repo_audit_report.json --apply`.
Metadata, test report và ownership confirmation đều giữ source URL/path riêng;
code repo không bị chunk hoặc tự suy diễn thành thành tích.

Các OpenClaw agent cũng nhận model từ `OPENCLAW_*_MODEL`: có thể là
`ollama/<local-model>` hoặc provider API đã auth như `openrouter/auto`. Re-run
bootstrap với `--force` để áp dụng thay đổi, sau khi xem backup được tạo.

## 10. L9 — Interview prep

Không nằm trong state machine tự động của orchestrator (interviews không đi
qua `applications.pipeline_step`) — chạy tay như một lệnh riêng, giống cách
chạy `message_reply_v1.py`:

```bash
# xem hàng đợi (interview đã classify, chưa có prep package) — không gọi LLM, không ghi DB
python services/interview-prep/interview_prep_v1.py --list-only

# sinh prep package cho 1 interview cụ thể và ghi vào DB
python services/interview-prep/interview_prep_v1.py --interview-id APPLICATION_UUID --apply

# không có --interview-id: xử lý toàn bộ hàng đợi
python services/interview-prep/interview_prep_v1.py --apply
```

Không truyền `--apply` = dry-run mặc định (in ra prep notes rồi rollback,
không ghi gì). Có cost-gate best-effort trước mỗi lần gọi LLM (mirror cách
orchestrator check `cost_controller_v1.py`) — nếu vượt ngân sách sẽ skip
interview đó và in lý do, không crash cả batch.

## Ghi chú Windows

- Bắt buộc chạy trong WSL2 — không có bản PowerShell/CMD cho các script `.sh`.
- Docker Desktop cần bật "Use the WSL 2 based engine" + WSL integration cho
  distro Ubuntu bạn dùng (Settings → Resources → WSL Integration).
- `00_check_main_user_env.sh` và `01_debug_docker.sh` (ở root repo) là
  script debug riêng cho máy Mac ban đầu (dùng `sw_vers`, `lsof` kiểu
  macOS) — không cần cho setup, có thể bỏ qua trên Windows/WSL.
