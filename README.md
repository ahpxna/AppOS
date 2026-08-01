# job-apply-os

Pipeline cá nhân, chạy local, tự động hoá việc apply job: tìm job (ATS discovery),
chấm điểm fit, sinh resume/cover letter có kiểm chứng bằng evidence, autofill form
(chỉ điền, không tự bấm submit), và soạn nháp trả lời tin nhắn nhà tuyển dụng.
Toàn bộ LLM chạy local qua Ollama, không cần API key trả phí.

Giả định: bạn chạy trên **WSL2 (Ubuntu)** nếu là máy Windows. Toàn bộ hướng dẫn dưới
đây chạy trong shell Bash (WSL/macOS/Linux). Không hỗ trợ chạy trực tiếp trên
PowerShell/CMD.

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
git clone <repo-url> job-apply-os
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

## 6. Cài Ollama + tải model

```bash
# cài Ollama trong WSL theo hướng dẫn tại ollama.com/download, sau đó:
ollama pull qwen3:8b
ollama pull qwen3:4b
ollama pull deepseek-r1:14b
ollama pull nomic-embed-text
```

Ollama phải đang chạy (`ollama serve`, hoặc daemon tự chạy nền sau khi cài)
ở `http://127.0.0.1:11434` trước khi chạy bất kỳ service nào sinh nội dung.

## 7. OpenClaw (tuỳ chọn — chỉ cần cho L3 browser runtime / L7 autofill)

```bash
cp bootstrap/openclaw/secrets.example.json bootstrap/openclaw/secrets.local.json
# điền secrets.local.json nếu dùng Telegram/Gmail/web search (xem mục 6 trong .env.example)
python scripts/openclaw_bootstrap.py bootstrap
```

Chi tiết đầy đủ, gồm cả phương án container fallback
(`docker-compose.openclaw.yml`) và khuyến nghị chạy dưới OS user riêng: xem
`docs/openclaw.md`.

## 8. Kiểm tra đã setup đúng chưa

```bash
# test không phụ thuộc DB — chạy được ngay sau bước 5
python -m pytest test_safety_regression.py -v

# test có phụ thuộc DB thật — chạy sau bước 3+4
python services/orchestrator/orchestrator_v1.py --help
python services/discovery/ats_discovery_v1.py test --platform greenhouse --slug <slug-công-ty-thật>
```

## 9. Chạy pipeline

Entry point chính là `services/orchestrator/orchestrator_v1.py` — xem
`--help` để biết các lệnh (`advance`, `approve`, `deny`, v.v). Toàn bộ các
bước tốn tiền/nhạy cảm (fit-review khi điểm biên, gửi tin nhắn, generate
research) đều dừng lại chờ approval qua `services/approval/approval_service_v1.py`
trước khi tiếp tục — không có bước nào tự động submit đơn (L7 chỉ dừng ở
draft, xem `VERIFICATION_REPORT.md`).

## 10. L9 — Interview prep

Không nằm trong state machine tự động của orchestrator (interviews không đi
qua `applications.pipeline_step`) — chạy tay như một lệnh riêng, giống cách
chạy `message_reply_v1.py`:

```bash
# xem hàng đợi (interview đã classify, chưa có prep package) — không gọi LLM, không ghi DB
python services/interview-prep/interview_prep_v1.py --list-only

# sinh prep package cho 1 interview cụ thể và ghi vào DB
python services/interview-prep/interview_prep_v1.py --interview-id <uuid> --apply

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
