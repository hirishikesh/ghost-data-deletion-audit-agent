# Submitted README - project section (excerpt)

> This is the part of the judged `README.md` that was written for this project. The judged
> file also contained HackerRank's challenge instructions from the Orchestrate starter
> repository, which is published without a license; that text is omitted here and is not
> covered by this repository's license. Commands below are as submitted (`code/` layout);
> the current layout is described in the main [README](../../README.md). The complete
> original file is kept, unmodified, in the private archive under tag
> `hackerrank-submission-v1`.

## Our solution: deletion-audit agent

### Setup and run

Python 3.10+.

```bash
pip install -r code/requirements.txt
python code/main.py                       # writes output.csv (518 rows) at the repository root
python code/evaluation/main.py            # scores the 32 samples + custom cases
python code/evaluation/usage_report.py    # writes code/evaluation/usage_report.md
```

The model calls need `ANTHROPIC_API_KEY` in the environment, or in an uncommitted `.env` file
at the repository root (copy `.env.example`). On the first run Claude (`claude-opus-5-5`) reads
the 11 screenshots and a few unclear request texts (about $0.40 in total); every answer is then
cached in `code/cache/llm_cache.json`, so later runs are free and identical. The cache is local
and not part of `code.zip`. Total model spend is capped by `AUDIT_BUDGET_USD` (default 3.00).
`python code/main.py --offline` never calls a model: it uses the cache if present, and keyword
rules where there is no answer (screenshots are then skipped).

### How it works

```
dataset/ ──► parsers (SQL via sqlite3, 3 JSON layouts, 6 log formats joined by trace_id, TXT, PDF)
             screenshots ──► Claude (structured JSON transcription, cached)
                 │
                 ▼
         one list of Observations: (evidence_id, time, system, resource, present?, identifiers,
                                     exposed fields, enabled?, restore_allowed?, category)
                 │
request ──► intent (keyword rules; Claude only for unclear texts) ──► NOT_APPLICABLE, []
   │
   ▼
identity: current + historical email/phone/hash (sha256 of every email) + user_id
   ▼
discovery: resources whose evidence (observed between requested_at and audit_at) carries a
           unique identifier of the person; data-catalog links; search docs -> source account;
           connector/restore "repopulated" chains; worker-batch fallback when nothing names the account
   ▼
state at audit_at: latest observation from files captured <= audit_at (replicas merged:
           present anywhere = present); full snapshots prove absence of deletable records only
   ▼
policy (deletion_rules.csv) -> findings, remediation, verification
   ▼
order: phase -> operation order from the spec -> system_id -> resource_id; number from 1
```

- `code/audit/evidence.py` parsers, `vision.py` screenshots, `identity.py`, `intent.py`,
  `engine.py` (discovery, state, policy, ordering), `llm.py` (client, cache, token ledger).
- Prompts: `code/prompts/*.md`. Reports: `code/evaluation/evaluation_report.md`,
  `code/evaluation/usage_report.md`.
- Workflow statuses and activity results are never treated as proof of state; activity
  operations are only used to hint a record's category and, as a last resort, to tie an
  otherwise unnamed account or anonymised profile to a request.
