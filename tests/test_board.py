import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import board, build, serve, sources, store

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def _card(number=142, repo="o/r", col="intake", pr=None, labels=()):
    return {"id": f"{repo}#{number}", "num": number, "col": col, "repo": repo, "repoShort": repo.split("/")[-1],
            "product": "bk", "source": "manual", "title": "T", "auto": "auto-promoted" in labels,
            "stuck": "stuck" in labels, "pr": pr, "url": f"https://github.com/{repo}/issues/{number}", "body": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        store.write("products.json", {"bk": {"name": "Acme", "repos": ["o/r", "o/r2"]},
                                      "c2": {"name": "widget", "repos": ["o/r3"]}})

    def seed_card(self, **kw):
        store.write("board-cache.json", {"cards": [_card(**kw)], "fetchedAt": "2026-10-27T10:00:00Z", "error": None})


class ColumnRuleTest(unittest.TestCase):
    def test_first_match_wins(self):
        open_by_closed = {
            ("o/r", 1): [{"isDraft": False, "repo": "o/r", "number": 10, "url": "u"}],
            ("o/r", 2): [{"isDraft": True, "repo": "o/r", "number": 11, "url": "u"}],
        }
        # 1. needs-decision beats everything
        self.assertEqual(board.column_for("o/r", 1, {"needs-decision", "intake", "factory"}, open_by_closed), "waiting")
        self.assertEqual(board.column_for("o/r", 1, {"needs-decision", "intake"}, open_by_closed), "waiting")
        # 2. a linked non-draft PR -> review
        self.assertEqual(board.column_for("o/r", 1, {"intake", "factory"}, open_by_closed), "review")
        # 3. a linked draft PR -> building (even with factory + intake)
        self.assertEqual(board.column_for("o/r", 2, {"intake", "factory"}, open_by_closed), "building")
        # 3. label building -> building
        self.assertEqual(board.column_for("o/r", 3, {"building", "factory"}, open_by_closed), "building")
        # 4. factory -> triage
        self.assertEqual(board.column_for("o/r", 3, {"factory", "intake"}, open_by_closed), "triage")
        # 5. intake -> intake
        self.assertEqual(board.column_for("o/r", 3, {"intake"}, open_by_closed), "intake")
        # 6. anything else is not on the board
        self.assertIsNone(board.column_for("o/r", 3, set(), open_by_closed))
        self.assertIsNone(board.column_for("o/r", 3, {"bug"}, open_by_closed))
        # a non-draft PR wins over a draft PR for the same issue
        open2 = {("o/r", 5): [{"isDraft": False, "repo": "o/r", "number": 10, "url": "u"},
                              {"isDraft": True, "repo": "o/r", "number": 12, "url": "u"}]}
        self.assertEqual(board.column_for("o/r", 5, {"intake"}, open2), "review")


class MergedTest(unittest.TestCase):
    def test_merged_today_vs_older_closed(self):
        recent = NOW - timedelta(hours=1)
        older = NOW - timedelta(hours=25)
        merged = {("o/r", 100): [{"number": 5, "repo": "o/r", "url": "u", "mergedAt": recent.isoformat()}],
                  ("o/r", 101): [{"number": 6, "repo": "o/r", "url": "u", "mergedAt": older.isoformat()}]}
        self.assertIsNotNone(board._merged_pr("o/r", 100, merged, NOW))
        self.assertIsNone(board._merged_pr("o/r", 101, merged, NOW))
        self.assertIsNone(board._merged_pr("o/r", 999, merged, NOW))


class ClassifyTest(unittest.TestCase):
    def test_product_filter_of_repos(self):
        products = {"bk": {"name": "Acme", "repos": ["o/r1", "o/r2"]},
                    "c2": {"name": "widget", "repos": ["o/r3"]}}
        issues = [
            {"number": 1, "title": "a", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/o/r1", "labels": [{"name": "intake"}]},
            {"number": 2, "title": "b", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/o/r3", "labels": [{"name": "intake"}]},
            {"number": 3, "title": "c", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/other/x", "labels": [{"name": "intake"}]},
            {"number": 4, "title": "d", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/o/r1", "labels": [{"name": "bug"}]},
        ]
        cards = board.classify(products, issues, {}, {}, NOW)
        self.assertEqual(sorted(c["product"] for c in cards), ["bk", "c2"])
        self.assertEqual([c["repo"] for c in cards if c["product"] == "bk"], ["o/r1"])  # o/r2, other/x, no-label dropped
        # the merged column only for a closed issue closed by a merged PR in the last 24 h
        issues.append({"number": 5, "title": "e", "state": "closed", "updated_at": "2026-10-27T11:00:00Z",
                       "closed_at": "2026-10-27T11:00:00Z", "repository_url": "https://api.github.com/repos/o/r1",
                       "labels": [{"name": "intake"}]})
        merged = {("o/r1", 5): [{"number": 7, "repo": "o/r1", "url": "u", "mergedAt": (NOW - timedelta(hours=1)).isoformat()}]}
        cards = board.classify(products, issues, {}, merged, NOW)
        self.assertIn("merged", [c["col"] for c in cards if c["num"] == 5])

    def test_cross_repo_pr_links_only_its_issue(self):
        # a PR in o/a that closes o/b#5 links to o/b#5 only, never to o/a#5
        products = {"bk": {"name": "Acme", "repos": ["o/a", "o/b"]}}
        issues = [
            {"number": 5, "title": "x", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/o/b", "labels": [{"name": "intake"}]},
            {"number": 5, "title": "y", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
             "repository_url": "https://api.github.com/repos/o/a", "labels": [{"name": "intake"}]},
        ]
        open_by_closed = {("o/b", 5): [{"number": 10, "repo": "o/a", "url": "u", "isDraft": False}]}
        cards = board.classify(products, issues, open_by_closed, {}, NOW)
        by_repo = {c["repo"]: c["col"] for c in cards}
        self.assertEqual(by_repo["o/b"], "review")   # o/a#10 closes o/b#5
        self.assertEqual(by_repo["o/a"], "intake")   # o/a#5 is not linked


class SearchTest(unittest.TestCase):
    def test_search_adds_is_issue(self):
        products = {"bk": {"name": "Acme", "repos": ["o/r"]}}
        queries = []
        with mock.patch.object(board, "_search", side_effect=lambda q: (queries.append(q), [])[1]):
            board.search_issues(products)
        self.assertTrue(queries)
        for q in queries:
            self.assertIn("is:issue", q)
            self.assertIn("repo:o/r", q)

    def test_one_search_per_repo(self):
        products = {"bk": {"name": "Acme", "repos": ["o/a", "o/b"]}}
        queries = []
        with mock.patch.object(board, "_search", side_effect=lambda q: (queries.append(q), [])[1]):
            board.search_issues(products)
        self.assertEqual(len(queries), 4)  # open + closed, per repo
        self.assertTrue(any("repo:o/a" in q for q in queries))
        self.assertTrue(any("repo:o/b" in q for q in queries))
        for q in queries:
            self.assertFalse("repo:o/a" in q and "repo:o/b" in q)


class CollectTest(Base):
    def test_collect_caches_good_result(self):
        card = _card()
        with mock.patch.object(board, "search_issues", return_value=([], {}, {"o/r"})), \
             mock.patch.object(board, "_collect_pr_index", return_value=({}, {})), \
             mock.patch.object(board, "classify", return_value=[card]):
            data = board.collect(store.products(), NOW)
        self.assertIsNone(data["error"])
        self.assertEqual(data["cards"], [card])
        self.assertEqual(store.read("board-cache.json", {})["cards"], [card])

    def test_gh_failure_returns_cache_with_error(self):
        store.write("board-cache.json", {"cards": [_card()], "fetchedAt": "2026-10-27T10:00:00Z", "error": None})
        with mock.patch.object(board, "search_issues", side_effect=store.FactoryError("gh api failed")):
            data = board.collect(store.products(), NOW)
        self.assertEqual(data["error"], "gh api failed")
        self.assertEqual(data["fetchedAt"], "2026-10-27T10:00:00Z")
        self.assertEqual(data["cards"][0]["id"], "o/r#142")

    def test_every_repo_failed_falls_back_to_cache(self):
        store.write("board-cache.json", {"cards": [_card()], "fetchedAt": "2026-10-27T10:00:00Z", "error": None})
        errs = {"o/r": "boom", "o/r2": "boom", "o/r3": "boom"}
        with mock.patch.object(board, "search_issues", return_value=([], errs, set())):
            data = board.collect(store.products(), NOW)
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["fetchedAt"], "2026-10-27T10:00:00Z")
        self.assertEqual(data["cards"][0]["id"], "o/r#142")

    def test_partial_repo_failure_records_repo_errors(self):
        issue = {"number": 1, "title": "a", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
                 "repository_url": "https://api.github.com/repos/o/r", "labels": [{"name": "intake"}]}
        with mock.patch.object(board, "search_issues", return_value=([issue], {"o/r2": "boom"}, {"o/r"})), \
             mock.patch.object(board, "_collect_pr_index", return_value=({}, {})):
            data = board.collect(store.products(), NOW)
        self.assertIsNone(data["error"])
        self.assertEqual(data["repoErrors"], {"o/r2": "boom"})
        self.assertEqual(len(data["cards"]), 1)

    def test_failed_pr_read_keeps_that_repos_last_good_cards(self):
        """Without its PRs a repo's columns would be wrong (an issue with an open PR in Intake)."""
        store.write("board-cache.json", {"cards": [_card(number=130, repo="o/r", col="review")],
                                         "fetchedAt": "t", "error": None})
        issue = {"number": 130, "title": "a", "state": "open", "updated_at": "2026-10-27T11:00:00Z",
                 "repository_url": "https://api.github.com/repos/o/r", "labels": [{"name": "intake"}]}
        with mock.patch.object(board, "search_issues", return_value=([issue], {}, {"o/r"})), \
             mock.patch.object(board, "prs_for_repo", side_effect=board.FactoryError("API rate limit exceeded")):
            data = board.collect(store.products(), NOW)
        self.assertEqual(data["repoErrors"]["o/r"], "API rate limit exceeded")
        self.assertEqual([(c["repo"], c["num"], c["col"]) for c in data["cards"]], [("o/r", 130, "review")])

    def test_fallback_writes_cache(self):
        store.write("board-cache.json", {"cards": [_card()], "fetchedAt": "t", "error": None})
        res = board._cached_result("boom")
        self.assertEqual(res["error"], "boom")
        self.assertEqual(store.read("board-cache.json", {})["error"], "boom")

    def test_auto_promote_default_true(self):
        self.assertTrue(board.auto_promote())
        board.set_auto_promote(False)
        self.assertFalse(board.auto_promote())
        self.assertEqual(store.read("board.json", {}), {"auto_promote": False})


class ActionTest(Base):
    def test_promote_exact_commands(self):
        self.seed_card(col="intake")
        with mock.patch.object(board, "_issue_state", return_value=("open", {"intake", "source:sentry"})), \
             mock.patch.object(board, "_ensure_label") as el, \
             mock.patch.object(sources, "run") as run:
            res = board.act("promote", "o/r", 142)
        self.assertEqual(res, {"repo": "o/r", "number": 142, "col": "triage"})
        el.assert_called_once_with("o/r", "factory", board.FACTORY_LABEL_DESC, board.FACTORY_LABEL_COLOR)
        run.assert_has_calls([
            mock.call("gh", "issue", "edit", "142", "--repo", "o/r", "--add-label", "factory", "--remove-label", "intake"),
            mock.call("gh", "issue", "comment", "142", "--repo", "o/r", "--body", "Promoted from the factory board."),
        ])
        self.assertEqual(store.read("board-cache.json", {})["cards"][0]["col"], "triage")

    def test_close_exact_commands(self):
        self.seed_card(col="review", pr={"number": 44, "repo": "o/r", "url": "u"})
        with mock.patch.object(sources, "run") as run:
            res = board.act("close", "o/r", 142)
        self.assertEqual(res, {"repo": "o/r", "number": 142, "closed": True})
        run.assert_has_calls([
            mock.call("gh", "issue", "comment", "142", "--repo", "o/r", "--body", "Closed from the factory board."),
            mock.call("gh", "issue", "close", "142", "--repo", "o/r", "--reason", "not planned"),
        ])
        self.assertEqual(store.read("board-cache.json", {})["cards"], [])

    def test_close_failure_reports_comment_posted(self):
        self.seed_card(col="review")
        with mock.patch.object(sources, "run", side_effect=[None, store.FactoryError("gh issue close failed: boom")]):
            with self.assertRaises(board.BoardError) as cm:
                board.act("close", "o/r", 142)
        self.assertIn("comment was posted", str(cm.exception))

    def test_send_back_exact_command(self):
        self.seed_card(col="review", pr={"number": 44, "repo": "o/r", "url": "u"})
        with mock.patch.object(sources, "run") as run:
            res = board.act("send_back", "o/r", 142, "  fix it now  ")
        self.assertEqual(res, {"repo": "o/r", "number": 142, "pr": 44})
        run.assert_called_once_with("gh", "pr", "comment", "44", "--repo", "o/r", "--body", "fix it now")


class ValidationTest(Base):
    def test_unknown_repo_400(self):
        with self.assertRaises(board.BoardError) as cm:
            board.act("promote", "other/x", 1)
        self.assertEqual(cm.exception.status, 400)

    def test_non_string_repo_400(self):
        for bad in (["o/r"], {"a": 1}, 123, None):
            with self.assertRaises(board.BoardError) as cm:
                board.act("promote", bad, 1)
            self.assertEqual(cm.exception.status, 400)

    def test_bad_number_400(self):
        for bad in (0, -1, "12", None):
            with self.assertRaises(board.BoardError) as cm:
                board.act("promote", "o/r", bad)
            self.assertEqual(cm.exception.status, 400)

    def test_promote_refused_unless_open_intake_without_factory(self):
        # already promoted (has factory) -> 409
        with mock.patch.object(board, "_issue_state", return_value=("open", {"intake", "factory"})):
            with self.assertRaises(board.BoardError) as cm:
                board.act("promote", "o/r", 142)
            self.assertEqual(cm.exception.status, 409)
        # no intake -> 409
        with mock.patch.object(board, "_issue_state", return_value=("open", {"factory"})):
            with self.assertRaises(board.BoardError) as cm:
                board.act("promote", "o/r", 142)
            self.assertEqual(cm.exception.status, 409)
        # closed on GitHub -> 409
        with mock.patch.object(board, "_issue_state", return_value=("closed", {"intake"})):
            with self.assertRaises(board.BoardError) as cm:
                board.act("promote", "o/r", 142)
            self.assertEqual(cm.exception.status, 409)

    def test_send_back_without_text_400(self):
        self.seed_card(col="review", pr={"number": 44, "repo": "o/r"})
        for text in ("", "   ", None):
            with self.assertRaises(board.BoardError) as cm:
                board.act("send_back", "o/r", 142, text)
            self.assertEqual(cm.exception.status, 400)

    def test_send_back_only_review_or_building_409(self):
        self.seed_card(col="intake", pr={"number": 44, "repo": "o/r"})
        with self.assertRaises(board.BoardError) as cm:
            board.act("send_back", "o/r", 142, "fix")
        self.assertEqual(cm.exception.status, 409)

    def test_send_back_without_pr_409(self):
        self.seed_card(col="review", pr=None)
        with self.assertRaises(board.BoardError) as cm:
            board.act("send_back", "o/r", 142, "fix it")
        self.assertEqual(cm.exception.status, 409)


class LabelTest(Base):
    def test_missing_labels(self):
        with mock.patch.object(sources, "gh_json", return_value=[{"name": "intake"}, {"name": "factory"}]):
            self.assertEqual(board.missing_labels("o/r"), ["needs-decision", "building"])

    def test_missing_labels_for_products(self):
        with mock.patch.object(board, "missing_labels", side_effect=[["factory"], [], []]):
            self.assertEqual(board.missing_labels_for_products(), ["o/r missing factory"])

    def test_ensure_label_treats_already_exists_as_success(self):
        with mock.patch.object(sources, "run", side_effect=store.FactoryError("gh label create failed: already exists")):
            board._ensure_label("o/r", "factory", "desc", "color")  # no raise
        with mock.patch.object(sources, "run", side_effect=store.FactoryError("gh label create failed: boom")):
            with self.assertRaises(store.FactoryError):
                board._ensure_label("o/r", "factory", "desc", "color")


class ServeBoardTest(Base):
    def test_post_validation_status_codes(self):
        self.assertEqual(serve.save_board({"action": "promote", "repo": "other/x", "number": 1})[0], 400)
        self.assertEqual(serve.save_board({"action": "promote", "repo": "o/r", "number": 0})[0], 400)
        with mock.patch.object(board, "_issue_state", return_value=("open", {"factory"})):
            self.assertEqual(serve.save_board({"action": "promote", "repo": "o/r", "number": 142})[0], 409)
        self.seed_card(col="review", pr={"number": 44, "repo": "o/r"})
        self.assertEqual(serve.save_board({"action": "send_back", "repo": "o/r", "number": 142, "text": ""})[0], 400)
        self.assertEqual(serve.save_board("not a dict")[0], 400)

    def test_auto_promote_action(self):
        code, body = serve.save_board({"action": "auto_promote", "enabled": False})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"auto_promote": False})
        self.assertEqual(store.read("board.json", {}), {"auto_promote": False})
        self.assertEqual(serve.save_board({"action": "auto_promote", "enabled": "yes"})[0], 400)

    def test_refresh_action(self):
        with mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": "t", "error": None, "repoErrors": {}}):
            code, body = serve.save_board({"action": "refresh"})
        self.assertEqual(code, 200)
        self.assertEqual(body["cards"], [])
        self.assertTrue(body["auto_promote"])

    def test_get_board_returns_cache(self):
        store.write("board-cache.json", {"cards": [{"id": "o/r#1"}], "fetchedAt": "t", "error": None, "repoErrors": {}})
        data = serve.board_data()
        self.assertEqual(data["cards"], [{"id": "o/r#1"}])
        self.assertTrue(data["auto_promote"])


class TimelineTest(Base):
    def test_timeline_entries(self):
        with mock.patch.object(sources, "gh_json", side_effect=[
                {"createdAt": "2026-10-27T10:00:00Z"},
                [{"event": "labeled", "created_at": "2026-10-27T11:00:00Z", "label": {"name": "factory"}},
                 {"event": "cross-referenced", "created_at": "2026-10-27T12:00:00Z",
                  "source": {"type": "issue", "issue": {"number": 44, "pull_request": {"merged_at": "2026-10-27T12:00:00Z"}}}},
                 {"event": "closed", "created_at": "2026-10-27T12:05:00Z"}]]) as gh:
            entries = board.timeline("o/r", 142)
        texts = [e["txt"] for e in entries]
        self.assertEqual(gh.call_count, 2)
        self.assertIn("Issue created", texts)
        self.assertIn("Label added: factory", texts)
        self.assertIn("PR #44 merged", texts)
        self.assertIn("Issue closed", texts)
        # newest first, capped at 10
        self.assertEqual(entries[0]["txt"], "Issue closed")

    def test_timeline_failure_is_502(self):
        with mock.patch.object(sources, "gh_json", side_effect=[
                {"createdAt": "2026-10-27T10:00:00Z"}, store.FactoryError("gh api failed: boom")]):
            with self.assertRaises(board.BoardError) as cm:
                board.timeline("o/r", 142)
        self.assertEqual(cm.exception.status, 502)

    def test_cross_referenced_other_repo_shows_owner_repo_number(self):
        e = {"event": "cross-referenced",
             "source": {"type": "issue", "issue": {"number": 9,
                                                   "repository_url": "https://api.github.com/repos/o/other",
                                                   "pull_request": {}}}}
        self.assertEqual(board._event_text(e, "o/r"), "o/other#9 opened")
        self.assertEqual(board._event_text(e, "o/other"), "PR #9 opened")

    def test_timeline_route_502(self):
        with mock.patch.object(board, "timeline", side_effect=board.BoardError("gh api failed: boom", 502)):
            code, _ = serve.board_timeline({"repo": ["o/r"], "number": ["142"]})
        self.assertEqual(code, 502)


class BuildBoardTest(Base):
    def test_build_survives_board_failure(self):
        warnings = []
        with mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": None, "error": "gh down", "repoErrors": {}}):
            data = build.board_data(store.products(), NOW, warnings)
        self.assertEqual(data["error"], "gh down")
        self.assertEqual(data["auto_promote"], True)
        self.assertTrue(any(w.startswith("board: gh down") for w in warnings))

    def test_build_warns_per_failed_repo(self):
        warnings = []
        with mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": None, "error": None,
                                                               "repoErrors": {"o/r": "boom"}}):
            build.board_data(store.products(), NOW, warnings)
        self.assertTrue(any("o/r" in w and "boom" in w for w in warnings))

    def test_build_board_data_catches_raise(self):
        warnings = []
        with mock.patch.object(board, "collect", side_effect=RuntimeError("boom")):
            data = build.board_data(store.products(), NOW, warnings)
        self.assertEqual(data["error"], "boom")
        self.assertTrue(any(w.startswith("board:") for w in warnings))


class SimplificationTest(unittest.TestCase):
    def test_source_for(self):
        self.assertEqual(board.source_for({"source:sentry"}), "sentry")
        self.assertEqual(board.source_for({"source:bug"}), "bug")
        self.assertEqual(board.source_for({"source:support"}), "support")
        self.assertEqual(board.source_for(set()), "manual")

    def test_no_unused_colums(self):
        self.assertFalse(hasattr(board, "COLUMNS"))


if __name__ == "__main__":
    unittest.main()
