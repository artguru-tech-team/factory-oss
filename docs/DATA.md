# Page data contract

`factory build` injects one `DATA` object at `/*DATA*/null` in `template/factory.html`. Its shape is the prototype's `DATA` (`prototype/factory.html`). This file lists every place where real data needed a change, and where each value comes from.

## Changes from the prototype

| Field | Prototype | Real page | Why |
|---|---|---|---|
| `NOW` | fixed 2026-10-27 14:06 | the browser clock at load | real ages |
| build `series` | synthesized in the page (easing curve) | `projects[].series`: daily `[scope, merged]` from the start to today, computed by the build | the prototype curve was invented |
| build dates | first phase start, last listed phase due | earliest phase start, latest phase due, whatever the list order; a build whose first phase starts after today is "Not started", on track; the graph axis always covers today | phases can be added in any order |
| build `scopes` | first scope = whole programme | the first scope is always the whole project: "Whole programme" for the DS kit (then "Before the flip"), "Whole project" for a manual build (then its named scopes); a scope over 0-unit phases is refused | the page reads `scopes[0]` as the whole project |
| loops | higher is better | still higher is better; `loop meta` refuses a target at or below the baseline | a lower-is-better metric was drawn as 0% progress |
| `warnings` | none | build warnings, for example PRs gh cannot read, projects not ready to draw, inbox items on hidden projects, unknown T3 run statuses, Claude threads not in the token scan, colliding agent handles; not shown on the page (render warnings such as a skipped or clock-shifted page write are printed by the CLI, not embedded in `DATA.warnings`) | printed by `factory build` |
| build series start | first phase start | the earlier of the first phase start and today (a build that has not started yet still plots today's merges; a one-day series is a dot) | merges can land before the plan's start |
| build forecast | speed floored at 0.2 units/day | no forecast when nothing merged in the last 7 days; health is then at risk, or off track past the last due date. A project with every unit merged shows "All N units merged", on track | a floor invents a finish date |
| loop `lastActive` | always set | `null` until the first `loop start`, `loop record` or update; the page then counts idle days from `createdAt` | no invented activity |
| build `undatedMerged` | none | merged units with no PR merge date, counted from the start; named in the graph key | so the graph says what it assumes |
| `agents[].product` | derived from `project` | its own field | T3 threads that match no project still belong to a product |
| `agents[].project` | always set | may be `null` ("No project") | same |
| `agents[].tokens` | always a number | `null` when unknown, shown as "—" | Claude, Antigravity, and OpenCode tokens from their respective stores; agents outside T3 have no token source |
| `agents[].state` | running, waiting, failed, quota, done | adds `stalled` ("Silent 2 h+") and `unknown` ("Unknown T3 status": a run status factory does not know, never shown as done; `factory doctor` fails and the build warns); T3 statuses map as: preparing, queued, starting, pending, running = running (or stalled); waiting = waiting (until its thread is silent 24 h, then it leaves the page); failed, interrupted, rolled_back = failed; completed, cancelled, idle = done (the full lists of T3 0.1.3) | a T3 run still marked running with no thread activity for 2 h; no ghost "running" rows |
| `agents[].t3Status` | none | the T3 run status as T3 wrote it (T3 rows only) | the build warning and `factory doctor` count the `unknown` rows by it, so they agree with the page |
| `agents[].hc` | typed | role digit (1 thread, 2 delegate, 3 subagent) + the shortest sha1 prefix of the thread id (3+ characters) not already taken, older threads first | unique, and a newer thread never changes an older one's handle |
| `agents[].thread` | none | the T3 thread id | traceability |
| `agents[].phase` | the phase index the agent works on | dropped | T3 has no such link, and the page never read it |
| `tokens` | none | `{window, scannedAt, claudeAll, note}` | the Agents view says what the counts cover and how old the scan is |
| `tokenHistory` | none | `{"24h": {...}, "7d": {...}, "30d": {...}}` | Tokens view KPI metrics, stacked bar chart, and per-window agent tables |
| `agentTokens` | none | array of 24 h agent table rows (`hc`, `who`, `provider`, `model`, `project`, `parent`, `input`, `output`, `cache`, `cache_write`, `tot`, `cost`, `unmetered`, `why`) | Tokens view 24 h per-agent token and cost breakdown (per-window tables stored in `tokenHistory[window].agents`) |
| `tokenWarnings` | none | array of token-source warning strings | shown as a banner above the Tokens view; error box only when no source loaded at all |
| "Tokens today" | calendar day | "last 24 h" | a rolling window does not empty at midnight UTC |
| `prs` keys | PR number | `owner/repo#N` | numbers collide across repos |
| run lane `items[i][3]` | Todo, In review | always "Open" | GitHub issues carry no such status; a linked-PR status would be a guess |
| `provColors` | Claude, Antigravity, OpenCode, LiteLLM | adds Codex (`var(--lav)`); a provider with no colour gets `var(--ash)` | T3 can run Codex |
| `updates` | all | the 10 newest per project | the page shows two; the rest only grow the page |
| `prs[k][1]` (CI) | pass, fail, run | adds `none` (no checks), grey dot | real PRs can have no checks |
| `inbox[].thread` | none | T3 thread id of the asking agent; null when unknown | answer push targets this thread via webhook |
| `answers` | `localStorage` | embedded at build time, then `GET /api/answers`; saved with `POST /api/answers` | answers must reach the agents |
| `relayConfigured` | none | boolean only: `true` if webhook URL is configured in `~/.factory/.webhook` | whether agent messaging can send via relay |
| `agents[].canReceive` | none | boolean: `true` if agent has a live T3 thread | agents outside T3 show Message disabled ("not in T3: no way to deliver") |
| `log` | typed | resolved inbox items (you or default), open applied items (default), loop records (loop) | computed |
| `exps[i]` | `[change, value, kept]` | the CLI also stores a time as a 4th item; the page gets the first three | the decision log needs the time |
| prototype states panel | toggles | removed; the stale banner shows by itself past 15 min, "signed out" on a 403 | real states |
| Office button | `alert()` | link to `office` carrying `location.search` when `officeInstalled` is true; otherwise disabled button | wired to installed Office |
| `officeInstalled` | none | boolean: true if `site/office.html` is installed | enables Office button |
| `fmtV` units | `%` or `€` | `%` or the project's own unit | loops with other units |
| `protos` | none | array of `{id, product, feature, project, versions: [{v, status, date, agent, note, look_at, thread, file, decisions, sent_back_reason, pr, token}]}` | agent prototypes to validate or send back |
| `protoError` | none | `null`, or error string if `protos.json` is corrupt or invalid shape | error banner shown on Prototypes view |
| `agents[].machine` | none | machine id where the agent runs (local machine id or remote machine id); remote rows get `canReceive: false` | multi-machine Office support |
| `machines` | none | array of `{id, label, online, lastSeen}` in `office.json` and `/api/office` | switcher for multiple office machines |

## Sources

| Value | Source |
|---|---|
| products | `~/.factory/products.json` |
| projects, plans, loops, next, PRs | `~/.factory/products/<p>/projects/<id>.json` through the CLI |
| DS migration phases, scopes, series, added, open PRs, next | `--source ds-kit`: `UNITS.md` on `origin/feat/ds-migration` (same parsing and status rules as `~/ds-pages/build_progress.py`), its git history for the scope, `gh pr list` for PR states and merge dates, `progress-notes.json` for `next` and status overrides. Phase dates: week *n* starts on 2026-09-28 + 7(n−1), except v2 W1–W4, which FAST-TRACK.md compresses into 2026-10-12 → 10-23 (that file wins for Phase 0); a phase runs from its earliest unit week's Monday to its latest unit week's Friday. "Before the flip" = phases −1, 0, 1, 2 and FLIP (as in `progress.tpl.html`). One deliberate difference from `build_progress.py`: a kit status such as `merged (PR #1081, …)` counts as merged (ds-pages reads the whole cell and shows it as to do). |
| run lane `daily`, `items` | `gh issue list --label <run label>` on the product repos: came in = open at the window start plus created since; closed = closed since the window start |
| agents | T3 `statev2.sqlite`: one row per thread (native subagents through `projection_subagents`, since they have no runs) that is waiting (open runtime request or run status waiting), running, stalled, failed, or done in the last 24 h. Product from the T3 project's workspace path; project from the thread's own link in `thread-projects.json`; else, with no linked ancestor, `--match` on its title or its root thread's title (unchanged); else its own title's `--match`, then the nearest linked ancestor. |
| assign | `DATA.assign` = `{dispatcher: "online"\|"offline", fix, providers, free, pending}`. `dispatcher`: config `assign.dispatcher` is a top-level thread open in T3. `free`: live T3 rows that can receive and have no project (the dispatcher left out). `pending`: `~/.factory/assign-pending.json` asks younger than 24 h, `stale` after 30 min. |
| tokens | Multi-provider stores joined to T3 threads through `nativeThreadRef.nativeId`: Claude (`~/.t3/userdata/usage-scan-cache-v9.json`), Antigravity (`~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations/<id>.db`), and OpenCode (`~/.local/share/opencode/opencode.db`). Input + cache writes + output. Cache reads tracked separately. |
| skills | Registry: `~/.claude/skills/*/SKILL.md`, `~/.agents/skills/*/SKILL.md`, and installed plugins `~/.claude/plugins/installed_plugins.json` (frontmatter `name` and `description`). Purpose groups mapped via `factory/skills_groups.json` (`orchestration`, `review`, `operations`, `browsing`, `design`, `security`, `other`). Triggers extracted from description text. Usage: real skill calls (`name: "Skill"` with `input.skill`, or `<command-name>`) scanned from `~/.claude/projects/**/*.jsonl` modified in last 30 days, mapped to T3 threads via `nativeThreadRef.nativeId` and agent handles via `t3.handles()`. Antigravity and OpenCode skill calls are not recorded in this format and not counted. |
| PR rows | `gh pr list` / `gh pr view`: title, CI from `statusCheckRollup`, status from state, draft and review decision |
| relay outbox | `~/.factory/relay.json` (answers and messages pushed to asking T3 threads via webhook in `~/.factory/.webhook`). States: `queued`, `sending`, `sent` (T3 accepted), `delivered` (confirmed in the session), `failed` (after 3 unconfirmed sends, with Retry), `stale`. |
| autopilot | `~/.factory/autopilot.json`: `{paused, pausedAt, daily_cap, projects: {id: {thread, goal, every_hours, enabled, max_per_day, goal_source, last_nudge_at, history, nudged_on}}}`. `DATA.autopilot` adds one row per project with the thread's live state (from `autopilot.agent_rows`, i.e. `t3.agents()` plus threads it dropped, and `autopilot.open_inbox_threads()`), a state label ("Idle 2h 40m" / "Working" / "Waiting on you" / "Failed: check the session" / "Unknown" / "Thread gone"), the nudge badge, `last_result` + time, `next_due`, and a footer summary (`next_run`, `last_nudged`, `last_skipped`). When T3 cannot be read, `DATA.autopilot.t3Error` is set and every row shows state "unknown". |
| chatscan | `~/.factory/chatscan.json`: `{thread_id: last_filed_message_key}`: records the last assistant message key filed into the inbox for each T3 thread so duplicate questions are not filed. |
| board | `factory/board.py`: one `gh api search/issues` per repo (open issues with label `intake`/`factory`/`needs-decision`/`building` plus `is:issue`, and closed in the last 24 h), each in its own try, and one `gh api graphql` per repo for its PRs' `closingIssuesReferences` (keyed by `repository.nameWithOwner` + number) and merged PRs of the last 24 h. `DATA.board` is `{cards, fetchedAt, error, repoErrors, auto_promote}`. A card is `{id, num, col, repo, repoShort, product, source, title, auto, stuck, pr, url, body}`. A repo whose search fails is recorded in `repoErrors` and skipped; `error` is set only when every repo failed (then the cached cards are returned with `fetchedAt` = the cache time). The last result is cached in `board-cache.json`. `auto_promote` comes from `board.json` (default true, read by the triage agent). |
| protos, protoError | `~/.factory/protos.json`: list of prototypes (`{id, product, feature, project, versions}`). Real files copied to `protos/<id>/v<n>.html`. Each version gets a per-file HMAC token `token` from `store.proto_token()`. If corrupt or invalid shape, `protos` is `[]` and `protoError` is the error string (build warning added, page error banner shown). |
| `machines.json` | `~/.factory/machines.json`: registered machines pushing telemetry: `{<id>: {label, tokenHash, createdAt, lastPushAt, agents}}`. |
| `office.json` | `~/.factory/site/office.json` written by `factory build`: `{"builtAt", "machine": {"id", "label"}, "machines": [{id, label, online, lastSeen}], "products", "projects", "agents", "tokens", "provColors", "relayConfigured"}`. Drops agents in state `done` older than 1 h. |
| `roadmap` | `~/.factory/roadmap.json`: `{"items": [...], "sources": {"<product>": {"syncedAt", "error", "warning"}}, "local": {"<item id>": {"note", "project"}}}`; items carry `updatedAt`. `error` makes the product read-only (Notion unreachable, bad config); `warning` does not (Notion returned no rows: the last copy is kept). `DATA.roadmap` provides `items` (with flags, progress, running agents, late indicator), `flags`, `sources`, `cap`, and `error`. Lanes: `now`, `next`, `later`, `icebox`, `done`. Products with configured Notion sources (`roadmap.sources.<product>`) sync on `factory build` and `factory roadmap sync`. If corrupt or unreadable, `items` is `[]` and `error` is the error message string (build warning added, page error banner shown). |

## Error sweep (state, not page data)

The sweep writes issues onto the board; it is not rendered directly. Its state lives in `~/.factory/sweep.json` and `~/.factory/secrets/<name>`:

- `sweep.json` — `{sentry: {base_url, org, projects: {project slug → owner/repo}}, signoz: {base_url, services: {service name → owner/repo}, default_repo}, min_events, max_per_repo, include_performance, history: [{at, source, swept, filed, skipped, error}]}` (history keeps the last 30 runs).
- `secrets/sentry-token` (read-only Sentry auth token) and `secrets/signoz-key` (SigNoz Viewer API key) — one value per file, mode 600 in a 0700 dir, set from stdin with `factory secret set <name>`, printed by name only.

The sweep files one `intake` issue per error with the `intake` label plus `source:sentry`/`source:signoz`; the board's `SOURCE_LABELS` map (`source:sentry` → "sentry", `source:signoz` → "signoz") and the template's `BOARD_SOURCE` filter (`SigNoz`) surface the source on the Board view.

## Support digest (state, not page data)

The support digest reads customer conversations from Chatwoot and files an `applied` information item into the factory inbox; its state lives in `~/.factory/digest.json` and `~/.factory/secrets/<name>`:

- `digest.json` — `{"enabled": false, "project": "<project-id>", "chatwoot": {"base_url": "https://<host>", "account_id": 1, "inboxes": [1, 3]}, "llm": {"base_url": "https://<gateway>/v1", "model": "<model name>", "usd_per_mtok": 1.0}, "question": "Top 3 customer asks this week", "days": 7, "max_conversations": 300, "max_chars_per_conversation": 1200, "daily_cap_usd": 1.0, "history": [{"at", "conversations", "cost_usd", "result", "error", "posted"}]}` (history keeps the last 30 runs; `posted` marks the run that put the day's digest in the inbox, one per UTC day).
- `DATA.digest` — `{enabled, daily_cap_usd, spend_today, at, result, error, last_run}` built by `factory build` for the Automation view.
- `secrets/chatwoot-reader` (read-only Chatwoot agent user token) and `secrets/llm-key` (OpenAI-compatible gateway API key) — mode 600 in a 0700 dir, set from stdin with `factory secret set <name>`, printed by name only.

Customer conversation text is scrubbed for PII before the model sees it, gateway requests send `"no-log": true` with `metadata: {"job": "factory-support-digest"}`, and ticket text is never stored in history, logs, or exceptions.

## Board view data contract

`DATA.board` drives the Automation → Board sub-tab. Columns are fixed (`intake`, `triage`, `building`, `review`, `waiting`, `merged`); a card's column is the first match of the open-issue rule, and `merged` for a closed issue closed by a merged PR in the last 24 h. `gh pr list --json` has no `closingIssuesReferences` field in the bundled gh, so the PR link reads it through GraphQL instead (each closing reference is keyed by repo + number, so a PR in one repo that closes an issue in another links to the issue, never to a same-number issue in the PR's repo). Writes go through `POST /api/board` (promote / close / send_back / auto_promote / refresh), `GET /api/board` returns the cached board, and the timeline through `GET /api/board/timeline`; after a write the cached card is updated so the next page load shows the new column.

## Tokens view data contract

`DATA.tokenHistory` contains aggregated token telemetry and charts data across three windows: `24h`, `7d`, and `30d`.
`DATA.agentTokens` contains the 24 h per-agent token breakdown table rows without a `windows` map (each window's table rows are stored in `DATA.tokenHistory[window].agents`).

### Counting rules and sources per provider

All providers follow the exact same counting rule:
**Total Tokens = Input tokens + Cache write tokens + Output tokens**
Cache read tokens are tracked and displayed separately, never added into the total.

| Provider | Data Source | Details & Counting |
|---|---|---|
| **Claude** | T3 usage scan cache (`~/.t3/userdata/usage-scan-cache-v9.json`, read-only) | Per-session token records (`input_tokens`, `cache_write_tokens`, `cache_read_tokens`, `output_tokens`). Joined to T3 threads via `nativeThreadRef.nativeId`. |
| **Antigravity** | Per-conversation SQLite databases (`~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations/<id>.db`, `mode=ro`) | Table `gen_metadata` (data protobuf BLOB decoded via stdlib varint wire parser). Tag 1 -> sub-message tag 4 (`UsageMetadata`): tag 2 is the uncached prompt (input), tag 3 `candidatesTokenCount` (output, tag 9 thoughts excluded), tag 5 `cachedContentTokenCount` (cache read). Tag 9 -> 4 -> 1 provides epoch seconds timestamp; tag 21 provides the model name. Joined to T3 threads via `nativeThreadRef.nativeId`. |
| **OpenCode** | OpenCode SQLite database (`~/.local/share/opencode/opencode.db`, `mode=ro`) | Tables `session_message` and `message` (de-duplicated) with `time_created` (epoch ms) and `data` JSON containing `modelID` and `tokens` object: `input` (input tokens), `output` + `reasoning` (output tokens), `cache.write` (cache write tokens), `cache.read` (cache read tokens). Joined to T3 threads via `nativeThreadRef.nativeId` matching session id. |

### Cost estimation

Estimated cost is calculated from a per-model pricing table in USD (`factory/tokens.py`), based on published per-million input, output, cache-read, and cache-write rates.
- If a model is not listed in the pricing table, its cost is `null` and displayed as `"—"`.
- EUR cost is converted using a fixed exchange rate constant: `EUR_USD_RATE = 0.92` (1 USD = 0.92 EUR).

## Office data contract

`site/office.json` is generated by `factory build` alongside `site/factory.html`. It contains:
- `builtAt`: ISO timestamp of the build.
- `machine`: `{"id", "label"}` identifying the host machine (from config `machine.id`/`machine.label` or hostname defaults), preserved for backwards compatibility.
- `machines`: array of `{id, label, online, lastSeen}`. Starts with the local machine (`online: true`, `lastSeen: builtAt`), followed by registered machines from `machines.json` (`online: lastPushAt within 3 minutes`, `lastSeen: lastPushAt`). With no registered machines, `machines` contains one entry and the client shows no switcher.
- `products`: product definitions.
- `projects`: project definitions and metrics.
- `agents`: agent rows identical to the dashboard page, each tagged with `machine: <id>` (local machine id or remote machine id). Remote rows have `canReceive: false`. Agents in state `done` older than 1 hour are dropped (applies to remote rows too). Offline machines contribute no agents.
- `tokens`: token telemetry snapshot.
- `provColors`: provider color mappings.
- `relayConfigured`: boolean indicating if webhook relay is configured.

`~/.factory/machines.json` stores registered extra machines:
`{<id>: {"label": <label>, "tokenHash": <sha256>, "createdAt": <iso>, "lastPushAt": <iso|null>, "agents": [...rows...]}}`.

`POST /api/machines/<id>/agents` accepts JSON `{"agents": [...rows...], "sentAt": <iso>}` from extra machines, validates `X-Factory-Machine-Token` against the stored hash, validates up to 300 rows keeping known agent keys cut to 500 characters, sets `canReceive: false` and `machine: <id>`, updates `lastPushAt` with the server timestamp, and returns `{"ok": true, "accepted": N}`.

`GET /api/office` serves `site/office.json` merged dynamically with the latest `machines` online status and remote agent rows from `machines.json`, plus live `inbox` items and `answers`.
`officeInstalled` in `DATA` is a boolean indicating whether `site/office.html` is installed, enabling the Office button.



## Roadmap view data contract

`DATA.roadmap` drives the Roadmap view. Lanes are `now`, `next`, `later`, `icebox`, and `done`. Now holds at most `roadmap.cap` items (default 7). `DATA.roadmap.items` provides:
- `id`: unique item id (`FEAT-N` for Notion rows or `<product>-<slug>` for local items).
- `product`: product key.
- `title`: title (with `P<n> — ` prefix stripped; re-added in Now when rank_prefix is enabled).
- `lane`: current lane (`now`, `next`, `later`, `icebox`, `done`).
- `rank`: 1..n order within lane (P1..Pn in Now).
- `due`: optional target launch date (`YYYY-MM-DD`).
- `note`: optional context / definition of done.
- `project`: optional linked factory project id (auto-linked when project `feat` matches `id`).
- `done`: completion date (`YYYY-MM-DD`) when in Done.
- `prog`: `[merged, total]` units from linked build project, or `null`.
- `agents`: count of running/waiting/stalled agents on the linked project.
- `late`: boolean true if `due < today` and lane is not `done`.
- `flag`: mismatch flag (`building` when outside Now/Done with active agents; `finished` when build plan is 100% merged and lane is not Done).
- `flagTxt`: explanation text for the mismatch flag.
- `source`: `"notion"` or `"factory"`.
- `sourceId`, `sourceStatus`, `url`: Notion page id, status name, and URL.

Sources configured in `config.json` under `roadmap.sources.<product>` sync from Notion databases via the Notion API (`Notion-Version: 2022-06-28`) with token from `secrets/notion-token`. Stale/unreachable sources mark `DATA.roadmap.sources[product].error` and show the product read-only.
