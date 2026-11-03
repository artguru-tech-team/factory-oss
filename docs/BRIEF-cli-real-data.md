# Brief: `factory` CLI + build on real data (first slice)

Owner: The owner. Prototype validated 2026-10-28. Build exactly what the prototype shows; do not redesign.

## Read first
- `docs/DECISIONS.md`: validated product decisions (binding).
- `docs/DESIGN.md`: visual authority (Linear style reference, dark only).
- `docs/PRODUCT.md`: users, purpose, constraints.
- `prototype/factory.html`: the validated page. Its `DATA` object is the data contract; keep its shape, and change it only where real data needs a field (document every change).
- `docs/access_jwt.reference.py`: the Cloudflare Access JWT check already used in production by ~/ds-pages/serve.py.

## Goal
Agents write to the dashboard through a CLI, and a build script renders the validated page from real sources. The owner stops reading hand-edited JSON. This slice ends with the page rendering **real product data** locally, verified in a browser at 390 and 1440 px.

## Stack and layout (keep it small)
- Python 3, standard library first. PyJWT is installed and allowed. No web framework, no new runtime dependency without a reason in the PR.
- `factory/` package; console entry `factory` (a `bin/factory` wrapper is fine).
- State lives in `~/.factory/` (JSON files, atomic writes, one lock): `products/<product>/projects/<id>.json`, `inbox.json`, `answers.json`, `updates/<project>.jsonl`, `agents-extra.json`.
- `template/factory.html` comes from the prototype. The build injects data at one marker (`/*DATA*/`) and writes `~/.factory/site/factory.html`. No external requests in the output page; keep the embedded Inter.
- `factory serve` binds 127.0.0.1 only (port 3045). It serves the built page and `GET/POST /api/answers`, requires `?k=` (key in `~/.factory/.key`, created on first run with mode 600), and applies the Access JWT check from `access_jwt.reference.py` to tunnelled requests.

## CLI surface (agents' only write path)
Every command prints JSON with `--json` and exits non-zero with a one-line error otherwise.
- `factory project add <id> --product acme --kind build|loop|run --name ... [--feat FEAT-12] [--prio 1]`; `factory project set <id> ...`; `factory project list`.
- Build plans: `factory plan phase <project> <name> --total N --start YYYY-MM-DD --due YYYY-MM-DD`; `factory plan units <project> <phase> --merged N --in-pr N --blocked N`; `factory plan scope <project> "<scope name>" <phase,...>`; `factory plan added <project> N` (scope added since start).
- Loops: `factory loop record <project> "<change>" --value X --kept|--discarded`; `factory loop start <project> "<change>"`; `factory loop meta <project> --metric ... --unit ... --baseline X --target Y`.
- Updates: `factory update <project> --health on-track|at-risk|off-track --agent <hc> "<text>"`.
- Inbox: `factory ask --project <id> --kind urgent|applied|action --title ... --context ... --option a="..." --option b="..." --reco a --why ... --from <hc>` prints the new id. `factory answers [--since ISO] [--wait SECONDS] [--id <id>]... [--from <hc>]` prints answered items. `factory resolve <id>` removes an item that was applied.
- Agents not visible in T3 (Hermes, remote boxes): `factory agent set <hc> --who ... --provider ... --model ... --project ... --state running|waiting|failed|quota|done --task ...`.
- `factory build` renders the page. `factory doctor` runs the source checks below and says exactly what is missing.

## Real sources the build reads
1. **T3 (agent registry), read-only:** `~/.t3/userdata/statev2.sqlite`, opened with `mode=ro`. Tables seen 2026-10-27: `orchestration_v2_projection_threads` (thread_id, project_id, title, provider_instance_id, created_at, updated_at, archived_at, deleted_at, payload_json), `orchestration_v2_projection_provider_sessions` (provider, status, model, updated_at, driver, provider_instance_id), `orchestration_v2_projection_runs` (status, requested_at, completed_at), `orchestration_v2_projection_subagents` (status, provider, started_at, completed_at, child_thread_id), `orchestration_v2_projection_runtime_requests` (kind, status, created_at, resolved_at), `projection_projects`. The schema is internal to T3: check the columns you read at startup and fail loudly with a clear message when they change. Map T3 projects to products by workspace path; a mapping file in `~/.factory/` is fine. Do not write to this database.
2. **Tokens:** investigate `~/.t3/userdata/usage-scan-cache-v9.json` and `usage-model-rates.json` (T3's own usage scan of Claude, Codex and other session files). If per-agent tokens cannot be derived reliably, show per-provider totals only and say so in the page. Never show invented numbers.
3. **GitHub:** `gh pr list` / `gh issue list` for the repos mapped to each product (acme: example/acme-monorepo and related; ask the orchestrator if a mapping is unclear). Run lane = open issues labelled bug or small-feature (configurable); the 14-day intake vs closed series comes from issue created and closed dates.
4. **DS migration (first real build project):** import it from what ~/ds-pages already uses. `~/ds-pages/build_progress.py` reads the unit catalogue (UNITS.md) from `origin/feat/ds-migration` with `git show` and PR state from `gh`. Reuse that logic to fill phases, merged / in PR / blocked / to do and the "Before the flip" scope. Open decisions in `~/ds-pages/data/progress-notes.json` can seed the inbox. Read only: do not modify ~/ds-pages or its running server.
5. Notion rows are not read by the build in this slice. Projects carry their FEAT id; Notion sync comes later through the project-management skill.

## Done means
- Unit tests (stdlib `unittest` is fine) for the CLI state changes and for the T3 reader against a small fixture database.
- `factory build` with real data, then the page rendered in Playwright (`/path/to/playwright/index.mjs`) at 390 and 1440 px: no console errors, no horizontal scroll, the inbox saves an answer through `POST /api/answers` and `factory answers` returns it.
- Screenshots of home, the DS migration drawer and the agents view, attached to the PR.
- README: install, every command, where state lives, how to run the server, and how an agent should use the CLI.

## Rules
- Branch `factory/feat/cli-real-data`, PR to `main`. Commit author email user@example.com.
- No deploy in this slice: no tunnel, DNS, or Cloudflare changes. The orchestrator wires `factory.example.com` behind the existing Access app afterwards, with the owner's go.
- No secrets in the repo or the page. The `?k=` key stays in `~/.factory/.key`.
- After opening the PR: run the review loop (simplify first, then adversarial review until clean, max 3 rounds), then stop and report. Never merge.
