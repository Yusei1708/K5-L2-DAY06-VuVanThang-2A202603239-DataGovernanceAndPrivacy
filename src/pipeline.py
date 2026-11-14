"""YOUR PoC PIPELINE — Track T08 (Retention & Delete Request Simulator).

Lab contract = 5 automated steps, run by `python -m src.main ...`:
    01 load      input + provenance check (manifest.csv)
    02 process   lineage BFS + policy precedence + checkpoint/resume
    03 rank      prioritise top-B exceptions for human review
    04 review    human-in-the-loop: queue -> reviewer decisions -> overrides + audit log
    05 decide    GO / HOLD / REWORK with reason_code + owner, write outputs
"""
from __future__ import annotations

import pandas as pd

from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import apply_overrides, load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track
from .t08_deletion import (
    VALID_ACTIONS,
    load_t08_inputs,
    process_requests_with_checkpoint,
    write_t08_artifacts,
)


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        if ctx.audit:
            ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    data: dict = {"manifest": pd.read_csv(manifest) if manifest.exists() else None}
    if ctx.track == "t08":
        data.update(load_t08_inputs(ctx.input_dir))
    return data


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    spec = get_track(ctx.track)
    if ctx.track != "t08":
        raise NotImplementedError(
            f"Implement process() for {spec.track} ({spec.title}). It must produce a DataFrame with at least "
            f"{spec.pred_columns} (saved as out/{spec.pred_file}). See docs/tracks/{spec.track.upper()}.md"
        )
    return process_requests_with_checkpoint(ctx, data)


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Top-B exceptions by `score`, exporting CSV queue and offline HTML contact sheet."""
    budget = int(ctx.param("budget", 30))
    if "score" not in scored.columns:
        raise ValueError("rank(): process() output needs a numeric `score` column")
    id_col = "item_id" if "item_id" in scored.columns else scored.columns[0]
    slice_col = "exception_category" if "exception_category" in scored.columns else None
    q = make_review_queue(
        scored,
        id_col=id_col,
        score_col="score",
        budget=budget,
        reason_col="reason" if "reason" in scored.columns else None,
        action_col="action" if "action" in scored.columns else None,
        slice_col=slice_col,
    )
    write_review_queue(q, ctx.out("review_queue.csv"))
    render_contact_sheet(
        q,
        ctx.out("contact_sheet.html"),
        image_col=None,
        fields=("rank", "item_id", "score", "reason", "suggested_action", "slice"),
        title=f"{ctx.track.upper()} Human Review Queue ({ctx.run_name})",
    )
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Apply reviewed queue decisions/overrides and write append-only audit log entries."""
    out = scored.copy()
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        bad_overrides = [
            str(r.override_value).strip().lower()
            for r in reviewed.itertuples(index=False)
            if r.reviewer_decision in {"edit", "reject"} and str(r.override_value).strip() != ""
            and str(r.override_value).strip().lower() not in VALID_ACTIONS
        ]
        if bad_overrides:
            raise ValueError(f"Invalid T08 override_value(s) {bad_overrides}; expected one of {sorted(VALID_ACTIONS)}")

        reviewed_norm = reviewed.copy()
        override_mask = reviewed_norm["reviewer_decision"].isin({"edit", "reject"}) & (reviewed_norm["override_value"].str.strip() != "")
        reviewed_norm.loc[override_mask, "override_value"] = (
            reviewed_norm.loc[override_mask, "override_value"].str.strip().str.lower()
        )
        ctx.stats["n_reviewed"] = len(reviewed_norm)

        out = apply_overrides(out, reviewed_norm, id_col="item_id", value_col="action", audit=ctx.audit)
        rev_map = reviewed_norm.set_index("item_id").to_dict(orient="index")
        for idx, row in out.iterrows():
            info = rev_map.get(str(row["item_id"]))
            if not info:
                continue
            out.at[idx, "reviewed_by"] = info["reviewer"]
            out.at[idx, "reviewer_decision"] = info["reviewer_decision"]
            if info["reviewer_decision"] not in {"edit", "reject"} or not info["override_value"]:
                if ctx.audit:
                    ctx.audit.log(
                        actor=info["reviewer"],
                        action=f"human_review:{info['reviewer_decision']}",
                        item_id=str(row["artefact_id"]),
                        before=row["action"],
                        after=row["action"],
                        reason=info["review_note"] or str(row["reason"]),
                        request_id=row["request_id"],
                        pair_id=row["item_id"],
                    )
            else:
                out.at[idx, "reason"] = f"human_override:{info['reviewer_decision']}:{info['review_note'] or 'reviewed'}"

    if ctx.audit:
        mode = "dry_run" if bool(ctx.param("dry_run", True)) else "simulated_exec"
        for row in out.itertuples(index=False):
            ctx.audit.log(
                actor="pipeline",
                action=f"{mode}:{row.action}",
                item_id=str(row.artefact_id),
                after={"request_id": row.request_id, "action": row.action, "pair_id": row.item_id},
                reason=str(row.reason),
            )
    return out


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    """Risk-gated release decision enforcing zero unauthorized or legal-hold deletions."""
    if ctx.track != "t08":
        return Decision(
            status="HOLD",
            reason_code="POLICY_NOT_IMPLEMENTED",
            reason="Decision policy implemented for T08 only",
            owner=ctx.config.get("owner", "TODO-owner"),
            scope=ctx.track,
        )

    legal_hold_deleted = final[(final["legal_hold"].astype(str).str.strip() != "") & (final["action"] == "delete")]
    unreviewed_exc_deleted = final[
        final["exception_category"].isin({"permission_acl", "unknown_origin", "multi_subject"})
        & (final["action"] == "delete")
        & (final["reviewer_decision"].astype(str).str.strip() == "")
    ]

    counts = {k: int(v) for k, v in final["action"].value_counts().items()}
    dry_run = bool(ctx.param("dry_run", True))
    evidence = [
        str(ctx.out("actions.csv")),
        str(ctx.out("audit_log.jsonl")),
        str(ctx.out("review_queue.csv")),
        str(ctx.out("dry_run_summary.json")),
        str(ctx.out("sla_summary.json")),
    ]

    if len(legal_hold_deleted) > 0:
        return Decision(
            status="HOLD",
            reason_code="LEGAL_HOLD_VIOLATION",
            reason=f"Blocked: {len(legal_hold_deleted)} legal-hold artefact(s) marked for deletion.",
            owner=ctx.config.get("owner", "VuVanThang"),
            scope=ctx.track,
            evidence=evidence,
            metrics=counts,
        )
    if len(unreviewed_exc_deleted) > 0:
        return Decision(
            status="REWORK",
            reason_code="UNREVIEWED_EXCEPTION_DELETE",
            reason=f"Rework: {len(unreviewed_exc_deleted)} exception item(s) deleted without human sign-off.",
            owner=ctx.config.get("owner", "VuVanThang"),
            scope=ctx.track,
            evidence=evidence,
            metrics=counts,
        )

    n_exc = int((final["action"] != "delete").sum())
    n_rev = int(ctx.stats.get("n_reviewed", 0))
    status = "GO" if (n_exc == 0 or n_rev > 0) else "HOLD"
    reason_code = (
        ("T08_DRY_RUN_REVIEWED_GO" if dry_run else "T08_EXECUTION_REVIEWED_GO")
        if status == "GO"
        else "T08_EXCEPTIONS_PENDING_HUMAN_REVIEW"
    )
    reason = (
        f"Dry-run validated {len(final)} lineage pairs ({counts.get('delete', 0)} delete, "
        f"{counts.get('retain_legal_hold', 0)} legal hold, {counts.get('escalate', 0)} escalate) "
        f"with human review sign-off ({n_rev} items) and full audit trail."
        if status == "GO"
        else f"Dry-run isolated {n_exc} exception rows pending human review sign-off in review_queue_reviewed.csv."
    )
    return Decision(
        status=status,
        reason_code=reason_code,
        reason=reason,
        owner=ctx.config.get("owner", "VuVanThang"),
        scope=ctx.track,
        evidence=evidence,
        metrics={**counts, "n_reviewed": n_rev},
    )


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    if ctx.track == "t08":
        write_t08_artifacts(ctx, data, final)
    ordered_cols = spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]
    final[ordered_cols].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
