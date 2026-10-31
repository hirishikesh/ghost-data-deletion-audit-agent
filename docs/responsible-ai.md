# Responsible AI: harms, safeguards and limits

This document describes what can go wrong when software audits personal-data deletion,
what this project does about it, and what it does not yet do. It is written to be checked:
every safeguard carries a status, and "Implemented" is used only where code exists and a test
or a measured run supports it.

**Status labels**

| Label | Meaning |
|---|---|
| Implemented - tested | Code exists and an automated test in `tests/` fails without it |
| Implemented - measured | Code exists and a recorded run shows the effect (named) |
| Implemented - by construction | Follows from the code structure; verified by code inspection, no dedicated test |
| Partial | Some protection exists; the gap is stated |
| Proposed | Not built |

This is an audit pipeline over exported evidence. It is **not** a production deletion system
and is not claimed to be safe for unattended use.

---

## 1. Potential harms and failure modes

| Harm | How it could happen here | Severity |
|---|---|---|
| **False "deletion complete"** - a person is told their data is gone while a copy remains | a replica, backup or unlinked copy is missed; missing evidence read as absence; a worker "success" trusted | Highest |
| **Action against the wrong person** | identity collision on a reused email; a correlated (not proven) account match; identifiers from one record attached to another | High |
| **Deleting data that must be kept** | a security-audit or legal-hold record treated as deletable | High |
| **Ignoring or over-reading the request** | a deletion request read as an access request (data kept), or an access request read as deletion | High |
| **Personal data leaking** | into the public repository, logs, diagnostics, or to the model provider beyond necessity | High |
| **Unsafe evidence processing** | untrusted SQL writing files; manifest paths leaving the dataset folder | Medium |
| **Model errors** | hallucinated or misread screenshot values; prompt injection in request text or on screen | Medium |
| **Misleading claims** | public-sample metrics presented as real-world accuracy | Medium |

## 2. Current safeguards and their verification status

### Decisions and evidence

| Safeguard | Status | Verified by |
|---|---|---|
| Model never makes a compliance decision: identity, linking, state, policy and plan ordering are deterministic Python; the model only transcribes screenshots and classifies unclear request text. Deterministic means reproducible and traceable, not correct - and a misread screenshot value is still used as a fact once it passes validation | Implemented - by construction | `ghost_audit/engine.py`, `cli.py`; model calls only in `vision.py` and `intent.py` |
| Workflow status and worker results are not proof of deletion; only evidence decides state | Implemented - measured | labelled samples where the workflow reported COMPLETE but data remained (e.g. `case_05`, `case_24`), all correct |
| Only evidence captured at or before `audit_at` is cited or used for state | Implemented - measured | sample `case_27` (later evidence showing the account must be ignored) |
| `COMPLETE` requires a linked record, evidence for every required check, and no unread evidence in the request window | Implemented - tested | `test_unread_screenshot_blocks_a_complete_verdict`, `test_ambiguous_or_unknown_subject_is_never_complete` |
| State per physical copy: a newer primary check cannot hide a replica | Implemented - tested | `test_newer_primary_check_does_not_hide_a_stale_replica` |
| Logically-deleted rows still holding identifiers are not "deleted" | Implemented - tested | `test_logically_deleted_row_still_holding_pii_is_remaining` |
| Retained categories (security audit within retention, legal hold, backups) are verified present / restore-blocked, never deleted | Implemented - measured | full 518-row contract check: 0 delete or absent steps on retained systems |

### Identity and ownership

| Safeguard | Status | Verified by |
|---|---|---|
| Identifiers have validity intervals; an email resolves to whoever held it at `requested_at` | Implemented - tested | `test_reused_email_resolves_to_the_owner_at_request_time` |
| Overlapping owners are `ambiguous` - never the first sorted id - and the request is not marked complete | Implemented - tested | `test_overlapping_owners_are_ambiguous_not_first_sorted` |
| Only identifiers owned by exactly one person link records | Implemented - by construction | `IdentityIndex.unique_keys` |
| Record-scoped associations in PDFs, logs and screenshots | Implemented - tested | `test_pdf_links_identifiers_to_their_own_record_only`, `test_reused_trace_id_does_not_cross_link_people`, `test_alias_exposure_uses_only_the_matching_row` |
| Every linked record labelled `evidence`, `workflow_unique`, `workflow_tie`, `analytics_unique` or `analytics_tie` | Implemented - tested | `test_tied_workflow_accounts_stay_one_to_one_and_are_labelled` |
| One owner per correlated account and per anonymised profile across all requests | Implemented - tested | `test_linking.py` |
| Destructive steps on tied matches are flagged; `--production-safe` removes them | Implemented - tested | `test_destructive_step_on_a_tied_match_needs_review` |

### Model use

| Safeguard | Status | Verified by |
|---|---|---|
| Narrow prompts with a fixed system prompt; request text and screen content passed only as data | Implemented - tested | `test_mixed_signals_go_to_the_model_as_data_only` |
| Clear requests never reach the model, whatever the text says | Implemented - tested | `test_injected_instructions_do_not_override_a_clear_request` |
| Structured JSON output, then validation of every value (id grammar, identifier formats, length caps) | Implemented - tested | `test_model_extraction_is_validated` |
| Screen text cannot change a record's state; only explicit status fields can | Implemented - tested | `test_screen_text_cannot_flip_a_backup_to_blocked` |
| API errors, refusals, truncation and invalid JSON are contained and reported as evidence gaps | Implemented - tested | `test_api_failure_is_contained`, `test_truncated_answer_is_not_trusted` |
| Spend cap (`AUDIT_BUDGET_USD`), billed failures counted, tokens attributed to the model that served | Implemented - tested | `test_spend_cap_refuses_calls_before_they_are_made`, `test_refused_call_is_billed_and_not_cached`, `test_tokens_are_attributed_to_the_model_that_served` |
| Reproducible answers via a local cache | Implemented - tested | `test_cached_answers_replay_offline` |

### Input and execution safety

| Safeguard | Status | Verified by |
|---|---|---|
| Evidence SQL runs in memory under an authorizer that denies `ATTACH`, `DETACH` and `load_extension` | Implemented - tested | `test_evidence_sql_cannot_write_files` (also run on Python 3.10 in CI) |
| Manifest paths must stay inside the dataset folder | Implemented - tested | `test_manifest_path_outside_the_dataset_is_refused` |
| Request timestamps must be exact UTC; bad input is flagged, not mis-ordered | Implemented - tested | `test_invalid_timestamp_is_flagged_not_crashing` |
| Unreadable files are recorded as gaps instead of crashing; `--strict` fails the run on any gap | Implemented - tested | gap tests above; `--strict` exercised by the CI demo step |

## 3. Data-handling boundaries

| Where data goes | What | Status |
|---|---|---|
| **Stays on the machine** | the dataset, evidence files, `output.csv`, `.cache/` (model answers, which include screenshot transcriptions with identifiers), the API key in `.env` | Implemented - by construction; all git-ignored |
| **Sent to the model provider** | the 11 screenshot images (unredacted) and request texts the keyword rules cannot settle (9 of 550 in the challenge data). No identity tables, SQL, logs or other evidence are sent | Implemented - measured (usage ledger) |
| **Written for reviewers** | per-case diagnostics with internal ids, link kinds and review reasons - no emails, phones or names | Implemented - tested: `test_diagnostics_never_contain_direct_identifiers` |
| **Published to GitHub** | code, tests, fictional synthetic data (`example.test`), documentation | Partial - see below |

- **API key**: read from the environment or an uncommitted `.env`; never written to the cache,
  the failure ledger, diagnostics or reports. Implemented - by construction.
- **Repository contents**: dataset, `.env`, cache, outputs, agent traces and the competition
  log are git-ignored. Every path and blob in every commit was scanned once for keys, tokens
  and private files before the first push. **Partial**: that scan was manual; an automated
  secret scan in CI is proposed.
- **Screenshots go to the provider unredacted.** Proposed: crop to the record region or run
  local OCR first, sending only what cannot be read locally.
- **The local cache stores identifiers in plain text.** Proposed: encrypt at rest and expire
  entries with the audit.

## 4. Human oversight requirements

The system produces **findings and a remediation plan**. It has no connection to live systems
and cannot delete, redact or disable anything (Implemented - by construction: the only
network client is the model API; the only database work is in-memory SQLite over evidence
files).

Before any plan is acted on in a real environment, a person should:

1. **Review every flagged case** in the diagnostics file: ambiguous or unknown identity,
   unread evidence, checks without evidence, and destructive steps on tied matches.
2. **Treat correlated links as unproven.** `workflow_tie` and `analytics_tie` resources must
   be confirmed against an authoritative source before deletion.
3. **Authorize destructive steps explicitly** (delete, redact, anonymise) and record who
   approved them. Proposed: an approval record per step - today the plan has no sign-off
   trail.
4. **Re-run the audit after remediation** and keep the evidence that proves the final state.

## 5. Remaining fail-open behaviour

These paths can still produce a reassuring answer from incomplete information. They are
listed so they can be reviewed, not hidden.

| Behaviour | Effect | Planned correction |
|---|---|---|
| **Offline intent fallback**: when the model is unavailable, a request the rules cannot settle is treated as deletion unless it contains keep-wording. It is recorded (`intent_decided_by: fallback`) but not flagged for review | a non-deletion request could receive a deletion plan | flag every fallback decision for review; withhold destructive steps until intent is confirmed |
| **Absence by omission**: when a record has no explicit observation, its absence from a later filtered snapshot is accepted as proof of absence (for records that must be deleted). Labelled samples rely on this | a record missing from an extract but present in the system would read as deleted | label this as weaker evidence in diagnostics; require corroboration in `--production-safe` mode |
| **Unlinked copies are invisible**: a copy that carries no identifier owned by the person cannot be found | `COMPLETE` can be issued while such a copy exists | inherent to evidence-based auditing; mitigated only by better source coverage |
| **Model-classified "not a deletion"**: an ambiguous text the model judges as non-deletion becomes `NOT_APPLICABLE` | a real deletion request could go unaudited | route model-decided `NOT_APPLICABLE` results to review |
| **Plausible-but-wrong transcription** passes validation | a misread id or status on a screenshot is trusted | second independent read or OCR cross-check for records that drive destructive steps |
| **Tied matches in `output.csv`**: the challenge format has no confidence field, so tied accounts appear like other checks; the label lives only in diagnostics | a reader of the CSV alone cannot tell proven from correlated | keep diagnostics alongside every output; `--production-safe` strips destructive steps |

## 6. Known limitations

- **111 workflow-correlated accounts are ties** (55 groups). Each is an account the request's
  own workflow processed, but the data does not say which belongs to whom; tested orderings
  are right about half the time. No model can recover information that is absent from the
  evidence.
- **60 anonymised analytics profiles are left unassigned** where a window holds fewer
  profiles than requests - their checks are missing rather than guessed.
- **One catalog-versus-screen identity conflict** (`marketing_001`) is resolved by rule
  (catalog decides ownership, screen decides state), not by a second source.
- **Offline mode is deliberately conservative**: with screenshots unread, 369 of 508 deletion
  requests in the challenge data are flagged.
- **Prompt-injection resistance is enforced by code, not proven against the model.** The
  tests show clear requests bypass the model and screen text cannot change state; there is no
  adversarial evaluation with real model calls.
- **The evaluation set is small** (32 labelled samples) and contains none of the adversarial
  cases the post-competition fixes target; those are covered by synthetic tests.

## 7. Evaluation integrity

Public-sample metrics and competition scores are different things and are reported apart:

- **Competition (official)**: rank #192 of 4,094, 73.2 / 100, of which the output CSV on the
  hidden test set scored 27.9 / 30.
- **Public samples (development)**: 32 labelled requests used during development. Useful for
  catching regressions; not an estimate of real-world accuracy.

The most important lesson came from the samples themselves. A development version reached
**0.987 step F1** (0.875 exact sequences) - but it let two requests in different files claim
the same anonymised analytics profile, and in shared batches it handed out profiles that were
not valid for the request. Enforcing one owner per profile and assigning only when the data
supports it lowered F1 to **0.979** (0.781 exact sequences) and removed every known invalid
assignment. The submitted version is the lower-scoring, correct one: an invariant that the
data supports (a profile belongs to one person) outranks a better number on 32 examples.

## 8. Remaining production-readiness risks

- No live connectors, authentication, authorization or approval workflow.
- No monitoring, alerting or audit trail of who acted on a plan.
- No evaluation beyond 32 labelled samples and synthetic tests; no measurement on real
  production data.
- Model-provider data handling (retention, region) has not been assessed for a real
  deployment; screenshots containing personal data are sent unredacted.
- Single-process batch design; no load, concurrency or failure-recovery testing.
- The remaining fail-open paths in section 5 are open.

## 9. Planned improvements

In order: flag fallback and model-decided intent for review; mark absence-by-omission as weak
evidence; automated secret scanning in CI; screenshot redaction before upload; encrypted,
expiring cache; per-step approval records; adversarial prompt evaluation with real model
calls; second-read verification for screenshot values that drive destructive steps. See
[roadmap.md](roadmap.md).
