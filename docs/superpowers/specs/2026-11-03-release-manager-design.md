# Daily release manager: design

Date: 2026-11-03. Session `release/feat/daily-release-manager`. Factory project `fac-release-mgr`.

## Why

Features merge to `develop` and then stay there, because nobody remembers to cut the release.
The owner wants an automation that runs every morning and, for each product, decides whether to
release now, wait, or cherry-pick part of `develop`. They also want it to judge risk, review the
release, ship it, watch production afterwards, and roll back when something breaks. A release
that is safe to ship should reach production without anyone having to think about it.

## Decisions the owner made

Each line comes from a factory inbox answer (2026-11-02 to 2026-11-03).

1. **Fully automatic (level 4).** It ships, cherry-picks and rolls back without asking, then reports
   what it did. The owner does not approve individual releases.
2. **It applies every prod migration itself** (inbox `d256`, option c). That includes PlanetScale
   prod deploy requests for the Web monorepo, which the owner clicks today.
3. **Every product from day one** (inbox `d265`, option c).
4. **Pace** (inbox `d270`, option a): at most 1 release per product per day, Monday to Thursday, from
   a morning run. On Friday it ships hotfixes only. Nothing ships on Saturday or Sunday.
5. **A `no-release` label holds a PR** (inbox `d290`, option a). A labelled PR never ships until the label
   is removed. Everything else on `develop` counts as shippable. A held PR that later PRs depend on is
   handled like a red PR: the others ship by cherry-pick, and any whose cherry-pick conflicts wait.
6. **Factory releases itself** (inbox `d291`, option b). Factory deploys last each day, after every
   other product's watch window has ended. The `~/factory-live` rule is lifted for this step only.

## Goals

- Every finished PR on `develop` reaches production within 1 working day, unless the release manager
  holds it for a reason it states.
- Each PR gets a green, yellow or red risk score, and the score decides how the PR ships.
- Each release gets the release-risk review that `release-skill` describes, and the review must pass
  before the merge.
- Production is watched after each release. A failed watch rolls the release back automatically.
- Every action is visible on the factory dashboard: what shipped, what was held and why, and what was rolled back.

## Non-goals

- New canary or traffic-splitting infrastructure. v1 uses the deploy path each product has today.
- A feature-flag system.
- Replacing the per-PR review loop. Each feature PR still gets its own `/adversarial-pr-review`. The
  release review only covers what crosses PRs: interactions between them, migrations, env vars and deploy wiring.

## Anti-goals (a failure even if it runs)

- A silent action: a ship or rollback with no dashboard entry.
- A release so large that a failed watch cannot name the PR to blame.
- A migration the code rollback cannot survive. A release whose code expects the old schema must
  never sit on top of a dropped column.
- A Friday afternoon or weekend feature release nobody is watching.
- The owner's inbox filling up with items that only report information. Inbox items are for things they
  must act on.

## The rule change this needs

Current rules block level 4 in these places. The build must change each one, limited to the release
manager, in the same PR as the code:

- `~/.claude/skills/release-skill/SKILL.md` hard rule 1 ("merging needs the owner's go typed in this
  session") and flow A step 5 ("the owner clicks Deploy").
- `~/.claude/CLAUDE.md`: "prod, money, secrets and merges still need the owner's approval". Add an
  exception for release-manager runs, citing this design.
- `release-skill` hard rule 3 ("never push to main, staging or develop"): add two allowed pushes,
  both without force. The first is the merge of `main` back into `develop` after a cherry-pick release.
  That push is a fast-forward of `develop`, like the existing staging mirror. The second is the rollback
  revert commit on `web-infra` `prod`, which `cicd.yml` already writes to for every deploy.
- The PR review policy: a release PR gets the release-risk review, not the full
  `/adversarial-pr-review` loop, because it carries no new code. `release-skill` already works this
  way. Each feature PR inside the release had its own loop, and the release manager checks for its
  `factory/review` status.
- The PreToolUse merge hook stays as it is. The release manager earns the `factory/review` status on
  the exact head SHA, like any agent.
- `~/.claude/CLAUDE.md` factory dashboard policy ("never touch `~/factory-live`, port 3045 or its cron
  lines"): add an exception for the release manager's Factory deploy step only (section 9). The cron
  lines stay off limits.
- `factory/autopilot.py` `SENSITIVE_RE` stays as it is. The release manager is not autopilot and does
  not go through it.

## Products and their release paths (recon 2026-11-03)

| Product | Repo | Release today | Migrations | Deploy | Rollback |
|---|---|---|---|---|---|
| Web monorepo | `acme/web` | release branch develop→main (`release-skill` flow A) | PlanetScale prod DR opened by workflow on main push | `cicd.yml` → `web-infra` image tag → ArgoCD | revert the image-tag commit in `web-infra` prod, ArgoCD sync (about 5 min) |
| Web admin | `acme/admin` | gitflow `release/x.y.z` (flow B) | none | Vercel on main | Vercel rollback to the previous prod deployment (seconds) |
| App | `acme/app` | PR `develop`→`main`; `auto-release.yml` and `release-please.yml` handle tags and notes | `migrate-production.yml` runs `prisma migrate deploy` on main push | Vercel deploy hook after migrations | Vercel rollback |
| Store | `acme/store` | PR `develop`→`main` (e.g. #282) | Supabase (`supabase/`) | Vercel project `store-front` | Vercel rollback |
| Factory | `acme/factory` | main only, no develop | none | `~/factory-live` checkout + restart (README "Deploy a change") | check out the previous SHA in `~/factory-live`, restart |

App already scores PR risk in CI (`risk-review.yml` using `.claude/incidents/patterns.yml`).
The release manager reads that result instead of re-deriving it. Factory is in v1 (section 9). `app-ui` and `streamer`
have no develop-to-main flow, so they stay out of v1.

## How it works

### 1. Morning run (plain Python, no LLM)

A new `factory release` command group in this repo. Cron runs `factory release plan` at 07:30
server time, Monday to Friday. For each product it:

1. Lists the PRs merged into `develop` and not yet on `main`, with merge date and labels.
2. Scores each PR (section 2).
3. Checks pacing and holds (section 3).
4. Writes a verdict with its reasons to the factory store: `ship-all`, `cherry-pick`, `solo-red`, or `wait`.
5. When the verdict ships something, relays the plan into the product's release thread (section 4).

`factory release status` prints today's verdicts. `factory release pause [product]` and
`factory release resume [product]` are the kill switch: a paused product gets a verdict but no
relay. The dashboard shows the verdicts and the pause state.

### 2. Risk score per PR

- **Red:** touches a money path (payouts, wallet, fees, payments, checkout), adds a migration,
  touches auth, or changes infra/Helm/deploy workflows.
- **Yellow:** diff over 400 changed lines, no test file changed, a new env var, or a dependency bump.
- **Green:** everything else.

A release carries at most 10 PRs, the oldest first. The rest wait for the next day, so a failed watch
points at 10 PRs at most.

A release's score is its worst PR's score. If a release carries 4 or more yellow PRs, it counts as red.
Path patterns live per product in config, so each repo lists its own money and auth paths.
App's `risk-review` result counts as an extra input: a critical match there makes the PR red.

### 3. Verdict and pacing

- **wait:** no unreleased PRs, CI red on `develop`, the product is paused, a release already went out
  today, it is Saturday or Sunday, or a rollback happened in the last 24 hours.
- **ship-all:** every PR is green or yellow, and the release counts as yellow at worst.
- **solo-red:** one red PR, shipped alone. Allowed Monday to Thursday only, and it starts at the morning
  run, not later in the day. With 2 or more red PRs, it ships the oldest one alone and holds the rest.
- **cherry-pick:** red PRs are present and cannot ship today (Friday, or another red already chosen).
  It cuts a release branch from `main` and cherry-picks the green and yellow PRs. A PR whose
  cherry-pick conflicts is dropped from the release and waits for the next one.
- **`no-release` label:** the PR is left out of every verdict until the label is removed.
- **Friday:** only PRs labelled `bug`/`fix` that score green. Everything else waits until Monday.

### 4. The release run (Claude agent)

Each product has one T3 thread named `{product}/chore/release-manager`. The morning run pushes the
plan into it through the existing relay. Per the workhorse policy, this agent is Claude: it touches
prod, merges and migrations. It follows a new `release-manager` skill, generalised from
`release-skill` with one section per product:

1. Cut the release branch: the full release, the cherry-pick set, or the solo red PR.
2. Run the release-risk review (the `release-skill` checklist). Add a whole-release pass for PRs that
   break each other. A blocker means no `factory/review` status and no ship: report it and stop.
3. Merge the release PR, pinned to the reviewed head SHA.
4. Apply migrations: approve the PlanetScale prod DR, or let `migrate-production.yml` or the Supabase
   step run. A failed migration stops the code deploy and goes to the inbox as `urgent`.
5. Verify the deploy (the image SHA on every deployment, or Vercel Ready), then start the watch.
6. Watch (section 5), then promote (mark Released) or roll back (section 6).
7. Report: `factory update <project>`, a Journal line on each touched roadmap row, the release notes.

After a cherry-pick release, `develop` still holds the held PRs. The agent merges `main` back into
`develop` (a merge commit, never a force-push) so the next release diff stays clean.

### 5. Watch window

30 minutes for green and yellow releases, 60 minutes for a solo red release. It checks every 5 minutes
and compares against the same window the day before:

- Health endpoints return 200 (e.g. `ba9chich.com/payments/api/health`, home pages).
- The error count (`vercel logs --level error`, or pod logs and restarts via `kubectl`) is under 2x the baseline.
- No pod restart loop and no ArgoCD `Degraded` state (monorepo).
- Business signals: successful payments, payouts and signups per window are not below 50% of the baseline,
  checked only when the baseline has at least 10 events.

### 6. Rollback

A watch fails on 3 failed health checks in a row, or on an error or business signal outside its limit
at 2 checks in a row. Then the agent:

1. Rolls back code (per product, table above). It does not touch the migration. Because the review
   rule below guarantees the previous code works on the new schema, the rollback is safe.
2. Confirms production is healthy again with the same checks.
3. Sets a 24-hour hold on the product, files a GitHub issue with the evidence, and posts an `urgent`
   inbox item. A rollback is the one event the owner always hears about directly.

If the rollback itself fails, it stops acting on that product, pauses it, and posts `urgent`.

A rollback only changes what runs in production. The bad code is still on `main` and `develop`, so
the product stays held until the problem is removed from `develop`:

1. The agent opens a revert PR on `develop` for the PRs in the failed release. That PR goes through the
   normal per-PR review loop, and the release manager may merge it.
2. The hold lifts when the revert, or a fix PR linked to the rollback issue, is on `develop`. The 24-hour
   minimum still applies.
3. Vercel products stop auto-promoting `main` after an instant rollback, so the next release run promotes
   its deployment explicitly.

### 7. Migration safety rule

Migrations are applied automatically, so the review holds any release whose migration drops, renames,
or narrows a column or table the code currently on `main` still reads. Such changes ship in two
releases: first the code stops using the column, then a later release drops it. This rule is
Claude's call, not the owner's. It exists because a code rollback cannot undo a migration.

### 8. Reporting

- One `factory update` per release and per rollback.
- One daily line per product on the dashboard: the verdict and its reasons, including `wait` days.
- Inbox items (`urgent`) only for a rollback, a failed migration, a review blocker, or a failed rollback.
- An unreleased PR older than 3 working days with a `wait` verdict raises one `applied` inbox item
  naming the reason. That covers the original "we forgot" problem when automation is not enough.

### 9. Factory's own release

Factory has no `develop`. Its unreleased set is the commits on `origin/main` that `~/factory-live`
does not have yet. These are already reviewed and merged PRs, so there is no release branch or
release PR. The same scoring, pacing and `no-release` rules apply to the PRs behind those commits.

1. **Order.** The Factory step runs only after every other product's release run of the day has
   finished its watch and reported. Deploying Factory restarts the server and swaps the code that the
   cron, relay and `factory release` run from, so nothing else may be in flight.
2. **Deploy.** Follow README "Deploy a change": check out `origin/main` detached in `~/factory-live`,
   set any new config keys, run `factory build`, stop the server on 3045, wait for the port to free,
   then run `deploy/watchdog.sh`. Record the previous SHA first.
3. **Watch, 30 minutes.** `factory doctor` passes, the dashboard answers through the server on 3045,
   `factory build` succeeds, and the next scheduled cron runs (build, relay retry) log no errors.
4. **Rollback.** Check out the recorded previous SHA in `~/factory-live`, then repeat the restart steps.
   A failed Factory rollback may leave the relay down, so the agent also writes the `urgent` item
   to today's vault daily note.

Factory's watch runs from the release thread, not from cron. It therefore does not depend on the code
it just deployed.

## Dashboard part follows prototype-first

The dashboard additions (daily verdict per product, held PRs with reasons, pause state, release
journal) are a feature. Per the prototype-first policy, the build starts with a clickable HTML
prototype of that view, and the owner validates it before any dashboard code. The CLI, cron and skill
work can proceed in parallel.

## Left to the builder

- Exact path patterns per product for the risk score.
- Exact health URLs and business-signal queries per product, and where their baselines come from.
- How T3 threads are created and found for each product, reusing `relay` and `t3`.
- Release notes format, and whether App keeps `release-please` or the release manager takes over its notes.
- The store schema for verdicts and the release journal, following `store.py`.
- The timezone of the 07:30 run (the owner is in Tunisia, UTC+1).

## Claude's calls (check these)

- The migration safety rule (section 7).
- The scoring thresholds: 400 lines, 4 yellows count as red, the 2x error rate, the 50% business drop.
- Watch windows of 30 and 60 minutes.
- A 24-hour hold after a rollback.
- `app-ui` and `streamer` stay out of v1.
- One T3 thread per product, and Claude, not a workhorse, runs releases.
- At most 10 PRs per release.
- Launch in `shadow` mode: verdicts only, nothing relayed, for one week. Then each product switches to
  `live` by hand, starting with `admin`, the fastest to roll back.
- Watch decisions use a deterministic `factory release check` command, not the agent's judgment.
- After a rollback: a revert PR on `develop`, and the hold lasts until it lands.
- The two new allowed pushes, and the release-risk review standing in for the full loop on release PRs.

## Develop queue (added 2026-11-08)

Features sometimes stay in open PRs and never reach `develop`. The release manager now also runs the
step before the release: it reads every open PR, decides the merge order, merges the ready ones,
reminds the owner about the ones that wait on them, and sends agents to fix the broken ones.

Prototype: `factory-release-manager-develop-queue-merge-order-reminders-fix-agents`, v4 validated by
the owner on 2026-11-08 (design shotgun: layout A, "train + grouped list", plus the pipeline band). The
build follows that prototype; this section writes down what it decided.

### Decisions the owner made

1. **The release manager merges PRs into `develop`** and decides the order (chat, 2026-11-08).
2. **Features need owner OK** before they merge. Fixes and chores merge without asking.
3. **Dependency bots are batched weekly**: up to 5 green patch or minor bumps, merged on Wednesday.
4. **The view is layout A plus a pipeline band**: one row per product across Open PRs, On develop,
   Releasing and In production, then the week train (Mon to Thu), then the PR list grouped by status.

### What each open PR gets (one status, first match wins)

| Status | Rule | What the release manager does |
| --- | --- | --- |
| Wrong base | base is not `develop` (`main` for Factory) | asks the owner: retarget or close |
| Bot, red or conflicting | dependabot PR with failing checks or conflicts | waits for Dependabot's rebase; if `develop` itself is red, waits for that fix |
| Bot, major bump | `from vX` to `vY`, X differs from Y | lists it for the owner, never merges it alone |
| Bot, ready | green patch or minor bump | Wednesday batch, at most 5 |
| Stale | conflicts or red CI, and idle over 60 days | asks the owner: revive or close |
| Conflicts | `mergeable` is `CONFLICTING` | rebase agent |
| Red CI | failing checks | fix agent (or waits when `develop` is red) |
| Stale docs | title starts with `docs`, idle over 60 days | asks the owner: merge as is or close |
| Needs review | no `factory/review` success on the head commit | review loop agent |
| Feature, ready | title starts with `feat`, CI green, review clean | merges on its train day once the owner says OK |
| Ready | CI green, review clean, no conflicts | merges on its train day |

`develop` counts as red when a product has 5 or more open dependency PRs and over 80% of them fail.
Then the product's train is blocked: one fix agent works on `develop` and nothing merges until it is green.

### The week train

- Monday to Thursday, per product: ready fixes merge in batches of 3, oldest first; each feature
  merges alone on its own day; a PR in review can join a later day that has room and no feature
  (shown as conditional).
- A PR the owner moves to the front goes first. A held PR is skipped until unheld.
- **Merges happen after the day's release**, so a PR always sits on `develop` for a day before it
  ships: after the release's watch window ends, or at the morning run when that day has no release.
  (`release-branch` and `pr` flows read `develop` while the release is open, so merging earlier would
  slip unreviewed code into it.)
- Factory has no `develop`: its existing flow already merges PRs into `main` at the 11:00 and 14:00
  runs, so the queue only shows Factory and does not merge for it.
- Every merge re-checks the PR right before it: CI green, review status success, mergeable, no
  `no-release` label, not held, feature OK present. A failed re-check moves the PR back to its group.
- A product in `shadow` mode gets the queue and the train, and merges nothing.

### Agents

- At most 3 agents a day per product (rebase, fix, review loop together), newest work first; the
  rest show as queued. Agents follow the workhorse policy, push to the PR branch, and never merge.
- The owner can send one now from the dashboard ("Send agent"); it counts toward the day's 3.

### Reminders and actions

- The dashboard is the primary place. PR actions: OK to merge (features), Merge first, Hold,
  Send agent, Retarget, Close (close leaves a comment linking the queue).
- The dashboard records the action; the next release-manager run applies the GitHub writes
  (retarget, close). OK, front and hold only change the plan.
- One inbox item per product per day, only when something new waits on the owner, linking the queue.

### Production gate (from the v4 data)

On 2026-11-08 web's `errors-dashboard` check failed with no release out (8 errors/h vs a baseline
of 1). A watch that starts on an already failing check would blame the next release and could roll it
back. So the morning run runs `factory release check` first: a product whose production checks
already fail gets `wait` ("production already failing <check>"), and the queue still merges into
`develop` as usual.

### Claude's calls (check these)

- Batches of 3, 3 agents a day, 60 days for stale, the 80% rule for a red `develop`.
- Merge after the day's release, not at 07:30.
- Feature detection by the `feat` title prefix.
- One inbox item per product per day for the queue.
- The production gate holds the release, not the queue. It runs only when the day would ship, needs two
  bad samples in a row, counts a broken probe (expired token) as failing because the watch would roll
  back on it, and posts one urgent inbox item per product per day.
- Dependency bumps merge without the review loop: green CI, single bump with versions in the title on a 1.x or later major (a 0.x minor step takes the review loop), head commit written by the bot (GitHub-signed); grouped bumps and bot PRs with pushed commits take the review loop; the release manager posts the `factory/review` status itself right before the merge.
- One action per product per day: once the queue message went out, that day's verdict is frozen; a manual rerun cannot add a release the same day.
- A release that ends with no `shipped`, `rolled-back` or `rollback-failed` record keeps that product's queue
  closed for the rest of the day (fails safe). Recording the outcome (`shipped` or `rolled-back`) reopens it; otherwise the next day does.

## Release comms (added 2026-11-10)

The release manager also takes part in the communication part of the cycle. It writes a changelog,
a weekly what's-new digest, a short social video made with the brag tool, and the emails that
announce them. Prototype `factory-release-manager-release-comms-changelog-what-s-new-digest-brag-video-emails`,
validated by the owner on 2026-11-10. The build follows that prototype.

### Decisions the owner made

1. **Rhythm** (option a): a changelog at every release; every Friday one what's-new digest
   email and one brag video per product; a big feature gets its own email and video the day it ships.
2. **Audience** (option b): all three products from day one: App, Web, Store.
3. **Approval** (option b): small updates send on their own; the owner approves every big
   feature's email and video in their inbox before it goes out (Send now / Edit first / Skip this one).

### Rules (from the prototype review, so comms stay true)

- Only PRs in production count. Merged to `develop` is not shipped.
- Internal PRs never reach users: titles starting with chore, ci, build, docs, test or refactor; admin
  dashboards; CLIs; agent and ops tooling; dependency bumps; any PR labelled `no-comms`. They appear only
  in the changelog's Internal group.
- Every user-facing sentence is backed by its PR's body, and the draft shows which PR each sentence comes
  from. Nothing the PR does not state is claimed.
- A change scheduled for a later date (a sale, a launch) is announced on or after that date, never before.
- The audience is chosen per change: creators, brands or customers (App #1468 went to brands).
- Recipient counts are read from the product's database at send time, never estimated.
- Big = a PR linked to a roadmap FEAT row, or labelled `big`. Everything else is small.
- Friday send time: 10:00 Tunis.

### Claude's calls (check these; the prototype showed them as recommended)

- A big feature that already had its own email appears in Friday's digest as a one-line recap.
- An unapproved big feature is held everywhere, including the Friday digest; the rest of the digest goes out.
- Language: Web and Store in French first with English fallback; App in English first with French fallback.
- The `no-comms` label is supported.
- If the brag video render fails, the send waits up to 2 hours and alerts; then the email goes out with a static card instead of the video.

### Before the build

- App sends through its SendGrid broadcast flow today. Web and Store need a sender and a
  recipient list first (to be found at build time, asked in the inbox if it needs the owner).
- Sends are outward-facing: the first live send of each product goes through the owner's inbox even when it
  is "small", so they see the real email once.
