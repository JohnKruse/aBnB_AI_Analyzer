# Storage and cache lifecycle

Current implementation: 6 November 2026. This guide describes what is durable,
what survives deletion, and which records may be reused.

## Layout

| Location under `searches/` | Ownership and purpose |
|---|---|
| `<name>/analyzer.sqlite3` | Search-owned snapshots, trip context, evaluations and curation |
| `<name>/output_data/` | Compatibility exports and search-owned evaluation/review checkpoints |
| `<name>/jobs/` | Live job manifests and cancellation/worker state |
| `<name>/config.yaml` | Search configuration compatible with legacy tools |
| `.property_cache.sqlite3` | Shared property metadata, review snapshots and stay-specific quotes |
| `.priority_profiles.json` | Shared named priority profiles |
| `.lifecycle/` | Search lifecycle locks |

The shared property cache is a separate database, not an ancestor workspace
`analyzer.sqlite3`. Do not create a search-storage database at the `searches/`
root: resource resolution would risk treating new searches as one workspace.

## Search-owned database

`src/storage_service.py` uses built-in SQLite with WAL, foreign keys, versioned
schemas and bounded lock waits. Its records include:

- `resources`, `frame_rows`, `json_documents`: typed snapshot/document payloads and
  per-resource revisions.
- `search_context`: saved trip settings.
- `quotes`: indexed amounts and their date/guest/currency context.
- `curation_items`: manual order, hidden state and shortlist membership.
- `shortlist_snapshots`, `shortlist_snapshot_rows`: fixed copied rows in a specified
  order. Current results do not mutate a saved shortlist snapshot.

Filtering runs against a prepared table in session memory. The prepared-table
cache checks resource revisions and trip/profile fingerprints; status projections
avoid repeatedly loading entire review bodies. Live job manifests are read live.

CSV/JSON paths identify database resources. Reading an existing legacy export
imports it without rewriting that source file. Later application writes commit to
SQLite and normally refresh atomic compatibility exports. An export-write failure
leaves the committed database copy available and emits a warning. Removing an
export does not remove its database resource. Deliberately edited exports are
imported on a subsequent read; malformed changes are reported.

`migrate_workspace(workspace)` imports supported files without provider calls.
`export_workspace(workspace, targetdir)` regenerates compatibility exports.
YAML, operational manifests/locks and application logs remain files.

## Shared property evidence

`src/property_cache.py` manages `.property_cache.sqlite3` directly. Its `evidence`
table separates three kinds of records:

| Kind | Key | Reuse conditions |
|---|---|---|
| Metadata | Property ID | Original property-information fetch is within 30 days |
| Quote | Property ID and dates/party/currency | Both applicable information and quote are recent, and the stay context matches |
| Reviews | Property ID and sampling-policy variant | Review fetch is within 30 days and its sample can satisfy the requested policy |

The `imports` table records which existing workspaces have been backfilled and
changed. Existing search records are imported lazily; deletion also imports the
selected workspace before removal. New collection checkpoints update the shared
cache. Locking and transactions protect writes; failed/unavailable attempts are
not usable cached quotes.

The shared cache retains the latest qualifying record per key, not an immutable
copy of every download. Older evidence is retained, but `load` excludes evidence
outside the 30-day reuse window. Copying a record preserves its original timestamp.
A successful new quote does not renew the age of cached property metadata.

General metadata excludes old stay prices, availability, quote-specific
cancellation/booking information and personal AI findings. On a changed stay,
recent metadata and reviews may be reused while a new price is requested. Public
bootstrap keys and cookies stay in memory and are not persisted in the shared
cache. The cache is not an authentication-token store.

## Trip and selection changes

Trip changes create a revision. A quote is not current merely because its property
ID exists: dates, party, currency and provider support must match. Same-stay quotes
copied into a new search are rebound to its revision only after matching the shared
context key. Discovery can supply a newer usable quote without rewriting the AI's
older evidence snapshot.

The processing threshold is stored separately in `processing_selection.json`.
Changing it does not revise the trip or delete collected evidence. Listings absent
from a refresh remain stored as `not_returned`; they are not automatically treated
as unavailable. A refresh's candidate scope is separate from history retention.

## Profiles and AI records

Shared named profiles use an atomic JSON library managed by
`src/priority_profiles.py`. Each search also keeps the local profile snapshot
needed to reproduce an assessment. Editing a profile retains its creation time;
the default for an uninitialized choice is the most recently created profile.

Assessment caches, immutable runs, failure snapshots and current-evaluation
sidecars are search-owned. Reuse checks source and assessment fingerprints;
weight-only changes can rerank saved findings. The shared property cache does not
retain personal AI assessments when a search is deleted.

## Deletion and backup

Batch deletion preflights every selected workspace and refuses active writers.
It preserves property evidence in the shared cache before removing the selected
workspaces. Search databases, exports, settings, curation and local AI runs are
then removed. Shared property evidence and the named profile library remain.
There is no automatic deletion of expired trips or old shared evidence.

For a backup or move to another machine, stop Streamlit and copy the complete
`searches/` directory, including shared hidden files. Copying only a single search
folder omits cross-search evidence and shared named profiles. All runtime data,
SQLite files and secrets are excluded from normal Git publication.
