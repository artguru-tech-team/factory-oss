# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Static HTML prototype first: one self-contained page (inline CSS/JS, embedded Inter, no external requests, dark only per DESIGN.md), mock data. Later it becomes the template of the real dashboard, fed by a build script and a small CLI that agents use instead of editing JSON. Whether a Mission Control fork sits underneath is still undecided.

## Users

The owner, sole owner of several products (Acme, Widget, Gizmo and others), running many AI agents in parallel. He uses it on desktop at his desk and on his phone between other tasks (both are first-class), and must know in 30 seconds where a project stands, what is running, and what is waiting on him.

## Product Purpose

One control page for an AI software factory: progress per project, what each agent is doing, token use per agent and overall, the decision log, and a form where agents' questions wait for the owner's answer with the agent's recommendation. Success: the owner answers decisions from the page and never has to ask an agent "where are we".

## Positioning

The agents run through T3 Code, which stays the control surface (launching, steering, delegating). This page is the owner's read view and decision inbox across every project and every provider (Claude, Antigravity, OpenCode, Codex), not another agent runner.

## Operating Context

- Agents: T3 threads and delegated tasks, Claude Code sessions on two Linux users, Antigravity and OpenCode via T3, Hermes on a server.
- Current practice: ~/<project>-pages with progress.tpl.html, build_progress.py, progress-notes.json, decisions.json, serve.py, Cloudflare tunnel with ?k= key.
- Rules: low-risk reversible decisions get applied by default and marked so; prod, infra, money, secrets, force-push and merges always wait for the owner.
- Auto-refresh every 5 min, never while the form has unsaved input.
- Habblaud (pixel-art office of agents) is the optional gamified view.

## Capabilities and Constraints

- Sections in order: status line, waiting on me (form, count on top), progress (computed, never typed), agents at work, PRs, next.
- Profile switcher across products. Each product holds projects of three kinds: build (phases and units vs target dates), loop (a metric over time, experiments kept or discarded, vs a target) and a run lane (bugs and small features: opened vs closed, oldest open item).
- Projects are Notion Product Roadmap rows (FEAT-N); each has a plan file that agents update through the factory CLI. A project-management skill enforces that agents keep plans, journals and status current.
- Failed or quota-blocked agents are marked explicitly; no ghost "running" rows.
- Undecided: Mission Control fork vs T3-as-registry; skills registry placement.
- Access: Cloudflare Access login (email code, owner only) in front of the public dashboards; serve.py checks the Access JWT; local agents use 127.0.0.1 with ?k=.

## Evidence on Hand

Real dashboard in use: ~/ds-pages (DS migration). Prototype data is synthetic and must be labeled so.

## Product Principles

1. The owner's time is the scarce resource: what waits on him comes first.
2. Numbers are computed from sources, never typed.
3. Stale data kills trust: every state carries its age.
4. Agents write through a CLI, the owner reads and decides on the page.

## Accessibility & Inclusion

Desktop and phone. Dark only (DESIGN.md, Linear style reference); body contrast stays within the Mist/Fog greys on Void. Large touch targets for the decision form.
