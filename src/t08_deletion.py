"""Track T08 PoC core engine: lineage traversal, policy precedence, checkpointing, SLA & notifications."""
from __future__ import annotations

import html
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .common.context import RunContext
from .common.io import config_hash, read_json, sha256_file, write_json

VALID_ACTIONS = {"delete", "retain_legal_hold", "escalate"}

_CLEAN_TYPE_PRIORITY = {
    "embedding": 0.15,
    "face_crop": 0.14,
    "raw_video": 0.13,
    "backup": 0.12,
    "frame": 0.11,
    "annotation": 0.10,
}


def load_t08_inputs(input_dir: Path) -> dict[str, Any]:
    """Load data_graph.json and requests.json for T08 and build lookup indices."""
    graph_path = input_dir / "data_graph.json"
    req_path = input_dir / "requests.json"
    if not graph_path.exists():
        raise FileNotFoundError(f"Missing T08 graph file: {graph_path}")
    if not req_path.exists():
        raise FileNotFoundError(f"Missing T08 requests file: {req_path}")

    graph = read_json(graph_path)
    requests = read_json(req_path)
    artefacts = graph.get("artefacts", [])
    by_id: dict[str, dict] = {a["artefact_id"]: a for a in artefacts}
    children_of: dict[str, list[str]] = defaultdict(list)
    for art in artefacts:
        for pid in art.get("parent_ids") or []:
            children_of[pid].append(art["artefact_id"])
    for pid in list(children_of):
        children_of[pid].sort()

    return {
        "graph": graph,
        "artefacts": artefacts,
        "by_id": by_id,
        "children_of": dict(children_of),
        "requests": sorted(requests, key=lambda r: r["request_id"]),
        "graph_sha256": sha256_file(graph_path),
        "requests_sha256": sha256_file(req_path),
    }


def _subject_matches_filename(artefact: dict, subject_id: str) -> bool:
    fn = str(artefact.get("filename") or "")
    return f"_{subject_id}_" in fn or fn.startswith(f"{subject_id}_")


def trace_subject_lineage(
    subject_id: str,
    artefacts: list[dict],
    by_id: dict[str, dict],
    children_of: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Trace all artefacts linked to `subject_id` via subject-aware BFS traversal.

    Child artefacts that carry a non-empty `subject_ids` list without `subject_id` belong
    to another subject (for example a bystander face crop branched off a shared frame)
    and are excluded rather than traversed.
    """
    seeds = sorted(
        a["artefact_id"]
        for a in artefacts
        if subject_id in (a.get("subject_ids") or []) or _subject_matches_filename(a, subject_id)
    )
    visited = set(seeds)
    queue: deque[str] = deque(seeds)

    while queue:
        cur = queue.popleft()
        for child_id in children_of.get(cur, []):
            if child_id in visited:
                continue
            child = by_id[child_id]
            child_subs = child.get("subject_ids") or []
            if child_subs and subject_id not in child_subs:
                continue
            visited.add(child_id)
            queue.append(child_id)

    # Reconstruct deterministic root-to-node path via in-scope parents (handles cycles safely).
    memo: dict[str, list[str]] = {}

    def _build_path(aid: str, stack: set[str]) -> list[str]:
        if aid in memo:
            return memo[aid]
        parents_in_scope = [p for p in sorted(by_id[aid].get("parent_ids") or []) if p in visited and p not in stack]
        if not parents_in_scope:
            memo[aid] = [aid]
            return memo[aid]
        stack.add(aid)
        best_parent = min((_build_path(p, stack) for p in parents_in_scope), key=len)
        stack.remove(aid)
        memo[aid] = best_parent + [aid]
        return memo[aid]

    return {aid: _build_path(aid, set()) for aid in sorted(visited)}


def classify_artefact_action(
    artefact: dict,
    allowed_actor: str = "data-platform",
    inherited_hold: str | None = None,
) -> tuple[str, float, str, str]:
    """Apply T08 policy precedence: legal hold > permission/provenance > multi-subject > delete."""
    hold_id = artefact.get("legal_hold") or inherited_hold
    if hold_id:
        return ("retain_legal_hold", 1.00, f"legal_hold:{hold_id}", "legal_hold")

    acl = list(artefact.get("acl") or [])
    owner = artefact.get("owner")
    if allowed_actor not in acl or (owner is not None and owner not in acl):
        return ("escalate", 0.95, f"acl_denied:acl={acl}", "permission_acl")

    if not artefact.get("origin_known", True):
        return ("escalate", 0.90, "origin_unknown:missing_provenance", "unknown_origin")

    subjects = list(artefact.get("subject_ids") or [])
    if len(subjects) > 1:
        art_type = artefact.get("type", "artefact")
        score = 0.80 if art_type == "training_export" else 0.85
        return ("escalate", score, f"multi_subject:{len(subjects)}_subjects:{art_type}", "multi_subject")

    art_type = artefact.get("type", "artefact")
    score = _CLEAN_TYPE_PRIORITY.get(art_type, 0.10)
    return ("delete", score, "lineage_verified_single_subject", "clean_delete")


def _inherited_legal_hold_map(
    lineage_paths: dict[str, list[str]],
    by_id: dict[str, dict],
) -> dict[str, str | None]:
    """Propagate any non-null legal_hold along an in-scope subject lineage path."""
    holds: dict[str, str | None] = {}
    for aid, path in lineage_paths.items():
        hold = None
        for ancestor_id in path:
            ancestor_hold = by_id[ancestor_id].get("legal_hold")
            if ancestor_hold:
                hold = str(ancestor_hold)
                break
        holds[aid] = hold
    return holds


def _evaluate_single_request(
    req: dict,
    data: dict[str, Any],
    allowed_actor: str,
    propagate_legal_hold: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    rid = req["request_id"]
    sid = req["subject_id"]
    lineage = trace_subject_lineage(sid, data["artefacts"], data["by_id"], data["children_of"])
    inherited_holds = _inherited_legal_hold_map(lineage, data["by_id"]) if propagate_legal_hold else {}

    rows: list[dict[str, Any]] = []
    for aid, path in lineage.items():
        art = data["by_id"][aid]
        action, score, reason, exc_cat = classify_artefact_action(
            art,
            allowed_actor=allowed_actor,
            inherited_hold=inherited_holds.get(aid),
        )
        rows.append(
            {
                "request_id": rid,
                "artefact_id": aid,
                "action": action,
                "item_id": f"{rid}:{aid}",
                "score": round(score, 6),
                "subject_id": sid,
                "artefact_type": art.get("type", ""),
                "exception_category": exc_cat,
                "reason": reason,
                "lineage_depth": len(path) - 1,
                "lineage_path": "->".join(path),
                "uri": art.get("uri", ""),
                "legal_hold": art.get("legal_hold") or "",
                "origin_known": bool(art.get("origin_known", True)),
                "acl": ";".join(art.get("acl") or []),
                "n_subjects": len(art.get("subject_ids") or []),
                "deadline": req.get("deadline", ""),
                "dry_run": dry_run,
                "reviewed_by": "",
                "reviewer_decision": "",
            }
        )
    return rows


def process_requests_with_checkpoint(ctx: RunContext, data: dict[str, Any]) -> pd.DataFrame:
    """Process all T08 deletion requests deterministically with resume checkpoint support."""
    allowed_actor = str(ctx.param("allowed_actor", "data-platform"))
    propagate_legal_hold = bool(ctx.param("propagate_legal_hold", False))
    dry_run = bool(ctx.param("dry_run", True))
    resume_enabled = bool(ctx.param("resume", True))

    input_digest = config_hash(
        {
            "graph_sha256": data["graph_sha256"],
            "requests_sha256": data["requests_sha256"],
            "allowed_actor": allowed_actor,
            "propagate_legal_hold": propagate_legal_hold,
            "dry_run": dry_run,
        }
    )
    ckpt_path = ctx.output_dir / "checkpoint_state.json"
    completed_by_req: dict[str, list[dict[str, Any]]] = {}
    resumed_count = 0

    if resume_enabled and ckpt_path.exists():
        saved = read_json(ckpt_path)
        if saved.get("input_digest") == input_digest and isinstance(saved.get("completed"), dict):
            completed_by_req = dict(saved["completed"])
            resumed_count = sum(1 for r in data["requests"] if r["request_id"] in completed_by_req)

    all_rows: list[dict[str, Any]] = []
    for req in data["requests"]:
        rid = req["request_id"]
        if rid in completed_by_req:
            req_rows = completed_by_req[rid]
        else:
            req_rows = _evaluate_single_request(
                req,
                data,
                allowed_actor=allowed_actor,
                propagate_legal_hold=propagate_legal_hold,
                dry_run=dry_run,
            )
            completed_by_req[rid] = req_rows
            write_json(
                ckpt_path,
                {
                    "track": ctx.track,
                    "input_digest": input_digest,
                    "completed_count": len(completed_by_req),
                    "total_requests": len(data["requests"]),
                    "completed": completed_by_req,
                },
            )
        all_rows.extend(req_rows)

    ctx.stats["n_requests"] = len(data["requests"])
    ctx.stats["resumed_requests"] = resumed_count
    ctx.stats["newly_processed_requests"] = len(data["requests"]) - resumed_count

    df = pd.DataFrame(all_rows)
    return df.sort_values(["request_id", "artefact_id"], kind="mergesort").reset_index(drop=True)


def write_t08_artifacts(ctx: RunContext, data: dict[str, Any], final: pd.DataFrame) -> dict[str, Any]:
    """Write dry-run diff, simulated deletion ledger, SLA dashboard, and offline notifications."""
    dry_run = bool(ctx.param("dry_run", True))
    ref_raw = str(ctx.param("reference_time", "2026-11-14T15:00:00+07:00"))
    warn_days = int(ctx.param("sla_warn_days", 25))
    ref_dt = datetime.fromisoformat(ref_raw)

    counts = final["action"].value_counts().to_dict()
    exc_counts = final[final["action"] != "delete"]["exception_category"].value_counts().to_dict()

    # 1. Dry-run diff & simulated deletion ledger (non-destructive)
    deletes = final[final["action"] == "delete"]
    holds = final[final["action"] == "retain_legal_hold"]
    escalates = final[final["action"] == "escalate"]

    dry_run_summary = {
        "mode": "dry_run" if dry_run else "simulated_delete",
        "total_pairs": int(len(final)),
        "action_counts": {k: int(v) for k, v in sorted(counts.items())},
        "exception_breakdown": {k: int(v) for k, v in sorted(exc_counts.items())},
        "legal_hold_items": holds[["request_id", "artefact_id", "legal_hold", "uri"]].to_dict(orient="records"),
        "simulated_deleted_count": int(len(deletes)),
        "simulated_deleted_uris": deletes[["request_id", "artefact_id", "artefact_type", "uri"]].to_dict(orient="records"),
        "escalated_items_count": int(len(escalates)),
    }
    write_json(ctx.out("dry_run_summary.json"), dry_run_summary)

    # 2. Per-request SLA dashboard & offline notification payloads
    sla_rows: list[dict[str, Any]] = []
    notif_lines: list[str] = []
    for req in data["requests"]:
        rid = req["request_id"]
        sub_df = final[final["request_id"] == rid]
        deadline_dt = datetime.fromisoformat(req["deadline"])
        days_left = round((deadline_dt - ref_dt).total_seconds() / 86400.0, 2)
        n_del = int((sub_df["action"] == "delete").sum())
        n_hold = int((sub_df["action"] == "retain_legal_hold").sum())
        n_esc = int((sub_df["action"] == "escalate").sum())

        if days_left < 0:
            sla_status = "BREACHED"
        elif n_hold > 0:
            sla_status = "BLOCKED_LEGAL_HOLD"
        elif days_left <= warn_days or n_esc > 0:
            sla_status = "AT_RISK_NEEDS_REVIEW"
        else:
            sla_status = "ON_TRACK"

        sla_rows.append(
            {
                "request_id": rid,
                "subject_id": req["subject_id"],
                "received_at": req.get("received_at", ""),
                "deadline": req["deadline"],
                "days_remaining": days_left,
                "total_artefacts": int(len(sub_df)),
                "delete": n_del,
                "retain_legal_hold": n_hold,
                "escalate": n_esc,
                "sla_status": sla_status,
            }
        )

        if n_hold > 0:
            hold_codes = sorted({x for x in sub_df.loc[sub_df["action"] == "retain_legal_hold", "legal_hold"] if x})
            notif_lines.append(
                json.dumps(
                    {
                        "channel": "offline_stub:legal_dpo",
                        "request_id": rid,
                        "subject_id": req["subject_id"],
                        "event": "LEGAL_HOLD_EXCEPTION",
                        "legal_holds": hold_codes,
                        "deadline": req["deadline"],
                        "message": f"Hold raw artefact under {hold_codes}; schedule deletion upon hold release.",
                    },
                    ensure_ascii=False,
                )
            )
        if n_esc > 0:
            cats = sorted(set(sub_df.loc[sub_df["action"] == "escalate", "exception_category"]))
            notif_lines.append(
                json.dumps(
                    {
                        "channel": "offline_stub:data_steward",
                        "request_id": rid,
                        "subject_id": req["subject_id"],
                        "event": "ESCALATION_REVIEW_REQUIRED",
                        "categories": cats,
                        "count": n_esc,
                        "deadline": req["deadline"],
                        "message": f"{n_esc} escalated artefacts require human sign-off ({', '.join(cats)}).",
                    },
                    ensure_ascii=False,
                )
            )

    sla_summary = {
        "reference_time": ref_raw,
        "sla_warn_days": warn_days,
        "requests": sla_rows,
    }
    write_json(ctx.out("sla_summary.json"), sla_summary)
    ctx.out("notifications.jsonl").write_text("\n".join(notif_lines) + ("\n" if notif_lines else ""), encoding="utf-8")
    _write_sla_dashboard_html(ctx.out("sla_dashboard.html"), sla_rows, counts, dry_run)
    return {"dry_run_summary": dry_run_summary, "sla_summary": sla_summary}


def _write_sla_dashboard_html(path: Path, sla_rows: list[dict[str, Any]], counts: dict[str, int], dry_run: bool) -> None:
    rows_html = []
    for r in sla_rows:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(r['request_id'])}</td>"
            f"<td>{html.escape(r['subject_id'])}</td>"
            f"<td>{html.escape(r['deadline'])}</td>"
            f"<td>{r['days_remaining']}</td>"
            f"<td>{r['total_artefacts']}</td>"
            f"<td>{r['delete']}</td>"
            f"<td>{r['retain_legal_hold']}</td>"
            f"<td>{r['escalate']}</td>"
            f"<td><strong>{html.escape(r['sla_status'])}</strong></td>"
            "</tr>"
        )
    doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>T08 Retention & Deletion SLA Dashboard</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:20px;color:#222}"
        "table{border-collapse:collapse;width:100%;margin-top:12px}"
        "th,td{border:1px solid #ddd;padding:6px 8px;font-size:13px;text-align:left}"
        "th{background:#f3f4f6}</style></head><body>"
        f"<h2>T08 Retention &amp; Delete Request SLA Dashboard ({'DRY-RUN' if dry_run else 'SIMULATED'})</h2>"
        f"<p>Totals — delete: {counts.get('delete', 0)}, retain_legal_hold: {counts.get('retain_legal_hold', 0)}, "
        f"escalate: {counts.get('escalate', 0)}</p>"
        "<table><thead><tr><th>Request</th><th>Subject</th><th>Deadline</th><th>Days Left</th>"
        "<th>Total</th><th>Delete</th><th>Legal Hold</th><th>Escalate</th><th>SLA Status</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></body></html>"
    )
    path.write_text(doc, encoding="utf-8")
