# Public Office Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a read-only, redacted copy of the Office scene at https://office.example.com, fed by an allowlist-built public feed.

**Architecture:** `factory build` writes `~/.factory/site-public/office-public.json` from an allowlist (factory repo). A habblaud public entry, built as one HTML file, is installed as `site-public/index.html` and polls that JSON. A separate stdlib static server (`factory office serve-public`, port 3046) serves only those two files; the Cloudflare tunnel routes office.example.com to it, with no Access app.

**Tech Stack:** Python 3 stdlib (factory, `unittest`), TypeScript + Vite + Vitest (habblaud), Cloudflare tunnel.

**Spec:** `docs/superpowers/specs/2026-11-08-public-office-design.md` (decisions d621, d628, d631, d637, d646, d653). Validated prototype: factory proto `factory-public-office-example` v1, code on habblaud local branch `office/feat/public-office-proto` (commit f4dac61, `~/github/habblaud-wt-public-proto`).

## Global Constraints

- Public address: `office.example.com`. Frame allowed from: `example.com`, `app.example.com`, `store.example.com`, each plus `*.` subdomains.
- Public feed fields, and only these: top level `builtAt`, `products{key: {name}}`, `agents[]`; per agent `id`, `parent`, `product`, `state`, `provider`, `since`, `label`.
- `state` in the feed is one of `running`, `waiting`, `stalled`. Rows with state `failed`, `quota`, `unknown`, `done`, or task `probe` are dropped (review of PR #79: the scene drops done rows anyway, and shows `stalled` as idle, so `waiting` is kept as itself).
- `provider` is one of `Claude`, `Antigravity`, `OpenCode`, `Codex`, else `AI agent`. No model id.
- `label` comes only from this table (`{p}` = product name): `feat` "Building a feature for {p}", `fix` "Fixing a bug on {p}", `refactor` "Cleaning up code in {p}", `chore` "Maintenance on {p}", `docs` "Writing docs for {p}", `perf` "Speeding up {p}", `test` "Writing tests for {p}", anything else "Working on {p}".
- `id` = first 10 hex chars of HMAC-SHA256(salt, thread id, or `hc` when there is no thread). Salt = `~/.factory/.public-salt` (32 random hex bytes, mode 600, created on first use). The salt never leaves that file.
- Page copy (validated in prototype v1): header "Acme Office"; live line "● LIVE  N agents in the office, M working" / "● LIVE  nobody in right now"; empty state "The team is off right now." + "Back soon."; footer link "AI agents building Acme, live" → https://example.com; agent card = neutral name, label, "Agent · <provider>" or "Helper agent · <provider>".
- Public page polls `office-public.json` every 5 minutes and on window focus. The factory build cadence (cron, every 10 min) is not changed.
- Never touch `~/factory-live`, port 3045, the cron lines, or Cloudflare without the owner's approval through `factory ask` (kind `urgent`).

## Decisions from prototype validation (2026-11-08, "validated" with no notes)

- Card detail, empty message, frame look: as in prototype v1.
- Phone: the whole building scaled to fit (no zoom to one room).
- Reception sign: Claude's call, rename "Habblaud" to "Acme" in the public build only (not asked; reversible).
- All six products are public names: Web, Store, App, Factory, streamer, showcase.

## Review Focus

1. A private row has no `thread` (only `hc`): it still gets a stable public id, and `hc` itself never appears in the output.
2. An agent's `product` is not in `products`: the agent is dropped, never shown under a raw key.
3. A session title with no `/` or an unknown kind (`platform/wip/x`, `probe`, empty): label is "Working on {p}" (or dropped for `probe`), never any part of the title.
4. `office-public.json` cannot be fetched (server restarting, 404, bad JSON): the page keeps the last scene and shows no error panel; first load with no data shows the empty state.
5. A request for any path other than `/` and `/office-public.json` (`/../config.json`, `/office.html`, `/api/office`, `/%2e%2e/.key`) gets 404 from the public server; POST gets 405.

---

### Task 1: Public feed builder (factory)

**Files:**
- Create: `factory/office_public.py`
- Test: `tests/test_office_public.py`

**Interfaces:**
- Consumes: `office.filter_office_agents(agents, built_dt)`, `store.path`, `store.write_text`, `t3.ts`.
- Produces: `public_salt() -> bytes` (reads or creates `.public-salt`); `build_public_feed(data: dict, salt: bytes) -> dict` (data = `build.collect()` output); `PRIVATE_KEYS: tuple[str, ...]` for the leak test.

- [ ] **Step 1: Write the failing tests** in `tests/test_office_public.py` (`unittest`, temp `FACTORY_HOME` like `tests/test_office.py`):
  - `test_only_allowlisted_fields`: feed keys == `{"builtAt","products","agents"}`; each agent's keys == `{"id","parent","product","state","provider","since","label"}`.
  - `test_labels_from_table`: tasks `"acme/feat/x"` → "Building a feature for Acme", `"app/fix/y"` → "Fixing a bug on App", `"platform/wip/z"`, `"plain"`, `""` → "Working on Acme".
  - `test_dropped_rows`: states `failed`, `quota`, `unknown`, task `probe`, product missing from products → not in output; state `waiting` kept, `done` dropped.
  - `test_ids_stable_and_hashed`: same thread → same id across two calls; id is 10 hex chars; row with no thread uses `hc`; neither thread id nor `hc` appears in `json.dumps(feed)`.
  - `test_parent_mapped`: subagent row (`parent` != own `hc`) → `parent` == the parent's public id; top-level row → `parent` is `None`.
  - `test_provider_names`: `"claude"` → "Claude", `"opencode"` → "OpenCode", `"weird"` → "AI agent"; no `model` anywhere.
  - `test_no_private_value_leaks`: build a fixture `data` with realistic private values (task `"acme/feat/secret-roadmap"`, project `acme-paypal`, next step `"top up card +$70"`, PR `org/repo#12`, machine label, inbox title, tokens) and assert none of those strings appear in `json.dumps(feed)`.
  - `test_salt_file_created_600`: first `public_salt()` creates `.public-salt`, mode `0o600`, 64 hex chars; second call returns the same bytes.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_office_public -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `factory/office_public.py` with the constants from Global Constraints. Agents run through `office.filter_office_agents` first. Kind = second `/` segment of `task` when the task has at least 3 segments. Build a `hc → public id` map before mapping parents.
- [ ] **Step 4: Run** the same command → all PASS; then `python3 -m unittest discover -s tests` → no new failures.
- [ ] **Step 5: Commit** `feat(office): public feed builder with allowlist and leak test`.

### Task 2: Write the public feed on every build

**Files:**
- Modify: `factory/build.py:447` (inside `render`, under the existing lock)
- Test: `tests/test_office_public.py`, `tests/test_build.py:210-214`

**Interfaces:**
- Consumes: `office_public.build_public_feed`, `office_public.public_salt`.
- Produces: file `site-public/office-public.json` (mode 600 is fine: same user serves it).

- [ ] **Step 1: Write the failing test** `test_render_writes_public_feed`: after `build.render(data)`, `site-public/office-public.json` exists and parses to a feed with the allowlisted top-level keys. Update `RenderTest.test_stamp_written_before_page` expected list to append `os.path.join("site-public", "office-public.json")`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_office_public tests.test_build -v` → FAIL.
- [ ] **Step 3: Add** one line after the `office.json` write: `store.write(os.path.join("site-public", "office-public.json"), office_public.build_public_feed(data, office_public.public_salt()))`. A failure here must not stop `factory.html` being written: wrap in `try/except Exception as e` and append a warning `f"public office feed: {e}"`.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `feat(office): factory build writes site-public/office-public.json`.

### Task 3: Install the public page

**Files:**
- Modify: `factory/office.py` (add `install_public`), `factory/cli.py:517-527, 1447-1450`
- Test: `tests/test_office_public.py`

**Interfaces:**
- Produces: `office.install_public(src_path) -> {"installed": True, "path": str, "size": int}`; CLI `factory office install --public <file>`.

- [ ] **Step 1: Write the failing tests:** `test_install_public_ok` (valid `<!doctype html>` file → `site-public/index.html` written, `site/office.html` untouched); `test_install_public_rejects_private_bundle` (file containing `api/messages` or `api/office` → `FactoryError` naming the string); same size and doctype checks as `install`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `install_public` reusing `install`'s checks (extract one `_read_page(src_path) -> bytes` helper both call), plus a rejection if any of `b"api/"` appears. Add `--public` flag to `office install`; message `ok: installed public page {path} ({size} bytes)`.
- [ ] **Step 4: Run** → PASS; `factory office install --help` shows `--public`.
- [ ] **Step 5: Commit** `feat(office): factory office install --public`.

### Task 4: Public static server

**Files:**
- Create: `factory/public_serve.py`
- Modify: `factory/cli.py` (command `office serve-public [--port 3046] [--host 127.0.0.1]`)
- Test: `tests/test_public_serve.py`

**Interfaces:**
- Produces: `public_serve.make_handler(root: str) -> type[BaseHTTPRequestHandler]`; `public_serve.serve(host: str = "127.0.0.1", port: int = 3046) -> None` (blocks).

- [ ] **Step 1: Write the failing tests** (start a `ThreadingHTTPServer` on port 0 in a thread, root = temp dir holding `index.html` and `office-public.json`):
  - `GET /` → 200 `text/html; charset=utf-8`, body = index.html; `GET /index.html` → 404 (one name per file).
  - `GET /office-public.json` → 200 `application/json`, `Cache-Control: public, max-age=60`; `/` has `Cache-Control: public, max-age=300`.
  - `GET /?x=1` → 200 (query ignored).
  - 404 for `/../config.json`, `/%2e%2e/.key`, `/office.html`, `/api/office`, `/site/factory.html`.
  - `POST /` and `PUT /` → 405; `HEAD /` → 200 with no body.
  - Every response has `Content-Security-Policy` containing `default-src 'self'`, `frame-ancestors https://example.com https://*.example.com https://app.example.com https://*.app.example.com https://store.example.com https://*.store.example.com https://office.example.com https://*.office.example.com`, plus `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The CSP allows the page's inline script and `data:` images/fonts (the bundle is one inline file): `script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'`.
  - Missing `index.html` → 503 with a one-line text body.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_public_serve -v` → FAIL.
- [ ] **Step 3: Implement** with a fixed route table `{"/": "index.html", "/office-public.json": "office-public.json"}` resolved against `store.path("site-public")`; no filesystem path is ever built from the request.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(office): public static server for site-public`.

### Task 5: Habblaud public entry (product version of the prototype)

**Files (habblaud repo, new branch `office/feat/public-office` from `factory/feat/office-on-habblaud`):**
- Create: `client/public.html`, `client/src/public-main.ts`, `client/src/public.css`, `shared/demo/public.ts`, `shared/demo/public.test.ts`, `vite.public.config.ts`
- Modify: `scripts/inline-build.mjs` (input html path argument), `package.json` (script `build:public`)

**Interfaces:**
- Consumes: the feed shape from Task 1; `mapFactoryDataToSnapshot`, `pickName`, `createWorld`, `DEFAULT_WORLD_OPTIONS`; `OfficeStore.showPublic(snap)` (add as in prototype commit f4dac61).
- Produces: `npm run build:public -- <out.html>` → one self-contained HTML file for Task 3.

- [ ] **Step 1: Write the failing Vitest tests** in `shared/demo/public.test.ts`: `publicSnapshot` gives one room per product named by `products[k].name`; snapshot has no `inbox`; agent names come from the name pool (never the id); `model` and `gitBranch` undefined; `canReceive` false; empty `agents` → zero agents.
- [ ] **Step 2: Run** `npx vitest run shared/demo/public.test.ts` → FAIL.
- [ ] **Step 3: Implement** from the prototype (commit f4dac61), minus `nextTick`, the toolbar, the site-frame demo and the embedded sample. `public-main.ts` imports nothing from `client/src/ui` or `net/store` SSE paths beyond `OfficeStore` + `showPublic`; it fetches `office-public.json` (relative URL, `cache: 'no-store'`) at start, every 300 000 ms and on `focus`; on fetch or parse failure it keeps the current scene. Reception sign text "Acme" in the public build.
- [ ] **Step 4: Run** `npx vitest run` and `npm run typecheck` → PASS. Build: `npm run build:public -- /tmp/public-office.html`; `grep -c "api/" /tmp/public-office.html` → `0`.
- [ ] **Step 5: Browser check** (Playwright, `--no-sandbox`): serve the HTML and a copy of today's `office-public.json` with `factory office serve-public` on a free port; load at 1280x800 and 390x844; record requests for 30 s → only `GET /` and `GET /office-public.json`; zero console errors; click an agent → card with name, label, provider; replace the JSON with `{"agents": []}` and trigger `focus` → empty state. Save screenshots and look at them.
- [ ] **Step 6: Commit** `feat(office): public read-only build`.

### Task 6: Go live (needs the owner's approval)

- [ ] **Step 1:** `factory ask --kind urgent`: "Run the public Office server and route office.example.com?" Options: run `factory office serve-public` as a user systemd service (or under the existing tunnel watchdog) + add the tunnel ingress `office.example.com → http://127.0.0.1:3046` + DNS route; wait for the answer.
- [ ] **Step 2:** On yes: install the page (`factory office install --public <file>`), start the service, add the ingress line to `~/.cloudflared/config.yml` above the 404 catch-all, `cloudflared tunnel route dns <tunnel> office.example.com`, restart the tunnel through its watchdog. Document the service and route in the factory README "Running live" section in the same PR.
- [ ] **Step 3: Verify from outside:** `curl -sI https://office.example.com/` → 200 with the CSP header; `curl -s https://office.example.com/office-public.json` parses and has only allowlisted keys; `curl -s -o /dev/null -w '%{http_code}' https://office.example.com/api/office` → 404; open it in a real phone browser.

### Task 7: Embed on the company sites (one PR per repo, after Task 6)

- Web, App, Store sites: add `<iframe src="https://office.example.com" title="Acme Office, live" loading="lazy" style="width:100%;aspect-ratio:9/5;border:0">`. Placement per site goes through that site's own prototype rule if it adds a new section. Other sites: a text link.
