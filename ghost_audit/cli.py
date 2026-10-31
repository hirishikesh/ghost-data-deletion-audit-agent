"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python -m ghost_audit                     # audit dataset/deletion_requests.csv -> output.csv
    python -m ghost_audit --requests dataset/sample_deletion_requests.csv --output sample_pred.csv
    python -m ghost_audit --offline           # never call a model; use cache + rules only
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .engine import DESTRUCTIVE, HEURISTIC_LINKS, AuditEngine
from .evidence import load_manifest, parse_all
from .identity import IdentityIndex
from .intent import IntentClassifier
from .llm import LLMClient
from .model import valid_ts
from .vision import VisionExtractor

ROOT = Path.cwd()


TIME_FIELDS = ("requested_at", "reported_at", "audit_at")


def build(dataset: Path, llm: LLMClient):
    vision = VisionExtractor(llm)
    gaps: list = []
    observations, coverage, texts = parse_all(dataset, vision=vision, gaps=gaps)
    identities = IdentityIndex(dataset)
    engine = AuditEngine(dataset, identities, observations, coverage, texts, load_manifest(dataset),
                         gaps=gaps)
    engine.vision_rejected = dict(vision.rejected)
    return engine, identities


def case_problem(case: dict) -> str | None:
    """Input checks done before any reasoning: a bad timestamp would silently mis-order."""
    for f in TIME_FIELDS:
        if not valid_ts(case.get(f)):
            return f"{f} is not a UTC timestamp like 2026-09-01T12:00:00Z"
    if case["requested_at"] > case["audit_at"]:
        return "requested_at is after audit_at"
    return None


def run(requests_path: Path, dataset: Path, llm: LLMClient, engine=None, identities=None,
        production_safe: bool = False):
    """Audit every request in `requests_path`. Returns (rows, diagnostics per case).

    production_safe drops destructive remediation that rests on a tied correlation instead
    of evidence; those cases keep their findings and are flagged for human review.
    """
    if engine is None:
        engine, identities = build(dataset, llm)
    intent = IntentClassifier(llm)
    with open(requests_path, encoding="utf-8") as fh:
        cases = list(csv.DictReader(fh))
    # Workflow events are shared across all requests in the dataset, so account linking
    # is solved jointly over every known request, not just the file being audited.
    known = {}
    for path in (dataset / "deletion_requests.csv", dataset / "sample_deletion_requests.csv"):
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                known.update({r["case_id"]: r for r in csv.DictReader(fh)})
    known.update({c["case_id"]: c for c in cases})
    usable = {cid: c for cid, c in known.items() if case_problem(c) is None}
    resolved = {cid: identities.resolve_detail(c["email"], c["requested_at"]) for cid, c in usable.items()}
    engine.workflow_account.clear()
    engine.workflow_unique.clear()
    engine.workflow_choices.clear()
    engine.claimed.clear()
    engine.assign_workflow_accounts(list(usable.values()),
                                    uid_of=lambda c: resolved[c["case_id"]].user_id,
                                    is_deletion=lambda c: intent.is_deletion(c["request_text"]))
    # Anonymised analytics profiles are also linked jointly over every known request, so
    # each has one owner whichever file is audited: pass 1 finds the requests that are
    # otherwise complete, then profiles are assigned, then pass 2 produces the answers.
    subjects = [(c, resolved[cid].user_id) for cid, c in usable.items()
                if resolved[cid].user_id and intent.is_deletion(c["request_text"])]
    engine.analytics_assignment.clear()
    engine.analytics_uncertain.clear()
    first = {c["case_id"]: engine.audit(c, uid) for c, uid in subjects}
    # Eligible: nothing found remaining. A request that is only "not proven yet" (e.g. no
    # record linked) stays eligible - its anonymised profile may be the proof. The second
    # pass applies every proof requirement again.
    engine.assign_anonymised_profiles(
        [(c, uid) for c, uid in subjects
         if not any(s["phase"] == "FINDING" for s in first[c["case_id"]].steps)
         and not any(s["system_id"] == "analytics_platform" for s in first[c["case_id"]].steps)])
    results = {c["case_id"]: engine.audit(c, uid) for c, uid in subjects}
    rows, diagnostics = [], {}
    for case in cases:
        cid = case["case_id"]
        problem = case_problem(case)
        if problem:
            # Cannot place the request in time: nothing can be proven either way.
            rows.append((cid, "INCOMPLETE", []))
            diagnostics[cid] = {"status": "INCOMPLETE", "review": [f"invalid request: {problem}"]}
            continue
        if not intent.is_deletion(case["request_text"]):
            rows.append((cid, "NOT_APPLICABLE", []))
            diagnostics[cid] = {"status": "NOT_APPLICABLE", "intent": "non_deletion",
                                "intent_decided_by": intent.decisions.get(case["request_text"])}
            continue
        who = resolved[cid]
        if who.user_id is None:
            # Unknown or ambiguous subject: no record can be linked, so deletion is unproven.
            rows.append((cid, "INCOMPLETE", []))
            diagnostics[cid] = {"status": "INCOMPLETE", "intent": "deletion",
                                "identity": who.status, "identity_candidates": who.candidates,
                                "review": [f"identity {who.status}: {who.reason}"]}
            continue
        result = results[cid]
        steps = result.steps
        if production_safe:
            steps = strip_unconfirmed_actions(steps, result.links)
        rows.append((cid, result.status, steps))
        diagnostics[cid] = {"status": result.status, "intent": "deletion",
                            "intent_decided_by": intent.decisions.get(case["request_text"]),
                            "identity": who.status, "user_id": who.user_id,
                            "identity_audit_ids": who.audit_ids, "links": result.links,
                            "review": result.review, "notes": result.notes}
    rows.sort(key=lambda r: case_sort_key(r[0]))
    return rows, diagnostics


def strip_unconfirmed_actions(steps: list[dict], links: dict) -> list[dict]:
    """Remove destructive remediation whose target is only a tied correlation."""
    keep = [s for s in steps if not (s["phase"] == "REMEDIATION" and s["operation"] in DESTRUCTIVE
                                     and links.get(s["resource_id"]) in HEURISTIC_LINKS)]
    return [dict(s, step_no=i) for i, s in enumerate(keep, 1)]


def write_diagnostics(diagnostics: dict, gaps: list, rejected: dict, path: Path):
    """One JSON line per case. Holds internal ids only - never emails, phones or names."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for cid in sorted(diagnostics, key=case_sort_key):
            fh.write(json.dumps({"case_id": cid, **diagnostics[cid]}, sort_keys=True) + "\n")
        fh.write(json.dumps({"case_id": "_run", "evidence_gaps": [vars(g) for g in gaps],
                             "screenshot_values_rejected": rejected}, sort_keys=True) + "\n")


def case_sort_key(case_id: str):
    num = case_id.rsplit("_", 1)[-1]
    return (0, int(num)) if num.isdigit() else (1, case_id)


def write_output(rows, path: Path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for case_id, status, steps in rows:
            w.writerow([case_id, status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None, help="defaults to <dataset>/deletion_requests.csv")
    ap.add_argument("--output", default=str(ROOT / "output.csv"))
    ap.add_argument("--offline", action="store_true", help="do not call any model")
    ap.add_argument("--cache-dir", default=str(ROOT / ".cache"))
    ap.add_argument("--usage-log", default=None, help="defaults to <cache-dir>/usage_last_run.json")
    ap.add_argument("--diagnostics", default=None,
                    help="per-case review file (JSONL); defaults to <output>.diagnostics.jsonl")
    ap.add_argument("--production-safe", action="store_true",
                    help="omit destructive steps that rest on a tied correlation instead of evidence")
    ap.add_argument("--strict", action="store_true",
                    help="exit with status 2 if any evidence file could not be read")
    args = ap.parse_args()
    dataset = Path(args.dataset)
    requests = Path(args.requests) if args.requests else dataset / "deletion_requests.csv"
    llm = LLMClient(cache_dir=Path(args.cache_dir), offline=args.offline)
    engine, identities = build(dataset, llm)
    rows, diagnostics = run(requests, dataset, llm, engine, identities,
                            production_safe=args.production_safe)
    write_output(rows, Path(args.output))
    diag_path = Path(args.diagnostics) if args.diagnostics else Path(args.output).with_suffix(".diagnostics.jsonl")
    write_diagnostics(diagnostics, engine.gaps, engine.vision_rejected, diag_path)
    llm.write_usage(Path(args.usage_log) if args.usage_log else Path(args.cache_dir) / "usage_last_run.json",
                    requests=len(rows))
    counts = {}
    for _, s, _ in rows:
        counts[s] = counts.get(s, 0) + 1
    review = sum(1 for d in diagnostics.values() if d.get("review"))
    print(f"wrote {len(rows)} rows to {args.output}: {counts}")
    print(f"{review} case(s) flagged for review, {len(engine.gaps)} evidence file(s) unread -> {diag_path}")
    print(llm.usage_summary(len(rows)))
    if args.strict and engine.gaps:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
