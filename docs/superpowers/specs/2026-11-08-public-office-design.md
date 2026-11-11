# Public Office: design

Date: 2026-11-08. Owner: the owner. Factory project: `factory-run`. Decisions: inbox d621, d628, d631, d637, d646.

## Why

The owner wants anyone to watch the company's AI agents at work. They will share the link on social media and keep
it visible on the Acme site and every other company site, to show the company runs as an
AI-augmented team (d621).

## What it is

A public, read-only copy of the Office scene (the pixel-art building of agents, built from the habblaud
fork) at **https://office.example.com** (d637). It shows the real agents, live, with every confidential
detail removed (d631: same scene, not a new widget).

## Goals

1. A visitor with no login sees the Office scene: rooms per product, one character per running agent,
   its state (working, waiting, idle) and its model provider. Idle agents keep their social life
   (lounge, coffee, games): that is what makes the scene worth posting.
2. Each desk shows only a generic label built from product and kind of work, such as "Building a feature
   for Acme" or "Fixing a bug on App" (d628).
3. The page polls every 5 minutes. The data is as fresh as the factory build, which runs every
   10 minutes from cron; the cadence stays as it is (changing it touches the live deployment).
4. The company sites can embed the scene in a frame, or link to it.

## Non-goals

- No interaction that writes: no chat, inbox, answers, permissions, room rename, demo toggle, update check.
- No session view (`/api/threads/<id>/messages`), no timeline, no history.
- No analytics or visitor counter in the first version.

## Anti-goals (a failure even if it works)

- Any confidential string reaches a visitor, in the feed, the page source or a network response. The list
  below is what "confidential" means.
- A visitor's request reaches `factory serve` (port 3045) or any private endpoint.
- The page breaks or shows private data when the private feed gains a new field.

## What stays private (never in the public feed)

From today's `site/office.json` and `/api/office`:

- Session titles (`task`, e.g. `platform/feat/plugin-marketplace`) and project ids and names
  (`bk-paypal-fr`, `aura-payment-fraud`): they reveal the roadmap.
- Project `next` steps and `updates`: they contain money and supplier details.
- `prs` (private repo links), `feat` ids, `prio`.
- `thread`, `parent`, `hc` handles, `canReceive`, `via`.
- `machine` (the VPS hostname), `relayConfigured`.
- `inbox`, `answers`, `levers`.
- `tokens` and any usage or spend figure.

## Public feed: `office-public.json`

Built by `factory build` next to `office.json`, from an **allowlist**: the builder copies named fields
and nothing else, so a new private field stays private by default.

```json
{
  "builtAt": "2026-11-08T17:00:00Z",
  "products": {"web": {"name": "Web"}, "app": {"name": "App"}},
  "agents": [
    {"id": "a3f9c2", "product": "web", "state": "running", "provider": "Claude",
     "since": "2026-11-08T16:41:00Z", "label": "Building a feature for Acme"}
  ]
}
```

- `id`: a salted hash of the thread id, stable for one thread so the character keeps its name and desk
  between refreshes, but not reversible to the T3 id. The salt lives in `~/.factory/.public-salt` (mode 600), never in the feed.
- `label`: from product name plus kind of work. Kind comes from the session name segment (`feat`,
  `fix`, `refactor`, `chore`, `docs`, `perf`, `test`) or the project kind; anything unknown becomes
  "Working on <Product>". Fixed phrase table, no free text from the session.
- `provider`: one of the known providers (Claude, Antigravity, OpenCode, Codex); else "AI agent".
  The exact model id is left out (builder may add the family name if the owner wants it later).
- Which agents appear (Claude's call): agents in state `running` (shown working), `waiting` (shown
  waiting) and `stalled` (shown idle), plus their subagents as badged characters. Agents in state `failed`,
  `quota` or `unknown`, and every quota probe (task `probe`), are left out. Today's real feed has 44
  failed rows, 22 of them probes: shown as-is, the scene would be mostly error signs.
- Products: only products the owner has made public by name. Today all products in `products.json`
  (Web, App, Store, Factory, streamer, showcase) are public brands: confirm the list once
  during prototype validation.

A check (unit test) builds the public feed from today's real private feed and fails if any value of a
private field appears anywhere in the output.

## Public page

A habblaud build in a new **public mode**. It builds on branch `factory/feat/office-on-habblaud`
(worktree `~/github/habblaud-wt-office`, 13 commits ahead of `main`), which holds the factory adapter
(`mapFactoryDataToSnapshot`, `pollFactoryLive`). Public mode stacks on that branch and merges after it.

- Reads only `office-public.json` (same origin, relative URL), polls every 5 minutes and on focus.
- Removes every control that writes and every panel listed under Non-goals. The code for them is not in
  the public bundle (build flag, dead-code removed), not merely hidden.
- Character names come from habblaud's neutral name pool (`shared/names.ts`, `pickName`) keyed on the
  public `id`.
- Hides account usage bars, counters of tokens, and the activity feed text that quotes session titles.
- Empty office (nobody running, e.g. at night): the existing `empty-office` illustration with a short line
  such as "The team is off right now. Back soon."
- A small footer: "AI agents building Acme, live" plus a link to the main site.
- Works in a frame: allows embedding from the company domains (d646: `example.com`, `app.example.com`,
  `store.example.com`, `office.example.com`, each with its subdomains, as the `frame-ancestors` list), scales to the
  frame width, and works on a phone screen (social media visitors arrive on phones).

## Hosting

- Two static files in a new directory `~/.factory/site-public/` (`index.html`, `office-public.json`).
  Nothing private is ever written there.
- Served by a separate, tiny static server on `127.0.0.1:3046` that serves only that directory: GET and
  HEAD only, no directory listing, no other paths. Not `factory serve`, so a bug there cannot open the
  private API.
- Cloudflare tunnel route `office.example.com` to `127.0.0.1:3046`, with no Access app. The zone
  `example.com` is on Cloudflare already. This is an infrastructure change: the owner approves it in the
  inbox at that step.
- Response headers: strict CSP (`default-src 'self'`, no `connect-src` beyond self), `frame-ancestors`
  limited to the company domains, short cache on the JSON (60 s) and longer on the HTML.

## Order of work

1. Prototype (prototype-first rule): a self-contained HTML file served from the VPS showing the public
   scene with a redacted copy of today's feed: states for several agents, empty office, phone width, and
   inside a frame. The owner validates it.
2. factory: public feed builder, allowlist, leak test, `factory build` writes `site-public/`.
3. habblaud: public mode build, installed with a new `factory office install --public <file>`.
4. Static server + tunnel route (needs the owner's approval), then a live check from outside.
5. Embed snippet for the sites (a frame tag), one PR per site repository: Web, App,
   Store (d646). Other sites link to the page.

## Left to the builder

- Exact phrase table for labels, within the rule above.
- Whether the static server is a small Python stdlib script in the factory repo or an existing static
  server, as long as it serves only `site-public/`.
- How the public build flag is wired in habblaud's Vite config.

## Calls made by Claude (not yet confirmed by the owner)

- 5-minute poll on the 10-minute build. Failed, quota and probe agents hidden; subagents shown.
- Public mode stacked on `factory/feat/office-on-habblaud`. Model ids hidden, provider shown. Tokens and spend hidden.
- Salted hash ids, neutral character names.
- Separate static server on port 3046 rather than a route in `factory serve`.
- Frame embed on the sites, plus the link for social media.

## Prototype validation (2026-11-08)

Prototype v1 (factory proto `factory-public-office-example`) validated by the owner with no notes. So: card
detail, empty message and frame look stay as shown; on a phone the whole building is scaled to fit; all six
products are public names. Claude's call, not asked: the reception sign reads "Acme" in the public build.
Implementation plan: `docs/superpowers/plans/2026-11-09-public-office.md`.
