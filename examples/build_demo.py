"""Build a small, entirely fictional dataset that shows what the auditor catches.

    python examples/build_demo.py
    python -m ghost_audit --dataset examples/demo_dataset --offline --output examples/demo_output.csv

Five customers, five situations:
  case_1  deletion really finished - proven by a service lookup          -> COMPLETE
  case_2  workflow said "complete", but a failover replica still holds
          the profile ("ghost data")                                      -> INCOMPLETE
  case_3  a sync connector is still active and a backup can restore the
          profile; the plan disables/blocks first, then deletes           -> INCOMPLETE
  case_4  the customer asked for a copy of their data, not deletion       -> NOT_APPLICABLE
  case_5  the email is not in the identity registry                       -> INCOMPLETE + review
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))
sys.path.insert(0, str(HERE.parent))

from synth import listing, make_dataset, request, user  # noqa: E402

OUT = HERE / "demo_dataset"


def failover_snapshot():
    rec = ('{"resource_id":"account_2","customer_reference":"user_2","full_name":"Bea Example",'
           '"email":"bea@example.test","phone":"+44 7700 900202","account_status":"ACTIVE"}')
    return "\n".join([
        "-- Captured at: 2026-09-04T00:00:00Z",
        "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, full_name TEXT, "
        "email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);",
        "CREATE TABLE customer_data_failover_replica (resource_id TEXT, origin_system TEXT, "
        "collection_name TEXT, record_json TEXT, updated_at TEXT);",
        f"INSERT INTO customer_data_failover_replica VALUES('account_2','account_service',"
        f"'customer_profiles','{rec}','2026-09-04T00:00:00Z');",
    ])


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    users = [user("user_1", "ann@example.test", "Ann Example", "+44 7700 900101"),
             user("user_2", "bea@example.test", "Bea Example", "+44 7700 900202"),
             user("user_3", "cal@example.test", "Cal Example", "+44 7700 900303"),
             user("user_4", "dee@example.test", "Dee Example", "+44 7700 900404")]
    reqs = [request("case_1", "ann@example.test"),
            request("case_2", "bea@example.test"),
            request("case_3", "cal@example.test", reported_status="PARTIALLY_COMPLETE"),
            request("case_4", "dee@example.test",
                    text="Could you send me a copy of the data you hold? Please keep my account."),
            request("case_5", "unknown@example.test")]
    lookups = listing("2026-09-05T00:00:00Z", [
        ("account_service", "account_1", "USER_PROFILE", "NOT_FOUND", "ann@example.test"),
        ("account_service", "account_2", "USER_PROFILE", "NOT_FOUND", "bea@example.test"),
        ("account_service", "account_3", "USER_PROFILE", "FOUND", "cal@example.test"),
        ("integration_service", "data_flow_3", "DATA_FLOW", "ACTIVE", "cal@example.test"),
        ("backup_service", "backup_3", "BACKUP_ARCHIVE", "ACTIVE", "user_3"),
        ("security_service", "audit_record_3", "SECURITY_AUDIT_LOG", "FOUND", "user_3"),
    ])
    evidence = [
        ("evidence_01", "evidence/records/service_lookups.json", "2026-09-05T00:00:00Z",
         "service_readback", lookups),
        ("evidence_02", "evidence/databases/failover_snapshot.sql", "2026-09-04T00:00:00Z",
         "failover_replica_capture", failover_snapshot()),
    ]
    make_dataset(OUT, users, reqs, evidence=evidence)
    print(f"demo dataset written to {OUT}")


if __name__ == "__main__":
    main()
