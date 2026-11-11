#!/usr/bin/env python3
"""Setup for the Release comms browser test: a temp FACTORY_HOME with realistic comms data
built by calling real comms functions (changelog, set_copy, save_draft, decide, sent, failed).
Prints JSON with url, factoryHome, key, and serves until killed.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import comms, release, serve, store, t3, sources
from tests import datasource_env


def setup_temp_factory():
    temp_dir = tempfile.mkdtemp(prefix="factory-comms-")
    os.environ["FACTORY_HOME"] = temp_dir

    db_path = os.path.join(temp_dir, "statev2.sqlite")
    con = sqlite3.connect(db_path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.commit()
    con.close()

    env = {
        **os.environ,
        "FACTORY_HOME": temp_dir,
        **datasource_env(temp_dir, FACTORY_T3_DB=db_path),
    }
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))

    # Add products
    subprocess.check_call([bin_path, "product", "set", "web", "--name", "Web", "--repo", "acme/web"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "product", "set", "app", "--name", "App", "--repo", "acme/app"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "product", "set", "store", "--name", "Store", "--repo", "acme/store"],
                          env=env, stdout=subprocess.DEVNULL)

    subprocess.check_call([bin_path, "release", "product", "add", "web", "--repo", "acme/web", "--flow", "pr", "--project", "web",
                           "--display-name", "Web", "--color", "#4670CF", "--comms-langs", "fr", "en", "--comms-audiences", "creators"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "release", "product", "add", "app", "--repo", "acme/app", "--flow", "pr", "--project", "app",
                           "--display-name", "App", "--color", "#17C964", "--comms-langs", "en", "fr", "--comms-audiences", "creators", "brands"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "release", "product", "add", "store", "--repo", "acme/store", "--flow", "pr", "--project", "store",
                           "--display-name", "Store", "--color", "#5433eb", "--comms-langs", "fr", "en", "--comms-audiences", "customers"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)

    now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)

    # Shipped releases for Web:
    # 1. 2026-10-30: PR 1001 (sent job)
    # 2. 2026-11-01: PR 1050 (failed job)
    # 3. 2026-11-06: PRs 1392, 1155, 1180, 1422, 1205 (active week with needs-approval, deferred, etc.)
    rel = release.release()
    rel["journal"] = [
        {"product": "web", "event": "shipped", "at": "2026-10-30T10:00:00Z", "sha": "1001sha", "prs": [1001]},
        {"product": "web", "event": "shipped", "at": "2026-11-01T10:00:00Z", "sha": "1050sha", "prs": [1050]},
        {"product": "web", "event": "shipped", "at": "2026-11-06T10:00:00Z", "sha": "33dfe87", "prs": [1392, 1155, 1180, 1422, 1205]},
        {"product": "app", "event": "shipped", "at": "2026-11-08T12:00:00Z", "sha": "c2e123", "prs": [42]},
    ]
    release.save_release(rel)

    # Mock gh_json for PR facts
    pr_database = {
        1001: {
            "number": 1001,
            "title": "feat(feed): creator tips sound alert",
            "body": "Roadmap: FEAT-10\nSound alert on new creator tips.",
            "url": "https://github.com/acme/web/pull/1001",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": [{"name": "big"}]},
        },
        1050: {
            "number": 1050,
            "title": "feat(pay): direct bank payouts",
            "body": "Roadmap: FEAT-20\nDirect bank payouts for verified creators.",
            "url": "https://github.com/acme/web/pull/1050",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": [{"name": "big"}]},
        },
        1392: {
            "number": 1392,
            "title": "fix(feed): add Contact to the mobile More sheet",
            "body": "Fixes contact link visibility in mobile view.",
            "url": "https://github.com/acme/web/pull/1392",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": []},
        },
        1155: {
            "number": 1155,
            "title": "feat(feed): one-tap video post to page",
            "body": "Roadmap: FEAT-42\nPublish videos directly to creator page.",
            "url": "https://github.com/acme/web/pull/1155",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": [{"name": "big"}]},
        },
        1180: {
            "number": 1180,
            "title": "feat(pay): direct bank payouts",
            "body": "Roadmap: FEAT-45\nDirect bank payouts for verified creators.",
            "url": "https://github.com/acme/web/pull/1180",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": [{"name": "big"}]},
        },
        1422: {
            "number": 1422,
            "title": "fix: fee switch rollback charges net-credited tips twice",
            "body": "Addresses double charging issue during fee switch rollback.",
            "url": "https://github.com/acme/web/pull/1422",
            "author": {"login": "owner", "__typename": "User"},
            "labels": {"nodes": []},
        },
        1205: {
            "number": 1205,
            "title": "chore(deps): bump prisma from 5.1.0 to 5.2.0",
            "body": "Dependabot bump.",
            "url": "https://github.com/acme/web/pull/1205",
            "author": {"login": "dependabot[bot]", "__typename": "Bot"},
            "labels": {"nodes": []},
        },
    }

    def mock_gh_json(*args, **kwargs):
        repo_data = {}
        for num, fact in pr_database.items():
            repo_data[f"pr_{num}"] = fact
        return {"data": {"repository": repo_data}}

    with mock.patch.object(sources, "gh_json", side_effect=mock_gh_json):
        # 1. Initialize since and build changelogs
        comms.changelog("web", at="2026-10-30T10:00:00Z")
        comms.changelog("web", at="2026-11-01T10:00:00Z")
        comms.changelog("web", at="2026-11-06T10:00:00Z")

        # Initialize since for app (started, but no releases yet)
        with store.lock():
            c = comms.comms()
            c["since"]["app"] = "2026-11-01T00:00:00Z"
            comms.save_comms(c)

        # 2. Set plain-words copy on changelog items
        comms.set_copy("web@2026-11-06T10:00:00Z", {
            "1392": {
                "fr": "Lien de contact direct accessible depuis le menu mobile",
                "en": "Direct contact link accessible from mobile menu"
            },
            "1155": {
                "fr": "Publication vidéo en un clic sur votre page créateur",
                "en": "One-tap video posting directly to creator page"
            },
            "1422": {
                "fr": "Correction du calcul des pourboires lors du basculement des frais",
                "en": "Fixed tip calculation during fee switch rollback"
            }
        })

        # 3. Create and send job 1001 (sent job with real count & sent_at)
        comms.due("web", now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
        draft_1001 = {
            "subject": {"text": {"fr": "Alerte sonore pour vos pourboires", "en": "Sound alert for creator tips"}, "prs": [1001]},
            "sentences": [
                {"text": {"fr": "Recevez une alerte sonore instantanée dès qu'un pourboire arrive.", "en": "Receive instant sound alerts when tips arrive."}, "prs": [1001], "audience": "creators"}
            ]
        }
        comms.save_draft("f-web-1001", draft_1001, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
        j1001 = comms.comms()["jobs"]["f-web-1001"]
        comms.decide("f-web-1001", "send", ask=j1001["ask_id"], now=datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
        comms.sent("f-web-1001", count=1420, now=datetime(2026, 10, 30, 12, 10, tzinfo=timezone.utc))

        # 4. Create and fail job 1050 (failed job with real reason)
        comms.due("web", now=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
        draft_1050 = {
            "subject": {"text": {"fr": "Virements bancaires directs", "en": "Direct bank payouts"}, "prs": [1050]},
            "sentences": [
                {"text": {"fr": "Transférez vos gains directement vers votre compte bancaire.", "en": "Transfer earnings directly to bank account."}, "prs": [1050], "audience": "creators"}
            ]
        }
        comms.save_draft("f-web-1050", draft_1050, now=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
        j1050 = comms.comms()["jobs"]["f-web-1050"]
        comms.decide("f-web-1050", "send", ask=j1050["ask_id"], now=datetime(2026, 11, 1, 12, 5, tzinfo=timezone.utc))
        comms.failed("f-web-1050", "SMTP error: relay server connection timed out after 30s")

        # 5. Create deferred job 1180 (without draft subject to test PR-title fallback)
        comms.due("web", now=now)
        draft_1180 = {"defer": {"1180": "2026-11-20"}}
        comms.save_draft("f-web-1180", draft_1180, now=now)

        # 6. Create needs-approval job 1155 (with video storyboard and social caption)
        draft_1155 = {
            "subject": {"text": {"fr": "Publication vidéo en un clic", "en": "One-tap video posting"}, "prs": [1155]},
            "sentences": [
                {"text": {"fr": "Partagez vos vidéos directement sur votre page sans quitter le flux.", "en": "Post videos directly to your page without leaving feed."}, "prs": [1155], "audience": "creators"}
            ],
            "caption": {
                "text": {
                    "fr": "Publiez en un clic sur Web ! Découvrez la nouvelle expérience vidéo (#1155).",
                    "en": "One-tap video publishing is live on Web! Check out the new video flow (#1155)."
                },
                "prs": [1155]
            },
            "video": {
                "url": "https://assets.example.com/videos/1155.mp4",
                "frames": [
                    {"text": {"fr": "Un seul clic pour partager", "en": "One tap to share"}, "prs": [1155]},
                    {"text": {"fr": "Votre vidéo en direct sur votre page", "en": "Your video live on your page"}, "prs": [1155]},
                    {"text": {"fr": "Disponible dès maintenant sur Web", "en": "Available now on Web"}, "prs": [1155]}
                ]
            }
        }
        comms.save_draft("f-web-1155", draft_1155, now=now)

    key = store.key()
    return {"factoryHome": temp_dir, "t3Db": db_path, "key": key, "bin": bin_path, "env": env}


def main():
    info = setup_temp_factory()
    os.environ["FACTORY_HOME"] = info["factoryHome"]
    for k, v in info["env"].items():
        os.environ[k] = v

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?k={info['key']}"
    print(json.dumps({"url": url, "factoryHome": info["factoryHome"], "key": info["key"], "port": port}), flush=True)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        shutil.rmtree(info["factoryHome"], ignore_errors=True)


if __name__ == "__main__":
    main()
