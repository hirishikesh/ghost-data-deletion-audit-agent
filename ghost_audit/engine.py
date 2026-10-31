"""Deterministic audit engine.

For one request: find every resource tied to the person, reconstruct its state at
audit_at from evidence (never from workflow claims), apply deletion_rules.csv and
emit ordered FINDING / REMEDIATION / VERIFICATION steps.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import SYSTEM_CATEGORY
from .model import Coverage, Observation, is_null, keys_in_text, key_of, system_for

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
                 "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
                     "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED",
                      "VERIFY_RESTORE_BLOCKED"]
PHASE_ORDER = {"FINDING": FINDING_ORDER, "REMEDIATION": REMEDIATION_ORDER,
               "VERIFICATION": VERIFICATION_ORDER}
STATE = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
         "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
         "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
         "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
         "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
         "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
         "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"}

DIRECT = {"name", "email", "phone", "email_hash"}
# Worker operations reveal which treatment a record is meant to get. They are used only
# as a category hint when the evidence itself does not say, never as proof of state.
HINT_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT",
               "ANONYMIZE_RECORD": "ANONYMIZE", "RETAIN_RECORD": "RETAIN"}
DEFAULT_CATEGORY = {"support_platform": "SUPPORT_MESSAGE", "analytics_platform": "ANALYTICS_PROFILE"}
LINK_RE = re.compile(r"(?:linked|confirmed) (\S+) \(([^)]+)\) (?:to|belongs only to) (\S+) and (user_\d+)")


def alias_exposure(text: str, alias: str, rid: str) -> set:
    """Identifiers visible on the record a catalog alias points to - not the whole screen.

    A screenshot lists many customers; only the row whose label or id is the alias counts.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        rows = [r for r in data["records"] if isinstance(r, dict)
                and alias in (r.get("label"), r.get("resource_id"), rid)]
        exposed = set()
        for r in rows:
            for fld, label in (("email", "email"), ("phone", "phone"), ("full_name", "name")):
                if not is_null(r.get(fld)):
                    exposed.add(label)
        return exposed
    lines = [l for l in text.splitlines() if alias in l or rid in l]
    found = set().union(*(keys_in_text(l) for l in lines)) if lines else set()
    return {"email" for k in found if k.startswith("e:")} | {"phone" for k in found if k.startswith("p:")}


@dataclass
class Activity:
    activity_id: str
    time: str
    event_type: str
    source: str
    target: str
    resource_id: str
    result: str
    details: str


@dataclass
class Resource:
    resource_id: str
    system: str
    category: str | None = None
    action: str | None = None
    state: Observation | None = None         # latest evidence-backed observation
    link_time: str = ""
    # How the resource was tied to the person. Only "evidence" is proof; the workflow/
    # analytics kinds are correlations ("*_tie" ones are a pick among equals).
    link: str = "evidence"


HEURISTIC_LINKS = {"workflow_tie", "analytics_tie"}
DESTRUCTIVE = {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"}


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    review: list = field(default_factory=list)     # why a human must look before acting
    links: dict = field(default_factory=dict)      # resource_id -> link kind


class AuditEngine:
    def __init__(self, dataset: Path, identities, observations, coverage, texts, manifest,
                 gaps: list | None = None):
        self.ids = identities
        self.gaps = list(gaps or [])     # evidence files that exist but could not be read
        self.vision_rejected: dict = {}  # screenshot values dropped by validation
        self.rules = self._load_rules(dataset)
        self.activities = self._load_activities(dataset)
        self.file_time = {r["file_id"]: r["captured_at"] for r in manifest}
        self.document_files = {r["file_id"] for r in manifest
                               if r["file_path"].lower().endswith((".pdf", ".png", ".jpg", ".jpeg"))}
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.coverage: dict[str, list[Coverage]] = defaultdict(list)
        for c in coverage:
            self.coverage[c.system].append(c)
        for o in observations:
            self.by_resource[o.resource_id].append(o)
        self.hints: dict[str, list[Activity]] = defaultdict(list)
        for a in self.activities:
            if a.source == "deletion_worker":
                self.hints[a.resource_id].append(a)
        self._apply_catalog_links(texts)
        # key -> resources it appears on: (resource, time, found?) - a lookup that came back
        # empty only shows the key was searched, so it is a weaker link than a record holding it.
        self.key_index: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
        for rid, obs in self.by_resource.items():
            for o in obs:
                for k in o.keys:
                    self.key_index[k].append((rid, o.obs_time, o.present is not False))
        # Resources tied by evidence to a known person; never handed to someone else.
        self.owned: dict[str, set] = defaultdict(set)
        for k, hits in self.key_index.items():
            for rid, _, _ in hits:
                self.owned[rid] |= self.ids.owners.get(k, set())
        self.claimed: dict[str, str] = {}   # workflow-linked resource -> person (user_id) holding it
        self.workflow_account: dict[str, str] = {}   # case_id -> account from its workflow event
        self.workflow_unique: set[str] = set()       # cases whose workflow account was unique
        self.workflow_choices: dict[str, int] = {}   # tied case -> accounts it was chosen from
        self.analytics_fallback = True               # see assign_anonymised_profiles(); ablation switch
        self.analytics_assignment: dict[str, tuple[str, str]] = {}  # case_id -> (profile, how)
        self.analytics_uncertain: dict[str, str] = {}               # case_id -> why none assigned

    # ------------------------------------------------------------ loading
    @staticmethod
    def _load_rules(dataset: Path) -> dict[str, dict]:
        with open(dataset / "deletion_rules.csv", encoding="utf-8") as fh:
            return {r["data_category"]: r for r in csv.DictReader(fh)}

    @staticmethod
    def _load_activities(dataset: Path) -> list[Activity]:
        with open(dataset / "system_activity.csv", encoding="utf-8") as fh:
            return [Activity(r["activity_id"], r["event_time"], r["event_type"], r["source_system"],
                             r["target_system"], r["resource_id"], r["reported_result"], r["details"])
                    for r in csv.DictReader(fh)]

    def _apply_catalog_links(self, texts: dict[str, str]):
        """Data-catalog links tie a resource (and its business alias) to a person.

        Any evidence file that shows the alias or resource id is then evidence that the
        resource is present at that file's capture time.
        """
        for a in self.activities:
            if a.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            m = LINK_RE.search(a.details)
            if not m:
                continue
            rid, alias, email, uid = m.groups()
            system = a.target if a.target != "object_storage" else "object_storage"
            link = Observation(a.activity_id, a.time, a.time, system, rid, None,
                               keys={k for k in (key_of(email), key_of(uid)) if k}, source="catalog_link")
            self.by_resource[rid].append(link)
            for fid, text in texts.items():
                # Business aliases (file names, statement numbers) are matched only in documents
                # and screenshots; structured sources carry their own resource ids.
                if not text or fid not in self.document_files:
                    continue
                if alias == uid:
                    # A bare user id is too common to match as text; only a screen whose
                    # record *is* that reference (e.g. a reference registry) counts.
                    shown = f'"label": "{alias}"' in text
                else:
                    shown = alias in text or re.search(rf"\b{re.escape(rid)}\b", text)
                already = any(o.evidence_id == fid and o.present is not None for o in self.by_resource[rid])
                if shown and not already:
                    ft = self.file_time.get(fid, a.time)
                    o = Observation(fid, ft, ft, system_for(rid) or system, rid, True,
                                    keys=set(link.keys), source="catalog_alias")
                    o.exposed = alias_exposure(text, alias, rid)
                    self.by_resource[rid].append(o)

    # ------------------------------------------------------------ discovery
    def discover(self, uid: str, audit_at: str, requested_at: str = "") -> dict[str, Resource]:
        keys = self.ids.unique_keys(uid)
        found: dict[str, Resource] = {}
        for k in keys:
            for rid, t, strong in self.key_index.get(k, []):
                # Only evidence gathered for this request (between request and audit) links a
                # resource to it; older sightings belong to earlier requests or are search misses.
                if requested_at <= t <= audit_at:
                    if rid not in found:
                        found[rid] = Resource(rid, self._system(rid), link_time=t)
        # A connector sync or a backup restore that recreated a record ties that record to
        # the same person as the flow/backup that produced it.
        for a in self.activities:
            if a.resource_id in found and a.time <= audit_at and "repopulated downstream resource" in a.details:
                target = a.details.rstrip(".").split()[-1]
                if target not in found and system_for(target):
                    found[target] = Resource(target, self._system(target), link_time=a.time)
        # A search document names the account it was built from.
        for rid in list(found):
            for o in self.by_resource[rid]:
                for k in o.keys:
                    if k.startswith("r:") and k[2:] not in found and o.obs_time <= audit_at:
                        found[k[2:]] = Resource(k[2:], self._system(k[2:]), link_time=o.obs_time)
        return found

    STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

    def assign_workflow_accounts(self, cases: list[dict], uid_of, is_deletion) -> None:
        """Tie requests to the account their deletion workflow processed.

        The workflow's reported_status is produced from the worker's account-deletion event:
        that event runs 40-80 min before reported_at and its result (SUCCESS / PARTIAL /
        FAILED) is exactly the reported status (COMPLETE / PARTIALLY_COMPLETE /
        NOT_STARTED) - 223 of 224 requests whose account evidence names agree. So the
        result is used to *link* event and request, never as proof of the account's state.
        Requests left with one candidate take it; that account then leaves every other
        request's list; repeat until stable.
        """
        from datetime import datetime, timedelta
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        events = [a for a in self.activities if a.source == "deletion_worker"
                  and a.target == "account_service" and a.event_type == "DELETE_RECORD"]
        taken: dict[str, str] = {}      # account -> case_id holding it by evidence
        open_cases = {}
        for c in cases:
            uid = uid_of(c)
            if not uid or not is_deletion(c):
                continue
            linked = [r for r in self.discover(uid, c["audit_at"], c["requested_at"])
                      if r.startswith("account_")]
            if linked:
                for r in linked:
                    taken[r] = c["case_id"]
                continue
            rep = datetime.strptime(c["reported_at"], fmt)
            want = self.STATUS_RESULT.get(c["reported_status"])
            in_window = [a for a in events
                         if rep - timedelta(minutes=90) <= datetime.strptime(a.time, fmt) <= rep + timedelta(minutes=10)
                         and c["requested_at"] <= a.time <= c["audit_at"]
                         and not (self.owned.get(a.resource_id, set()) - {uid})]
            # Prefer the event whose result produced this status; if none matches, any
            # account event of this request's workflow window.
            cands = {a.resource_id for a in in_window if a.result == want} or \
                    {a.resource_id for a in in_window}
            open_cases[c["case_id"]] = cands
        for cands in open_cases.values():
            cands -= set(taken)
        changed = True
        while changed:
            changed = False
            for cid, cands in sorted(open_cases.items()):
                if len(cands) == 1 and cid not in self.workflow_account:
                    rid = next(iter(cands))
                    self.workflow_account[cid] = rid
                    self.workflow_unique.add(cid)
                    for other, oc in open_cases.items():
                        if other != cid and rid in oc:
                            oc.discard(rid)
                            changed = True
        # Still ambiguous: several requests share one batch with the same status. Hand out
        # the remaining accounts in event order; flagged in the case notes as a tie.
        order = {a.resource_id: a.time for a in events}
        used = set(self.workflow_account.values())
        for cid, cands in sorted(open_cases.items()):
            if cid in self.workflow_account:
                continue
            free = sorted((c for c in cands if c not in used), key=lambda r: (order[r], r))
            if free:
                self.workflow_account[cid] = free[0]
                self.workflow_choices[cid] = len(free)
                used.add(free[0])

    def anonymise_candidates(self, case: dict, uid: str) -> set[str]:
        """Analytics profiles this request's completed workflow may have anonymised.

        Once anonymised, a profile carries no identifier, so evidence cannot link it. The
        worker logs each successful policy action in the request's batch, 0-90 min before
        reported_at - the window in which every evidence-linked redact/block/disable/retain
        action of a request lands. Candidates: successful anonymise actions in that window
        whose record evidence shows without identifiers and that no other person holds.
        """
        from datetime import datetime, timedelta
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        rep = datetime.strptime(case["reported_at"], fmt)

        def clean(rid):
            st = self.state_at(rid, "analytics_platform", case["audit_at"], use_coverage=False)
            return st is not None and st.present and not st.exposed
        return {a.resource_id for a in self.activities
                if a.source == "deletion_worker" and a.target == "analytics_platform"
                and a.event_type == "ANONYMIZE_RECORD" and a.result == "SUCCESS"
                and rep - timedelta(minutes=90) <= datetime.strptime(a.time, fmt) <= rep + timedelta(minutes=10)
                and case["requested_at"] <= a.time <= case["audit_at"]
                and not (self.owned.get(a.resource_id, set()) - {uid}) and clean(a.resource_id)}

    def assign_anonymised_profiles(self, eligible: list[tuple[dict, str]]) -> None:
        """Jointly give anonymised profiles to requests whose deletion is otherwise complete.

        A request takes a profile when it is the only eligible request left for it
        (constraint propagation). Where several requests share the same profiles: if there
        are as many profiles as requests, every request certainly has one and they are
        handed out in event order (a flagged tie); if there are fewer, which request had a
        profile is unknown, so none is assigned (flagged as uncertain).
        """
        cands = {c["case_id"]: self.anonymise_candidates(c, uid) for c, uid in eligible}
        cands = {k: v for k, v in cands.items() if v}
        changed = True
        while changed:
            changed = False
            for cid, cs in sorted(cands.items()):
                if cid in self.analytics_assignment or len(cs) != 1:
                    continue
                rid = next(iter(cs))
                holders = [k for k, v in cands.items() if rid in v and k not in self.analytics_assignment]
                if holders == [cid]:
                    self.analytics_assignment[cid] = (rid, "unique")
                    changed = True
            taken = {r for r, _ in self.analytics_assignment.values()}
            for cid, cs in cands.items():
                if cid not in self.analytics_assignment and cs & taken:
                    cs -= taken
                    changed = True
        # Remaining: connected groups of requests sharing profiles.
        order = {a.resource_id: a.time for a in self.activities if a.target == "analytics_platform"}
        left = {k: v for k, v in cands.items() if k not in self.analytics_assignment and v}
        seen = set()
        for cid in sorted(left):
            if cid in seen:
                continue
            group, pool, frontier = {cid}, set(left[cid]), [cid]
            while frontier:
                frontier.pop()
                for other, v in left.items():
                    if other not in group and v & pool:
                        group.add(other)
                        pool |= v
                        frontier.append(other)
            seen |= group
            # Interchangeable only if every request in the group could have every profile.
            if len(pool) == len(group) and all(left[k] == pool for k in group):
                for k, rid in zip(sorted(group), sorted(pool, key=lambda r: (order.get(r, ""), r))):
                    self.analytics_assignment[k] = (rid, f"tie, 1 of {len(pool)}")
            else:
                for k in group:
                    self.analytics_uncertain[k] = f"{len(pool)} anonymised profile(s) for {len(group)} requests"

    def _system(self, rid: str) -> str:
        s = system_for(rid)
        if s:
            return s
        for o in self.by_resource.get(rid, []):
            if o.system:
                return o.system
        return "unknown"

    # ------------------------------------------------------------ state
    def state_at(self, rid: str, system: str, audit_at: str, use_coverage: bool = True
                 ) -> Observation | None:
        """State at audit_at from evidence captured at or before audit_at, judged per copy.

        Every physical copy - the primary/service view and each named replica - keeps its
        own latest observation: a newer observation of the primary says nothing about a
        failover or regional replica. The record is present if any copy still holds it,
        and exposed if any copy holding it still shows identifiers. A later dump of the same
        replica that omits the row shows that replica absent (deletable records only).
        """
        obs = [o for o in self.by_resource.get(rid, [])
               if o.present is not None and o.obs_time <= audit_at and o.file_time <= audit_at
               and o.evidence_id.startswith("evidence_")]
        if obs:
            per_copy = {}
            for copy in sorted({o.copy for o in obs}):
                own = [o for o in obs if o.copy == copy]
                latest_t = max(o.obs_time for o in own)
                same = sorted((o for o in own if o.obs_time == latest_t),
                              key=lambda o: (o.file_time, o.evidence_id))
                m = Observation(same[-1].evidence_id, same[-1].file_time, latest_t, system, rid,
                                any(o.present for o in same), copy=copy)
                for o in same:
                    if o.present:
                        m.exposed |= o.exposed
                        m.evidence_id = o.evidence_id
                # A replica re-captured later without the row has lost it. The primary is
                # different: its "snapshots" are filtered extracts, so an explicit
                # observation of the primary is never overruled by an omission.
                if m.present and use_coverage and copy != "primary" and any(
                        c.copy == copy and latest_t < c.obs_time <= audit_at and c.file_time <= audit_at
                        and rid not in c.resource_ids for c in self.coverage.get(system, [])):
                    m.present = False
                per_copy[copy] = m
            holding = [m for m in per_copy.values() if m.present]
            if holding:
                merged = max(holding, key=lambda m: (m.obs_time, m.file_time, m.evidence_id))
                for m in holding:
                    merged.exposed |= m.exposed
            else:
                merged = max(per_copy.values(), key=lambda m: (m.obs_time, m.file_time, m.evidence_id))
            # Flow, restore and retention settings belong to the service, not to a copy.
            for attr in ("enabled", "restore_allowed", "retention_until"):
                stated = [o for o in obs if getattr(o, attr) is not None]
                if stated:
                    top_t = max(o.obs_time for o in stated)
                    top = sorted((o for o in stated if o.obs_time == top_t),
                                 key=lambda o: (o.file_time, o.evidence_id))
                    setattr(merged, attr, getattr(top[-1], attr))
            return merged
        if not use_coverage:
            return None
        covs = [c for c in self.coverage.get(system, [])
                if c.obs_time <= audit_at and c.file_time <= audit_at]
        if covs:
            c = max(covs, key=lambda c: (c.obs_time, c.file_time, c.evidence_id))
            return Observation(c.evidence_id, c.file_time, c.obs_time, system, rid,
                               rid in c.resource_ids)
        return None

    def category(self, res: Resource, audit_at: str) -> tuple[str | None, str | None]:
        cats = [o.category for o in sorted(self.by_resource.get(res.resource_id, []),
                                           key=lambda o: o.obs_time)
                if o.category and o.obs_time <= audit_at]
        hint = None
        for a in self.hints.get(res.resource_id, []):
            if a.time <= audit_at and a.event_type in HINT_ACTION:
                hint = HINT_ACTION[a.event_type]
        cat = cats[-1] if cats else SYSTEM_CATEGORY.get(res.system)
        if res.system in ("support_platform", "analytics_platform"):
            explicit = [c for c in cats if c not in ("SUPPORT_MESSAGE", "ANALYTICS_PROFILE")]
            if explicit:
                cat = explicit[-1]
            elif hint == "DELETE":
                cat = "SUPPORT_ATTACHMENT" if res.system == "support_platform" else "MARKETING_PROFILE"
            elif not cat:
                cat = DEFAULT_CATEGORY[res.system]
        if cat == "DATA_FLOW":
            return cat, "DISABLE"
        rule = self.rules.get(cat or "")
        return cat, rule["required_action"] if rule else None

    # ------------------------------------------------------------ planning
    def audit(self, case: dict, uid: str, extra_resources: dict[str, Resource] | None = None
              ) -> CaseResult:
        audit_at = case["audit_at"]
        case = dict(case, _uid=uid)
        res = self.discover(uid, audit_at, case["requested_at"])
        for rid, r in (extra_resources or {}).items():
            res.setdefault(rid, r)
        notes = []
        if not any(r.system == "account_service" for r in res.values()):
            rid = self.workflow_account.get(case["case_id"])
            if rid:
                unique = case["case_id"] in self.workflow_unique
                kind = "unique" if unique else \
                    f"tie in shared batch, 1 of {self.workflow_choices.get(case['case_id'], '?')}"
                notes.append(f"{rid}: account linked by its workflow event ({kind})")
                res[rid] = Resource(rid, "account_service",
                                    link="workflow_unique" if unique else "workflow_tie")
                self.claimed[rid] = uid
        if self.analytics_fallback and case["case_id"] in self.analytics_assignment \
                and not any(r.system == "analytics_platform" for r in res.values()):
            rid, kind = self.analytics_assignment[case["case_id"]]
            res[rid] = Resource(rid, "analytics_platform",
                                link="analytics_unique" if kind == "unique" else "analytics_tie")
            notes.append(f"{rid}: anonymised profile linked by its workflow window ({kind})")
        if case["case_id"] in self.analytics_uncertain:
            notes.append("anonymised analytics not assigned: " + self.analytics_uncertain[case["case_id"]])
        return self._plan(case, res, notes)

    def _plan(self, case: dict, res: dict[str, Resource], notes: list) -> CaseResult:
        audit_at = case["audit_at"]
        notes = list(notes)
        findings, remediation, verification = [], [], []
        for rid, r in sorted(res.items()):
            r.category, r.action = self.category(r, audit_at)
            # Snapshots are filtered extracts: a missing row proves absence only for records
            # that must be deleted, never that a retained or redacted record is gone.
            r.state = self.state_at(rid, r.system, audit_at, use_coverage=r.action == "DELETE")
            st = r.state
            ev = st.evidence_id if st else ""
            if r.action is None:
                notes.append(f"{rid}: no rule for category {r.category}")
                continue
            if r.category == "DATA_FLOW":
                if st and st.enabled:
                    findings.append(("REPORT_ACTIVE_DATA_FLOW", r, ev))
                    remediation.append(("DISABLE_DATA_FLOW", r))
                verification.append(("VERIFY_DATA_FLOW_DISABLED", r, ev))
            elif r.category == "BACKUP_ARCHIVE":
                if st and st.restore_allowed:
                    findings.append(("REPORT_RESTORE_RISK", r, ev))
                    remediation.append(("BLOCK_RESTORE", r))
                verification.append(("VERIFY_RESTORE_BLOCKED", r, ev))
            elif r.action == "DELETE":
                if st and st.present:
                    findings.append(("REPORT_REMAINING_RECORD", r, ev))
                    remediation.append(("DELETE_RECORD", r))
                if st or r.category == "USER_PROFILE":
                    verification.append(("VERIFY_RECORD_ABSENT", r, ev))
            elif r.action == "RETAIN":
                if r.category == "SECURITY_AUDIT_LOG" and st and st.retention_until \
                        and st.retention_until < audit_at:
                    # Outside the retention period the record no longer has to be kept.
                    if st.present:
                        findings.append(("REPORT_REMAINING_RECORD", r, ev))
                        remediation.append(("DELETE_RECORD", r))
                    verification.append(("VERIFY_RECORD_ABSENT", r, ev))
                elif st is None or st.present:
                    verification.append(("VERIFY_RECORD_PRESENT", r, ev))
            elif r.action == "REDACT":
                if st and st.present:
                    if st.exposed & DIRECT:
                        findings.append(("REPORT_EXPOSED_PII", r, ev))
                        remediation.append(("REDACT_RECORD", r))
                    verification.append(("VERIFY_RECORD_REDACTED", r, ev))
            elif r.action == "ANONYMIZE":
                if st and st.present:
                    if st.exposed:
                        findings.append(("REPORT_EXPOSED_PII", r, ev))
                        remediation.append(("ANONYMIZE_RECORD", r))
                    verification.append(("VERIFY_RECORD_ANONYMIZED", r, ev))
        review = []
        # Missing evidence is never read as proof of deletion.
        unproven = [r.resource_id for op, r, ev in verification if not ev]
        unread = [g for g in self.gaps if case["requested_at"] <= g.captured_at <= audit_at]
        if not res:
            review.append("no record could be linked to the person; deletion not proven")
        if unproven and not findings:
            review.append("checks without evidence: " + ", ".join(sorted(unproven)))
        if unread:
            review.append("unread evidence captured in the request window: "
                          + ", ".join(sorted(g.file_id for g in unread)))
        for op, r in remediation:
            if op in DESTRUCTIVE and r.link in HEURISTIC_LINKS:
                review.append(f"{op} on {r.resource_id} rests on a tied correlation, not evidence; "
                              "confirm ownership before acting")
        not_proven = not res or bool(unproven) or bool(unread)
        status = "INCOMPLETE" if findings or not_proven else "COMPLETE"
        steps = []
        if status == "INCOMPLETE":
            steps += [("FINDING", op, r, ev) for op, r, ev in findings]
            steps += [("REMEDIATION", op, r, "") for op, r in remediation]
            steps += [("VERIFICATION", op, r, "") for op, r, _ in verification]
        else:
            # A completed deletion is proven only by checks that cite evidence.
            steps += [("VERIFICATION", op, r, ev) for op, r, ev in verification if ev]
        return CaseResult(case["case_id"], status, self._serialise(steps), notes, review,
                          {rid: r.link for rid, r in res.items()})

    @staticmethod
    def _serialise(steps) -> list[dict]:
        phase_rank = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
        steps = sorted(steps, key=lambda s: (phase_rank[s[0]], PHASE_ORDER[s[0]].index(s[1]),
                                             s[2].system, s[2].resource_id))
        return [{"step_no": i, "phase": ph, "system_id": r.system, "resource_id": r.resource_id,
                 "operation": op, "state": STATE[op], "evidence_id": ev}
                for i, (ph, op, r, ev) in enumerate(steps, 1)]
