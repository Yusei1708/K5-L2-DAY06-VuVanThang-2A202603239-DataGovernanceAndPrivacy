"""T08 baseline: delete only matched raw_video files by filename, with no lineage or exception checks."""
from __future__ import annotations

import pandas as pd

from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import make_review_queue, write_review_queue
from .metrics.tracks import get_track
from .t08_deletion import load_t08_inputs


def run(ctx: RunContext) -> None:
    """Execute the naive T08 filename-matching baseline."""
    if ctx.track != "t08":
        raise NotImplementedError(f"src.baseline_t08 supports only t08 (got {ctx.track})")

    spec = get_track(ctx.track)
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems and ctx.config.get("strict_provenance", True):
        raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")

    data = load_t08_inputs(ctx.input_dir)
    rows: list[dict] = []
    for req in data["requests"]:
        rid = req["request_id"]
        sid = req["subject_id"]
        # Naive baseline matches only the single raw_video filename containing subject_id.
        matches = [
            a
            for a in data["artefacts"]
            if a.get("type") == "raw_video" and sid in str(a.get("filename") or "")
        ]
        for art in sorted(matches, key=lambda x: x["artefact_id"]):
            rows.append(
                {
                    "request_id": rid,
                    "artefact_id": art["artefact_id"],
                    "action": "delete",
                    "item_id": f"{rid}:{art['artefact_id']}",
                    "score": 0.5,
                    "artefact_type": art.get("type", ""),
                    "reason": f"filename_match:{art.get('filename')}",
                }
            )

    df = pd.DataFrame(rows)
    ctx.stats["n_items"] = len(df)
    ctx.stats["n_requests"] = len(data["requests"])

    budget = int(ctx.param("budget", 30))
    queue = make_review_queue(
        df,
        id_col="item_id",
        score_col="score",
        budget=budget,
        reason_col="reason",
        action_col="action",
        slice_col="artefact_type",
    )
    write_review_queue(queue, ctx.out("review_queue.csv"))
    render_contact_sheet(
        queue,
        ctx.out("contact_sheet.html"),
        image_col=None,
        fields=("rank", "item_id", "score", "reason", "suggested_action", "slice"),
        title="t08 raw-only baseline queue",
    )

    if ctx.audit:
        # Log summary only (no per-artefact trace), reflecting baseline audit gap.
        ctx.audit.log(
            actor="baseline",
            action="baseline_raw_filename_delete",
            item_id="baseline_batch",
            after={"n_rows": len(df)},
            reason="Raw filename deletion only; no lineage traversal or hold check",
        )

    cols = spec.pred_columns + [c for c in df.columns if c not in spec.pred_columns]
    df[cols].to_csv(ctx.out(spec.pred_file), index=False)

    write_decision(
        Decision(
            status="HOLD",
            reason_code="BASELINE_RAW_ONLY",
            reason="Raw-filename baseline skips lineage and exception checks; never release on this.",
            owner=ctx.config.get("owner", "baseline"),
            scope=ctx.track,
            evidence=[str(ctx.out(spec.pred_file))],
            metrics={"n_items": len(df)},
        ),
        ctx.out("decision.json"),
        ctx.out("decision_log.csv"),
    )
