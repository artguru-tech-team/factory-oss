# Brief: Prototypes tab (factory feature)

## Goal
One place on the dashboard that lists every prototype agents produce, so the owner
validates or sends back each one without hunting links across threads.
The validated prototype is `~/factory-pages/proto-prototypes/factory-prototypes.html`
(screenshots `ds-*.png` beside it). Match its layout, states and wording. It is
written in the live design system, so reuse the classes already in
`template/factory.html`; add no new colours.

## Where
Repo `~/github/factory`, branch `factory/feat/prototypes-tab` (already checked
out). Read first: `README.md`, `factory/store.py`, `factory/cli.py`,
`factory/serve.py`, `factory/relay.py` (`save_message` path in serve.py),
`factory/build.py`, `template/factory.html` (how Skills/Agents tabs are
rendered from `DATA`), `tests/test_messages.py` (test style).

## What to build
1. **Store**: `$FACTORY_HOME/protos.json`, list of protos
   `{id, product, feature, project, versions:[{v, status, date, agent, thread,
   file, note, look_at:[...], decisions, sent_back_reason, pr}]}`.
   Status is one of `waiting`, `validated`, `sent-back`, `built`, `superseded`.
   Write only under `store.lock()`, same as the other json files.
2. **CLI** (`factory proto ...`, with `--json` like other commands, help text on each):
   - `add --product P --feature "F" --file PATH [--project FEAT-N] [--agent H]
     [--thread T3_THREAD_ID] [--note "..."] [--look-at "..."]...`
     id = slug of product + feature. Same id again = new version v+1; the older
     waiting/sent-back version becomes `superseded`. Copy the file into
     `$FACTORY_HOME/protos/<id>/v<n>.html` (the source may move later). Refuse
     a missing file or a non-.html file.
   - `list`, `show <id>`.
   - `built <id> --pr N` marks the latest validated version built.
3. **Serve** (`factory/serve.py`):
   - `GET /protos/<id>/v<n>.html` serves the copied file with header
     `Content-Security-Policy: sandbox allow-scripts` (decided: no same-origin
     access to the dashboard API). Path must match `^[a-z0-9-]+/v\d+\.html$`;
     anything else 404. Same Access gate as every other route.
   - `POST /api/protos` `{"id":..., "action":"validate"|"sendback", "reason":...}`.
     sendback needs a non-empty reason (400 otherwise). Only a `waiting` latest
     version can change (409 otherwise). Validate sets status and
     `decisions=["Validated by the owner on <date>"]`.
     Then queue a relay message to the version's `thread` with the same code
     path the Agents tab "Message" uses, text "Prototype <feature> v<n>:
     validated" or "Prototype <feature> v<n>: sent back: <reason>". No thread
     = save the status anyway and return `delivered:false, why:"not in T3"`.
4. **Build**: add `protos` to the page `DATA`; a corrupt protos.json shows the
   prototype's error banner, never crashes the build (add a build warning).
5. **Page**: nav entry "Prototypes" in the same nav list as Skills/Tokens, with
   a count of waiting ones. List grouped like the prototype (waiting first,
   older versions folded), product filter, drawer with Open prototype link,
   what-to-look-at list, Validate and Send back (reason textarea, required).
   Empty state text: "No prototypes yet. Agents add one with `factory proto add`."
   Actions POST to `/api/protos` and show the toast wording of the prototype.
   No devbar (that was prototype-only).

## Ruled out
- No editing of prototype files from the dashboard. No delete command.
- Do not touch `~/factory-live`, `~/.factory`, port 3045, or crontab. Use a temp
  `FACTORY_HOME` for every run and test.
- No owner-specific values (names, paths, domains) in code: the repo is going open source.

## Prove it works
- `python3 -m pytest -q` passes, with new tests in `tests/test_protos.py`:
  add twice = v2 + v1 superseded; bad file refused; serve returns the CSP header
  and 404 on `../` paths; sendback without reason = 400; validate on a
  non-waiting version = 409; validate with no thread returns delivered:false;
  corrupt protos.json = build warning, page still built.
- Build the page with a temp FACTORY_HOME holding 3 protos, open it with
  Playwright (`--no-sandbox`) at 1440 and 390 wide: no console errors, no
  horizontal scroll, drawer opens, Send back with empty reason shows the error.
  Save screenshots to `docs/screenshots/protos-*.png`.

## Return
Commit on the branch as `user@example.com` (do not push, do not open a
PR). Reply with: commit SHA, files changed, test count, the Playwright result,
and anything in this brief you could not do or decided differently, with why.
