"""Deterministic parsers: turn every text-readable evidence file into Observations.

Images are handled by vision.py; everything else (SQL, JSON, logs, TXT, PDF) is
parsed here without any model call.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .model import (DIRECT_IDENTIFIERS, EMAIL_RE, PHONE_RE, RESOURCE_RE, Coverage, Observation,
                    is_null, key_of, keys_in_text, system_for)

# ---------------------------------------------------------------- field roles
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}
EMAIL_FIELDS = {"email", "owner_email", "requester_email", "customer_email"}
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone"}
HASH_FIELDS = {"email_hash"}
REF_FIELDS = {"user_id", "customer_reference", "subject_reference", "subject_selector"}
TEXT_FIELDS = {"message_text", "message_excerpt"}

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "export_service": "USER_EXPORT",
    "object_store": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
    "product_telemetry": "PRODUCT_TELEMETRY",
    "object_storage": "SUPPORT_ATTACHMENT",
    # analytics_platform and support_platform hold more than one category;
    # they are decided per record (see category_for).
}

TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "customer_analytics_reports": "analytics_platform",
    "support_records": "support_platform",
    "support_conversations": "support_platform",
    "customer_exports": "export_service",
    "customer_export_jobs": "export_service",
    "connector_subscriptions": "integration_service",
    "connector_configurations": "integration_service",
    "backup_restore_entries": "backup_service",
    "backup_catalog": "backup_service",
    "security_audit_records": "security_service",
    "security_audit_events": "security_service",
    "deleted_customer_references": "identity_service",
}

LISTING_STATE = {
    # result -> (present, exposed?, enabled, restore_allowed)
    "FOUND": (True, None, None, None),
    "NOT_FOUND": (False, None, None, None),
    "IDENTIFIABLE": (True, True, None, None),
    "ANONYMIZED": (True, False, None, None),
    "REDACTED": (True, False, None, None),
    "ACTIVE": (True, None, True, True),
    "DISABLED": (True, None, False, None),
    "BLOCKED": (True, None, None, False),
}


def table_system(table: str) -> str | None:
    for prefix, system in TABLE_SYSTEM.items():
        if table.startswith(prefix):
            return system
    return None


def category_for(system: str, source: str = "", rec: dict | None = None) -> str | None:
    """Data category from deletion_rules.csv, when the evidence itself makes it clear."""
    if system in SYSTEM_CATEGORY:
        return SYSTEM_CATEGORY[system]
    src = (source or "").lower()
    rec = rec or {}
    if system == "support_platform":
        if "attachment" in src or rec.get("file") or rec.get("file_name"):
            return "SUPPORT_ATTACHMENT"
        if "message_text" in rec or "message" in src or "conversation" in src:
            return "SUPPORT_MESSAGE"
        return None
    if system == "analytics_platform":
        if "contact" in src or "email_opt_in" in rec:
            return "MARKETING_PROFILE"
        if "telemetry" in src:
            return "PRODUCT_TELEMETRY"
        if "analytics" in src or "profile" in src or "audience_cohort" in rec:
            return "ANALYTICS_PROFILE"
    return None


def as_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().strip('"').lower()
    if s in {"true", "1", "yes", "enabled", "allowed"}:
        return True
    if s in {"false", "0", "no", "disabled", "stopped", "blocked"}:
        return False
    return None


def record_to_obs(rec: dict, system: str, source: str, evidence_id: str, file_time: str,
                  obs_time: str) -> Observation | None:
    """Normalise one record-shaped dict (SQL row, JSON item) into an Observation."""
    rec = dict(rec)
    for nested in ("document", "profile"):
        if isinstance(rec.get(nested), dict):
            rec.update({k: v for k, v in rec.pop(nested).items() if k not in rec or is_null(rec[k])})
    rid = rec.get("resource_id")
    if not rid:
        return None
    system = system or system_for(rid)
    obs = Observation(evidence_id, file_time, rec.get("observed_at") or obs_time, system, rid,
                      True, source=source)
    for f, v in rec.items():
        if is_null(v):
            continue
        k = key_of(v)
        if f in NAME_FIELDS:
            obs.exposed.add("name")
        elif f in EMAIL_FIELDS:
            obs.exposed.add("email")
        elif f in PHONE_FIELDS:
            obs.exposed.add("phone")
        elif f in HASH_FIELDS:
            obs.exposed.add("email_hash")
        elif f in REF_FIELDS:
            obs.exposed.add("reference")
        elif f in TEXT_FIELDS:
            found = keys_in_text(str(v))
            obs.keys |= found
            obs.exposed |= {"email" if x.startswith("e:") else "phone" for x in found
                            if x[0] in "ep"}
            continue
        if k and (f in NAME_FIELDS | EMAIL_FIELDS | PHONE_FIELDS | HASH_FIELDS | REF_FIELDS):
            obs.keys.add(k)
    if "status_code" in rec:
        obs.present = str(rec["status_code"]) == "200"
    if "hits" in rec and isinstance(rec["hits"], dict):
        obs.present = bool(rec["hits"].get("total"))
    if "enabled" in rec:
        obs.enabled = as_bool(rec["enabled"])
    if "restore_allowed" in rec:
        obs.restore_allowed = as_bool(rec["restore_allowed"])
    if rec.get("retention_until"):
        obs.retention_until = rec["retention_until"]
    obs.category = category_for(system, source, rec)
    return obs


# ---------------------------------------------------------------------- SQL
def _sql_authorizer(action, arg1, arg2, db_name, trigger):
    """Evidence SQL is untrusted: it may build tables in memory but never touch the disk.

    Without this, a snapshot containing `ATTACH DATABASE 'x.db'` creates and writes a file.
    """
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def table_copy(table: str) -> str:
    """Which physical copy a snapshot table holds: 'primary' or the replica's name."""
    t = table.lower()
    if "replica" in t or "failover" in t:
        for prefix in ("customer_data_", "customer_profiles_"):
            if t.startswith(prefix):
                return t[len(prefix):]
        return t
    return "primary"


def parse_sql(path: Path, evidence_id: str, file_time: str):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"Captured at:\s*(\S+)", text)
    snap_time = m.group(1) if m else file_time
    db = sqlite3.connect(":memory:")
    # Stays installed for the reads below (they are allowed). Removing it with
    # set_authorizer(None) only works from Python 3.11; on 3.10 it denies every query.
    db.set_authorizer(_sql_authorizer)
    db.executescript(text)
    db.row_factory = sqlite3.Row
    observations, covered = [], defaultdict(set)
    scope_times = {}
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
    if "snapshot_scope" in tables:
        for r in db.execute("select * from snapshot_scope"):
            scope_times[r["origin_system"]] = r["captured_at"]
            covered[(r["origin_system"], "primary")]  # mark system as covered even when empty
    for table in tables:
        if table == "snapshot_scope":
            continue
        copy = table_copy(table)
        for row in db.execute(f'select * from "{table}"'):
            row = dict(row)
            if "record_json" in row:
                rec = json.loads(row["record_json"])
                system = row.get("origin_system") or system_for(rec.get("resource_id", ""))
                source = f"{table}:{row.get('collection_name', '')}"
            else:
                rec = row
                system = table_system(table) or system_for(row.get("resource_id", ""))
                source = table
            obs_time = scope_times.get(system, snap_time)
            o = record_to_obs(rec, system, source, evidence_id, file_time, obs_time)
            if not o:
                continue
            o.obs_time = obs_time  # a snapshot row is observed at snapshot time
            o.copy = copy
            if "account_status" in rec and str(rec["account_status"]).upper() == "DELETED" \
                    and system == "account_service":
                # A logically deleted row is still stored. It counts as gone only once it no
                # longer exposes direct identifiers; otherwise the person is still in it.
                o.logically_deleted = True
                o.present = bool(o.exposed & DIRECT_IDENTIFIERS)
            if row.get("source_resource_id"):
                o.keys.add("r:" + row["source_resource_id"])
            observations.append(o)
            covered[(system, copy)].add(o.resource_id)
    coverage = [Coverage(evidence_id, file_time, scope_times.get(s, snap_time), s, ids, copy)
                for (s, copy), ids in covered.items()]
    return observations, coverage


# --------------------------------------------------------------------- JSON
def parse_json(path: Path, evidence_id: str, file_time: str):
    data = json.loads(path.read_text(encoding="utf-8"))
    observations, coverage = [], []
    if "snapshots" in data:                                   # snapshot bundle
        for snap in data["snapshots"]:
            system, coll, t = snap["data_store"], snap["collection"], snap["captured_at"]
            if snap["records"] and "record_type" in snap["records"][0]:      # lookup history
                for rec in snap["records"]:
                    observations.append(listing_obs(
                        rec["system"], rec["resource_id"], rec["record_type"], rec["result"],
                        rec.get("subject_reference") or rec.get("lookup_value"), evidence_id,
                        file_time, rec.get("recorded_at", t)))
                continue
            ids = set()
            for rec in snap["records"]:
                o = record_to_obs(rec, system, coll, evidence_id, file_time, t)
                if o:
                    o.obs_time = t
                    observations.append(o)
                    ids.add(o.resource_id)
            coverage.append(Coverage(evidence_id, file_time, t, system, ids))
    elif "service_exports" in data:                           # service resource export
        t0 = data.get("exported_at", file_time)
        for system, sections in data["service_exports"].items():
            for section, items in sections.items():
                for rec in items:
                    o = record_to_obs(rec, system, section, evidence_id, file_time, t0)
                    if o:
                        if section == "index_queries" and rec.get("query"):
                            k = key_of(rec["query"])
                            if k:
                                o.keys.add(k)
                        observations.append(o)
    elif data.get("records") and "record_type" in data["records"][0]:   # record listing
        for rec in data["records"]:
            o = listing_obs(rec["system"], rec["resource_id"], rec["record_type"], rec["result"],
                            rec.get("subject_reference") or rec.get("lookup_value"), evidence_id,
                            file_time, rec.get("recorded_at", file_time))
            observations.append(o)
    elif "records" in data:                                   # single collection capture
        t = data.get("captured_at", file_time)
        coll = data.get("collection", "")
        system = table_system(coll)
        ids = set()
        for rec in data["records"]:
            o = record_to_obs(rec, system, coll, evidence_id, file_time, t)
            if o:
                o.obs_time = t
                observations.append(o)
                ids.add(o.resource_id)
        if system:
            coverage.append(Coverage(evidence_id, file_time, t, system, ids))
    return observations, coverage


def listing_obs(system, rid, record_type, result, subject, evidence_id, file_time, t):
    present, exposed, enabled, restore = LISTING_STATE.get(result.upper(), (True, None, None, None))
    o = Observation(evidence_id, file_time, t, system, rid, present, source=f"listing:{record_type}")
    k = key_of(subject)
    if k:
        o.keys.add(k)
    if exposed or (exposed is None and present and k and k[0] in "eph"
                   and record_type not in ("DATA_FLOW", "BACKUP_ARCHIVE")):
        o.exposed.add({"e": "email", "p": "phone", "h": "email_hash", "u": "reference"}[k[0]]
                      if k else "email")
    if record_type == "DATA_FLOW":
        o.category, o.enabled = "DATA_FLOW", enabled
    else:
        o.category = record_type
        o.restore_allowed = restore
    return o


# ------------------------------------------------------------------- TXT
def parse_txt(path: Path, evidence_id: str, file_time: str):
    observations = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == 6 and RESOURCE_RE.fullmatch(parts[2]):
            t, system, rid, rtype, result, subject = parts
            observations.append(listing_obs(system, rid, rtype, result, subject,
                                            evidence_id, file_time, t))
    return observations, []


# ------------------------------------------------------------------- logs
KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
KEY_FIELDS = ("lookup_key", "subject_key", "subject_ref", "scan_key", "owner_key", "match_key",
              "correlation_key", "subject_reference", "matched_value")
# Fields that state what a trace was looking up (as opposed to what a result line contains).
LOOKUP_FIELDS = ("lookup_key", "subject_key", "subject_ref", "scan_key", "owner_key", "match_key")


def parse_log_line(line: str) -> dict | None:
    line = line.strip()
    m = re.match(r"(\d{4}-\d\d-\d\dT[\d:]+Z)\s*(.*)", line)
    if not m:
        return None
    ts, rest = m.groups()
    fields = {"ts": ts}
    if rest.startswith("{"):                                   # JSON line
        try:
            fields.update({k: str(v) for k, v in json.loads(rest).items()})
        except json.JSONDecodeError:
            return None
    elif rest.startswith("|"):                                 # | LEVEL | svc | trace | event | kv
        parts = [p.strip() for p in rest.strip("|").split("|", 4)]
        if len(parts) == 5:
            fields.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
            rest = parts[4]
    elif rest.startswith("["):                                 # [LEVEL] [svc] [trace] event: kv
        mm = re.match(r"\[(\w+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(\w+):\s*(.*)", rest)
        if mm:
            fields.update(level=mm.group(1), service=mm.group(2), trace_id=mm.group(3),
                          event=mm.group(4))
            rest = mm.group(5)
    rest = rest.replace('\\"', '"')
    for k, v in KV_RE.findall(rest):
        v = v.strip('"')
        if k == "message":
            continue
        fields.setdefault(k, v)
    # requestId=/function= style
    fields.setdefault("trace_id", fields.get("requestId", ""))
    fields.setdefault("service", fields.get("function", ""))
    msg = re.search(r'message="(.*)"\s*$', rest)
    if msg:
        for k, v in KV_RE.findall(msg.group(1)):
            fields.setdefault(k, v.strip('"'))
    return fields


def parse_log(path: Path, evidence_id: str, file_time: str):
    lines = [f for f in (parse_log_line(l) for l in path.read_text(encoding="utf-8").splitlines()) if f]

    def trace(f):
        return (f.get("service", ""), f.get("trace_id")) if f.get("trace_id") else None
    trace_keys, trace_lookups, trace_resources = defaultdict(set), defaultdict(set), defaultdict(set)
    for f in lines:
        t = trace(f)
        if t is None:
            continue
        for kf in KEY_FIELDS:
            k = key_of(f.get(kf))
            if k:
                trace_keys[t].add(k)
                if kf in LOOKUP_FIELDS:
                    trace_lookups[t].add(k)
        if f.get("resource_id"):
            trace_resources[t].add(f["resource_id"])
    observations = []
    for f in lines:
        o = log_event_obs(f, evidence_id, file_time)
        if not o:
            continue
        t = trace(f)
        # A lookup key travels to the trace's result line only when the trace is one lookup
        # of one resource; a reused or multi-lookup trace id must not cross-link identifiers.
        if t is not None and len(trace_lookups[t]) <= 1 and len(trace_resources[t]) <= 1:
            o.keys |= trace_keys[t]
        observations.append(o)
    return observations, []


def log_event_obs(f: dict, evidence_id: str, file_time: str) -> Observation | None:
    event = f.get("event", "")
    if event == "record_lookup":
        return listing_obs(f["system"], f["resource_id"], f["record_type"], f["result"],
                           f.get("subject_reference"), evidence_id, file_time, f["ts"])
    rid = f.get("resource_id")
    if not rid or not RESOURCE_RE.fullmatch(rid) or event.endswith("_started"):
        return None
    system = system_for(rid) or f.get("service")
    if f.get("service") == "support_replica":
        system = "support_platform"
    o = Observation(evidence_id, file_time, f["ts"], system, rid, None, source=event)
    for kf in ("correlation_key", "matched_value", "subject_reference", "customer_reference",
               "subject_ref"):
        k = key_of(f.get(kf))
        if k:
            o.keys.add(k)
    exposed_from = {"email": "email", "requester_email": "email", "customer_email": "email",
                    "owner_email": "email", "phone": "phone", "requester_phone": "phone",
                    "requester_name": "name", "email_hash": "email_hash",
                    "customer_reference": "reference"}
    for fld, label in exposed_from.items():
        if not is_null(f.get(fld)):
            o.exposed.add(label)
            k = key_of(f.get(fld))
            if k:
                o.keys.add(k)
    if event in ("profile_lookup_completed", "audit_record_read_completed", "contact_query_completed",
                 "contact_lookup_completed"):
        rows = f.get("rows")
        status = f.get("http_status")
        o.present = (rows not in (None, "0")) if rows is not None else status == "200"
        if status == "404":
            o.present = False
    elif event == "index_query_completed":
        o.present = f.get("result", "hit") == "hit"
    elif event in ("support_ticket_read_completed", "analytics_profile_read_completed",
                   "analytics_profile_read", "export_metadata_read_completed"):
        o.present = True
    elif event in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
        o.present = True
        k = key_of(f.get("matched_value"))
        o.exposed.add({"e": "email", "p": "phone", "h": "email_hash"}.get(k[0], "email") if k else "email")
    elif event == "export_download_check_completed":
        o.present = as_bool(f.get("available")) is not False
    elif event in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
        o.present = True
        en = as_bool(f.get("enabled"))
        if as_bool(f.get("scheduler_registered")) is False:
            en = False
        o.enabled = en
    elif event == "restore_authorization_completed":
        o.present = True
        o.restore_allowed = str(f.get("decision", "")).lower() in ("allow", "allowed", "approve", "approved")
    elif event == "message_read":
        o.present = True
        found = keys_in_text(f.get("message_text", ""))
        o.keys |= found
        o.exposed |= {"email" if k.startswith("e:") else "phone" for k in found if k[0] in "ep"}
    elif event == "retained_order_loaded":
        o.present = True
    elif event == "restore_policy_read_completed":
        o.present = as_bool(f.get("archive_present")) is not False
        o.restore_allowed = as_bool(f.get("restore_allowed"))
    elif event == "restore_candidate_evaluated":
        o.present = True
        o.restore_allowed = as_bool(f.get("eligible")) is True and as_bool(f.get("deletion_block")) is not True
    else:
        return None
    if event == "audit_record_read_completed":
        o.retention_until = f.get("retention_until")
    if event == "analytics_profile_read" and f.get("direct_identifiers") == "0":
        o.exposed -= {"email", "phone"}
    rec = {"email_opt_in": 1} if event == "contact_lookup_completed" else {}
    if event == "attachment_pii_scan":
        rec = {"file": f.get("file")}
    if event in ("message_pii_scan", "message_read"):
        rec = {"message_text": ""}
    o.category = category_for(system, event, rec)
    return o


# -------------------------------------------------------------------- PDF
def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


NEGATION_RE = re.compile(r"\b(deleted|removed|purged|erased|destroyed|anonymi[sz]ed|no longer|"
                         r"not found|does not exist)\b", re.I)


def parse_pdf(path: Path, evidence_id: str, file_time: str):
    return document_observations(pdf_text(path), evidence_id, file_time)


def document_observations(text: str, evidence_id: str, file_time: str):
    """Business documents (invoices, attachments, extracts) as observations.

    A document that names one record *is* that record: everything it shows belongs to it.
    When it names several records, each gets only the identifiers on its own line, so a
    customer listed in another section is never linked to it. A record named next to
    deletion wording ("deleted", "removed", ...) is only mentioned, not shown as present.
    """
    lines = text.splitlines()
    rids = sorted(set(x.group(0) for x in RESOURCE_RE.finditer(text)))
    observations = []
    for m in rids:
        own = [l for l in lines if re.search(rf"\b{re.escape(m)}\b", l)]
        keys = keys_in_text(text) if len(rids) == 1 else set().union(*(keys_in_text(l) for l in own))
        mentioned_only = any(NEGATION_RE.search(l) for l in own)
        system = system_for(m)
        o = Observation(evidence_id, file_time, file_time, system, m,
                        None if mentioned_only else True, keys=set(keys), source="document")
        o.exposed = {"email" for k in keys if k.startswith("e:")} | \
                    {"phone" for k in keys if k.startswith("p:")}
        if system == "support_platform" and ("attachment" in text.lower() or ".pdf" in text):
            o.category = "SUPPORT_ATTACHMENT"
        else:
            o.category = category_for(system, "document")
        mm = re.search(r"retain until (\d\d \w{3} \d{4})", text)
        if mm and system == "security_service":
            from datetime import datetime
            o.retention_until = datetime.strptime(mm.group(1), "%d %b %Y").strftime("%Y-%m-%dT00:00:00Z")
        observations.append(o)
    return observations, [], text


# ------------------------------------------------------------- dispatcher
def load_manifest(dataset: Path) -> list[dict]:
    with open(dataset / "evidence_files.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class EvidenceGap:
    """An evidence file that exists but could not be read; it may hold the answer."""

    file_id: str
    captured_at: str
    reason: str


def resolve_evidence_path(dataset: Path, rel: str) -> Path | None:
    """The manifest is input data: refuse paths that leave the dataset folder."""
    root = dataset.resolve()
    path = (dataset / rel).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def parse_all(dataset: Path, vision=None, gaps: list | None = None):
    """Parse every evidence file. Returns observations, coverage and raw text per file.

    A file that cannot be read (bad path, corrupt content, screenshot without a model) is
    appended to `gaps` instead of being silently skipped, so it can block a COMPLETE verdict.
    """
    observations, coverage, texts = [], [], {}
    gaps = gaps if gaps is not None else []
    for row in load_manifest(dataset):
        fid, t = row["file_id"], row["captured_at"]
        path = resolve_evidence_path(dataset, row["file_path"])
        if path is None:
            gaps.append(EvidenceGap(fid, t, "path missing or outside the dataset folder"))
            continue
        ext = path.suffix.lower()
        try:
            if ext == ".sql":
                obs, cov = parse_sql(path, fid, t)
            elif ext == ".json":
                obs, cov = parse_json(path, fid, t)
            elif ext == ".log":
                obs, cov = parse_log(path, fid, t)
            elif ext == ".txt":
                obs, cov = parse_txt(path, fid, t)
            elif ext == ".pdf":
                obs, cov, texts[fid] = parse_pdf(path, fid, t)
            elif ext in (".png", ".jpg", ".jpeg"):
                if vision is None:
                    gaps.append(EvidenceGap(fid, t, "screenshot not read: no vision extractor"))
                    continue
                obs, cov, texts[fid] = vision.parse_image(path, fid, t, row["origin"])
                if fid in vision.missing:
                    gaps.append(EvidenceGap(fid, t, vision.missing[fid]))
            else:
                gaps.append(EvidenceGap(fid, t, f"unsupported file type {ext}"))
                continue
            if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
                texts[fid] = path.read_text(encoding="utf-8")
        except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
            gaps.append(EvidenceGap(fid, t, f"unreadable: {type(exc).__name__}"))
            continue
        observations += obs
        coverage += cov
    return observations, coverage, texts
