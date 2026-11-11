# Daily release manager implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily `factory release` loop that scores every unreleased PR, decides ship-all / cherry-pick / solo-red / wait per product, hands the plan to a Claude release thread, and gives that thread deterministic health checks for watching and rolling back.

**Architecture:** Plain Python in `factory/release.py`, on the same pattern as `factory/sweep.py`: config and state in `~/.factory/release.json` through `store.read`/`store.write`, and GitHub through `sources.gh_json`, mocked in tests. The LLM part (cut, review, merge, migrate, deploy, watch, roll back) lives in a `release-manager` skill that the release threads follow. The skill calls `factory release check` for every watch decision, so a rollback is triggered by numbers, not judgment.

**Tech Stack:** Python 3 stdlib only (`unittest`, `zoneinfo`, `urllib`), the `gh` CLI, and the existing `store`, `sources`, `relay` and `t3` modules.

**Spec:** `docs/superpowers/specs/2026-11-03-release-manager-design.md`

## Global Constraints

- No new Python dependencies; stdlib only, like the rest of `factory/`.
- Tests follow `tests/test_sweep.py`: `FACTORY_HOME` set to a temp dir, CLI driven through `cli.main(["--json", ...])`, `sources.gh_json` patched with `mock.patch.object`.
- Run the suite with `python3 -m unittest discover -s tests -t .` from the repo root.
- Weekday and "today" logic uses `zoneinfo.ZoneInfo("Africa/Tunis")`. The server runs on CET, so never use naive local time.
- Pace: at most 1 release per product per day. Feature releases Monday to Thursday only. Friday allows only PRs labelled `bug` or `fix` that score green. Saturday and Sunday are always `wait`.
- A release carries at most 10 PRs, oldest merge first.
- A PR labelled `no-release` is never shipped.
- Red: a money path, a migration, auth, or infra/Helm/deploy workflows. Yellow: more than 400 changed lines, no test file changed, a new env var, or a dependency bump. A release with 4 or more yellow PRs counts as red.
- A rollback holds the product for at least 24 hours, and until `factory release unhold` runs.
- Watch: 30 minutes for green and yellow releases, 60 for solo red, checks every 5 minutes. Fail on 3 health failures in a row, or 2 in a row for error or business signals. Errors fail above 2x the baseline. Business signals fail below 50% of the baseline, and only when the baseline is at least 10.
- Products in v1: `web` (monorepo), `admin`, `app`, `store`, `factory`. `app-ui` and `streamer` stay out.
- Every `factory release` write happens under `store.lock()`.
- New config starts with `mode: "shadow"` (verdicts computed, nothing relayed). `live` is switched on per product by hand after a week of shadow verdicts match what a human would pick. This rollout step is Claude's call, recorded in the spec's calls list.

## Review Focus

1. **One product's `gh` failure.** A rate limit or a 502 on one repo must not stop the run for the others. That product gets `wait` with the reason "could not read: <error>". (Task 6 test `test_plan_one_repo_fails_others_continue`.)
2. **A second plan run the same day.** A cron run plus a manual run must not relay twice. A product already relayed today keeps its verdict and is not re-sent. (Task 6 test `test_plan_is_idempotent_per_day`.)
3. **Every PR held or red on a Friday.** The verdict must be `wait`, never a cherry-pick with an empty ship list. (Task 4 test `test_friday_all_red_waits`.)
4. **A PR whose file list is truncated.** GitHub caps the files API at 3000 files. A PR with more files than were listed scores red ("file list incomplete"). (Task 3 test `test_truncated_files_is_red`.)
5. **Pause during an in-flight release.** `factory release pause` must stop the thread's next step, not only the next morning. The skill checks `factory release status <product>` before merge, before migration and before deploy. (Task 8 step, checked in its dry run.)

---

### Task 1: Release config, state and product registry

**Files:**
- Create: `factory/release.py`
- Modify: `factory/cli.py` (new `release` group, next to the `sweep` group at about L1111)
- Test: `tests/test_release.py`

**Interfaces:**
- Produces:
  - `RELEASE_REL = "release.json"`
  - `DEFAULT_RELEASE = {"products": {}, "paused": {}, "holds": {}, "journal": [], "verdicts": {}, "stale_asked": {}}`
  - `release() -> dict`: validated load, raises `FactoryError` on a bad shape. Same approach as `sweep.sweep()`.
  - `save_release(data) -> None`
  - Product entry shape: `{"repo": str, "flow": "release-branch"|"gitflow"|"pr"|"factory", "base": str, "head": str, "thread": str|None, "red_paths": [str], "project": str, "mode": "shadow"|"live", "checks": [check]}`. `base` is the source branch (`develop`; `main` for factory). `head` is the prod branch (`main`; `live` for factory, meaning the `~/factory-live` HEAD).
  - CLI: `factory release product add <name> --repo R --flow F --project P [--base develop] [--head main] [--thread T] [--red-path GLOB ...]`, and `factory release product set <name> [--thread T] [--mode shadow|live] [--red-path GLOB ...]`.

- [ ] **Step 1: Write failing tests** `test_product_add_defaults_to_shadow` (after an add, `release()["products"]["app"]["mode"] == "shadow"` and `base == "develop"`), `test_bad_flow_rejected` (exit code non-zero, stderr names the allowed flows), and `test_bad_shape_refused` (write `{"products": []}` to `release.json`, then `release.release()` raises `FactoryError`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_release -v`. Expected: FAIL (no module `factory.release`).
- [ ] **Step 3: Implement** `release()`, `save_release()`, `product_add(a)` and `product_set(a)` in `factory/release.py`, then wire the `release product` subgroup in `cli.parser()`.
- [ ] **Step 4: Run the tests.** Expected: PASS. Then run the full suite. Expected: no new failures.
- [ ] **Step 5: Commit** `feat(release): product registry and state file`

### Task 2: List unreleased PRs

**Files:**
- Modify: `factory/release.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Consumes: the Task 1 product entry.
- Produces: `unreleased_prs(product: dict) -> list[dict]`. Each dict has `{"number": int, "title": str, "mergedAt": str, "labels": [str], "files": [str], "changed": int, "filesComplete": bool, "patch": str}`, sorted by `mergedAt` ascending.

- [ ] **Step 1: Write failing tests:**
  - `test_unreleased_from_compare`: a mocked `gh_json` returns a compare payload with 3 commits, 2 of which map to PR #11 and 1 to PR #12. Assert the result is `[11, 12]` in merge order, with no duplicates.
  - `test_factory_compares_live_head`: for `flow == "factory"`, the compare base is the SHA from `git -C ~/factory-live rev-parse HEAD`. Patch `sources.run`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Use `gh api repos/{repo}/compare/{head}...{base}`, then `repos/{repo}/commits/{sha}/pulls` per commit (deduplicate), then `repos/{repo}/pulls/{n}/files?per_page=100` (paginate up to 30 pages). `filesComplete` is `len(files) == pr.changed_files`. `patch` is the joined `patch` fields, used by Task 3 for env-var detection. Path for `~/factory-live` comes from `os.path.expanduser`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(release): list unreleased PRs per product`

### Task 3: Risk score

**Files:**
- Modify: `factory/release.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Consumes: the PR dict (Task 2) and `product["red_paths"]`.
- Produces:
  - `DEFAULT_RED = ["**/migrations/**", "prisma/**", "supabase/migrations/**", "**/auth/**", "**/middleware.*", ".github/workflows/**", "helm/**", "charts/**", "**/payout*", "**/wallet*", "**/payment*", "**/checkout*", "**/fee*"]`
  - `score_pr(pr: dict, red_paths: list[str]) -> tuple[str, list[str]]`: color `"red"|"yellow"|"green"` plus reasons.
  - `release_score(scored: list[tuple[str, list[str]]]) -> str`

- [ ] **Step 1: Write failing tests** (one line each):
  - `test_migration_is_red`: a file `libs/prisma-schema/prisma/migrations/20261101_x/migration.sql` gives `"red"`, and the reasons include `"migration"`.
  - `test_product_red_path`: `red_paths=["apps/payments/**"]` with a file `apps/payments/a.ts` gives `"red"`.
  - `test_big_diff_is_yellow`: `changed=401` gives yellow; `changed=400` with a test file gives green.
  - `test_no_test_file_is_yellow`: no path matching `test`/`spec`/`__tests__` gives yellow.
  - `test_new_env_var_is_yellow`: a patch containing `+  const k = process.env.NEW_KEY` gives yellow.
  - `test_lockfile_is_yellow`: `package-lock.json` or `bun.lock` changed gives yellow.
  - `test_truncated_files_is_red`: `filesComplete=False` gives red with `"file list incomplete"` (Review Focus 4).
  - `test_four_yellows_make_red`: `release_score` of 4 yellows is `"red"`; 3 yellows give `"yellow"`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** with `fnmatch.fnmatch` against `DEFAULT_RED + red_paths`. Env var regex: `^\+.*(process\.env\.|getEnv\()([A-Z0-9_]+)` over patch lines. For App, read the `risk-review.yml` comment format from `acme/app` `.github/workflows/risk-review.yml`. If a PR's latest risk-review comment marks a critical match, add red with `"risk-review critical"`. Fetch it in Task 2 as an extra `riskCritical: bool` field, mocked in a test fixture copied from a real comment.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(release): green/yellow/red PR score`

### Task 4: Verdict and pacing

**Files:**
- Modify: `factory/release.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Consumes: scored PRs from Task 3, plus state from Task 1 (`paused`, `holds`, `journal`).
- Produces: `decide(prs: list[dict], state: dict, product: str, ci_green: bool, now: datetime) -> dict`, returning `{"verdict": "ship-all"|"cherry-pick"|"solo-red"|"wait", "ship": [int], "held": [{"number": int, "reason": str}], "reasons": [str], "score": str}`. Each PR dict carries `color` and `scoreReasons` from Task 3.

The rules, in order (the first match decides `wait`):

```
wait  if no PRs left after dropping `no-release` (held reason "no-release label")
wait  if paused[product] or paused["*"]
wait  if a hold exists for product (reason names the hold's issue)
wait  if a journal "rolled-back" for product is under 24h old
wait  if a journal "shipped" or "started" for product has today's Africa/Tunis date
wait  if Saturday or Sunday (Africa/Tunis)
wait  if not ci_green
take the oldest 10 (the rest held: "over the 10-PR cap")
Friday: keep only green PRs labelled bug/fix; none -> wait
no red and release_score(kept) != "red"   -> ship-all
exactly one red, or several (oldest one)  -> solo-red on Mon-Thu, ship = [that PR]
reds present and solo-red not allowed     -> cherry-pick the green/yellow ones; none -> wait
```

A release whose 4 or more yellows make it red, with no red PR, ships as `cherry-pick` of the 3 oldest yellows plus every green PR. This is Claude's call, so the pacing stays at 1 release a day.

- [ ] **Step 1: Write failing table tests**, one per rule, with fixed `now` values (`2026-11-02` is a Monday, `2026-11-06` a Friday, `2026-11-07` a Saturday, all at 07:30 Africa/Tunis):
  - `test_no_release_label_is_held`
  - `test_paused_waits`
  - `test_hold_waits`
  - `test_rollback_24h_waits` (23h59 waits, 24h01 does not)
  - `test_already_shipped_today_waits`
  - `test_weekend_waits`
  - `test_ci_red_waits`
  - `test_cap_10_oldest_first` (12 green PRs ship the 10 oldest; 2 are held "over the 10-PR cap")
  - `test_friday_only_green_fixes`
  - `test_friday_all_red_waits` (Review Focus 3)
  - `test_all_green_ships_all`
  - `test_one_red_ships_solo_on_monday`
  - `test_two_reds_ship_oldest_solo`
  - `test_four_yellows_cherry_pick_three`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** `decide`. It is pure: no I/O, `now` is passed in.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(release): verdict and pacing rules`

### Task 5: Journal, pause, hold, status

**Files:**
- Modify: `factory/release.py`, `factory/cli.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Produces:
  - `record(product: str, event: str, prs: list[int], sha: str|None, note: str, now=None) -> dict`. Events: `started`, `shipped`, `rolled-back`, `rollback-failed`, `held`. Keep the last 500 entries.
  - A `rolled-back` event also sets `holds[product] = {"since": iso, "issue": note}`. A `rollback-failed` event also sets `paused[product] = true`.
  - CLI:
    - `factory release record <product> <event> [--pr N ...] [--sha S] [--note TEXT]`
    - `factory release pause [product]` (no product means `"*"`)
    - `factory release resume [product]`
    - `factory release unhold <product>`: refused under 24h from the hold's `since`.
    - `factory release status [product]`: today's verdicts, pause, holds and the last 5 journal lines. `--json` gives `{"paused": bool, "hold": {...}|null, "verdict": {...}|null}` for one product.

- [ ] **Step 1: Write failing tests:**
  - `test_rollback_sets_hold`
  - `test_rollback_failed_pauses`
  - `test_unhold_refused_before_24h`
  - `test_pause_all_and_resume`
  - `test_status_json_for_product`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the commands, every write under `store.lock()`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(release): journal, pause, hold and status`

### Task 6: `factory release plan`: the morning run, relay and cron

**Files:**
- Modify: `factory/release.py`, `factory/cli.py`, `factory/relay.py:33` (add `"release"` to `MESSAGE_KINDS`, and store its text verbatim like `"autopilot"` at L165), `deploy/crontab.txt`
- Test: `tests/test_release.py`, `tests/test_relay.py`

**Interfaces:**
- Consumes: Tasks 2 to 5, `relay.enqueue_message(thread, text, kind="release")`, `store.add_ask_item(...)`.
- Produces:
  - `plan(now=None, only=None, dry=False) -> dict`: `{product: verdict}`. It writes `verdicts[YYYY-MM-DD][product]`.
  - `plan_message(product: str, verdict: dict, prs: list[dict]) -> str`: the text relayed to the thread. It starts with `Release manager (factory release plan, <date>):`, then gives the product, verdict, ship list (number, title, color, reasons), held list with reasons, and the line `Follow the release-manager skill for <product>.`
  - CLI: `factory release plan [--only PRODUCT] [--dry-run]`.

Behaviour:
- Products are processed in sorted order, except that `factory` is skipped unless `--only factory` is passed.
- `--only factory` returns `wait` ("other releases still in flight") while any product has a `started` event today without a later `shipped`, `rolled-back` or `rollback-failed`.
- CI status: `gh api repos/{repo}/commits/{base}/status`. CI counts as green when `state == "success"` or when there are no statuses and every check-run on that commit concluded `success` or `skipped`.
- The plan relays only when the verdict ships something, `mode == "live"`, a `thread` is set and the product is not already relayed today.
- A PR whose `mergedAt` is more than 3 working days old under a `wait` verdict raises one `applied` inbox item, title `Ship or hold PR #<n> on <product>: waiting <d> days`. It is deduplicated in `stale_asked` by `product#number`.

- [ ] **Step 1: Write failing tests:**
  - `test_plan_relays_live_ship_verdict`: `relay.enqueue_message` is called once with `kind="release"`, and the text starts with `Release manager (factory release plan`.
  - `test_plan_shadow_does_not_relay`
  - `test_plan_one_repo_fails_others_continue` (Review Focus 1)
  - `test_plan_is_idempotent_per_day` (Review Focus 2)
  - `test_factory_waits_while_others_in_flight`
  - `test_stale_pr_asks_once`
  - In `tests/test_relay.py`: `test_release_kind_is_verbatim`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement**, then add these cron lines to `deploy/crontab.txt` (server time, CET):
  - `30 7 * * 1-5 /usr/bin/python3 $HOME/factory-live/bin/factory release plan >> $HOME/.factory/release.log 2>&1 # factory-release`
  - `0 11 * * 1-4 /usr/bin/python3 $HOME/factory-live/bin/factory release plan --only factory >> $HOME/.factory/release.log 2>&1 # factory-release-self`
  - If the 11:00 run waits, a second try at `0 14 * * 1-4` with the same command and the tag `# factory-release-self-2`.
- [ ] **Step 4: Run the full suite.** Expected: PASS. Then run `FACTORY_HOME=$(mktemp -d) bin/factory release plan --dry-run`. Expected: it prints one verdict per registered product and exits 0.
- [ ] **Step 5: Commit** `feat(release): morning plan run, relay and cron`

### Task 7: `factory release check`: deterministic watch signals

**Files:**
- Modify: `factory/release.py`, `factory/cli.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Consumes: `product["checks"]`.
- Produces:
  - Check shapes:
    - `{"name": str, "kind": "http", "url": str}`: passes on HTTP 200 within 10 s.
    - `{"name": str, "kind": "errors", "cmd": [str], "baseline_cmd": [str]}`: each command prints one integer. Fails when `now > 2 * baseline` (with baseline 0, fails when `now > 5`).
    - `{"name": str, "kind": "business", "cmd": [str], "baseline_cmd": [str]}`: fails when `baseline >= 10 and now < 0.5 * baseline`.
  - `check(product: str) -> dict`: `{"ok": bool, "checks": [{"name", "kind", "ok", "value", "baseline", "detail"}]}`.
  - CLI: `factory release check <product>`. It exits 0 when everything is ok, 3 when any check fails, and with the usual `FactoryError` code when the config is bad. `factory release check add <product> --name N --kind K (--url U | --cmd "..." --baseline-cmd "...")` appends a check.
  - The streak rule (3 health failures, or 2 signal failures, in a row) is applied by the caller. `check` reports one sample.

- [ ] **Step 1: Write failing tests**, using a local `http.server` the way `test_sweep.py` does:
  - `test_http_200_ok`
  - `test_http_500_fails_exit_3`
  - `test_errors_over_2x_fails`
  - `test_business_drop_ignored_under_10_baseline`
  - `test_business_drop_fails` (baseline 20, now 9)
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Commands run through `subprocess.run(cmd, timeout=60)` as argv lists, never a shell string.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(release): health and signal checks for the watch`

### Task 8: The `release-manager` skill and the rule changes

**Files:**
- Create: `~/.claude/skills/release-manager/SKILL.md` (outside the repo)
- Modify: `~/.claude/skills/release-manager/SKILL.md` (hard rules 1 and 3, flow A step 5), `~/.claude/CLAUDE.md` (the prod/merge approval line and the `~/factory-live` line)
- Modify: `README.md` (new "Release manager" section: commands, cron lines, shadow and live modes, kill switch)

**Interfaces:**
- Consumes: `factory release status|record|check|pause` (Tasks 5 and 7) and the relayed plan text (Task 6).

The skill has these sections: hard rules, the release run (spec section 4, steps 1 to 7), watch, rollback, migration safety rule, reporting, and one section per product.
- The web and admin sections move over from release flows A and B.
- The App, Store and Factory sections follow the spec's product table, and the README "Deploy a change" section for Factory.

The skill must say:
- Run `factory release status <product> --json` before the merge, before migrations and before the deploy. Stop if `paused` is true (Review Focus 5).
- Run `factory release record <product> started` before the cut, and record `shipped`, `rolled-back` or `rollback-failed` at the end.
- Watch by running `factory release check <product>` every 5 minutes in a background loop. Do not use foreground `sleep`, which the harness blocks.
- Hold any migration that drops, renames or narrows a column or table that `main` still reads.
- After a rollback, open a revert PR on `develop` (Vercel products: promote explicitly on the next release), file an issue, post `factory ask --kind urgent`, and run `factory release unhold` only after the revert lands and 24 h have passed.

The rule edits name the release manager and this spec. Each exception covers only release-manager runs.

- [ ] **Step 1: Write the skill and the rule edits.**
- [ ] **Step 2: Dry-run the skill on `admin`** in a fresh Claude session: hand it a real `plan_message` from `factory release plan --dry-run`. Expected: it cuts the branch, writes the review, and stops before the merge, because the product is in `shadow` and the skill refuses to merge in shadow. It also stops at the first status check when the product is paused.
- [ ] **Step 3: Commit** the README in the repo (`docs(release): release manager runbook`). Skill and CLAUDE.md edits are outside the repo, so list them in the PR body.

### Task 9: Register the five products and their release threads

**Files:** none in the repo. This is operational. Each step is a `factory release product add` or `check add` command, recorded in the PR body.

- [ ] **Step 1: Create one T3 thread per product,** named `{product}/chore/release-manager` (the T3 `t3_thread_launch` tool, project root of that product's repo).
- [ ] **Step 2: Register each product** with its repo, flow, project, thread and red paths from the spec's product table. Flows: `web` is `release-branch`, `admin` is `gitflow`, `app` and `store` are `pr`, `factory` is `factory`.
- [ ] **Step 3: Add the checks** per product. At least one `http` check per product (e.g. `https://example.com/payments/api/health`, `https://example.com/`), one `errors` check, and one `business` check where a payment or signup count is available read-only.
- [ ] **Step 4: Verify.** Run `factory release plan --dry-run`. Expected: 4 verdicts (Factory is skipped without `--only`), each with reasons that match what `git log origin/main..origin/develop` shows. Run `factory release check <product>` for each. Expected: every check returns `ok: true`, exit 0.

### Task 10: Dashboard prototype (stops for owner's validation)

**Files:**
- Create: `prototype/release.html` (self-contained HTML, inline CSS and JS, mock data, styled like `prototype/factory.html`)

The prototype shows:
- one card per product with the day's verdict and reasons;
- ship and held lists with each PR's color and reasons;
- a pause toggle per product and a global one;
- holds with their issue link;
- the last journal lines.

It covers these states: everything `wait`, one `solo-red` in flight, a rollback with a hold, everything paused, and a `gh` read error on one product.

- [ ] **Step 1: Build the prototype.** Register it with `factory proto` and serve it from the VPS.
- [ ] **Step 2: Send the owner the link** with the list of states to click through. Stop. The dashboard code (in `factory/build.py`, `factory/serve.py` and the page) is planned after validation, following the decisions written down during validation.

### Launch

After Tasks 1 to 9 are merged and deployed to `~/factory-live`, run one week in `shadow`. Post each day's verdicts as a `factory update fac-release-mgr`. Then switch each product to `live` with `factory release product set <name> --mode live`, starting with `admin`, the fastest to roll back.
