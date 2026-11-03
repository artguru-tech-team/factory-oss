# Brief: Autopilot (factory feature, build step 2 of 6)

## Goal
Projects the owner opts in get a scheduled nudge in their T3 session, so work keeps
moving while he is away. Never nudge a session that is working, waiting on
the owner, or done; never nudge toward production, money or a merge (those go to
the inbox). A global pause stops everything.

Validated prototype: `~/factory-pages/proto-automation/factory-automation.html`,
tab "Autopilot" (screenshots `ds-autopilot-*.png` beside it). Match its table,
drawers, states and wording, in the live design system (`template/factory.html`
classes, no new colours). Build only the Autopilot tab and the pause banner now;
the Board and Automations tabs are later steps.

## Where
Repo `~/github/factory`, branch `factory/feat/autopilot` (checked out). Read
first: `README.md`, `factory/store.py`, `factory/cli.py`, `factory/t3.py`
(`agents()` gives each thread's state: running / waiting / stalled / done /
failed), `factory/relay.py` (`enqueue_message`, delivery is confirmed and
re-sent), `factory/serve.py` (`save_message`, `save_proto` for the POST
pattern, `live_agent_threads`, `is_subagent_thread`), `factory/build.py`,
`template/factory.html` (Prototypes view: nav entry, drawer, toast, seg tabs),
`tests/test_protos.py` (test style).

## Decisions already made (do not re-decide)
- Opt-in per project; a project has one T3 thread, a goal (done criteria, text),
  and an interval in hours (1, 3, 6, 12, 24; default 3).
- At most 1 nudge per project per run, and at most `max_per_day` nudges per
  project per day (default 4). A global daily cap across projects (default 12).
- Global pause (kill switch) in the store; while paused, `run` does nothing and
  says so.
- Warm nudges only (same thread, through the relay). Fresh-session handoff is
  out of scope; when the thread's context is large, the page only shows the
  size.
- Goals touching production, money or a merge are never nudged. Detect with a
  word list on the goal (`merge`, `deploy`, `release`, `prod`, `production`,
  `payout`, `payment`, `wallet`, `money`, `migration`): the run skips with
  "touches production/money/merge: needs you", and creates at most one
  `factory ask` action item per project per day (title "Unblock autopilot:
  <project>", context = the goal). Reuse the existing ask code path.

## What to build
1. **Store** `$FACTORY_HOME/autopilot.json`:
   `{"paused": false, "pausedAt": null, "daily_cap": 12, "projects": {"<project id>":
   {"thread", "goal", "every_hours", "enabled", "max_per_day", "last_nudge_at",
   "history": [{"at", "result"}]}}}` (history keeps the last 20). Shape-check on
   read like `store.protos()`; write only under `store.lock()`.
2. **CLI** `factory autopilot ...` (help text and `--json` on each):
   - `add <project> --thread T --goal "..." [--every 3] [--max-per-day 4] [--off]`:
     the project must exist (same error and fix as other commands); the thread
     must be a live non-subagent T3 thread unless `--force`.
   - `set <project> [--goal] [--every] [--max-per-day] [--thread] [--on|--off]`,
     `remove <project>`, `list`.
   - `pause` / `resume` (global), `done <project>` (an agent says the goal is
     met: disables it and records "done" in history).
   - `run [--dry-run] [--project P]`: for each enabled, due project (now -
     last_nudge_at >= every_hours) decide one of: nudged / skipped working /
     skipped waiting on you (thread state waiting, or an unresolved inbox item
     whose thread is this one) / skipped done / skipped thread gone /
     skipped cap / needs you (word list). Nudge = `relay.enqueue_message(thread,
     text)` then trigger delivery. Text: "Autopilot nudge from the factory
     dashboard. Goal: <goal>. Keep going toward it. If something blocks you,
     ask with `factory ask`. When the goal is met, run `factory autopilot done
     <project>`." Record the result in history; print one line per project.
     A T3 read failure skips all with "T3 unreadable", never crashes.
3. **Serve**: `POST /api/autopilot` with actions `toggle {project, enabled}`,
   `save {project, goal, every_hours}`, `add {project, thread, goal,
   every_hours, enabled}`, `send {project}` (nudge now, same checks as run
   except "due" and the word list still applies), `pause`, `resume`. Same POST
   gate as `/api/protos`. Validate every field (400 with the fix).
4. **Build**: `autopilot` into page DATA: config + per project the thread's
   current state from `t3.agents()` rows (state label, idle since, context
   tokens if the agents rows carry them, else "-"), last run result, next due
   time. A corrupt autopilot.json = build warning + the prototype's error
   banner, never a crash.
5. **Page**: nav entry "Automation" (same nav list as Prototypes) opening the
   Autopilot view: pause banner (Pause all / Resume), the projects table as in
   the prototype (state dot, goal, interval, nudge type badge, toggle, Send
   now, Edit), Add project drawer (project select from DATA.projects, thread
   select from live agent threads of that project, goal required, interval,
   enabled), Edit drawer, confirm dialog before Send now on a waiting
   session, empty state ("No projects on autopilot" + Add project), toasts as
   in the prototype.
6. **Cron**: add `*/30 * * * * factory autopilot run` (with the same env and
   log pattern as the build line, log `autopilot.log`) to
   `deploy/crontab.txt`. Do not install it.
7. **README**: state file, commands, route, cron line.

## Ruled out
- No fresh-session handoff, no LLM call inside `run` (it is plain Python: no
  token cost of its own).
- Do not touch `~/factory-live`, `~/.factory`, port 3045, the live crontab.
  Use a temp `FACTORY_HOME` for every run and test.
- No owner-specific values (names, paths, domains) in code or help text.

## Prove it works
- `python3 -m pytest -q` passes; new `tests/test_autopilot.py` covers: add
  refuses unknown project / dead thread; run nudges a due idle project once
  and not again before `every_hours`; skips working, waiting, done, gone;
  per-project and global caps; paused run does nothing; word-list goal makes
  one ask per day and no nudge; T3 failure does not crash and nudges nothing;
  POST validation (400s) and send on a waiting thread; corrupt
  autopilot.json = build warning, page still built.
- Playwright (`--no-sandbox` if needed) on a temp build at 390 and 1440: no
  console errors, no horizontal scroll, toggle, Add drawer validation (goal
  required), Pause all banner. Screenshots to `docs/screenshots/autopilot-*.png`.

## Return
Commit on the branch as `user@example.com`; do not push or open a PR.
Reply with: commit SHA, files changed, test count, the Playwright result, and
anything you did differently from this brief, with why.
