# Tài Liệu Thiết Kế Kỹ Thuật (`DESIGN.md` — $\le 2$ Trang)

## 1. Kiến Trúc Tổng Thể (Overall Architecture)

Hệ thống được thiết kế theo kiến trúc **Modular Monolith kết hợp Ports & Adapters (Hexagonal)**, đóng gói trọn vẹn trong `docker-compose.yml` gồm 2 dịch vụ (`app` và `db: pgvector/pgvector:pg16`):

```text
[CLI: his-kb] ──┐
                ├──► [All-Boundary DLP Filter] ──► [Core Services: Ingest | Hybrid Search | 7-Step State Machine]
[FastAPI API] ──┘                                           │                              │
                                                            ▼                              ▼
                                           [PostgreSQL 16 + pgvector (10 bảng)] ◄──► [Durable Asyncio Worker]
                                                                                           │
                                                            ┌──────────────────────────────┴───────────────────────┐
                                                            ▼                  ▼                   ▼               ▼
                                                     [GitAdapter]        [LLMRouter]        [JiraAdapter]  [DiscordAdapter]
                                                   (bot/<key> only)   (Primary->Fallback)      (Mock)          (Mock)
```

- **Lớp Giao tiếp**: CLI `his-kb` (`Typer`) cho Phần A và REST API (`FastAPI`) cho cả Phần A (`POST /search`) và Phần B (`POST /chat/commands`, `GET /tasks/{id}`, `GET /stories/{key}`, `GET /audit-logs`).
- **Lớp Bảo vệ Dữ liệu (`AllBoundaryDLP` & `SanitizingLogFilter`)**: Chặn Secret (connection string, API key, JWT, private key $\rightarrow$ `[REDACTED_SECRET]`) và che PII bệnh nhân 3 lớp (nhãn ngữ cảnh, họ tên tiếng Việt tự do 2–4 âm tiết, gazetteer; CCCD; BHYT; SĐT; ngày sinh; email; mã hồ sơ $\rightarrow$ `[MASKED_*]`) tại mọi ranh giới trước khi chạm DB, log, LLM và response.
- **Lớp Lưu trữ bền vững (`SQLAlchemy 2.0` + `Alembic`)**: 10 bảng gồm `users`, `ingestion_runs`, `kb_files`, `kb_chunks`, `code_map_entries`, `workflow_stories`, `processed_messages`, `background_tasks`, `workflow_audit_logs`, `llm_usage_ledgers`. Hỗ trợ native `pgvector` `VECTOR(128)` + `TSVECTOR` trên PostgreSQL 16 và tương thích dialect `sqlite` cho kiểm thử nhanh trên máy host.

## 2. Chiến Lược Chunk Tài Liệu & Source Code (`his-kb`)

1. **Tài liệu nghiệp vụ (`.md`, `.docx`, `.pdf`) — `DocumentChunker`**:
   - File `.md` và `.docx` (`python-docx`) được tách theo cấu trúc tiêu đề (`Heading 1..4`). Các bảng biểu (`Table`) được chuyển đổi nguyên khối sang bảng Markdown gắn với tiêu đề mục chứa nó, tuyệt đối không cắt ngang bảng.
   - File `.pdf` (`pypdf`) được trích xuất theo từng trang (`page_number`) và tiêu đề mục lớn, bảo toàn metadata trích dẫn `{file, heading, page, hisModule}`.
2. **Source code nhánh `staging` (`.cs`, `.ts`, `.tsx`) & Code Map — `CodeChunker`**:
   - **Loại bỏ thư mục rác**: Bỏ qua ngay từ tầng duyệt cây thư mục mọi đường dẫn chứa `bin/`, `obj/`, `node_modules/`, `.git/`, `dist/`, `build/`.
   - **Không cắt ngang thân hàm bằng State-Machine Lexer**: Thay vì dùng regex đếm ngoặc thô dễ vỡ khi gặp `{}` trong chuỗi hoặc comment, bộ `compute_line_brace_deltas()` theo dõi trạng thái từ vựng (`NORMAL`, `LINE_COMMENT`, `BLOCK_COMMENT`, `STRING`, `INTERP_STRING`, `VERBATIM_STRING`, `TEMPLATE_LITERAL`) và chỉ tính độ sâu ngoặc `{}` ở trạng thái `NORMAL`. Nếu hàm vượt quá `4000` ký tự, hệ thống tách theo ranh giới câu lệnh nhưng luôn gắn kèm header `// Signature Context: Class.Method(...)` ở đầu mỗi sub-chunk.
   - **Đồng bộ 1-1 Code Map & RBAC Pre-Filtering**: Bộ trích xuất lấy chữ ký `[Route]`, `[HttpGet/Post/...]` của C# Controller, các lớp `Entities` và React component để lưu vào `code_map_entries` đồng thời tạo chunk `chunk_type = 'code_map'` (không chứa thân hàm). Khi tra cứu Hybrid (`RRF k=60`), bộ lọc quyền `WHERE chunk_type IN ('doc', 'code_map')` cho `BA`/`QC` được áp dụng **trước khi xếp hạng và cắt Top-K**, đảm bảo `BA`/`QC` nhận `0` `source_code` mà không bị hụt kết quả.

## 3. State Machine 7 Bước & Các Quy Ước Làm Rõ Đề Bài

| Bước | Role | Lệnh chạy Agent (`202 Accepted`) | Trạng thái khi chạy -> xong | Cửa duyệt đồng bộ (`200 OK`) | Chuyển bước kế tiếp |
|---|---|---|---|---|---|
| **1. Phân tích** | `BA` | `/ba analyze <KEY>` | `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/ba approve <KEY>` | Sang **Bước 2** (`WAITING_AGENT`) |
| **2. Thiết kế test** | `QC` | `/qc design-test <KEY>` | `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/qc approve <KEY>` | Sang **Bước 3** (`WAITING_AGENT`) |
| **3. Lập kế hoạch** | `Dev` | `/dev plan <KEY>` | `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/dev approve <KEY>` \| `/dev reject <KEY> <lý_do>` | `approve` $\rightarrow$ **Bước 4**; `reject` $\rightarrow$ ở lại **Bước 3** (`WAITING_AGENT`) |
| **4. Hiện thực** | `Dev` | `/dev implement <KEY>` | `RUNNING_AGENT` (tạo `bot/<KEY>`, mở PR vào `develop`) | *(Không có cửa duyệt tay)* | Tự động sang **Bước 5** (`WAITING_AGENT`) |
| **5. Review & merge**| `Dev` | `/dev summarize-pr <KEY>`| `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/dev confirm-merge <KEY>` *(sau khi Dev tự merge)* | Sang **Bước 6** (`WAITING_AGENT`) |
| **6. Kiểm thử** | `QC` | `/qc test <KEY>` | `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/qc pass <KEY>` \| `/qc bug <KEY> \| <log>` (`202`) | `pass` $\rightarrow$ **Bước 7**; `bug` $\rightarrow$ quay về **Bước 3** (`loop_count++`) |
| **7. UAT** | `BA` | `/ba uat <KEY>` | `RUNNING_AGENT` $\rightarrow$ `WAITING_APPROVAL` | `/ba close <KEY>` | Đóng Story: **`DONE`** |

- **Các quy ước làm rõ những điểm đề bài để mở**:
  1. **Xác thực Role & Khóa In-Flight**: Ánh xạ `userId -> role` qua bảng `users` (`ba_user`, `qc_user`, `dev_user`). Sai Role trả `403 Forbidden`; nhảy cóc bước hoặc gửi lệnh duyệt sớm khi Agent đang chạy nền (`step_status == 'RUNNING_AGENT'`) trả `409 Conflict`.
  2. **Idempotency theo `messageId` (Stripe-style & IETF Transient Eviction)**: Lưu `(message_id, payload_sha256, status_code, response_json)` trong `processed_messages` cho kết quả thành công (`200`/`202`) và lỗi bất biến (`400`/`422`/`403`). Các lỗi xung đột trạng thái tạm thời (`409 Conflict`, in-flight lock) được ghi nhận trong `audit_logs` nhưng **không cache vào `processed_messages`** để hỗ trợ client retry tự nhiên khi Story đã chuyển bước hợp lệ. Trùng `messageId` nhưng khác payload trả `422 Unprocessable Entity`.
  3. **Bảo vệ nhánh cứng & PR Idempotent**: `GitAdapter` không có hàm `merge()`. `_validate_bot_write_branch()` kiểm tra regex `^bot/[A-Za-z0-9._-]+$`, chặn mọi nhánh bảo vệ (`main`, `develop`, `staging`) và refspec (`:`, `..`, `refs/`, `+`, `--force`). Khi lặp vòng `6 -> 3 -> 4`, `create_or_update_pull_request()` cập nhật lại PR `OPEN` hiện hữu từ `bot/<KEY>` vào `develop` thay vì tạo PR trùng.

## 4. Các Đánh Đổi Kỹ Thuật (Trade-offs)

- **`DeterministicEmbedding` (128 chiều) + Lexical FTS RRF thay vì gọi Cloud Embedding API**: Đảm bảo hệ thống chạy hoàn toàn offline bằng 1 lệnh `docker compose up` không cần API key và kiểm thử tất định 100%, đánh đổi khả năng hiểu ngữ nghĩa sâu đối với các câu hỏi diễn đạt ẩn dụ ngoài từ điển miền HIS.
- **Hàng đợi DB (`background_tasks` + Lease `lease_expires_at` + `SKIP LOCKED`) thay vì Redis/Celery**: Giảm số lượng container hạ tầng xuống tối thiểu và đảm bảo tính nguyên tử trong cùng transaction với `workflow_stories` / `processed_messages`, đánh đổi thông lượng phân tán ở quy mô hàng nghìn worker song song.
- **Nhận diện Họ tên bệnh nhân tiếng Việt 3 lớp (Regex nhãn + Họ Việt Nam 2–4 âm tiết viết hoa + Gazetteer)**: Đạt tỷ lệ rò rỉ 0% trên tập dữ liệu kiểm thử (kể cả tên viết tự do không có tiền tố `"Bệnh nhân:"`), đánh đổi nguy cơ che nhầm (false positive) một số danh từ riêng tiếng Việt không nằm trong whitelist kỹ thuật.

## 5. Phần Chưa Làm & Hướng Phát Triển Tiếp

- Tích hợp mô hình Multilingual Embedding chạy cục bộ (`bge-m3` qua ONNX) và mô hình NER tiếng Việt chuyên biệt cho dữ liệu y tế để giảm false-positive khi che PII tự do.
- Bổ sung Webhook thật kết nối Jira Server, GitLab/GitHub Enterprise (kèm Branch Protection phía Git server) và Discord Bot Gateway.
- Chạy lệnh build/test của Bước 4 trong container sandbox cô lập (ephemeral test runner) và mở rộng bộ câu hỏi đánh giá `his-kb eval` lên 50–100 câu hỏi thực tế.
