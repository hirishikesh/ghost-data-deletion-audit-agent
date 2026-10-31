"""Identity resolution: every identifier a person has ever had, and when they had it.

Each identifier (email, phone, email hash) gets validity intervals rebuilt from the current
snapshot plus the audit log of changes. A request email is resolved to the person who held
it *at requested_at*. When that is not one person, the answer is "ambiguous" or "unknown" -
never the first id in sorted order.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .model import email_hash, key_of, norm_email

BEGIN, END = "0000-00-00T00:00:00Z", "9999-12-31T23:59:59Z"
TRACKED = ("email", "phone", "email_hash")


@dataclass
class Resolution:
    user_id: str | None
    status: str                       # current | historical | ambiguous | unknown
    candidates: list[str] = field(default_factory=list)
    audit_ids: list[str] = field(default_factory=list)   # audit rows proving a historical match
    reason: str = ""


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.users: dict[str, dict] = {}
        self.keys: dict[str, set] = defaultdict(set)         # user_id -> keys
        self.owners: dict[str, set] = defaultdict(set)       # key -> user_ids
        self.history: dict[str, list] = defaultdict(list)    # user_id -> audit rows
        self.valid: dict[str, list] = defaultdict(list)      # key -> [(user_id, start, end)]
        with open(dataset / "user_identities.csv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.users[row["user_id"]] = row
        with open(dataset / "user_audit_log.csv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.history[row["user_id"]].append(row)
        for uid, row in self.users.items():
            self._add(uid, uid, BEGIN, END)
            for fld in TRACKED:
                self._timeline(uid, fld, row.get(fld, ""))

    def _timeline(self, uid: str, fld: str, current: str):
        """Rebuild when each value of one field was valid, from the change log."""
        changes = sorted((r for r in self.history[uid] if r["field_name"] == fld),
                         key=lambda r: r["event_time"])
        spans, start = [], BEGIN
        for r in changes:
            spans.append((r["old_value"], start, r["event_time"]))
            start = r["event_time"]
        spans.append((current, start, END))
        for value, s, e in spans:
            self._add(uid, value, s, e)
            if fld == "email" and value:
                # The hash of every email the person used, valid while that email was.
                self._add(uid, email_hash(value), s, e)

    def _add(self, uid: str, value, start: str, end: str):
        k = key_of(value)
        if k:
            self.keys[uid].add(k)
            self.owners[k].add(uid)
            self.valid[k].append((uid, start, end))

    def resolve_detail(self, email: str, at: str | None = None) -> Resolution:
        """Who held this email at time `at` (or ever, if `at` is None)."""
        k = "e:" + norm_email(email)
        owners = sorted(self.owners.get(k, set()))
        if not owners:
            return Resolution(None, "unknown", reason="email not in the identity registry")
        if at:
            active = sorted({u for u, s, e in self.valid[k] if s <= at < e})
            if len(active) > 1:
                return Resolution(None, "ambiguous", active,
                                  reason="email held by several people at requested_at")
            if len(active) == 1:
                return self._resolved(active[0], k)
        if len(owners) == 1:
            res = self._resolved(owners[0], k)
            if at:
                res.reason = "email not held by anyone at requested_at; single historical owner"
            return res
        return Resolution(None, "ambiguous", owners,
                          reason="email held by several people over time")

    def _resolved(self, uid: str, k: str) -> Resolution:
        current = norm_email(self.users[uid]["email"]) == k[2:]
        proof = [] if current else [r["audit_id"] for r in self.history[uid]
                                    if r["field_name"] == "email" and norm_email(r["old_value"]) == k[2:]]
        return Resolution(uid, "current" if current else "historical", [uid], proof)

    def resolve(self, email: str, at: str | None = None) -> tuple[str | None, list[str]]:
        """Backward-compatible form: (user_id or None, audit ids proving a historical match)."""
        r = self.resolve_detail(email, at)
        return r.user_id, r.audit_ids

    def unique_keys(self, uid: str) -> set[str]:
        """Keys that belong to this user only (a shared old phone is not proof)."""
        return {k for k in self.keys[uid] if self.owners[k] == {uid}}
