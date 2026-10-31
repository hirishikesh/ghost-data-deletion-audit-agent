"""Score predictions against labelled answers.

Usage (from the repository root):
    python evaluation/evaluate.py                    # run the agent on the 32 samples and score
    python evaluation/evaluate.py --pred sample_pred.csv --gold dataset/sample_output.csv

Metrics
- status accuracy:   deletion_status matches.
- step-level P/R/F1: steps compared as (phase, system_id, resource_id, operation, state,
                     evidence_id) multisets, ignoring order and step_no.
- field-level:       steps aligned by position; share of the 7 fields that match, over the
                     longer of the two arrays (missing/extra steps count as all-wrong).
- sequence accuracy: whole steps_json array identical (order included).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path: Path) -> dict[str, tuple[str, list]]:
    with open(path, encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
                for r in csv.DictReader(fh)}


def step_key(s: dict):
    return tuple(s[f] for f in FIELDS[1:])


def score(pred: dict, gold: dict) -> dict:
    n = len(gold)
    status_ok = tp = n_pred = n_gold = field_ok = field_total = seq_ok = 0
    per_case = {}
    for cid, (gstatus, gsteps) in gold.items():
        pstatus, psteps = pred.get(cid, ("MISSING", []))
        status_ok += pstatus == gstatus
        pc, gc = Counter(map(step_key, psteps)), Counter(map(step_key, gsteps))
        match = sum((pc & gc).values())
        tp += match
        n_pred += len(psteps)
        n_gold += len(gsteps)
        width = max(len(psteps), len(gsteps))
        ok = sum(1 for i in range(min(len(psteps), len(gsteps))) for f in FIELDS
                 if psteps[i].get(f) == gsteps[i].get(f))
        field_ok += ok
        field_total += width * len(FIELDS)
        same = [step_key(s) for s in psteps] == [step_key(s) for s in gsteps]
        seq_ok += same and pstatus == gstatus
        per_case[cid] = {"status": (pstatus, gstatus), "missing": sorted((gc - pc).elements()),
                         "extra": sorted((pc - gc).elements()), "exact": same and pstatus == gstatus}
    precision = tp / n_pred if n_pred else 1.0
    recall = tp / n_gold if n_gold else 1.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": field_ok / field_total if field_total else 1.0,
        "sequence_accuracy": seq_ok / n,
        "per_case": per_case,
    }


def report(result: dict, show_failures: bool = True) -> str:
    lines = [f"cases: {result['cases']}",
             f"status accuracy:   {result['status_accuracy']:.3f}",
             f"step precision:    {result['step_precision']:.3f}",
             f"step recall:       {result['step_recall']:.3f}",
             f"step F1:           {result['step_f1']:.3f}",
             f"field accuracy:    {result['field_accuracy']:.3f}",
             f"sequence accuracy: {result['sequence_accuracy']:.3f}"]
    if show_failures:
        for cid, c in result["per_case"].items():
            if c["exact"]:
                continue
            lines.append(f"- {cid}: status pred={c['status'][0]} gold={c['status'][1]}")
            for s in c["missing"]:
                lines.append(f"    missing {s}")
            for s in c["extra"]:
                lines.append(f"    extra   {s}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred")
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--gold", default=None, help="defaults to <dataset>/sample_output.csv")
    ap.add_argument("--requests", default=None, help="defaults to <dataset>/sample_deletion_requests.csv")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    args.gold = args.gold or str(Path(args.dataset) / "sample_output.csv")
    args.requests = args.requests or str(Path(args.dataset) / "sample_deletion_requests.csv")
    if args.pred:
        pred = load(Path(args.pred))
    else:
        from ghost_audit.cli import run
        from ghost_audit.llm import LLMClient
        llm = LLMClient(cache_dir=ROOT / ".cache", offline=args.offline)
        rows, _ = run(Path(args.requests), Path(args.dataset), llm)
        pred = {cid: (status, steps) for cid, status, steps in rows}
    print(report(score(pred, load(Path(args.gold))), show_failures=not args.quiet))


if __name__ == "__main__":
    main()
