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

1. missing_citation - citation absent, or locator vague (e.g. "a recent filing", "an article")
   -> REJECT.
2. stale_source - update pub_date is on or before the record's as_of date -> HOLD.
3. unit_or_definition_change - update notes a unit, definition or methodology change
   -> HOLD.
4. large_delta - metric is tracked AND |claimed - current| / |current| > 0.25 -> HOLD.
5. conflicting_sources - another update in the same batch targets the same entity+metric and
   the claimed values differ by more than 2% (relative to the lower value) -> HOLD (both
   updates). If they agree within 2%, this is not a conflict: APPLY the update with the better
   authority rank (tie: later pub_date) and REJECT the other as superseded_duplicate.
6. low_authority_override - metric is tracked, update's authority rank is worse than the
   current source's rank, and value differs by more than 2% -> HOLD. If it differs by 2% or
   less -> REJECT (redundant_confirmation: value already current from a better source).
7. Otherwise -> APPLY. Reason new_coverage when the metric was untracked, else
   authoritative_newer_consistent.

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
