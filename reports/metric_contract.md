# Metric contract (khai báo bắt buộc — chốt trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py::eval_t08`. File này là bản cam kết cách đánh giá cho Track T08.
> Không thay đổi testset, threshold hay công thức sau khi thấy kết quả.

| Mục | Khai báo chính thức (Track T08) |
|---|---|
| Track | `t08` · Retention & Delete Request Simulator (`fixtures/t08_deletion/`) |
| Primary metric (tên đúng như evaluator in ra) | `Deletion Completeness` |
| Tăng tốt hay giảm tốt? | `higher` (càng cao càng tốt, mục tiêu = `1.0000`) |
| Đơn vị đánh giá | Cặp `(request_id, artefact_id)` trên `10` yêu cầu xóa (`DR-001`..`DR-010`) |
| Công thức + numerator / denominator | `done / len(must)` với `done = count(expected_action == "delete" AND pred_action == "delete")` và `len(must) = 128` |
| Cách ghép prediction ↔ GT | Exact key join trên `(request_id, artefact_id)` không trùng khóa; `action` chuẩn hóa viết thường trong `{delete, retain_legal_hold, escalate}` |
| Candidate pool cố định | `10` yêu cầu trong `requests.json` × đồ thị `212` artefact trong `data_graph.json` (`158` cặp thuộc phạm vi xóa/ngoại lệ) |
| Review budget B / K | `B = 30` (khớp toàn bộ 30 ca ngoại lệ: `2` `retain_legal_hold` + `28` `escalate`) |
| Seed | `42` |
| Threshold / Policy precedence (đóng băng trước eval) | Theo `configs/tracks/t08.yaml`: `legal_hold != null -> retain_legal_hold` (`score=1.00`) > `acl thiếu actor/owner -> escalate` (`score=0.95`) > `origin_known == false -> escalate` (`score=0.90`) > `len(subject_ids) > 1 -> escalate` (`score=0.85/0.80`) > `clean single-subject -> delete` (`score<=0.15`); `propagate_legal_hold: false`, `dry_run: true`, `resume: true` |
| Eval split (không dùng để tune) | Toàn bộ `158` cặp `(request_id, artefact_id)` được evaluator chấm đồng bộ; đối chiếu nội bộ bằng `reports/internal_gt_t08.csv` (ngoài `fixtures/*/gt/`), đánh giá chính thức dùng private `fixtures/t08_deletion/gt/gt_actions.csv` của giảng viên |
| Chỉ số lỗi xấu | 1) `False-delete count` (`lower`, mục tiêu = `0`): tổng số ca ngoại lệ bị gán `delete` + ca xóa ngoài phạm vi yêu cầu (`stray`); 2) `Exception handling accuracy` (`higher`, mục tiêu = `1.0`); 3) `Audit trace completeness` (`higher`, mục tiêu = `1.0`) |
| Slices sẽ báo cáo (≥2) | `delete` (`N=128`), `escalate` (`N=28`), `retain_legal_hold` (`N=2`); bổ sung theo `exception_category` (`clean_delete`, `multi_subject`, `unknown_origin`, `legal_hold`, `permission_acl`) |
| Cách báo độ bất định khi N nhỏ | Với slice nhỏ như `retain_legal_hold` (`N=2`), báo nguyên phân số đếm chính xác (`numerator/denominator`) kèm phân tích từng ca cụ thể (`DR-002:a0043`, `DR-005:a0099`) thay vì suy diễn tỷ lệ phần trăm đơn lẻ |
| Ngày giờ chốt + người chốt | `2026-11-14 14:45 +07:00` · `VuVanThang (2A202603239 - Data Governance Lead)` |

Ghi chú: Với hai chỉ số `Exception handling accuracy` và `Audit trace completeness`, giá trị Baseline bằng `0.0000` nên bảng so sánh ghi rõ `baseline=0` thay vì phần trăm cải thiện.
