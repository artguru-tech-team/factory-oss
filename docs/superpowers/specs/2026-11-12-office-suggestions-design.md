# Office suggestions: design

Visitors of the public office suggest features for the instance's products, see what others suggested,
and vote. An agent character walks over, takes the suggestion, and "analyses" it in the product's room.

## Prototype validation (2026-11-12)

Prototype `factory-feature-suggestions-per-product` v6 validated by the owner. Versions v1 to v5 were sent
back or replaced; what v6 shows is now fixed, and the build follows it:

- The form lives inside the public office (not a page per product). One build, served by factory.
- Form: product (one of the instance's public products), text, optional email. The email is shown to the
  owner only, never in any public feed or page.
- After sending, an agent drawn by the office's own character renderer walks to the visitor, says
  "Got it. I received your suggestion and I will take care of it.", walks to the product's room and shows
  a work card: suggestion number, similar suggestions, fit with the roadmap, rough effort, then "Sent to
  the team for review".
- While typing, the form lists similar existing ideas with a vote button ("vote on this instead?").
- Ideas board: one tab per product, ideas sorted by votes, a status chip per idea (Under review, Planned,
  Building, Done, Declined). The visitor's own ideas are marked.
- "Latest ideas" card in the page UI, outside the scene: the 3 newest ideas across products (2 on phones),
  product, age, votes. A sent idea appears there at once, marked as the visitor's. Ideas from other
  visitors appear without a reload. A row opens the board on that product's tab. Desktop: bottom-left.
  Phones: full width above the buttons.
- Suggesting needs no account. Voting needs the visitor's account **on that product** (each product's
  own login; one product's account does not vote on another product's ideas). One vote per idea per
  account; clicking again removes it.

## Constraints from the public office design

`2026-11-08-public-office-design.md` forbids any interaction that writes, and lists "a visitor's request
reaches `factory serve`" as a failure. This feature adds writes, so:

- Writes go to a **new, separate** service. Not `factory serve`, and not the static server
  (`public_serve.py` stays GET and HEAD only, two routes).
- The new service reads and writes only `suggestions.db`, its
  salt file, config and its vote-key secrets; it never imports the dashboard server, the build or the relay.
- The public page still never contains the string `api/` (`install_public` keeps rejecting it). The new
  routes live under `/suggest/`.

## Parts

### 1. Store and CLI (factory)

- `suggestions.db` (SQLite, stdlib) in the factory home, owned by the new service.
  Tables: `ideas` (id, product, text, email, status, created_at, ip_hash, screened),
  `votes` (idea_id, account_hash, created_at, UNIQUE(idea_id, account_hash)).
- `account_hash` = HMAC of product + the product's user id with an instance salt. The service never stores
  a raw user id.
- CLI for the owner and agents: `factory suggestions list [--product] [--status]`,
  `factory suggestions status <id> <status>`, `factory suggestions hide <id>`.
  The dashboard gets a read-only list later; the CLI comes first (agent CLI rule).

### 2. Public write service

`factory office serve-suggest`, `127.0.0.1:3047`, Python stdlib like `public_serve.py`.

| Route | What |
|---|---|
| `GET /suggest/ideas.json` | Public ideas: id, product, text, status, votes, created_at. Allowlist, never email or hashes. Cache 30 s. |
| `POST /suggest/idea` | `{product, text, email?}`. Returns `{id}`. |
| `POST /suggest/vote` | `{idea_id, token, on}`. `token` proves the product account (part 4). |

Limits: body at most 2 KB; text 10 to 500 characters; product must be a configured public product;
5 ideas per IP hash per hour and 60 votes per account per hour; a hidden honeypot field; JSON only.
Same CSP and frame-ancestors as the static server. Leak test: `ideas.json` never contains an email, a
hash or a field outside the allowlist.

### 3. Office client (habblaud public build)

Port the prototype's overlay into the public build: form, similar ideas, agent walk with the office
renderer, work card, ideas board, Latest ideas card, per-product sign-in. It reads
`/suggest/ideas.json` (poll every 60 s; the visitor's own idea shows at once from the POST reply) and
posts to `/suggest/idea` and `/suggest/vote`. The work card content (similar ideas, roadmap fit, effort)
is computed in the page from `ideas.json` and the public feed; the real analysis is part 5.

Idea text is untrusted: the client renders it with textContent only.

### 4. Vote sign-in per product

Each product signs a short-lived vote token for its logged-in user: `{product, uid, exp}` with HMAC-SHA256
and a key shared only between that product and the suggest service (one key per product, kept in the
secret store, never in config or code).

- Standalone office page: "Sign in with <product>" opens the product's
  `/office-vote-token?return=<office url>`. The product asks its user to log in if needed, then redirects
  back with the token in the URL fragment. The page keeps it in memory and in `sessionStorage`.
- Office framed on the product's own site: the parent page sends the same token to the frame with
  `postMessage` (origin-checked on both sides), so a logged-in user votes without another login.
- Token life 1 hour. `exp` is Unix seconds (integer or decimal). The service checks signature, product, expiry, then derives `account_hash`.

This needs one small change in each product (an endpoint that signs the token), one PR per product
repository.

### 5. Analysis and moderation

- A new idea is stored as `screened = 0`. It shows in the submitter's own page at once (from the POST
  reply) and enters `ideas.json` once screened.
- Screening: an automatic filter (links, contact details, repeated text, a blocklist) passes or holds the
  idea right after each insert (`POST /suggest/idea` runs it; `factory suggestions screen` re-runs it); held ideas wait for the owner (`factory suggestions list --held`).
- Triage: a workhorse agent reads new screened ideas once a day, groups duplicates, notes roadmap fit,
  and posts one digest to the owner's inbox. Status changes stay with the owner.

### 6. Hosting

The tunnel route for the office host gains one path rule: `/suggest/*` to `127.0.0.1:3047`; everything
else stays on `3046`. This is an infrastructure change: the owner approves it in the inbox at that step.

## Order of work

1. Store, CLI, write service, limits, leak test (factory, one PR).
2. Office client in the public build (habblaud, one PR), against a local service.
3. Vote tokens: verification in the service (factory) and the signing endpoint in each product (one PR
   per product repository).
4. Screening and daily triage digest.
5. Tunnel path rule (owner approval), live check from outside, then `factory proto built`.

## Calls made by Claude (not yet confirmed by the owner)

- Separate service on its own port rather than writes in either existing server.
- Limits and token life as listed above.

## Confirmed by the owner

- 2026-11-12: vote sign-in by a product-signed token, by redirect or by `postMessage` when framed.
- 2026-11-12: new ideas go public after the automatic screen, with no manual approval.
