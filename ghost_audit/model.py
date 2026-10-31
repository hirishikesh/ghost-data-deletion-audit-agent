"""Shared data model: every evidence source is normalised into Observations."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,}\d")
RESOURCE_RE = re.compile(
    r"\b(account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"cache|marketing|email_delivery|object|legal_hold|telemetry)_\d+\b"
)

# Resource-id prefix -> owning system, used when a source does not name the system.
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "marketing": "marketing_platform",
    "email_delivery": "email_delivery_service",
    "object": "object_storage",
    "legal_hold": "legal_service",
    "telemetry": "product_telemetry",
}

DIRECT_IDENTIFIERS = {"name", "email", "phone", "email_hash"}

NULLS = {None, "", "null", "none", "None", "NULL", "[REDACTED]", "redacted", "no direct identifier"}


def is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in NULLS)


def system_for(resource_id: str) -> Optional[str]:
    m = RESOURCE_RE.fullmatch(resource_id or "")
    return PREFIX_SYSTEM.get(m.group(1)) if m else None


def norm_email(v: str) -> str:
    return v.strip().strip('"').lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v)


def email_hash(email: str) -> str:
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


def key_of(value) -> Optional[str]:
    """Turn a raw identifier into a typed lookup key (e:/p:/h:/u:), or None."""
    if is_null(value):
        return None
    v = str(value).strip().strip('"')
    if USER_RE.fullmatch(v):
        return "u:" + v
    if EMAIL_RE.fullmatch(v):
        return "e:" + norm_email(v)
    if HASH_RE.fullmatch(v.lower()):
        return "h:" + v.lower()
    if PHONE_RE.fullmatch(v):
        return "p:" + norm_phone(v)
    return None


def keys_in_text(text: str) -> set[str]:
    """Every identifier key that appears anywhere in free text."""
    out = set()
    for m in EMAIL_RE.findall(text or ""):
        out.add("e:" + norm_email(m))
    for m in USER_RE.findall(text or ""):
        out.add("u:" + m)
    for m in HASH_RE.findall((text or "").lower()):
        out.add("h:" + m)
    for m in PHONE_RE.findall(text or ""):
        if len(norm_phone(m)) >= 8:
            out.add("p:" + norm_phone(m))
    return out


@dataclass
class Observation:
    """One fact about one resource, as seen in one evidence item."""

    evidence_id: str            # file_id (or activity_id) that shows the fact
    file_time: str              # capture time of the whole evidence item (manifest)
    obs_time: str               # time the fact itself was observed
    system: str
    resource_id: str
    present: Optional[bool]     # True present, False absent, None = link only
    keys: set = field(default_factory=set)        # identifiers that tie it to a person
    exposed: set = field(default_factory=set)     # direct identifiers still visible
    category: Optional[str] = None                # deletion_rules data_category, if known
    enabled: Optional[bool] = None                # data flows
    restore_allowed: Optional[bool] = None        # backups
    retention_until: Optional[str] = None         # security audit records
    source: str = ""                              # table / collection / event name
    copy: str = "primary"                         # physical copy: primary or a named replica
    logically_deleted: bool = False               # row flagged deleted but still stored


@dataclass
class Coverage:
    """A full snapshot of one system: anything not listed was absent at that time."""

    evidence_id: str
    file_time: str
    obs_time: str
    system: str
    resource_ids: set
    copy: str = "primary"


TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def valid_ts(value) -> bool:
    """Timestamps are compared as strings, so only one exact UTC format is accepted."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        return False
    from datetime import datetime
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True
