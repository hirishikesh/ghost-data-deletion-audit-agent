"""Workflow-correlated accounts and anonymised profiles: one owner, uncertainty kept."""
from synth import activity, audit_requests, listing, make_dataset, request, user

REP = "2026-09-02T12:00:00Z"   # both requests report back in the same worker batch


def _two_requests(tmp_path, accounts, account_rows=(), anonymised=(), status="COMPLETE"):
    users = [user("user_1", "a@example.test", phone="+44 7700 900001"),
             user("user_2", "b@example.test", phone="+44 7700 900002")]
    reqs = [request(f"case_{i}", f"{c}@example.test", reported_status=status, reported_at=REP)
            for i, c in ((1, "a"), (2, "b"))]
    result = {"COMPLETE": "SUCCESS", "NOT_STARTED": "FAILED"}[status]
    acts = [activity(f"activity_{i}", f"2026-09-02T11:0{i}:00Z", "DELETE_RECORD", "account_service",
                     rid, result, f"Deletion worker submitted primary profile {rid} to account_service.")
            for i, rid in enumerate(accounts)]
    acts += [activity(f"activity_9{i}", f"2026-09-02T11:3{i}:00Z", "ANONYMIZE_RECORD",
                      "analytics_platform", rid) for i, rid in enumerate(anonymised)]
    rows = list(account_rows) + [("analytics_platform", rid, "ANALYTICS_PROFILE", "ANONYMIZED", "")
                                 for rid in anonymised]
    ev = [("evidence_01", "evidence/records/l.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", rows))] if rows else []
    ds = make_dataset(tmp_path / "ds", users, reqs, evidence=ev, activities=acts)
    return audit_requests(ds, tmp_path / "cache")


def _accounts(rows):
    return {cid: [s["resource_id"] for s in steps if s["system_id"] == "account_service"]
            for cid, (_, steps) in rows.items()}


def test_tied_workflow_accounts_stay_one_to_one_and_are_labelled(tmp_path):
    absent = [("account_service", r, "USER_PROFILE", "NOT_FOUND", "") for r in ("account_7", "account_8")]
    rows, diag, _ = _two_requests(tmp_path, ["account_7", "account_8"], absent)
    acc = _accounts(rows)
    assert sorted(acc["case_1"] + acc["case_2"]) == ["account_7", "account_8"]   # never shared
    assert {diag[c]["links"][acc[c][0]] for c in acc} == {"workflow_tie"}        # not "evidence"


def test_unique_workflow_match_is_labelled_unique(tmp_path):
    # Different statuses make each worker event belong to exactly one request.
    users = [user("user_1", "a@example.test", phone="+44 7700 900001"),
             user("user_2", "b@example.test", phone="+44 7700 900002")]
    reqs = [request("case_1", "a@example.test", reported_status="COMPLETE", reported_at=REP),
            request("case_2", "b@example.test", reported_status="NOT_STARTED", reported_at=REP)]
    acts = [activity("activity_1", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_service",
                     "account_7", "SUCCESS"),
            activity("activity_2", "2026-09-02T11:05:00Z", "DELETE_RECORD", "account_service",
                     "account_8", "FAILED")]
    ds = make_dataset(tmp_path / "ds", users, reqs, activities=acts)
    rows, diag, _ = audit_requests(ds, tmp_path / "cache")
    assert _accounts(rows) == {"case_1": ["account_7"], "case_2": ["account_8"]}
    assert diag["case_1"]["links"]["account_7"] == "workflow_unique"


def test_destructive_step_on_a_tied_match_needs_review(tmp_path):
    present = [("account_service", r, "USER_PROFILE", "FOUND", "") for r in ("account_7", "account_8")]
    rows, diag, _ = _two_requests(tmp_path, ["account_7", "account_8"], present)
    assert any("DELETE_RECORD" in r and "tied correlation" in r for r in diag["case_1"]["review"])
    safe, _, _ = _two_requests(tmp_path / "safe", ["account_7", "account_8"], present)
    from ghost_audit.cli import strip_unconfirmed_actions
    steps = strip_unconfirmed_actions(safe["case_1"][1], diag["case_1"]["links"])
    assert not any(s["operation"] == "DELETE_RECORD" for s in steps)
    assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))


def test_fewer_anonymised_profiles_than_requests_assigns_none(tmp_path):
    absent = [("account_service", "account_7", "USER_PROFILE", "NOT_FOUND", "a@example.test"),
              ("account_service", "account_8", "USER_PROFILE", "NOT_FOUND", "b@example.test")]
    rows, diag, _ = _two_requests(tmp_path, [], absent, anonymised=["analytics_5"])
    assert not any(s["system_id"] == "analytics_platform" for _, steps in rows.values() for s in steps)
    assert all(any("not assigned" in n for n in diag[c]["notes"]) for c in ("case_1", "case_2"))


def test_exact_count_profiles_are_shared_out_never_duplicated(tmp_path):
    absent = [("account_service", "account_7", "USER_PROFILE", "NOT_FOUND", "a@example.test"),
              ("account_service", "account_8", "USER_PROFILE", "NOT_FOUND", "b@example.test")]
    rows, diag, _ = _two_requests(tmp_path, [], absent, anonymised=["analytics_5", "analytics_6"])
    got = [s["resource_id"] for _, steps in rows.values() for s in steps if s["system_id"] == "analytics_platform"]
    assert sorted(got) == ["analytics_5", "analytics_6"]
    assert {diag[c]["links"][r] for c in ("case_1", "case_2") for r in diag[c]["links"]
            if r.startswith("analytics")} == {"analytics_tie"}
