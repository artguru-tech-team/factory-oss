# Brief: Board (factory feature, build step 4 of 6)

## Goal
One board across every product's repos that shows each factory issue's stage, so
the owner promotes intake issues, closes junk, and sees what is building, in review,
waiting on them, or merged today, without opening GitHub repo by repo.

Validated prototype: `~/factory-pages/proto-automation/factory-automation.html`,
tab "Board" (screenshots `ds-board-*.png`, `ds-card-drawer-*.png` beside it).
Match its filter row, columns, cards, card drawer, states (loading, empty,
error "GitHub unreachable" with last good time and Retry) and wording, in the live
design system (`template/factory.html` classes, no new colours). The Automation view
already exists (Autopilot tab, PR #36): add **Board** as its first sub-tab with the
same `.seg` tabs the prototype uses; Autopilot stays as the second tab. Do NOT build
the "Automations" tab (later step).

## Where
Repo `~/github/factory-wt-board` (git worktree, branch `factory/feat/board`, already
checked out). Read first: `README.md`, `docs/DATA.md`, `factory/sources.py`
(`gh_json`, how PRs/issues are read today), `factory/store.py` (products have
`repos`), `factory/build.py` (`collect`, `autopilot_data` as the pattern for a
view's data + warning + error), `factory/serve.py` (`save_autopilot` for the POST
pattern and gate), `template/factory.html` (Automation view, `viewAutomation`, the
Prototypes drawer, `toast`, `.seg`), `tests/test_autopilot.py` (test style),
`~/factory-pages/DECISIONS.md` section "Automation".

## Decisions already made (do not re-decide)
- Issues enter as label `intake`. The `factory` label is the owner's approval. Never
  add `factory` from code except the Promote action the owner clicks.
- Columns, in order, with the prototype's names and subtitles:
  `intake` Intake, `triage` Triage, `building` Building, `review` Review,
  `waiting` Waiting on you, `merged` Merged.
- Column rule for an OPEN issue in a product repo (first match wins):
  1. label `needs-decision` -> waiting
  2. a linked open PR (the PR body/closing reference names the issue, read
     `closingIssuesReferences` via `gh pr list --json`) that is not draft -> review
  3. a linked open draft PR, or label `building` -> building
  4. label `factory` -> triage
  5. label `intake` -> intake
  6. anything else is not on the board.
  A CLOSED issue closed by a merged PR in the last 24 h -> merged. Other closed issues
  are not shown.
- Card: `#number`, repo short name, title, source badge from labels
  (`source:sentry` Sentry, `source:bug` Bug report, `source:support` Support, else
  Manual), "auto" marker when label `auto-promoted`, PR link when linked, "stuck"
  when label `stuck` or no update for 48 h in triage/building.
- Filters: product (from store products), source, "Only stuck", as in the prototype.
- Auto-promote strip: show the toggle and the rules list exactly as the prototype.
  The toggle reads/writes `board.json` key `auto_promote` (default true, per
  DECISIONS). Nothing in this step acts on it (the triage agent reads it later);
  say so in the status text: "Read by the triage agent".
- Card drawer actions (POST `/api/board`, same gate as `/api/autopilot`):
  - `promote {repo, number}` (intake only): add label `factory`, remove `intake`,
    comment "Promoted from the factory board." -> toast "Promoted to triage".
  - `close {repo, number}`: close as not planned with comment "Closed from the
    factory board." -> toast "Issue closed".
  - `send_back {repo, number, text}` (review/building with a PR): post the text as a
    PR comment -> toast "Sent back to agent". Text required (400 otherwise).
  - "Approve tradeoff" is NOT in this step: hide it.
  Every write goes through `gh` (`gh issue edit/close/comment`, `gh pr comment`) with
  the repo checked against the products' `repos` list (400 for any other repo) and
  `number` a positive int.
- Card drawer timeline: issue created, labels added (from `gh api
  repos/{r}/issues/{n}/timeline` or `events`, at most 10 newest), PR opened/merged.
  Read lazily: fetch on drawer open via `GET /api/board/timeline?repo=&number=`,
  not at build time.

## What to build
1. **Data** `factory/board.py`: `collect(products)` -> `{"cards": [...], "fetchedAt",
   "error"}` with one `gh` search per product (use `gh search issues` or GraphQL
   `search` with `repo:` qualifiers, `label:intake,factory,needs-decision,building`
   OR, open, plus closed in the last 24 h), and one `gh pr list --state open --json
   number,isDraft,closingIssuesReferences,url` per repo. Cache the last good result
   in `$FACTORY_HOME/board-cache.json`; on any `gh` failure return the cache with
   `error` set and `fetchedAt` = cache time (the prototype's "Last good data 14:02").
2. **Build**: `DATA.board` from `board.collect`, never crashing the build (warning
   on failure, like autopilot).
3. **Serve**: `POST /api/board` (actions above), `GET /api/board/timeline`. After a
   write, update the cache entry so the next page load shows the new column.
4. **CLI** `factory board list [--product P] [--json]` and `factory board promote
   <repo> <number>` / `close <repo> <number>` (same code path as the POST). Help text
   on each.
5. **Page**: Board sub-tab as in the prototype (filter row, auto-promote strip,
   6 columns that scroll horizontally on a phone, cards, drawer with actions, the
   loading/empty/error states). Empty text: "No factory issues" / "Automations file
   new issues as intake."
6. **README** + `docs/DATA.md`: board data, `board.json`, `board-cache.json`, route,
   commands.

## Ruled out
- No drag and drop between columns (stage comes from GitHub).
- No Automations tab, no Approve tradeoff, no triage agent, no auto-promote logic.
- Do not touch `~/factory-live`, `~/.factory`, port 3045, crontab. Use a temp
  `FACTORY_HOME` for every run and test. Mock `gh` in tests (no network).
- No owner-specific values (names, paths, domains) in code, help text or comments:
  the repo is open source.

## Prove it works
- `python3 -m pytest -q` passes; new `tests/test_board.py` covers: each column rule
  (incl. first-match order), merged-today vs older closed, product filter of repos,
  gh failure returns the cache with error, POST validation (unknown repo 400, bad
  number 400, promote on a non-intake card 409, send_back without text 400), the
  exact `gh` commands each action runs (mocked), build survives a board failure.
- Playwright (`--no-sandbox`) on a temp build with mocked board data at 1440 and 390:
  no console errors, no horizontal page scroll (the board grid may scroll inside its
  own wrapper), drawer opens, Promote shows the toast, error state shows "GitHub
  unreachable". Screenshots to `docs/screenshots/board-*.png`.

## Return
Commit on the branch as the owner's configured git email; do not push or open a PR.
Reply with: commit SHA, files changed, test count, the Playwright result, and
anything you did differently from this brief, with why.
