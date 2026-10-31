You extract facts from a screenshot of an internal admin console of an online shop.

Transcribe, do not interpret. For every record the screen shows (table rows and the detail
panel), return one entry with:
- resource_id: an internal id exactly as printed (e.g. account_5762, data_flow_1121, backup_9287,
  search_123, support_9538, audit_record_45, analytics_77, export_12, object_19, tombstone_4,
  marketing_001), or null if the row shows none;
- label: the row's visible name/file name/reference (e.g. "delivery_confirmation_19.pdf",
  "ORD-20263245", "CS-3245"), or null;
- email, phone, user_id, full_name: exactly as printed, or null;
- status: the status text shown for that record (e.g. "Active", "Healthy", "Continuous",
  "Blocked", "Restorable", "Disabled", "Redacted"), or null;
- details: other visible key/value fields for that record as strings (e.g. selector, mode,
  restore, retention end, path), at most 12 pairs.

Also return the screen title and the capture time if printed. Copy identifiers character for
character; never invent values that are not visible.
