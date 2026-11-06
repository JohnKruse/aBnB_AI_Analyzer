# Architecture and remaining work

Updated 6 November 2026. This document replaces the initial browser-revamp plan
with the implemented boundaries and current backlog. It is not a list of pending
features that have already shipped. See [implementation status](browser_revamp_status.md)
for verification and [the user guide](user_guide.md) for the UI.

## Product contract

After launching the local app, ordinary searches should not require terminal
preprocessing, copied Airbnb URL bounds or YAML edits. One four-step flow owns:

1. Trip definition, area selection, discovery and discovery refresh.
2. Processing-budget selection and named evaluation priorities.
3. Automatic details/reviews/AI preparation, progress, recovery and quote updates.
4. Local review/curation and portable HTML export.

Refresh actions belong with the work they refresh. Processing scope is separate
from display filters and Top N limits. Existing evidence is retained when the
budget changes or a property drops out of discovery. Search deletion removes its
own tables/settings while preserving shared property evidence.

## Service boundaries

| Module | Responsibility |
|---|---|
| `review_app.py` | Browser entry point and retained legacy helpers |
| `src/ui/browser_app.py` | Widget state, four-step layout, service calls, progress fragments and export controls |
| `src/ui/map_view.py`, `src/ui/assets/` | Area/results maps, stable viewport, fractional zoom and station overlays |
| `search_service.py` | Validated trip definitions, geometry, revisions and destination lookup |
| `search_catalog.py` | Search summaries, lifecycle locks and batch deletion |
| `airbnb_service.py` | Provider compatibility, discovery, scoped collection and quote interpretation |
| `processing_service.py` | Saved budget selection, current scope and freshness checks |
| `review_collection.py` | Newest-first bounded samples and review-page checkpoints |
| `request_policy.py` | Pacing, retry classification, cooldowns, cancellation and waiting budget |
| `job_service.py` | In-process workers, writer locks, persisted manifests and bounded collection resumes |
| `property_cache.py` | Cross-search metadata, review and stay-quote reuse independent of search deletion |
| `priority_profiles.py` | Atomic shared named-profile library |
| `evaluation_service.py` | Evidence bundles, validation, fingerprints, immutable runs, reranking and AI retry queue |
| `feature_extractor.py` | Evidence-backed property facts and unknown/conflict states |
| `curation_service.py` | Manual order, shortlist and hide/restore |
| `storage_service.py` | Search SQLite records, migration, compatibility exports and shortlist snapshots |
| `transit_service.py` | Saved London station data and shared rail/Tube symbols |
| `report_service.py`, `report_assets/` | Self-contained HTML generation and recipient-side behavior |

## Data and execution invariants

- Trip dates, party and currency determine quote context. Never copy an old amount
  into a new stay and label it current without checking that context.
- Prices must be explicit positive stay totals. Do not promote nightly values,
  invalid placeholders or unsupported quote contexts to verified stay prices.
- Property metadata/reviews can be reused for 30 days. New price requests and
  cross-search imports must preserve original property/review fetch timestamps.
- Shared evidence must not be stored through an ancestor search database. Shared
  API bootstrap keys/cookies must remain ephemeral.
- Do not call pyairbnb's unbounded public details wrapper for the actual provider.
  Pace separate metadata/price/review requests and checkpoint successful work.
- Review-free is different from review-fetch-failed. Unknown evidence is different
  from absence, and an unscored valid AI assessment is different from an API failure.
- AI scores require validated source IDs and excerpts. Failed answers cannot
  replace valid saved assessments. Retrying must be bounded and must not repeat
  successful properties.
- Status polling must not start network collection. Page reruns must not remount
  the area map after every reported center/zoom change.
- Search-cell selection cannot imply deletion. Batch deletion preflights active
  writers and preserves shared property evidence before dropping search workspaces.
- A report is an explicit data snapshot with offline controls; it never runs AI or
  refreshes live Airbnb data while a recipient changes a filter.

## Verification gates

Use temporary search roots and fake/injected providers for ordinary tests. Cover
context changes, cache expiry, deletion/reuse, cancellation and exhausted retries,
not just the successful path. Streamlit AppTest checks widget transitions; the
Node fixtures check actual export/station JavaScript. Neither replaces live
browser gesture and provider contract checks.

Current checks and known limits are recorded in the status document. Do not keep
old performance numbers or test counts here as if they apply to a later revision.
Do not use a debugging notebook with saved outputs as a release artifact.

## Remaining work

| Priority | Candidate improvement | Boundary |
|---|---|---|
| High | Explain result changes between discovery runs | Preserve comparable membership snapshots and show added/not-returned/price-change reasons; do not infer bookings from absence |
| High | Verify provider-supported party/price behavior live | Remain explicit about children/infants/pets and tax/fee uncertainty |
| Medium | Better station-data maintenance and coverage | Refresh saved London data and consider cached station/route data for other cities; avoid fragile live-tile dependencies |
| Medium | Limited AI concurrency | Start with a small bound only after shared cooldown/budget/status state is safe under concurrent requests; preserve single-writer checkpoints |
| Medium | More complete review analysis | Consider additional bounded samples/chunk aggregation without implying the default 25-review sample is exhaustive |
| Later | Cross-search personal assessment reuse | Require matching evidence, model, instructions and profile fingerprints; the current shared cache is property evidence only |
| Later | Hosted sharing and email workflows | Build on existing HTML exports; sending remains an explicit separate action |

These are future candidates, not automatic background tasks or commitments to
run more collection/model requests.
