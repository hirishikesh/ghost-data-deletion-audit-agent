# Post-competition fixes

Everything here was done **after** HackerRank Orchestrate (October 2026) closed. The judged
code is kept unchanged as tag `hackerrank-submission-v1` in a private archive repository
(see the README for why); nothing below contributed to the competition score. Where this
document says "the tag", it means that archived snapshot.

## How the defects were found and checked

The starting list came from my own interview preparation and an independent review of the
packaged submission (ChatGPT). I had no AI Judge transcript or written feedback, so none of
this is presented as interviewer feedback. Every suspected defect was then **reproduced
against the tagged submission** before any fix:

1. Re-run the tag and confirm it regenerates the submitted `output.csv` byte for byte.
2. Probe the real (private) dataset for each suspected weakness.
3. Where the dataset never triggers it, build a minimal fictional case that does.
4. Fix, add that case as a regression test, and confirm the challenge output is unchanged
   when all evidence is readable.

Most defects turned out to be **latent**: real code paths that the challenge data never
exercised. They are fixed because a deletion auditor has to stay correct on data it has not
seen, not because they moved the score.

## Fixes

| # | Area | How the submitted version failed | Seen in challenge data? | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | Missing evidence | With no API key or cache, unread screenshots were skipped and **6 requests came out `COMPLETE` with zero verification steps**; an unresolvable email also returned `COMPLETE, []` | **Yes** (offline run) | `COMPLETE` requires a linked record, an evidence-cited check for every required check, and no unread evidence captured in the request window; otherwise `INCOMPLETE` with a review reason | `test_unread_screenshot_blocks_a_complete_verdict`, `test_ambiguous_or_unknown_subject_is_never_complete` |
| 2 | Evidence SQL | `executescript()` ran untrusted snapshot text: `ATTACH DATABASE 'x.db'` **created and wrote a file** | Reproduced with a crafted file | SQLite authorizer denies `ATTACH`/`DETACH`/`load_extension`; the file becomes a recorded evidence gap | `test_evidence_sql_cannot_write_files` |
| 3 | Identity | `resolve()` returned `sorted(owners)[0]` when an old email had several owners; current holder always won regardless of request time | Latent (no shared identifiers) | Identifiers carry validity intervals from the change log; resolution at `requested_at`; overlaps are `ambiguous`, never guessed | `test_reused_email_resolves_to_the_owner_at_request_time`, `test_overlapping_owners_are_ambiguous_not_first_sorted` |
| 4 | Replicas | `state_at()` kept only the newest timestamp, so a newer primary "not found" hid an older failover/regional replica still holding the profile | Latent | State per physical copy; present if any copy holds it; a later dump of the same replica can clear it. The primary keeps explicit-observation-beats-omission | `test_newer_primary_check_does_not_hide_a_stale_replica`, `test_replica_later_snapshot_without_the_row_clears_that_copy`, `test_filtered_primary_extract_does_not_overrule_a_live_lookup` |
| 5 | SQL semantics | `account_status=DELETED` was read as physically absent even if the row still held the email | Latent (no such rows) | Logical deletion recorded separately; a deleted-flagged row still exposing identifiers is a remaining record | `test_logically_deleted_row_still_holding_pii_is_remaining`, `..._without_identifiers_counts_as_gone` |
| 6 | PDF parsing | Every resource id in a document received every identifier in it; any mention counted as presence | Latent (one record per PDF) | Multi-record documents link identifiers line by line; records named next to deletion wording are mentions | `test_pdf_links_identifiers_to_their_own_record_only` |
| 7 | Log correlation | All keys of a trace id were attached to every result line in it | Latent (no reused traces) | Keys travel only within a one-lookup, one-resource trace, scoped by service | `test_reused_trace_id_does_not_cross_link_people` |
| 8 | Screenshot state | State words were searched in **all** transcribed text, so a note ("restore disabled per audit") or injected text could mark a restorable backup as blocked | Latent | Only the explicit status field and named state fields can change state | `test_screen_text_cannot_flip_a_backup_to_blocked` |
| 9 | Model output | Shape was guaranteed by structured output, content was not | - | Ids must match the resource grammar, identifiers must parse, text is length-capped; rejects are counted | `test_model_extraction_is_validated` |
| 10 | API failures | Only `BadRequestError` was caught: a rate limit or network error after retries aborted the audit; truncated or non-JSON answers raised | - | All `anthropic.APIError`s, refusals, truncation and bad JSON are contained; the evidence becomes a reported gap | `test_api_failure_is_contained`, `test_truncated_answer_is_not_trusted` |
| 11 | Spend accounting | Refused calls returned before their tokens were counted (they are billed); every call was booked under the requested model even after a server-side fallback | - | Billed failures go to a ledger counted by the budget guard; usage is attributed to the model that served; unknown models are priced at the highest known rate | `test_refused_call_is_billed_and_not_cached`, `test_tokens_are_attributed_to_the_model_that_served` |
| 12 | Catalog aliases | Exposure for an alias was taken from every identifier on the whole screen | Latent | Exposure comes from the matching row only | `test_alias_exposure_uses_only_the_matching_row` |
| 13 | Workflow matches | 111 tied account assignments were presented exactly like evidence links | Yes (reporting) | Every resource carries its link kind (`evidence`, `workflow_unique`, `workflow_tie`, `analytics_*`); destructive steps on tied links are flagged; `--production-safe` removes them | `test_tied_workflow_accounts_stay_one_to_one_and_are_labelled`, `test_destructive_step_on_a_tied_match_needs_review` |
| 14 | Inputs | Malformed timestamps crashed workflow matching or mis-ordered string comparisons; manifest paths could leave the dataset folder | Reproduced with crafted input | Strict UTC timestamp validation per request; manifest paths must resolve inside the dataset | `test_invalid_timestamp_is_flagged_not_crashing`, `test_manifest_path_outside_the_dataset_is_refused` |

Already correct in the submission and now pinned by tests: one owner per workflow-linked
account, one owner per anonymised profile, and "fewer profiles than requests means assign
none" (`test_linking.py`).

## Mistake caught by the test suite during the rework

While making state replica-aware I first applied "a later snapshot that omits the row clears
it" to the primary copy too. The labelled-sample test failed on `case_24`: a filtered bundle
that simply did not list an account overruled a same-morning lookup showing it live. The rule
now applies to named replica dumps only, and `test_filtered_primary_extract_does_not_overrule_a_live_lookup`
keeps it that way. A second interaction (the "not proven" rule hiding `case_04` from the
anonymised-profile step) was caught the same way.

CI then caught two problems local runs could not:

- the SQL sandbox first removed its authorizer with `set_authorizer(None)`, which works only
  from Python 3.11. On 3.10 every following query was denied, so all SQL evidence silently
  became evidence gaps - the 3.10 job failed while 3.12 passed. The authorizer now stays
  installed (it allows ordinary reads).
- a test imported `httpx` directly; the SDK version CI resolved does not install it.

## Effect on results

| Measurement | Submission (tag) | v2 |
|---|---|---|
| 32 labelled samples, all evidence readable | status 1.000 · step F1 0.979 · field 0.947 · sequence 0.781 | identical (0 of 32 rows differ) |
| 518 challenge requests, all evidence readable | - | identical output (0 of 518 rows differ) |
| Offline, no key and no cache: `COMPLETE` rows with no steps | **6** | **0** |
| Offline: rows marked `COMPLETE` | 165 | 45 (the rest are flagged: unread screenshots fall in their window) |
| Crafted SQL with `ATTACH` | writes a file | blocked, recorded as an evidence gap |
| Automated tests | none (5 hand-run custom cases) | 35 (34 synthetic + 1 on the private samples) |

No hidden-test score exists for v2. The public-sample numbers are unchanged by design: the
fixes target situations the samples do not contain.
