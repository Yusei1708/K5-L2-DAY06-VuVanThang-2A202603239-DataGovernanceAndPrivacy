# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Báo cáo được tổng hợp cho bài thực hành cá nhân **Track T08 · Retention & Delete Request Simulator**.
> Mọi số liệu hiệu năng và kiểm chứng đều được trích xuất trực tiếp từ các tệp do script sinh ra (`reports/metrics_baseline.json`, `reports/metrics_poc.json`, `reports/table1_baseline_vs_poc.md`).

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID / Học viên | `G08_VuVanThang_2A202603239` · `VuVanThang` (`2A202603239`) |
| Challenge | `T08` · Retention & Delete Request Simulator |
| Vai trò thực hiện | `VuVanThang` — Thiết kế cấu hình, Baseline (`src/baseline_t08.py`), PoC (`src/t08_deletion.py`, `src/pipeline.py`), HITL & audit, Unit tests (`tests/test_t08_poc.py`), Báo cáo & Demo |
| Git URL | `https://github.com/Yusei1708/K5-L2-DAY06-VuVanThang-2A202603239-DataGovernanceAndPrivacy` |
| Tag `demo-180m` / `final-24h` | Gắn trên commit nộp duy nhất ở nhánh `main` (`demo-180m`, `final-24h`) |
| Ngày hoàn thành (giờ VN) | `2026-11-14` (`+07:00`) |

## 01 · Problem statement

- **Ai bị ảnh hưởng / chịu hậu quả khi hệ thống sai?**
  - **Chủ thể dữ liệu (`data subject`):** Nếu hệ thống chỉ xóa file gốc `raw_video` mà bỏ sót `backup`, `frame`, `face_crop`, `embedding` trong vector DB hoặc `training_export`, thông tin sinh trắc học nhận dạng vẫn tồn tại sau yêu cầu xóa hợp pháp.
  - **Tổ chức / Pháp chế (Legal & DPO) & bên thứ ba:** Nếu xóa nhầm dữ liệu đang chịu `legal_hold` (`LH-2026-03`, `LH-2026-07`), xóa bản sao khi thiếu quyền ACL (`vendor-x`), hoặc xóa nhầm dữ liệu chứa danh tính của chủ thể khác trong cùng khung hình (`bystander contamination`), hệ thống vi phạm nghĩa vụ bảo toàn chứng cứ và quyền riêng tư của người khác.
- **Điểm nghẽn cụ thể:** Dữ liệu đa phương tiện phân tán qua nhiều tầng dẫn xuất (tối đa `depth = 3`: `raw_video -> frame -> face_crop -> embedding` và nhánh `backup`, `annotation`, `training_export`) với các ràng buộc ngoại lệ khác nhau trên từng nút đồ thị.
- **Đầu vào → đầu ra:** Đầu vào là `data_graph.json` (`212` artefact mock) và `requests.json` (`10` delete request `DR-001`..`DR-010`). Đầu ra bắt buộc là `out/<dir>/actions.csv` (`request_id, artefact_id, action ∈ {delete, retain_legal_hold, escalate}`), `audit_log.jsonl`, `review_queue.csv`, `contact_sheet.html`, `decision.json`, kèm `dry_run_summary.json`, `sla_summary.json`, `sla_dashboard.html`, `notifications.jsonl`.
- **Tiêu chí GO / HOLD / REWORK (đóng băng trước eval):**
  - `HOLD` (`LEGAL_HOLD_VIOLATION`): Có bất kỳ artefact mang `legal_hold` nào bị gán nhãn `delete`.
  - `REWORK` (`UNREVIEWED_EXCEPTION_DELETE`): Có artefact ngoại lệ (`permission_acl`, `unknown_origin`, `multi_subject`) bị gán `delete` khi chưa có xác nhận duyệt người dùng.
  - `HOLD` (`T08_EXCEPTIONS_PENDING_HUMAN_REVIEW`): Đã tách sạch ngoại lệ nhưng chưa có file xác nhận của người duyệt (`review_queue_reviewed.csv`).
  - `GO` (`T08_DRY_RUN_REVIEWED_GO` / `T08_EXECUTION_REVIEWED_GO`): `0` vi phạm legal hold, `100%` hành động có audit log, và toàn bộ hàng đợi ngoại lệ (`budget=30`) đã được người chịu trách nhiệm ký duyệt.
- **Giả định cần DPO / Legal xác nhận:**
  1. Giá trị `legal_hold` trong fixture được đánh dấu tường minh trên từng artefact (`propagate_legal_hold: false`); nếu bộ phận pháp chế yêu cầu khóa lan truyền toàn bộ bản dẫn xuất từ `raw_video`, cần bật `propagate_legal_hold: true` sau khi đánh giá mức giảm hoàn tất xóa (`1.0000 -> 0.7969`).
  2. Mọi artefact dùng chung nhiều chủ thể (`frame`, `annotation` 2 người hoặc `training_export` 9–10 người) được `escalate` để làm mờ/tách vùng hoặc tái tạo tập huấn luyện ở chu kỳ tiếp theo thay vì xóa trực tiếp dữ liệu của chủ thể không yêu cầu xóa.

## 02 · Data & provenance

| Split / Nguồn | N | Nguồn | License | Synthetic? |
|---|---|---|---|---|
| Input pool (`fixtures/t08_deletion/`) | `212` artefacts, `10` requests (`158` cặp đánh giá) | `synthetic:tools/make_fixtures.py (seed 42)` | `CC0-1.0 (synthetic, generated for AI20K Day06)` | Có (100% mock, không chứa dữ liệu/PII thật) |
| Internal verification ref (`reports/internal_gt_t08.csv`) | `158` cặp `(request_id, artefact_id, expected_action)` | Xây dựng độc lập theo spec [T08.md](../docs/tracks/T08.md) từ input graph (đặt ngoài `fixtures/*/gt/`) | Nội bộ theo fixture CC0-1.0 | Có |
| Official eval (`fixtures/t08_deletion/gt/gt_actions.csv`) | Private overlay giảng viên | Repo chấm điểm giảng viên | Private eval | Có |

- **Schema `data_graph.json` (`212` nút):** `raw_video` (`15`), `backup` (`15`), `frame` (`45`), `face_crop` (`45`), `embedding` (`45`), `annotation` (`45`), `training_export` (`2`). Các thuộc tính chính: `artefact_id`, `type`, `subject_ids`, `parent_ids`, `uri`, `owner`, `acl`, `legal_hold`, `origin_known`, `created_at`, `filename`.
- **Seed:** `42`. Thứ tự duyệt request và nút con được sắp xếp tất định (`sorted`) nên kết quả bất biến khi chạy lặp lại.
- **`manifest.csv` verified (SHA256):** Có — kiểm tra bằng `.venv/bin/python -m tools.verify_manifests` (`14 manifests, 0 failing`) và kiểm tra runtime ở bước `01 load()` khi `strict_provenance: true`.
- **Dữ liệu thật:** Không dùng dữ liệu thật (`data/real/` không sử dụng theo đúng quy định Track T08).

## 03 · Research & design

- **Tài liệu tham khảo & lý do áp dụng:**
  1. **Luật Bảo vệ dữ liệu cá nhân số 91/2025/QH15 & Nghị định 356/2025** (theo đề bài T08): quy định quyền yêu cầu xóa dữ liệu cá nhân đi kèm các ngoại lệ khi pháp luật yêu cầu tiếp tục lưu giữ (nghĩa vụ pháp lý / giải quyết tranh chấp) → là căn cứ cho quy tắc `legal_hold -> retain_legal_hold` kèm lưu vết lý do và hẹn xóa sau khi hết hold.
  2. **GDPR Article 17 — Right to erasure (`https://gdpr-info.eu/art-17-gdpr/`, xem ngày 2026-11-14):** Đ.17(2) đặt nghĩa vụ thông báo xóa tới các bản sao/bên xử lý liên kết, Đ.17(3)(b),(e) quy định ngoại lệ khi lưu giữ để tuân thủ nghĩa vụ pháp lý hoặc thiết lập/thực thi/bào chữa khiếu nại pháp lý → dẫn tới kiến trúc truy vết toàn đồ thị (`BFS`) kết hợp hàng đợi `escalate` cho bản sao thuộc `acl=['vendor-x']` (`a0156`).
  3. **California Delete Request and Opt-out Platform (DROP) (`https://privacy.ca.gov/DROP`, xem ngày 2026-11-14)** & **Google Research Data Cards Playbook (`https://sites.research.google/datacardsplaybook/`, xem ngày 2026-11-14):** DROP minh họa luồng xử lý tập trung một yêu cầu xóa tới nhiều kho lưu trữ với nhật ký tuân thủ định kỳ; Data Cards Playbook nhấn mạnh tính minh bạch provenance → dẫn tới cơ chế gắn nhãn `origin_unknown` (`origin_known=false`) và `manifest.csv` SHA256.
- **Luồng xử lý (`src/t08_deletion.py` + `src/pipeline.py`):**
  ```
  requests.json + data_graph.json
        │
        ▼ (01 load: verify_manifest SHA256 + checkpoint_digest)
  Subject-aware Lineage BFS (parent_ids -> children; chặn bystander crop không chứa subject_id)
        │
        ▼ (02 process: legal_hold > acl/owner > origin_known > multi_subject > delete; checkpoint từng request)
  Risk Ranking top-B (B=30, đưa toàn bộ legal_hold=1.00, acl=0.95, origin=0.90, multi=0.85/0.80 lên đầu)
        │
        ▼ (03 rank: out/<run>/review_queue.csv + contact_sheet.html)
  HITL Sign-off & Audit Log (04 review: apply_overrides + append-only out/<run>/audit_log.jsonl)
        │
        ▼ (05 decide + artifacts: dry_run_summary.json, sla_dashboard.html, notifications.jsonl -> decision.json)
  ```
- **Thành phần offline:** 100% thuật toán dùng thư viện chuẩn Python + `pandas` / `pyyaml` nội bộ, không dùng model nặng và không gọi mạng (`--offline` bảo vệ qua `offline.py`).
- **Trade-offs đã cân nhắc:**
  - Chọn **Subject-aware BFS** thay vì BFS mù trên toàn đồ thị con: giúp truy ra trọn vẹn `158` cặp mà tuyệt đối không lôi nhầm `face_crop`/`embedding` của chủ thể khác gắn dưới cùng một `frame` chung (`a0011 -> a0012/a0013`).
  - Tách cấu hình `propagate_legal_hold: false` (mặc định theo schema nút) so với `true` (khóa dây chuyền toàn nhánh): tránh làm rớt `Deletion Completeness` một cách âm thầm mà vẫn cho phép Legal/DPO bật khi chính sách thay đổi.

## 04 · Baseline vs PoC

- **Baseline (`src/baseline_t08.py`):** Với mỗi `request_id` (`subject_id`), chỉ quét các nút `type == "raw_video"` có `filename` khớp `subject_id` (tổng `10` bản `raw_video`), gán đồng loạt `delete`, không duyệt lineage dẫn xuất (`backup`, `frame`, `face_crop`, `embedding`, `annotation`, `training_export`), không kiểm tra `legal_hold` / `acl` / `origin_known` / `multi_subject`, và không ghi audit trace cho từng artefact.
- **PoC (`src/pipeline.py` + `src/t08_deletion.py`):** Truy vết đồ thị lineage hai điều kiện (Subject-aware BFS), phân loại theo tháp ưu tiên chính sách, hỗ trợ checkpoint khôi phục từng request khi gián đoạn, sinh hàng đợi duyệt `B=30`, nhật ký kiểm toán `audit_log.jsonl` (cấp từng artefact), bảng SLA & kênh thông báo offline.
- **Điều kiện so sánh công bằng:** Cùng fixture `fixtures/t08_deletion/`, cùng `10` request / pool `212` artefact, cùng `seed = 42`, chế độ `--offline`.

**Bảng 1 · Baseline vs PoC** — trích xuất trực tiếp từ `python -m src.compare reports/metrics_baseline.json reports/metrics_poc.json --out reports/table1_baseline_vs_poc.md`:

Baseline: `baseline` (git None, seed 42) · PoC: `poc` (git None, seed 42)

| Metric (unit) | Slice | Baseline | PoC | Δ abs | Δ rel | Better | N |
|---|---|---|---|---|---|---|---|
| **Deletion Completeness** (must-delete artefacts deleted / must-delete artefacts) | all | 0.0625 | 1.0000 | 0.9375 | +1500.0% | higher | 158 |
| False-delete count (count (target 0): exceptions deleted + deletes outside request scope) | all | 2.0000 | 0.0000 | -2.0000 | -100.0% | lower | None |
| Exception handling accuracy (legal-hold/escalate rows handled as expected / such rows) | all | 0.0000 | 1.0000 | 1.0000 | baseline=0 | higher | None |
| Audit trace completeness (actions with an audit entry / actions) | all | 0.0000 | 1.0000 | 1.0000 | baseline=0 | higher | None |
| Runtime / 100 items (s) | all | 0.2430 | 0.0433 | | | lower | |

## 05 · Evaluation protocol

- **Đơn vị & công thức:**
  - `Deletion Completeness` = số cặp `(request_id, artefact_id)` có `expected_action == "delete"` và `pred == "delete"` chia cho tổng `128` cặp phải xóa.
  - `False-delete count` = số ca ngoại lệ (`retain_legal_hold` hoặc `escalate`) bị dự đoán nhầm thành `delete` cộng số dòng `delete` nằm ngoài phạm vi hợp lệ (`stray`).
  - `Exception handling accuracy` = số ca ngoại lệ khớp đúng `retain_legal_hold` hoặc `escalate` chia tổng `30` ca ngoại lệ.
  - `Audit trace completeness` = số hành động có `artefact_id` xuất hiện trong trường `item_id` của `audit_log.jsonl` chia tổng số hành động.
- **Tham số đóng băng:** `budget = 30`, `dry_run = true`, `allowed_actor = "data-platform"`, `propagate_legal_hold = false`, `seed = 42`.
- **Lệnh đánh giá chính xác (chạy trên đối chiếu nội bộ; thay `--gt` bằng mặc định khi có overlay giảng viên):**
  ```bash
  .venv/bin/python -m src.evaluate --track t08 --gt reports/internal_gt_t08.csv --pred out/baseline/actions.csv --run-name baseline --out reports/metrics_baseline.json
  .venv/bin/python -m src.evaluate --track t08 --gt reports/internal_gt_t08.csv --pred out/poc/actions.csv --run-name poc --out reports/metrics_poc.json
  ```

## 06 · Result slices, Ablation & Failure cases

| Slice | N | Baseline Accuracy | PoC Accuracy | Ghi chú |
|---|---|---|---|---|
| `delete` | `128` | `0.0625` (`8/128`) | `1.0000` (`128/128`) | Baseline chỉ xóa 8 `raw_video`, sót 120 bản dẫn xuất (`backup`, `frame`, `face_crop`, `embedding`, `annotation`) |
| `escalate` | `28` | `0.0000` (`0/28`) | `1.0000` (`28/28`) | Gồm `1` ACL `vendor-x`, `3` `origin_known=false`, `12` shared frame/annotation, `12` `training_export` |
| `retain_legal_hold` | `2` | `0.0000` (`0/2`) | `1.0000` (`2/2`) | `DR-002:a0043` (`LH-2026-03`) và `DR-005:a0099` (`LH-2026-07`); slice nhỏ (`N=2`) báo số đếm chính xác |

**Ablation · Ảnh hưởng của cấu hình `propagate_legal_hold`:**
- `propagate_legal_hold = false` (mặc định): `128 delete, 28 escalate, 2 retain_legal_hold` → `Deletion Completeness = 1.0000`, `Exception handling accuracy = 1.0000`.
- `propagate_legal_hold = true` (lan truyền từ raw hold xuống toàn nhánh con cùng chủ thể): `102 delete, 25 escalate, 31 retain_legal_hold` → `Deletion Completeness = 0.7969` (`102/128`), `Exception handling accuracy = 0.9000` (`27/30`), `False-delete count = 0`. Cho thấy lan truyền mù làm giữ lại thêm `26` bản dẫn xuất và `3` lượt export dù chưa có chỉ thị khóa cấp derivative từ DPO/Legal.

**Phân tích 2 Failure Mode bắt buộc trong fixture `t08_deletion`:**

1. **Failure Mode 1 — Xóa nhầm Legal Hold & Nhiễu danh tính trong nhánh chung (False-delete & Bystander Contamination):**
   - **Minh họa từ metadata:**
     - `DR-002` (`subj_03`): `a0043` (`mock://raw/raw_video/a0043`, `filename="drive_subj_03_003.mp4"`, `legal_hold="LH-2026-03"`). Baseline khớp theo tên file nên gán `delete` thẳng vào `a0043`; tương tự `DR-005` (`subj_07`) gán `delete` vào `a0099` (`legal_hold="LH-2026-07"`), tạo nên `False-delete count = 2`.
     - `DR-001` (`subj_01`): frame chung `a0011` (`subject_ids=["subj_00", "subj_01"]`) đẻ ra `a0012` (`face_crop`, `subject_ids=["subj_00"]`) và `a0013` (`embedding`, `subject_ids=["subj_00"]`). Nếu thuật toán duyệt con theo `parent_ids` mà không kiểm tra `subject_ids`, `DR-001` sẽ xóa nhầm dữ liệu của `subj_00` (`stray false delete`).
   - **Root cause & khắc phục:** Baseline không đọc cờ `legal_hold`; mặt khác BFS thông thường bỏ qua phân tách chủ thể tại node tách nhánh `face_crop`. PoC đặt `legal_hold` ở mức ưu tiên cao nhất và áp dụng điều kiện lọc `child.subject_ids` rỗng hoặc chứa `subject_id`.

2. **Failure Mode 2 — Xóa nửa vời (Incomplete Lineage: xóa raw nhưng sót backup, face crop, embedding vector DB & training export):**
   - **Minh họa từ metadata:** Trong `DR-004` (`subj_05`), Baseline xóa duy nhất `a0071` (`drive_subj_05_005.mp4`), để lại nguyên vẹn `1` `backup` (`a0072`), `3` `frame` (`a0073, a0077, a0081`), `3` `face_crop` (`a0074, a0078, a0082`), `3` `annotation` (`a0076, a0080, a0084`), `3` `embedding` trôi nổi nguồn gốc `origin_known=false` (`a0075, a0079, a0083` trên `mock://vector-db/...`), cùng `2` `training_export` (`a0211, a0212`). Trong `DR-008` (`subj_11`), bản `backup` `a0156` lưu tại `mock://glacier/backup/a0156` có `acl=["vendor-x"]` bị bỏ quên hoàn toàn.
   - **Root cause & khắc phục:** Dựa trên quy ước đặt tên file raw không truy được tới các bản dẫn xuất vốn có `filename = null` và lưu trên các hệ thống khác nhau (`glacier`, `lake`, `vector-db`, `exports`). PoC dựng đồ thị ngược `children_of` từ `parent_ids`, đảm bảo tìm đủ 100% node con và định tuyến các node có vấn đề ACL (`a0156`) hoặc `origin_known=false` (`a0075/79/83`) sang `escalate`.

- **Latency / Compute:** Trên `Linux x86_64` (`Python 3.14.9`), PoC xử lý `158` cặp trong `0.0684s` (~`0.0433s / 100 items`, tức ~`0.43 ms/item`); Baseline chạy `10` dòng hết `0.0243s` (~`0.2430s / 100 items`).

## 07 · Human & decision

- **Review queue (`B = 30`):** Toàn bộ `30` ca ngoại lệ được xếp đầu bảng `out/poc/review_queue.csv` theo thứ tự rủi ro giảm dần (`1.0` legal hold → `0.95` ACL `vendor-x` → `0.90` `origin_known=false` → `0.85` frame/annotation chung → `0.80` training export), kèm file HTML offline `out/poc/contact_sheet.html` có nút **Export decisions CSV**. Bản đã duyệt được lưu tại `reports/review_queue.csv`.
- **Override & Audit log (`out/poc/audit_log.jsonl`):** Mọi quyết định duyệt (`approve`/`escalate`/`edit`/`reject`) đều yêu cầu trường `reviewer` bắt buộc (`load_review_decisions`); các sửa đổi được kiểm tra hợp lệ thuộc `{delete, retain_legal_hold, escalate}`, áp qua `apply_overrides`, và ghi nối tiếp vào `audit_log.jsonl` cùng bản ghi hành động từng artefact (`346` dòng sau 2 lượt chạy trước/sau duyệt, `Audit trace completeness = 1.0000`).
- **Decision policy & bằng chứng chuyển trạng thái (`reports/decision_log.csv`):**
  - Lần chạy 1 (chưa có `review_queue_reviewed.csv`): `status = HOLD`, `reason_code = T08_EXCEPTIONS_PENDING_HUMAN_REVIEW`, `owner = VuVanThang (2A202603239 - Data Governance Lead)`.
  - Lần chạy 2 (sau khi ký duyệt 30 ngoại lệ): `status = GO`, `reason_code = T08_DRY_RUN_REVIEWED_GO`, mở cổng phát hành bản mô phỏng dry-run.
- **Tính năng mở rộng (mốc +24h):**
  - **Idempotency & Resume:** Kiểm tra chạy 2 lần liên tiếp tạo `actions.csv` giống nhau từng byte; checkpoint per-request trong `checkpoint_state.json` cho phép khôi phục tiếp các request còn lại nếu tiến trình bị ngắt giữa chừng.
  - **SLA Dashboard & Notification Stub offline:** Sinh `out/poc/sla_dashboard.html`, `sla_summary.json`, và `notifications.jsonl` (`12` thông báo định tuyến cho `legal_dpo` và `data_steward` hoàn toàn offline).

## 08 · Reproduce & tests

```bash
make setup
.venv/bin/python -m src.main --track t08 --pipeline src.baseline_t08 --output out/baseline --run-name baseline --offline --seed 42
.venv/bin/python -m src.main --input fixtures/t08_deletion --config configs/lab.yaml --output out/ --offline --seed 42
./run.sh
.venv/bin/python -m pytest -q
```

- **Kết quả kiểm thử:** Toàn bộ `tests/test_t08_poc.py` (`5/5` test: E2E + idempotency, precedence + chống rò rỉ bystander, human review override + chặn xóa legal hold + lỗi input, resume sau ngắt job, so sánh Baseline vs PoC) và toàn bộ test hạ tầng/metric trong `pytest -q` đều **PASSED (0 failed)**; các test gắn dấu `requires_gt` tự động skip khi chưa có private overlay `gt/` của giảng viên.
- **Môi trường chạy thực tế:** `Linux-6.18.60-1-lts-x86_64-with-glibc2.44` · `x86_64` · `Python 3.14.9` · `numpy 2.5.4`, `pandas 3.1.1`, `pytest 9.2.0`.

## 09 · Limitations & ethics

- **Rủi ro pháp lý / PII / license:** Fixture `t08_deletion` là đồ thị tổng hợp CC0-1.0 (`mock://...`) và không chứa dữ liệu thật; mã nguồn tuyệt đối không thực thi lệnh xóa tập tin thật trên hệ thống.
- **Giới hạn & điều chưa biết:**
  - Nếu áp dụng ở quy mô lớn gấp 10×–100× (hàng triệu artefact phân tán đa kho), `data_graph.json` cần chuyển sang graph database / lineage catalog phân trang thay vì tải toàn bộ vào RAM.
  - Các `training_export` (`a0211`, `a0212`) mới dừng ở mức gắn cờ `escalate` cho vòng tái tạo tập dữ liệu hoặc machine unlearning; PoC chưa tự động tái huấn luyện model.
  - Khi chạy `src.main` trên một thư mục output mới chưa có `review_queue_reviewed.csv`, quyết định mặc định giữ `HOLD` để bảo đảm con người phải ký duyệt trước khi thực thi xóa thực sự.
- **Khẳng định tuân thủ:** Kết quả dry-run và checklist tự động của PoC là công cụ hỗ trợ kỹ thuật, **không cấu thành tuyên bố "đạt GDPR hay Luật Bảo vệ dữ liệu cá nhân Việt Nam"**. Mọi ngoại lệ về thời hạn lưu trữ, lan truyền `legal_hold`, và chuyển yêu cầu cho bên thứ ba (`vendor-x`) đều cần DPO và bộ phận pháp chế phê duyệt chính thức.

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence** (đối chiếu với `git shortlog -sne --all` và `reports/team_contributions.csv`)

| Học viên | Git author/email | Module / vai trò | Commit / Tag chứng minh | Trình bày? |
|---|---|---|---|---|
| `VuVanThang (2A202603239)` | `VuVanThang <Yusei1708@users.noreply.github.com>` | Lead / Integrator / Core T08 / QA / HITL & Report (`configs/`, `src/t08_deletion.py`, `src/pipeline.py`, `src/baseline_t08.py`, `tests/test_t08_poc.py`, `reports/`, `demo/`) | Commit duy nhất trên nhánh `main` (`demo-180m`, `final-24h`) | Có |

## 11 · References

| URL | Ngày xem | 1–2 dòng ý nghĩa |
|---|---|---|
| `https://gdpr-info.eu/art-17-gdpr/` | `2026-11-14` | Quy định quyền được xóa (Đ.17(1)), nghĩa vụ lan truyền tới bản sao (Đ.17(2)), và các ngoại lệ pháp lý/lưu giữ (Đ.17(3)). |
| `https://privacy.ca.gov/DROP` | `2026-11-14` | Mô hình Delete Request and Opt-out Platform (California SB 362) về cơ chế tiếp nhận yêu cầu xóa tập trung và theo dõi tuân thủ định kỳ. |
| `https://sites.research.google/datacardsplaybook/` | `2026-11-14` | Khung chuẩn hóa tài liệu hóa nguồn gốc (provenance), vòng đời và giới hạn dữ liệu để giảm rủi ro nút mất dấu nguồn (`origin_known=false`). |

---

**Bảng 3 · Checklist chốt khi nộp**

- [x] Link repo và tag cuối cùng (`demo-180m`, `final-24h`) trong `README.md`; input mẫu hoàn toàn synthetic (`CC0-1.0`), không chứa PII thật.
- [x] Đúng 1 lệnh run (`./run.sh` / `python -m src.main ...`), 1 lệnh evaluate (`src.evaluate`), 1 lệnh tests (`pytest -q`); 100% offline.
- [x] Có đầy đủ Baseline vs PoC metrics, 2 failure case kèm metadata & root cause, HITL reviewer evidence, `audit_log.jsonl`, và `decision_log.csv`.
- [x] Có license/source provenance (`manifest.csv` SHA256), seed `42`, môi trường offline, và định nghĩa rõ ràng phần đối chiếu nội bộ.
- [x] Git author/email khớp hoàn toàn giữa `git shortlog -sne --all` và `reports/team_contributions.csv`.
