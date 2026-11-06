# User guide

Current workflow: 6 November 2026. Start with the [README](../README.md) for setup.

## Create a search

1. Click **＋ New Search** in the left panel.
2. Enter a destination and use **Find** to locate it. If lookup fails, pan the map
   manually. Set check-in/out, guests and currency.
3. Frame the desired area and click **Use Map Area**. Unlock it to revise the
   boundary. Map movement alone does not contact Airbnb.
4. Click **Discover listings**. The app saves the search and runs discovery.

The saved-search page has one numbered sequence. Details/reviews and AI are
subtasks of preparation, not another competing numbered sequence.

## 1. Search area and listings

Open **Trip dates, party, and search area** to edit the saved trip. Saving changed
trip settings creates a revision; old quote contexts are not relabeled as new.
The discovery status shows what has been returned for the saved area.

**Refresh listings + prices** reruns discovery, updates initial prices and the
histogram, and retains previously seen listings that did not return this time.
It does not run detailed collection or AI.

Same place name does not establish the same search. Compare map boundary, dates,
guests and currency before interpreting differences in the result set.

## 2. Budget and priorities

The histogram uses usable total-stay quotes for the saved context. Drag the slider
below it to choose a maximum, in increments of 50. The first value is the median
rounded upward to the next 50. Select **Process all prices** for no upper threshold.
Properties with unknown prices are included only when explicitly selected.

The selected count is a processing scope, not a display limit. A later budget
increase can add properties without discarding work already collected.

Choose a named profile under **AI evaluation priorities**, or create/save one.
Weights run from 0 (ignored) to 5 (highest importance). Additional instructions
provide context for the findings. The most recently created shared profile is
used when the current search/session has no explicit profile choice. Renaming
and saving creates a copy; saving the same name updates it.

## 3. Prepare selected properties

**Fetch selected + evaluate** saves the threshold and priorities, collects missing
or expired details/reviews, then evaluates the eligible selection automatically.
Successful work is checkpointed and eligible cached work is reused.

The status cards separate details/review coverage from AI coverage. An assessment
can be valid yet have insufficient evidence for a numeric score. Attempted,
failed, missing and successful are different counts; none should be read as an
independent new discovery count.

Temporary AI failures and invalid answers move to the back of the queue. Up to
three attempts are allowed per property by default. A citation error such as
“unknown source ID” means the answer was rejected, not that the property was
removed. Valid answers for other properties remain saved.

A rate limit may apply to the provider/account rather than a single listing.
The countdown shows when the queue may continue. A terminal run explicitly says
when no automatic retry is scheduled. Use **Refresh pipeline status** if needed;
that button reads status and does not start another collection run.

Use **Cancel active job** to request cancellation. Let the in-flight request finish.
Restarting Streamlit stops its in-process workers; resume from checkpoints afterward.

### Update prices without AI

Open **Update collected prices**, then click **Refresh prices + availability**.
This uses the saved processing selection and rechecks detailed stay quotes without
running AI. Recent property information/reviews are reused; missing review work
can be collected with the bounded policy. Save a selection by starting preparation
before using this control.

To change the candidate listing set or refresh the histogram, return to step 1.
To process more expensive properties, change step 2 and start preparation again.

## 4. Review and share

Export all properties, the top 30 by AI fit, or your shortlist. Hidden properties
are excluded. Open the downloaded HTML directly in a browser or attach it to an
email yourself. The report is a snapshot; refreshes happen in Streamlit, followed
by a new export.

The optional in-app browser exposes presentation filters, a linked property map,
and a detail pane. Both app results and new reports initially sort by descending
AI Fit. Properties without a current numeric score are placed after scored ones.
The app initially displays Top 20; choose **All** to inspect all matching rows.
Manual ordering and move controls are available in the app.

A star marks a favorite/shortlist entry. It does not change the AI score or the
Airbnb account. In the HTML, **Favorites only** narrows the view; **Save reviewed
copy** preserves favorites, notes, filter settings and sorting in a new file.
Save before closing if you want to retain those changes.

## Why a property may appear to disappear

Check these separately:

- **Discovery:** Airbnb may return a different ranked set, or the boundary/dates/
  guests may differ. “Not returned” does not mean confirmed unavailable.
- **Processing:** a price threshold excludes preparation outside the chosen budget.
- **Display:** unknown/stale prices, home type, amenities, cancellation, shortlist,
  hidden state and the Top N limit can narrow what is shown.
- **Ranking:** newly scored properties can displace unrated ones from Top 20/30.

Choose All, include unknown prices, clear restrictive filters and inspect hidden
properties before concluding that saved records were deleted. An exported report
cannot reveal properties that were not included in that export.

Zero reviews is not an exclusion rule. A successful response containing zero
reviews counts as collection success. A failed review request is different: the
property remains saved but does not join the successful automatic AI subset yet.

## Price labels

| Label | Meaning |
|---|---|
| Current | A positive usable total matches the saved trip context; it is still a snapshot. |
| Stale | The old quote is not applicable to the current revision, or the listing did not return in the latest discovery. |
| Unverified | An amount exists, but the app cannot establish all required context/support. |
| Missing / failed | No usable total was obtained, or the price/detail request failed. |
| Unavailable | The provider explicitly reported unavailability for the relevant stay. |

The 30-day cache window controls reuse; it is not simply the definition of the
stale label. Tax/fee completeness may be unknown even for a current quoted total.

## Maps

The background is deliberately quiet so property and station markers stand out.
Train stations have blue train pictograms; Tube/Metro stations have red roundel-
style symbols. Saved London rail and Tube/DLR station names remain visible at all
zoom levels. The data is bundled and may miss recent station/network changes.
Use the layer menu to hide stations in the in-app maps. Non-London live station
loading needs internet and can fail independently of property search.

Quarter-level zoom reduces the size of each adjustment. Ordinary map gestures
retain the viewport; Find deliberately resets it to a selected destination.
Property locations come from Airbnb and are not independently verified addresses.
Station markers do not provide route lines, timetables or walking-time estimates.

## Delete, retain and back up

In the search table, check the **Delete** column and choose **Delete checked (N)**,
then confirm. Clicking other cells only opens a search. Active jobs block deletion.
No automatic deletion runs when a trip passes.

The search database/settings and its AI runs are removed. Property details,
compatible review snapshots and context-specific quotes are retained separately.
Fresh shared records can be reused for 30 days; older evidence remains stored but
is not automatically reused. Copying records does not reset their age.

Stop Streamlit and back up the entire `searches/` directory, including hidden
shared-cache/profile files, rather than only one search folder. See the
[storage guide](sqlite_storage.md) for the exact layout.
