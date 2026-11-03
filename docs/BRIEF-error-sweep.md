# Brief: Error sweep (factory feature, build step 3 of 6)

## Goal
Every morning, read the new or busy production errors from Sentry and SigNoz and file
each one, once, as an `intake` issue in the right product repo, with enough context
(stack frames, counts, link) for the owner to Promote or Close it from the Board.

Decided (DECISIONS.md "Automation" + owner answers 2026-11-02):
- Plain Python, no LLM in the run. Sentry is read with a read-only Sentry auth token,
  SigNoz with a Viewer-role API key. Composio is not used by the sweep.
- Issues are filed with label `intake` plus `source:sentry` (Sentry) or `source:signoz`
  (SigNoz). The Board learns `source:signoz` with the text "SigNoz" (item 8).
- Never the `factory` label (that is the owner's approval).

## Where
Repo `~/github/factory-wt-sweep` (git worktree, branch `factory/feat/error-sweep`,
already checked out). Read first: `README.md`, `docs/DATA.md`, `factory/board.py`
(`_ensure_label`, how `gh` is called, `SOURCE_LABELS`), `factory/sources.py`
(`run`, `gh_json`), `factory/cli.py` (command style, `--json`, help text),
`factory/store.py` (`read`/`write`/`lock`, FactoryError), `factory/autopilot.py`
(a scheduled plain-Python job with history), `deploy/crontab.txt`,
`tests/test_board.py` and `tests/test_autopilot.py` (test style).

## What to build
1. **Secrets** (`factory/secrets.py` + CLI `factory secret set <name>` reading the
   value from stdin, `factory secret list` printing names only, never values):
   files `$FACTORY_HOME/secrets/<name>` with mode 0600 (dir 0700). Names used here:
   `sentry-token`, `signoz-key`. A missing secret is a clear FactoryError naming the
   `factory secret set` fix. Never log, print or put a secret in an exception message.
2. **Config** `$FACTORY_HOME/sweep.json` (shape-checked like `store.autopilot()`):
   ```json
   {"sentry": {"base_url": "https://de.sentry.io", "org": "<slug>",
               "projects": {"<project slug>": "<owner/repo>"}},
    "signoz": {"base_url": "https://<host>", "services": {"<service name>": "<owner/repo>"},
               "default_repo": "<owner/repo or null>"},
    "min_events": 5, "max_per_repo": 5, "include_performance": false,
    "history": [{"at", "source", "swept", "filed": [...], "skipped": {...}, "error"}]}
   ```
   CLI: `factory sweep map sentry <project> <repo>`, `factory sweep map signoz
   <service> <repo>`, `factory sweep config set <key> <value>` for base_url/org/
   min_events/max_per_repo/default_repo. Every repo must be one of the products' repos
   (same check as the Board). History keeps the last 30 runs.
3. **Sentry source** (`factory/sweep.py`, stdlib `urllib` only):
   `GET {base_url}/api/0/projects/{org}/{project}/issues/?query=is:unresolved%20lastSeen:-24h&statsPeriod=24h`
   with `Authorization: Bearer <token>`; follow the `Link` header `rel="next"; results="true"`
   cursor, max 5 pages. Keep an issue when `issueCategory == "error"` (performance issues
   only when `include_performance`) AND (events in the last 24 h, the sum of
   `stats["24h"]` buckets, >= `min_events` OR `firstSeen` within the last 24 h OR
   `substatus == "regressed"`). For each kept issue fetch the latest event
   `GET {base_url}/api/0/organizations/{org}/issues/{id}/events/latest/` and take up to
   8 in-app stack frames (filename, function, lineNo) from the exception entry.
4. **SigNoz source**: `POST {base_url}/api/v1/listErrors` with JSON
   `{"start": <ns>, "end": <ns>, "order": "descending", "orderParam": "exceptionCount",
   "limit": 50, "offset": 0}` and header `SIGNOZ-API-KEY: <key>`. Each group has
   `exceptionType`, `exceptionMessage`, `exceptionCount`, `firstSeen`, `lastSeen`,
   `serviceName`, `groupID`. Keep groups with count >= `min_events` or firstSeen in the
   last 24 h. Details: `GET {base_url}/api/v1/errorFromGroupID?groupID=..&timestamp=<ns>`
   for `exceptionStacktrace` (take the first 8 frames' lines). Repo = `services[serviceName]`
   or `default_repo`; no repo = skipped ("no repo mapped for service X"). Check these two
   endpoints against the SigNoz source/docs for the current version
   (`GET {base_url}/api/v1/version` answers without auth) and adapt field names if they
   differ; say what you found in the reply. The Sentry and SigNoz parts must be
   independent: one failing never stops the other.
5. **Filing** (`gh`, argv lists, no shell): dedupe by a marker line in the issue body,
   `<!-- factory-sweep: sentry:<shortId> -->` or `signoz:<groupID>`, searched with
   `gh api search/issues` `q="factory-sweep: sentry:<shortId>" in:body repo:<repo> is:issue`
   (open or closed). Open match: skip ("already filed #N"). Closed match: file a new
   issue that says "Regression of #N". Ensure labels `intake` and `source:sentry` /
   `source:signoz` exist (reuse the Board's `_ensure_label`). Title: `[Sentry] <title>` /
   `[SigNoz] <exceptionType>: <message first 80 chars>`. Body: counts (24 h and total),
   users, first/last seen, culprit/service, the frames as a code block, the link
   (Sentry permalink; SigNoz `{base_url}/error-detail?groupId=<id>`), and the marker.
   At most `max_per_repo` new issues per repo per run, biggest 24 h count first; the
   rest are listed in the run's `skipped` as "cap".
6. **Run** `factory sweep run [--source sentry|signoz|all] [--dry-run] [--json]`:
   dry-run prints what it would file and files nothing. One output line per decision.
   Record the run in history (not on dry-run). Cron: add
   `0 7 * * * <same pattern as the build line> factory sweep run >> $HOME/.factory/sweep.log 2>&1 # factory-sweep`
   to `deploy/crontab.txt`. Do not install it.
7. **Doctor**: a line per source: secret present (name only), and a cheap auth check
   (`GET {sentry}/api/0/organizations/{org}/` 200; SigNoz `listErrors` with limit 1).
8. **Board**: add `source:signoz` -> "SigNoz" to the source map in `factory/board.py` and
   the template's source filter.
9. **README** + `docs/DATA.md`: secrets, sweep.json, commands, cron line.

## Ruled out
- No LLM, no Composio, no auto-promote (the triage agent is a later step), no
  resolving or commenting in Sentry/SigNoz.
- Do not touch `~/factory-live`, `~/.factory`, port 3045, the live crontab. Temp
  `FACTORY_HOME` for every run and test. Mock HTTP and `gh` in tests; never call
  Sentry, SigNoz or GitHub for real.
- No owner-specific values (names, paths, domains, org slugs) in code, help text,
  tests or comments: the repo is open source. Use example.com / `acme` in tests.

## Prove it works
`python3 -m pytest -q` passes; new `tests/test_sweep.py` covers: secret file modes and
that no command prints a secret; config validation (unknown repo refused); Sentry
filter rules (performance skipped, min_events, new, regressed); pagination stops;
SigNoz mapping and default_repo; dedupe (open match skipped, closed match = regression
issue); per-repo cap ordering; exact `gh` argv for create; one source failing does not
stop the other; dry-run files nothing and records nothing; history kept to 30.

## Return
Commit on the branch with the owner's configured git email; do not push or open a PR.
Reply with: commit SHA, files changed, test count, what you found about the SigNoz
endpoints, and anything you did differently from this brief, with why.
