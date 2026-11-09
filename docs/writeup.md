# Source-backed record reconciliation - a small working demo for Soria

Built by Karan Kapur, November 2026. All data in this demo is synthetic: three
fictional companies, fictional sources, fictional values. Nothing here touches
real filings, real customers, or Soria's systems.

## What it is

Soria's core loop: turn messy source updates into a continuously updated,
source-backed record. I built a small working version of that loop for a
fictional healthcare research team ("Meridian Research") tracking three
fictional companies.

## How it works

1. A source update arrives - a CMS-style CSV extract, a filing excerpt, a news
   brief, an analyst spreadsheet note (plain-text slice only for the demo).
2. An extractor (Claude) pulls the candidate update out as structured data:
   entity, metric, value, unit, period, publication date, source type, and the
   exact citation (page, row, or paragraph) where the value appears.
3. A deterministic rule pack - written like an internal SOP - decides what
   happens: APPLY (authoritative, newer, consistent), HOLD for analyst review
   (conflicting sources, stale source, unit or definition change, an
   implausibly large jump, a low-authority source trying to override a better
   one), or REJECT (no usable citation, unknown entity).
4. Every value in the record keeps its source and citation. Every decision,
   applied or not, lands in an audit log with its reason; an applied value
   records what it supersedes. A review page shows the record with provenance,
   the analyst review queue, and the decision log.

## Test results (actual runs, Claude doing extraction)

- Unit tests: 15/15 pass (stdlib unittest).
- Dev set (12 synthetic scenarios): 12/12 decisions correct, every extracted
  field correct, 0 wrong auto-applies.
- Frozen holdout (33 synthetic scenarios, same-author): 33/33 decisions
  correct - 14 applied, 14 held, 5 rejected, all with the expected reasons.
  0 wrong auto-applies, 0 extraction failures. The cases were written before
  the implementation was finished and revised once to state periods explicitly
  after dev testing showed two cases were ambiguous; the code was then frozen
  and hashed, and the holdout was run exactly once, kept from the model until
  that run.
- The 19 holds and rejections are the point: the system abstains with a stated
  reason (conflict, staleness, unit change, missing citation, unknown entity,
  low-authority override) instead of writing a bad value into the record.

## Honest caveats

- Both test sets are my own synthetic cases, revised as described above.
  Same-author tests are not an independent benchmark, the cases use clearly
  stated periods and plain text, and performance on messy real inputs is
  untested.
- Not implemented in this slice: PDF parsing, real spreadsheet formats, live
  feeds, broad entity resolution. The audit log is append-only within a run,
  not tamper-proof.

## Run it

    python3 -m unittest -v
    python3 run_holdout.py --corpus corpus-dev-12-v2.json --mode claude --outdir out-dev
    python3 run_demo.py --mode claude --outdir out-demo   # writes review.html
