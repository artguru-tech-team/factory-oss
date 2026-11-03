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
