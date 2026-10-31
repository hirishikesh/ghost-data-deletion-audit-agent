"""Replica-aware state, SQL deletion semantics and parser contamination."""
import json
import os

from synth import audit_requests, listing, make_dataset, request, user

from ghost_audit.evidence import document_observations, parse_all, parse_log

EMAIL = "p@example.test"


def sql_snapshot(captured, table, rows):
    """A replication snapshot in the challenge's record_json layout."""
    out = [f"-- Captured at: {captured}", "BEGIN TRANSACTION;",
           f'CREATE TABLE "{table}" (resource_id TEXT, origin_system TEXT, collection_name TEXT, '
           "record_json TEXT, updated_at TEXT);"]
    for rec in rows:
        payload = json.dumps(rec).replace("'", "''")
        out.append(f"INSERT INTO \"{table}\" VALUES('{rec['resource_id']}','account_service',"
                   f"'customer_profiles','{payload}','{captured}');")
    return "\n".join(out + ["COMMIT;"])


PROFILE = {"resource_id": "account_1", "customer_reference": "user_1", "full_name": "Pat Example",
           "email": EMAIL, "phone": "+44 7700 900000", "account_status": "ACTIVE"}


def _run(tmp_path, evidence):
    ds = make_dataset(tmp_path / "ds", [user("user_1", EMAIL)], [request("case_1", EMAIL)],
                      evidence=evidence)
    return audit_requests(ds, tmp_path / "cache")


def test_newer_primary_check_does_not_hide_a_stale_replica(tmp_path):
    # Submitted state_at() kept only the newest timestamp: the 5 Sept service lookup
    # ("not found") hid the failover replica still holding the full profile on 3 Sept.
    ev = [("evidence_01", "evidence/db/failover.sql", "2026-09-03T00:00:00Z", "db",
           sql_snapshot("2026-09-03T00:00:00Z", "customer_data_failover_replica", [PROFILE])),
          ("evidence_02", "evidence/records/lookup.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", [("account_service", "account_1", "USER_PROFILE",
                                             "NOT_FOUND", EMAIL)]))]
    rows, _, _ = _run(tmp_path, ev)
    status, steps = rows["case_1"]
    assert status == "INCOMPLETE"
    assert ("FINDING", "account_1", "REPORT_REMAINING_RECORD", "evidence_01") in \
        [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]


def test_replica_later_snapshot_without_the_row_clears_that_copy(tmp_path):
    """Control: once the same replica is re-captured without the row, the copy is gone."""
    ev = [("evidence_01", "evidence/db/f1.sql", "2026-09-03T00:00:00Z", "db",
           sql_snapshot("2026-09-03T00:00:00Z", "customer_data_failover_replica", [PROFILE])),
          ("evidence_02", "evidence/db/f2.sql", "2026-09-06T00:00:00Z", "db",
           sql_snapshot("2026-09-06T00:00:00Z", "customer_data_failover_replica",
                        [dict(PROFILE, resource_id="account_99", customer_reference="user_99",
                              email="other@example.test", phone="+44 7700 900999")])),
          ("evidence_03", "evidence/records/lookup.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", [("account_service", "account_1", "USER_PROFILE",
                                             "NOT_FOUND", EMAIL)]))]
    rows, _, _ = _run(tmp_path, ev)
    assert rows["case_1"][0] == "COMPLETE"


def test_filtered_primary_extract_does_not_overrule_a_live_lookup(tmp_path):
    """Found while building v2: extending the replica rule to the primary flipped a
    labelled sample. A service lookup that shows the profile live beats a later filtered
    bundle that simply does not list it."""
    bundle = json.dumps({"exported_at": "2026-09-06T00:00:00Z", "snapshots": [
        {"captured_at": "2026-09-06T00:00:00Z", "data_store": "account_service",
         "collection": "customer_profiles", "records": []}]})
    ev = [("evidence_01", "evidence/records/lookup.json", "2026-09-05T00:00:00Z", "svc",
           listing("2026-09-05T00:00:00Z", [("account_service", "account_1", "USER_PROFILE",
                                             "FOUND", EMAIL)])),
          ("evidence_02", "evidence/records/bundle.json", "2026-09-06T00:00:00Z", "bundle", bundle)]
    status, steps = _run(tmp_path, ev)[0]["case_1"]
    assert status == "INCOMPLETE" and steps[0]["operation"] == "REPORT_REMAINING_RECORD"


def inventory(captured, status, **fields):
    cols = "resource_id, customer_reference, full_name, email, phone, email_hash, account_status, updated_at"
    vals = ["account_1", fields.get("ref"), fields.get("name"), fields.get("email"), None, None, status, captured]
    sql_vals = ", ".join("NULL" if v is None else f"'{v}'" for v in vals)
    return "\n".join([f"-- Captured at: {captured}", f"CREATE TABLE customer_profiles ({cols});",
                      f"INSERT INTO customer_profiles VALUES ({sql_vals});"])


def test_logically_deleted_row_still_holding_pii_is_remaining(tmp_path):
    # Submitted parse_sql() set present=False for account_status=DELETED, so a row that
    # still held the email was reported as deleted.
    ev = [("evidence_01", "evidence/db/inv.sql", "2026-09-05T00:00:00Z", "db",
           inventory("2026-09-05T00:00:00Z", "DELETED", email=EMAIL, name="Pat Example"))]
    status, steps = _run(tmp_path, ev)[0]["case_1"]
    assert status == "INCOMPLETE" and steps[0]["operation"] == "REPORT_REMAINING_RECORD"


def test_logically_deleted_row_without_identifiers_counts_as_gone(tmp_path):
    ev = [("evidence_01", "evidence/db/inv.sql", "2026-09-05T00:00:00Z", "db",
           inventory("2026-09-05T00:00:00Z", "DELETED", ref="user_1"))]
    status, steps = _run(tmp_path, ev)[0]["case_1"]
    assert status == "COMPLETE" and steps[0]["operation"] == "VERIFY_RECORD_ABSENT"


def test_pdf_links_identifiers_to_their_own_record_only():
    # Submitted parse_pdf() gave every resource id every identifier in the document.
    text = ("Statement for support_11 - requester ann@example.test\n"
            "Related ticket support_22 opened by bob@example.test\n"
            "Earlier attachment support_33 was deleted on request")
    obs = {o.resource_id: o for o in document_observations(text, "evidence_01", "2026-09-01T00:00:00Z")[0]}
    assert obs["support_11"].keys == {"e:ann@example.test"}
    assert obs["support_22"].keys == {"e:bob@example.test"}
    assert obs["support_33"].present is None          # mentioned as deleted, not shown


def test_reused_trace_id_does_not_cross_link_people(tmp_path):
    # Submitted parse_log() pooled every key of a trace onto every result line in it.
    log = tmp_path / "svc.log"
    log.write_text("\n".join([
        "2026-09-01T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started lookup_key=ann@example.test",
        "2026-09-01T10:00:01Z level=INFO service=account_service trace_id=tr-1 resource_id=account_1 event=profile_lookup_completed rows=1 http_status=200",
        "2026-09-01T10:05:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started lookup_key=bob@example.test",
        "2026-09-01T10:05:01Z level=INFO service=account_service trace_id=tr-1 resource_id=account_2 event=profile_lookup_completed rows=1 http_status=200",
    ]), encoding="utf-8")
    obs, _ = parse_log(log, "evidence_01", "2026-09-01T11:00:00Z")
    assert all(not o.keys for o in obs)


def test_single_lookup_trace_still_links(tmp_path):
    """Control: the normal one-lookup trace keeps working."""
    log = tmp_path / "svc.log"
    log.write_text("\n".join([
        "2026-09-01T10:00:00Z level=DEBUG service=account_service trace_id=tr-9 event=profile_lookup_started lookup_key=ann@example.test",
        "2026-09-01T10:00:01Z level=INFO service=account_service trace_id=tr-9 resource_id=account_1 event=profile_lookup_completed rows=0 http_status=404",
    ]), encoding="utf-8")
    obs, _ = parse_log(log, "evidence_01", "2026-09-01T11:00:00Z")
    assert obs[0].keys == {"e:ann@example.test"} and obs[0].present is False


def test_evidence_sql_cannot_write_files(tmp_path, monkeypatch):
    # Submitted parse_sql() ran executescript() on untrusted text: ATTACH created a file.
    monkeypatch.chdir(tmp_path)
    evil = "-- Captured at: 2026-09-01T00:00:00Z\nATTACH DATABASE 'planted.db' AS x;\nCREATE TABLE x.t(a);"
    ds = make_dataset(tmp_path / "ds", [user("user_1", EMAIL)], [],
                      evidence=[("evidence_01", "evidence/db/evil.sql", "2026-09-01T00:00:00Z", "db", evil)])
    gaps = []
    parse_all(ds, gaps=gaps)
    assert not os.path.exists(tmp_path / "planted.db") and not os.path.exists(ds / "planted.db")
    assert [g.file_id for g in gaps] == ["evidence_01"]


def test_manifest_path_outside_the_dataset_is_refused(tmp_path):
    (tmp_path / "outside.json").write_text(listing("2026-09-01T00:00:00Z", []), encoding="utf-8")
    ds = make_dataset(tmp_path / "ds", [user("user_1", EMAIL)], [], manifest_extra=[
        {"file_id": "evidence_01", "file_path": "../outside.json", "origin": "x",
         "captured_at": "2026-09-01T00:00:00Z"}])
    gaps = []
    parse_all(ds, gaps=gaps)
    assert gaps and "outside" in gaps[0].reason
