"""Screenshots are the only evidence code cannot read, so Claude transcribes them.

The model only transcribes visible rows into a fixed JSON schema (cached on disk). Turning
those rows into resource states is deterministic and lives here.
"""
from __future__ import annotations

import json
from pathlib import Path

from .evidence import category_for
from .model import RESOURCE_RE, Observation, is_null, key_of, system_for

PROMPT = (Path(__file__).resolve().parent / "prompts" / "screenshot_extractor.md").read_text(encoding="utf-8")
NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "screen_title": NULLABLE,
        "captured_at": NULLABLE,
        "records": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "resource_id": NULLABLE, "label": NULLABLE, "email": NULLABLE, "phone": NULLABLE,
                "user_id": NULLABLE, "full_name": NULLABLE, "status": NULLABLE,
                "details": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"], "additionalProperties": False}},
            },
            "required": ["resource_id", "label", "email", "phone", "user_id", "full_name",
                         "status", "details"],
            "additionalProperties": False}},
    },
    "required": ["screen_title", "captured_at", "records"],
    "additionalProperties": False,
}

ORIGIN_SYSTEM = {
    "account_admin_portal": "account_service", "search_index_admin": "search_service",
    "integration_admin": "integration_service", "analytics_platform": "analytics_platform",
    "export_service": "export_service", "support_platform": "support_platform",
    "backup_service": "backup_service", "security_service": "security_service",
    "identity_service": "identity_service", "object_storage": "object_storage",
    "marketing_platform": "marketing_platform",
}
DISABLED_WORDS = ("disabled", "paused", "stopped", "inactive")
BLOCKED_WORDS = ("blocked", "restore disabled", "not restorable", "locked")
ANON_WORDS = ("anonymized", "anonymised", "redacted", "removed", "deleted")
# Only these fields may change a record's state. Free-text details (notes, messages,
# activity lines) are transcribed text from the screen, and could say anything - including
# injected instructions - so they never flip a flow to disabled or a backup to blocked.
STATE_DETAIL_KEYS = {"status", "state", "mode", "sync mode", "restore", "restore access",
                     "restore allowed", "restore status", "enabled"}
MAX_RECORDS = 200
MAX_FIELD = 320


class VisionExtractor:
    def __init__(self, llm):
        self.llm = llm
        self.missing: dict[str, str] = {}        # evidence_id -> why it could not be read
        self.rejected: dict[str, int] = {}       # evidence_id -> model values dropped by validation

    def parse_image(self, path: Path, evidence_id: str, file_time: str, origin: str):
        data = self.llm.json_call("screenshot_extraction", PROMPT,
                                  "Extract every record shown in this screenshot.", SCHEMA,
                                  image_path=path, max_tokens=8000)
        if data is None:
            self.missing[evidence_id] = ("screenshot not read: no model answer "
                                         "(offline, no API key, API failure or refusal)")
            return [], [], ""
        data, dropped = validate_extraction(data)
        if dropped:
            self.rejected[evidence_id] = dropped
        text = json.dumps(data, ensure_ascii=False)
        return self.to_observations(data, evidence_id, file_time, origin), [], text

    @staticmethod
    def to_observations(data: dict, evidence_id: str, file_time: str, origin: str):
        default_system = ORIGIN_SYSTEM.get(origin)
        out = []
        for rec in data.get("records", []):
            rid = (rec.get("resource_id") or "").strip()
            system = system_for(rid)
            if not system:
                continue
            details = {d["key"].lower(): d["value"] for d in rec.get("details") or []}
            o = Observation(evidence_id, file_time, file_time, system, rid, True, source="screenshot")
            for field, label in (("email", "email"), ("phone", "phone"), ("full_name", "name"),
                                 ("user_id", "reference")):
                if is_null(rec.get(field)):
                    continue
                o.exposed.add(label)
                k = key_of(rec.get(field))
                if k:
                    o.keys.add(k)
            for v in details.values():
                k = key_of(v)
                if k:
                    o.keys.add(k)
            state_text = [rec.get("status") or ""] + [v for k, v in details.items() if k in STATE_DETAIL_KEYS]
            status = " ".join(str(x) for x in state_text).lower()
            if system == "integration_service":
                o.enabled = not any(w in status for w in DISABLED_WORDS)
                o.exposed.clear()
            elif system == "backup_service":
                o.restore_allowed = not any(w in status for w in BLOCKED_WORDS)
                o.exposed.clear()
            elif any(w in (rec.get("status") or "").lower() for w in ANON_WORDS):
                o.exposed.clear()
            o.category = category_for(system, "screenshot " + (default_system or ""), {})
            if system == "support_platform" and (rec.get("label") or "").lower().endswith(
                    (".pdf", ".jpg", ".png")):
                o.category = "SUPPORT_ATTACHMENT"
            out.append(o)
        return out


def validate_extraction(data) -> tuple[dict, int]:
    """Check model output before any of it is used; return the cleaned data and drop count.

    Structured output guarantees the shape, not the content. Ids must match the system's
    resource-id grammar, identifiers must parse as emails / phones / user ids, and text is
    length-capped. Anything else is dropped (counted), never repaired by guessing.
    """
    dropped = 0
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return {"screen_title": None, "captured_at": None, "records": []}, 1
    clean = []
    for rec in records[:MAX_RECORDS]:
        if not isinstance(rec, dict):
            dropped += 1
            continue
        rec = dict(rec)
        for f in ("resource_id", "label", "email", "phone", "user_id", "full_name", "status"):
            v = rec.get(f)
            if v is not None and (not isinstance(v, str) or len(v) > MAX_FIELD):
                rec[f] = None
                dropped += 1
        if rec.get("resource_id") and not RESOURCE_RE.fullmatch(rec["resource_id"].strip()):
            rec["resource_id"] = None
            dropped += 1
        for f in ("email", "phone", "user_id"):
            if rec.get(f) and not is_null(rec[f]) and key_of(rec[f]) is None:
                rec[f] = None
                dropped += 1
        rec["details"] = [d for d in (rec.get("details") or [])[:24]
                          if isinstance(d, dict) and isinstance(d.get("key"), str)
                          and isinstance(d.get("value"), str) and len(d["value"]) <= MAX_FIELD]
        clean.append(rec)
    dropped += max(0, len(records) - MAX_RECORDS)
    return {**data, "records": clean}, dropped
