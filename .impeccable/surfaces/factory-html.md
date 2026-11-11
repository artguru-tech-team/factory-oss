---
version: 1
slug: "factory-html"
primary_target: "factory.html"
related_targets: []
---

Scope: factory.html, the owner's control page for the AI software factory. Mode: Operate.
Audience/job: The owner on desktop and phone, 30 seconds: where each project stands, what runs, what waits on him; answers decisions in place.
Constraints: self-contained HTML, no external requests, light/dark, synthetic data labeled, auto-refresh blocked by unsaved input.

## Direction contract
THESIS: A Linear-grade control room for one owner: what waits on him first, every project read as health + progress + forecast, agents as named chips. Refuses KPI-tile dashboards and unlabeled charts.
OWN-WORLD: DESIGN.md (Linear style reference): Void/Carbon/Obsidian surfaces, 0.5px Graphite hairlines, Inter Variable 400/510, Berkeley-style mono for IDs and numbers, 6/12/9999 radii, acid lime only on Save answer. Iris = progress, Lavender = in PR, Teal = run lane, health uses Linear's green / orange / red / dashed grey.
STORY: Status line (waiting count first), Inbox with in-place answers, project cards sorted by what needs him, peek drawer per project with progress breakdown, project graph, milestones, updates, agents, PRs.
FIRST VIEWPORT: "N waiting on you" sentence, the inbox with the first decision ready to answer, the first two project cards with health and predicted finish.
SIGNATURE: the peek drawer sliding between projects with ←/→ and a project strip carrying each health icon.
RISK: density on phone; every chart carries its own key and labels at both sizes.
SEED: user-supplied DESIGN.md (no roll).

## Home layout (shape, 2026-10-27): variant A "Panel" chosen
- Navigation: product tabs on top (count of levers waiting per product, plus All). Desktop: left rail (Home, Agents, Log). Phone: bottom tab bar with the same three.
- Home for one product: inbox column (compact lever rows, tap expands the full lever form in place) beside the project list (desktop two columns; phone stacked, inbox first).
- Project order: has a lever waiting, then late, then at risk, then on track; projects with no agent and no activity for 3 days fold into "Quiet". Run lane always last.
- Each project kind keeps its own view: build = phase track with agent lamps and plan marker; loop = progress from baseline to target plus the last experiments as kept/discarded marks and the running one; run lane = opened vs closed this week, oldest open item.
- Several loops: each loop is compact by default, and the most recently active one is expanded to its full chart. Every loop shows % of its own target, so loops with different metrics can be compared.
- All view: one section per product, one line per project (kind glyph, name, headline number, health, agent lamps, lever count). Tapping a line opens that project.
- Project page (#p/<id>): plan timeline by phase (planned vs actual dates, today line), units or experiments or items, agents, PRs, next, log for that project.
- Out of scope this round: skills registry tab, tokens page beyond the Agents tab.

## Visual authority change (2026-10-27)
The owner supplied DESIGN.md (Linear style reference). It replaces the signal-box world: dark only, Inter Variable (embedded), hairline borders, 6/12 px radii, acid lime only for the one primary action (Save answer). Build: factory-linear.html.
Linear components adopted: project health (On track / At risk / Off track / No update), project updates feed with missed-update warning, project graph (scope vs completed, velocity, predicted finish), milestones (diamonds), run-lane burn-up (cycle graph), progress pie, peek drawer, inbox.
Health rule: builds use predicted finish vs target (0 days late or less = on track, 1-4 = at risk, 5+ = off track); loops use the latest agent update, or "No update" after 3 idle days; run lanes are at risk when the oldest item passes 10 days.
Deviation: at-risk orange #f2994a is not in DESIGN.md; added because the system has no warning hue.
