# factory

The owner's control page for the AI software factory: what waits on the owner, where every project stands (build, loop, run lane), what each agent is doing, and multi-provider token telemetry and costs (Tokens view: Claude, Antigravity, OpenCode across 24 h / 7 d / 30 d windows with daily stacked bar charts and per-agent accounting). Agents write through the `factory` CLI; T3 Code is the agent registry.

- Validated prototype: `prototype/factory.html` (synthetic data). The real page is `template/factory.html`, filled by `factory build`.
- Decisions from validation: `docs/DECISIONS.md` (binding). Wiki: [Infrastructure — Factory dashboard decisions (2026-10-28)](https://app.notion.com/p/Infrastructure-Factory-dashboard-decisions-2026-10-28-40697e8235fa8125bec6cece87e85770).
- Design authority: `docs/DESIGN.md`. Product context: `docs/PRODUCT.md`.
- Current slice: `docs/BRIEF-cli-real-data.md`. Data contract and its changes from the prototype: `docs/DATA.md`.

## Install

Python 3.10+ with the standard library; PyJWT for the Cloudflare Access check (`pip install pyjwt`). `gh` logged in, and `git`.

```sh
ln -s "$PWD/bin/factory" ~/.local/bin/factory   # or run bin/factory, or python3 -m factory
factory doctor                                    # says exactly which source is missing
```

## Where state lives

Everything is in `~/.factory/` (`$FACTORY_HOME` overrides it). Writes are atomic (temp file then rename) under one lock (`.lock`).

A guard protects against development checkouts writing live state: when running from any checkout other than the live installation (`~/factory-live`), `FACTORY_HOME` must be set (e.g. `export FACTORY_HOME=$(mktemp -d)`). To explicitly use the live state from a development checkout, set `FACTORY_HOME=~/.factory`. Without `FACTORY_HOME`, commands refuse to run with a one-line error.

| Path | What |
|---|---|
| `config.json` | configuration settings, JSON file in the state dir |
| `secrets/<name>` | one secret value per file, mode 600 in a 0700 dir, set from stdin with `factory secret set <name>` (printed by name only, never by value) |
| `sweep.json` | the error sweep config (Sentry/SigNoz base urls, project/service→repo maps, min_events, max_per_repo, include_performance, run history) |
| `.key` | the `?k=` key for requests from this machine (the watchdog, tests), created on first use, mode 600. Never in the repo, the page or a link for the owner. |
| `.webhook` | the T3 webhook URL for pushing answers into T3 threads, mode 600. Never in the repo, the page or logs. |
| `relay.json` | outbox for answers and messages pushed to agent threads. Messages wait here until delivered via the webhook. |
| `protos.json` | prototypes agents registered with `factory proto add`: product, feature, versions (status, agent, thread, what to look at, decision). |
| `autopilot.json` | autopilot opt-in per project: `{paused, pausedAt, daily_cap, projects: {id: {thread, goal, every_hours, enabled, max_per_day, goal_source, last_nudge_at, history, nudged_on}}}` (`goal_source` is `page` or `cli`, the origin of the goal; `nudged_on` counts today's and yesterday's nudges for the caps). |
| `chatscan.json` | state of the chat question scan: `{thread_id: last_message_key}` filed into the inbox. |
| `board.json` | the board's auto-promote toggle: `{auto_promote}` (default true; read by the triage agent, nothing else in this step acts on it). |
| `board-cache.json` | the last board result (`{cards, fetchedAt, error, repoErrors}`), written on every collect; when `gh` is unreachable the cached cards are returned with `error` set so the page shows "Last good data HH:MM". |
| `protos/<id>/v<n>.html` | the copied prototype files, served sandboxed. |
| `skills-cache.json` | per-transcript skill-call cache, mode 600, safe to delete |
| `products.json` | products: name, T3 workspace paths, GitHub repos, run-lane labels |
| `products/<product>/projects/<id>.json` | one project: kind, plan, loop results, next steps, PRs, T3 match rules |
| `updates/<project>.jsonl` | project updates, one JSON line each |
| `inbox.json` | open inbox items and resolved ones (the decision log) |
| `answers.json` | The owner's answers, written by the page through `POST /api/answers` |
| `agents-extra.json` | agents T3 does not see (Hermes, remote boxes) |
| `tokens-cache.json` | token event cache, 30 days, mode 600, safe to delete, rebuilt on the next build |
| `site/factory.html` | the built page |

Read-only sources: `~/.t3/userdata/statev2.sqlite` (opened with `mode=ro`, schema checked first), `~/.t3/userdata/usage-scan-cache-v9.json` (Claude tokens), `~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations/*.db` (Antigravity protobuf gen_metadata), `~/.local/share/opencode/opencode.db` (OpenCode messages), skills definitions (`~/.claude/skills/*/SKILL.md`, `~/.agents/skills/*/SKILL.md`, and `~/.claude/plugins/installed_plugins.json`), skill call transcripts (`~/.claude/projects/**/*.jsonl`), GitHub through `gh`, and for the DS migration the kit at `kit.ref` in `kit.repo` plus the `kit.notes` file (see Configuration). Nothing is written to T3 or the kit repo: the build runs no `git fetch`, so something else must keep `kit.ref` fetched. State files are created mode 600 in a 700 directory.

## Configuration

Configuration is stored in a single JSON file `config.json` in the state dir (`~/.factory/config.json` or `$FACTORY_HOME/config.json`). Keys use dot notation mapping to nested objects.

Values are read via `factory config get <key>`, updated via `factory config set <key> <value> [--as-json]` (stored as a string unless `--as-json`), and inspected with `factory config list [--json]`.

### Keys, meanings and defaults

| Key | Meaning | Default |
|---|---|---|
| `owner.name` | The owner's display name for runtime messages and inbox text | `the owner` |
| `access.team` | Cloudflare Access team domain (e.g. `https://<team>.cloudflareaccess.com`) | Unset (no Cloudflare Access configured; fail-closed on tunnelled requests carrying `Cf-Ray` or `Cf-Connecting-Ip`, local requests allowed) |
| `access.aud` | Cloudflare Access Application Audience (AUD) tag | Unset |
| `kit.repo` | Path to local git clone of the DS migration kit repo | Unset (kit source disabled: a `ds-kit` project shows empty, the build warns and `factory doctor` fails) |
| `kit.ref` | Git ref or branch to read kit documents from | `main` |
| `kit.gh` | GitHub `owner/repo` for migration kit PRs | Unset |
| `kit.notes` | Path to progress notes JSON file (also the default for `import ds-notes --notes`) | Unset |
| `kit.answered` | Path to answered decisions JSON, default for `import ds-notes --answered` | Unset |
| `kit.bases` | List of base branch names to query PRs for | `[]` |
| `kit.search_base` | Base branch name to query with `--search` filter in kit PRs | `develop` |
| `kit.search` | GitHub PR search filter for `kit.search_base` PR queries | `ds- in:head` |

### Example

```sh
factory config set owner.name "the owner"
factory config set access.team "https://<team>.cloudflareaccess.com"
factory config set access.aud "<application AUD tag>"
factory config set kit.repo "~/github/<repo>"
factory config set kit.ref "origin/<branch>"
factory config set kit.gh "<owner>/<repo>"
factory config set kit.notes "~/path/to/progress-notes.json"
factory config set kit.bases --as-json '["<branch>", "develop"]'
```

Values are stored as strings; pass `--as-json` for lists and numbers. Upgrading from a version without `config.json`: set `access.*` and `kit.*` right after checking out the new version, or every tunnelled request is refused and the DS kit project shows empty (the build warns and `factory doctor` fails until `kit.repo` is set).

## Commands

Every command prints JSON with `--json` and exits non-zero with a one-line error otherwise.

**Products** (the T3 workspace to product mapping)
- `factory product set <key> --name N [--workspace PATH]... [--repo owner/repo]... [--run-label L]...`; `factory product list`

**Projects**
- `factory project add <id> --product P --kind build|loop|run --name N [--feat FEAT-12] [--prio 1]`
- `factory project set <id> [--name] [--feat] [--prio] [--next TEXT]... [--pr N|owner/repo#N]... [--match TEXT]... [--source ds-kit|manual]`. `--next`, `--pr` and `--match` replace the list; `--add-pr N` (repeat) adds PRs and keeps the others. `--match` claims T3 threads whose title, or whose root thread's title, contains the text.
- `factory project list`

**Build plans**
- `factory plan phase <project> <name> --total N --start YYYY-MM-DD --due YYYY-MM-DD [--pr REF[:UNITS]]...`. `--pr` links a PR that delivers UNITS of the phase (default 1; REF is a number or owner/repo#N; repeat; replaces the phase's list; `--pr ''` unlinks). A linked phase is counted by the build from GitHub: a merged PR's units are merged, an open PR's are in PR, a PR closed without merging counts as neither and warns, and units no PR carries stay to do. The PRs' units plus blocked must fit in the total. The build saves changed counts with a snapshot, so the graph follows.
- `factory plan rename <project> <old> <new>` (also renames the phase in every scope)
- `factory plan remove <project> <phase> [--force]` (refuses a phase with merged units or named in a scope; `--force` removes it anyway, takes it out of those scopes and deletes any of them left with 0 units. Its merged units leave today's count, so the project graph's merged line drops)
- `factory plan units <project> <phase> [--merged N] [--in-pr N] [--blocked N]`. On a phase with linked PRs only `--blocked` is accepted.
- `factory plan scope <project> "<scope name>" <phase,...>`
- `factory plan added <project> N` (scope added since the start)

Each `plan phase`, `plan units` and `plan remove` records today's scope and merged count; the project graph is drawn from those snapshots. A project with `--source ds-kit` ignores them: its phases, scopes, graph and PRs come from the DS kit.

**Loops**
- `factory loop meta <project> [--metric M] [--unit U] [--baseline X] [--target Y]`. The target must be above the baseline: the page draws higher-is-better metrics only.
- `factory loop start <project> "<change>"`
- `factory loop record <project> "<change>" --value X --kept|--discarded`

**Agent identity**
- `factory whoami [--thread <id>]` prints your handle, T3 thread id, title and project, as the page shows them: put the handle in `--agent` / `--from`. It finds the thread from `CLAUDE_CODE_SESSION_ID` or `ANTIGRAVITY_CONVERSATION_ID` in your environment, else, when neither is set, from the worktree you run it in. A native subagent (Claude's Task tool) shares its parent's session, so it gets the parent's handle: let the parent post.

**Updates**
- `factory update <project> --health on-track|at-risk|off-track --agent <hc> "<text>"`

**Inbox**
- `factory ask --project <id> --kind urgent|applied|action --title T [--short S] [--context C] --option a="..." --option b="..." [--reco a] [--why W] [--from <hc>] [--thread <id>]` prints the new id. Field `thread` records the asking T3 thread (via `whoami`, or overridden with `--thread`). An action without options gets Done / Blocked. An applied item needs `--reco` (the option the agent applied).
- `factory answers [--since ISO] [--wait SECONDS] [--id <id>]... [--from <hc>]` prints answered items that are not resolved yet. With `--id` (repeatable) and/or `--from`, only matching items are listed, and `--wait` blocks until at least one matching item is answered (or the timeout passes). An unknown or already resolved `--id` fails at once instead of waiting. Without filters, behaviour is unchanged. Since answers stay listed until resolved, `--since` is optional.
- `factory resolve <id>` moves an item to the decision log once its answer is applied.

**Prototypes**
- `factory proto add --product <key> --feature "F" --file proto.html [--project FEAT-N] [--agent <handle>] [--thread <id>] [--note "..."] [--look-at "..."]...` copies the file and adds it as waiting. The id is the slug of product + feature; the same product and feature again adds v2 and supersedes the older waiting or sent-back version. Pass `--thread` so the decision reaches you. Prints the id.
- `factory proto list`, `factory proto show <id>`.
- `factory proto built <id> --pr N` once the validated prototype is built.

**Autopilot** (opt-in scheduled nudges that keep a project's T3 session moving)
- `factory autopilot add <project> --thread T --goal "..." [--every 3] [--max-per-day 4] [--off] [--force]` — the project must exist; the thread must be a live non-subagent T3 thread unless `--force`. Sets the goal's source to `cli` (the nudge tells the agent the goal was set from the CLI).
- `factory autopilot set <project> [--goal] [--every] [--max-per-day] [--thread] [--on|--off]`, `factory autopilot remove <project>`, `factory autopilot list`.
- `factory autopilot pause` / `factory autopilot resume` (global kill switch), `factory autopilot done <project>` (the goal is met: disables it and records "done").
- `factory autopilot run [--dry-run] [--project P]` — nudges each enabled, due project whose session is idle (done); skips working, waiting-on-you and gone sessions; caps at `max_per_day` per project and `daily_cap` across projects (counted from `nudged_on`, not the history log); goals touching production/money/a merge (word stems: "merged", "deployment" count) raise one "Unblock autopilot" inbox action on the project's thread instead; while it is open the project is "waiting on you", and answering it sends your choice to that session. The relay checks pause, the project and the session state again right before a queued nudge goes out, and drops it if any changed. Delivers queued nudges synchronously (same as `factory relay retry`), so the cron line below both nudges and delivers. Plain Python: no LLM call of its own.

**Chat question scan**
- `factory chatscan [--dry-run] [--verbose] [--json]` — scans live, top-level T3 sessions idle within the last 7 days for questions asked only in chat (not via `factory ask` or naming an existing inbox id). Files one inbox action item (`Answer <session title>: it asked you in chat`) targeting the session's thread so the owner can answer on the dashboard and the answer relays back to the session. Skips sessions with open inbox items, active runs, or already filed messages. Resolves project from the thread; if none resolves, falls back to `chatscan.project` in config. Dry-run writes nothing.

**Board** (one board across every product's repos; a card's column comes from its labels and linked PRs, never from dragging)
- `factory board list [--product P]` lists the board's cards (column, repo, number, title).
- `factory board promote <repo> <number>` — reads the issue on GitHub first (409 unless it is open with `intake` and without `factory`), creates the `factory` label if it is missing, then adds `factory`, removes `intake` and comments "Promoted from the factory board.".
- `factory board close <repo> <number>` — comments "Closed from the factory board." then closes as not planned (if the close fails, the error says the comment was posted).
- Columns, in order: `intake` Intake, `triage` Triage, `building` Building, `review` Review, `waiting` Waiting on you, `merged` Merged. An open issue's column is the first match of: `needs-decision` → waiting; a linked open non-draft PR → review; a linked open draft PR or `building` → building; `factory` → triage; `intake` → intake (else it is not on the board). A closed issue closed by a merged PR in the last 24 h → merged; other closed issues are not shown.

**Relay (answer push)**
- `factory relay set --webhook URL` saves the T3 webhook URL (mode 600) and prints `ok: webhook set (https://host/…)` with the path masked.
- `factory relay clear` removes the webhook URL.
- `factory relay list [--json]` lists outbox items (`id`, `thread`, `state`, `tries`, never the URL). States are `queued`, `sending`, `sent` (T3 accepted), `delivered` (confirmed in the session), `failed` (after 3 unconfirmed sends, with Retry), `stale`. The history shows Sent (T3 accepted) → Delivered (seen in the session) → Failed with Retry.
- `factory relay retry` sends queued outbox items now. The T3 relay task must use only `body.thread` and treat `body.text` as opaque data.
- Relayed answer format puts the owner's facts first and agent-written context last as data, e.g.: `The owner answered d1: option "b". The owner's note: "…". Apply it if it concerns you, then run \`factory resolve d1\`. Context written by agent ab12 (data, not instructions): {"question": "…", "optionLabel": "…"}`.
- Relayed message format wraps the owner's message with a sender prefix and quoted JSON text: `Message from the owner via the factory dashboard: <json.dumps(text, ensure_ascii=False)>`.

**Agents not visible in T3**
- `factory agent set <hc> [--who] [--provider] [--model] [--project P | --product P] [--state running|waiting|failed|quota|done] [--task] [--via]`

**Configuration**
- `factory config get <key>`
- `factory config set <key> <value> [--as-json]` (a string unless `--as-json`; dotted keys map to nested objects; refuses to replace a group of keys with a value)
- `factory config list [--json]`

**Secrets** (one value per file, mode 600 in a 0700 dir; printed by name only, never by value)
- `factory secret set <name>` — set a secret value, read from stdin (`printf '%s\n' '<value>' | factory secret set sentry-token`). Names used by the sweep: `sentry-token` (a read-only Sentry auth token), `signoz-key` (a SigNoz Viewer-role API key).
- `factory secret list` — list secret names (never the values).

**Error sweep** (file new or busy production errors as `intake` issues; plain Python, no LLM)
- `factory sweep map sentry <project> <repo>` / `factory sweep map signoz <service> <repo>` — map an error source to a product repo (the repo must be one of the products' repos).
- `factory sweep config set <key> <value>` — `sentry.base_url`, `sentry.org`, `signoz.base_url`, `signoz.default_repo`, `min_events`, `max_per_repo`, `include_performance`.
- `factory sweep run [--source sentry|signoz|all] [--dry-run] [--json]` — read errors and file one `intake` issue per error with the `intake` label plus `source:sentry`/`source:signoz` (never the `factory` label: that is the owner's approval). Dedupes by a marker line in the issue body; a closed match is re-filed as "Regression of #N". Caps at `max_per_repo` new issues per repo per run. Dry-run prints what it would file and files/records nothing. The Sentry and SigNoz parts are independent: one failing never stops the other.

**Key**
- `factory key rotate` under `store.lock()`, replaces `.key` with a new random key (mode 600, atomic), printing only `ok: key rotated` (never the key; `--json` returns `{"rotated": true}`).

**Imports, build, checks, server**
- `factory import ds-notes [--project ds] [--notes PATH] [--answered PATH]` seeds the inbox with the open decisions and actions of the notes file (`--notes`, default `kit.notes`; ids `ds-<id>`; idempotent; items already answered in `--answered`, default `kit.answered`, are skipped).
- `factory build` renders `~/.factory/site/factory.html`. A failing source stops the build and the last good page stays; it shows its age and a banner past 15 minutes. Some problems only warn (printed by the build, and in `DATA.warnings`), for example: a PR or repo gh cannot read (the page shows "Not found on GitHub"); a project not ready to draw (a build with no units, a loop with no baseline and target), which stays off the page and which `factory ask` refuses; a T3 run status factory does not know (those agents show as "Unknown T3 status", and `factory doctor` fails); Claude threads not in T3's token scan yet; an agent handle that collides with a T3 one. Builds may run at the same time (cron and agents): the page is written under the state lock, and a build that started earlier is skipped (`skipped: a newer build already wrote the page`, exit 0) rather than replacing a newer page. A `site/built-at` stamp more than 5 minutes in the future (a clock change) is ignored with a warning.
- `factory doctor` checks the state dir, key, relay state (configured webhook and outbox count), products, the T3 schema, the token scan, `gh`, each repo, the DS kit, and stale phases: a phase with units in PR while none of its PRs (or, with none linked, the project's) is open, or more than 2 days past due with merged + blocked under the total; a PR gh cannot read is reported as such. The build prints the same stale-phase warnings.
- `factory serve [--port 3045]`

## Running the server

```sh
factory build && factory serve          # 127.0.0.1:3045 only
open "http://127.0.0.1:3045/?k=$(cat ~/.factory/.key)"
factory key rotate                      # replaces .key with a new random key (mode 600)
```

`GET /` is the page; `GET /api/answers` and `POST /api/answers` (`{"<id>": {"choice": "a", "note": "..."}}`) read and save answers; `GET /api/inbox/open` returns `{"open": [ids], "closed": {id: {resolvedAt, answeredAt}}}` for the last 24 h (behind the same gate); `POST /api/messages` (`{"thread": "...", "text": "..."}`) sends a message to an agent session and `GET /api/messages?thread=<id>` returns that thread's history. `POST /api/protos` (`{"id": ..., "v": <version on screen>, "action": "validate"|"sendback", "reason": ...}`) decides that version if it is still the latest and waiting (send back needs a reason; a newer or already decided version answers 409 with the current one) and messages the agent's thread. `POST /api/autopilot` drives the Autopilot view, one action per call:
- `{"action": "pause"}` / `{"action": "resume"}` — the global kill switch (no other fields).
- `{"action": "toggle", "project": "<id>", "enabled": true|false}` — enable/disable one project.
- `{"action": "save", "project": "<id>", "goal": "...", "every_hours": 3}` — change a goal (sets `goal_source` to `page`) and interval.
- `{"action": "add", "project": "<id>", "thread": "...", "goal": "...", "every_hours": 3, "enabled": false}` — add a project; `enabled` defaults off, `goal_source` is set to `page`.
- `{"action": "send", "project": "<id>", "confirmed": false}` — nudge one project now (same checks as `run`, minus "due"); pass `confirmed: true` to send anyway on a session that is waiting on you or has an open inbox item. Delivery here is asynchronous, unlike the CLI's synchronous `run`.

`POST /api/board` drives the Board view, one action per call (the same gate as `/api/autopilot`):
- `{"action": "promote", "repo": "<owner/repo>", "number": 142}` — reads the issue on GitHub first (409 unless it is open with `intake` and without `factory`), creates the `factory` label if missing, adds `factory`, removes `intake`, comments "Promoted from the factory board.".
- `{"action": "close", "repo": ..., "number": ...}` — comments "Closed from the factory board." then closes as not planned.
- `{"action": "send_back", "repo": ..., "number": ..., "text": "..."}` — posts the text as a comment on the issue's linked PR (review/building cards only; text required).
- `{"action": "auto_promote", "enabled": true|false}` — writes the board's auto-promote toggle (read by the triage agent).
- `{"action": "refresh"}` — runs `board.collect` now and returns the fresh board (the error state's Retry button).
- `repo` must be a string naming one of the products' repos (400 otherwise) and `number` a positive integer. Every write goes through `gh`.

`GET /api/board` returns the cached board (`{cards, fetchedAt, error, repoErrors, auto_promote}`); the Board view loads its cards from it on open (falling back to the build's embedded `DATA.board`). The search runs one `gh` call per repo (each in its own try): a repo that fails is recorded in `repoErrors` and skipped, and `error` is set only when every repo failed (then the cached cards are returned).

`GET /api/board/timeline?repo=<owner/repo>&number=<n>` returns the card drawer's timeline (issue created, labels added, PR opened/merged; at most 10, newest first), read lazily on drawer open; a failed fetch answers 502 with the error.

`GET /protos/<id>/v<n>.html` serves a prototype with `Content-Security-Policy: sandbox allow-scripts`; the page links it with `?t=`, a token that opens only that file, so the prototype's scripts never see the `?k=` key. The token derives from the key: after `factory key rotate`, run `factory build` or the links answer 401 until the next build. A `POST /api/answers` on an item an agent already closed answers 409 `{"error": "already handled", ...}` and the page shows the "Already handled" notice (see "The inbox on the page"), then removes the item. A POST must be `application/json` and, from a browser, same-origin (no cross-site forms or scripts). Requests that arrive through the Cloudflare tunnel (they carry `Cf-Ray` or `Cf-Connecting-Ip`) need a valid Cloudflare Access JWT (`factory/access_jwt.py`, team and AUD from `access.*` config; it fails closed, also when unconfigured), and that login is enough. Requests from this machine need `?k=`. The cron schedule and the public hostname are in "Running live" below.


## Running live (<your-host>)

Live since 2026-10-28. The live instance runs from `~/factory-live`, a separate clone of this repo detached at `origin/main`. Nobody edits it, so switching branches in a development checkout never changes the live page.

- **Cloudflare:** the shared named tunnel `<tunnel-id>` (`~/.cloudflared/config.yml`, kept alive by `tunnel-watchdog.sh`) routes `<your-host>` to `127.0.0.1:3045`. The hostname is a destination of the Access app "<access-app>" (policy "<policy-name>", email code login, 720 h session). `factory serve` checks the Access JWT (AUD of that app) on every tunnelled request, and that login is enough there. Requests from this machine carry no JWT, so they need the `?k=` key.
- **Injected analytics:** a domain zone may have Cloudflare's Google tag gateway on. It inserts a GTM snippet, and through it Microsoft Clarity, into every HTML response. `factory serve` sends a Content-Security-Policy that lets only the page's own inline script run (`serve.csp`), so the snippet is blocked and nothing leaves the page.
- **Schedule:** `deploy/crontab.txt` runs `factory build` every 10 minutes (log `~/.factory/build.log`), `factory autopilot run` every 30 minutes (log `~/.factory/autopilot.log`), `factory chatscan` every 15 minutes (log `~/.factory/chatscan.log`), `factory sweep run` every morning at 07:00 (log `~/.factory/sweep.log`), `factory relay retry` every 5 minutes (log `~/.factory/relay.log`), and `deploy/watchdog.sh` every 5 minutes and at boot. The watchdog checks that `factory serve` answers HTTP 401 on `127.0.0.1:3045/` (5 s timeout); if down, hung, or another process holds the port, it kills the listener, waits up to 10 s for the port to clear, restarts `factory serve`, and logs the reason to `~/.factory/watchdog.log`. Run from an agent session (a deploy), it starts the server in its own systemd user scope (`factory-serve-<port>-<time>.scope`) so T3 stopping that session cannot take it down; from cron it stays in `cron.service` (the user manager has no lingering and stops when the last login ends); server output goes to `~/.factory/serve.log`.
- **Install or update the cron lines:** `(crontab -l 2>/dev/null | grep -v '# factory-'; grep -v '^#' ~/factory-live/deploy/crontab.txt) | crontab -`
- **First setup:** `git clone -q <repo-url> ~/factory-live && git -C ~/factory-live checkout -q --detach origin/main`, then the cron install above, then `~/factory-live/deploy/watchdog.sh`.
- **Deploy a change:** after the merge to `main`, run `git -C ~/factory-live fetch -q origin && git -C ~/factory-live checkout -q --detach origin/main`, set any new config keys with `~/factory-live/bin/factory config set ...` (then `factory doctor`), run `/usr/bin/python3 ~/factory-live/bin/factory build`, stop the server (`kill` the PID from `ss -ltnp | grep 3045`), wait until the port is free (`while ss -ltn | grep -q '127.0.0.1:3045 '; do sleep 0.2; done`), then run `~/factory-live/deploy/watchdog.sh`.
- **Health checks:** `curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:3045/?k=$(cat ~/.factory/.key)"` returns 200 when `factory serve` is up. `curl -s -o /dev/null -w '%{http_code}' https://<your-host>/` returns 302 (Access login); that proves DNS and Access only, not the tunnel or the server. For the tunnel, `cloudflared tunnel info <tunnel-id>` lists its connections. In a logged-in browser, 530 means the tunnel is down and 502 means `factory serve` is down.
- **Link for the owner:** `https://<your-host>` (no key: the Access login is the gate through the tunnel).

## How an agent uses the CLI

1. Read `factory --json project list` and find your project; add one with `project add` if the owner asked for it.
2. Keep the plan current: link each phase's PRs (`plan phase ... --pr N:UNITS`) so merges count themselves, else `plan units` when a unit merges or opens a PR, `loop record` after each experiment.
3. Post `factory update <project> --health ... --agent <your hc> "..."` at least every 3 days; a loop with no update for 3 days folds away as "No update".
4. Questions for the owner go through `factory ask`, never only in chat. Low-risk, reversible choices: apply the recommended option and ask with `--kind applied`. Prod, infra, money, secrets, force-push and merges: `--kind urgent`, then wait.
5. When the owner answers on the page, the answer is pushed as a message to your T3 thread via webhook. If the webhook is unreachable or not configured, messages wait in the outbox (`relay.json`). States progress: Sent (T3 accepted) → Delivered (seen in the session) → Failed with Retry (after 3 unconfirmed sends). `factory answers --wait` (with `--id` or `--from` to wait for your own items) stays the source of truth. Once applied, run `factory resolve <id>` to move it to the decision log.
6. Do not edit the JSON files by hand; if a command is missing, add it here.

## The inbox on the page

The page checks `GET /api/inbox/open` every 30 seconds. An item an agent has closed (`factory resolve`) disappears from every list (home, project drawer, All) within that interval, and the counts follow, with no page reload.

- **Typing an answer.** Closed items still disappear while the owner types in another item; the open answer keeps its text, chosen option and cursor. If the item being typed in is the one that closed, it stays on screen with the notice "Already handled by the agent (closed at HH:MM UTC). Your draft is kept; saving will not change anything." ("your answer was applied at HH:MM UTC" when the agent applied an earlier answer). If the Cloudflare login expires while typing, the page shows "Signed out" and keeps the draft so it can be copied before reloading.
- **Saving an answer to a closed item.** The server answers 409. The page shows "Already handled by the agent (your answer was applied at HH:MM UTC)." when the agent applied an earlier answer, or "(closed at HH:MM UTC)." otherwise, and removes the item 3 seconds later.
- **Full reload.** The page reloads itself only when nothing is being typed (no answer draft, no message draft, no 409 notice showing).

## Tests

```sh
python3 -m unittest discover -s tests -t .
FACTORY_HOME=/tmp/copy FACTORY_BIN=bin/factory [PRODUCT=acme] [PROJECT=ds] PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node tests/browser_check.mjs "http://127.0.0.1:3045/?k=KEY" /tmp/shots [lever id]
PLAYWRIGHT=/path/to/playwright/index.mjs node tests/test_inbox_draft_poll.mjs [/tmp/shots]
```

The inbox draft poll check (`tests/test_inbox_draft_poll.mjs`) is self-contained: it builds a temp `FACTORY_HOME` with `FACTORY_T3_DB` and fake datasource paths (never reads real agent data), starts an ephemeral server, and tests `home` and `drawer` (`#/p/<proj>`) at 390 and 1440 px, and `#/all` at 1440 px. It asserts that closed items vanish within 35 s across all copies (in `#main` and `#drawer`), all counts stay consistent, and active draft text and choices remain intact without console errors.

The browser check loads the page at 390 and 1440 px (home of `PRODUCT`, the drawer of `PROJECT`, the Agents and All views). It fails on console errors, external requests, horizontal scroll, and any chart element outside its card or with a NaN coordinate. Given a lever id, it also saves an answer through `POST /api/answers`, then checks that `factory answers` returns it. Run that part against a copy of the state (`FACTORY_HOME=/tmp/copy`) so test answers never reach the real inbox.
