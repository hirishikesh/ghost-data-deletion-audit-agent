"""Write evaluation/usage_report.md from the ledger of the last full run.

Usage (from the repository root, after `python -m ghost_audit`):
    python evaluation/usage_report.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE.parent / ".cache" / "usage_last_run.json"
OUT = HERE / "usage_report.md"


def table(rows, requests):
    lines = ["| Provider | Model | Purpose | API calls | Cache hits | Input tokens | Output tokens | "
             "Total tokens | Avg tokens / request | Cost (USD) | Cost / request (USD) |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    per_model = defaultdict(lambda: defaultdict(float))
    for r in rows:
        total = r["input_tokens"] + r["output_tokens"]
        lines.append(f"| Anthropic | {r['model']} | {r['purpose']} | {r['calls']} | {r.get('cache_hits', 0)} | "
                     f"{r['input_tokens']:,} | {r['output_tokens']:,} | {total:,} | {total / requests:.1f} | "
                     f"{r['cost_usd']:.4f} | {r['cost_usd'] / requests:.6f} |")
        m = per_model[r["model"]]
        for k in ("calls", "input_tokens", "output_tokens", "cost_usd"):
            m[k] += r[k]
        m["cache_hits"] += r.get("cache_hits", 0)
    overall = defaultdict(float)
    for model, m in sorted(per_model.items()):
        total = m["input_tokens"] + m["output_tokens"]
        lines.append(f"| **Anthropic** | **{model}** | **model total** | **{int(m['calls'])}** | "
                     f"**{int(m['cache_hits'])}** | **{int(m['input_tokens']):,}** | "
                     f"**{int(m['output_tokens']):,}** | **{int(total):,}** | **{total / requests:.1f}** | "
                     f"**{m['cost_usd']:.4f}** | **{m['cost_usd'] / requests:.6f}** |")
        for k, v in m.items():
            overall[k] += v
    total = overall["input_tokens"] + overall["output_tokens"]
    lines.append(f"| | | **overall** | **{int(overall['calls'])}** | **{int(overall['cache_hits'])}** | "
                 f"**{int(overall['input_tokens']):,}** | **{int(overall['output_tokens']):,}** | "
                 f"**{int(total):,}** | **{total / requests:.1f}** | **{overall['cost_usd']:.4f}** | "
                 f"**{overall['cost_usd'] / requests:.6f}** |")
    return "\n".join(lines)


def main():
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    n = data["requests"]
    md = f"""# Token usage and cost - final full-dataset run

Run: `python -m ghost_audit` over `dataset/deletion_requests.csv` ({n} requests), which wrote
`output.csv`. Prices are Anthropic list prices per million tokens: claude-opus-5-5
$4.00 input / $20.00 output.

## Where a model is used

Everything that can be computed exactly is deterministic Python (parsing SQL/JSON/logs/PDF,
identity resolution, state at `audit_at`, policy, ordering, evidence citing). A model is
called only for:

1. **Screenshot extraction** (`screenshot_extraction`): each of the 11 PNG admin-console
   screenshots is transcribed once into fixed JSON (structured output). Identical images are
   never sent twice.
2. **Request intent** (`intent_classification`): multilingual keyword rules settle clear
   deletion and non-deletion requests for free. Only texts where the rules disagree or find
   nothing go to the model, once per distinct text.

Every answer is cached in `.cache/llm_cache.json` (keyed by model, prompt, input hash),
so re-running is free and produces the same `output.csv`.

## Every API call made (all of development, which built the cache)

These are all calls that actually reached the API - the real cost behind the final
`output.csv`. It includes one connectivity check and 9 intent calls made with an earlier
version of the intent prompt (replaced after it misread a follow-up deletion request), so
intent shows 18 calls for 9 distinct texts.

{table(data['cache_build'], n)}

## Calls in the final run

The final run read every model answer from the cache (`Cache hits`), so it made the API calls
shown here (normally zero).

{table(data['this_run'], n)}

## Notes

- Per-request figures divide by all {n} requests, including those answered without a model.
- Effort is set to `low` for both purposes: transcription and a yes/no intent decision do not
  need deep reasoning. Structured outputs (`output_config.format`) guarantee parseable JSON,
  so there are no retry calls for malformed answers.
- No API key or credential is stored in the code, cache, or reports.
"""
    OUT.write_text(md, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
