"""Unit & integration tests for Track T08 (Retention & Delete Request Simulator)."""
from __future__ import annotations

import json

import pandas as pd

from src import main as runner
from src.common.io import read_json
from src.metrics.tracks import TRACKS


def _prim(res):
    return [m for m in res.metrics if m.primary][0].value


def _metric(res, name: str):
    return [m for m in res.metrics if m.name == name][0].value


def test_t08_poc_end_to_end_and_idempotency(tmp_path):
    out = tmp_path / "poc"
    rc1 = runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"])
    assert rc1 == 0
    for fname in [
        "actions.csv",
        "review_queue.csv",
        "contact_sheet.html",
        "audit_log.jsonl",
        "decision.json",
        "decision_log.csv",
        "dry_run_summary.json",
        "sla_summary.json",
        "sla_dashboard.html",
        "notifications.jsonl",
        "checkpoint_state.json",
        "run_info.json",
    ]:
        assert (out / fname).exists(), fname

    first_csv = (out / "actions.csv").read_text(encoding="utf-8")
    dec = read_json(out / "decision.json")
    assert dec["status"] == "HOLD" and dec["reason_code"] == "T08_EXCEPTIONS_PENDING_HUMAN_REVIEW"

    # Re-run to verify deterministic idempotency
    rc2 = runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"])
    assert rc2 == 0
    assert (out / "actions.csv").read_text(encoding="utf-8") == first_csv


def test_t08_policy_precedence_and_no_bystander_leak(tmp_path):
    out = tmp_path / "poc"
    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"]) == 0
    df = pd.read_csv(out / "actions.csv")
    assert len(df) == 158
    assert df.duplicated(["request_id", "artefact_id"]).sum() == 0
    assert df["action"].value_counts().to_dict() == {"delete": 128, "escalate": 28, "retain_legal_hold": 2}

    action_of = {(r.request_id, r.artefact_id): r.action for r in df.itertuples(index=False)}
    assert action_of[("DR-002", "a0043")] == "retain_legal_hold"
    assert action_of[("DR-005", "a0099")] == "retain_legal_hold"
    assert action_of[("DR-008", "a0156")] == "escalate"
    for emb in ("a0075", "a0079", "a0083"):
        assert action_of[("DR-004", emb)] == "escalate"
    assert action_of[("DR-001", "a0011")] == "escalate"
    assert action_of[("DR-001", "a0212")] == "escalate"

    # Bystander crops/embeddings belonging exclusively to subj_00 must never be pulled into DR-001 (subj_01)
    assert ("DR-001", "a0012") not in action_of
    assert ("DR-001", "a0013") not in action_of


def test_t08_human_review_override_and_legal_hold_guard(tmp_path):
    out = tmp_path / "poc"
    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"]) == 0

    rq = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    assert len(rq) == 30
    rq["reviewer"] = "VuVanThang"
    rq["reviewer_decision"] = "approve"
    mask = rq["item_id"] == "DR-008:a0156"
    rq.loc[mask, "reviewer_decision"] = "edit"
    rq.loc[mask, "override_value"] = "retain_legal_hold"
    rq.loc[mask, "review_note"] = "Vendor hold confirmation pending"
    rq.to_csv(out / "review_queue_reviewed.csv", index=False)

    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"]) == 0
    df = pd.read_csv(out / "actions.csv")
    assert df.loc[(df["request_id"] == "DR-008") & (df["artefact_id"] == "a0156"), "action"].item() == "retain_legal_hold"
    assert read_json(out / "decision.json")["status"] == "GO"

    audit_actions = [json.loads(line)["action"] for line in (out / "audit_log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "override:edit" in audit_actions

    # Attempting to override a legal-hold item to `delete` must trigger HOLD violation
    lh_mask = rq["item_id"] == "DR-002:a0043"
    rq.loc[lh_mask, "reviewer_decision"] = "edit"
    rq.loc[lh_mask, "override_value"] = "delete"
    rq.loc[lh_mask, "review_note"] = "unsafe override test"
    rq.to_csv(out / "review_queue_reviewed.csv", index=False)

    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"]) == 0
    dec_bad = read_json(out / "decision.json")
    assert dec_bad["status"] == "HOLD" and dec_bad["reason_code"] == "LEGAL_HOLD_VIOLATION"

    # Invalid override action must fail cleanly with exit code 2
    rq.loc[lh_mask, "override_value"] = "purge_everything"
    rq.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(out), "--offline", "--seed", "42"]) == 2


def test_t08_resume_after_interruption(tmp_path):
    full_out = tmp_path / "full"
    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(full_out), "--offline", "--seed", "42"]) == 0

    partial_out = tmp_path / "partial"
    partial_out.mkdir()
    ckpt = read_json(full_out / "checkpoint_state.json")
    ckpt["completed"] = {k: v for k, v in ckpt["completed"].items() if k <= "DR-005"}
    ckpt["completed_count"] = len(ckpt["completed"])
    (partial_out / "checkpoint_state.json").write_text(json.dumps(ckpt), encoding="utf-8")

    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(partial_out), "--offline", "--seed", "42"]) == 0
    info = read_json(partial_out / "run_info.json")
    assert info["stats"]["resumed_requests"] == 5
    assert info["stats"]["newly_processed_requests"] == 5
    assert (partial_out / "actions.csv").read_text(encoding="utf-8") == (full_out / "actions.csv").read_text(encoding="utf-8")


def test_t08_baseline_vs_poc_metrics(tmp_path):
    base_out = tmp_path / "baseline"
    poc_out = tmp_path / "poc"
    assert runner.main(["--track", "t08", "--pipeline", "src.baseline_t08", "--output", str(base_out), "--offline", "--seed", "42"]) == 0
    assert runner.main(["--config", "configs/lab.yaml", "--track", "t08", "--output", str(poc_out), "--offline", "--seed", "42"]) == 0

    # Verify against internal evaluation labels built outside fixtures/*/gt/
    gt_path = tmp_path / "gt_dev.csv"
    pd.read_csv("reports/internal_gt_t08.csv").to_csv(gt_path, index=False)

    res_base = TRACKS["t08"].evaluator(gt_path, base_out / "actions.csv", {})
    res_poc = TRACKS["t08"].evaluator(gt_path, poc_out / "actions.csv", {})

    assert _prim(res_base) < 0.1
    assert _metric(res_base, "False-delete count") == 2.0
    assert _prim(res_poc) == 1.0
    assert _metric(res_poc, "False-delete count") == 0.0
    assert _metric(res_poc, "Exception handling accuracy") == 1.0
    assert _metric(res_poc, "Audit trace completeness") == 1.0
