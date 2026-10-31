# Roadmap

Priorities set after the competition. "Done" items are in v2 with tests; the rest are
open, ordered by value.

## Priority 1 - Correctness

| Item | Status |
|---|---|
| Time-aware identity ownership; explicit `ambiguous` / `unknown` | Done |
| Replica-aware (per-copy) state reconstruction | Done |
| Missing evidence never read as completed deletion | Done |
| Logical vs physical deletion in SQL snapshots | Done |
| Record-scoped PDF / log / screenshot associations | Done |
| Disambiguate the 111 tied workflow-account matches with stronger signals (e.g. per-request worker trace ids, if a source system exposed them) | Open - the current data has no such signal; tested orderings are ~50% |
| Link anonymised analytics profiles through a pre-anonymisation snapshot (profile seen with identifiers before the worker ran) instead of timing alone | Open |
| Resolve catalog-vs-screen identity conflicts (`marketing_001`) with a second independent source before acting | Open - currently catalog decides ownership, screen decides state |
| Flag offline-fallback and model-decided intent (incl. `NOT_APPLICABLE`) for review; withhold destructive steps until intent is confirmed | Open - fail-open path, see [responsible-ai.md](responsible-ai.md#5-remaining-fail-open-behaviour) |
| Mark "absence by omission" from filtered snapshots as weaker evidence; require corroboration in `--production-safe` | Open - fail-open path |

## Priority 2 - Reliability and security

| Item | Status |
|---|---|
| Sandboxed SQLite execution (authorizer), manifest path containment, input timestamp validation | Done |
| Contained API failures, refusal/truncation handling, served-model attribution, billed-failure ledger | Done |
| Validated screenshot extraction; state only from explicit fields | Done |
| Per-case diagnostics without emails; `--strict` and `--production-safe` | Done |
| Second-pass screenshot verification (independent re-read or OCR cross-check) for records that drive a destructive step | Open - costs extra model calls; plausible-but-wrong transcriptions are not caught today |
| Adversarial evaluation of the intent and screenshot prompts with real model calls | Open - current injection tests prove the deterministic guards, not model behaviour |
| Prompt-versioned cache with an explicit invalidation report | Open |
| Automated secret scanning in CI (the pre-publication scan was manual) | Open |
| Redact or crop screenshots before sending them to the model provider | Open |
| Encrypt the local model cache and expire it with the audit | Open |
| Per-step approval records (who authorised each destructive action) | Open |

## Priority 3 - Maintainability

| Item | Status |
|---|---|
| Installable package (`ghost_audit`), `pyproject.toml`, console script | Done |
| Synthetic dataset builder shared by tests and the demo | Done |
| CI on Python 3.10 and 3.12 (tests + offline demo) | Done |
| Split `engine.py` (discovery, state, policy, linking) into separate modules | Open |
| Type checking (mypy) and linting (ruff) in CI | Open |

## Priority 4 - Presentation

| Item | Status |
|---|---|
| README with architecture diagram, results and limitations | Done |
| Post-competition fix log with reproduction notes | Done |
| Walk-through notebook on the synthetic demo | Open |
