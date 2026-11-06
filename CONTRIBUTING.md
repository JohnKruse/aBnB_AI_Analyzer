# Contributing to BnB AI Analyzer

Read the [README](README.md), [architecture](docs/retooling_plan.md) and
[storage lifecycle](docs/sqlite_storage.md) before changing the app. Preserve
existing work in the checkout; avoid mixing unrelated changes into a fix.

## Development environment

Use Python 3.12 and the focused `requirements-app.txt` runtime. `bash launch.sh`
creates/checks the local environment and launches Streamlit. Historical desktop
and notebook dependencies remain in separate manifests.

For the test suite, install pytest into that environment and run:

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

Node.js is needed for the real-JavaScript map lifecycle checks. Those checks skip
when it is unavailable. A skipped check must not be presented as verified browser
behavior. No live Airbnb request, geocoding call or paid model call belongs in an
ordinary unit-test dependency.

## Change design

- Keep Streamlit as a caller of services. Workers must not call Streamlit APIs.
- Use temporary workspaces and injected providers in tests; never modify a user's
  saved searches as a test fixture.
- Keep current quote context, reusable property facts and personal AI evidence
  separate. Reusing a record must not reset its original fetch timestamp.
- Keep per-property success checkpoints and finite retry limits. Shared rate
  limits require cooldowns; invalid evidence citations must never become scores.
- Keep the area map's initialization script stable while a user pans/zooms.
  Use dynamic layers for boundary changes, not viewport-resetting remounts.
- Respect the four-step UI and shared button colors: teal primary, blue secondary,
  red destructive, gray disabled. Keep update actions with their owning stage.
- Preserve the legacy `abnb_*` entry-point names. Existing provider/service names
  use `airbnb`; there is no blanket requirement to rename those APIs or strings.
- Prefer focused functions, meaningful names, type annotations where useful and
  comments that explain non-obvious behavior. Run checks appropriate to the change.

## Source and runtime data

Application code belongs under `src/`; tests under `tests/`; maintained docs under
`docs/`. Search workspaces and shared runtime stores live under `searches/` and
are not source files. The shared property database must not be replaced by an
ancestor `analyzer.sqlite3`.

Never commit `.env` files, credentials, search snapshots, generated reports,
SQLite files, logs or personal notebook outputs. The two bundled London station
CSVs are deliberate exceptions to the broad CSV ignore rule; custom/private
CSV overlays remain local unless deliberately reviewed for publication.

Review `git status` and the proposed diff before committing. Publishing code is
separate from publishing private runtime data.

## Pull requests and documentation

Describe the concrete resulting behavior, its reason, relevant tests and material
limits. Update user-facing controls/examples when names or flow change. Keep
verification dates/counts in the [status document](docs/browser_revamp_status.md)
and distinguish offline test evidence from live-provider/browser checks.

Use a focused commit message, for example:

```text
fix(evaluation): retry rejected answers after the remaining listings
```

The old [feasibility brief](docs/flash_stage0_brief.md) is archived, not a current
implementation task. Do not describe shipped features as future work in it.

## Diagnostics

Application logs go to `logs/bnb_analyzer.log`, with rotated copies. Unexpected
browser exceptions include redacted tracebacks. Background job progress and
errors live under each search's `jobs/` directory. Do not paste credentials or
full personal provider payloads into issue reports.

```bash
tail -f logs/bnb_analyzer.log
```

A useful bug report includes the action, displayed status/error, relevant saved
trip/selection context and whether it reproduces with a small temporary fixture.
Map gesture/render defects need an actual browser check in addition to AppTest.

Contributions use the repository's existing [license](LICENSE).
