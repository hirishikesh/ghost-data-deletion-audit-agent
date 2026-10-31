# Token usage and cost - final full-dataset run

Run: `python code/main.py` over `dataset/deletion_requests.csv` (518 requests), which wrote
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

Every answer is cached in `code/cache/llm_cache.json` (keyed by model, prompt, input hash),
so re-running is free and produces the same `output.csv`.

## Every API call made (all of development, which built the cache)

These are all calls that actually reached the API - the real cost behind the final
`output.csv`. It includes one connectivity check and 9 intent calls made with an earlier
version of the intent prompt (replaced after it misread a follow-up deletion request), so
intent shows 18 calls for 9 distinct texts.

| Provider | Model | Purpose | API calls | Cache hits | Input tokens | Output tokens | Total tokens | Avg tokens / request | Cost (USD) | Cost / request (USD) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Anthropic | claude-opus-5-5 | connectivity_check | 1 | 0 | 216 | 9 | 225 | 0.4 | 0.0010 | 0.000002 |
| Anthropic | claude-opus-5-5 | intent_classification | 18 | 0 | 11,700 | 847 | 12,547 | 24.2 | 0.0637 | 0.000123 |
| Anthropic | claude-opus-5-5 | screenshot_extraction | 11 | 0 | 42,904 | 8,883 | 51,787 | 100.0 | 0.3493 | 0.000674 |
| **Anthropic** | **claude-opus-5-5** | **model total** | **30** | **0** | **54,820** | **9,739** | **64,559** | **124.6** | **0.4141** | **0.000799** |
| | | **overall** | **30** | **0** | **54,820** | **9,739** | **64,559** | **124.6** | **0.4141** | **0.000799** |

## Calls in the final run

The final run read every model answer from the cache (`Cache hits`), so it made the API calls
shown here (normally zero).

| Provider | Model | Purpose | API calls | Cache hits | Input tokens | Output tokens | Total tokens | Avg tokens / request | Cost (USD) | Cost / request (USD) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Anthropic | claude-opus-5-5 | intent_classification | 0 | 22 | 0 | 0 | 0 | 0.0 | 0.0000 | 0.000000 |
| Anthropic | claude-opus-5-5 | screenshot_extraction | 0 | 11 | 0 | 0 | 0 | 0.0 | 0.0000 | 0.000000 |
| **Anthropic** | **claude-opus-5-5** | **model total** | **0** | **33** | **0** | **0** | **0** | **0.0** | **0.0000** | **0.000000** |
| | | **overall** | **0** | **33** | **0** | **0** | **0** | **0.0** | **0.0000** | **0.000000** |

## Notes

- Per-request figures divide by all 518 requests, including those answered without a model.
- Effort is set to `low` for both purposes: transcription and a yes/no intent decision do not
  need deep reasoning. Structured outputs (`output_config.format`) guarantee parseable JSON,
  so there are no retry calls for malformed answers.
- No API key or credential is stored in the code, cache, or reports.
