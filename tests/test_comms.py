"""Tests for release comms: changelog, due, truth validation, approval routing, sends, and CLI."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from factory import cli, comms, release, store
from factory.comms import CommsValidationError
from factory.store import FactoryError

TUNIS = ZoneInfo("Africa/Tunis")


def v2(old):
    """Adapt a draft written in the first schema (subject text, one audience) to claims with PRs."""
    sentences = [dict(s, audience=old.get("audience")) for s in old.get("sentences", [])]
    first_prs = sentences[0]["prs"] if sentences else []
    return {"subject": {"text": old.get("subject"), "prs": first_prs}, "sentences": sentences}


def decide_now(job_id, choice, now=None):
    """Apply an answer to the job's current ask, as the agent does with the relayed item id."""
    return comms.decide(job_id, choice, ask=comms.comms()["jobs"][job_id].get("ask_id"), now=now)


def save_v1(job_id, draft, now=None):
    return comms.save_draft(job_id, v2(draft), now=now)



class CommsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

        # Configure release products
        with store.lock():
            r = release.release()
            r["products"]["app"] = {
                "repo": "acme/app",
                "flow": "pr",
                "base": "develop",
                "head": "main",
                "thread": "t-c2e",
                "red_paths": [],
                "project": "c2e",
                "mode": "live",
                "checks": [],
            }
            r["products"]["web"] = {
                "repo": "acme/web",
                "flow": "release-branch",
                "base": "develop",
                "head": "main",
                "thread": "t-bk",
                "red_paths": [],
                "project": "bk",
                "mode": "live",
                "checks": [],
            }
            r["products"]["store"] = {
                "repo": "acme/store", "flow": "pr", "base": "develop", "head": "main",
                "thread": "t-aura", "red_paths": [], "project": "aura", "mode": "live", "checks": [],
            }
            r["products"]["admin"] = {
                "repo": "acme/admin",
                "flow": "gitflow",
                "base": "develop",
                "head": "main",
                "thread": "t-admin",
                "red_paths": [],
                "project": "bk-admin",
                "mode": "live",
                "checks": [],
            }
            # product metadata lives in the release config: a comms block makes a comms product
            r["products"]["app"].update(display_name="App", comms={
                "langs": ["en", "fr"], "audiences": ["creators", "brands"], "internal_scopes": []})
            r["products"]["web"].update(display_name="Web", comms={
                "langs": ["fr", "en"], "audiences": ["creators"], "internal_scopes": ["tracing"]})
            r["products"]["store"].update(display_name="Store", comms={
                "langs": ["fr", "en"], "audiences": ["customers"],
                "internal_scopes": ["store", "shop-manager", "suppliers", "demo"]})
            r["products"]["admin"]["display_name"] = "Admin"
            release.save_release(r)

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    # ---------------- classify table tests ----------------

    def test_classify_product_not_in_constant_is_internal(self):
        fact = {"title": "feat: new dashboard", "labels": [], "body": ""}
        res = comms.classify("admin", fact)
        self.assertFalse(res["user"])
        self.assertFalse(res["big"])
        self.assertEqual(res["group"], "internal")

    def test_classify_internal_prefixes(self):
        prefixes = ["chore", "ci", "build", "docs", "test", "refactor"]
        for p in prefixes:
            fact = {"title": f"{p}: update thing", "labels": [], "body": ""}
            res = comms.classify("app", fact)
            self.assertFalse(res["user"], f"{p} should be internal")
            self.assertEqual(res["group"], "internal")

            # With parenthesis scope e.g. chore(deps):
            fact_scope = {"title": f"{p}(core): update thing", "labels": [], "body": ""}
            res_scope = comms.classify("app", fact_scope)
            self.assertFalse(res_scope["user"], f"{p}(core) should be internal")
            self.assertEqual(res_scope["group"], "internal")

    def test_classify_internal_labels(self):
        for label in ["no-comms", "dependencies"]:
            fact = {"title": "feat: user thing", "labels": [label], "body": ""}
            res = comms.classify("app", fact)
            self.assertFalse(res["user"])
            self.assertEqual(res["group"], "internal")

    def test_classify_bot_author(self):
        fact = {"title": "feat: auto bump", "labels": [], "author": "dependabot[bot]"}
        res = comms.classify("app", fact)
        self.assertFalse(res["user"])
        self.assertEqual(res["group"], "internal")

        fact2 = {"title": "feat: auto bump", "labels": [], "is_bot": True}
        res2 = comms.classify("app", fact2)
        self.assertFalse(res2["user"])
        self.assertEqual(res2["group"], "internal")

    def test_classify_user_facing_groups(self):
        # feat -> new
        res = comms.classify("app", {"title": "feat: creator payouts", "labels": []})
        self.assertTrue(res["user"])
        self.assertEqual(res["group"], "new")

        # fix -> fixed
        res = comms.classify("app", {"title": "fix: crash on login", "labels": []})
        self.assertTrue(res["user"])
        self.assertEqual(res["group"], "fixed")

        # anything else -> improved
        res = comms.classify("app", {"title": "perf: speed up video upload", "labels": []})
        self.assertTrue(res["user"])
        self.assertEqual(res["group"], "improved")

        res_noprefix = comms.classify("app", {"title": "Better mobile experience", "labels": []})
        self.assertTrue(res_noprefix["user"])
        self.assertEqual(res_noprefix["group"], "improved")

    def test_classify_big_via_label_and_roadmap(self):
        # Big via label
        res = comms.classify("app", {"title": "feat: creator shop", "labels": ["big"]})
        self.assertTrue(res["user"])
        self.assertTrue(res["big"])

        # Big via Roadmap FEAT line in body
        res = comms.classify("app", {
            "title": "feat: creator shop",
            "labels": [],
            "body": "Here is the PR description.\nRoadmap: FEAT-42\nDetails follow.",
        })
        self.assertTrue(res["user"])
        self.assertTrue(res["big"])

        # Internal PR is never big even with label or roadmap
        res = comms.classify("app", {
            "title": "chore: internal migration",
            "labels": ["big"],
            "body": "Roadmap: FEAT-42",
        })
        self.assertFalse(res["user"])
        self.assertFalse(res["big"])

    # ---------------- changelog tests ----------------

    def test_changelog_label_ordinal_and_markdown_groups(self, monkeypatch=None):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_10": {"number": 10, "title": "feat: studio editor", "body": "Roadmap: FEAT-1", "url": "http://gh/10", "labels": {"nodes": []}},
                        "pr_11": {"number": 11, "title": "fix: audio sync", "body": "", "url": "http://gh/11", "labels": {"nodes": []}},
                        "pr_12": {"number": 12, "title": "perf: faster export", "body": "", "url": "http://gh/12", "labels": {"nodes": []}},
                        "pr_13": {"number": 13, "title": "chore: update ci", "body": "", "url": "http://gh/13", "labels": {"nodes": []}},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            # Record 2 releases on the same Tunis day: 2026-11-10 08:00 UTC and 14:00 UTC
            at1 = "2026-11-10T08:00:00Z"
            at2 = "2026-11-10T14:00:00Z"
            release.record("app", "shipped", prs=[10, 11, 12, 13], sha="abc1", now=datetime.fromisoformat(at1.replace("Z", "+00:00")))
            release.record("app", "shipped", prs=[10], sha="abc2", now=datetime.fromisoformat(at2.replace("Z", "+00:00")))

            entry1, md1 = comms.changelog("app", at=at1)
            self.assertEqual(entry1["label"], "2026-11-10")
            self.assertIn("## App 2026-11-10", md1)
            self.assertIn("### New\n- feat: studio editor (#10)", md1)
            self.assertIn("### Fixed\n- fix: audio sync (#11)", md1)
            self.assertIn("### Improved\n- perf: faster export (#12)", md1)
            self.assertIn("### Internal (not sent to users)\n- chore: update ci (#13)", md1)

            # 2nd release on same day gets (2nd) suffix
            entry2, md2 = comms.changelog("app", at=at2)
            self.assertEqual(entry2["label"], "2026-11-10 (2nd)")
            self.assertIn("## App 2026-11-10 (2nd)", md2)
            self.assertNotIn("### Fixed", md2)  # Omit empty group

            # Re-running is idempotent
            entry1_again, _ = comms.changelog("app", at=at1)
            self.assertEqual(entry1_again["label"], "2026-11-10")
        finally:
            factory.sources.gh_json = self._orig_gh

    # ---------------- due jobs tests ----------------

    def test_due_feature_job_and_friday_timing(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_10": {"number": 10, "title": "feat: big thing", "body": "Roadmap: FEAT-10", "url": "http://gh/10", "labels": {"nodes": [{"name": "big"}]}},
                        "pr_11": {"number": 11, "title": "fix: small thing", "body": "", "url": "http://gh/11", "labels": {"nodes": []}},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            # Shipped on Wednesday 2026-11-11
            at = "2026-11-11T12:00:00Z"
            release.record("app", "shipped", prs=[10, 11], sha="s1", now=datetime.fromisoformat(at.replace("Z", "+00:00")))

            # On Thursday: feature job is due, no digest job
            thursday_noon = datetime(2026, 11, 12, 12, 0, 0, tzinfo=TUNIS)
            jobs_thu = comms.due("app", now=thursday_noon)
            self.assertEqual(len(jobs_thu), 1)
            self.assertEqual(jobs_thu[0]["id"], "f-app-10")
            self.assertEqual(jobs_thu[0]["kind"], "feature")
            self.assertTrue(jobs_thu[0]["first_send"])

            # On Friday at 09:59 Tunis: digest is NOT due yet
            fri_0959 = datetime(2026, 11, 13, 9, 59, 0, tzinfo=TUNIS)
            jobs_0959 = comms.due("app", now=fri_0959)
            self.assertEqual([j["kind"] for j in jobs_0959], ["feature"])

            # On Friday at 10:00 Tunis: digest IS due
            fri_1000 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=TUNIS)
            jobs_1000 = comms.due("app", now=fri_1000)
            kinds = [j["kind"] for j in jobs_1000]
            self.assertIn("feature", kinds)
            self.assertIn("digest", kinds)

            digest = [j for j in jobs_1000 if j["kind"] == "digest"][0]
            self.assertEqual(digest["id"], "d-app-2026-11-13")
            # Feature PR 10 is unapproved, so it is excluded from digest
            self.assertEqual([p["number"] for p in digest["prs"]], [11])
            self.assertEqual(digest["recap"], [])
        finally:
            factory.sources.gh_json = self._orig_gh

    def test_due_recap_and_empty_week(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_20": {"number": 20, "title": "feat: big feature", "body": "Roadmap: FEAT-20", "url": "", "labels": [{"name": "big"}]},
                        "pr_21": {"number": 21, "title": "fix: small fix", "body": "", "url": "", "labels": []},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            at = "2026-11-11T12:00:00Z"
            release.record("app", "shipped", prs=[20, 21], sha="s2", now=datetime.fromisoformat(at.replace("Z", "+00:00")))

            now = datetime(2026, 11, 13, 10, 30, 0, tzinfo=TUNIS)
            # Feature job for PR 20 was drafted, approved, and sent!
            draft = {
                "subject": {"en": "Big feature!", "fr": "Grosse fonctionnalite !"},
                "sentences": [{"text": {"en": "We shipped PR 20.", "fr": "PR 20."}, "prs": [20]}],
                "audience": "creators",
            }
            save_v1("f-app-20", draft, now=now)
            decide_now("f-app-20", "send", now=now)
            comms.sent("f-app-20", count=500, now=now)

            # Now check due: feature 20 was sent so it's not due as a feature job
            jobs = comms.due("app", now=now)
            self.assertEqual(len(jobs), 1)
            digest = jobs[0]
            self.assertEqual(digest["kind"], "digest")
            # PR 21 is in prs, and PR 20 is in recap!
            self.assertEqual([p["number"] for p in digest["prs"]], [21])
            self.assertEqual([p["number"] for p in digest["recap"]], [20])
            self.assertFalse(digest["first_send"])  # Because PR 20 was sent

            # Empty week: store has no shipped releases this week -> no digest job
            jobs_aura = comms.due("store", now=now)
            self.assertEqual(jobs_aura, [])
        finally:
            factory.sources.gh_json = self._orig_gh

    # ---------------- draft validation tests ----------------

    def test_draft_validation_rules(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_30": {"number": 30, "title": "feat: video export", "body": "Roadmap: FEAT-30", "url": "", "labels": [{"name": "big"}]},
                        "pr_31": {"number": 31, "title": "chore: internal thing", "body": "", "url": "", "labels": []},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            at = "2026-11-11T12:00:00Z"
            release.record("app", "shipped", prs=[30, 31], sha="s3", now=datetime.fromisoformat(at.replace("Z", "+00:00")))
            now = datetime(2026, 11, 11, 14, 0, 0, tzinfo=TUNIS)

            # Rule 1: Missing subject for first language (en for app)
            draft_no_subj = {
                "subject": {"fr": "Sujet"},
                "sentences": [{"text": {"en": "Text", "fr": "Texte"}, "prs": [30]}],
                "audience": "creators",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_no_subj, now=now)
            self.assertIn("subject is missing", str(cm.exception))

            # Rule 1b: Missing sentence text for first language
            draft_no_text = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"fr": "Texte"}, "prs": [30]}],
                "audience": "creators",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_no_text, now=now)
            self.assertIn("sentence 1 is missing", str(cm.exception))

            # Rule 2: Sentence with no PR
            draft_no_pr = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"en": "Text"}, "prs": []}],
                "audience": "creators",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_no_pr, now=now)
            self.assertIn("no PR", str(cm.exception))

            # Rule 3: PR not in job's prs
            draft_wrong_pr = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"en": "Text"}, "prs": [999]}],
                "audience": "creators",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_wrong_pr, now=now)
            self.assertIn("not in this job", str(cm.exception))

            # Rule 4: Internal PR referenced
            draft_internal = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"en": "Text"}, "prs": [31]}],
                "audience": "creators",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_internal, now=now)
            self.assertIn("PR #31", str(cm.exception))

            # Rule 5: Audience not in product audiences
            draft_bad_aud = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"en": "Text"}, "prs": [30]}],
                "audience": "aliens",
            }
            with self.assertRaises(FactoryError) as cm:
                save_v1("f-app-30", draft_bad_aud, now=now)
            self.assertIn("audience", str(cm.exception))

            # Rule 6: not_before later than today Tunis
            draft_future = {
                "subject": {"en": "Subject"},
                "sentences": [{"text": {"en": "Text"}, "prs": [30]}],
                "audience": "creators",
                "not_before": "2026-11-20",
            }

            # Valid draft creates inbox ask item once
            valid_draft = {
                "subject": {"en": "Export high-res video"},
                "sentences": [{"text": {"en": "Now export 4K video."}, "prs": [30]}],
                "audience": "creators",
                "not_before": "2026-11-10",
            }
            job = save_v1("f-app-30", valid_draft, now=now)
            self.assertEqual(job["status"], "needs-approval")
            self.assertIn("ask_id", job)
            ask_id = job["ask_id"]

            inbox = store.inbox()
            ask_item = [it for it in inbox["items"] if it["id"] == ask_id][0]
            self.assertIn("Approve the App email:", ask_item["title"])
            self.assertEqual(ask_item["project"], "fac-release-mgr")

            # A new draft supersedes the open ask: the owner is asked about the version that will be sent.
            updated_draft = dict(valid_draft, subject={"en": "Export 4K video"})
            job2 = save_v1("f-app-30", updated_draft, now=now)
            self.assertNotEqual(job2["ask_id"], ask_id)
            open_ids = [it["id"] for it in store.inbox()["items"]]
            self.assertNotIn(ask_id, open_ids)
            self.assertIn(job2["ask_id"], open_ids)
            self.assertIn("Export 4K video", next(i for i in store.inbox()["items"] if i["id"] == job2["ask_id"])["context"])
            # A scheduled big feature: deferred, its open ask closed, back on its date
            job_d = comms.save_draft("f-app-30", dict(v2(draft_future), defer={"30": "2026-11-20"}), now=now)
            self.assertEqual(job_d["status"], "deferred")
            self.assertEqual(comms.due("app", now=now), [])
            back = comms.due("app", now=datetime(2026, 11, 20, 9, tzinfo=TUNIS))
            self.assertEqual([j["id"] for j in back], ["f-app-30"])
            self.assertNotIn(job2["ask_id"], [it["id"] for it in store.inbox()["items"]])
        finally:
            factory.sources.gh_json = self._orig_gh

    # ---------------- decide, sent, failed tests ----------------

    def test_decide_sent_failed_lifecycle(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_40": {"number": 40, "title": "feat: tips", "body": "Roadmap: FEAT-40", "url": "", "labels": [{"name": "big"}]},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            at = "2026-11-11T12:00:00Z"
            release.record("web", "shipped", prs=[40], sha="s4", now=datetime.fromisoformat(at.replace("Z", "+00:00")))
            now = datetime(2026, 11, 11, 14, 0, 0, tzinfo=TUNIS)

            draft = {
                "subject": {"fr": "Les pourboires sont la !"},
                "sentences": [{"text": {"fr": "Envoyez des pourboires."}, "prs": [40]}],
                "audience": "creators",
            }
            job = save_v1("f-web-40", draft, now=now)
            self.assertEqual(job["status"], "needs-approval")

            # Cannot send while in needs-approval
            with self.assertRaises(FactoryError):
                comms.sent("f-web-40", count=100, now=now)

            # Decide edit -> needs-edit; it cannot be sent until re-drafted and approved
            job_edit = decide_now("f-web-40", "edit", now=now)
            self.assertEqual(job_edit["status"], "needs-edit")
            with self.assertRaises(FactoryError):
                decide_now("f-web-40", "send", now=now)
            save_v1("f-web-40", draft, now=now)

            # Decide send -> ready
            job_ready = decide_now("f-web-40", "send", now=now)
            self.assertEqual(job_ready["status"], "ready")

            # Record failure
            job_failed = comms.failed("f-web-40", "SMTP connection timeout")
            self.assertEqual(job_failed["status"], "failed")
            self.assertEqual(job_failed["reason"], "SMTP connection timeout")

            # Can re-draft and decide again
            save_v1("f-web-40", draft, now=now)
            decide_now("f-web-40", "send", now=now)

            # Record sent
            job_sent = comms.sent("f-web-40", count=250, now=now)
            self.assertEqual(job_sent["status"], "sent")
            self.assertEqual(job_sent["count"], 250)
            self.assertTrue(job_sent.get("sent_at"))
        finally:
            factory.sources.gh_json = self._orig_gh

    # ---------------- video failed 2h fallback ----------------

    def test_video_failed_fallback_after_two_hours(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_50": {"number": 50, "title": "feat: video clip", "body": "Roadmap: FEAT-50", "url": "", "labels": [{"name": "big"}]},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            at = "2026-11-11T12:00:00Z"
            release.record("app", "shipped", prs=[50], sha="s5", now=datetime.fromisoformat(at.replace("Z", "+00:00")))
            t0 = datetime(2026, 11, 11, 12, 30, 0, tzinfo=timezone.utc)

            comms.video_failed("f-app-50", now=t0)

            # After 1 hour: static_card is not yet true
            t_1h = datetime(2026, 11, 11, 13, 30, 0, tzinfo=timezone.utc)
            jobs_1h = comms.due("app", now=t_1h)
            job_1h = [j for j in jobs_1h if j["id"] == "f-app-50"][0]
            self.assertFalse(job_1h.get("static_card", False))

            # After 2 hours: static_card becomes true
            t_2h = datetime(2026, 11, 11, 14, 31, 0, tzinfo=timezone.utc)
            jobs_2h = comms.due("app", now=t_2h)
            job_2h = [j for j in jobs_2h if j["id"] == "f-app-50"][0]
            self.assertTrue(job_2h.get("static_card"))
        finally:
            factory.sources.gh_json = self._orig_gh

    # ---------------- CLI tests ----------------

    def test_cli_commands(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_60": {"number": 60, "title": "feat: stickers", "body": "Roadmap: FEAT-60", "url": "http://gh/60", "labels": [{"name": "big"}]},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            at = "2026-11-10T08:00:00Z"
            release.record("app", "shipped", prs=[60], sha="s6", now=datetime.fromisoformat(at.replace("Z", "+00:00")))

            with store.lock():  # the CLI runs on the real clock: start comms before this release
                c = comms.comms()
                c["since"]["app"] = "2026-11-01T00:00:00Z"
                comms.save_comms(c)

            # 1. changelog command
            self.assertEqual(cli.main(["comms", "changelog", "app", "--at", at]), 0)
            self.assertEqual(cli.main(["comms", "video-failed", "f-app-60"]), 0)

            # 2. due command
            self.assertEqual(cli.main(["comms", "due", "app"]), 0)
            self.assertEqual(cli.main(["comms", "due", "--all"]), 0)

            # 3. draft validation error via CLI produces exit code 2
            draft_invalid_path = os.path.join(self.tmp.name, "bad_draft.json")
            with open(draft_invalid_path, "w") as f:
                json.dump(v2({"subject": {"fr": "Mauvaise langue"}, "sentences": []}), f)
            code_bad = cli.main(["comms", "draft", "f-app-60", "--file", draft_invalid_path])
            self.assertEqual(code_bad, 2)

            # Valid draft
            draft_path = os.path.join(self.tmp.name, "good_draft.json")
            with open(draft_path, "w") as f:
                json.dump(v2({
                    "subject": {"en": "Cool stickers"},
                    "sentences": [{"text": {"en": "Enjoy new stickers."}, "prs": [60]}],
                    "audience": "creators",
                }), f)
            code_good = cli.main(["comms", "draft", "f-app-60", "--file", draft_path])
            self.assertEqual(code_good, 0)

            # 4. decide
            ask = comms.comms()["jobs"]["f-app-60"]["ask_id"]
            self.assertEqual(cli.main(["comms", "decide", "f-app-60", "send"]), 1)  # no --ask
            self.assertEqual(cli.main(["comms", "decide", "f-app-60", "send", "--ask", ask]), 0)

            # 5. sent
            self.assertEqual(cli.main(["comms", "sent", "f-app-60", "--count", "120"]), 0)

            # 6. a sent job cannot fail or get a video alert afterwards
            self.assertEqual(cli.main(["comms", "failed", "f-app-60", "--reason", "manual test"]), 1)
            self.assertEqual(cli.main(["comms", "video-failed", "f-app-60"]), 2)

            # 8. list
            self.assertEqual(cli.main(["comms", "list", "--product", "app"]), 0)

            # 9. CLI with --json
            import io
            from contextlib import redirect_stdout
            f = io.StringIO()
            with redirect_stdout(f):
                code = cli.main(["--json", "comms", "list"])
            self.assertEqual(code, 0)
            json_data = json.loads(f.getvalue())
            self.assertTrue(isinstance(json_data, list))
        finally:
            factory.sources.gh_json = self._orig_gh

    def test_pr_facts_graphql_query_and_errors(self):
        recorded_args = []
        def fake_gh_json(*args):
            recorded_args.append(args)
            return {
                "data": {
                    "repository": {
                        "pr_1": {
                            "number": 1,
                            "title": "feat: one",
                            "body": "Roadmap: FEAT-1",
                            "url": "http://gh/1",
                            "author": {"login": "alice", "__typename": "User"},
                            "labels": {"nodes": [{"name": "big"}]},
                        },
                        "pr_2": {
                            "number": 2,
                            "title": "chore(deps): bump",
                            "body": "",
                            "url": "http://gh/2",
                            "author": {"login": "dependabot[bot]", "__typename": "Bot"},
                            "labels": {"nodes": [{"name": "dependencies"}]},
                        },
                    }
                }
            }

        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            facts = comms.pr_facts("acme/app", [1, 2])
            self.assertEqual(len(recorded_args), 1)
            query_arg = [a for a in recorded_args[0] if a.startswith("query=")][0]
            self.assertIn("pr_1: pullRequest(number: 1)", query_arg)
            self.assertIn("pr_2: pullRequest(number: 2)", query_arg)

            self.assertEqual(facts[1]["title"], "feat: one")
            self.assertFalse(facts[1]["is_bot"])
            self.assertEqual(facts[2]["title"], "chore(deps): bump")
            self.assertTrue(facts[2]["is_bot"])

            # gh error raises FactoryError
            def fake_gh_error(*args):
                return {"errors": [{"message": "API rate limit exceeded"}]}
            factory.sources.gh_json = fake_gh_error
            with self.assertRaises(FactoryError):
                comms.pr_facts("acme/app", [1])
        finally:
            factory.sources.gh_json = self._orig_gh

    def test_draft_ready_path_when_not_first_send(self):
        def fake_gh_json(*args):
            return {
                "data": {
                    "repository": {
                        "pr_70": {"number": 70, "title": "fix: bug", "body": "", "url": "", "labels": []},
                    }
                }
            }
        import factory.sources
        self._orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh_json
        try:
            now = datetime(2026, 11, 13, 11, 0, 0, tzinfo=TUNIS)

            # Pre-record a sent job for app so first_send is False
            with store.lock():
                c = comms.comms()
                c["jobs"]["f-app-1"] = {"id": "f-app-1", "product": "app", "kind": "feature", "status": "sent"}
                comms.save_comms(c)

            at = "2026-11-12T10:00:00Z"
            release.record("app", "shipped", prs=[70], sha="s7", now=datetime.fromisoformat(at.replace("Z", "+00:00")))

            due_jobs = comms.due("app", now=now)
            digest = [j for j in due_jobs if j["kind"] == "digest"][0]
            self.assertFalse(digest["first_send"])

            inbox_before = len(store.inbox()["items"])
            draft = {
                "subject": {"en": "Weekly update"},
                "sentences": [{"text": {"en": "Fixed a bug."}, "prs": [70]}],
                "audience": "creators",
            }
            job = save_v1(digest["id"], draft, now=now)
            self.assertEqual(job["status"], "ready")
            # No approval ask created
            inbox_after = len(store.inbox()["items"])
            self.assertEqual(inbox_before, inbox_after)
            self.assertIsNone(job.get("ask_id"))
        finally:
            factory.sources.gh_json = self._orig_gh


if __name__ == "__main__":
    unittest.main()


class CommsRegressionTest(unittest.TestCase):
    """Review fixes: since, rollback, digest window, terminal states, video on unknown jobs, ask thread."""

    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown

    def _gh(self, prs):
        def fake(*args):
            return {"data": {"repository": {f"pr_{n}": v for n, v in prs.items()}}}
        import factory.sources
        orig = factory.sources.gh_json
        factory.sources.gh_json = fake
        self.addCleanup(setattr, factory.sources, "gh_json", orig)

    def _ship(self, prs, at):
        release.record("app", "shipped", prs=prs, sha="s", now=datetime.fromisoformat(at.replace("Z", "+00:00")))

    def _draft(self, prs):
        return {"subject": {"text": {"en": "News"}, "prs": prs},
                "sentences": [{"text": {"en": "A thing."}, "prs": prs, "audience": "creators"}]}

    def test_first_run_ignores_releases_before_the_week(self):
        self._gh({1: {"number": 1, "title": "feat: old big", "body": "", "labels": {"nodes": [{"name": "big"}]}}})
        self._ship([1], "2026-10-20T12:00:00Z")
        self.assertEqual(comms.due("app", now=datetime(2026, 11, 12, 12, tzinfo=TUNIS)), [])

    def test_rolled_back_pr_is_not_announced(self):
        self._gh({2: {"number": 2, "title": "feat: big", "body": "", "labels": {"nodes": [{"name": "big"}]}},
                  3: {"number": 3, "title": "fix: small", "body": "", "labels": {"nodes": []}}})
        self._ship([2, 3], "2026-11-10T09:00:00Z")
        release.record("app", "rolled-back", prs=[2, 3], sha="s", note="bad",
                       now=datetime(2026, 11, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(comms.due("app", now=datetime(2026, 11, 13, 11, tzinfo=TUNIS)), [])

    def test_second_digest_does_not_repeat_last_week(self):
        self._gh({4: {"number": 4, "title": "fix: week one", "body": "", "labels": {"nodes": []}},
                  5: {"number": 5, "title": "fix: week two", "body": "", "labels": {"nodes": []}}})
        self._ship([4], "2026-11-10T09:00:00Z")
        fri1 = datetime(2026, 11, 13, 11, tzinfo=TUNIS)
        comms.save_draft("d-app-2026-11-13", self._draft([4]), now=fri1)
        decide_now("d-app-2026-11-13", "send", now=fri1)
        comms.sent("d-app-2026-11-13", 10, now=fri1)
        self._ship([5], "2026-11-17T09:00:00Z")
        jobs = comms.due("app", now=datetime(2026, 11, 20, 11, tzinfo=TUNIS))
        self.assertEqual([[p["n"] for p in j["prs"]] for j in jobs], [[5]])

    def test_sent_job_cannot_be_drafted_or_decided_again(self):
        self._gh({6: {"number": 6, "title": "fix: x", "body": "", "labels": {"nodes": []}}})
        self._ship([6], "2026-11-10T09:00:00Z")
        fri = datetime(2026, 11, 13, 11, tzinfo=TUNIS)
        job = comms.save_draft("d-app-2026-11-13", self._draft([6]), now=fri)
        self.assertEqual(job["status"], "needs-approval")  # first send
        decide_now(job["id"], "send", now=fri)
        comms.sent(job["id"], 3, now=fri)
        with self.assertRaises(FactoryError):
            decide_now(job["id"], "send", now=fri)
        with self.assertRaises(CommsValidationError):
            comms.save_draft(job["id"], self._draft([6]), now=fri)

    def test_video_failed_on_unknown_job_refuses(self):
        with self.assertRaises(CommsValidationError):
            comms.video_failed("f-app-999", now=datetime(2026, 11, 12, 12, tzinfo=TUNIS))
        self.assertEqual(comms.comms()["jobs"], {})

    def test_approval_ask_goes_to_the_product_thread(self):
        self._gh({7: {"number": 7, "title": "feat: big", "body": "Roadmap: FEAT-7", "labels": {"nodes": []}}})
        self._ship([7], "2026-11-10T09:00:00Z")
        job = comms.save_draft("f-app-7", self._draft([7]), now=datetime(2026, 11, 10, 12, tzinfo=TUNIS))
        item = next(i for i in store.inbox()["items"] if i["id"] == job["ask_id"])
        self.assertEqual(item["thread"], "t-c2e")
        self.assertIn("f-app-7", item["why"])

    def test_set_copy_validation_and_markdown_titles(self):
        self._gh({
            10: {"number": 10, "title": "feat: studio editor", "body": "Roadmap: FEAT-1", "url": "http://gh/10", "labels": {"nodes": []}},
            11: {"number": 11, "title": "chore: internal tooling", "body": "", "url": "http://gh/11", "labels": {"nodes": []}},
        })
        at = "2026-11-10T08:00:00Z"
        self._ship([10, 11], at)
        key = f"app@{at}"
        entry, md_initial = comms.changelog("app", at=at)

        # Refuse unknown changelog
        with self.assertRaises(CommsValidationError):
            comms.set_copy("app@2099-01-01T00:00:00Z", {"10": {"en": "Nice studio"}})

        # Refuse PR not in changelog
        with self.assertRaises(CommsValidationError):
            comms.set_copy(key, {"99": {"en": "Not there"}})

        # Refuse internal PR
        with self.assertRaises(CommsValidationError):
            comms.set_copy(key, {"11": {"en": "Internal change"}})

        # Refuse unknown language
        with self.assertRaises(CommsValidationError):
            comms.set_copy(key, {"10": {"de": "Guten Tag"}})

        # Refuse empty string
        with self.assertRaises(CommsValidationError):
            comms.set_copy(key, {"10": {"en": "   "}})

        # Valid set_copy
        updated = comms.set_copy(key, {"10": {"en": "Creator Studio editor with faster trims", "fr": "Éditeur studio pour créateurs"}})
        it10 = next(i for i in updated["items"] if i["n"] == 10)
        self.assertEqual(it10["copy"]["en"], "Creator Studio editor with faster trims")
        self.assertEqual(it10["copy"]["fr"], "Éditeur studio pour créateurs")

        # Changelog markdown uses first language (en for app) when titles=False
        _, md_copy = comms.changelog("app", at=at, titles=False)
        self.assertIn("- Creator Studio editor with faster trims (#10)", md_copy)
        self.assertNotIn("- feat: studio editor (#10)", md_copy)

        # titles=True shows original PR title
        _, md_titles = comms.changelog("app", at=at, titles=True)
        self.assertIn("- feat: studio editor (#10)", md_titles)
        self.assertNotIn("- Creator Studio editor with faster trims (#10)", md_titles)

        # CLI factory comms copy
        copies_file = os.path.join(self.tmp.name, "copies.json")
        with open(copies_file, "w", encoding="utf-8") as f:
            json.dump({"10": {"en": "CLI copy update"}}, f)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["comms", "copy", key, "--file", copies_file])
        self.assertEqual(code, 0)
        self.assertIn(f"copy saved for {key}", out.getvalue())

        entry_after_cli, md_cli = comms.changelog("app", at=at)
        it10_cli = next(i for i in entry_after_cli["items"] if i["n"] == 10)
        self.assertEqual(it10_cli["copy"]["en"], "CLI copy update")
        self.assertIn("- CLI copy update (#10)", md_cli)

    def test_changelog_refresh_preserves_copy(self):
        self._gh({
            10: {"number": 10, "title": "feat: studio editor v1", "body": "Roadmap: FEAT-1", "url": "http://gh/10", "labels": {"nodes": []}},
            11: {"number": 11, "title": "fix: bugfix", "body": "", "url": "http://gh/11", "labels": {"nodes": []}},
        })
        at = "2026-11-10T08:00:00Z"
        self._ship([10, 11], at)
        key = f"app@{at}"
        entry, _ = comms.changelog("app", at=at)
        comms.set_copy(key, {"10": {"en": "Studio editor with speed trim"}})

        # Update gh facts: PR 10 title changes on GitHub
        self._gh({
            10: {"number": 10, "title": "feat: studio editor v2 updated", "body": "Roadmap: FEAT-1", "url": "http://gh/10", "labels": {"nodes": []}},
            11: {"number": 11, "title": "fix: bugfix updated", "body": "", "url": "http://gh/11", "labels": {"nodes": []}},
        })

        # Calling changelog without refresh returns stored changelog (title still v1)
        entry_cached, _ = comms.changelog("app", at=at, refresh=False)
        it10_cached = next(i for i in entry_cached["items"] if i["n"] == 10)
        self.assertEqual(it10_cached["title"], "feat: studio editor v1")

        # Calling changelog with refresh=True re-fetches facts, updates title, but PRESERVES stored copy
        entry_refreshed, md_refreshed = comms.changelog("app", at=at, refresh=True)
        it10_refreshed = next(i for i in entry_refreshed["items"] if i["n"] == 10)
        self.assertEqual(it10_refreshed["title"], "feat: studio editor v2 updated")
        self.assertEqual(it10_refreshed["copy"]["en"], "Studio editor with speed trim")
        self.assertIn("- Studio editor with speed trim (#10)", md_refreshed)

        # CLI factory comms changelog --refresh
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["comms", "changelog", "app", "--at", at, "--refresh"])
        self.assertEqual(code, 0)
        self.assertIn("Studio editor with speed trim (#10)", out.getvalue())



class CommsRound1Test(unittest.TestCase):
    """Review round 1 fixes."""

    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown
    _gh = CommsRegressionTest._gh
    _ship = CommsRegressionTest._ship
    _draft = CommsRegressionTest._draft

    FRI = datetime(2026, 11, 13, 11, tzinfo=TUNIS)

    def _small(self, *ns):
        self._gh({n: {"number": n, "title": f"fix: thing {n}", "body": "", "labels": {"nodes": []}} for n in ns})

    def _send_digest(self, job_id, prs, now):
        comms.save_draft(job_id, self._draft(prs), now=now)
        if comms.comms()["jobs"][job_id]["status"] == "needs-approval":
            decide_now(job_id, "send", now=now)
        return comms.sent(job_id, 5, now=now)

    def test_failed_only_from_ready(self):
        self._small(1)
        self._ship([1], "2026-11-10T09:00:00Z")
        self._send_digest("d-app-2026-11-13", [1], self.FRI)
        with self.assertRaises(FactoryError):
            comms.failed("d-app-2026-11-13", "late webhook")
        self.assertEqual(comms.comms()["jobs"]["d-app-2026-11-13"]["status"], "sent")

    def test_unfinished_digest_stays_due_after_friday(self):
        self._small(1, 2)
        self._ship([1], "2026-11-10T09:00:00Z")
        comms.save_draft("d-app-2026-11-13", self._draft([1]), now=self.FRI)  # first send: waits on owner
        sat = datetime(2026, 11, 14, 12, tzinfo=TUNIS)
        self.assertEqual([j["id"] for j in comms.due("app", now=sat)], ["d-app-2026-11-13"])
        self._ship([2], "2026-11-17T09:00:00Z")
        fri2 = datetime(2026, 11, 20, 11, tzinfo=TUNIS)
        jobs = {j["id"]: [p["n"] for p in j["prs"]] for j in comms.due("app", now=fri2)}
        self.assertEqual(jobs, {"d-app-2026-11-13": [1], "d-app-2026-11-20": [2]})

    def test_stored_digest_drops_pr_rolled_back_after_drafting(self):
        self._small(1, 2)
        self._ship([1, 2], "2026-11-10T09:00:00Z")
        comms.save_draft("d-app-2026-11-13", self._draft([1, 2]), now=self.FRI)
        release.record("app", "rolled-back", prs=[2], sha="s", note="bad",
                       now=datetime(2026, 11, 13, 10, 30, tzinfo=TUNIS))
        later = datetime(2026, 11, 13, 12, tzinfo=TUNIS)
        job = comms.due("app", now=later)[0]
        self.assertEqual([p["n"] for p in job["prs"]], [1])
        with self.assertRaises(FactoryError):
            decide_now("d-app-2026-11-13", "send", now=later)  # the draft still cites #2

    def test_unreadable_pr_is_internal(self):
        self._gh({})
        self._ship([9], "2026-11-10T09:00:00Z")
        entry, _ = comms.changelog("app")
        self.assertEqual(entry["items"][0]["group"], "internal")
        self.assertEqual(comms.due("app", now=self.FRI), [])

    def test_internal_tooling_in_product_repos(self):
        cases = [("store", "feat(shop-manager): restock view"), ("store", "fix(suppliers): retry"),
                 ("web", "feat(tracing): user agent"), ("app", "feat(closing): admin payout review"),
                 ("app", "[agent] fix: flaky job"), ("app", "feat(admin): filters")]
        for product, title in cases:
            res = comms.classify(product, {"title": title, "body": "Roadmap: FEAT-1", "labels": []})
            self.assertFalse(res["user"], title)
            self.assertFalse(res["big"], title)
        self.assertTrue(comms.classify("web", {"title": "feat(merchant-api): application form"})["user"])

    def test_deferred_pr_joins_the_first_digest_on_its_date(self):
        self._small(1, 2)
        self._ship([1, 2], "2026-11-10T09:00:00Z")
        draft = dict(self._draft([1]), defer={"2": "2026-11-27"})
        comms.save_draft("d-app-2026-11-13", draft, now=self.FRI)
        decide_now("d-app-2026-11-13", "send", now=self.FRI)
        comms.sent("d-app-2026-11-13", 5, now=self.FRI)
        early = comms.due("app", now=datetime(2026, 11, 20, 11, tzinfo=TUNIS))
        self.assertEqual(early, [])
        on_date = comms.due("app", now=datetime(2026, 11, 27, 11, tzinfo=TUNIS))
        self.assertEqual([[p["n"] for p in j["prs"]] for j in on_date], [[2]])
        bad = dict(self._draft([1, 2]), defer={"2": "2026-11-27"})
        with self.assertRaises(CommsValidationError):  # a deferred PR cannot be cited early
            comms.save_draft("d-app-2026-11-13", bad, now=self.FRI)

    def test_audience_per_sentence_and_claims_everywhere(self):
        self._small(1, 2)
        self._ship([1, 2], "2026-11-10T09:00:00Z")
        d = {"subject": {"text": {"en": "News"}, "prs": [1]},
             "sentences": [{"text": {"en": "For brands."}, "prs": [1], "audience": "brands"},
                           {"text": {"en": "For creators."}, "prs": [2], "audience": "creators"}],
             "caption": {"text": {"en": "New this week"}, "prs": [1, 2]},
             "video": {"url": "https://x/v.mp4", "frames": [{"text": {"en": "Frame"}, "prs": [2]}]}}
        job = comms.save_draft("d-app-2026-11-13", d, now=self.FRI)
        self.assertIn("brands, creators", next(i for i in store.inbox()["items"] if i["id"] == job["ask_id"])["context"])
        for broken in [dict(d, caption={"text": {"en": "No PR"}, "prs": []}),
                       dict(d, video={"url": "https://x/v.mp4", "frames": [{"text": {"en": "F"}, "prs": [77]}]}),
                       dict(d, subject={"text": {"en": "S", "fr": " "}, "prs": [1]}),
                       dict(d, sentences=[{"text": {"en": "T"}, "prs": [True], "audience": "brands"}]),
                       dict(d, sentences=[{"text": {"en": "T"}, "prs": [1], "audience": "fans"}])]:
            with self.assertRaises(CommsValidationError):
                comms.save_draft("d-app-2026-11-13", broken, now=self.FRI)

    def test_non_object_draft_exits_2(self):
        path = os.path.join(self.tmp.name, "list.json")
        with open(path, "w") as f:
            json.dump([1, 2], f)
        self.assertEqual(cli.main(["comms", "draft", "d-app-2026-11-13", "--file", path]), 2)

    def test_video_failure_alerts_once_and_stores_static_card(self):
        self._gh({5: {"number": 5, "title": "feat: big", "body": "", "labels": {"nodes": [{"name": "big"}]}}})
        self._ship([5], "2026-11-10T09:00:00Z")
        t0 = datetime(2026, 11, 10, 12, tzinfo=TUNIS)
        comms.video_failed("f-app-5", now=t0)
        comms.video_failed("f-app-5", now=t0)
        urgent = [i for i in store.inbox()["items"] if i["kind"] == "urgent"]
        self.assertEqual(len(urgent), 1)
        self.assertEqual(urgent[0]["thread"], "t-c2e")
        comms.due("app", now=datetime(2026, 11, 10, 14, 1, tzinfo=TUNIS))
        self.assertTrue(comms.comms()["jobs"]["f-app-5"]["static_card"])

    def test_big_feature_sent_after_its_friday_is_recapped_once(self):
        self._gh({5: {"number": 5, "title": "feat: big", "body": "", "labels": {"nodes": [{"name": "big"}]}},
                  6: {"number": 6, "title": "fix: small", "body": "", "labels": {"nodes": []}},
                  7: {"number": 7, "title": "fix: next", "body": "", "labels": {"nodes": []}}})
        self._ship([5, 6], "2026-11-10T09:00:00Z")
        self._send_digest("d-app-2026-11-13", [6], self.FRI)  # #5 still held
        mon = datetime(2026, 11, 16, 12, tzinfo=TUNIS)
        comms.save_draft("f-app-5", self._draft([5]), now=mon)
        decide_now("f-app-5", "send", now=mon)
        comms.sent("f-app-5", 5, now=mon)
        self._ship([7], "2026-11-17T09:00:00Z")
        fri2 = datetime(2026, 11, 20, 11, tzinfo=TUNIS)
        job = comms.due("app", now=fri2)[0]
        self.assertEqual([p["n"] for p in job["recap"]], [5])
        self._send_digest(job["id"], [7], fri2)
        self._ship([8], "2026-11-24T09:00:00Z")
        self._gh({8: {"number": 8, "title": "fix: later", "body": "", "labels": {"nodes": []}}})
        self.assertEqual(comms.due("app", now=datetime(2026, 11, 27, 11, tzinfo=TUNIS))[0]["recap"], [])


class CommsRound2Test(unittest.TestCase):
    """Review round 2 fixes."""

    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown
    _gh = CommsRegressionTest._gh
    _ship = CommsRegressionTest._ship
    _draft = CommsRegressionTest._draft
    _small = CommsRound1Test._small
    FRI = CommsRound1Test.FRI

    def test_deferred_pr_held_by_unfinished_digest_is_not_offered_twice(self):
        self._small(4, 5)
        self._ship([4, 5], "2026-11-10T09:00:00Z")
        comms.save_draft("d-app-2026-11-13", dict(self._draft([4]), defer={"5": "2026-11-25"}), now=self.FRI)
        decide_now("d-app-2026-11-13", "send", now=self.FRI)
        comms.sent("d-app-2026-11-13", 5, now=self.FRI)
        fri3 = datetime(2026, 11, 27, 11, tzinfo=TUNIS)
        comms.save_draft("d-app-2026-11-27", self._draft([5]), now=fri3)
        comms.failed("d-app-2026-11-27", "provider down")
        fri4 = datetime(2026, 12, 4, 11, tzinfo=TUNIS)
        self.assertEqual([(j["id"], [p["n"] for p in j["prs"]]) for j in comms.due("app", now=fri4)],
                         [("d-app-2026-11-27", [5])])

    def test_failed_digest_keeps_its_deferred_pr_out_of_the_next_digest(self):
        self._small(4, 5)
        self._ship([4, 5], "2026-11-10T09:00:00Z")
        comms.save_draft("d-app-2026-11-13", dict(self._draft([4]), defer={"5": "2026-11-25"}), now=self.FRI)
        decide_now("d-app-2026-11-13", "send", now=self.FRI)
        comms.failed("d-app-2026-11-13", "provider down")
        fri3 = datetime(2026, 11, 27, 11, tzinfo=TUNIS)
        due_ids = [j["id"] for j in comms.due("app", now=fri3)]
        self.assertEqual(due_ids, ["d-app-2026-11-13"])  # #5 is held by the unfinished digest

    def test_answer_to_a_superseded_ask_is_refused(self):
        self._gh({7: {"number": 7, "title": "feat: big", "body": "", "labels": {"nodes": [{"name": "big"}]}}})
        self._ship([7], "2026-11-10T09:00:00Z")
        now = datetime(2026, 11, 10, 12, tzinfo=TUNIS)
        v1 = comms.save_draft("f-app-7", self._draft([7]), now=now)
        comms.save_draft("f-app-7", self._draft([7]), now=now)
        with self.assertRaises(FactoryError):
            comms.decide("f-app-7", "send", ask=v1["ask_id"], now=now)
        self.assertEqual(comms.comms()["jobs"]["f-app-7"]["status"], "needs-approval")

    def test_sent_rechecks_production(self):
        self._small(4)
        self._ship([4], "2026-11-10T09:00:00Z")
        with store.lock():
            c = comms.comms()
            c["jobs"]["f-app-1"] = {"id": "f-app-1", "product": "app", "kind": "feature", "status": "sent"}
            comms.save_comms(c)
        job = comms.save_draft("d-app-2026-11-13", self._draft([4]), now=self.FRI)
        self.assertEqual(job["status"], "ready")
        release.record("app", "rolled-back", prs=[4], sha="s", note="bad",
                       now=datetime(2026, 11, 13, 10, 30, tzinfo=TUNIS))
        later = datetime(2026, 11, 13, 12, tzinfo=TUNIS)
        self.assertFalse(comms.check("d-app-2026-11-13", now=later)["ok"])
        # sent records a send that already happened: it never refuses, so the job is not sent twice
        self.assertEqual(comms.sent("d-app-2026-11-13", 5, now=later)["status"], "sent")

    def test_skip_stops_a_failing_digest(self):
        self._small(4)
        self._ship([4], "2026-11-10T09:00:00Z")
        comms.save_draft("d-app-2026-11-13", self._draft([4]), now=self.FRI)
        decide_now("d-app-2026-11-13", "send", now=self.FRI)
        comms.failed("d-app-2026-11-13", "provider down")
        self.assertEqual(comms.decide("d-app-2026-11-13", "skip")["status"], "skipped")
        self.assertEqual(comms.due("app", now=datetime(2026, 11, 14, 12, tzinfo=TUNIS)), [])

    def test_due_unknown_product_errors(self):
        self.assertEqual(cli.main(["comms", "due", "nope"]), 1)


class CommsSpecRound2Test(unittest.TestCase):
    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown

    def test_build_test_db_scopes_are_internal(self):
        for title in ["fix(build): cap the Node heap", "fix(tests): reliable suite", "feat(db): never-delete phase 2"]:
            self.assertFalse(comms.classify("app", {"title": title})["user"], title)


class CommsRound3Test(unittest.TestCase):
    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown
    _gh = CommsRegressionTest._gh
    _ship = CommsRegressionTest._ship
    _draft = CommsRegressionTest._draft

    def test_check_gate_and_cli_exit_3(self):
        self._gh({4: {"number": 4, "title": "fix: a", "body": "", "labels": {"nodes": []}}})
        self._ship([4], "2026-11-02T09:00:00Z")
        with store.lock():
            c = comms.comms()
            c["since"]["app"] = "2026-11-01T00:00:00Z"
            c["jobs"]["d-app-2026-11-06"] = {
                "id": "d-app-2026-11-06", "product": "app", "kind": "digest", "status": "ready",
                "prs": [{"n": 4}], "recap": [], "until": "2026-11-06T09:00:00Z",
                "draft": {"subject": {"text": {"en": "S"}, "prs": [4]}, "sentences": []}}
            comms.save_comms(c)
        self.assertEqual(cli.main(["comms", "check", "d-app-2026-11-06"]), 0)
        release.record("app", "rolled-back", prs=[4], sha="s", note="bad")
        self.assertEqual(cli.main(["comms", "check", "d-app-2026-11-06"]), 3)

    def test_deferred_feature_can_be_skipped(self):
        self._gh({5: {"number": 5, "title": "feat: sale", "body": "", "labels": {"nodes": [{"name": "big"}]}}})
        self._ship([5], "2026-11-10T09:00:00Z")
        now = datetime(2026, 11, 10, 12, tzinfo=TUNIS)
        comms.save_draft("f-app-5", dict(self._draft([5]), defer={"5": "2026-11-27"}), now=now)
        self.assertEqual(comms.decide("f-app-5", "skip")["status"], "skipped")

    def test_check_refuses_a_pr_already_announced_in_a_sent_digest(self):
        self._gh({4: {"number": 4, "title": "fix: a", "body": "", "labels": {"nodes": []}}})
        self._ship([4], "2026-11-02T09:00:00Z")
        draft = {"subject": {"text": {"en": "S"}, "prs": [4]}, "sentences": []}
        with store.lock():
            c = comms.comms()
            c["since"]["app"] = "2026-11-01T00:00:00Z"
            for jid, status in (("d-app-2026-11-06", "sent"), ("d-app-2026-11-13", "ready")):
                c["jobs"][jid] = {"id": jid, "product": "app", "kind": "digest", "status": status,
                                  "prs": [{"n": 4}], "recap": [], "draft": draft}
            comms.save_comms(c)
        res = comms.check("d-app-2026-11-13")
        self.assertFalse(res["ok"])
        self.assertIn("already announced", res["reason"])


class CommsRound4Test(unittest.TestCase):
    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown

    def test_serve_comms_data_read_only_when_empty(self):
        from factory import serve, sources
        def fail_gh(*args, **kwargs):
            raise AssertionError("gh_json must not be called during read-only comms_data")
        orig_gh = sources.gh_json
        sources.gh_json = fail_gh
        try:
            comms_file = os.path.join(self.tmp.name, "comms.json")
            self.assertFalse(os.path.exists(comms_file))

            data = serve.comms_data()
            self.assertIn("products", data)
            for prod in ("app", "web", "store"):
                self.assertIn(prod, data["products"])
                self.assertTrue(data["products"][prod].get("not_started"))
                self.assertEqual(data["products"][prod].get("due"), [])

            self.assertFalse(os.path.exists(comms_file))
        finally:
            sources.gh_json = orig_gh

    def test_changelog_copy_race_preserves_concurrent_copy(self):
        from unittest.mock import patch
        import factory.sources

        fake_facts = {
            10: {"number": 10, "title": "feat: editor", "body": "Roadmap: FEAT-1", "url": "http://gh/10", "labels": {"nodes": []}},
        }
        def fake_gh(*args):
            return {"data": {"repository": {"pr_10": fake_facts[10]}}}

        orig_gh = factory.sources.gh_json
        factory.sources.gh_json = fake_gh
        try:
            at = "2026-11-10T08:00:00Z"
            release.record("app", "shipped", prs=[10], sha="s10", now=datetime.fromisoformat(at.replace("Z", "+00:00")))
            key = f"app@{at}"
            comms.changelog("app", at=at)

            orig_pr_facts = comms.pr_facts
            def race_pr_facts(*args, **kwargs):
                res = orig_pr_facts(*args, **kwargs)
                comms.set_copy(key, {"10": {"en": "Concurrent Copy text"}})
                return res

            with patch("factory.comms.pr_facts", side_effect=race_pr_facts):
                entry_after, _ = comms.changelog("app", at=at, refresh=True)

            it10 = next(i for i in entry_after["items"] if i["n"] == 10)
            self.assertEqual(it10.get("copy", {}).get("en"), "Concurrent Copy text")

            stored_entry = comms.comms()["changelogs"][key]
            stored_it10 = next(i for i in stored_entry["items"] if i["n"] == 10)
            self.assertEqual(stored_it10.get("copy", {}).get("en"), "Concurrent Copy text")
        finally:
            factory.sources.gh_json = orig_gh

    def test_rules_in_serve_comms_data(self):
        from factory import serve
        data = serve.comms_data()
        for prod, p_data in data["products"].items():
            rules = p_data.get("rules", {})
            self.assertIn("internal_list", rules)
            self.assertIn("language_order", rules)
            self.assertIn("audiences", rules)
            il = rules["internal_list"]
            self.assertEqual(il, sorted(list(set(il))))
            self.assertNotIn("internal_prefixes", rules)
            self.assertNotIn("internal_scopes", rules)
            self.assertNotIn("languages", rules)

    def test_window_rolls_to_next_friday_when_today_cuts_no_digest(self):
        # Fri 13 Nov 14:00: nothing for today's digest, PR 5 shipped at 10:30 belongs to Fri 20 Nov.
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-08T23:00:00Z"
            c["changelogs"]["web@2026-11-13T09:30:00Z"] = {
                "product": "web", "at": "2026-11-13T09:30:00Z", "label": "x",
                "items": [{"n": 5, "title": "fix: a", "group": "fixed"}]}
            comms.save_comms(c)
        w = comms.window("web", now=datetime(2026, 11, 13, 14, 0, tzinfo=TUNIS))
        self.assertEqual(release._parse_utc(w["until"]), release._parse_utc("2026-11-20T09:00:00Z"))
        self.assertEqual([p["n"] for p in w["pending"]], [5])
        due = comms.due("web", now=datetime(2026, 11, 20, 10, 0, tzinfo=TUNIS), write=False)
        self.assertEqual([[p["n"] for p in j["prs"]] for j in due if j["kind"] == "digest"], [[5]])

    def test_window_keeps_today_while_a_digest_would_be_cut(self):
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-08T23:00:00Z"
            c["changelogs"]["web@2026-11-10T09:00:00Z"] = {
                "product": "web", "at": "2026-11-10T09:00:00Z", "label": "x",
                "items": [{"n": 6, "title": "fix: b", "group": "fixed"}]}
            comms.save_comms(c)
        w = comms.window("web", now=datetime(2026, 11, 13, 14, 0, tzinfo=TUNIS))
        self.assertEqual(release._parse_utc(w["until"]), release._parse_utc("2026-11-13T09:00:00Z"))
        self.assertEqual([p["n"] for p in w["pending"]], [6])

    def test_window_big_pending_skips_a_launch_deferred_past_the_window(self):
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-08T23:00:00Z"
            c["changelogs"]["web@2026-11-10T09:00:00Z"] = {
                "product": "web", "at": "2026-11-10T09:00:00Z", "label": "x",
                "items": [{"n": 14, "title": "feat: sale", "group": "new", "big": True}]}
            c["deferred"]["web"] = {"14": "2026-11-25"}
            comms.save_comms(c)
        w = comms.window("web", now=datetime(2026, 11, 11, 12, 0, tzinfo=TUNIS))
        self.assertEqual(w["big_pending"], [])

    def test_window_release_falls_in_window_ending_next_friday(self):
        # A release on Fri 6 Nov 11:00 Tunis falls in the window that ends Fri 13 Nov 10:00.
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-02T00:00:00Z"
            # Previous digest cut on Fri 6 Nov 10:00 Tunis
            c["jobs"]["d-web-2026-11-06"] = {
                "id": "d-web-2026-11-06",
                "product": "web",
                "kind": "digest",
                "status": "sent",
                "until": "2026-11-06T09:00:00Z",  # 10:00 Tunis
                "prs": [90],
            }
            # Release on Fri 6 Nov 11:00 Tunis (10:00 UTC)
            c["changelogs"]["web@2026-11-06T10:00:00Z"] = {
                "product": "web",
                "at": "2026-11-06T10:00:00Z",
                "label": "v1.2",
                "items": [{"n": 101, "title": "feat: shiny button", "group": "new"}],
            }
            comms.save_comms(c)

        now = datetime(2026, 11, 6, 11, 0, 0, tzinfo=TUNIS)
        w = comms.window("web", now=now)
        self.assertFalse(w["not_started"])
        self.assertEqual(release._parse_utc(w["cutoff"]), release._parse_utc("2026-11-06T09:00:00Z"))
        self.assertEqual(release._parse_utc(w["until"]), release._parse_utc("2026-11-13T09:00:00Z"))
        self.assertEqual(w["pending"], [{"n": 101, "title": "feat: shiny button"}])

    def test_window_saturday_after_undrafted_friday_still_shows_pending(self):
        # A Saturday after an undrafted Friday digest still shows those PRs as pending.
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-02T00:00:00Z"
            # Previous digest was Fri 6 Nov 10:00 Tunis
            c["jobs"]["d-web-2026-11-06"] = {
                "id": "d-web-2026-11-06",
                "product": "web",
                "kind": "digest",
                "status": "sent",
                "until": "2026-11-06T09:00:00Z",
                "prs": [90],
            }
            # Mid-week release on Tue 10 Nov
            c["changelogs"]["web@2026-11-10T12:00:00Z"] = {
                "product": "web",
                "at": "2026-11-10T12:00:00Z",
                "label": "v1.3",
                "items": [{"n": 105, "title": "fix: bugfix", "group": "fixed"}],
            }
            # No digest job was stored for Fri 13 Nov (undrafted)
            comms.save_comms(c)

        # Saturday 14 Nov 12:00 Tunis
        sat = datetime(2026, 11, 14, 12, 0, 0, tzinfo=TUNIS)
        w = comms.window("web", now=sat)
        self.assertFalse(w["not_started"])
        self.assertEqual(release._parse_utc(w["cutoff"]), release._parse_utc("2026-11-06T09:00:00Z"))
        self.assertEqual(release._parse_utc(w["until"]), release._parse_utc("2026-11-20T09:00:00Z"))
        self.assertEqual(w["pending"], [{"n": 105, "title": "fix: bugfix"}])

    def test_window_big_only_week_has_empty_pending_and_non_empty_big_pending(self):
        # A big-only week has empty pending and a non-empty big_pending.
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-02T00:00:00Z"
            c["changelogs"]["web@2026-11-10T12:00:00Z"] = {
                "product": "web",
                "at": "2026-11-10T12:00:00Z",
                "label": "v2.0",
                "items": [{"n": 200, "title": "feat: big revamp", "group": "new", "big": True}],
            }
            comms.save_comms(c)

        now = datetime(2026, 11, 10, 14, 0, 0, tzinfo=TUNIS)
        w = comms.window("web", now=now)
        self.assertEqual(w["pending"], [])
        self.assertEqual(w["big_pending"], [{"n": 200, "title": "feat: big revamp"}])

    def test_window_unsynced_journal_event_listed(self):
        # An unsynced journal event is listed.
        rel = release.release()
        rel["journal"].append({
            "product": "web",
            "event": "shipped",
            "at": "2026-11-10T15:00:00Z",
            "sha": "abc1234",
            "prs": [301, 302],
        })
        release.save_release(rel)

        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-02T00:00:00Z"
            comms.save_comms(c)

        w = comms.window("web", now=datetime(2026, 11, 10, 16, 0, 0, tzinfo=TUNIS))
        self.assertEqual(len(w["unsynced"]), 1)
        self.assertEqual(w["unsynced"][0]["at"], "2026-11-10T15:00:00Z")
        self.assertEqual(w["unsynced"][0]["prs"], [301, 302])

    def test_window_read_only_and_no_gh_calls(self):
        # No file changes, and gh is never called (mock it to raise).
        from factory import sources
        def fail_gh(*args, **kwargs):
            raise AssertionError("gh_json must not be called")
        orig_gh = sources.gh_json
        sources.gh_json = fail_gh
        try:
            with store.lock():
                c = comms.comms()
                c["since"]["web"] = "2026-11-02T00:00:00Z"
                comms.save_comms(c)

            path = store.path("comms.json")
            mtime_before = os.path.getmtime(path)
            content_before = open(path, "rb").read()

            w = comms.window("web")
            self.assertFalse(w["not_started"])

            self.assertEqual(os.path.getmtime(path), mtime_before)
            self.assertEqual(open(path, "rb").read(), content_before)
        finally:
            sources.gh_json = orig_gh

    def test_serve_comms_data_includes_window(self):
        from factory import serve
        with store.lock():
            c = comms.comms()
            c["since"]["web"] = "2026-11-02T00:00:00Z"
            comms.save_comms(c)
        data = serve.comms_data()
        self.assertIn("products", data)
        self.assertIn("window", data["products"]["web"])
        self.assertFalse(data["products"]["web"]["window"]["not_started"])
        self.assertTrue(data["products"]["store"]["window"]["not_started"])

class CommsRelayTest(unittest.TestCase):
    setUp = CommsTest.setUp
    tearDown = CommsTest.tearDown
    _gh = CommsRegressionTest._gh
    _ship = CommsRegressionTest._ship
    _draft = CommsRegressionTest._draft

    def test_due_feature_job_relays_one_message(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        now = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")
        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res = comms.relay(now=now)
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            thread = args[0] if args else kwargs.get("thread")
            msg = args[1] if len(args) > 1 else kwargs.get("text")
            kind = kwargs.get("kind") or (args[2] if len(args) > 2 else None)
            self.assertEqual(thread, "t-c2e")
            self.assertEqual(kind, "release")
            self.assertTrue(msg.startswith("Release manager (factory release comms app)"))
            self.assertIn("Due: 1 job(s)", msg)
            self.assertIn("- f-app-5: feature email, PRs #5", msg)
            self.assertIn('Follow the release-manager skill, section "Release comms" (steps 2 to 9).', msg)
            self.assertEqual(res["app"]["relayed"], ["f-app-5"])
            self.assertIsNone(res["app"]["skipped"])
            self.assertIsNone(res["app"]["error"])

        c = comms.comms()
        self.assertEqual(c["relayed"]["app"], {"date": "2026-11-10", "jobs": ["f-app-5"]})

    def test_job_waiting_on_owner_is_not_relayed_again_next_day(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        self._ship([5], "2026-11-10T09:00:00Z")
        with mock.patch("factory.relay.enqueue_message") as enq:
            comms.relay(now=datetime(2026, 11, 10, 11, tzinfo=TUNIS))
            comms.save_draft("f-app-5", self._draft([5]), now=datetime(2026, 11, 10, 11, 30, tzinfo=TUNIS))
            res = comms.relay(now=datetime(2026, 11, 11, 11, tzinfo=TUNIS))
            self.assertEqual(enq.call_count, 1)
        self.assertEqual(res["app"]["skipped"], "no jobs due")

    def test_failed_enqueue_keeps_another_runs_claim(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        self._ship([5], "2026-11-10T09:00:00Z")
        with store.lock():
            c = comms.comms()
            c["relayed"]["app"] = {"date": "2026-11-10", "jobs": ["f-app-99"]}
            comms.save_comms(c)
        with mock.patch("factory.relay.enqueue_message", side_effect=RuntimeError("outbox down")):
            res = comms.relay(now=datetime(2026, 11, 10, 11, tzinfo=TUNIS))
        self.assertIn("outbox down", res["app"]["error"])
        self.assertEqual(comms.comms()["relayed"]["app"]["jobs"], ["f-app-99"])

    def test_second_run_same_day_relays_nothing(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        now1 = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")
        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res1 = comms.relay(now=now1)
            self.assertEqual(mock_enqueue.call_count, 1)
            self.assertEqual(res1["app"]["relayed"], ["f-app-5"])

            now2 = datetime(2026, 11, 10, 14, tzinfo=TUNIS)
            res2 = comms.relay(now=now2)
            self.assertEqual(mock_enqueue.call_count, 1)
            self.assertEqual(res2["app"]["relayed"], [])
            self.assertIn("already relayed", res2["app"]["skipped"])

    def test_new_job_later_same_day_relays_only_new_id(self):
        self._gh({5: {"number": 5, "title": "feat: big 1", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]},
                  7: {"number": 7, "title": "feat: big 2", "body": "Roadmap: FEAT-7", "labels": [{"name": "big"}]}})
        now1 = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")
        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res1 = comms.relay(now=now1)
            self.assertEqual(res1["app"]["relayed"], ["f-app-5"])

            self._ship([7], "2026-11-10T13:00:00Z")
            now2 = datetime(2026, 11, 10, 15, tzinfo=TUNIS)
            res2 = comms.relay(now=now2)
            self.assertEqual(mock_enqueue.call_count, 2)
            args, kwargs = mock_enqueue.call_args
            msg = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertIn("Due: 1 job(s)", msg)
            self.assertIn("- f-app-7: feature email, PRs #7", msg)
            self.assertNotIn("f-app-5", msg)
            self.assertEqual(res2["app"]["relayed"], ["f-app-7"])

        c = comms.comms()
        self.assertEqual(c["relayed"]["app"]["jobs"], ["f-app-5", "f-app-7"])

    def test_shadow_paused_no_thread_and_not_configured_products_relay_nothing(self):
        self._gh({5: {"number": 5, "title": "feat: big", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        now = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")

        with store.lock():
            r = release.release()
            r["products"]["app"]["mode"] = "shadow"
            r["paused"]["web"] = True
            r["products"]["store"]["thread"] = None
            release.save_release(r)

        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res = comms.relay(now=now)
            mock_enqueue.assert_not_called()
            self.assertEqual(res["app"]["relayed"], [])
            self.assertEqual(res["app"]["skipped"], "shadow mode")
            self.assertEqual(res["web"]["relayed"], [])
            self.assertEqual(res["web"]["skipped"], "paused")
            self.assertEqual(res["store"]["relayed"], [])
            self.assertEqual(res["store"]["skipped"], "no thread")

        # Also test removing store completely (not configured)
        with store.lock():
            r = release.release()
            del r["products"]["store"]
            release.save_release(r)

        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res = comms.relay(now=now)
            mock_enqueue.assert_not_called()
            self.assertNotIn("store", res)

    def test_no_due_jobs_means_no_message(self):
        now = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res = comms.relay(now=now)
            mock_enqueue.assert_not_called()
            for prod in ("app", "web", "store"):
                self.assertEqual(res[prod]["relayed"], [])
                self.assertEqual(res[prod]["skipped"], "no jobs due")

    def test_enqueue_failure_releases_claim_so_next_run_retries(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        now = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")

        with mock.patch("factory.relay.enqueue_message", side_effect=RuntimeError("outbox corrupt")):
            res1 = comms.relay(now=now)
            self.assertEqual(res1["app"]["relayed"], [])
            self.assertIn("outbox corrupt", res1["app"]["error"])

        c = comms.comms()
        self.assertEqual((c.get("relayed", {}).get("app") or {}).get("jobs", []), [])

        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res2 = comms.relay(now=now)
            mock_enqueue.assert_called_once()
            self.assertEqual(res2["app"]["relayed"], ["f-app-5"])
            self.assertIsNone(res2["app"]["error"])

        c = comms.comms()
        self.assertEqual(c["relayed"]["app"]["jobs"], ["f-app-5"])

    def test_friday_10_05_tunis_run_with_due_digest_relays_it(self):
        self._gh({4: {"number": 4, "title": "fix: small tweak", "body": "", "labels": []}})
        self._ship([4], "2026-11-10T09:00:00Z")
        fri_10_05 = datetime(2026, 11, 13, 10, 5, tzinfo=TUNIS)
        with mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            res = comms.relay(now=fri_10_05)
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            msg = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertIn("Due: 1 job(s)", msg)
            self.assertIn("- d-app-2026-11-13: digest email, PRs #4", msg)
            self.assertEqual(res["app"]["relayed"], ["d-app-2026-11-13"])

    def test_cli_comms_relay(self):
        self._gh({5: {"number": 5, "title": "feat: big sale", "body": "Roadmap: FEAT-5", "labels": [{"name": "big"}]}})
        now = datetime(2026, 11, 10, 11, tzinfo=TUNIS)
        self._ship([5], "2026-11-10T09:00:00Z")

        orig_relay = comms.relay
        try:
            comms.relay = lambda: orig_relay(now=now)
            with mock.patch("factory.relay.enqueue_message"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = cli.main(["comms", "relay"])
                self.assertEqual(ret, 0)
                out = mock_out.getvalue()
                self.assertIn("app: relayed 1 job(s) (f-app-5)", out)
                self.assertIn("web: skipped (no jobs due)", out)
                self.assertIn("store: skipped (no jobs due)", out)

            # Second run prints already relayed today
            with mock.patch("factory.relay.enqueue_message"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = cli.main(["comms", "relay"])
                self.assertEqual(ret, 0)
                self.assertIn("app: skipped (already relayed today)", mock_out.getvalue())

            # Clear relayed to test --json with relayed job
            with store.lock():
                c = comms.comms()
                c["relayed"] = {}
                comms.save_comms(c)

            with mock.patch("factory.relay.enqueue_message"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = cli.main(["comms", "relay", "--json"])
                self.assertEqual(ret, 0)
                parsed = json.loads(mock_out.getvalue())
                self.assertIn("app", parsed)
                self.assertEqual(parsed["app"]["relayed"], ["f-app-5"])

            with store.lock():
                c = comms.comms()
                c["relayed"] = {}
                comms.save_comms(c)

            with mock.patch("factory.relay.enqueue_message", side_effect=RuntimeError("relay failure")), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = cli.main(["comms", "relay"])
                self.assertEqual(ret, 1)
        finally:
            comms.relay = orig_relay


class CommsProductConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

    def add(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["release", "product", *args]), 0)

    def test_flags_set_metadata_and_order_follows_config(self):
        self.add("add", "zeta", "--repo", "o/z", "--flow", "pr", "--project", "z")
        self.add("add", "alpha", "--repo", "o/a", "--flow", "pr", "--project", "a")
        self.add("set", "alpha", "--display-name", "Alpha One", "--color", "#112233", "--short", "al",
                 "--flow-label", "develop → main · 07:30", "--comms-langs", "fr", "en",
                 "--comms-audiences", "users", "--comms-internal-scopes", "ops2")
        p = release.release()["products"]["alpha"]
        self.assertEqual((p["display_name"], p["color"], p["short"]), ("Alpha One", "#112233", "al"))
        self.assertEqual(p["flow_label"], "develop → main · 07:30")
        self.assertEqual(p["comms"], {"langs": ["fr", "en"], "audiences": ["users"], "internal_scopes": ["ops2"]})
        self.assertEqual(comms.comms_products(), {"alpha": {"langs": ["fr", "en"], "audiences": ["users"],
                                                            "internal_scopes": {"ops2"}}})
        self.assertEqual(list(release.release()["products"]), ["zeta", "alpha"])
        self.assertEqual(comms.product_name("alpha"), "Alpha One")

    def test_no_comms_block_is_not_a_comms_product_and_name_falls_back(self):
        self.add("add", "plain", "--repo", "o/p", "--flow", "pr", "--project", "p")
        self.assertEqual(comms.comms_products(), {})
        self.assertIsNone(comms.comms_cfg("plain"))
        self.assertEqual(comms.product_name("plain"), "Plain")
        self.assertEqual(comms.due("plain"), [])

    def test_bad_color_refused(self):
        self.add("add", "x", "--repo", "o/x", "--flow", "pr", "--project", "x")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(cli.main(["release", "product", "set", "x", "--color", "red"]), 0)
        self.assertNotIn("color", release.release()["products"]["x"])

    def test_comms_block_without_langs_is_not_a_comms_product(self):
        self.add("add", "x", "--repo", "o/x", "--flow", "pr", "--project", "x")
        self.add("set", "x", "--comms-audiences", "users")
        self.assertEqual(comms.comms_products(), {})

    def test_empty_audience_refused(self):
        self.add("add", "x", "--repo", "o/x", "--flow", "pr", "--project", "x")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(cli.main(["release", "product", "set", "x", "--comms-langs", "en",
                                          "--comms-audiences", ""]), 0)
        self.assertNotIn("comms", release.release()["products"]["x"])

    def test_job_id_checks_every_product_it_could_belong_to(self):
        for name in ("web", "web-admin"):
            self.add("add", name, "--repo", f"o/{name}", "--flow", "pr", "--project", name)
            self.add("set", name, "--comms-langs", "en", "--comms-audiences", "users")
        seen = []
        with mock.patch.object(comms, "due", side_effect=lambda p, now=None: seen.append(p) or []):
            with self.assertRaises(Exception):
                comms._load_job("f-web-admin-3", None)
        self.assertEqual(sorted(seen), ["web", "web-admin"])
