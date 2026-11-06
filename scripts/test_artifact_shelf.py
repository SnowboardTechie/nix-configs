#!/usr/bin/env python3
"""The Artifact Shelf CLI (modules/services/artifact-shelf.py) against temp roots."""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "modules/services/artifact-shelf.py"
BASE = "https://shelf.example:8092"
NOW = "2026-11-06T10:00:00-07:00"


def load():
    sys.dont_write_bytecode = True  # keep __pycache__ out of modules/services
    spec = importlib.util.spec_from_file_location("artifact_shelf", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shelf = load()


class ShelfTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-shelf-test-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp / "root"
        self.home = self.tmp / "home"
        self.home.mkdir()
        # Link sources live here; the system temp dirs are not "pruned" for
        # these tests unless a test says so.
        self.keep = self.tmp / "keep"
        self.keep.mkdir()
        self.pruned = self.tmp / "pruned"
        self.pruned.mkdir()
        env = {
            "ARTIFACT_SHELF_ROOT": str(self.root),
            "ARTIFACT_SHELF_URL": BASE,
            "ARTIFACT_SHELF_OLDER_DAYS": "30",
            "ARTIFACT_SHELF_NOW": NOW,
            "ARTIFACT_SHELF_PRUNED_DIRS": str(self.pruned),
            "HOME": str(self.home),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args, now=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"ARTIFACT_SHELF_NOW": now} if now else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shelf.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def source(self, name="src", where=None, body="<h1>hi</h1>", entry="index.html"):
        path = (where or self.keep) / name
        path.mkdir(parents=True, exist_ok=True)
        (path / entry).parent.mkdir(parents=True, exist_ok=True)
        (path / entry).write_text(body)
        return path

    def publish(self, src, slug, *extra, title="Title", project="Proj", description="Desc", now=None):
        return self.run_cli(
            "publish", str(src), f"--slug={slug}", "--title", title, "--project", project,
            "--description", description, *extra, now=now,
        )

    def meta(self, slug):
        return json.loads((self.root / "meta" / f"{slug}.json").read_text())

    def index(self):
        return (self.root / "items" / "index.html").read_text()

    def listing(self):
        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual(0, code)
        return {r["slug"]: r for r in json.loads(out)}


class PublishTests(ShelfTestCase):
    def test_publish_copies_and_prints_url(self):
        src = self.source()
        code, out, err = self.publish(src, "2026-11-06-briefing")
        self.assertEqual(0, code, err)
        self.assertEqual(f"{BASE}/2026-11-06-briefing/\n", out)
        item = self.root / "items" / "2026-11-06-briefing"
        self.assertFalse(item.is_symlink())
        self.assertEqual("<h1>hi</h1>", (item / "index.html").read_text())
        record = self.meta("2026-11-06-briefing")
        self.assertEqual(
            {"slug", "title", "project", "description", "entry", "created", "updated", "linked", "source"},
            set(record),
        )
        self.assertFalse(record["linked"])
        self.assertEqual(str(src.resolve()), record["source"])
        self.assertEqual(NOW, record["created"])
        self.assertIn("2026-11-06-briefing/", self.index())
        # No staging leftovers in the served tree.
        self.assertEqual(["2026-11-06-briefing", "index.html"], sorted(p.name for p in (self.root / "items").iterdir()))

    def test_publish_link_makes_a_symlink(self):
        src = self.source()
        code, _, err = self.publish(src, "2026-11-06-proto", "--link")
        self.assertEqual(0, code, err)
        item = self.root / "items" / "2026-11-06-proto"
        self.assertTrue(item.is_symlink())
        self.assertEqual(src.resolve(), Path(os.readlink(item)))
        self.assertTrue(self.meta("2026-11-06-proto")["linked"])

    def test_link_refused_for_pruned_sources(self):
        scratch = self.home / ".hermes" / "cache" / "scratch"
        for where in (self.pruned, scratch):
            with self.subTest(where=where):
                src = self.source(where=where)
                code, _, err = self.publish(src, "2026-11-06-x", "--link")
                self.assertEqual(2, code)
                self.assertIn("refusing --link", err)
                self.assertFalse(os.path.lexists(self.root / "items" / "2026-11-06-x"))

    def test_link_refused_for_system_temp_by_default(self):
        with mock.patch.dict(os.environ):
            del os.environ["ARTIFACT_SHELF_PRUNED_DIRS"]
            # self.tmp is under the system temp dir (/var/folders on macOS).
            code, _, err = self.publish(self.source(), "2026-11-06-x", "--link")
        self.assertEqual(2, code)
        self.assertIn("refusing --link", err)
        # Copying from there is fine.
        with mock.patch.dict(os.environ):
            del os.environ["ARTIFACT_SHELF_PRUNED_DIRS"]
            code, _, err = self.publish(self.source(), "2026-11-06-x")
        self.assertEqual(0, code, err)

    def test_symlink_leading_outside_the_source_refused(self):
        secret = self.tmp / "secret"
        secret.mkdir()
        (secret / "id_ed25519").write_text("private")
        for kind in ("file", "dir"):
            for extra in ((), ("--link",)):
                with self.subTest(kind=kind, link=bool(extra)):
                    src = self.source(f"leak-{kind}")
                    for child in src.iterdir():
                        if child.is_symlink():
                            child.unlink()
                    target = secret / "id_ed25519" if kind == "file" else secret
                    (src / "notes").symlink_to(target)
                    code, _, err = self.publish(src, "2026-11-06-leak", *extra)
                    self.assertEqual(2, code)
                    self.assertIn("points outside the source", err)
                    self.assertFalse(os.path.lexists(self.root / "items" / "2026-11-06-leak"))
        broken = self.source("broken")
        (broken / "dangling").symlink_to(self.tmp / "nowhere")
        self.assertEqual(2, self.publish(broken, "2026-11-06-leak")[0])

    def test_symlink_inside_the_source_allowed(self):
        src = self.source()
        (src / "img").mkdir()
        (src / "img" / "a.png").write_bytes(b"png")
        (src / "alias.png").symlink_to(src / "img" / "a.png")
        code, _, err = self.publish(src, "2026-11-06-inner")
        self.assertEqual(0, code, err)
        self.assertTrue((self.root / "items/2026-11-06-inner/alias.png").is_symlink())

    def test_slug_validation(self):
        src = self.source()
        for bad in ("../etc", "2026-11-06-../x", "a/b", "Upper", "2026-11-06-Upper", "a.b", "2026-11-06-a.b",
                    "a--b", "-a", "a-", "", "2026-13-40-x", "2026-11-06-"):
            with self.subTest(slug=bad):
                code, _, err = self.publish(src, bad)
                self.assertEqual(2, code, bad)
                self.assertIn("invalid slug", err)
        self.assertEqual([], [p for p in (self.root / "items").iterdir() if p.name != "index.html"] if (self.root / "items").exists() else [])
        for bad in ("../x", "2026-11-06-a/b", "2026-11-06-A"):
            with self.subTest(command="retire", slug=bad):
                self.assertEqual(2, self.run_cli("retire", bad)[0])

    def test_date_prefix_added(self):
        code, out, _ = self.publish(self.source(), "gallery")
        self.assertEqual(0, code)
        self.assertEqual(f"{BASE}/2026-11-06-gallery/\n", out)
        self.assertTrue((self.root / "meta" / "2026-11-06-gallery.json").exists())

    def test_entry_must_exist_and_is_in_url(self):
        src = self.source(entry="pages/start.html")
        code, _, err = self.publish(src, "2026-11-06-e")
        self.assertEqual(2, code)
        self.assertIn("entry", err)
        code, out, _ = self.publish(src, "2026-11-06-e", "--entry", "pages/start.html")
        self.assertEqual(0, code)
        self.assertEqual(f"{BASE}/2026-11-06-e/pages/start.html\n", out)
        self.assertEqual(2, self.publish(src, "2026-11-06-f", "--entry", "../x.html")[0])

    def test_duplicate_refused_and_replace_keeps_created(self):
        src = self.source(body="v1")
        self.assertEqual(0, self.publish(src, "2026-11-06-d")[0])
        code, _, err = self.publish(self.source("v2", body="v2"), "2026-11-06-d")
        self.assertEqual(2, code)
        self.assertIn("--replace", err)
        later = "2026-11-07T09:30:00-07:00"
        code, _, err = self.publish(self.source("v2", body="v2"), "2026-11-06-d", "--replace", title="New", now=later)
        self.assertEqual(0, code, err)
        record = self.meta("2026-11-06-d")
        self.assertEqual(NOW, record["created"])
        self.assertEqual(later, record["updated"])
        self.assertEqual("New", record["title"])
        self.assertEqual("v2", (self.root / "items/2026-11-06-d/index.html").read_text())
        # Replacing a copy with a link and back.
        self.assertEqual(0, self.publish(src, "2026-11-06-d", "--replace", "--link")[0])
        self.assertTrue((self.root / "items/2026-11-06-d").is_symlink())
        self.assertEqual(0, self.publish(self.source("v2"), "2026-11-06-d", "--replace")[0])
        self.assertFalse((self.root / "items/2026-11-06-d").is_symlink())
        self.assertEqual("v1", (src / "index.html").read_text(), "the old link target is untouched")
        self.assertEqual(["2026-11-06-d", "index.html"], sorted(p.name for p in (self.root / "items").iterdir()))

    def test_touch_bumps_updated(self):
        self.publish(self.source(), "2026-11-06-t")
        later = "2026-11-08T08:00:00-07:00"
        self.assertEqual(0, self.run_cli("touch", "2026-11-06-t", now=later)[0])
        self.assertEqual(later, self.meta("2026-11-06-t")["updated"])
        self.assertEqual(NOW, self.meta("2026-11-06-t")["created"])

    def test_url_command(self):
        self.publish(self.source(), "2026-11-06-u")
        self.assertEqual((0, f"{BASE}/2026-11-06-u/\n", ""), self.run_cli("url", "2026-11-06-u"))
        self.assertEqual(2, self.run_cli("url", "2026-11-06-nope")[0])


class RetireTests(ShelfTestCase):
    def test_retire_moves_symlink_without_touching_target(self):
        src = self.source()
        self.publish(src, "2026-11-06-r", "--link")
        code, out, err = self.run_cli("retire", "2026-11-06-r")
        self.assertEqual(0, code, err)
        destination = Path(out.strip())
        self.assertEqual(self.root / "retired", destination.parent)
        self.assertTrue(destination.name.startswith("2026-11-06-r-"))
        self.assertTrue((destination / "2026-11-06-r").is_symlink())
        self.assertTrue((destination / "2026-11-06-r.json").is_file())
        self.assertEqual("<h1>hi</h1>", (src / "index.html").read_text())
        self.assertFalse(os.path.lexists(self.root / "items/2026-11-06-r"))
        self.assertNotIn("2026-11-06-r", self.index())
        self.assertEqual({}, self.listing())

    def test_retire_twice_in_one_second_keeps_both(self):
        self.publish(self.source(), "2026-11-06-r")
        self.run_cli("retire", "2026-11-06-r")
        self.publish(self.source(), "2026-11-06-r")
        self.assertEqual(0, self.run_cli("retire", "2026-11-06-r")[0])
        self.assertEqual(2, len(list((self.root / "retired").iterdir())))


class IndexTests(ShelfTestCase):
    def test_missing_entry_and_broken_link_hidden_but_listed(self):
        self.publish(self.source("ok"), "2026-11-06-ok")
        gone = self.source("gone")
        self.publish(gone, "2026-11-06-broken", "--link")
        self.publish(self.source("noentry"), "2026-11-06-noentry")
        (self.root / "items/2026-11-06-noentry/index.html").unlink()
        __import__("shutil").rmtree(gone)
        self.assertEqual(0, self.run_cli("index")[0])
        page = self.index()
        self.assertIn('href="2026-11-06-ok/"', page)
        self.assertNotIn("2026-11-06-broken", page)
        self.assertNotIn("2026-11-06-noentry", page)
        listing = self.listing()
        self.assertEqual("ok", listing["2026-11-06-ok"]["status"])
        self.assertEqual("missing", listing["2026-11-06-broken"]["status"])
        self.assertEqual("missing", listing["2026-11-06-noentry"]["status"])
        self.assertTrue(listing["2026-11-06-broken"]["linked"])
        self.assertIn("1 artifact,", page)

    def test_recent_and_older_split_at_threshold(self):
        self.publish(self.source("a"), "2026-11-06-fresh", now="2026-11-01T10:00:00-07:00")
        self.publish(self.source("b"), "2026-10-07-edge", now="2026-10-07T10:00:01-07:00")
        self.publish(self.source("c"), "2026-10-01-stale", now="2026-10-01T10:00:00-07:00")
        self.run_cli("index")
        page = self.index()
        recent, older = page.split("<details>")
        self.assertIn("2026-11-06-fresh", recent)
        self.assertIn("2026-10-07-edge", recent)
        self.assertIn("2026-10-01-stale", older)
        self.assertIn("<summary>Older (1)</summary>", older)
        self.assertLess(recent.index("2026-11-06-fresh"), recent.index("2026-10-07-edge"), "newest first")

    def test_unlabeled_item_shown(self):
        bare = self.root / "items" / "2026-11-05-bare"
        bare.mkdir(parents=True)
        (bare / "index.html").write_text("x")
        (self.root / "items" / "not-a-slug").mkdir()
        (self.root / "items" / "not-a-slug" / "index.html").write_text("x")
        self.run_cli("index")
        page = self.index()
        self.assertIn('href="2026-11-05-bare/">2026-11-05-bare</a>', page)
        self.assertIn('<span class="project">Unlabeled</span>', page)
        self.assertNotIn("not-a-slug", page)
        self.assertEqual("Unlabeled", self.listing()["2026-11-05-bare"]["project"])

    def test_user_strings_are_escaped(self):
        self.publish(self.source(), "2026-11-06-x", title="<script>alert(1)</script>",
                     project='"><img src=x>', description="a & b")
        page = self.index()
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn('"><img src=x>', page)
        self.assertIn("a &amp; b", page)
        self.assertEqual(1, page.count("<script>"), "only the page's own filter script")

    def test_index_written_through_temp_file_and_replace(self):
        self.publish(self.source(), "2026-11-06-x")
        (self.root / "items/index.html").unlink()
        calls = []
        real_replace = os.replace

        def spy(src, dst):
            calls.append((Path(src), Path(dst), Path(src).read_text()))
            return real_replace(src, dst)

        with mock.patch.object(shelf.os, "replace", spy):
            self.run_cli("index")
        target = self.root / "items/index.html"
        [(src, dst, content)] = [c for c in calls if c[1] == target]
        self.assertNotEqual(target, src)
        self.assertNotEqual(self.root / "items", src.parent, "the temp file is outside the served tree")
        self.assertTrue(content.endswith("</html>\n"), "the whole page was written before the replace")
        self.assertFalse(src.exists())

    def test_unchanged_index_is_not_rewritten(self):
        self.publish(self.source(), "2026-11-06-x")
        with mock.patch.object(shelf.os, "replace") as replace:
            self.run_cli("index")
        replace.assert_not_called()

    def test_thumbnail_rendered_when_present(self):
        src = self.source()
        (src / "shelf-thumb.png").write_bytes(b"\x89PNG")
        self.publish(src, "2026-11-06-thumb")
        self.publish(self.source("plain"), "2026-11-06-plain")
        page = self.index()
        self.assertIn('<img src="2026-11-06-thumb/shelf-thumb.png" alt="" loading="lazy">', page)
        self.assertEqual(1, page.count("<img "))

    def test_page_shape(self):
        self.run_cli("index")
        page = self.index()
        self.assertIn('<meta name="generator" content="artifact-shelf">', page)
        self.assertIn('name="viewport"', page)
        self.assertIn("<title>Artifact Shelf</title>", page)
        self.assertIn("Nothing is on the shelf yet.", page)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIsNone(re.search(r"(src|href)=\"(https?:)?//", page), "no external requests")

    def test_chips_and_created_and_size(self):
        for i in range(200):
            self.publish(self.source(f"s{i}"), f"2026-11-06-item-{i}", project=f"P{i % 5}",
                         title="A reasonably long artifact title " * 2, description="A one-line description " * 4)
        self.publish(self.source("s0"), "2026-11-06-item-0", "--replace", now="2026-11-07T10:00:00-07:00")
        page = self.index()
        self.assertLess(len(page.encode()), 200_000)
        self.assertIn('<div class="chips" hidden>', page)
        for p in range(5):
            self.assertIn(f'data-project="P{p}" aria-pressed="false">P{p}</button>', page)
        self.assertIn("Updated 2026-11-07 · created 2026-11-06", page)
        self.assertIn("200 artifacts, updated 2026-11-07 10:00", page)


if __name__ == "__main__":
    unittest.main()
