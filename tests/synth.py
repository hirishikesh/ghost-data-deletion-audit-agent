"""Build small, fully fictional datasets for regression tests.

Every person, address and record here is invented (example.test domain). The files follow
the challenge's input formats so the real pipeline runs end to end without private data.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ghost_audit.model import email_hash

RULES = [
    ("rule_01", "USER_PROFILE", "Always", "DELETE"),
    ("rule_02", "SECONDARY_IDENTIFIER", "Always", "DELETE"),
    ("rule_03", "SEARCH_INDEX_ENTRY", "Always", "DELETE"),
    ("rule_04", "CACHE_ENTRY", "Always", "DELETE"),
    ("rule_05", "SUPPORT_MESSAGE", "Shared conversation must be kept", "REDACT"),
    ("rule_06", "SUPPORT_ATTACHMENT", "Belongs only to the subject", "DELETE"),
    ("rule_07", "ANALYTICS_PROFILE", "Still attributable", "ANONYMIZE"),
    ("rule_08", "PRODUCT_TELEMETRY", "Still attributable", "ANONYMIZE"),
    ("rule_09", "USER_EXPORT", "Always", "DELETE"),
    ("rule_10", "MARKETING_PROFILE", "Always", "DELETE"),
    ("rule_11", "EMAIL_DELIVERY_LOG", "Contains an email", "REDACT"),
    ("rule_12", "SECURITY_AUDIT_LOG", "Within retention", "RETAIN"),
    ("rule_13", "LEGAL_HOLD_RECORD", "Hold active", "RETAIN"),
    ("rule_14", "BACKUP_ARCHIVE", "Restore can be blocked", "RETAIN"),
    ("rule_15", "ACCOUNT_TOMBSTONE", "Only the stable id is needed", "REDACT"),
]

DELETE_TEXT = "Please permanently delete my account and all personal data."


def user(uid: str, email: str, name: str = "Test Person", phone: str = "+44 7700 900000",
         updated_at: str = "2026-01-01T00:00:00Z") -> dict:
    return {"user_id": uid, "name": name, "email": email, "phone": phone,
            "email_hash": email_hash(email), "updated_at": updated_at}


def request(case_id: str, email: str, requested_at="2026-09-01T09:00:00Z",
            reported_status="COMPLETE", reported_at="2026-09-02T09:00:00Z",
            audit_at="2026-09-10T09:00:00Z", text: str = DELETE_TEXT) -> dict:
    return {"case_id": case_id, "email": email, "requested_at": requested_at,
            "reported_status": reported_status, "reported_at": reported_at,
            "audit_at": audit_at, "request_text": text}


def activity(aid: str, time: str, event_type: str, target: str, resource_id: str,
             result: str = "SUCCESS", details: str = "", source: str = "deletion_worker") -> dict:
    return {"activity_id": aid, "event_time": time, "event_type": event_type,
            "source_system": source, "target_system": target, "resource_id": resource_id,
            "reported_result": result, "details": details or f"Policy action completed for {resource_id}."}


def listing(captured_at: str, rows: list[tuple]) -> str:
    """A record-listing JSON: rows are (system, resource_id, record_type, result, subject)."""
    return json.dumps({"exported_at": captured_at, "records": [
        {"recorded_at": captured_at, "system": s, "resource_id": r, "record_type": t,
         "result": res, "subject_reference": subj} for s, r, t, res, subj in rows]})


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def make_dataset(root: Path, users: list[dict], requests: list[dict], evidence=(), audit=(),
                 activities=(), manifest_extra=()) -> Path:
    """Write a dataset folder. evidence: (file_id, rel_path, captured_at, origin, content)."""
    root.mkdir(parents=True, exist_ok=True)
    write_csv(root / "user_identities.csv", users,
              ["user_id", "name", "email", "phone", "email_hash", "updated_at"])
    write_csv(root / "user_audit_log.csv", list(audit),
              ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
    write_csv(root / "system_activity.csv", list(activities),
              ["activity_id", "event_time", "event_type", "source_system", "target_system",
               "resource_id", "reported_result", "details"])
    write_csv(root / "deletion_rules.csv",
              [dict(zip(["rule_id", "data_category", "condition", "required_action"], r)) for r in RULES],
              ["rule_id", "data_category", "condition", "required_action"])
    write_csv(root / "deletion_requests.csv", requests,
              ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at",
               "request_text"])
    manifest = []
    for fid, rel, captured, origin, content in evidence:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        manifest.append({"file_id": fid, "file_path": rel, "origin": origin, "captured_at": captured})
    manifest += list(manifest_extra)
    write_csv(root / "evidence_files.csv", manifest, ["file_id", "file_path", "origin", "captured_at"])
    return root


def audit_requests(dataset: Path, cache_dir: Path, **kw):
    """Run the real pipeline offline. Returns (rows by case_id, diagnostics, engine)."""
    from ghost_audit.cli import build, run
    from ghost_audit.llm import LLMClient
    llm = LLMClient(cache_dir=cache_dir, offline=True, env_root=cache_dir)
    engine, identities = build(dataset, llm)
    rows, diag = run(dataset / "deletion_requests.csv", dataset, llm, engine, identities, **kw)
    return {cid: (status, steps) for cid, status, steps in rows}, diag, engine
