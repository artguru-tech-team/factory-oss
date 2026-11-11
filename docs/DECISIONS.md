# Factory dashboard: decisions from prototype validation

Prototype `factory-linear.html` validated by the owner on 2026-10-28 ("it's good"). The product build follows it.

## Product
- One dashboard for every product (Acme, Widget, Gizmo, …), switchable; used on desktop and phone.
- T3 Code stays the control panel and is the agent registry (threads, subagents, provider sessions, runtime requests in `~/.t3/userdata/statev2.sqlite`, read-only). No Mission Control fork for now.
- Projects = Notion Product Roadmap rows (FEAT-N). Three kinds:
  - build: phases with merged / in PR / blocked / to do, due dates, project graph (scope vs merged, velocity, predicted finish), optional scopes (e.g. "Before the flip");
  - loop: metric vs baseline and target, experiments kept or discarded, running experiment;
  - run lane (one per product): intake vs closed over 14 days, oldest open item.
- Health: builds from predicted finish vs last due date (≤0 on track, 1–4 at risk, 5+ off track); loops from the latest agent update, "No update" after 3 idle days; run lanes at risk past 10 days oldest (RUN_MAX_AGE).
- Projects sorted by: has a decision waiting, off track, at risk, on track; quiet loops fold away; run lane last.

## Interface
- Visual authority: DESIGN.md (Linear style reference), dark only, embedded Inter, acid lime only on Save answer.
- Home: status line ("N waiting on you" first), inbox with answers in place, project cards. All view: one list per product.
- Project opens in a peek drawer (bottom sheet on phone), ←/→ and swipe between projects, project strip with health.
- Every chart carries its own key at both sizes.
- Keyboard: j/k, Enter, 1–4, Ctrl/⌘+Enter.
- Gamified view: Habblaud, fed from the same registry.

## Access
- Cloudflare Access login in front of dash / app-dash (owner only), Access JWT checked at origin. Through the tunnel the Access login alone opens factory.example.com (2026-10-29: no key in the owner's links); requests from this machine still need `?k=`.

## Still open
- Project-management skill that makes agents keep plans, updates and Notion journals current (to build with the `factory` CLI).

## Message an agent (validated 2026-10-29)
Prototype: `~/factory-pages/proto-messages/factory-messages.html` (screenshots alongside). The owner: "proto ok", keeping the three recommended defaults.
- A **Message** action on each T3 agent in the Agents tab opens an **inline** compose box under that row (not a drawer); 2,000 characters, Ctrl/Cmd+Enter sends, Send is the one lime action.
- Delivery shows **queued → delivered / failed** (with reason and Retry). No "read" receipt.
- The agent's history under the box mixes the owner's messages and the **answers delivered to it** (one list per agent), each clearly tagged.
- Agents outside T3 show Message disabled: "not in T3: no way to deliver".
- Relay not configured: Send disabled with the real fix ("copy the Webhook URL from T3 Settings → Scheduled tasks → factory relay, then `factory relay set --webhook '<URL>'`"), not the prototype's T3 Connect wording.
- Transport: the same relay as answer push (factory outbox → T3 webhook task → `t3_thread_send`).

## Skills and Tokens views (validated 2026-10-29)
Prototype: `~/factory-pages/proto-skills-tokens/factory-skills-tokens.html` (screenshots alongside). The owner: "proto ok". The build follows it; defaults below settle the prototyper's open questions.
- **Skills** is one registry across products: the product tabs do not filter it. Grouped by purpose, with search and a group filter, as in the prototype. Source: the skill folders agents load (`~/.claude/skills/*/SKILL.md`, `~/.agents/skills/*/SKILL.md`, and installed plugins `~/.claude/plugins/installed_plugins.json` frontmatter: name, description). Group: a mapping in `factory/skills_groups.json`, anything unmapped goes to "Other". "Used by" and "last used" come from real skill calls in agent transcripts over 30 days (`tool_use` with name "Skill" and input.skill, or `<command-name>` commands), linked to T3 threads via `nativeThreadRef.nativeId` and handles via `t3.handles()`; when a skill has none, show "—", never invented data.
- **Tokens** stacks the daily bars by provider, with 24 h / 7 d / 30 d windows and the per-agent table (handle, provider, model, input, output, cache, est. cost). Antigravity and OpenCode tokens are read from their own stores (OpenCode `~/.local/share/opencode/opencode.db`; Antigravity `conversations/<id>.db` protobuf), linked to T3 threads by `nativeThreadRef.nativeId`, so they are not "Unrecorded". "No data" remains for agents outside T3 and for windows a source does not cover, saying why.
- **Cost** is an estimate in USD with the EUR figure beside it, as in the prototype, from a per-model price table in the repo; a model missing from the table shows "—".
- Empty, loading and error states as in the prototype.

## Assign an agent (validated 2026-11-07)
Prototype: `~/.factory/protos/factory-assign-an-agent-from-a-project-s-agents-box/v1.html`. The page follows it, with the template's lime primary button instead of the prototype's purple.
- On a project's page the Agents box has **Assign an agent** (empty box) or a small **+ Assign** in its header (non-empty). A sheet (the page's drawer pattern, a bottom sheet at 390 px) has two tabs: **New agent** (provider, brief) and **Existing session** (a live session with no project, plus a message). The brief is pre-filled from the project's latest update and Next items.
- **The page never launches threads.** A new agent is requested from a **dispatcher session**: a live T3 thread named in config `assign.dispatcher`. The page messages it the project, provider and brief, and the dispatcher launches a worktree thread (an account with quota, per the workhorse policy) and runs `factory agent assign <project> --thread <id>`. Dispatcher unset, a subagent, or not open in T3 (an idle dispatcher, quiet for days, still counts): "offline", and the API answers 409 with `factory config set assign.dispatcher <thread>`.
- **Explicit thread-to-project link**, in one place: `thread-projects.json` `{thread: project}`, written by `factory agent assign` (and by the page for an existing session). `t3.thread_project()` checks the thread's own link, then its own title, then an ancestor's link (so a delegate inherits it), then the root's title: a delegate whose title names a project keeps it, and a linked root's title never outvotes its link. With no link it is the plain title match, unchanged; a link to a project that no longer exists is ignored. Every caller passes the thread id and the links loaded once per loop, so a link can never be missed.
- Providers are a fixed list: `claude`, `antigravity`, `opencode`. **No per-account quota display** (the dispatcher picks the account).
- A new-agent ask is recorded in `assign-pending.json`. The Agents box shows it as a **"starting"** row (provider, "asked N min ago"). After **30 min** it turns red: "Not started: check the dispatcher session". It is dropped at **24 h**. It ends when the dispatcher runs `factory agent assign` for that project. Another agent already on the project taking a turn does not end it.
- A second new-agent ask while one is "starting" (under 30 min) is 409; the sheet then offers "Start another agent", which sends `force`.
- Existing session: the dispatcher thread is refused; re-sending to a thread already linked to the same project is allowed (Edit brief after a failed delivery); a thread linked to another project is 409, re-checked under the store lock.
- Delivery reuses the relay: the response is the outbox entry, and Retry is `/api/messages` `{retry: id}`. Existing session: the thread must be live, not a subagent, and on no other project.

## Session search and messaging from Waiting on you (validated 2026-11-10)
Prototype: `~/.factory/protos/factory-search-sessions-by-name-and-message-a-session-from-waiting-on-you/v1.html`. The owner answered a806 and a812: "do what you proposed".
- **Search** in Assign an agent › Existing session: a text box focused when the sheet opens, matching the session title and handle (case-insensitive substring), the match highlighted, "N of M match". Enter picks the first match, Escape clears the box. Running sessions are listed first, failed ones last.
- **Message the session** sits under Save answer on an open Waiting on you row, outside the answer form (typing there never marks the answer unsaved; Cmd/Ctrl+Enter there sends the message, not the answer).
- The message goes through `POST /api/messages` with `item`: the server quotes the question on one line (`About d32 (<title, cut at 120 characters>): <text>`) so the agent knows what it is about.
- A question with **no session** (asked outside T3) shows a searchable picker over every live session that can take a message. **Picking a session links the question to it by default** (checkbox on, `link: true`): the row's badge shows that session and later answers go there. An answer saved before the link is relayed to that session when the link is made. The link is written only after the message is queued, and never overwrites a thread the question already has.
- A question whose session is no longer open in T3 says so and offers no compose box.
- Follow-ups from the PR #93 spec check (issues #94 to #97): the server answers 400 when `item` has a thread and `thread` is another one, naming that thread; the row badge uses the thread the server reports (`itemThread`, on success and on that 400), not the one the page asked for, and the typed text stays; auto-refresh waits for typed text, a send in flight, or a message that failed in the last 5 minutes (counted from the failure, so a retry that fails again gets 5 more minutes), and no longer for a picked session alone. Browser test: `tests/test_session_msg_browser.mjs`.

## Roadmap view (validated 2026-11-09)
Prototype: `~/factory-pages/proto-roadmap/factory-roadmap.html` (screenshots `rm-*.png` beside it). The owner validated v1 from the Prototypes tab. Defaults below settle the two questions left open.
- New nav entry **Roadmap**, product tabs as elsewhere plus "All products". Four lanes: **Now** (1–2 months, being built), **Next** (3–6 months, to decide), **Later** (6+ months, with a folded **Icebox**), **Done** (in prod, newest first, 4 shown then "Show N older").
- The owner manages it: drag between lanes and reorder, or the ⋯ menu (up one, down one, move to a lane, mark done, open details). The order in Now is the priority agents follow, shown as P1…Pn.
- **Now holds at most 7 per product, for every product** (default; config `roadmap.cap`). Moving an 8th in opens "Now is full": pick one to move to the top of Next, then Swap.
- Mark done on an item whose plan is not finished asks for confirmation ("is at m/t units. Mark it done anyway?").
- Add an idea at the top of Next and Later. A product with no items shows the empty state with one input that adds to Next.
- Details drawer: title, lane, target date, "Why it matters / what done means (agents read this)", read-only source status, progress (units), agents running now, links (project page, source row), journal. Save writes through.
- **Mismatch flag kept** (default): "Built, not in Now" (lane is not Now and the linked project has a running agent) and "Finished, not marked done" (linked plan has all units merged, lane is not Done). A banner counts them with "Show only these"; the drawer offers Move to Now / Ask agents to pause it (relay message to the project's thread) / Mark done.
- Every change shows a toast "… Agents see it at their next session start." with Undo. A failed source write rolls the card back and shows the error with Try again.
- A product whose roadmap lives in Notion reads it on build and writes moves back to Notion. Notion unreachable: that product turns read-only with "Notion did not answer…, shows the copy from HH:MM" and other products still work.
- Agents read the roadmap with `factory roadmap list <product>`.
