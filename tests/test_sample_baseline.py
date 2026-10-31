"""Labelled-sample regression against the submitted version's measured baseline.

Needs the private challenge dataset (set GHOST_AUDIT_DATASET); skipped otherwise. The
numbers are the submission's own public-sample results - a floor, not a hidden-test score.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evaluation"))

SUBMITTED = {"status_accuracy": 1.0, "step_f1": 0.979, "field_accuracy": 0.947,
             "sequence_accuracy": 0.781}


def test_samples_do_not_regress(private_dataset, tmp_path):
    import pytest
    if not (ROOT / ".cache" / "llm_cache.json").exists():
        # Without the screenshot answers the auditor is deliberately conservative: any
        # request whose window contains an unread screenshot is flagged, not COMPLETE.
        pytest.skip("needs .cache/llm_cache.json (run once with ANTHROPIC_API_KEY) so the "
                    "screenshots are read; without it the sample metrics are lower by design")
    from evaluate import load, score

    from ghost_audit.cli import run
    from ghost_audit.llm import LLMClient
    llm = LLMClient(cache_dir=ROOT / ".cache", offline=True, env_root=tmp_path)
    rows, diag = run(private_dataset / "sample_deletion_requests.csv", private_dataset, llm)
    got = score({c: (s, st) for c, s, st in rows}, load(private_dataset / "sample_output.csv"))
    for metric, floor in SUBMITTED.items():
        assert round(got[metric], 3) >= floor, metric
    assert not any(d.get("review") for d in diag.values())
