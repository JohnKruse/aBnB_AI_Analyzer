# Implementation status and verification

Updated 6 November 2026. This is the current delivery summary. The
[architecture and remaining work](retooling_plan.md) and
[user guide](user_guide.md) supply design context and operating instructions.

## Implemented

| Area | Current behavior |
|---|---|
| Workflow | One four-step page: search/discovery, budget/priorities, automatic preparation, review/share |
| Search management | Wide scrolling table, visible metadata, explicit multi-delete checkboxes, show/hide panel, no automatic search deletion |
| Search area | Online destination lookup, saved bounds/geometry, explicit area selection, stable live viewport and quarter-level zoom |
| Discovery | Separate fast action and same-search refresh; prior absent IDs retained as not returned |
| Budget | Total-stay histogram and 50-unit slider; initial median rounded upward; independent of trip revision |
| Collection | Selected scope, separate metadata/price calls, bounded newest-first reviews, per-property/page checkpoints |
| Recovery | Three request attempts by default; at most two collection resume rounds; size-dependent wait budget, countdown and cooperative cancellation |
| Shared evidence | Recent property details/reviews and matching quotes survive search deletion and can be reused across searches |
| Priorities | Shared named profiles with save/copy/update/delete; newest-created default for uninitialized selections |
| AI | Strict evidence citations, current-profile fit/coverage, search-owned caches, local reranking for weights, bounded tail-of-queue retries |
| Results | Descending AI Fit by default; unknown evidence remains distinct; presentation filters and manual ordering available |
| Maps | Quiet Esri background; distinct rail/Tube symbols and names at all zooms for saved London stations; optional live stations elsewhere in-app |
| Export | One interactive HTML snapshot with filters, sort, map, evidence, favorites, notes and Save reviewed copy |
| Persistence | Search SQLite stores plus compatibility exports; separate shared evidence database and profile library |
| Styling | Teal primary, blue secondary, red destructive and gray disabled buttons |

Free cancellation and maximum budget are not initial discovery constraints.
Cancellation remains a report/result filter. A zero-review result does not exclude
its property; a failed review request is a separate incomplete-collection state.

The installed pyairbnb high-level details wrapper retrieves every review page.
The adapter bypasses that wrapper and paces separate listing-page, quote and
bounded review calls. A fresh uncached property normally takes three such requests,
with more pages only when needed by the selected review policy. Review API pages
can contain 50 records even when only 25 are retained.

## Verification evidence

The latest full suite run on 6 November 2026, before this documentation refresh,
reported **336 passed**, with 42 dependency deprecation warnings:

```bash
.venv/bin/python -m pytest -q
```

Coverage includes search/deletion lifecycle, shared-cache retention and expiry,
stay-context isolation, missing-review handling, bounded collection and AI retries,
strict citation rejection, saved profile behavior, app flow and result sorting.
Streamlit AppTest runs the Python widget lifecycle against temporary workspaces
and fake providers. Node-based fixtures execute the actual map JavaScript offline.
Map identity regressions check the component hash across pan/zoom/boundary updates.

These tests do not demonstrate real-browser gesture behavior, live provider
coverage, current model access or successful live price queries for every party.
No paid AI call is a unit-test requirement. Live Overpass checks timed out during
development; saved London stations do not depend on that service.

## Runtime

The focused app requirements pin Streamlit 1.38.0, streamlit-folium 0.22.0,
streamlit-aggrid 1.0.5, Folium 0.17.0 and pyairbnb 2.2.2. Development verification
used Python 3.12. `bash launch.sh` starts the project-local runtime. Historical
Conda/desktop manifests remain separate from the focused app requirements.

## Known limits

- Airbnb discovery is a ranked provider result set, not a complete inventory.
  A missing result is not proof of unavailability; earlier deleted search
  snapshots cannot be reconstructed from counts alone.
- Price labels describe quote context, not live booking guarantees. Taxes/fees
  may not be fully specified. Party support differs between provider endpoints.
- Jobs live in the Streamlit process. The computer/app must stay running; a
  process restart requires resuming from saved checkpoints.
- AI calls remain sequential. Invalid answers can incur another paid attempt;
  the default cap is three total calls per property. Auth/billing errors stop the
  batch, and provider-wide rate limits require a shared cooldown.
- AI evidence bundles have character limits, and retrieved/analyzed coverage can
  differ. Unknown findings do not become negative facts.
- Saved London stations may be outdated. Live station lookup elsewhere is
  best-effort; exported HTML includes saved London stations only. Transit route
  lines, routing and timetables are not implemented.
- Public background tiles and Airbnb links need internet. HTML filters, evidence,
  notes/favorites and embedded station/property markers work locally.
- Shared property evidence survives deletion; personal AI runs and curation do not.
  The shared cache is not a complete immutable history of all earlier downloads.
- HTML export is implemented; automatic email sending and hosted sharing are not.

## Recommended manual verification

Use one small trip in the local app to verify Find, pan/zoom, lock/unlock, discovery,
budget changes, automatic preparation, price refresh and retry/cancel behavior.
Check the shown price/source/context against Airbnb. Export HTML, open it locally,
check sorting and stations, add a note/favorite, save a reviewed copy and reopen it.
Run live-provider checks separately from the offline suite.
