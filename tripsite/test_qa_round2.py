"""QA round 2 (2026-09-18): adversarial checks on the fix round. Fictional data only.

Run:  uv run python -m unittest tripsite/test_qa_round2.py -v

Tests still marked @expectedFailure are ACCEPTED LIMITS, not open defects (Rob, 2026-09-18:
the sites are invite-only behind Cloudflare Access and invitees may know the hotel). They
cover M1 (offset oracle) and M2 (leak-scan bypasses); see tripsite/README.md, "Known limits
(accepted)". M3 (pruning user-made folders) and L1 (writing through symlinks) were fixed
in round 3 and now run as ordinary tests. L-a (marker only counted in <head>) and L-b
(hardlinked outputs replaced, not written through) were fixed in round 4.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build
from test_build import SLUG, BuildCase

ACCENT_HOTEL = """\
locations:
  - {id: stay1, name: "Casa Pátzcuaro", category: stay, address: "7 Calle Falsa, 99001 Testville",
     lat: 19.4128960, lon: -99.1713001}
"""


class TestLeakScanHolds(BuildCase):
    """Variants the final scan already catches (regression guards)."""

    def _blocked(self, frontmatter: str, body: str) -> None:
        rc, page, err = self.build(frontmatter, body=body)
        self.assertEqual(rc, 1, err)
        self.assertEqual(page, "")
        self.assertIn("privacy check failed", err)

    def test_upper_case_name(self) -> None:
        self._blocked(ACCENT_HOTEL, "We stay at CASA PÁTZCUARO.")

    def test_name_split_across_markdown_lines(self) -> None:
        self._blocked(ACCENT_HOTEL, "We stay at Casa\nPátzcuaro.")

    def test_postcode_inside_url(self) -> None:
        self._blocked(ACCENT_HOTEL, "[map](https://maps.example/?q=99001)")

    def test_coords_in_url_encoded_query(self) -> None:
        self._blocked(ACCENT_HOTEL, "[x](https://www.google.com/maps/search/?api=1&query=19.412896%2C-99.1713001)")

    def test_string_coords_rejected_by_validation(self) -> None:
        rc, _, err = self.build(ACCENT_HOTEL + textwrap.dedent("""\
            popular:
              - {id: p, name: P, category: food, lat: '19.412896', lon: '-99.1713001', default_on: true}
            """))
        self.assertEqual(rc, 1)
        self.assertIn("expected a number", err)

    def test_event_390m_from_stay_is_folded(self) -> None:
        rc, page, err = self.build(ACCENT_HOTEL + textwrap.dedent("""\
            events:
              - {date: 2026-10-22, kind: plan, name: D, location: {lat: 19.4164, lon: -99.1713, label: Bar Secreto}}
            """))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Bar Secreto", page)
        self.assertNotIn("19.4164", page)
        self.assertIn('"ref": "_stay1"', page)


class TestLeakScanBypasses(BuildCase):
    """Accepted limit M2: the page is published although it names the stay (README)."""

    def _blocked(self, frontmatter: str, body: str) -> None:
        rc, _, _ = self.build(frontmatter, body=body)
        self.assertEqual(rc, 1, "published: " + body)

    @unittest.expectedFailure
    def test_name_partly_bold(self) -> None:
        # scan runs on HTML: "<strong>Casa</strong> Pátzcuaro" no longer contains the name
        self._blocked(ACCENT_HOTEL, "We stay at **Casa** Pátzcuaro.")

    @unittest.expectedFailure
    def test_name_without_accent(self) -> None:
        self._blocked(ACCENT_HOTEL, "We stay at Casa Patzcuaro.")

    @unittest.expectedFailure
    def test_name_nfd_decomposed(self) -> None:
        self._blocked(ACCENT_HOTEL, "We stay at Casa Hipo\u0301dromo.")

    @unittest.expectedFailure
    def test_name_double_space(self) -> None:
        self._blocked(ACCENT_HOTEL, "We stay at Casa  Pátzcuaro.")

    @unittest.expectedFailure
    def test_street_when_address_has_no_commas(self) -> None:
        hotel = ACCENT_HOTEL.replace("7 Calle Falsa, 99001 Testville", "7 Calle Falsa 99001 Testville")
        self._blocked(hotel, "Our street is Calle Falsa.")

    @unittest.expectedFailure
    def test_street_when_address_starts_with_building_name(self) -> None:
        hotel = ACCENT_HOTEL.replace("7 Calle Falsa, 99001", "Torre Sol, 7 Calle Falsa, 99001")
        self._blocked(hotel, "Our street is Calle Falsa.")


class TestOffsetStability(unittest.TestCase):
    @unittest.expectedFailure  # accepted limit M1 (README, "Known limits (accepted)")
    def test_slug_change_keeps_the_same_circle(self) -> None:
        # Rotating a leaked slug is the natural fix; two different circles for one stay
        # intersect to a ~63 m-radius area (vs ~150 m for one circle).
        place = {"id": "h", "name": "X", "lat": 19.4128960, "lon": -99.1713001}
        self.assertEqual(build.offset_centre("trip-aaaa-1111", place)[:2],
                         build.offset_centre("trip-aaaa-2222", place)[:2])


class TestPruneSafety(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.trip = self.tmp / "t.md"
        self.trip.write_text("---\n" + textwrap.dedent(f"""\
            trip: {{name: T, slug: {SLUG}, start: 2026-10-22, end: 2026-10-24, timezone: America/Mexico_City,
              city: C, country: X, center: {{lat: 19.42, lon: -99.155}}, zoom: 12, theme: noir}}
            privacy: {{hotel_display: hidden}}
            """) + "---\n", encoding="utf-8")
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_build(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = build.main([str(self.trip), "--out", str(self.out)])
        return rc, err.getvalue()

    def test_symlinked_folder_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "index.html").write_text("keep")
        self.out.mkdir()
        (self.out / "old-trip-0001").symlink_to(outside, target_is_directory=True)
        rc, err = self.run_build()
        self.assertEqual(rc, 1)
        self.assertIn("was not written by this tool", err)
        self.assertEqual((outside / "index.html").read_text(), "keep")

    def test_prefix_slug_only_removes_the_stale_one(self) -> None:
        for name in (SLUG, SLUG + "-2"):
            (self.out / name).mkdir(parents=True)
            (self.out / name / "index.html").write_text(f"<head>{build.GENERATOR_META}</head>x")
        rc, _ = self.run_build()
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / SLUG / "index.html").exists())
        self.assertFalse((self.out / (SLUG + "-2")).exists())

    def test_stale_symlinked_index_is_refused(self) -> None:
        # Round 3: was "only unlinks the link" (rc 0). Pruning now never touches a symlink:
        # the build stops, lists it, and leaves both the link and its target alone.
        precious = self.tmp / "precious.txt"
        precious.write_text("PRECIOUS")
        (self.out / "old-trip-0002").mkdir(parents=True)
        link = self.out / "old-trip-0002" / "index.html"
        link.symlink_to(precious)
        rc, err = self.run_build()
        self.assertEqual(rc, 1)
        self.assertIn("is a symlink", err)
        self.assertTrue(link.is_symlink())
        self.assertEqual(precious.read_text(), "PRECIOUS")

    def test_user_made_folder_is_not_deleted(self) -> None:
        # A hand-made dist/my-notes/index.html looks like a stale trip and is deleted.
        (self.out / "my-notes").mkdir(parents=True)
        (self.out / "my-notes" / "index.html").write_text("<h1>my handmade page</h1>")
        self.run_build()
        self.assertTrue((self.out / "my-notes" / "index.html").exists())

    def test_marker_outside_head_is_refused(self) -> None:
        # L-a: only the <head> counts. A marker in the body, or in a comment, is not ours.
        meta = build.GENERATOR_META
        pages = {
            "body": f"<html><head><title>mine</title></head><body>{meta}</body></html>",
            "body comment": f"<html><head><title>mine</title></head><body><!-- {meta} --></body></html>",
            "head comment": f"<html><head><!-- {meta} --><title>mine</title></head><body>x</body></html>",
        }
        for label, text in pages.items():
            with self.subTest(label):
                (self.out / "old-trip-0003").mkdir(parents=True, exist_ok=True)
                page = self.out / "old-trip-0003" / "index.html"
                page.write_text(text)
                rc, err = self.run_build()
                self.assertEqual(rc, 1)
                self.assertIn("old-trip-0003 has no tripsite marker", err)
                self.assertEqual(page.read_text(), text)

    def test_hardlinked_output_is_not_written_through(self) -> None:
        # L-b: output is replaced (new inode), never truncated through a hardlink.
        for rel in (Path(SLUG) / "index.html", Path("index.html"), Path("404.html")):
            with self.subTest(str(rel)):
                other = self.tmp / f"other-{rel.name}-{rel.parent.name}"
                other.write_text("PRECIOUS")
                target = self.out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                os.link(other, target)
                rc, err = self.run_build()
                self.assertEqual(rc, 0, err)
                self.assertEqual(other.read_text(), "PRECIOUS")
                self.assertIn(build.GENERATOR_META, target.read_text())
                self.assertNotEqual(os.stat(other).st_ino, os.stat(target).st_ino)

    def test_symlinked_output_file_is_not_written_through(self) -> None:
        precious = self.tmp / "precious.txt"
        precious.write_text("PRECIOUS")
        (self.out / SLUG).mkdir(parents=True)
        (self.out / SLUG / "index.html").symlink_to(precious)
        self.run_build()
        self.assertEqual(precious.read_text(), "PRECIOUS")


if __name__ == "__main__":
    unittest.main()
