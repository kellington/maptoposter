"""Regression tests for tripsite/build.py (stdlib unittest; no extra dependencies).

Run:  uv run python -m unittest tripsite/test_build.py -v

All trip data here is fictional. The five QA defects found on 2026-09-18 were
marked @expectedFailure; they are fixed and now run as ordinary tests.
"""

from __future__ import annotations

import contextlib
import io
import math
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build

SLUG = "test-trip-1234"
HOTEL_LAT, HOTEL_LON = 19.4128960, -99.1713001

BASE = """\
trip:
  name: Test Trip
  slug: {slug}
  start: 2026-10-22
  end: {end}
  timezone: America/Mexico_City
  city: Testville
  country: Testland
  center: {{lat: 19.42, lon: -99.155}}
  zoom: 12
  theme: terracotta
privacy:
  hotel_display: {display}
"""

HOTEL = f"""\
locations:
  - {{id: secret-inn, name: Secret Inn, category: stay, address: 1 Hidden Lane,
      lat: {HOTEL_LAT}, lon: {HOTEL_LON}, notes: room 12, url: https://secret-inn.example}}
"""

OLD_PAGE = f"<!doctype html><head>{build.GENERATOR_META}</head>old"  # a page this tool wrote

PRIVATE_STRINGS = ["Secret Inn", "Hidden Lane", "secret-inn", "room 12", "secret-inn.example",
                   str(HOTEL_LAT), str(HOTEL_LON)]


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


class BuildCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, frontmatter: str, body: str = "", display: str = "approximate",
              end: str = "2026-10-24") -> tuple[int, str, str]:
        """Returns (exit code, trip page html or '', stderr)."""
        trip = self.tmp / "trip.md"
        trip.write_text("---\n" + BASE.format(slug=SLUG, end=end, display=display)
                        + textwrap.dedent(frontmatter) + "---\n" + body, encoding="utf-8")
        out = self.tmp / "dist"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = build.main([str(trip), "--out", str(out)])
        page = out / SLUG / "index.html"
        return rc, (page.read_text(encoding="utf-8") if page.exists() else ""), err.getvalue()

    def load_data(self, frontmatter: str, display: str = "approximate") -> tuple[dict, build.Problems]:
        trip = self.tmp / "trip.md"
        trip.write_text("---\n" + BASE.format(slug=SLUG, end="2026-10-24", display=display)
                        + textwrap.dedent(frontmatter) + "---\n", encoding="utf-8")
        data, body, p = build.load_trip(trip)
        if not p.errors:
            build.filter_events(data, p)
            build.apply_privacy(data, body, p)
        return data, p


class TestHappyAndEdges(BuildCase):
    def test_outputs_and_noindex(self) -> None:
        rc, page, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', page)
        root = (self.tmp / "dist" / "index.html").read_text()
        self.assertIn("noindex,nofollow", root)
        self.assertNotIn(SLUG, root)
        self.assertEqual((self.tmp / "dist" / "robots.txt").read_text(), "User-agent: *\nDisallow: /\n")

    def test_empty_sections(self) -> None:
        rc, page, _ = self.build("locations: []\npopular:\nevents: []\n")
        self.assertEqual(rc, 0)
        self.assertIn("No popular places listed.", page)
        self.assertIn("None yet.", page)
        self.assertIn("Nothing here yet.", page)

    def test_one_day_trip_unicode_and_event_without_location(self) -> None:
        rc, page, _ = self.build(HOTEL + textwrap.dedent("""\
            popular:
              - {id: cafe, name: "Café Ñandú 🦜 <b>x</b>", category: café, lat: 19.41, lon: -99.16, default_on: false}
            events:
              - {date: 2026-10-22, kind: plan, name: "Dîner à l'hôtel 🍽"}
            """), end="2026-10-22")
        self.assertEqual(rc, 0)
        self.assertIn("· 1 day ·", page)
        self.assertIn("Café Ñandú 🦜 &lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn('class="linkish event-go"', page)  # no location -> no map button

    def test_duplicate_ids_rejected(self) -> None:
        rc, _, err = self.build("""\
            locations:
              - {id: a, name: A, category: food, address: x, lat: 1, lon: 1}
            popular:
              - {id: a, name: A2, category: food, lat: 1, lon: 1, default_on: true}
            """)
        self.assertEqual(rc, 1)
        self.assertIn("duplicate id 'a'", err)

    def test_yaml_no_becomes_bool_and_is_rejected(self) -> None:
        rc, _, err = self.build(HOTEL.replace("Secret Inn", "no"))
        self.assertEqual(rc, 1)
        self.assertIn("expected text, got bool", err)

    def test_unquoted_sexagesimal_time_is_recovered(self) -> None:
        data, p = self.load_data(HOTEL + "events:\n  - {date: 2026-10-22, time: 12:00, kind: plan, name: Noon}\n")
        self.assertFalse(p.errors)
        self.assertEqual(data["events"][0]["time"], "12:00")

    def test_script_breakout_is_escaped(self) -> None:
        rc, page, _ = self.build("""\
            locations:
              - {id: x, name: "</script><script>alert(1)</script>", category: food, address: a, lat: 1, lon: 1}
            """)
        self.assertEqual(rc, 0)
        self.assertNotIn("</script><script>alert", page)


class TestPrivacy(BuildCase):
    def test_approximate_strips_hotel_details(self) -> None:
        rc, page, _ = self.build(HOTEL + "events:\n  - {date: 2026-10-22, kind: plan, name: Nap, location: secret-inn}\n")
        self.assertEqual(rc, 0)
        for s in PRIVATE_STRINGS:
            self.assertNotIn(s, page)
        self.assertIn('"ref": "_stay1"', page)
        self.assertIn(f'"radius": {build.APPROX_RADIUS_M}', page)

    def test_hidden_has_no_hotel_data(self) -> None:
        rc, page, _ = self.build(HOTEL + "events:\n  - {date: 2026-10-22, kind: plan, name: Nap, location: secret-inn}\n",
                                 display="hidden")
        self.assertEqual(rc, 0)
        for s in PRIVATE_STRINGS + ["_stay", '"approx"', "19.413", "-99.171"]:
            self.assertNotIn(s, page)
        self.assertIn("Nap", page)  # event kept, map link dropped
        self.assertNotIn('class="linkish event-go"', page)

    def test_circle_centre_does_not_pinpoint_hotel(self) -> None:
        # FIXED (was: centre = exact coords rounded to 3 dp, 33.5 m from the hotel).
        data, _ = self.load_data(HOTEL)
        stay = data["ours"][0]
        self.assertGreaterEqual(metres(stay["lat"], stay["lon"], HOTEL_LAT, HOTEL_LON), 100)

    def test_event_inline_coords_at_hotel_warn_or_strip(self) -> None:
        # FIXED (was: an event {lat, lon, label} at the hotel published exact coords + label).
        _, page, err = self.build(HOTEL + textwrap.dedent(f"""\
            events:
              - date: 2026-10-22
                kind: plan
                name: Check in
                location: {{lat: {HOTEL_LAT}, lon: {HOTEL_LON}, label: Secret Inn front desk}}
            """))
        self.assertTrue("Secret Inn" not in page or "Secret Inn" in err)

    def test_stay_under_popular_is_protected(self) -> None:
        # FIXED (was: category stay under `popular` shown exactly). Now rejected.
        rc, page, _ = self.build(HOTEL + textwrap.dedent("""\
            popular:
              - {id: backup, name: Backup Hostel, category: stay, address: 9 Private St, lat: 19.40, lon: -99.16, default_on: true}
            """))
        self.assertTrue(rc != 0 or "9 Private St" not in page)

    def test_leak_warning_covers_popular_notes(self) -> None:
        # FIXED (was: only body + event name/notes were checked).
        _, p = self.load_data(HOTEL + textwrap.dedent("""\
            popular:
              - {id: taco, name: Taco stand, category: food, lat: 19.41, lon: -99.17, default_on: true,
                 notes: "Right outside Secret Inn"}
            """))
        self.assertTrue(any("Secret Inn" in w for w in p.warnings))


class TestYamlGotchas(BuildCase):
    def test_bare_integer_time_is_rejected(self) -> None:
        # FIXED (was: any int < 1440 read as minutes, so `time: 930` became 15:30).
        _, p = self.load_data(HOTEL + "events:\n  - {date: 2026-10-22, time: 930, kind: plan, name: Breakfast}\n")
        self.assertTrue(p.errors)
        self.assertIn('quoted "HH:MM"', p.errors[0])

    def test_unquoted_single_digit_hour_is_recovered(self) -> None:
        data, p = self.load_data(HOTEL + "events:\n  - {date: 2026-10-22, time: 9:30, kind: plan, name: B}\n")
        self.assertFalse(p.errors)
        self.assertEqual(data["events"][0]["time"], "09:30")

    def test_bare_small_integer_time_is_rejected(self) -> None:
        _, p = self.load_data(HOTEL + "events:\n  - {date: 2026-10-22, time: 12, kind: plan, name: B}\n")
        self.assertTrue(any("bare number" in e for e in p.errors))

    def test_duplicate_yaml_keys_rejected(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self.load_data(HOTEL + "locations: []\n")
        self.assertIn("duplicate key 'locations'", str(cm.exception))

    def test_duplicate_nested_keys_rejected(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self.load_data("locations:\n  - {id: a, id: b, name: A, category: food, address: x, lat: 1, lon: 1}\n")
        self.assertIn("duplicate key 'id'", str(cm.exception))

    def test_bool_for_text_has_quote_hint(self) -> None:
        rc, _, err = self.build(HOTEL.replace("Secret Inn", "NO"))
        self.assertEqual(rc, 1)
        self.assertIn('quote it, e.g. "NO"', err)

    def test_numeric_ids_match(self) -> None:
        data, p = self.load_data(textwrap.dedent("""\
            locations:
              - {id: 1, name: One, category: food, address: x, lat: 19.40, lon: -99.10}
            events:
              - {date: 2026-10-22, kind: plan, name: Lunch, location: 1}
            """))
        self.assertFalse(p.errors)
        self.assertEqual(data["ours"][0]["id"], "1")
        self.assertEqual(data["events"][0]["loc"], {"ref": "1"})


NEAR_HOTEL = (19.4150, -99.1700)  # ~285 m from the hotel


class TestPrivacyChecks(BuildCase):
    def test_circle_contains_hotel_and_is_deterministic(self) -> None:
        data1, _ = self.load_data(HOTEL)
        data2, _ = self.load_data(HOTEL)
        stay = data1["ours"][0]
        self.assertEqual((stay["lat"], stay["lon"]), (data2["ours"][0]["lat"], data2["ours"][0]["lon"]))
        d = metres(stay["lat"], stay["lon"], HOTEL_LAT, HOTEL_LON)
        self.assertGreaterEqual(d, 149.5)
        self.assertLessEqual(d, 250.5)
        self.assertLessEqual(d + 150, stay["radius"])
        # neither coordinate matches the hotel's at 4 dp
        self.assertNotEqual(round(stay["lat"], 4), round(HOTEL_LAT, 4))
        self.assertNotEqual(round(stay["lon"], 4), round(HOTEL_LON, 4))

    def test_offset_bounds_over_many_seeds(self) -> None:
        for i in range(300):
            place = {"id": f"h{i}", "name": "X", "lat": HOTEL_LAT, "lon": HOTEL_LON}
            lat, lon, d = build.offset_centre(f"slug-{i}", place)
            self.assertTrue(149.5 <= d <= 250.5, d)
            self.assertLessEqual(d + 150, build.APPROX_RADIUS_M)
            self.assertNotEqual(f"{lat:.4f}", f"{HOTEL_LAT:.4f}")
            self.assertNotEqual(f"{lon:.4f}", f"{HOTEL_LON:.4f}")

    def test_stay_category_synonyms_and_private_flag(self) -> None:
        for cat in ("Hotel", "AIRBNB", "lodging", "accommodation", "hostel"):
            rc, page, _ = self.build(HOTEL.replace("category: stay", f"category: {cat}"))
            self.assertEqual(rc, 0, cat)
            self.assertNotIn("Secret Inn", page, cat)
            self.assertNotIn(cat, page.split('id="trip-data"')[1], cat)  # real category not published
        rc, page, _ = self.build(HOTEL.replace("category: stay", "category: food, private: true"))
        self.assertEqual(rc, 0)
        self.assertNotIn("Secret Inn", page)
        self.assertIn('"approx": true', page)

    def test_private_flag_under_popular_rejected(self) -> None:
        rc, _, err = self.build(HOTEL + textwrap.dedent("""\
            popular:
              - {id: aunt, name: Aunt's flat, category: sight, private: true, lat: 19.40, lon: -99.16, default_on: true}
            """))
        self.assertEqual(rc, 1)
        self.assertIn("put stays under locations", err)

    def test_event_near_stay_is_folded_into_stay(self) -> None:
        data, p = self.load_data(HOTEL + textwrap.dedent(f"""\
            events:
              - date: 2026-10-22
                kind: plan
                name: Drinks
                location: {{lat: {NEAR_HOTEL[0]}, lon: {NEAR_HOTEL[1]}, label: Bar next to Secret Inn}}
            """))
        self.assertEqual(data["events"][0]["loc"], {"ref": "_stay1"})
        self.assertTrue(any("from a private stay" in w for w in p.warnings))

    def test_event_near_stay_hidden_drops_location(self) -> None:
        rc, page, _ = self.build(HOTEL + textwrap.dedent(f"""\
            events:
              - date: 2026-10-22
                kind: plan
                name: Drinks
                location: {{lat: {NEAR_HOTEL[0]}, lon: {NEAR_HOTEL[1]}, label: Bar next to Secret Inn}}
            """), display="hidden")
        self.assertEqual(rc, 0)
        self.assertNotIn("Bar next to", page)
        self.assertNotIn(str(NEAR_HOTEL[0]), page)
        self.assertNotIn('class="linkish event-go"', page)

    def test_event_far_from_stay_is_untouched(self) -> None:
        data, p = self.load_data(HOTEL + textwrap.dedent("""\
            events:
              - {date: 2026-10-22, kind: city, name: Parade, location: {lat: 19.4270, lon: -99.1677, label: Reforma}}
            """))
        self.assertEqual(data["events"][0]["loc"]["label"], "Reforma")
        self.assertFalse(any("private stay" in w for w in p.warnings))

    def test_exact_mode_publishes_stay(self) -> None:
        rc, page, _ = self.build(HOTEL, body="We stay at Secret Inn.", display="exact")
        self.assertEqual(rc, 0)
        self.assertIn("Secret Inn", page)

    def _assert_leak_blocks(self, frontmatter: str, body: str = "", display: str = "approximate") -> None:
        rc, page, err = self.build(frontmatter, body=body, display=display)
        self.assertEqual(rc, 1, err)
        self.assertEqual(page, "")  # nothing written
        self.assertIn("privacy check failed", err)

    def test_leak_scan_name_in_body(self) -> None:
        self._assert_leak_blocks(HOTEL, body="We are at *Secret Inn* all week.")

    def test_leak_scan_name_in_body_hidden(self) -> None:
        self._assert_leak_blocks(HOTEL, body="We are at Secret Inn.", display="hidden")

    def test_leak_scan_street_in_popular_notes(self) -> None:
        self._assert_leak_blocks(HOTEL + textwrap.dedent("""\
            popular:
              - {id: taco, name: Taco stand, category: food, lat: 19.40, lon: -99.16, default_on: true,
                 notes: "Corner of hidden lane"}
            """))

    def test_leak_scan_postcode(self) -> None:
        hotel = HOTEL.replace("address: 1 Hidden Lane", "address: '1 Hidden Lane, 99001 Testville'")
        self._assert_leak_blocks(hotel, body="Postcode 99001, easy to find.")

    def test_leak_scan_coords_in_body(self) -> None:
        self._assert_leak_blocks(HOTEL, body="Meet at 19.41290, -99.17130 please.")

    def test_leak_scan_coords_as_popular_place(self) -> None:
        self._assert_leak_blocks(HOTEL + textwrap.dedent(f"""\
            popular:
              - {{id: lobby, name: Lobby bar, category: food, lat: {HOTEL_LAT}, lon: {HOTEL_LON}, default_on: true}}
            """))

    def test_leak_scan_no_false_positive_on_shared_latitude(self) -> None:
        rc, _, err = self.build(HOTEL + textwrap.dedent(f"""\
            popular:
              - {{id: far, name: Far place, category: sight, lat: {HOTEL_LAT}, lon: -99.1300, default_on: true}}
            """))
        self.assertEqual(rc, 0, err)

    def test_warning_when_popular_name_contains_stay_name(self) -> None:
        _, p = self.load_data(HOTEL + textwrap.dedent("""\
            popular:
              - {id: sib, name: Secret Inn Rooftop, category: food, lat: 19.40, lon: -99.16, default_on: true}
            """))
        self.assertTrue(any("Secret Inn" in w for w in p.warnings))

    def test_scan_unit(self) -> None:
        stay = {"name": "Casa Uno", "address": "12 Calle Falsa, 06700 Ciudad", "url": None,
                "lat": 19.4128960, "lon": -99.1713001}
        self.assertEqual(build.scan_for_leaks("<p>nothing</p>", [stay]), [])
        self.assertTrue(build.scan_for_leaks("<p>CALLE FALSA</p>", [stay]))
        self.assertTrue(build.scan_for_leaks('"lat": 19.412896,\n "lon": -99.1713001', [stay]))
        self.assertTrue(build.scan_for_leaks("query=19.4129,-99.1713", [stay]))
        self.assertEqual(build.scan_for_leaks("code 067001 and 19.4129 alone", [stay]), [])


class TestSiteOutput(BuildCase):
    def test_headers_and_favicon(self) -> None:
        rc, page, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertEqual((self.tmp / "dist" / "_headers").read_text(), "/*\n  X-Robots-Tag: noindex, nofollow\n")
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', page)
        self.assertIn('<link rel="icon"', (self.tmp / "dist" / "index.html").read_text())

    def test_stale_slug_removed_unknown_file_refused(self) -> None:
        dist = self.tmp / "dist"
        (dist / "old-trip-0001").mkdir(parents=True)
        (dist / "old-trip-0001" / "index.html").write_text(OLD_PAGE)
        rc, _, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(x.name for x in dist.iterdir()),  # also: no temp files left behind
                         ["404.html", "_headers", "index.html", "robots.txt", SLUG])
        (dist / "notes.txt").write_text("stray")
        rc, _, err = self.build(HOTEL)
        self.assertEqual(rc, 1)
        self.assertIn("notes.txt was not written by this tool", err)
        self.assertTrue((dist / "notes.txt").exists())  # reported, never deleted

    def test_two_trips_one_run_and_duplicate_slug(self) -> None:
        a, b = self.tmp / "a.md", self.tmp / "b.md"
        for path, slug in ((a, "trip-aaaa"), (b, "trip-bbbb")):
            path.write_text("---\n" + BASE.format(slug=slug, end="2026-10-24", display="approximate")
                            + textwrap.dedent(HOTEL) + "---\n", encoding="utf-8")
        out = self.tmp / "multi"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = build.main([str(a), str(b), "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "trip-aaaa" / "index.html").exists())
        self.assertTrue((out / "trip-bbbb" / "index.html").exists())
        b.write_text(b.read_text().replace("trip-bbbb", "trip-aaaa"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = build.main([str(a), str(b), "--out", str(out)])
        self.assertEqual(rc, 1)
        self.assertIn("is also used by", err.getvalue())

    def test_dry_run_reports_stale_without_removing(self) -> None:
        dist = self.tmp / "dist"
        (dist / "old-trip-0001").mkdir(parents=True)
        (dist / "old-trip-0001" / "index.html").write_text(OLD_PAGE)
        trip = self.tmp / "trip.md"
        trip.write_text("---\n" + BASE.format(slug=SLUG, end="2026-10-24", display="approximate")
                        + textwrap.dedent(HOTEL) + "---\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = build.main([str(trip), "--out", str(dist), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("would REMOVE stale trip folder(s) not in this run: old-trip-0001", out.getvalue())
        self.assertTrue((dist / "old-trip-0001" / "index.html").exists())


class TestOutputSafety(BuildCase):
    """Round 3: M3 (only marked folders are pruned) and L1 (never write through a symlink)."""

    def test_pages_carry_generator_marker(self) -> None:
        rc, page, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertIn(build.GENERATOR_META, page)
        self.assertIn(build.GENERATOR_META, (self.tmp / "dist" / "index.html").read_text())

    def test_unmarked_stale_folder_is_listed_not_deleted(self) -> None:
        dist = self.tmp / "dist"
        (dist / "old-trip-0001").mkdir(parents=True)
        (dist / "old-trip-0001" / "index.html").write_text("<h1>hand-made</h1>")
        rc, _, err = self.build(HOTEL)
        self.assertEqual(rc, 1)
        self.assertIn("old-trip-0001 has no tripsite marker", err)
        self.assertEqual((dist / "old-trip-0001" / "index.html").read_text(), "<h1>hand-made</h1>")
        self.assertFalse((dist / SLUG).exists())  # nothing written either

    def test_marked_stale_folder_is_pruned(self) -> None:
        dist = self.tmp / "dist"
        (dist / "old-trip-0001").mkdir(parents=True)
        (dist / "old-trip-0001" / "index.html").write_text(OLD_PAGE)
        rc, _, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertFalse((dist / "old-trip-0001").exists())

    def test_current_slug_unmarked_page_is_overwritten(self) -> None:
        # a page from before the marker existed, same slug: rebuilt normally
        dist = self.tmp / "dist"
        (dist / SLUG).mkdir(parents=True)
        (dist / SLUG / "index.html").write_text("pre-marker build")
        rc, page, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertIn(build.GENERATOR_META, page)

    def test_symlinked_site_files_are_refused(self) -> None:
        for name in build.SITE_FILES:
            with self.subTest(name=name):
                dist = self.tmp / "dist"
                if dist.exists():
                    for f in sorted(dist.rglob("*"), reverse=True):
                        f.unlink() if (f.is_symlink() or f.is_file()) else f.rmdir()
                dist.mkdir(exist_ok=True)
                precious = self.tmp / f"precious-{name}"
                precious.write_text("PRECIOUS")
                (dist / name).symlink_to(precious)
                rc, _, err = self.build(HOTEL)
                self.assertEqual(rc, 1)
                self.assertIn(f"{name} is a symlink", err)
                self.assertEqual(precious.read_text(), "PRECIOUS")
                self.assertTrue((dist / name).is_symlink())

    def test_symlinked_stale_folder_contents_untouched(self) -> None:
        dist = self.tmp / "dist"
        (dist / "old-trip-0001").mkdir(parents=True)
        precious = self.tmp / "precious.html"
        precious.write_text(OLD_PAGE)  # even a marked target is never followed
        (dist / "old-trip-0001" / "index.html").symlink_to(precious)
        rc, _, _ = self.build(HOTEL)
        self.assertEqual(rc, 1)
        self.assertEqual(precious.read_text(), OLD_PAGE)

    def test_write_file_refuses_symlink(self) -> None:
        precious = self.tmp / "precious.txt"
        precious.write_text("PRECIOUS")
        link = self.tmp / "link.txt"
        link.symlink_to(precious)
        with self.assertRaises(OSError):
            build.write_file(link, "overwritten")
        self.assertEqual(precious.read_text(), "PRECIOUS")
        self.assertTrue(link.is_symlink())
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir()), ["link.txt", "precious.txt"])

    def test_generated_pages_pass_the_marker_check(self) -> None:
        # The trip page's </head> sits past the 4096-byte read (long inline CSS); the marker
        # must still be found, or a later run could never prune this trip.
        rc, page, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        self.assertGreater(page.index("</head>"), 4096)
        dist = self.tmp / "dist"
        for f in (dist / SLUG / "index.html", dist / "index.html", dist / "404.html"):
            self.assertTrue(build.has_marker(f), f.name)

    def test_empty_stale_folder_says_no_index_html(self) -> None:
        for extra in ((), (".DS_Store",)):
            with self.subTest(extra=extra):
                dist = self.tmp / "dist"
                stale = dist / "old-trip-0001"
                stale.mkdir(parents=True, exist_ok=True)
                for name in extra:
                    (stale / name).write_text("")
                rc, _, err = self.build(HOTEL)
                self.assertEqual(rc, 1)
                self.assertIn("old-trip-0001 has no index.html", err)
                self.assertNotIn("tripsite marker", err)
                self.assertTrue(stale.is_dir())


class TestNotFoundPage(BuildCase):
    """dist/404.html: without it, Cloudflare Pages serves index.html with 200 for any path."""

    def test_404_page_is_written_and_neutral(self) -> None:
        rc, _, _ = self.build(HOTEL)
        self.assertEqual(rc, 0)
        page = (self.tmp / "dist" / "404.html").read_text(encoding="utf-8")
        self.assertIn(build.GENERATOR_META, page)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', page)
        self.assertIn("<title>Not found</title>", page)
        self.assertIn("#F5EDE4", page)  # same look as the root placeholder
        for s in (SLUG, "trip-data", "Secret Inn"):
            self.assertNotIn(s, page)

    def test_404_is_a_protected_site_file(self) -> None:
        self.assertIn("404.html", build.SITE_FILES)
        rc, _, _ = self.build(HOTEL)  # a second run keeps it: not flagged, not pruned
        rc, _, err = self.build(HOTEL)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.tmp / "dist" / "404.html").is_file())


AUNTS = """\
  - {id: aunts, name: Aunt Flat, category: visit, private: true, address: 5 Quiet Road,
     lat: 19.3600, lon: -99.1800}
"""


def legend_of(page: str) -> str:
    """The map legend's HTML ('' if the page has none)."""
    if '<span class="legend">' not in page:
        return ""
    return page.split('<span class="legend">', 1)[1].split("</span>\n", 1)[0] + "</span>"


class TestLabelsAndLegend(BuildCase):
    """Round 3: L4 (per-place labels), L5 (legend only for drawn groups), L3 (warning)."""

    def test_private_non_stay_gets_its_own_label(self) -> None:
        rc, page, _ = self.build(HOTEL + AUNTS)
        self.assertEqual(rc, 0)
        self.assertIn("Where we’re staying (approximate area)", page)
        self.assertIn("Private place (approximate area)", page)
        self.assertIn('"label": "Private place"', page)
        self.assertIn('"label": "Where we’re staying"', page)
        for s in ("Aunt Flat", "Quiet Road", '"visit"', "aunts"):
            self.assertNotIn(s, page)
        legend = legend_of(page)
        self.assertIn("Where we’re staying (approximate)", legend)
        self.assertIn("Private place (approx.)", legend)

    def test_two_stays_are_numbered(self) -> None:
        second = AUNTS.replace("aunts", "inn2").replace("Aunt Flat", "Other Inn").replace(
            "category: visit, private: true", "category: hotel")
        rc, page, _ = self.build(HOTEL + second)
        self.assertEqual(rc, 0)
        self.assertIn("Where we’re staying 1 (approximate area)", page)
        self.assertIn("Where we’re staying 2 (approximate area)", page)
        self.assertNotIn("Private place (", page)  # no list entry or legend line
        self.assertNotIn('"label": "Private place', page)

    def test_legend_lists_only_drawn_groups(self) -> None:
        rc, page, _ = self.build(HOTEL)  # only an approximate stay
        self.assertEqual(rc, 0)
        legend = legend_of(page)
        self.assertIn("dot--stay", legend)
        self.assertNotIn("dot--ours", legend)
        self.assertNotIn("dot--popular", legend)
        self.assertNotIn("Private place", legend)
        rc, page, _ = self.build(textwrap.dedent("""\
            locations:
              - {id: bar, name: Bar, category: food, address: x, lat: 19.40, lon: -99.10}
            popular:
              - {id: z, name: Z, category: sight, lat: 19.43, lon: -99.13, default_on: false}
            """))
        legend = legend_of(page)
        self.assertIn("dot--ours", legend)
        self.assertIn("dot--popular", legend)
        self.assertNotIn("dot--stay", legend)
        rc, page, _ = self.build("locations: []\n")
        self.assertEqual(legend_of(page), "")

    def test_stay_swatch_has_light_ring_for_dark_themes(self) -> None:
        css = build.PAGE_CSS.split(".dot--stay {", 1)[1].split("}", 1)[0]
        self.assertIn("dashed", css)
        self.assertIn("box-shadow: 0 0 0 2px var(--mk-outline)", css)

    def test_popular_within_100m_of_stay_warns(self) -> None:
        near = textwrap.dedent("""\
            popular:
              - {id: kiosk, name: Kiosk, category: food, lat: 19.4133, lon: -99.1716, default_on: true}
            """)
        rc, _, err = self.build(HOTEL + near)
        self.assertEqual(rc, 0, err)  # a warning, not a failure
        self.assertIn("popular 'kiosk'", err)
        self.assertIn("< 100 m", err)
        far = near.replace("19.4133", "19.4150")  # ~240 m
        rc, _, err = self.build(HOTEL + far)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("popular 'kiosk'", err)

    def test_popular_near_stay_no_warning_when_exact(self) -> None:
        near = "popular:\n  - {id: kiosk, name: Kiosk, category: food, lat: 19.4133, lon: -99.1716, default_on: true}\n"
        _, p = self.load_data(HOTEL + near, display="exact")
        self.assertFalse(any("kiosk" in w for w in p.warnings))


class TestMarkdown(unittest.TestCase):
    def test_link_url_not_emphasised(self) -> None:
        out = build.markdown_to_html("See [the *map*](https://ex.com/a_b_c/*x*/d_e) and _this_.")
        self.assertIn('href="https://ex.com/a_b_c/*x*/d_e"', out)
        self.assertIn("<em>map</em>", out)
        self.assertIn("<em>this</em>", out)

    def test_emphasis_is_linear(self) -> None:
        import time
        for line in ("*" * 24000, "_" * 24000, "* a" * 8000, "**" + "a" * 24000):
            t0 = time.perf_counter()
            build.markdown_to_html(line)
            self.assertLess(time.perf_counter() - t0, 0.5, line[:10])

    def test_basic_emphasis_still_works(self) -> None:
        out = build.markdown_to_html("**bold** and *it* and snake_case_word")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>it</em>", out)
        self.assertIn("snake_case_word", out)


if __name__ == "__main__":
    unittest.main()
