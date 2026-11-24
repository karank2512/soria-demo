# Meridian Research - Source Reconciliation SOP (SYNTHETIC)

Fictional internal SOP for a fictional research team. All entities, metrics and values in this
pack and the companion corpora are invented for demonstration. No real company data.

This SOP is the deterministic rule layer of the demo: the extractor proposes candidate updates
with citations; these rules decide APPLY / HOLD / REJECT mechanically. Every decision is
written to an append-only audit log with its reason code.

## Authority ranks (1 = highest)

1. government_data      (e.g. CMS-style datasets)
2. regulatory_filing    (e.g. NAIC-style filings)
3. company_filing       (company-issued reports)
4. analyst_spreadsheet  (internal analyst updates)
5. news                 (press / news items)

## Record state

Each tracked (entity, metric) in the record holds: current_value, as_of date, and the
authority rank of the source that set it. A metric may be untracked (no current value).

## Decision procedure (apply in order; first match wins)

0. invalid_candidate - a required field is missing or has the wrong type (value must be a
   finite number; citation.present must be true/false; text fields non-empty strings)
   -> REJECT. Malformed candidates never count as batch peers in rule 5.
1. missing_citation - citation absent, or locator vague (e.g. "a recent filing", "an article")
   -> REJECT.
1b. value_not_in_source - only when the source document text is available: the claimed value
   does not appear as a number in the document -> HOLD. (Checks the value is stated in the
   document; it does not prove it sits at the cited locator.)
   source_not_found - source documents were supplied but none matches the update's source_id
   -> HOLD (the value cannot be verified).
   invalid_record_value - the record's current value for the metric is not a finite number
   -> HOLD, so a corrupt record value is never silently compared or replaced.
2. stale_source - update pub_date is on or before the record's as_of date -> HOLD.
3. unit_or_definition_change - update notes a unit, definition or methodology change
   -> HOLD.
4. large_delta - metric is tracked AND |claimed - current| / |current| > 0.25 -> HOLD.
5. conflicting_sources - another update in the same batch targets the same entity+metric and
   the claimed values differ by more than 2% (relative to the lower value) -> HOLD (both
   updates). If they agree within 2%, this is not a conflict: APPLY the update with the better
   authority rank (tie: later pub_date, then earlier in the batch) and REJECT the other as
   superseded_duplicate. When source documents are supplied, only updates whose value appears
   in their own document count as peers here.
6. low_authority_override - metric is tracked, update's authority rank is worse than the
   current source's rank, and value differs by more than 2% -> HOLD. If it differs by 2% or
   less -> REJECT (redundant_confirmation: value already current from a better source).
7. Otherwise -> APPLY. Reason new_coverage when the metric was untracked, else
   authoritative_newer_consistent.

## Batches

Batches are processed in input order. Within a batch every update is decided against the record
as it stood at the start of that batch; a batch's applies are written before the next batch is
decided.

## Analyst resolution

A HOLD stays open until an analyst resolves it (`workspace.py resolve`): ACCEPT writes the held
value into the record (with its citation, recording what it supersedes); DISMISS closes the hold
and leaves the record unchanged. Both are logged with the reviewer's name and note. If the
record value for that metric changed after the hold was raised (another hold accepted, or a
later batch applied), ACCEPT is refused unless the analyst passes --force; a forced accept is
marked in the log.

## Decisions and flags

- decision is mutually exclusive: exactly one of apply / hold / reject.
- reason_code is the single rule that fired.
- flags are overlapping annotations, e.g. low_authority on a news-sourced new_coverage apply,
  or low_authority_override alongside large_delta when both conditions hold.

## Never does

- Never drops the citation from an applied value; every record value keeps its source.
- Never silently overwrites: rejected and held updates are logged with their reason.
- Never treats a news item as authority over a tracked, better-sourced value.
- Never invents a value; only values present in a source update can be proposed.
