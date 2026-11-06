# BnB AI Analyzer

A local Streamlit app for discovering Airbnb properties, choosing a price range,
collecting listing details and recent reviews, and ranking properties against
saved personal priorities. Review results in the app or export one interactive
HTML file to share.

Search coverage depends on Airbnb's responses. Prices are quoted stay totals,
not guaranteed checkout amounts, and property pins may be approximate.

## Quick start

Use Python 3.12. From the project directory:

```bash
bash launch.sh
```

The launcher creates a project-local `.venv` if needed, installs missing runtime
dependencies from `requirements-app.txt`, and starts Streamlit. Open the local
URL printed in the terminal.

To choose the interpreter used to create the environment:

```bash
ABNB_PYTHON=/path/to/python3.12 bash launch.sh
```

For an explicit installation:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-app.txt
.venv/bin/python -m streamlit run review_app.py
```

Set `OPENAI_API_KEY` in the environment before launching if you want AI evaluation.
Opening saved results, filtering and exporting do not require a model call.
Coding-agent subscriptions do not supply the application's API credentials.
The configured model must be available to your API account.

## Four-step workflow

| Step | Actions |
|---|---|
| **1. Search area and listings** | Set destination, dates, guests, currency and map area. Discover listings, edit the saved trip, or use **Refresh listings + prices**. |
| **2. Budget and priorities** | Inspect the price histogram, set a maximum total-stay amount, and select or save a named AI priority profile. |
| **3. Prepare selected properties** | Click **Fetch selected + evaluate**. Details, reviews and AI run automatically, with checkpoints, bounded retries and live progress. |
| **4. Review and share** | Export an interactive property report, or open the optional in-app property browser to filter, shortlist and reorder results. |

Discovery is separate from preparation. It retrieves listing results and initial
prices without starting review collection or AI. Free cancellation and maximum
budget are not discovery constraints: the processing budget is chosen in step 2,
and cancellation is a results filter.

The budget slider sits below the histogram and moves in increments of 50 currency
units. Its initial value is the median usable stay price rounded upward to the
next multiple of 50. Green bars show included properties; gray bars show excluded
ones. Unknown prices require an explicit opt-in. Increasing the threshold later
adds work only where reusable evidence or matching assessments are missing.

Profiles such as City, Beach or Enrica are shared between searches. Save, copy by
changing the name, update or delete a profile. A search/session without an explicit
choice starts with the most recently created saved profile. Changing weights can
rerank matching saved findings locally; changing instructions, model or evidence
can require new AI calls.

## Updating an existing search

Open the saved search rather than creating another one:

| Goal | Control |
|---|---|
| Update returned listings, discovery prices and histogram | **Refresh listings + prices** in step 1 |
| Widen or narrow the properties being prepared | Change the budget in step 2, then **Fetch selected + evaluate** in step 3 |
| Recheck selected detailed quotes without running AI | **Update collected prices → Refresh prices + availability** in step 3 |
| Fill failed collection or AI work | Use the available retry action or resume preparation; successful matching work is reused |
| Update the downloaded report | Generate a new HTML export in step 4 |

An exported HTML report is a snapshot; it cannot rerun the collection pipeline.
See the [user guide](docs/user_guide.md) for selection, refresh and troubleshooting
examples.

## Reuse and search deletion

- Recent property details and compatible review samples are shared across searches
  for **30 days**, including after the originating search is deleted.
- Stay quotes can be reused only for matching dates, party and currency. A changed
  stay requires a new quote; recent property metadata and reviews can still be reused.
- Original fetch timestamps are preserved. Copying evidence or fetching a new
  price does not make older property information new again.
- Older shared evidence remains stored but is not eligible for automatic reuse.
- Matching AI assessment caches are search-owned, not part of the shared property
  cache. Deleting a search removes its AI runs and local assessment cache.

The left panel has a scrolling search table with location, stay dates, party size,
property count and last-updated time. Other cells open the search; only the labeled
**Delete** checkboxes mark searches for deletion. **Delete checked (N)** requires
confirmation and is blocked while a selected search has active work.

Deletion drops the search's workspace/database and settings after preserving its
property evidence in the shared cache. Searches are never automatically deleted.
Use **Hide searches / Show searches** to close or reopen the panel.

## Reviews, AI and recovery

The default review sample keeps the **25 most recent reviews within two years**.
The run options allow a different window/cap. The provider returns pages of up to
50 reviews, so keeping 25 may still require fetching one 50-review page. Original
review dates are retained, and analysis reports coverage and truncation.

The collection adapter bypasses pyairbnb's high-level `get_details()` wrapper,
which internally downloads all reviews and other auxiliary data. Listing pages,
trip quotes and bounded review pages are called separately, with per-request
pacing. An initial uncached property normally needs a listing page, a price
request and a review page; cached evidence can reduce that work.

Temporary collection errors receive up to three attempts per request. Collection
can then resume remaining transient failures at most twice, after 5- and 10-minute
cooldowns, or longer when required by `Retry-After`. Rate limits slow subsequent
requests. Retry waiting has a size-dependent budget: one minute per selected
property, with a 20-minute minimum and 60-minute maximum. Normal processing and
ordinary pacing are separate from that waiting budget.

AI calls remain sequential. A temporary failure or an invalid AI answer moves the
property to the end of the queue, with at most three total attempts per property
by default. Answers must cite exact supplied source IDs and supporting excerpts;
invalid answers are not saved as valid scores. Authentication, access, billing
quota or checkpoint-writing errors stop the batch explicitly. Account-wide rate
limits pause the queue rather than issuing more requests immediately.

Active cooldowns show a live countdown. A stopped run does not promise an automatic
retry. Each successful property is checkpointed; cancellation is cooperative.
The browser tab may close, but the Streamlit process and computer must remain
running. After a process interruption, reopen the search and resume. Restart
Streamlit when idle to load Python code updates.

## Results and shared reports

The app and newly exported reports default to **AI Fit, highest first**. Unrated,
stale and insufficient-evidence assessments sort after current scored properties.
The in-app list initially shows Top 20; select **All** to inspect the full matching
set. Other sorts and manual ordering remain available in the app.

A lack of reviews does not eliminate a property. A successful empty review result
is complete collection; AI can use listing evidence and mark unsupported criteria
unknown. A failed review download remains a collection failure and is excluded
from the automatic AI subset until resolved.

The HTML report contains its joined data, filters, sortable grid, property details,
map markers, trip context and saved profile metadata. Recipients can mark favorites
and add notes. **Save reviewed copy** downloads a new file preserving their edits,
filters and sort. Closing without saving does not retain those edits. Favorites
belong to the report/app, not the Airbnb account.

Street tiles and Airbnb links need internet. Embedded property evidence and saved
London station markers work offline. No automatic email sending or hosted sharing
service is included.

## Maps and data meaning

Search and results maps use an Esri Light Gray background. Saved London rail and
Tube/DLR stations appear at every zoom level, with names and distinct blue train
and red roundel-style icons. These bundled station files may omit recent changes.
The in-app maps can query OpenStreetMap/Overpass for stations elsewhere when
zoomed in; availability is not guaranteed. Exported reports embed the saved London
stations and do not query Overpass. Route lines and transit travel-time routing
are not implemented.

Panning does not run discovery. Choose and save the search area explicitly.
**Find** attempts an online destination lookup and intentionally recenters the
map; ordinary panning/zooming retains the live viewport through reruns. Interactive
maps support quarter-level zoom and reduced wheel sensitivity. Exported map +/−
controls also use quarter-level steps.

Only positive, usable stay totals are treated as current prices. Missing, failed,
unverified and stale quotes remain distinct; nightly rates are not silently used
as total-stay amounts. A current label means the quote matches the saved context,
not that its value is live or that every tax/fee is confirmed. Previously seen
properties absent from a refreshed discovery are retained as **not returned**;
absence alone is not evidence of unavailability.

See [map overlays](overlays/README.md) for saved station data and custom CSV layers.

## Storage, development and documentation

Search-owned records live in `searches/<search-name>/analyzer.sqlite3`, with
CSV/JSON compatibility exports. Shared property evidence lives separately in
`searches/.property_cache.sqlite3`; named priority profiles live in
`searches/.priority_profiles.json`. Stop the app before backing up the complete
`searches/` directory, including those shared files. Logs, databases, generated
search data and credentials are excluded from version control.

`review_app.py` is the browser entry point; the older `abnb_launcher.py`,
`abnb_launcher_ui.py` and `abnb_monitor.py` remain for compatibility.
`requirements-app.txt` is the focused runtime set. The historical requirements and
Conda manifests also include desktop/notebook packages.

- [User guide and troubleshooting](docs/user_guide.md)
- [Implementation status and verification limits](docs/browser_revamp_status.md)
- [SQLite storage and shared cache](docs/sqlite_storage.md)
- [Architecture and remaining work](docs/retooling_plan.md)
- [Contributor guide](CONTRIBUTING.md)

For offline development checks, install pytest in the environment and run:

```bash
.venv/bin/python -m pytest -q
```

Tests use temporary workspaces and fake providers. The JavaScript map checks also
require Node.js; they are skipped when Node is unavailable. Test success does not
prove that live Airbnb endpoints or a configured model are currently available.
