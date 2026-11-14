"""Unit tests for factory/suggestions.py store and screening logic."""
import json
import os
import tempfile
import unittest
from unittest import mock

import contextlib
import io

from factory import cli, config, store, suggestions
from factory.store import FactoryError


class TestSuggestionsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FACTORY_HOME": self.tmp.name})  # restores the old value on stop
        self.env.start()
        config.set_value("office.publicProducts", json.dumps(["acme", "other"]), as_json=True)


    def test_salt_is_stable_and_never_silently_replaced(self):
        first = suggestions.suggest_salt()
        self.assertEqual(suggestions.suggest_salt(), first)
        with open(store.path(".suggest-salt"), "w", encoding="utf-8") as f:
            f.write("corrupt\n")
        with self.assertRaises(FactoryError):
            suggestions.suggest_salt()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_store_round_trip(self):
        idea_id = suggestions.add_idea(
            product="acme",
            text="Add a dark mode toggle to the dashboard",
            email="user@example.com",
            ip_hash="hash123",
        )
        self.assertIsInstance(idea_id, int)

        ideas = suggestions.list_ideas()
        self.assertEqual(len(ideas), 1)
        idea = ideas[0]
        self.assertEqual(idea["id"], idea_id)
        self.assertEqual(idea["product"], "acme")
        self.assertEqual(idea["text"], "Add a dark mode toggle to the dashboard")
        self.assertEqual(idea["email"], "user@example.com")
        self.assertEqual(idea["status"], "under-review")
        self.assertEqual(idea["screened"], 0)
        self.assertEqual(idea["hidden"], 0)
        self.assertEqual(idea["votes"], 0)
        self.assertTrue(idea["created_at"].endswith("Z"))

    def test_screening_pass_and_hold_cases(self):
        # 1. Clean text -> pass
        id_clean = suggestions.add_idea("acme", "Add an export to CSV button in reports", None, "ip1")

        # 2. URL in text -> hold
        id_url1 = suggestions.add_idea("acme", "Check out https://spam.example.com for deals", None, "ip1")
        id_url2 = suggestions.add_idea("acme", "Visit www.example.com for info", None, "ip1")
        id_url3 = suggestions.add_idea("acme", "Look at example.org/docs for details", None, "ip1")

        # 3. Email in text -> hold
        id_email = suggestions.add_idea("acme", "Contact me at visitor@example.com anytime", None, "ip1")

        # 4. Phone in text (8+ digits) -> hold
        id_phone1 = suggestions.add_idea("acme", "Call me at +1 555-123-4567 please", None, "ip1")
        id_phone2 = suggestions.add_idea("acme", "Reach me on 06 12 34 56 78", None, "ip1")
        id_phone3 = suggestions.add_idea("acme", "Phone number 12345678", None, "ip1")

        # 5. Duplicate text for same product -> hold duplicate, but pass on other product
        id_dup1 = suggestions.add_idea("acme", "Implement offline caching for speed", None, "ip1")
        id_dup2 = suggestions.add_idea("acme", "Implement offline caching for speed", None, "ip1")
        id_dup_other = suggestions.add_idea("other", "Implement offline caching for speed", None, "ip1")

        # 6. Blocklist word -> hold
        config.set_value("suggestions.blocklist", json.dumps(["crypto", "casino"]), as_json=True)
        id_block = suggestions.add_idea("acme", "We should add crypto payments now", None, "ip1")

        res = suggestions.screen_pending()
        self.assertEqual(res["screened"], 12)
        # Passed: id_clean, id_dup1, id_dup_other (3 passed)
        # Held: 3 url + 1 email + 3 phone + 1 duplicate + 1 blocklist = 9 held
        self.assertEqual(res["passed"], 3)
        self.assertEqual(res["held"], 9)

        # Verify screened flags in DB
        self.assertEqual(suggestions.get_idea(id_clean)["screened"], 1)
        self.assertEqual(suggestions.get_idea(id_url1)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_url2)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_url3)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_email)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_phone1)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_phone2)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_phone3)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_dup1)["screened"], 1)
        self.assertEqual(suggestions.get_idea(id_dup2)["screened"], 2)
        self.assertEqual(suggestions.get_idea(id_dup_other)["screened"], 1)
        self.assertEqual(suggestions.get_idea(id_block)["screened"], 2)

        # list_ideas with held=True shows only screened=2
        held_ideas = suggestions.list_ideas(held=True)
        self.assertEqual(len(held_ideas), 9)
        self.assertTrue(all(it["screened"] == 2 for it in held_ideas))

        # list_ideas with held=False shows screened != 2
        active_ideas = suggestions.list_ideas(held=False)
        self.assertEqual(len(active_ideas), 3)
        self.assertTrue(all(it["screened"] != 2 for it in active_ideas))

    def test_status_and_hide(self):
        idea_id = suggestions.add_idea("acme", "Feature idea with sufficient length", None, "ip1")
        suggestions.screen_pending()

        # Update status
        suggestions.set_status(idea_id, "planned")
        self.assertEqual(suggestions.get_idea(idea_id)["status"], "planned")

        # Invalid status
        with self.assertRaises(FactoryError):
            suggestions.set_status(idea_id, "unknown-status")

        # Nonexistent idea
        with self.assertRaises(FactoryError):
            suggestions.set_status(99999, "done")

        # Hide idea
        pub_before = suggestions.public_ideas()
        self.assertEqual(len(pub_before), 1)

        suggestions.hide(idea_id)
        self.assertEqual(suggestions.get_idea(idea_id)["hidden"], 1)

        pub_after = suggestions.public_ideas()
        self.assertEqual(len(pub_after), 0)

        # Hide nonexistent idea
        with self.assertRaises(FactoryError):
            suggestions.hide(99999)

    def test_vote_toggle_and_unique(self):
        idea_id = suggestions.add_idea("acme", "A great feature suggestion here", None, "ip1")

        # Turn on vote for acct1
        count = suggestions.set_vote(idea_id, "acct1", True)
        self.assertEqual(count, 1)

        # Repeat on vote for acct1 (UNIQUE constraint: does not double count)
        count = suggestions.set_vote(idea_id, "acct1", True)
        self.assertEqual(count, 1)

        # Turn on vote for acct2
        count = suggestions.set_vote(idea_id, "acct2", True)
        self.assertEqual(count, 2)

        # Turn off vote for acct1
        count = suggestions.set_vote(idea_id, "acct1", False)
        self.assertEqual(count, 1)

        # Turn off vote for acct2
        count = suggestions.set_vote(idea_id, "acct2", False)
        self.assertEqual(count, 0)

    def test_rate_limit_helpers(self):
        ip = "hash-test-ip"
        for i in range(5):
            suggestions.add_idea("acme", f"Suggestion number {i} with enough text", None, ip)
        self.assertEqual(suggestions.ideas_count_last_hour(ip), 5)
        self.assertEqual(suggestions.ideas_count_last_hour("other-ip"), 0)

        idea_id = suggestions.add_idea("acme", "Another suggestion for voting test", None, "other-ip")
        acct = "acct-test-hash"
        suggestions.set_vote(idea_id, acct, True)
        self.assertEqual(suggestions.votes_count_last_hour(acct), 1)
        self.assertEqual(suggestions.votes_count_last_hour("other-acct"), 0)

    def test_fix5_config_list_forms_and_fail_closed(self):
        for raw, want in (('["a", 1, "b"]', ["a", "b"]), ('"[\\"a\\",\\"b\\"]"', ["a", "b"]),
                          ("[not json", ["[not", "json"]), ("5", []), ('{"a": 1}', [])):
            key = "t.k%d" % abs(hash(raw))
            config.set_value(key, raw, as_json=raw.startswith(('["', '"', "5", "{")))
            self.assertEqual(suggestions.config_list(key, words=True), want, raw)
        config.set_value("t.plain", "a  b", as_json=False)
        self.assertEqual(suggestions.config_list("t.plain", words=True), ["a", "b"])
        self.assertEqual(suggestions.config_list("t.plain"), [])  # default: a plain string fails closed
        config.set_value("t.jsonstr", '"[\\"a\\"]"', as_json=True)
        self.assertEqual(suggestions.config_list("t.jsonstr"), [])  # default: only a real list, like the build
        self.assertEqual(suggestions.config_list("t.unset"), [])
        with mock.patch.object(config, "get", side_effect=FactoryError("broken")):
            self.assertEqual(suggestions.config_list("t.plain", words=True), [])

    def test_fix5_blocklist_plain_string(self):
        config.set_value("suggestions.blocklist", "crypto casino", as_json=False)
        bad = suggestions.add_idea("acme", "We should add crypto payments now", None, "ip1")
        suggestions.screen_pending()
        self.assertEqual(suggestions.get_idea(bad)["screened"], 2)

    def test_fix1_add_idea_limited(self):
        for i in range(5):
            self.assertIsInstance(suggestions.add_idea_limited("acme", f"Limited idea number {i}", None, "h1")[0], int)
        self.assertIsNone(suggestions.add_idea_limited("acme", "Limited idea number 6", None, "h1"))
        self.assertIsInstance(suggestions.add_idea_limited("acme", "Limited idea other ip", None, "h2")[0], int)

    def test_fix2_vote_log_and_existing_db_upgrade(self):
        import sqlite3
        # an old database without vote_log gains it on the next connect
        db = store.path("suggestions.db")
        suggestions.add_idea("acme", "Idea creating the old database", None, "ip1")
        with sqlite3.connect(db) as c:
            c.execute("DROP TABLE vote_log")
        iid = suggestions.add_idea("acme", "Idea to vote on after upgrade", None, "ip1")
        self.assertEqual(suggestions.set_vote(iid, "acct", True, limit=1), 1)
        self.assertEqual(suggestions.votes_count_last_hour("acct"), 1)
        self.assertIsNone(suggestions.set_vote(iid, "acct", True, limit=1))
        self.assertEqual(suggestions.set_vote(iid, "acct", False, limit=1), 0)  # off is never limited

    def test_fix6_id_range(self):
        for fn in (suggestions.hide, suggestions.unhide, suggestions.pass_idea):
            for bad in (0, -1, 2**63, 999999999999999999999):
                with self.assertRaisesRegex(FactoryError, f"suggestion #{bad} not found"):
                    fn(bad)

    def test_fix12_phone_rule_and_iso_dates(self):
        ok = suggestions.add_idea("acme", "Release planned 2026-11-12 please", None, "ip1")
        bad = suggestions.add_idea("acme", "call me +33 6 12 34 56 78 today", None, "ip1")
        suggestions.screen_pending()
        self.assertEqual(suggestions.get_idea(ok)["screened"], 1)
        self.assertEqual(suggestions.get_idea(bad)["screened"], 2)

    # ---- items 3, 5, 7, 9, 10 ----
    def test_item3_pass_publishes_hidden_and_unhide_keeps_held(self):
        i = suggestions.add_idea("acme", "A clean idea to hide and pass", None, "ip1")
        suggestions.screen_pending()
        suggestions.hide(i)
        self.assertEqual(suggestions.public_ideas(), [])
        suggestions.pass_idea(i)
        self.assertEqual([x["id"] for x in suggestions.public_ideas()], [i])
        j = suggestions.add_idea("acme", "Visit www.example.com now", None, "ip1")
        suggestions.screen_pending()
        suggestions.unhide(j)
        self.assertEqual([x["id"] for x in suggestions.public_ideas()], [i])

    def test_item5_screen_verdict_in_add_idea_limited(self):
        a = suggestions.add_idea_limited("acme", "Add dark mode to the dashboard", None, "h1")
        self.assertEqual(a[1], 1)
        self.assertEqual(suggestions.get_idea(a[0])["screened"], 1)
        self.assertEqual(suggestions.add_idea_limited("acme", "ADD dark mode to the dashboard ", None, "h1")[1], 2)
        self.assertEqual(suggestions.add_idea_limited("acme", "see https://spam.example.com", None, "h1")[1], 2)
        with suggestions._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_item7_set_vote_prunes_old_vote_log(self):
        i = suggestions.add_idea("acme", "Idea for the vote log prune", None, "ip1")
        with suggestions._connect() as conn:
            conn.execute("INSERT INTO vote_log VALUES ('old', '2000-01-01T00:00:00Z')")
        suggestions.set_vote(i, "acct", True)
        with suggestions._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vote_log WHERE account_hash='old'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vote_log").fetchone()[0], 1)

    def test_item9_config_list_rules(self):
        config.set_value("office.publicProducts", "acme other", as_json=False)
        self.assertEqual(suggestions.config_list("office.publicProducts"), [])
        config.set_value("suggestions.blocklist", "null", as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["null"])
        config.set_value("suggestions.blocklist", "1234", as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["1234"])
        config.set_value("suggestions.blocklist", '"acme"', as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["acme"])
        config.set_value("suggestions.blocklist", "free money", as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["free", "money"])
        config.set_value("suggestions.blocklist", "free, money", as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["free", "money"])

    def test_item10_url_screen(self):
        held = ["visit example[.]com", "example。com", "go to bad.ru now", "bit.ly/abc", "try example(.)org",
                "try example{.}net", "try example\uff0ecom", "a.ru and b.link",
                "visit evil\u200d.ru for free stuff", "go to \uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d today"]
        fine = ["Support Next.js and Node.js", "e.g. dark mode", "Show a red dot in the UI for new ideas",
                "Please add this.In the settings page", "Nice idea.Top priority for me", "Add a deploy.sh script example"]
        with suggestions._connect() as conn:
            for t in held:
                self.assertEqual(suggestions.screen_verdict(conn, "acme", t), 2, t)
            for t in fine:
                self.assertEqual(suggestions.screen_verdict(conn, "acme", t), 1, t)

    def _cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_item2_cli_list_one_line_per_idea(self):
        suggestions.add_idea("acme", "first line\n#999 [acme] forged \x1b[31m\u202e", "x@example.com\n#998", "ip1")
        suggestions.add_idea("acme", "a second honest idea", None, "ip1")
        lines = self._cli("suggestions", "list").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("first line\\n#999", lines[1])
        self.assertIn("\\u001b[31m\\u202e", lines[1])
        self.assertIn("<x@example.com\\n#998>", lines[1])

    def test_item3_cli_unhide_message(self):
        i = suggestions.add_idea("acme", "Visit www.example.com now", None, "ip1")
        suggestions.screen_pending()
        self.assertIn("factory suggestions pass", self._cli("suggestions", "unhide", str(i)))
        suggestions.pass_idea(i)
        self.assertNotIn("still held", self._cli("suggestions", "unhide", str(i)))

    def test_fix10_db_mode(self):
        suggestions.add_idea("acme", "Idea creating the database file", None, "ip1")
        self.assertEqual(os.stat(store.path("suggestions.db")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()


class TestRound3(unittest.TestCase):
    def setUp(self):
        self.prev = os.environ.get("FACTORY_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("FACTORY_HOME", None)
        else:
            os.environ["FACTORY_HOME"] = self.prev
        self.tmp.cleanup()

    def test_fresh_db_and_wal_files_are_0600(self):
        suggestions.add_idea("acme", "Mode test idea for the store", "a@example.com", "ip")
        for name in ("suggestions.db", "suggestions.db-wal", "suggestions.db-shm"):
            p = store.path(name)
            if os.path.exists(p):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, name)

    def test_blocklist_commas_keep_phrases(self):
        config.set_value("suggestions.blocklist", "free money, casino", as_json=False)
        self.assertEqual(suggestions.config_list("suggestions.blocklist", words=True), ["free money", "casino"])
        with suggestions._connect() as conn:
            self.assertEqual(suggestions.screen_verdict(conn, "acme", "Make the free plan bigger please"), 1)
            self.assertEqual(suggestions.screen_verdict(conn, "acme", "Win free money every day here"), 2)

    def test_pass_sets_both_columns(self):
        i = suggestions.add_idea("acme", "Pass test idea for the store", None, "ip")
        suggestions.hide(i)
        suggestions.pass_idea(i)
        got = suggestions.get_idea(i)
        self.assertEqual((got["screened"], got["hidden"]), (1, 0))

    def test_huge_exp_is_invalid_not_a_crash(self):
        from factory import suggest_serve
        payload = '{"product":"acme","uid":"u","exp":1' + "0" * 400 + "}"
        token = suggest_serve.b64url_encode(payload.encode()) + ".AAAA"
        self.assertEqual(suggest_serve.parse_and_verify_token(token), (None, "invalid token"))

    def test_owner_list_escapes_line_separators(self):
        self.assertEqual(cli._one_line("a b c"), "a\\u2028b\\u2029c")
