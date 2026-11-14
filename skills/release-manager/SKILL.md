---
name: release-manager
description: Follow the daily release manager plan for a product. Triggers on a message starting "Release manager (factory release plan", "Release manager (factory release queue" or "Release manager (factory release comms", or when asked to run a release-manager release. Covers branch cutting, release-risk review, status gates, merging, migrations, deploy verification, the health and signal watch, rollback and reporting for every product registered with `factory release product add`.
---

# Release manager

Automated daily release execution for factory products. Design: the factory repo's
`docs/superpowers/specs/2026-11-03-release-manager-design.md`; commands: README "Release manager".

The generic flow is below. Steps that differ per product (migration tool, deploy target, deploy
check, rollback command, extra allowed pushes, comms sender) come from the product's release guide:
`RELEASE.md` at the root of the product repo on `main`, read in Step 0. The `factory` flow needs no
`RELEASE.md`: its steps are in "Releasing the factory itself".

## Hard rules

1. **Shadow mode never merges.** Read `mode` from `factory --json release status <product>`. In
   `shadow`, cut the release branch, run the release-risk review, post the review comment and the
   `factory/review` status, then stop and report `shadow: stopped before merge`.
2. **Live mode merges without a typed go from the owner.** That is the point of `live`.
3. **Status gates.** Run `factory release status <product> --json` before the merge, before
   migrations and before the deploy. Stop if `paused` is true or `hold` is not null. If a gate trips
   after the merge, record `factory release record <product> held --note "<exact step to resume>"`,
   post `factory ask --project <project-id> --kind urgent` naming that step, and stop; resume that
   step when the owner messages the thread after `resume`/`unhold`.
4. **Lifecycle recording** with the CLI:
   - `factory release record <product> started --pr N ... [--sha S]` before cutting the branch;
   - `... shipped --pr N ... --sha <merge-sha>` after a clean watch;
   - `... rolled-back --note "<issue url>"` on rollback;
   - `... rollback-failed --note "<details>"` if rollback fails;
   - `... held --pr N ... --note "<reason>"` when holding PRs.
5. **Review status.** Every merge needs a `factory/review` status on the exact head commit:
   `gh api repos/<owner>/<repo>/statuses/<head-sha> -f state=success -f context=factory/review -f description="Release review clean, 1 round"`.
6. **Pushes.** Never push directly to `main`, `staging` or `develop`, and never force-push. Allowed
   non-force pushes: the release branch, a revert branch, the merge of `main` back into `develop`
   (Step 6 and Rollback step 3), and extra pushes the product's `RELEASE.md` lists, which may only
   be non-force fast-forwards to `develop` or `staging`, never `main`.
7. **Never print secret values.** Read env files and deploy values by key name only.
8. **Isolated worktrees.** Work in a detached worktree under `$CLAUDE_JOB_DIR/tmp` (or a temp dir),
   never in a primary checkout. Remove it at cleanup. The factory's live checkout (`~/factory-live`
   in the README) is touched only in "Releasing the factory itself".
9. **Migration safety rule** (section below). When it trips after the cut, recut without that PR
   and never post `factory/review` on the old head.
10. **Cut from `baseSha` only on `ship-all`.** The release branch is cut from the plan's
    `Cut from: <baseSha>` line (not `origin/develop`) only when the verdict is `ship-all`, which
    means nothing is held. If any PR is held (no-release label, 10-PR cap, Friday filter,
    cherry-pick conflict), the shape is `cherry-pick` or `solo-red`.
11. **Every `factory ask` takes `--project <project-id>`**, read from the `project` field of
    `factory --json release status <product>`.

## The release run

### Step 0: Plan freshness
1. Read `<date>` in the plan header (`Release manager (factory release plan, <date>):`).
2. If `<date>` is not today in the release clock's time zone (`factory/release.py`), stop and
   reply `stale plan from <date>; waiting for today's run`.
3. Read today's verdict from `factory --json release status <product>`. If it is `wait` (or `ship`
   is empty), stop with `wait: <reasons>`. Record nothing, cut nothing.
4. Any flow except `factory`: read `RELEASE.md` on the product's `main`. Missing: post
   `factory ask --project <project-id> --kind action` asking the owner to add one, and stop.
   Record nothing, cut nothing (a `started` with no end blocks the factory's own release all day).

### Step 1: Record started and cut the release branch
1. `factory release record <product> started --pr <numbers...>`.
2. Run the pre-cut checks from `RELEASE.md` (for example: staging migrations already applied).
3. Shape from the verdict:
   - `ship-all`: every unreleased PR, cut from `baseSha`, then merge `origin/main` into it.
   - `solo-red`: that one PR, cherry-picked onto `origin/main` (same recipe as cherry-pick).
   - `cherry-pick`: new branch from `origin/main`, `git cherry-pick -m 1 <merge sha>` per approved
     PR in mergedAt order. On a conflict: `git cherry-pick --abort`, drop the PR, record
     `factory release record <product> held --pr <num> --note "cherry-pick conflict"`, continue.
     `record shipped --pr` lists only the PRs actually merged.
4. Open the release PR into `main` (`release: develop → main (YYYY-MM-DD)` or `release: x.y.z`).

### Step 2: Release-risk review
1. Check the diff against `main`:
   - `ship-all`: `git diff --stat origin/develop` is empty; cherry-pick: matches the picked commits.
   - Nothing dropped from `main` (hotfixes merged straight to `main`).
   - New env vars and secrets: production config has them, by name.
   - Migrations: schema diff, unique timestamp prefix, no data changes, migration safety rule.
   - Money and auth paths.
   - A change to `RELEASE.md` is a release-risk item: list it in the review comment.
   - Whole release: the PRs do not break each other.
2. Post the review comment on the PR.
3. Post the `factory/review` status on the exact head commit (hard rule 5).
4. Shadow mode: report `shadow: stopped before merge` and stop.

### Step 3: Status gate and merge
1. Status gate.
2. Run the merge prechecks from `RELEASE.md` (for example: the migration tool is logged in). On
   failure, `factory ask --project <project-id> --kind urgent` and stop before merging.
3. Merge pinned to the reviewed head:
   `gh pr merge <pr-number> -R <owner>/<repo> --merge --match-head-commit <head-sha>`

### Step 4: Status gate and migrations
1. Status gate (on a trip: hard rule 3 with note `step 4 (migrations)`).
2. Apply migrations as `RELEASE.md` says, and confirm they completed.
3. A failed migration: stop the code rollout, post
   `factory ask --project <project-id> --kind urgent --title "Migration failed on <product>"`, and
   record `factory release record <product> held --note "migration failure"`.

### Step 5: Status gate and deploy verification
1. Status gate (on a trip: hard rule 3 with note `step 5 (deploy verification)`).
2. Verify the deploy as `RELEASE.md` says (for example: pods run image `sha-<merge-sha-7>`, or the
   hosting provider's production deployment is ready and smoke routes return 200). The factory
   itself: section "Releasing the factory itself" below.

### Step 6: Watch window
1. Run the watch as a BACKGROUND command (section "Watch window"):
   `factory --json release watch <product> --minutes 30` (`--minutes 60` for `solo-red`).
2. On exit:
   - **0:** `factory release record <product> shipped --pr <numbers...> --sha <merge-sha>`. After a
     cherry-pick or solo-red release, merge `main` back into `develop` without force:
     ```bash
     W=$CLAUDE_JOB_DIR/tmp/dev-sync-$(date +%m%d)
     git -C <repo> fetch -q origin develop
     git -C <repo> worktree add --detach $W origin/develop
     git -C $W merge --no-edit origin/main
     git -C $W push origin HEAD:refs/heads/develop
     git -C <repo> worktree remove --force $W
     ```
   - **3:** read `kind` (`http` or `signal`) from the JSON and roll back now (section "Rollback").
   - **1 (bad config):** post `factory ask --project <project-id> --kind urgent --title "Release watch failed configuration on <product>"`. Do not record shipped, do not roll back.
   - **Anything else (for example 143, killed):** post an urgent ask "Release watch exited unexpectedly on <product>". Do not record shipped, do not roll back.

### Step 7: Reporting
1. `factory update <project> --health on-track --agent <handle> "Shipped <product> release: PRs <numbers...> (SHA <merge-sha>)"`
2. Release comms (section below) when the product has a comms config.
3. The inbox is for urgent blockers and rollbacks only; a normal release posts no ask.

## Develop queue (open PRs into develop)

The morning plan either appends a "Develop queue" section to the ship message or sends its own
message starting "Release manager (factory release queue". `factory release queue-today <product> --json`
always gives the current list.

1. **When:** on a ship day, only after the watch ended clean and `shipped` is recorded. On a
   no-ship day, right away. Never Friday to Sunday. Never for the `factory` flow (it merges into
   `main`). Shadow mode: report only. One queue run per product per day.
2. **Status gate**; stop if `paused` or a `hold` is set.
3. **Merges, one at a time, in the listed order:**
   - `factory --json release queue-recheck <owner/repo#n>`; exit 3 means skip it and note the reason.
     Keep its `head_sha`: the only commit the gate checked.
   - Dependency bump (reason says "bot"): post the review status yourself on that sha, then merge:
     `gh api repos/<owner>/<repo>/statuses/<head_sha> -f state=success -f context=factory/review -f description="Dependency batch: green CI, patch or minor"`.
   - `gh pr merge <n> -R <owner/repo> --merge --match-head-commit <head_sha>`. Never `--admin`.
   - `factory release queue-done <owner/repo#n> merged`.
   - Wait for develop's CI on the new commit before the next merge; red means stop for the day.
4. **Agents (at most 3, as listed):** dispatch each to a worker agent: rebase = rebase on develop and
   push to the PR branch; fix = make CI green; review = run your PR review loop; `develop-fix` =
   make develop's CI green in a new PR. Agents never merge. Record each with
   `factory release queue-done <owner/repo#n> agent-sent --note "<task link>"`
   (develop fix: `factory release queue-done <product> agent-sent --develop-fix --note "<task link>"`).
5. **Pending dashboard actions:** retarget
   (`gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -f base=develop`; `gh pr edit` fails on repos
   with classic project cards) and close (`gh pr close <n> --comment "Closed from the factory develop queue"`),
   then `factory release queue-done <ref> retargeted|closed`.
6. **Waiting on the owner:** only when `asks_new` is not empty, one `factory ask --project <project-id> --kind action`
   listing those items (OK a feature, retarget, revive or close).
7. **Report** in the thread: merged, skipped with reasons, agents sent, actions applied.

## Release comms (changelog, what's-new emails, video)

For products registered with `--comms-*` flags.

1. **After every `shipped`:** `factory comms changelog <product>`. Write one plain-words line per
   user-facing PR from its body (never beyond what the body says), in each of the product's
   languages, into a JSON file and run `factory comms copy <product>@<at> --file <copies.json>`.
2. **What is due:** `factory comms due <product> --json`. Nothing due: stop.
3. **Draft** each due job with no draft, or with status `needs-edit` or `failed`, then
   `factory comms draft <job> --file <draft.json>`. A `ready` job is already approved: go to step 6.
   The command refuses anything untrue: fix the draft, never the rule.
   - Every claim cites the job's PRs: `subject`, each of `sentences` (with its `audience`),
     `caption` and `video.frames`. Text comes only from the PR body; no numbers the PR does not state.
   - Languages and audiences: the product's `--comms-langs` and `--comms-audiences`.
   - A change that starts later: `"defer": {"<pr>": "YYYY-MM-DD"}`; it comes back on its date.
4. **Video** (feature email and Friday digest), if you have a video skill: 9:16, 20 s at most,
   frames = the draft's `video.frames`, real product screens only. If the render fails:
   `factory comms video-failed <job>`; after 2 h the job sends with a static card.
5. **Approval:** a feature job or a product's first send returns `needs-approval` and opens one
   inbox item. When the answer arrives, `factory comms decide <job> send|edit|skip --ask dN`, then
   `factory resolve dN`. Never send a job that is not `ready`.
6. **Gate, right before sending:** `factory comms check <job>`. Exit 3 means do not send.
7. **Send** with the sender `RELEASE.md` names: always a dry run, then a test send, then the real
   send with the expected count, repeated while `remaining > 0`. `stuck > 0`: stop, never resend.
   No `RELEASE.md` on the product's `main`, or no sender in it: `factory ask --project <project-id> --kind action`
   and do not send. The test goes to the team inbox. A product's first live send waits for the owner's go on that
   test (`factory ask --project <project-id> --kind action`).
8. **Record:** `factory comms sent <job> --count <recipients the sender reported>`, or
   `factory comms failed <job> --reason "..."` when nothing went out (it stays due).
9. **Report** in the thread: changelog built, jobs drafted, approvals asked, sends with counts.

## Watch window

`factory --json release watch <product> --minutes 30` (60 for `solo-red`), as a BACKGROUND
command. The Bash background timeout must be at least window + 15 minutes (30 min: 2700000 ms;
60 min: 4500000 ms; max 7200000 ms). The harness notifies on exit: never sleep, never poll.

- Samples `factory release check <product>` (default every 300 s) and logs each sample to
  `$FACTORY_HOME/release-watch-<product>.log`.
- Two streaks: `http` checks, and `errors` + `business` signal checks. A streak resets when its
  group passes.
- Exit 3 (rollback): 3 consecutive `http` failures, or 2 consecutive signal failures. After the
  window, at most 2 more samples; a check still failing then means rollback.
- Exit 0: passed. Exit 1: bad config (no checks). Other: unexpected (Step 6 rules).

## Rollback

When the watch exits 3:
1. **Roll back the code now** with the rollback command in `RELEASE.md` (for example: revert the
   image-tag commit in the deploy repo, or promote the previous hosting deployment). Never touch the
   database migration.
2. **Verify recovery:** `factory release check <product>`.
3. **Revert PR into `main`, then sync `develop`** (every flow except `factory`, which has its own):
   ```bash
   B=revert/release-$(date +%F)
   W=$CLAUDE_JOB_DIR/tmp/revert-$(date +%m%d)
   git -C <repo> fetch -q origin main
   git -C <repo> worktree add -b $B $W origin/main
   git -C $W revert --no-commit -m 1 <release merge sha> || { echo "revert conflict: stop, urgent ask"; exit 1; }  # cherry-pick flow: git -C $W revert --no-commit <shaN> ... <sha1>
   # Keep migration files: the database keeps the migration, so main must keep the file.
   M=$(git -C $W ls-tree -r --name-only HEAD | grep -E '(^|/)migrations/|^prisma/|^supabase/migrations/' || true)
   [ -n "$M" ] && git -C $W checkout HEAD -- $M
   git -C $W commit --no-edit -m "revert: release $(date +%F)"
   git -C $W push origin $B
   PUSHED_SHA=$(git -C $W rev-parse HEAD)
   gh pr create -R <owner>/<repo> --base main --head $B --title "revert: release $(date +%F)" --body "Rollback release. Migration files are kept: the database keeps the migration."
   git -C <repo> worktree remove --force $W
   ```
   Give it the release-risk review, post `factory/review` on `$PUSHED_SHA`, merge with
   `--match-head-commit $PUSHED_SHA`, then merge `main` back into `develop` (Step 6 recipe).
   If `RELEASE.md` says the rollback turned off automatic production promotion, promote the revert
   deployment once it is ready and its smoke routes (from `RELEASE.md`) pass, then run
   `factory release check <product>`.
4. **File a GitHub issue** with the failure log, check output and rollback evidence.
5. **Urgent inbox item:**
   ```bash
   factory ask --project <project-id> --kind urgent \
     --title "Rollback on <product>: release failed watch" \
     --context "Watch failed. Code rolled back to the previous version. Issue filed." \
     --option a="Investigate root cause and review revert PR" --reco a
   ```
6. **Record:** `factory release record <product> rolled-back --pr <the release's PRs> --note "<issue url>"`.
   Those PRs count as done; a reland is a new PR. `rolled-back` sets a 24-hour hold.
7. **If the rollback itself fails:** `factory release record <product> rollback-failed --note "<details>"`
   (this pauses the product) and post an urgent ask "CRITICAL: Rollback failed on <product>".
8. **Unhold:** the release thread runs `factory release unhold <product>` itself, only after the
   revert PR is merged and `develop` synced (the CLI enforces 24 h).

## Migration safety rule

Code rollback cannot undo a migration, so schema changes must work with the code already running:
- Hold any migration that drops, renames or narrows a column or table that `main` still reads.
- Such changes ship in two releases: first code that stops using the column, then the migration.
- On a violation: `factory release record <product> held --pr <num> --note "Migration drops/narrows column still read by main"`,
  recut without that PR, and never post `factory/review` on the old head.

## Releasing the factory itself (`--flow factory`)

The factory runs from a live checkout (`~/factory-live` in the README) and deploys `main` only:
steps 1 to 3 do not apply.
- **Order:** the factory releases last each day. Check with `factory --json release status <each>`
  that no other release is in flight.
- **Shadow:** mode `shadow` means stop before the deploy and report `shadow: stopped before deploy`.
- **Deploy** (README "Running live", "Deploy a change"), after a status gate:
  1. `PREV_SHA=$(git -C ~/factory-live rev-parse HEAD)`; `git -C ~/factory-live fetch -q origin`;
     `NEW_SHA=<baseSha from the plan>`.
  2. `factory release record factory started --sha "$NEW_SHA" --note "prev=$PREV_SHA"` (shell
     variables do not survive between tool calls; the note keeps the previous SHA).
  3. `git -C ~/factory-live checkout -q --detach "$NEW_SHA"` (the verdict's `baseSha`, not `origin/main`).
  4. Set new config keys with `~/factory-live/bin/factory config set ...`, then `factory doctor`.
  5. If you run the Office, rebuild its bundle and `factory office install <file>`; a failed Office
     build does not block the release, say so in the report.
  6. `~/factory-live/bin/factory build`, stop the server (README), then `~/factory-live/deploy/watchdog.sh`.
- **Watch** (30 minutes; 60 when the verdict is `solo-red`: use `--minutes 60` and a background
  timeout of 4500000 ms): it must not run the code it just deployed. Read the previous SHA from the
  latest `started` note (`prev=<sha>`) in `factory --json release status factory`, then in ONE
  background command:
  ```bash
  W=$CLAUDE_JOB_DIR/tmp/factory-prev-<prev-sha>; git -C <dev clone> fetch origin <prev-sha> && git -C <dev clone> worktree add --detach $W <prev-sha> && FACTORY_HOME=~/.factory python3 $W/bin/factory --json release watch factory --minutes <30, or 60 on solo-red>; rc=$?; git -C <dev clone> worktree remove --force $W; exit $rc
  ```
  Exit 0: `factory release record factory shipped --sha <new-sha>`. Exit 1: record `held --sha <new-sha> --note "watch could not run: <error>"`, urgent ask, no rollback. Other: urgent ask, no rollback.
- **Rollback (exit 3 only):**
  1. Take the previous SHA from `lastStarted.note` (`prev=<sha>`). Empty: stop and post an urgent ask
     "Factory rollback aborted: prev SHA missing".
  2. `git -C ~/factory-live checkout -q --detach <prev-sha>`, build, stop the server, run the watchdog.
     Then do generic Rollback steps 2, 4 and 5: `factory release check factory`, file the issue,
     post the urgent "Rollback on factory" ask.
  3. Revert PR into `main` so the next run does not redeploy the bad commits (the reverse diff works
     across merges and keeps later commits). `<new-sha>` is `lastStarted.sha`:
     ```bash
     NEW=<new-sha>; PREV=<prev-sha>
     [ -n "$NEW" ] && [ -n "$PREV" ] || { echo "missing sha: stop, urgent ask"; exit 1; }
     B=revert/factory-release-$(date +%F)
     W=$CLAUDE_JOB_DIR/tmp/factory-revert-$(date +%m%d)
     git -C <dev clone> fetch -q origin main
     git -C <dev clone> worktree add -b $B $W origin/main
     git -C $W diff $NEW $PREV | git -C $W apply --index --3way || { echo "apply conflict: stop, urgent ask"; exit 1; }
     git -C $W commit -m "revert: factory release $(date +%F) ($PREV..$NEW)"
     git -C $W push origin $B
     gh pr create -R <owner>/<factory repo> --base main --head $B --title "revert: factory release $(date +%F)" --body "Rollback of $PREV..$NEW"
     git -C <dev clone> worktree remove --force $W
     ```
     Release-risk review, `factory/review` on the pushed SHA, merge with `--match-head-commit`.
  4. `factory release record factory rolled-back --pr <the release's PRs> --note "<issue url>"`.
  5. If it fails: `record factory rollback-failed` and an urgent ask; the dashboard may be down, so
     also tell the owner through whatever channel they gave you outside the factory.

## What `RELEASE.md` holds (one per product repo)

- Repo, deploy target, flow, release branch name.
- Pre-cut and merge prechecks (commands).
- How migrations run and how to confirm them, with known gotchas.
- Deploy verification commands and smoke URLs.
- Rollback command, and whether it turns off automatic promotion.
- Extra pushes this product allows (hard rule 6).
- Comms sender: the dry run, test and send commands.
