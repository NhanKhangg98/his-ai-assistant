# Tài Liệu Thiết Kế Kỹ Thuật (`DESIGN.md` — $\le 2$ Trang)

## 1. Kiến Trúc Tổng Thể (Overall Architecture)

Hệ thống được thiết kế theo kiến trúc **Modular Monolith kết hợp Ports & Adapters (Hexagonal)**, đóng gói trọn vẹn trong `docker-compose.yml` gồm 2 dịch vụ (`app` và `db: pgvector/pgvector:pg16`):

```text
[CLI: his-kb] ──┐
                ├──► [All-Boundary DLP Filter] ──► [Core Services: Ingest | Hybrid Search | 7-Step State Machine]
[FastAPI API] ──┘                                           │                              │
                                                            ▼                              ▼
                                           [PostgreSQL 16 + pgvector (10 bảng)] ◄──► [Durable 2-Phase Async Worker]
                                                                                           │
                                                            ┌──────────────────────────────┴───────────────────────┐
                                                            ▼                  ▼                   ▼               ▼
                                                     [GitAdapter]        [LLMRouter]        [JiraAdapter]  [DiscordAdapter]
                                                   (bot/<key> only)   (Primary->Fallback)      (Mock)          (Mock)
```

- **Lớp Giao tiếp**: CLI `his-kb` (`Typer`) cho Phần A và REST API (`FastAPI`) cho cả Phần A (`POST /search`) và Phần B (`POST /chat/commands`, `GET /tasks/{id}`, `GET /stories/{key}`, `GET /audit-logs`).
- **Lớp Bảo vệ Dữ liệu (`AllBoundaryDLP` & `SanitizingLogFormatter`)**: Chuẩn hóa Unicode NFC chống bypass tổ hợp. Chặn Secret (connection string, API key, JWT, private key $\rightarrow$ `[REDACTED_SECRET]`) và che PII bệnh nhân 3 lớp (nhãn ngữ cảnh, họ tên tiếng Việt tự do 2–4 âm tiết có whitelist thuật ngữ y khoa, CCCD có dấu phân cách, BHYT không phân biệt hoa thường, SĐT, ngày sinh $\rightarrow$ `[MASKED_*]`). Gắn `SanitizingLogFormatter` trên mọi handler để bảo đảm 100% log từ child logger và multi-line traceback không rò rỉ secret/PII.
- **Lớp Lưu trữ bền vững (`SQLAlchemy 2.0` + `Alembic`)**: 10 bảng gồm `users`, `ingestion_runs`, `kb_files`, `kb_chunks`, `code_map_entries`, `workflow_stories`, `processed_messages`, `background_tasks`, `workflow_audit_logs`, `llm_usage_ledgers`. Hỗ trợ native `pgvector` `VECTOR(128)` + `TSVECTOR` trên PostgreSQL 16 và tương thích dialect `sqlite` cho kiểm thử offline trên máy host.

## 2. Chiến Lược Chunk Tài Liệu & Source Code (`his-kb`)

1. **Tài liệu nghiệp vụ (`.md`, `.docx`, `.pdf`) — `DocumentChunker`**:
   - File `.md` và `.docx` (`python-docx`) được tách theo cấu trúc tiêu đề (`Heading 1..4`). Các bảng biểu (`Table`) được chuyển đổi nguyên khối sang bảng Markdown gắn với tiêu đề mục chứa nó, tuyệt đối không cắt ngang bảng.
   - File `.pdf` (`pypdf`) được trích xuất theo từng trang (`page_number`) và tiêu đề mục lớn, bảo toàn metadata trích dẫn `{file, heading, page, hisModule}`.
2. **Source code nhánh `staging` (`.cs`, `.ts`, `.tsx`) & Code Map — `CodeChunker`**:
   - **Loại bỏ thư mục rác**: Bỏ qua ngay từ tầng duyệt cây thư mục mọi đường dẫn chứa `bin/`, `obj/`, `node_modules/`, `.git/`, `dist/`, `build/`.
   - **Không cắt ngang thân hàm bằng State-Machine Lexer**: Thay vì dùng regex đếm ngoặc thô dễ vỡ khi gặp `{}` trong chuỗi hoặc comment, bộ `compute_line_brace_deltas()` theo dõi trạng thái từ vựng (`NORMAL`, `LINE_COMMENT`, `BLOCK_COMMENT`, `STRING`, `INTERP_STRING`, `VERBATIM_STRING`, `TEMPLATE_LITERAL`) và chỉ tính độ sâu ngoặc `{}` ở trạng thái `NORMAL`. Nếu hàm vượt quá `4000` ký tự, hệ thống tách theo ranh giới câu lệnh nhưng luôn gắn kèm header `// Signature Context: Class.Method(...)` ở đầu mỗi sub-chunk.
   - **Đồng bộ 1-1 Code Map & RBAC Pre-Filtering**: Trích xuất chữ ký `[Route]`, `[HttpGet/Post/...]` của C# Controller, các lớp `Entities` và React component để lưu vào `code_map_entries` đồng thời tạo chunk `chunk_type = 'code_map'` (không chứa thân hàm). Khi tra cứu Hybrid (`RRF k=60`), bộ lọc quyền `WHERE chunk_type IN ('doc', 'code_map')` cho `BA`/`QC` được áp dụng **trước khi xếp hạng và cắt Top-K**, đảm bảo `BA`/`QC` nhận `0` `source_code` mà không bị hụt kết quả. Yêu cầu anonymous gọi role `Dev` bị từ chối 403 ngay tại cổng search.

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
  2. **Idempotency theo `messageId` (Stripe-style & IETF Transient Eviction)**: Lưu `(message_id, payload_sha256, status_code, response_json)` trong `processed_messages`. Xung đột tạm thời (`409 Conflict`, in-flight lock) không cache vào `processed_messages` để client retry tự nhiên sau khi Story chuyển trạng thái. Trùng `messageId` nhưng khác payload trả `422 Unprocessable Entity`. Collision đồng thời được bảo vệ bằng savepoint `begin_nested()` và xử lý graceful replay.
  3. **Bảo vệ nhánh cứng & PR Idempotent**: `GitAdapter` tuyệt đối không có hàm merge. `_validate_bot_write_branch()` kiểm tra regex `^bot/[A-Za-z0-9._-]+$`, chặn mọi nhánh bảo vệ (`main`, `develop`, `staging`), chặn path traversal và refspec (`:`, `..`, `refs/`, `+`, `--force`). Khi lặp vòng `6 -> 3 -> 4`, `create_or_update_pull_request()` tái sử dụng PR `OPEN` hiện hữu từ `bot/<KEY>` vào `develop` thay vì tạo trùng.
  4. **Grounding & Phân hệ**: Output của Agent ở các bước 1, 2, 3 được ground trực tiếp từ chunk trích xuất qua `search_service` và nhận diện theo phân hệ (`Registration`, `Pharmacy`, `Billing`). Mock build/test được đánh dấu rõ ràng với metadata `isSimulated: True`.

## 4. Các Đánh Đổi Kỹ Thuật (Trade-offs)

- **`DeterministicEmbedding` (128 chiều) + Lexical FTS RRF thay vì gọi Cloud Embedding API**: Đảm bảo hệ thống chạy hoàn toàn offline bằng 1 lệnh `docker compose up` không phụ thuộc mạng/API key bên ngoài và kiểm thử tất định 100%, đánh đổi khả năng biểu diễn ngữ nghĩa sâu với câu hỏi ẩn dụ xa từ vựng nghiệp vụ.
- **Durable 2-Phase Queue trong PostgreSQL (`background_tasks` + Lease `lease_expires_at` + `SKIP LOCKED`) thay vì Redis/Celery**:
  - *Phase 1*: Atomic claim task, tăng `attempts`, gán lease và commit ngay để đảm bảo crash recovery không làm mất số lần retry và dead-letter poison task quá `max_attempts`.
  - *Phase 2*: Thực thi Agent trong transaction độc lập và cập nhật Story.
  - *Đánh đổi*: Tối giản kiến trúc xuống 1 DB duy nhất, phù hợp quy mô vừa và nhỏ; đánh đổi thông lượng phân tán hàng chục nghìn tác vụ đồng thời của Kafka/Celery.
- **Nhận diện Họ tên tiếng Việt 3 lớp (Regex nhãn + Họ Việt Nam 2–4 âm tiết + Gazetteer)**: Đạt tỷ lệ rò rỉ 0% trên tập dữ liệu kiểm thử (kể cả tên viết tự do không có tiền tố `"Bệnh nhân:"`), đánh đổi nguy cơ che nhầm một số danh từ riêng tiếng Việt chưa kịp whitelist.
- **Local Mock Adapters cho Git, Jira, Discord, LLM**: Giúp toàn bộ bài thi chạy và test độc lập ngay trên máy chấm bài mà không cần token thật, tuân thủ nguyên tắc Ports & Adapters để thay thế adapter thật trong production mà không đổi logic lõi.

## 5. Phần Chưa Làm & Hướng Phát Triển Tiếp

- **Mô hình AI cục bộ**: Tích hợp Multilingual Embedding (`bge-m3` qua ONNX Runtime) và mô hình NER tiếng Việt y tế chuyên biệt để tối ưu hóa triệt để độ chính xác che PII tự do.
- **Hạ tầng Thực thi Sandbox**: Đưa lệnh build/test ở Bước 4 vào Docker container cách ly (ephemeral runner) giới hạn tài nguyên CPU/RAM và cấm kết nối mạng thay vì mock build.
- **Bảo mật & Tích hợp Doanh nghiệp**: Tích hợp OIDC/JWT SSO cho nhân viên y tế, bổ sung Webhook hai chiều với GitHub/GitLab Enterprise (kèm server-side branch protection) và Discord Bot Gateway.
- **Khả năng quan sát (Observability)**: Mở rộng `LlmUsageLedger` và `WorkflowAuditLog` với chuẩn OpenTelemetry tracing (Olaris/Jaeger) để giám sát độ trễ từng bước state machine.
