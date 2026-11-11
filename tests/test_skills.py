import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone

from factory import build, skills, store, t3

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def t3_fixture(path):
    con = sqlite3.connect(path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.executemany("insert into projection_projects values (?,?,?)",
                    [("p1", "/w/repo", None)])

    def thread(tid, pid, title, created):
        payload = {"lineage": {"rootThreadId": tid}}
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                    (tid, pid, title, created, created, None, None, json.dumps(payload)))

    def provider_thread(tid, native_id):
        payload = {"nativeThreadRef": {"nativeId": native_id}}
        con.execute("insert into orchestration_v2_projection_provider_threads values (?,?,?)",
                    (tid, "claudeAgent", json.dumps(payload)))

    thread("t1", "p1", "Orchestrator thread", "2026-10-25T10:00:00Z")
    provider_thread("t1", "session-alpha")

    thread("thread:delegated-task:d1", "p1", "Delegate task", "2026-10-26T10:00:00Z")
    provider_thread("thread:delegated-task:d1", "session-beta")

    thread("thread:delegated-task:d2", "p1", "Subagent task", "2026-10-26T11:00:00Z")
    provider_thread("thread:delegated-task:d2", "session-parent")

    thread("thread:delegated-task:d3", "p1", "Subagent line session", "2026-10-26T12:00:00Z")
    provider_thread("thread:delegated-task:d3", "session-explicit")

    con.commit()
    return con


class SkillsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        empty_projects = os.path.join(self.tmp.name, "empty_claude_projects")
        os.makedirs(empty_projects, exist_ok=True)
        os.environ["FACTORY_CLAUDE_PROJECTS"] = empty_projects
        self.addCleanup(os.environ.pop, "FACTORY_CLAUDE_PROJECTS", None)

    def test_extract_triggers(self):
        # 1. Frontmatter explicit triggers list
        fm = {"triggers_list": ["trigger one", "trigger two"]}
        self.assertEqual(skills.extract_triggers(fm, "Some description"), ["trigger one", "trigger two"])

        # 2. Description with Triggers:
        desc = "Audit codebase. Triggers: Supabase products (Auth, DB); client libraries; migrations."
        trigs = skills.extract_triggers({}, desc)
        self.assertEqual(trigs, ["Supabase products", "client libraries", "migrations"])

        # 3. Description with quoted Use when:
        desc2 = 'Catch user up. Use when the user asks "where are we", "sitrep", or "catch me up".'
        trigs2 = skills.extract_triggers({}, desc2)
        self.assertEqual(trigs2, ["where are we", "sitrep", "catch me up"])

        # 4. Description without triggers
        desc3 = "A plain skill with no triggers."
        self.assertEqual(skills.extract_triggers({}, desc3), [])

        # 5. Finding 11: Weak unquoted Use when fragments return none
        desc5 = "Manage project learnings. Use when the user wants to record a learning or document an insight."
        self.assertEqual(skills.extract_triggers({}, desc5), [])

    def test_parse_frontmatter_folded_scalars(self):
        # Finding 10: YAML folded scalars (>- , |) stripped and lines joined
        c1 = "---\nname: replica\ndescription: >-\n  Plans the stack, database schema\n  and API for an app clone.\n---\n"
        fm1 = skills.parse_frontmatter(c1)
        self.assertEqual(fm1["description"], "Plans the stack, database schema and API for an app clone.")

        c2 = "---\nname: literal\ndescription: |\n  First line.\n  Second line.\n---\n"
        fm2 = skills.parse_frontmatter(c2)
        self.assertEqual(fm2["description"], "First line. Second line.")

    def test_collect_registry_and_groups(self):
        claude_dir = os.path.join(self.tmp.name, "claude_skills")
        agents_dir = os.path.join(self.tmp.name, "agents_skills")
        os.makedirs(os.path.join(claude_dir, "factory"), exist_ok=True)
        os.makedirs(os.path.join(claude_dir, "custom-unmapped"), exist_ok=True)
        os.makedirs(os.path.join(agents_dir, "factory"), exist_ok=True)
        os.makedirs(os.path.join(agents_dir, "bitwarden"), exist_ok=True)

        with open(os.path.join(claude_dir, "factory", "SKILL.md"), "w") as f:
            f.write("---\nname: factory\ndescription: Multi-agent orchestration skill.\n---\n# Factory\n")
        with open(os.path.join(claude_dir, "custom-unmapped", "SKILL.md"), "w") as f:
            f.write("---\nname: custom-unmapped\ndescription: Unmapped skill.\n---\n")
        with open(os.path.join(agents_dir, "factory", "SKILL.md"), "w") as f:
            f.write("---\nname: factory\ndescription: Duplicate from agents.\n---\n")
        with open(os.path.join(agents_dir, "bitwarden", "SKILL.md"), "w") as f:
            f.write("---\nname: bitwarden\ndescription: Secrets retrieval. Triggers: 'vault is locked'.\n---\n")

        groups_map = {"factory": "orchestration", "bitwarden": "security"}
        reg, sources = skills.collect_registry(
            groups=groups_map,
            claude_skills_dir=claude_dir,
            agents_skills_dir=agents_dir,
            plugins_file=os.path.join(self.tmp.name, "none.json")
        )

        self.assertEqual(len(reg), 3)
        self.assertIn("factory", reg)
        self.assertIn("bitwarden", reg)
        self.assertIn("custom-unmapped", reg)

        # Check groups
        self.assertEqual(reg["factory"]["group"], "orchestration")
        self.assertEqual(reg["factory"]["groupLabel"], "Orchestration")
        self.assertEqual(reg["bitwarden"]["group"], "security")
        self.assertEqual(reg["bitwarden"]["groupLabel"], "Security & Secrets")
        self.assertEqual(reg["custom-unmapped"]["group"], "other")
        self.assertEqual(reg["custom-unmapped"]["groupLabel"], "Other")

        # Check triggers
        self.assertEqual(reg["bitwarden"]["triggers"], ["vault is locked"])
        self.assertEqual(reg["factory"]["triggers"], [])

        # Finding 2 & 7: providers empty by default, source tag set
        self.assertEqual(reg["factory"]["providers"], [])
        self.assertEqual(reg["factory"]["source"], "local")

    def test_scan_usage_with_t3_mapping(self):
        db_path = os.path.join(self.tmp.name, "statev2.sqlite")
        con = t3_fixture(db_path)

        projects_dir = os.path.join(self.tmp.name, "projects")
        proj_sub = os.path.join(projects_dir, "proj-1")
        os.makedirs(proj_sub, exist_ok=True)

        # 1. Transcript for session-alpha (maps to t1 handle)
        file_alpha = os.path.join(proj_sub, "session-alpha.jsonl")
        with open(file_alpha, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Skill", "input": {"skill": "factory"}}
                    ]
                }
            }) + "\n")
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:30:00Z",
                "message": {
                    "content": "<command-message>audit</command-message>\n<command-name>/adversarial-pr-review</command-name>"
                }
            }) + "\n")

        # 2. Transcript for session-beta (maps to delegate handle)
        file_beta = os.path.join(proj_sub, "session-beta.jsonl")
        with open(file_beta, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-27T08:00:00Z",
                "attributionSkill": "plugin:bitwarden"
            }) + "\n")

        # 3. Old transcript (> 30 days)
        file_old = os.path.join(proj_sub, "session-old.jsonl")
        with open(file_old, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-08-01T00:00:00Z",
                "attributionSkill": "old-skill"
            }) + "\n")
        old_time = time.time() - 40 * 86400
        os.utime(file_old, (old_time, old_time))

        usage = skills.scan_usage(con, projects_dir=projects_dir, days=30, now=NOW)

        self.assertIn("factory", usage)
        self.assertIn("adversarial-pr-review", usage)
        self.assertIn("bitwarden", usage)
        self.assertNotIn("old-skill", usage)

        self.assertEqual(usage["factory"]["last_used"], "2026-10-26T12:00:00Z")
        self.assertEqual(usage["adversarial-pr-review"]["last_used"], "2026-10-26T12:30:00Z")
        self.assertEqual(usage["bitwarden"]["last_used"], "2026-10-27T08:00:00Z")

        alpha_agents = list(usage["factory"]["agents"])
        self.assertEqual(len(alpha_agents), 1)
        self.assertTrue(alpha_agents[0].startswith("1"))

        beta_agents = list(usage["bitwarden"]["agents"])
        self.assertEqual(len(beta_agents), 1)
        self.assertTrue(beta_agents[0].startswith("2"))

        con.close()

    def test_1_cache_per_transcript(self):
        """1 BLOCKER: cache keyed by (path, size, mtime) with unchanged, grown, and shrunk transitions."""
        projects_dir = os.path.join(self.tmp.name, "cache_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session-1.jsonl")

        # Initial write
        line1 = json.dumps({
            "timestamp": "2026-10-26T10:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "skill-init"}}]}
        }) + "\n"
        with open(fp, "w") as f:
            f.write(line1)

        usage = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-init", usage)

        # Cache file exists mode 600 in factory state dir
        cache = store.read("skills-cache.json", None)
        self.assertIsNotNone(cache)
        self.assertIn(fp, cache)
        self.assertEqual(cache[fp]["hits"], [["skill-init", "session-1", "2026-10-26T10:00:00Z"]])
        init_offset = cache[fp]["offset"]

        # Grown: append line, read only from stored offset
        line2 = json.dumps({
            "timestamp": "2026-10-26T11:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "skill-appended"}}]}
        }) + "\n"
        with open(fp, "a") as f:
            f.write(line2)

        # Advance mtime slightly so stat shows change
        t_new = time.time() + 5
        os.utime(fp, (t_new, t_new))

        usage2 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-init", usage2)
        self.assertIn("skill-appended", usage2)

        cache2 = store.read("skills-cache.json", {})
        self.assertGreater(cache2[fp]["offset"], init_offset)
        self.assertEqual(len(cache2[fp]["hits"]), 2)

        # Shrunk/replaced: rewrite with fewer bytes
        line_short = json.dumps({
            "timestamp": "2026-10-26T12:00:00Z",
            "attributionSkill": "skill-shrunk"
        }) + "\n"
        with open(fp, "w") as f:
            f.write(line_short)
        t_newer = time.time() + 10
        os.utime(fp, (t_newer, t_newer))

        usage3 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-shrunk", usage3)
        self.assertNotIn("skill-init", usage3)
        self.assertNotIn("skill-appended", usage3)

    def test_3_subagent_transcript_session_mapping(self):
        """3 BLOCKER: line sessionId maps subagent calls; falls back to parent folder for subagents/."""
        db_path = os.path.join(self.tmp.name, "sub_statev2.sqlite")
        con = t3_fixture(db_path)

        projects_dir = os.path.join(self.tmp.name, "sub_projects")
        # Subagent folder layout: <session-parent>/subagents/agent-1.jsonl
        sub_dir = os.path.join(projects_dir, "proj", "session-parent", "subagents")
        os.makedirs(sub_dir, exist_ok=True)

        # 1. Line with explicit sessionId "session-explicit"
        file1 = os.path.join(sub_dir, "agent-1.jsonl")
        with open(file1, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "sessionId": "session-explicit",
                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "explicit-sub-skill"}}]}
            }) + "\n")

        # 2. Line without sessionId, falls back to parent folder "session-parent"
        file2 = os.path.join(sub_dir, "agent-2.jsonl")
        with open(file2, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "fallback-sub-skill"}}]}
            }) + "\n")

        usage = skills.scan_usage(con, projects_dir=projects_dir, now=NOW)
        self.assertIn("explicit-sub-skill", usage)
        self.assertIn("fallback-sub-skill", usage)

        # session-explicit mapped to d3 handle (starts with 2)
        exp_agents = list(usage["explicit-sub-skill"]["agents"])
        self.assertEqual(len(exp_agents), 1)

        # session-parent mapped to d2 handle (starts with 2)
        fall_agents = list(usage["fallback-sub-skill"]["agents"])
        self.assertEqual(len(fall_agents), 1)
        self.assertNotEqual(exp_agents[0], fall_agents[0])

        con.close()

    def test_4_window_applied_per_call(self):
        """4 IMPORTANT: 30-day window applied per call: drop hits whose ts is before cutoff even if file is new."""
        projects_dir = os.path.join(self.tmp.name, "window_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "file.jsonl")

        with open(fp, "w") as f:
            # Within 30 days
            f.write(json.dumps({
                "timestamp": "2026-10-20T10:00:00Z",
                "attributionSkill": "fresh-skill"
            }) + "\n")
            # Older than 30 days relative to NOW (2026-10-27)
            f.write(json.dumps({
                "timestamp": "2026-08-01T10:00:00Z",
                "attributionSkill": "stale-skill"
            }) + "\n")

        usage = skills.scan_usage(projects_dir=projects_dir, days=30, now=NOW)
        self.assertIn("fresh-skill", usage)
        self.assertNotIn("stale-skill", usage)

    def test_5_command_name_mid_message_and_multiple(self):
        """5 IMPORTANT: only when stripped user content starts with <command-name> or <command-message>, only first."""
        projects_dir = os.path.join(self.tmp.name, "cmd_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session-cmds.jsonl")

        with open(fp, "w") as f:
            # Mid-message pasted command: ignored
            f.write(json.dumps({
                "timestamp": "2026-10-26T10:00:00Z",
                "message": {"content": "I pasted this: <command-name>/pasted-skill</command-name> mid-message"}
            }) + "\n")
            # Starts with <command-message>, has multiple commands: only first counted
            f.write(json.dumps({
                "timestamp": "2026-10-26T10:15:00Z",
                "message": {"content": "<command-message>audit</command-message>\n<command-name>/first-cmd</command-name>\n<command-name>/second-cmd</command-name>"}
            }) + "\n")
            # Starts directly with <command-name>
            f.write(json.dumps({
                "timestamp": "2026-10-26T10:30:00Z",
                "message": {"content": "   <command-name>/direct-cmd</command-name>   "}
            }) + "\n")

        usage = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertNotIn("pasted-skill", usage)
        self.assertIn("first-cmd", usage)
        self.assertNotIn("second-cmd", usage)
        self.assertIn("direct-cmd", usage)

    def test_6_sort_used_desc_then_name(self):
        """6 IMPORTANT: sort by lastUsed desc, unused last, then name."""
        claude_dir = os.path.join(self.tmp.name, "sort_claude")
        projects_dir = os.path.join(self.tmp.name, "sort_projects")
        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(projects_dir, exist_ok=True)

        for sname in ["zebra", "apple", "beta", "alpha"]:
            sdir = os.path.join(claude_dir, sname)
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {sname}\ndescription: Test {sname}\n---\n")

        fp = os.path.join(projects_dir, "trans.jsonl")
        with open(fp, "w") as f:
            # apple used 1 day ago (2026-10-26)
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "attributionSkill": "apple"
            }) + "\n")
            # zebra used 2 days ago (2026-10-25)
            f.write(json.dumps({
                "timestamp": "2026-10-25T12:00:00Z",
                "attributionSkill": "zebra"
            }) + "\n")

        slist, _, _ = skills.collect(
            now=NOW,
            con=None,
            projects_dir=projects_dir,
            claude_skills_dir=claude_dir,
            agents_skills_dir=os.path.join(self.tmp.name, "empty_agents"),
            plugins_file=os.path.join(self.tmp.name, "none.json")
        )

        names = [s["name"] for s in slist]
        # apple (used 2026-10-26), zebra (used 2026-10-25), alpha (unused A), beta (unused B)
        self.assertEqual(names, ["apple", "zebra", "alpha", "beta"])

        # Also verify Finding 2: providers is ["Claude"] for used, [] for unused
        self.assertEqual(slist[0]["providers"], ["Claude"])
        self.assertEqual(slist[1]["providers"], ["Claude"])
        self.assertEqual(slist[2]["providers"], [])
        self.assertEqual(slist[3]["providers"], [])

    def test_12_plugin_call_exact_match(self):
        """12 NIT: plugin call ns:name matches exactly when that plugin skill is in registry, not local name."""
        projects_dir = os.path.join(self.tmp.name, "plugin_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "trans.jsonl")

        with open(fp, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "attributionSkill": "myplug:browse"
            }) + "\n")

        # When registry contains both "myplug:browse" and local "browse"
        known = {"myplug:browse", "browse"}
        usage = skills.scan_usage(projects_dir=projects_dir, now=NOW, known_skills=known)

        self.assertIn("myplug:browse", usage)
        self.assertNotIn("browse", usage)

    def test_13_single_warning_on_t3_failure(self):
        """13 NIT: build.py skills_data emits only one warning when T3 is unavailable."""
        warnings = []
        # T3 connect throws in tests when con unavailable or mocked to fail
        import unittest.mock as mock
        with mock.patch("factory.t3.connect", side_effect=sqlite3.OperationalError("db locked")):
            slist = build.skills_data(NOW, warnings)
        self.assertIsInstance(slist, list)
        self.assertEqual(len(warnings), 1)
        self.assertIn("T3 database unavailable", warnings[0])

    def test_collect_handles_missing_sources_gracefully(self):
        slist, warns, dur = skills.collect(
            now=NOW,
            con=None,
            projects_dir=os.path.join(self.tmp.name, "missing_projects"),
            claude_skills_dir=os.path.join(self.tmp.name, "missing_claude"),
            agents_skills_dir=os.path.join(self.tmp.name, "missing_agents"),
            plugins_file=os.path.join(self.tmp.name, "missing_plugins.json")
        )
        self.assertEqual(slist, [])
        self.assertIsInstance(warns, list)
        self.assertGreaterEqual(dur, 0)

    def test_round2_1_partial_line_offset(self):
        """1 IMPORTANT: write one full line + first 40 bytes of a second, scan, append rest, scan again -> both counted once."""
        projects_dir = os.path.join(self.tmp.name, "partial_line_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session-partial.jsonl")

        line1 = json.dumps({
            "timestamp": "2026-10-26T10:00:00Z",
            "attributionSkill": "skill-first"
        }) + "\n"

        line2_full = json.dumps({
            "timestamp": "2026-10-26T11:00:00Z",
            "attributionSkill": "skill-second"
        }) + "\n"

        # Write line 1 and first 40 bytes of line 2
        with open(fp, "w") as f:
            f.write(line1)
            f.write(line2_full[:40])

        usage1 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-first", usage1)
        self.assertNotIn("skill-second", usage1)

        cache1 = store.read("skills-cache.json", {})
        self.assertEqual(cache1[fp]["offset"], len(line1.encode("utf-8")))

        # Append the rest of line 2
        with open(fp, "a") as f:
            f.write(line2_full[40:])

        t_new = time.time() + 5
        os.utime(fp, (t_new, t_new))

        usage2 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-first", usage2)
        self.assertIn("skill-second", usage2)

        cache2 = store.read("skills-cache.json", {})
        self.assertEqual(len(cache2[fp]["hits"]), 2)
        skills_counted = [h[0] for h in cache2[fp]["hits"]]
        self.assertEqual(skills_counted, ["skill-first", "skill-second"])

    def test_round2_2_cache_prunes_stale_and_compact_json(self):
        """2 IMPORTANT: prune keys not in scan set, drop hits older than 30d, write compact JSON."""
        projects_dir = os.path.join(self.tmp.name, "prune_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp_active = os.path.join(projects_dir, "active.jsonl")

        with open(fp_active, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T12:00:00Z",
                "attributionSkill": "active-fresh"
            }) + "\n")

        dead_fp = os.path.join(projects_dir, "dead.jsonl")

        # Pre-seed cache with stale key and old hit (>30d)
        initial_cache = {
            dead_fp: {
                "size": 100,
                "mtime": time.time(),
                "offset": 100,
                "head_hash": "dummy",
                "hits": [["dead-skill", "dead-sid", "2026-10-25T00:00:00Z"]]
            },
            fp_active: {
                "size": os.path.getsize(fp_active),
                "mtime": os.path.getmtime(fp_active),
                "offset": os.path.getsize(fp_active),
                "head_hash": "dummy",
                "hits": [
                    ["stale-hit-skill", "active", "2026-08-01T00:00:00Z"],
                    ["active-fresh", "active", "2026-10-26T12:00:00Z"]
                ]
            }
        }
        store.write("skills-cache.json", initial_cache)

        skills.scan_usage(projects_dir=projects_dir, days=30, now=NOW)

        # 1. Stale key is dropped
        with open(store.path("skills-cache.json"), encoding="utf-8") as f:
            raw_text = f.read()
        cache = json.loads(raw_text)
        self.assertNotIn(dead_fp, cache)
        self.assertIn(fp_active, cache)

        # 2. Hits older than 30 days are dropped
        hits = cache[fp_active]["hits"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "active-fresh")

        # 3. Compact JSON: only 1 newline at EOF, no whitespace separators
        self.assertEqual(raw_text.count("\n"), 1)
        self.assertNotIn(": ", raw_text)
        self.assertNotIn(", ", raw_text)

    def test_round2_3_rewritten_grown_file_rescans(self):
        """3 NIT: a rewritten file that grows triggers full rescan via head_hash mismatch."""
        projects_dir = os.path.join(self.tmp.name, "rewrite_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session-rewrite.jsonl")

        # Initial file (short)
        short_line = json.dumps({
            "timestamp": "2026-10-26T10:00:00Z",
            "attributionSkill": "old-skill"
        }) + "\n"
        with open(fp, "w") as f:
            f.write(short_line)

        usage1 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("old-skill", usage1)

        # Completely rewrite with different content that happens to be longer
        long_line = json.dumps({
            "timestamp": "2026-10-26T12:00:00Z",
            "attributionSkill": "brand-new-skill-with-a-much-longer-name-to-exceed-size"
        }) + "\n"
        self.assertGreater(len(long_line), len(short_line))
        with open(fp, "w") as f:
            f.write(long_line)

        t_new = time.time() + 5
        os.utime(fp, (t_new, t_new))

        usage2 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("brand-new-skill-with-a-much-longer-name-to-exceed-size", usage2)
        self.assertNotIn("old-skill", usage2)

        cache = store.read("skills-cache.json", {})
        self.assertEqual(len(cache[fp]["hits"]), 1)
        self.assertEqual(cache[fp]["hits"][0][0], "brand-new-skill-with-a-much-longer-name-to-exceed-size")

    def test_round2_4_platform_group_and_moves(self):
        """4 NIT: Platform & Frameworks group in skills_groups.json, freeze/unfreeze in security, writing/brainstorming in orchestration."""
        mapping = skills.load_group_mapping()
        self.assertEqual(mapping.get("freeze"), "security")
        self.assertEqual(mapping.get("unfreeze"), "security")
        self.assertEqual(mapping.get("brainstorming"), "orchestration")
        self.assertEqual(mapping.get("writing-beats"), "orchestration")
        self.assertEqual(mapping.get("writing-fragments"), "orchestration")
        self.assertEqual(mapping.get("writing-shape"), "orchestration")
        self.assertEqual(mapping.get("nextjs"), "platform")
        self.assertEqual(mapping.get("supabase"), "platform")
        self.assertEqual(mapping.get("sentry-cli"), "platform")
        self.assertEqual(mapping.get("vercel-cli"), "platform")
        self.assertEqual(mapping.get("ios-clean"), "platform")
        self.assertEqual(skills.GROUP_LABELS.get("platform"), "Platform & Frameworks")

    def test_round2_5_corrupt_cache_recovery(self):
        """5 NIT: on corrupt cache read failure, start from empty cache and rewrite it."""
        projects_dir = os.path.join(self.tmp.name, "corrupt_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session.jsonl")

        with open(fp, "w") as f:
            f.write(json.dumps({
                "timestamp": "2026-10-26T10:00:00Z",
                "attributionSkill": "fresh-skill"
            }) + "\n")

        # Corrupt the cache file
        store.write_text("skills-cache.json", "{not valid json\n")

        # Should not raise FactoryError, should scan successfully
        usage = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("fresh-skill", usage)

        # Cache file is recovered and valid JSON
        cache = store.read("skills-cache.json", None)
        self.assertIsNotNone(cache)
        self.assertIn(fp, cache)
        self.assertEqual(cache[fp]["hits"][0][0], "fresh-skill")

    def test_cold_rescan_streaming_large_file(self):
        """Issue #15: Cold rescan iterates line by line up to last newline, keeping partial line for next build."""
        projects_dir = os.path.join(self.tmp.name, "streaming_projects")
        os.makedirs(projects_dir, exist_ok=True)
        fp = os.path.join(projects_dir, "session-stream.jsonl")

        # Generate a multi-line file with complete lines and a trailing partial line
        expected_skills = []
        with open(fp, "wb") as f:
            for i in range(200):
                if i % 20 == 0:
                    sk = f"stream-skill-{i}"
                    expected_skills.append(sk)
                    line = json.dumps({
                        "timestamp": "2026-10-26T10:00:00Z",
                        "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": sk}}]}
                    }).encode("utf-8") + b"\n"
                else:
                    line = json.dumps({
                        "timestamp": "2026-10-26T10:00:00Z",
                        "type": "text",
                        "content": "filler content " * 30
                    }).encode("utf-8") + b"\n"
                f.write(line)
            valid_end_offset = f.tell()
            # Trailing partial line
            f.write(b'{"timestamp": "2026-10-26T10:00:00Z", "attributionSkill": "partial-skill"')

        usage = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        for sk in expected_skills:
            self.assertIn(sk, usage)
        self.assertNotIn("partial-skill", usage)

        cache = store.read("skills-cache.json", {})
        self.assertEqual(cache[fp]["offset"], valid_end_offset)
        self.assertEqual(len(cache[fp]["hits"]), len(expected_skills))

    def test_block_stream_partial_last_line_and_no_newline(self):
        """Test identical offset and hits semantics on partial last line and file with no newline."""
        projects_dir = os.path.join(self.tmp.name, "edge_cases_projects")
        os.makedirs(projects_dir, exist_ok=True)

        # 1. File with no newline
        fp_nonl = os.path.join(projects_dir, "no_newline.jsonl")
        with open(fp_nonl, "wb") as f:
            f.write(b'{"timestamp": "2026-10-26T10:00:00Z", "attributionSkill": "skill-no-nl"}')

        usage1 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertNotIn("skill-no-nl", usage1)
        cache1 = store.read("skills-cache.json", {})
        self.assertEqual(cache1[fp_nonl]["offset"], 0)
        self.assertEqual(cache1[fp_nonl]["hits"], [])

        # 2. File with partial last line
        fp_part = os.path.join(projects_dir, "partial_last.jsonl")
        line1 = json.dumps({"timestamp": "2026-10-26T10:00:00Z", "attributionSkill": "skill-complete"}).encode("utf-8") + b"\n"
        partial = b'{"timestamp": "2026-10-26T10:00:00Z", "attributionSkill": "skill-incomplete"'
        with open(fp_part, "wb") as f:
            f.write(line1)
            f.write(partial)

        usage2 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-complete", usage2)
        self.assertNotIn("skill-incomplete", usage2)
        cache2 = store.read("skills-cache.json", {})
        self.assertEqual(cache2[fp_part]["offset"], len(line1))
        self.assertEqual(len(cache2[fp_part]["hits"]), 1)

        # 3. Append completion of line
        with open(fp_part, "ab") as f:
            f.write(b'}\n')
        t_new = time.time() + 5
        os.utime(fp_part, (t_new, t_new))

        usage3 = skills.scan_usage(projects_dir=projects_dir, now=NOW)
        self.assertIn("skill-complete", usage3)
        self.assertIn("skill-incomplete", usage3)
        cache3 = store.read("skills-cache.json", {})
        self.assertEqual(cache3[fp_part]["offset"], len(line1) + len(partial) + 2)
        self.assertEqual(len(cache3[fp_part]["hits"]), 2)


if __name__ == "__main__":
    unittest.main()
