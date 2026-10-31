"""Identity resolution, unresolved subjects and malformed inputs."""
from synth import audit_requests, listing, make_dataset, request, user

from ghost_audit.identity import IdentityIndex


def _changed_email(tmp_path):
    """alex used shared@... until 1 June 2026; sam has held it since 1 August 2026."""
    users = [user("user_1", "alex.new@example.test"), user("user_2", "shared@example.test",
                                                            updated_at="2026-08-01T00:00:00Z")]
    audit = [{"audit_id": "audit_1", "user_id": "user_1", "event_time": "2026-06-01T00:00:00Z",
              "field_name": "email", "old_value": "shared@example.test",
              "new_value": "alex.new@example.test"},
             {"audit_id": "audit_2", "user_id": "user_2", "event_time": "2026-08-01T00:00:00Z",
              "field_name": "email", "old_value": "sam.old@example.test",
              "new_value": "shared@example.test"}]
    return make_dataset(tmp_path / "ds", users, [], audit=audit)


def test_reused_email_resolves_to_the_owner_at_request_time(tmp_path):
    # Submitted code: resolve() returned the current holder for any request, so a request
    # filed in May (when alex held the address) went to sam.
    ids = IdentityIndex(_changed_email(tmp_path))
    may = ids.resolve_detail("shared@example.test", "2026-05-01T00:00:00Z")
    sep = ids.resolve_detail("shared@example.test", "2026-09-01T00:00:00Z")
    assert (may.user_id, may.status, may.audit_ids) == ("user_1", "historical", ["audit_1"])
    assert (sep.user_id, sep.status) == ("user_2", "current")


def test_overlapping_owners_are_ambiguous_not_first_sorted(tmp_path):
    # Submitted code: sorted(owners)[0] - "user_1" - with no evidence it is right.
    users = [user("user_1", "a.now@example.test"), user("user_2", "b.now@example.test")]
    audit = [{"audit_id": f"audit_{i}", "user_id": u, "event_time": "2026-06-01T00:00:00Z",
              "field_name": "email", "old_value": "dup@example.test", "new_value": new}
             for i, (u, new) in enumerate([("user_1", "a.now@example.test"),
                                           ("user_2", "b.now@example.test")], 1)]
    ids = IdentityIndex(make_dataset(tmp_path / "ds", users, [], audit=audit))
    r = ids.resolve_detail("dup@example.test", "2026-05-01T00:00:00Z")
    assert r.user_id is None and r.status == "ambiguous" and r.candidates == ["user_1", "user_2"]


def test_ambiguous_or_unknown_subject_is_never_complete(tmp_path):
    # Submitted code returned COMPLETE with an empty step list for an unresolved email.
    users = [user("user_1", "known@example.test")]
    reqs = [request("case_1", "nobody@example.test")]
    rows, diag, _ = audit_requests(make_dataset(tmp_path / "ds", users, reqs), tmp_path / "c")
    assert rows["case_1"] == ("INCOMPLETE", [])
    assert diag["case_1"]["identity"] == "unknown" and diag["case_1"]["review"]


def test_invalid_timestamp_is_flagged_not_crashing(tmp_path):
    users = [user("user_1", "p@example.test")]
    reqs = [request("case_1", "p@example.test", requested_at="2026-13-45 9am"),
            request("case_2", "p@example.test", requested_at="2026-09-20T00:00:00Z",
                    audit_at="2026-09-10T00:00:00Z")]
    rows, diag, _ = audit_requests(make_dataset(tmp_path / "ds", users, reqs), tmp_path / "c")
    assert rows["case_1"] == ("INCOMPLETE", []) and "requested_at" in diag["case_1"]["review"][0]
    assert rows["case_2"] == ("INCOMPLETE", []) and "after audit_at" in diag["case_2"]["review"][0]


def test_evidence_backed_deletion_is_complete(tmp_path):
    """Control: a properly evidenced absence is still COMPLETE with a cited check."""
    users = [user("user_1", "p@example.test")]
    ev = [("evidence_01", "evidence/records/l.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", [("account_service", "account_1", "USER_PROFILE",
                                             "NOT_FOUND", "p@example.test")]))]
    rows, _, _ = audit_requests(make_dataset(tmp_path / "ds", users, [request("case_1", "p@example.test")],
                                             evidence=ev), tmp_path / "c")
    status, steps = rows["case_1"]
    assert status == "COMPLETE"
    assert [(s["resource_id"], s["operation"], s["evidence_id"]) for s in steps] == \
        [("account_1", "VERIFY_RECORD_ABSENT", "evidence_01")]


def test_diagnostics_never_contain_direct_identifiers(tmp_path):
    """Privacy claim in docs/responsible-ai.md: the review file holds internal ids only."""
    from ghost_audit.cli import write_diagnostics
    users = [user("user_1", "pat.private@example.test", name="Pat Private", phone="+44 7700 900555"),
             user("user_2", "lee.private@example.test", name="Lee Private", phone="+44 7700 449012")]
    audit = [{"audit_id": f"audit_{i}", "user_id": u, "event_time": "2026-06-01T00:00:00Z",
              "field_name": "email", "old_value": "dup.private@example.test", "new_value": new}
             for i, (u, new) in enumerate([("user_1", "pat.private@example.test"),
                                           ("user_2", "lee.private@example.test")], 1)]
    ev = [("evidence_01", "evidence/records/l.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", [("account_service", "account_1", "USER_PROFILE",
                                             "FOUND", "pat.private@example.test")]))]
    reqs = [request("case_1", "pat.private@example.test"),                   # finding
            request("case_2", "dup.private@example.test", requested_at="2026-05-01T00:00:00Z"),  # ambiguous
            request("case_3", "nobody.private@example.test"),                # unknown
            request("case_4", "pat.private@example.test", audit_at="bad")]   # invalid
    ds = make_dataset(tmp_path / "ds", users, reqs, evidence=ev, audit=audit)
    rows, diag, engine = audit_requests(ds, tmp_path / "c")
    out = tmp_path / "diag.jsonl"
    write_diagnostics(diag, engine.gaps, engine.vision_rejected, out)
    text = out.read_text(encoding="utf-8")
    assert diag["case_2"]["identity"] == "ambiguous"
    for secret in ("@", "Private", "7700", "900555", "449012"):
        assert secret not in text, secret
