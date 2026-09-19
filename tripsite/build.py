"""tripsite v0: build a static, shareable trip website from trip profiles.

Usage:
    uv run tripsite/build.py trips/*.md                 # every live trip, in ONE run
    uv run tripsite/build.py trips/<trip>.md --dry-run  # validate only, write nothing
    uv run tripsite/build.py trips/<trip>.md --out /some/other/dir

The trip file is Markdown with YAML frontmatter; see tripsite/README.md for the schema.
Output: dist/<slug>/index.html per trip (single self-contained page), dist/index.html
(neutral placeholder, no trip listing), dist/404.html (neutral "Not found"; without it
Cloudflare Pages serves index.html with 200 for every missing path), dist/robots.txt
(disallow all) and dist/_headers (X-Robots-Tag). A Cloudflare Pages direct upload replaces
the whole site, so dist/ is kept to exactly the trips passed on this run: any other trip
folder is removed, but only if its index.html carries this tool's generator marker in its
<head>. Outputs are written via a temp file + os.replace. Symlinks are never followed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment
    sys.exit(
        "error: PyYAML is not installed.\n"
        "Run the build with uv (it installs project dependencies):\n"
        "    uv run tripsite/build.py trips/<trip>.md"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
THEMES_DIR = REPO_ROOT / "themes"
DEFAULT_OUT = REPO_ROOT / "dist"

# Leaflet 1.9.4 from unpkg. SRI hashes computed locally from the unpkg files on
# 2026-09-18; they match the hashes published on leafletjs.com/download.html.
LEAFLET_CSS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
THEME_RE = re.compile(r"^[a-z0-9_]+$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
HEX_COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")

HOTEL_DISPLAY_CHOICES = ("exact", "approximate", "hidden")
EVENT_KINDS = ("plan", "city")
STAY_CATEGORY = "stay"
# A place is a "stay" (private, handled per privacy.hotel_display) if its category is
# one of these (case-insensitive) OR it has `private: true`.
STAY_CATEGORIES = frozenset({"stay", "hotel", "lodging", "airbnb", "accommodation", "hostel"})
# approximate: the circle centre is moved OFFSET_MIN_M..OFFSET_MAX_M from the stay, on a
# bearing kept 20-70 degrees off north/south/east/west (so the centre's lat AND lon both
# differ from the stay's at 4 dp). The radius is fixed, so it says nothing about the offset.
OFFSET_MIN_M, OFFSET_MAX_M = 150, 250
APPROX_RADIUS_M = 400  # >= OFFSET_MAX_M + 150: the stay is inside, never near the centre
NEAR_STAY_M = 400      # an event's inline {lat, lon} this close to a stay is treated as the stay
POPULAR_NEAR_STAY_M = 100  # a popular pin this close to a private stay gets a warning (not an error)
APPROX_MAX_ZOOM = 14   # the page never zooms closer than this onto an approximate area

# Fixed, tile-safe marker palette (the theme colours only style the page chrome;
# several themes vanish on OSM tiles, e.g. noir's white/black).
MARKER_COLOURS = {
    "ours": "#C62828",        # strong red, white outline
    "popular": "#1565C0",     # strong blue, white outline
    "event": "#00695C",       # dark teal, white outline
    "stay": "#7B1FA2",        # purple fill, semi-transparent
    "stay-edge": "#2A0A3A",   # dark outline for the stay area
    "outline": "#FFFFFF",
}
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
           "%3Ccircle cx='8' cy='8' r='6' fill='%23C62828' stroke='white' stroke-width='2'/%3E%3C/svg%3E")


# --------------------------------------------------------------------------- #
# YAML loading: duplicate keys are errors; base-60 ints remember their text
# --------------------------------------------------------------------------- #


class Sexagesimal(int):
    """An int YAML 1.1 read from a colon form like 12:00 (= 720). Keeps the source text so
    an unquoted time can be recovered, while a bare 930 stays a plain int and is rejected."""

    text: str = ""


class TripLoader(yaml.SafeLoader):
    def construct_mapping(self, node: Any, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen: dict[Any, int] = {}
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    first = seen.get(key)
                except TypeError:  # unhashable key: SafeLoader reports it below
                    continue
                if first is not None:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found duplicate key {key!r} (first used on line {first + 1})",
                        key_node.start_mark)
                seen[key] = key_node.start_mark.line
        return super().construct_mapping(node, deep=deep)

    def construct_yaml_int(self, node: Any) -> int:
        value = super().construct_yaml_int(node)
        text = str(self.construct_scalar(node)).strip()
        if ":" in text:
            s = Sexagesimal(value)
            s.text = text
            return s
        return value


TripLoader.add_constructor("tag:yaml.org,2002:int", TripLoader.construct_yaml_int)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


@dataclass
class Problems:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


_MISSING = object()


def get(mapping: Any, key: str, where: str, p: Problems, required: bool = True) -> Any:
    """Return mapping[key]; record an error if it is required and missing/empty."""
    if not isinstance(mapping, dict):
        p.err(where, "expected a mapping (key: value pairs)")
        return None
    value = mapping.get(key, _MISSING)
    if value is _MISSING or value is None or value == "":
        if required:
            p.err(where, f"missing required field '{key}'")
        return None
    return value


def as_text(value: Any, where: str, p: Problems) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        p.err(where, "expected text, got bool (YAML reads unquoted yes/no/on/off/true/false as "
                     "true/false): quote it, e.g. \"NO\"")
        return None
    if not isinstance(value, (str, int, float)):
        p.err(where, f"expected text, got {type(value).__name__}")
        return None
    text = value.text if isinstance(value, Sexagesimal) else str(value).strip()
    return text or None


def as_date(value: Any, where: str, p: Problems) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    p.err(where, f"bad date {value!r} (use YYYY-MM-DD, e.g. 2026-10-22)")
    return None


def as_time(value: Any, where: str, p: Problems) -> str | None:
    """Accept "HH:MM" strings. Unquoted 12:00 is read by YAML 1.1 as the base-60
    integer 720; TripLoader keeps its source text, so recover it from that. A bare
    integer (930, 12) is ambiguous and rejected."""
    if value is None:
        return None
    text = value.text if isinstance(value, Sexagesimal) else value
    if isinstance(text, str):
        m = TIME_RE.match(text.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    if isinstance(value, int) and not isinstance(value, (bool, Sexagesimal)):
        p.err(where, f"bad time {value!r}: a bare number is ambiguous; write it as a quoted "
                     f"\"HH:MM\", e.g. time: \"09:30\"")
        return None
    p.err(where, f"bad time {text!r} (use quoted \"HH:MM\", e.g. \"12:00\")")
    return None


def as_id(value: Any, where: str, p: Problems) -> str | None:
    """Ids are compared as text, so `id: 1` and `location: 1` match."""
    if isinstance(value, int) and not isinstance(value, (bool, Sexagesimal)):
        return str(value)
    return as_text(value, where, p)


def as_coord(value: Any, where: str, p: Problems, lo: float, hi: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        p.err(where, f"expected a number, got {value!r}")
        return None
    if not lo <= float(value) <= hi:
        p.err(where, f"{value} is out of range ({lo}..{hi})")
        return None
    return float(value)


def as_bool(value: Any, where: str, p: Problems) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        p.err(where, f"expected true or false, got {value!r}")
        return None
    return value


def as_url(value: Any, where: str, p: Problems) -> str | None:
    text = as_text(value, where, p)
    if text is None:
        return None
    if not re.match(r"^https?://[^\s\"'<>]+$", text):
        p.err(where, f"URL must start with http:// or https:// and contain no spaces: {text!r}")
        return None
    return text


def as_list(value: Any, where: str, p: Problems) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        p.err(where, "expected a list (lines starting with '- ')")
        return []
    return value


# --------------------------------------------------------------------------- #
# Loading and validating a trip file
# --------------------------------------------------------------------------- #


def split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    lines = text.lstrip("﻿").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"error: {path}: file must start with a '---' line (YAML frontmatter)")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    raise SystemExit(f"error: {path}: no closing '---' line after the YAML frontmatter")


def load_trip(path: Path) -> tuple[dict, str, Problems]:
    """Parse and validate a trip file. Returns (normalised data, markdown body, problems)."""
    if not path.is_file():
        raise SystemExit(f"error: trip file not found: {path}")
    front, body = split_frontmatter(path.read_text(encoding="utf-8"), path)
    try:
        raw = yaml.load(front, Loader=TripLoader)  # TripLoader subclasses SafeLoader
    except yaml.YAMLError as exc:
        raise SystemExit(f"error: {path}: frontmatter is not valid YAML:\n{exc}")
    except ValueError as exc:  # e.g. 2026-10-32 matches YAML's date pattern but isn't a date
        raise SystemExit(f"error: {path}: bad date in frontmatter ({exc}); use real YYYY-MM-DD dates")
    if not isinstance(raw, dict):
        raise SystemExit(f"error: {path}: frontmatter must be a mapping with 'trip:', 'locations:' etc.")

    p = Problems()
    known_top = {"trip", "privacy", "locations", "popular", "events"}
    for key in raw:
        if key not in known_top:
            p.warn("frontmatter", f"unknown top-level key '{key}' ignored")

    trip = _validate_trip(raw.get("trip"), p)
    privacy = _validate_privacy(raw.get("privacy"), p)
    seen_ids: dict[str, str] = {}
    ours = [_validate_place(item, f"locations[{i}]", p, seen_ids, popular=False)
            for i, item in enumerate(as_list(raw.get("locations"), "locations", p))]
    popular = [_validate_place(item, f"popular[{i}]", p, seen_ids, popular=True)
               for i, item in enumerate(as_list(raw.get("popular"), "popular", p))]
    ours = [x for x in ours if x]
    popular = [x for x in popular if x]
    events = [_validate_event(item, f"events[{i}]", p, seen_ids)
              for i, item in enumerate(as_list(raw.get("events"), "events", p))]
    events = [x for x in events if x]

    data = {"trip": trip, "privacy": privacy, "ours": ours, "popular": popular, "events": events}
    return data, body, p


def _validate_trip(raw: Any, p: Problems) -> dict:
    w = "trip"
    if raw is None:
        p.err(w, "missing required section 'trip:'")
        return {}
    t: dict[str, Any] = {}
    for key in ("name", "city", "country"):
        t[key] = as_text(get(raw, key, w, p), f"{w}.{key}", p)

    slug = as_text(get(raw, "slug", w, p), f"{w}.slug", p)
    if slug and not SLUG_RE.match(slug):
        p.err(f"{w}.slug", f"{slug!r} must be 3-80 chars of lowercase letters, digits and hyphens")
        slug = None
    t["slug"] = slug

    t["start"] = as_date(get(raw, "start", w, p), f"{w}.start", p)
    t["end"] = as_date(get(raw, "end", w, p), f"{w}.end", p)
    if t["start"] and t["end"] and t["end"] < t["start"]:
        p.err(w, f"end ({t['end']}) is before start ({t['start']})")

    tz = as_text(get(raw, "timezone", w, p), f"{w}.timezone", p)
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            p.err(f"{w}.timezone", f"unknown timezone {tz!r} (use an IANA name like America/Mexico_City)")
    t["timezone"] = tz

    center = get(raw, "center", w, p)
    if center is not None:
        t["center"] = {
            "lat": as_coord(get(center, "lat", f"{w}.center", p), f"{w}.center.lat", p, -90, 90),
            "lon": as_coord(get(center, "lon", f"{w}.center", p), f"{w}.center.lon", p, -180, 180),
        }

    zoom = get(raw, "zoom", w, p)
    if zoom is not None and (isinstance(zoom, (bool, Sexagesimal)) or not isinstance(zoom, int)
                             or not 1 <= zoom <= 19):
        p.err(f"{w}.zoom", f"expected a whole number 1..19, got {zoom!r}")
        zoom = None
    t["zoom"] = zoom

    theme = as_text(get(raw, "theme", w, p), f"{w}.theme", p)
    if theme and (not THEME_RE.match(theme) or not (THEMES_DIR / f"{theme}.json").is_file()):
        names = ", ".join(sorted(f.stem for f in THEMES_DIR.glob("*.json")))
        p.err(f"{w}.theme", f"no theme {theme!r} in themes/ (available: {names})")
        theme = None
    t["theme"] = theme
    return t


def _validate_privacy(raw: Any, p: Problems) -> dict:
    if raw is None:
        p.warn("privacy", "section missing; defaulting to hotel_display: approximate, noindex: true")
        return {"hotel_display": "approximate", "noindex": True}
    display = as_text(get(raw, "hotel_display", "privacy", p), "privacy.hotel_display", p)
    if display and display not in HOTEL_DISPLAY_CHOICES:
        p.err("privacy.hotel_display", f"{display!r} must be one of {', '.join(HOTEL_DISPLAY_CHOICES)}")
    noindex = as_bool(get(raw, "noindex", "privacy", p, required=False), "privacy.noindex", p)
    if noindex is False:
        p.warn("privacy.noindex", "false is ignored: every generated page is always noindex,nofollow")
    return {"hotel_display": display, "noindex": True}


def _validate_place(raw: Any, w: str, p: Problems, seen_ids: dict[str, str],
                    popular: bool) -> dict | None:
    if not isinstance(raw, dict):
        p.err(w, "expected a mapping with id, name, category, lat, lon ...")
        return None
    place: dict[str, Any] = {"group": "popular" if popular else "ours"}
    pid = as_id(get(raw, "id", w, p), f"{w}.id", p)
    if pid:
        if not ID_RE.match(pid):
            p.err(f"{w}.id", f"{pid!r} may only use letters, digits, '-' and '_'")
        elif pid in seen_ids:
            p.err(f"{w}.id", f"duplicate id {pid!r} (also used at {seen_ids[pid]})")
        else:
            seen_ids[pid] = w
    place["id"] = pid
    place["name"] = as_text(get(raw, "name", w, p), f"{w}.name", p)
    category = as_text(get(raw, "category", w, p), f"{w}.category", p)
    place["category"] = category.lower() if category else None
    place["lat"] = as_coord(get(raw, "lat", w, p), f"{w}.lat", p, -90, 90)
    place["lon"] = as_coord(get(raw, "lon", w, p), f"{w}.lon", p, -180, 180)
    private = as_bool(get(raw, "private", w, p, required=False), f"{w}.private", p)
    place["stay"] = bool(private) or (place["category"] or "") in STAY_CATEGORIES
    if popular and place["stay"]:
        why = "private: true" if private else f"category {place['category']!r} is a stay category"
        p.err(w, f"{why}; put stays under locations (only there are they kept private)")
    if popular:
        place["default_on"] = as_bool(get(raw, "default_on", w, p), f"{w}.default_on", p)
        place["address"] = as_text(get(raw, "address", w, p, required=False), f"{w}.address", p)
    else:
        place["address"] = as_text(get(raw, "address", w, p), f"{w}.address", p)
    place["notes"] = as_text(get(raw, "notes", w, p, required=False), f"{w}.notes", p)
    place["url"] = as_url(get(raw, "url", w, p, required=False), f"{w}.url", p)
    return place


def _validate_event(raw: Any, w: str, p: Problems, seen_ids: dict[str, str]) -> dict | None:
    if not isinstance(raw, dict):
        p.err(w, "expected a mapping with date, kind, name ...")
        return None
    ev: dict[str, Any] = {}
    ev["date"] = as_date(get(raw, "date", w, p), f"{w}.date", p)
    ev["time"] = as_time(get(raw, "time", w, p, required=False), f"{w}.time", p)
    kind = as_text(get(raw, "kind", w, p), f"{w}.kind", p)
    if kind and kind not in EVENT_KINDS:
        p.err(f"{w}.kind", f"{kind!r} must be one of {', '.join(EVENT_KINDS)}")
    ev["kind"] = kind
    ev["name"] = as_text(get(raw, "name", w, p), f"{w}.name", p)
    ev["booked"] = as_bool(get(raw, "booked", w, p, required=False), f"{w}.booked", p)
    ev["source"] = as_url(get(raw, "source", w, p, required=False), f"{w}.source", p)
    ev["notes"] = as_text(get(raw, "notes", w, p, required=False), f"{w}.notes", p)

    loc = raw.get("location")
    if isinstance(loc, int) and not isinstance(loc, (bool, Sexagesimal)):
        loc = str(loc)
    if loc is None:
        ev["loc"] = None
    elif isinstance(loc, str):
        if loc.strip() not in seen_ids:
            p.err(f"{w}.location", f"unknown place id {loc!r} (must match an id in locations/popular)")
        ev["loc"] = {"ref": loc.strip()}
    elif isinstance(loc, dict):
        lw = f"{w}.location"
        ev["loc"] = {
            "lat": as_coord(get(loc, "lat", lw, p), f"{lw}.lat", p, -90, 90),
            "lon": as_coord(get(loc, "lon", lw, p), f"{lw}.lon", p, -180, 180),
            "label": as_text(get(loc, "label", lw, p, required=False), f"{lw}.label", p) or ev["name"],
        }
    else:
        p.err(f"{w}.location", "expected a place id or {lat, lon, label}")
        ev["loc"] = None
    return ev


# --------------------------------------------------------------------------- #
# Privacy and event filtering
# --------------------------------------------------------------------------- #


def metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(min(1.0, h)))


def offset_centre(slug: str, place: dict) -> tuple[float, float, float]:
    """Deterministic (lat, lon, offset_m) for an approximate stay's circle centre.

    Seeded by the slug, id AND the private fields (name, exact coords): with the id
    alone (often guessable, e.g. "hotel") anyone could recompute the offset from the
    public page and subtract it. Same inputs -> same circle on every rebuild."""
    seed = "|".join([slug, place["id"], place.get("name") or "", repr(place["lat"]), repr(place["lon"])])
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    u1 = int.from_bytes(h[0:4], "big") / 2 ** 32
    u2 = int.from_bytes(h[4:8], "big") / 2 ** 32
    u3 = int.from_bytes(h[8:9], "big") % 4
    bearing = math.radians(u3 * 90 + 20 + u1 * 50)  # 20-70 deg into a random quadrant
    # 1 m inside the band so rounding the centre to 5 dp (< 1 m) can't push it outside
    dist = OFFSET_MIN_M + 1 + u2 * (OFFSET_MAX_M - OFFSET_MIN_M - 2)
    # destination point on a sphere
    r = 6371000.0
    la1, lo1 = math.radians(place["lat"]), math.radians(place["lon"])
    ang = dist / r
    la2 = math.asin(math.sin(la1) * math.cos(ang) + math.cos(la1) * math.sin(ang) * math.cos(bearing))
    lo2 = lo1 + math.atan2(math.sin(bearing) * math.sin(ang) * math.cos(la1),
                           math.cos(ang) - math.sin(la1) * math.sin(la2))
    lat, lon = round(math.degrees(la2), 5), round((math.degrees(lo2) + 540) % 360 - 180, 5)
    return lat, lon, metres_between(lat, lon, place["lat"], place["lon"])


def apply_privacy(data: dict, body: str, p: Problems) -> None:
    """Rewrite stay locations per privacy.hotel_display so exact details never reach the page.

    Stays are places under `locations` whose category is in STAY_CATEGORIES or that have
    `private: true` (stays under `popular` are rejected during validation). The original
    stay records are kept in data["_private_stays"] for the post-render leak scan."""
    display = data["privacy"]["hotel_display"]
    slug = data["trip"]["slug"]
    kept: list[dict] = []
    hidden_ids: set[str] = set()
    renamed: dict[str, str] = {}  # the id often names the hotel, so it is replaced too
    private_stays: list[dict] = []
    # Per-place public label: lodging reads "Where we're staying"; any other private place
    # (e.g. a relative's flat marked private: true) reads "Private place". Numbered if repeated.
    kind_total: dict[str, int] = {}
    if display == "approximate":
        for place in data["ours"]:
            if place.get("stay"):
                kind = _approx_kind(place)
                kind_total[kind] = kind_total.get(kind, 0) + 1
    kind_seen: dict[str, int] = {}
    for place in data["ours"]:
        if not place.get("stay") or display == "exact":
            kept.append(place)
            continue
        private_stays.append(place)
        _warn_if_leaked(place, body, data, p)
        _warn_if_popular_near(place, data, p)
        if display == "hidden":
            hidden_ids.add(place["id"])
            continue
        # approximate: a fixed-radius circle whose centre is offset 150-250 m from the stay.
        # id is made opaque; name, address, notes, url and the real category are dropped.
        opaque_id = f"_stay{len(renamed) + 1}"  # leading '_' can't collide: user ids start alphanumeric
        renamed[place["id"]] = opaque_id
        lat, lon, _ = offset_centre(slug, place)
        kind = _approx_kind(place)
        kind_seen[kind] = kind_seen.get(kind, 0) + 1
        label = APPROX_LABELS[kind]
        if kind_total[kind] > 1:
            label = f"{label} {kind_seen[kind]}"
        kept.append({
            "group": "ours", "id": opaque_id, "category": STAY_CATEGORY, "approx": True,
            "radius": APPROX_RADIUS_M, "lat": lat, "lon": lon, "label": label, "approx_kind": kind,
        })
    data["ours"] = kept
    data["_private_stays"] = private_stays
    for ev in data["events"]:
        loc = ev["loc"]
        ref = loc.get("ref") if loc else None
        if loc and ref is None and loc.get("lat") is not None:
            near = _nearest_stay(loc, private_stays)
            if near is not None:
                stay, dist = near
                p.warn(f"events '{ev['name']}'",
                       f"inline location is {dist:.0f} m from a private stay (< {NEAR_STAY_M} m); "
                       f"its coordinates and label are not published"
                       + ("; it points at the approximate stay area instead" if display == "approximate"
                          else "; the event is kept without a map link")
                       + ". Use a place id if it is really somewhere else.")
                ref = stay["id"]
        if ref in hidden_ids:
            ev["loc"] = None  # keep the event, lose the map link
        elif ref in renamed:
            ev["loc"] = {"ref": renamed[ref]}


APPROX_LABELS = {"stay": "Where we’re staying", "private": "Private place"}


def _approx_kind(place: dict) -> str:
    """'stay' for lodging categories, 'private' for other places marked private: true."""
    return "stay" if (place.get("category") or "") in STAY_CATEGORIES else "private"


def _warn_if_popular_near(place: dict, data: dict, p: Problems) -> None:
    """A popular pin right next to a private stay shows roughly where it is. Warning only."""
    for other in data["popular"]:
        d = metres_between(place["lat"], place["lon"], other["lat"], other["lon"])
        if d < POPULAR_NEAR_STAY_M:
            p.warn(f"popular '{other['id']}'",
                   f"is {d:.0f} m from private place '{place['id']}' (< {POPULAR_NEAR_STAY_M} m), so its "
                   "pin shows roughly where that place is. Remove it if that matters.")


def _nearest_stay(loc: dict, stays: list[dict]) -> tuple[dict, float] | None:
    best = None
    for stay in stays:
        d = metres_between(loc["lat"], loc["lon"], stay["lat"], stay["lon"])
        if d < NEAR_STAY_M and (best is None or d < best[1]):
            best = (stay, d)
    return best


def _warn_if_leaked(place: dict, body: str, data: dict, p: Problems) -> None:
    texts = [body]
    for e in data["events"]:
        loc = e.get("loc") or {}
        texts.append(f"{e.get('name') or ''} {e.get('notes') or ''} {loc.get('label') or ''}")
    for other in data["ours"] + data["popular"]:
        if other is not place:
            texts.append(f"{other.get('name') or ''} {other.get('notes') or ''} {other.get('address') or ''}")
    haystack = " ".join(texts).casefold()
    for label in (place.get("name"), place.get("address")):
        if label and label.casefold() in haystack:
            p.warn(f"locations '{place['id']}'",
                   f"stay is not shown exactly, but {label!r} appears in the body, an event or another "
                   "place's name/notes/address; remove it there (the build fails if it reaches the page)")


_POSTCODE_RES = (
    re.compile(r"\b\d{4,6}\b"),                                    # 99001, 75001, 10115
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.IGNORECASE),    # UK: SW1A 1AA
    re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", re.IGNORECASE),             # CA: K1A 0B1
)


def _stay_fragments(stay: dict) -> list[str]:
    """Text that must not appear on the page for a non-exact stay."""
    frags = [stay.get("name"), stay.get("address"), stay.get("url")]
    address = stay.get("address") or ""
    parts = [s.strip() for s in address.split(",") if s.strip()]
    if parts:
        street = re.sub(r"\s+", " ", re.sub(r"\b\d+[A-Za-z]?\b", " ", parts[0])).strip(" -#")
        if len(street) >= 5:
            frags.append(street)
    for part in parts[1:]:  # postcodes: after the first comma, so a house number isn't one
        for rx in _POSTCODE_RES:
            frags.extend(m.group(0) for m in rx.finditer(part))
    return [f for f in frags if f]


def _coord_variants(x: float) -> set[str]:
    """4-dp rounded and truncated forms, unsigned. Any longer rendering (5+ dp) of the
    same value starts with one of these."""
    a = abs(x)
    return {f"{a:.4f}", f"{math.floor(a * 10 ** 4) / 10 ** 4:.4f}"}


def scan_for_leaks(page: str, stays: list[dict]) -> list[str]:
    """Final safety net: the rendered page must not contain a private stay's name, address
    fragments (street, postcode), url, or its coordinates at 4+ decimals (as a lat/lon pair)."""
    text = (page.replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">"))
    text = html.unescape(text)
    folded = text.casefold()
    hits = []
    for stay in stays:
        for frag in _stay_fragments(stay):
            f = frag.casefold()
            if re.fullmatch(r"[\w ]+", f):
                found = re.search(r"(?<!\w)" + re.escape(f) + r"(?!\w)", folded)
            else:
                found = f in folded
            if found:
                hits.append(f"private stay text {frag!r} appears on the page")
        lats, lons = _coord_variants(stay["lat"]), _coord_variants(stay["lon"])
        for m in re.finditer(r"(?<![\d.])(\d{1,3}\.\d{4,})", text):
            if not any(m.group(1).startswith(v) for v in lats | lons):
                continue
            is_lat = any(m.group(1).startswith(v) for v in lats)
            window = text[max(0, m.start() - 80): m.end() + 80]
            partner = lons if is_lat else lats
            if any(re.search(r"(?<![\d.])" + re.escape(v), window) for v in partner):
                hits.append(f"private stay coordinates (~{m.group(1)}) appear on the page")
                break
    return hits


def filter_events(data: dict, p: Problems) -> int:
    start, end = data["trip"]["start"], data["trip"]["end"]
    kept, dropped = [], 0
    for ev in data["events"]:
        if ev["date"] < start or ev["date"] > end:
            p.warn("events", f"dropped '{ev['name']}' on {ev['date']}: outside trip dates {start}..{end}")
            dropped += 1
        else:
            kept.append(ev)
    kept.sort(key=lambda e: (e["date"], e["time"] or ""))
    data["events"] = kept
    return dropped


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def load_theme(name: str) -> dict[str, str]:
    raw = json.loads((THEMES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if isinstance(v, str) and HEX_COLOUR_RE.match(v)}


def theme_css(theme: dict[str, str]) -> str:
    """Every hex colour in the theme becomes --theme-<key> (underscores -> hyphens)."""
    lines = [f"  --theme-{k.replace('_', '-')}: {v};" for k, v in sorted(theme.items())]
    return ":root {\n" + "\n".join(lines) + "\n}"


def pretty_category(cat: str) -> str:
    return cat.replace("_", " ").replace("-", " ").capitalize()


def markdown_to_html(md: str) -> str:
    """Deliberately tiny Markdown subset: headings, paragraphs, - / 1. lists,
    ``` code blocks, `code`, **bold**, *italic*, [text](http-url). Everything is
    HTML-escaped first; anything unsupported shows as plain text."""
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    in_code = False
    code: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                flush_para(); close_list()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped:
            flush_para(); close_list()
        elif heading:
            flush_para(); close_list()
            level = min(len(heading.group(1)) + 2, 6)  # page already uses h1/h2
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet or number:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append("<li>" + _inline((bullet or number).group(1)) + "</li>")
        else:
            close_list()
            para.append(stripped)
    if in_code:
        out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush_para(); close_list()
    return "\n".join(out)


LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
# Italic content can't contain the delimiter, so each attempt stops at the next '*'/'_':
# linear time even on a line made entirely of asterisks.
ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([^*\s](?:[^*]*[^*\s])?)\*(?![\w*])")
ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_([^_\s](?:[^_]*[^_\s])?)_(?![\w_])")


def _emphasis(s: str) -> str:
    s = BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = ITALIC_STAR_RE.sub(r"<em>\1</em>", s)
    return ITALIC_UNDER_RE.sub(r"<em>\1</em>", s)


def _inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    rendered = []
    for part in parts:
        if len(part) > 2 and part.startswith("`") and part.endswith("`"):
            rendered.append("<code>" + esc(part[1:-1]) + "</code>")
            continue
        s = esc(part)
        # Emphasis applies to the text around links and to link text, never to the URL.
        pos, out = 0, []
        for m in LINK_RE.finditer(s):
            out.append(_emphasis(s[pos:m.start()]))
            out.append(f'<a href="{m.group(2)}" rel="noopener noreferrer" target="_blank">'
                       f'{_emphasis(m.group(1))}</a>')
            pos = m.end()
        out.append(_emphasis(s[pos:]))
        rendered.append("".join(out))
    return "".join(rendered)


def fmt_day(d: dt.date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def fmt_range(start: dt.date, end: dt.date) -> str:
    days = (end - start).days + 1
    if start.year == end.year:
        text = f"{fmt_day(start)} – {fmt_day(end)} {end.year}"
    else:
        text = f"{fmt_day(start)} {start.year} – {fmt_day(end)} {end.year}"
    return f"{text} · {days} day{'s' if days != 1 else ''}"


def render_popular_panel(popular: list[dict]) -> str:
    if not popular:
        return '<p class="muted">No popular places listed.</p>'
    by_cat: dict[str, list[tuple[int, dict]]] = {}
    for i, place in enumerate(popular):
        by_cat.setdefault(place["category"], []).append((i, place))
    blocks = []
    for cat in sorted(by_cat):
        items = "\n".join(
            f'      <li><label><input type="checkbox" class="toggle-place" data-id="{esc(pl["id"])}"'
            f' data-category="{esc(cat)}"{" checked" if pl["default_on"] else ""}> {esc(pl["name"])}</label></li>'
            for _, pl in by_cat[cat]
        )
        blocks.append(
            f'  <fieldset class="cat">\n'
            f'    <legend><label><input type="checkbox" class="toggle-cat" data-category="{esc(cat)}">'
            f' {esc(pretty_category(cat))} <span class="muted">({len(by_cat[cat])})</span></label></legend>\n'
            f'    <ul>\n{items}\n    </ul>\n  </fieldset>'
        )
    return "\n".join(blocks)


def render_ours_list(ours: list[dict]) -> str:
    if not ours:
        return '<p class="muted">None yet.</p>'
    items = []
    for place in ours:
        label = f"{place['label']} (approximate area)" if place.get("approx") else place["name"]
        items.append(f'    <li><button type="button" class="linkish focus-place" data-id="{esc(place["id"])}">'
                     f'{esc(label)}</button></li>')
    return "  <ul class=\"ours\">\n" + "\n".join(items) + "\n  </ul>"


def render_events(data: dict) -> str:
    trip, events = data["trip"], data["events"]
    by_day: dict[dt.date, list[tuple[int, dict]]] = {}
    for i, ev in enumerate(events):
        by_day.setdefault(ev["date"], []).append((i, ev))
    days = []
    day = trip["start"]
    while day <= trip["end"]:
        entries = by_day.get(day, [])
        if entries:
            lis = "\n".join(_render_event(i, ev) for i, ev in entries)
            inner = f'      <ul class="events">\n{lis}\n      </ul>'
        else:
            inner = '      <p class="muted">Nothing scheduled.</p>'
        days.append(f'    <li class="day" id="day-{day.isoformat()}">\n'
                    f'      <h3>{esc(fmt_day(day))}</h3>\n{inner}\n    </li>')
        day += dt.timedelta(days=1)
    return '  <ol class="days">\n' + "\n".join(days) + "\n  </ol>"


def _render_event(i: int, ev: dict) -> str:
    badge = "Our plan" if ev["kind"] == "plan" else "City event"
    name = esc(ev["name"])
    if ev["loc"]:
        name = (f'<button type="button" class="linkish event-go" data-event="{i}" '
                f'title="Show on map">{name} <span aria-hidden="true">⌖</span></button>')
    bits = [f'<span class="badge">{badge}</span>',
            f'<span class="time">{esc(ev["time"] or "All day")}</span>',
            f'<span class="ev-name">{name}</span>']
    if ev["booked"] is True:
        bits.append('<span class="tag">Booked</span>')
    elif ev["booked"] is False:
        bits.append('<span class="tag tag--todo">Not booked</span>')
    extra = []
    if ev["notes"]:
        extra.append(f'<span class="notes">{esc(ev["notes"])}</span>')
    if ev["source"]:
        extra.append(f'<a class="source" href="{esc(ev["source"])}" rel="noopener noreferrer" '
                     f'target="_blank">Source</a>')
    extra_html = f'<div class="ev-extra">{" ".join(extra)}</div>' if extra else ""
    return (f'        <li class="event event--{esc(ev["kind"])}">'
            f'<div class="ev-main">{" ".join(bits)}</div>{extra_html}</li>')


PLACE_KEYS = ("group", "id", "name", "category", "lat", "lon", "default_on", "address", "notes", "url",
              "approx", "radius", "label")


def page_json(data: dict) -> str:
    """Trip data for the page script. Escaped so it can't close the <script> tag."""
    t = data["trip"]
    payload = {
        "trip": {"name": t["name"], "center": t["center"], "zoom": t["zoom"]},
        "maxApproxZoom": APPROX_MAX_ZOOM,
        "places": [{k: pl[k] for k in PLACE_KEYS if pl.get(k) is not None}
                   for pl in data["ours"] + data["popular"]],
        "events": [
            {"name": e["name"], "kind": e["kind"], "date": e["date"].isoformat(),
             "day": fmt_day(e["date"]), "time": e["time"], "loc": e["loc"]}
            for e in data["events"]
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


# Every page this tool writes carries this marker; only marked trip folders are ever pruned.
GENERATOR_META = '<meta name="generator" content="tripsite">'

HEAD_COMMON = """<meta charset="utf-8">
{GENERATOR_META}
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="strict-origin-when-cross-origin">
<link rel="icon" href="{FAVICON}">""".replace("{FAVICON}", FAVICON).replace("{GENERATOR_META}", GENERATOR_META)


def marker_css() -> str:
    lines = [f"  --mk-{k}: {v};" for k, v in MARKER_COLOURS.items()]
    return ":root {\n" + "\n".join(lines) + "\n}"


LEGEND_ENTRIES = (  # (css class, text); an entry is shown only if its group is on the page
    ("dot--ours", "Our places"),
    ("dot--popular", "Popular places"),
    ("dot--stay", "Where we’re staying (approximate)"),
    ("dot--stay", "Private place (approx.)"),
)


def render_legend(data: dict) -> str:
    approx_kinds = {pl.get("approx_kind") for pl in data["ours"] if pl.get("approx")}
    present = (any(not pl.get("approx") for pl in data["ours"]), bool(data["popular"]),
               "stay" in approx_kinds, "private" in approx_kinds)
    items = [f'<span class="dot {cls}"></span> {esc(text)}'
             for (cls, text), on in zip(LEGEND_ENTRIES, present) if on]
    if not items:
        return ""
    return '      <span class="legend">' + "\n      ".join(items) + "</span>"


def render_trip_page(data: dict, body: str, theme: dict[str, str]) -> str:
    t = data["trip"]
    about = markdown_to_html(body) if body.strip() else '<p class="muted">Nothing here yet.</p>'
    legend = render_legend(data)
    return f"""<!doctype html>
<html lang="en">
<head>
{HEAD_COMMON}
<title>{esc(t["name"])}</title>
<link rel="stylesheet" href="{LEAFLET_CSS_URL}" integrity="{LEAFLET_CSS_SRI}" crossorigin="">
<style>
{theme_css(theme)}
{marker_css()}
{PAGE_CSS}
</style>
</head>
<body>
<header class="site-header">
  <h1>{esc(t["name"])}</h1>
  <p class="meta">{esc(fmt_range(t["start"], t["end"]))} · {esc(t["city"])}, {esc(t["country"])}</p>
</header>
<main>
<section class="map-section" aria-label="Map">
  <div class="map-wrap">
    <div id="map" role="region" aria-label="Map of places"></div>
    <p class="map-tools">
      <button type="button" id="fit-all" class="linkish">Show all places on map</button>
{legend}
    </p>
  </div>
  <aside class="panel">
    <h2>Our places</h2>
{render_ours_list(data["ours"])}
    <h2>Popular places</h2>
{render_popular_panel(data["popular"])}
  </aside>
</section>
<section class="events-section" aria-labelledby="events-h">
  <h2 id="events-h">Schedule</h2>
  <p class="muted legend-events"><span class="badge badge--plan">Our plan</span>
    <span class="badge badge--city">City event</span> · Times are local ({esc(t["timezone"])}).
    Click an event with ⌖ to show it on the map.</p>
{render_events(data)}
</section>
<section class="about" aria-labelledby="about-h">
  <h2 id="about-h">About this trip</h2>
{about}
</section>
</main>
<footer class="site-footer"><p class="muted">Private trip page. Map data © OpenStreetMap contributors.</p></footer>
<script type="application/json" id="trip-data">
{page_json(data)}
</script>
<script src="{LEAFLET_JS_URL}" integrity="{LEAFLET_JS_SRI}" crossorigin=""></script>
<script>
{PAGE_JS}
</script>
</body>
</html>
"""


def render_root_index() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
{HEAD_COMMON}
<title>World Cities</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh;
         margin: 0; background: #F5EDE4; color: #8B4513; }}
</style>
</head>
<body>
<main><h1>World Cities</h1></main>
</body>
</html>
"""


def render_not_found() -> str:
    """dist/404.html. With a top-level 404.html, Cloudflare Pages answers a missing path
    with this page and status 404; without one it treats the site as a single-page app and
    serves index.html with 200. Neutral: no trip slugs, same look as the root placeholder."""
    return f"""<!doctype html>
<html lang="en">
<head>
{HEAD_COMMON}
<title>Not found</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh;
         margin: 0; background: #F5EDE4; color: #8B4513; }}
</style>
</head>
<body>
<main><h1>Not found</h1></main>
</body>
</html>
"""


ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
HEADERS_TXT = "/*\n  X-Robots-Tag: noindex, nofollow\n"
SITE_FILES = ("index.html", "404.html", "robots.txt", "_headers")


PAGE_CSS = """
:root {
  --bg: var(--theme-bg, #fff);
  --text: var(--theme-text, #222);
  --accent: var(--theme-road-motorway, var(--text));
  --accent-2: var(--theme-road-primary, var(--accent));
  --soft: var(--theme-parks, #eee);
  --line: var(--theme-road-residential, #ccc);
  --city: var(--theme-water, #9bc);
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--text); }
body { margin: 0 auto; max-width: 1200px; padding: 1rem 1.25rem 2rem;
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
h1 { margin: 0; font-size: 2rem; letter-spacing: .02em; }
h2 { font-size: 1.2rem; margin: 1.5rem 0 .5rem; }
h3 { font-size: 1rem; margin: 0 0 .25rem; }
a { color: var(--accent); }
.meta { margin: .25rem 0 1rem; }
.muted { opacity: .75; }
.site-header { border-bottom: 3px solid var(--accent); margin-bottom: 1rem; }
.map-section { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 1rem; align-items: start; }
#map { height: 520px; border: 2px solid var(--line); border-radius: 6px; }
.map-tools { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; justify-content: space-between;
             font-size: .9rem; margin: .4rem 0 0; }
.dot { display: inline-block; width: .9em; height: .9em; border-radius: 50%; vertical-align: -.1em;
       border: 2px solid var(--mk-outline); box-shadow: 0 0 0 1px rgba(0, 0, 0, .45); margin-left: .5em; }
.dot--ours { background: var(--mk-ours); }
.dot--popular { background: var(--mk-popular); }
/* white ring behind the dark dashes so the edge shows on dark themes (noir) too */
.dot--stay { background: color-mix(in srgb, var(--mk-stay) 30%, #fff); border: 2px dashed var(--mk-stay-edge);
             box-shadow: 0 0 0 2px var(--mk-outline); }
.popup hr { border: 0; border-top: 1px solid #ccc; margin: .4rem 0; }
.panel { background: var(--soft); border-radius: 6px; padding: .25rem 1rem 1rem; max-height: 560px; overflow: auto; }
.panel h2:first-child { margin-top: .75rem; }
.panel ul { list-style: none; margin: 0; padding: 0; }
.panel li { margin: .15rem 0; }
.cat { border: 1px solid var(--line); border-radius: 4px; margin: 0 0 .6rem; padding: .25rem .6rem .4rem; }
.cat legend { font-weight: 600; padding: 0 .25rem; }
.cat ul { padding-left: 1.2rem; }
label { cursor: pointer; }
.linkish { background: none; border: 0; padding: 0; font: inherit; color: var(--accent);
           text-decoration: underline; cursor: pointer; text-align: left; }
.days { list-style: none; padding: 0; margin: 0; display: grid;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .75rem; }
.day { border: 1px solid var(--line); border-radius: 6px; padding: .6rem .75rem; }
.events { list-style: none; margin: 0; padding: 0; }
.event { padding: .4rem .5rem; margin: .35rem 0; border-radius: 4px; border-left: 5px solid; }
.event--plan { border-left-color: var(--accent); background: var(--soft); }
.event--city { border-left-color: var(--city); border-left-style: dashed; }
.ev-main { display: flex; flex-wrap: wrap; gap: .1rem .5rem; align-items: baseline; }
.ev-name { font-weight: 600; }
.ev-extra { font-size: .9rem; margin-top: .15rem; }
.ev-extra .source { margin-left: .4rem; }
.time { font-variant-numeric: tabular-nums; font-size: .9rem; }
.badge { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; padding: .05rem .4rem;
         border-radius: 3px; background: var(--accent); color: var(--bg); }
.event--city .badge, .badge--city { background: var(--city); color: var(--text); }
.badge--plan { background: var(--accent); color: var(--bg); }
.tag { font-size: .75rem; border: 1px solid var(--line); border-radius: 3px; padding: 0 .3rem; }
.tag--todo { border-style: dashed; }
.about { border-top: 1px solid var(--line); margin-top: 1.5rem; }
.popup strong { display: block; }
.popup p { margin: .25rem 0; }
.popup .cat-label { font-size: .8rem; opacity: .75; }
.site-footer { margin-top: 2rem; font-size: .85rem; }
@media (max-width: 800px) {
  body { padding: .75rem; }
  h1 { font-size: 1.5rem; }
  .map-section { grid-template-columns: 1fr; }
  #map { height: 60vh; min-height: 320px; }
  .panel { max-height: none; }
  .days { grid-template-columns: 1fr; }
}
"""


PAGE_JS = r"""
(function () {
  "use strict";
  if (!window.L) {
    document.getElementById("map").textContent = "Map could not load (Leaflet unavailable).";
    return;
  }
  var data = JSON.parse(document.getElementById("trip-data").textContent);
  var style = getComputedStyle(document.documentElement);
  function cssVar(name, fallback) { return style.getPropertyValue(name).trim() || fallback; }
  // Map features use a fixed tile-safe palette (see MARKER_COLOURS), not the theme.
  var C = {
    outline: cssVar("--mk-outline", "#fff"),
    ours: cssVar("--mk-ours", "#C62828"),
    popular: cssVar("--mk-popular", "#1565C0"),
    event: cssVar("--mk-event", "#00695C"),
    stay: cssVar("--mk-stay", "#7B1FA2"),
    stayEdge: cssVar("--mk-stay-edge", "#2A0A3A")
  };
  var MAX_APPROX_ZOOM = data.maxApproxZoom || 14;

  var map = L.map("map", { scrollWheelZoom: false })
    .setView([data.trip.center.lat, data.trip.center.lon], data.trip.zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  map.on("focus", function () { map.scrollWheelZoom.enable(); });
  map.on("blur", function () { map.scrollWheelZoom.disable(); });

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function isHttp(u) { return typeof u === "string" && /^https?:\/\//i.test(u); }
  function link(href, text) {
    var a = el("a", text);
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  }
  function gmaps(lat, lon) {
    return "https://www.google.com/maps/search/?api=1&query=" + lat + "," + lon;
  }
  function pretty(cat) {
    var s = String(cat || "").replace(/[_-]/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function placePopup(p) {
    var box = el("div", null, "popup");
    if (p.approx) {
      box.appendChild(el("strong", p.label || "Private place"));
      box.appendChild(el("p", "Approximate area (about " + p.radius + " m).", "cat-label"));
      return box;
    }
    box.appendChild(el("strong", p.name));
    box.appendChild(el("span", pretty(p.category) + (p.group === "ours" ? " · our place" : ""), "cat-label"));
    if (p.address) box.appendChild(el("p", p.address));
    if (p.notes) box.appendChild(el("p", p.notes));
    var links = el("p");
    if (isHttp(p.url)) {
      links.appendChild(link(p.url, "Website"));
      links.appendChild(document.createTextNode(" · "));
    }
    links.appendChild(link(gmaps(p.lat, p.lon), "Open in Google Maps"));
    box.appendChild(links);
    return box;
  }

  var layers = {};   // place id -> Leaflet layer
  var placeById = {};
  data.places.forEach(function (p) {
    placeById[p.id] = p;
    var layer;
    if (p.approx) {
      layer = L.circle([p.lat, p.lon], {
        radius: p.radius, color: C.stayEdge, weight: 2.5, dashArray: "6 5",
        fillColor: C.stay, fillOpacity: 0.22
      });
    } else if (p.group === "ours") {
      layer = L.circleMarker([p.lat, p.lon], {
        radius: 10, color: C.outline, weight: 3, fillColor: C.ours, fillOpacity: 1
      });
    } else {
      layer = L.circleMarker([p.lat, p.lon], {
        radius: 7, color: C.outline, weight: 2, fillColor: C.popular, fillOpacity: 0.95
      });
    }
    layer.bindPopup(placePopup(p));
    layers[p.id] = layer;
    if (p.group === "ours" || p.default_on) layer.addTo(map);
  });

  // Popular-place checkboxes: one per place, plus one per category.
  var placeBoxes = Array.prototype.slice.call(document.querySelectorAll(".toggle-place"));
  var catBoxes = Array.prototype.slice.call(document.querySelectorAll(".toggle-cat"));
  function syncCategory(cat) {
    var boxes = placeBoxes.filter(function (b) { return b.dataset.category === cat; });
    var on = boxes.filter(function (b) { return b.checked; }).length;
    catBoxes.forEach(function (cb) {
      if (cb.dataset.category !== cat) return;
      cb.checked = on === boxes.length;
      cb.indeterminate = on > 0 && on < boxes.length;
    });
  }
  function setPlace(box, on) {
    box.checked = on;
    var layer = layers[box.dataset.id];
    if (!layer) return;
    if (on) layer.addTo(map); else map.removeLayer(layer);
  }
  placeBoxes.forEach(function (box) {
    box.addEventListener("change", function () {
      setPlace(box, box.checked);
      syncCategory(box.dataset.category);
    });
  });
  catBoxes.forEach(function (cb) {
    cb.addEventListener("change", function () {
      placeBoxes.forEach(function (b) { if (b.dataset.category === cb.dataset.category) setPlace(b, cb.checked); });
      syncCategory(cb.dataset.category);
    });
    syncCategory(cb.dataset.category);
  });

  function showMap() {
    var box = document.getElementById("map").getBoundingClientRect();
    if (box.top < 0 || box.bottom > window.innerHeight) {
      document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  function focusLayer(layer, approx) {
    var target = layer.getLatLng();
    // Never zoom in far on an approximate area: its centre is deliberately not the stay.
    var zoom = approx ? MAX_APPROX_ZOOM : Math.max(map.getZoom(), 15);
    map.setView(target, zoom);
    layer.openPopup();
    showMap();
  }
  function eventHeader(ev) {
    var box = el("div");
    box.appendChild(el("strong", ev.name));
    box.appendChild(el("span", (ev.kind === "plan" ? "Our plan" : "City event") + " · " + ev.day +
      " · " + (ev.time || "All day"), "cat-label"));
    return box;
  }
  function focusPlace(id, ev) {
    var layer = layers[id], p = placeById[id];
    if (!layer) return;
    if (!map.hasLayer(layer)) {
      var box = placeBoxes.filter(function (b) { return b.dataset.id === id; })[0];
      if (box) { setPlace(box, true); syncCategory(box.dataset.category); } else layer.addTo(map);
    }
    if (ev) {
      // Event + place in one popup; the plain place popup comes back when it closes.
      var combo = el("div", null, "popup");
      combo.appendChild(eventHeader(ev));
      combo.appendChild(el("hr"));
      combo.appendChild(placePopup(p));
      layer.setPopupContent(combo);
      layer.once("popupclose", function () { layer.setPopupContent(placePopup(p)); });
    }
    focusLayer(layer, !!p.approx);
  }

  // One shared marker for events with their own {lat, lon}: moved, never stacked.
  var eventMarker = null;
  function focusEvent(i) {
    var ev = data.events[i];
    if (!ev || !ev.loc) return;
    if (ev.loc.ref) { focusPlace(ev.loc.ref, ev); return; }
    var ll = [ev.loc.lat, ev.loc.lon];
    if (!eventMarker) {
      eventMarker = L.circleMarker(ll, {
        radius: 8, color: C.outline, weight: 3, fillColor: C.event, fillOpacity: 1
      }).bindPopup("");
    }
    var box = el("div", null, "popup");
    box.appendChild(eventHeader(ev));
    if (ev.loc.label) box.appendChild(el("p", ev.loc.label));
    var links = el("p");
    links.appendChild(link(gmaps(ev.loc.lat, ev.loc.lon), "Open in Google Maps"));
    box.appendChild(links);
    eventMarker.closePopup();
    eventMarker.setLatLng(ll).setPopupContent(box);
    if (!map.hasLayer(eventMarker)) eventMarker.addTo(map);
    focusLayer(eventMarker, false);
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest(".event-go, .focus-place") : null;
    if (!t) return;
    if (t.classList.contains("event-go")) focusEvent(Number(t.dataset.event));
    else focusPlace(t.dataset.id);
  });

  // Places only (event markers excluded); an approximate area counts as its whole circle.
  document.getElementById("fit-all").addEventListener("click", function () {
    var bounds = null;
    Object.keys(layers).forEach(function (id) {
      var l = layers[id];
      if (!map.hasLayer(l)) return;
      var b = placeById[id].approx ? l.getBounds() : L.latLngBounds([l.getLatLng()]);
      bounds = bounds ? bounds.extend(b) : b;
    });
    if (bounds) map.fitBounds(bounds, { padding: [30, 30], maxZoom: MAX_APPROX_ZOOM });
  });
})();
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


JUNK_FILES = (".DS_Store",)


NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def is_real_file(path: Path) -> bool:
    """True only for a regular file. A symlink is never followed (always False)."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


_HTML_COMMENT_RE = re.compile(rb"<!--.*?(?:-->|\Z)", re.DOTALL)
_HEAD_END_RE = re.compile(rb"</head\s*>|<body[\s>]", re.IGNORECASE)


def has_marker(path: Path) -> bool:
    """True if path is a real file (not a symlink) whose <head> carries GENERATOR_META.

    Only the first 4096 bytes are read. HTML comments are ignored, and so is everything
    from </head> (or <body>) on, so a hand-made page that mentions the marker in its body
    or in a comment is not mistaken for one of ours."""
    if not is_real_file(path):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as fh:
        head = _HTML_COMMENT_RE.sub(b"", fh.read(4096))
    end = _HEAD_END_RE.search(head)
    if end:
        head = head[:end.start()]
    return GENERATOR_META.encode("utf-8") in head


def write_file(path: Path, text: str) -> None:
    """Write text to a temp file in path's folder, then os.replace it into place.

    Replacing (not truncating) means a hardlinked output gets a new inode and the other
    link keeps its content. os.replace over a symlink would swap the link itself, so a
    symlink is refused first (plan_out_dir refuses those earlier still)."""
    if path.is_symlink():
        raise OSError(f"{path} is a symlink; refusing to replace it")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def plan_out_dir(out_dir: Path, slugs: set[str]) -> tuple[list[Path], list[str]]:
    """Decide what to remove so out_dir holds only SITE_FILES and the slugs being built.

    Returns (paths to delete, problems). Only things this tool writes are ever deleted:
    a stale trip folder is removed only if it matches the slug pattern and holds nothing
    but an index.html carrying GENERATOR_META (plus .DS_Store). Symlinks are never
    followed, written through or deleted. Anything else is reported, never deleted."""
    remove: list[Path] = []
    problems: list[str] = []
    if not out_dir.exists():
        return remove, problems
    if not out_dir.is_dir():
        return remove, [f"{out_dir} exists and is not a folder"]
    not_ours = "was not written by this tool"
    for entry in sorted(out_dir.iterdir()):
        if entry.is_symlink():
            problems.append(f"{entry} is a symlink, so it {not_ours} (never followed or replaced)")
            continue
        if entry.name in SITE_FILES and is_real_file(entry):
            continue
        if entry.name in JUNK_FILES and is_real_file(entry):
            remove.append(entry)
            continue
        if entry.is_dir() and SLUG_RE.match(entry.name):
            inner = sorted(entry.iterdir())
            links = [f for f in inner if f.is_symlink()]
            for f in links:
                problems.append(f"{f} is a symlink, so it {not_ours} (never followed or replaced)")
            if links:
                continue
            if all(is_real_file(f) and f.name in ("index.html",) + JUNK_FILES for f in inner):
                if entry.name in slugs:  # rebuilt now: its index.html is overwritten
                    remove.extend(f for f in inner if f.name in JUNK_FILES)
                    continue
                if not any(f.name == "index.html" for f in inner):  # empty, or .DS_Store only
                    problems.append(f"{entry} has no index.html, so it {not_ours} (not deleted)")
                    continue
                if has_marker(entry / "index.html"):
                    remove.extend(inner + [entry])
                    continue
                problems.append(f"{entry} has no tripsite marker in index.html, so it {not_ours} "
                                "(not deleted)")
                continue
        problems.append(f"{entry} {not_ours}")
    return remove, problems


def slugs_in(out_dir: Path) -> list[str]:
    if not out_dir.is_dir():
        return []
    return sorted(d.name for d in out_dir.iterdir() if d.is_dir() and (d / "index.html").is_file())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a static trip website from trip profiles. Pass EVERY live trip in one run "
                    "(e.g. trips/*.md): the output folder is trimmed to exactly these trips.")
    ap.add_argument("trip_files", type=Path, nargs="+", metavar="trip_file",
                    help="trip profile(s), e.g. trips/mexico-city-2026.md or trips/*.md")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    ap.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    args = ap.parse_args(argv)

    # 1. Validate every trip; nothing is written unless all of them pass.
    trips: list[tuple[Path, dict, str, int]] = []
    failed = 0
    slug_owner: dict[str, Path] = {}
    for path in args.trip_files:
        data, body, p = load_trip(path)
        dropped = 0
        if not p.errors:
            dropped = filter_events(data, p)
            apply_privacy(data, body, p)
            slug = data["trip"]["slug"]
            if slug in slug_owner:
                p.err("trip.slug", f"{slug!r} is also used by {slug_owner[slug]}")
            slug_owner.setdefault(slug, path)
        for w in p.warnings:
            print(f"warning: {path}: {w}", file=sys.stderr)
        if p.errors:
            print(f"error: {path} has {len(p.errors)} problem(s):", file=sys.stderr)
            for e in p.errors:
                print(f"  - {e}", file=sys.stderr)
            failed += 1
            continue
        trips.append((path, data, body, dropped))
    if failed:
        return 1

    # 2. Render and run the leak scan before touching the output folder.
    pages: dict[str, str] = {}
    for path, data, body, dropped in trips:
        t = data["trip"]
        print(f"trip:    {t['name']} ({t['start']} → {t['end']}), theme {t['theme']}, slug {t['slug']}")
        print(f"places:  {len(data['ours'])} ours (stay: {data['privacy']['hotel_display']}), "
              f"{len(data['popular'])} popular")
        print(f"events:  {len(data['events'])} kept, {dropped} dropped (outside trip dates)")
        page = render_trip_page(data, body, load_theme(t["theme"]))
        leaks = scan_for_leaks(page, data.get("_private_stays", []))
        if leaks:
            print(f"error: {path}: privacy check failed, nothing written "
                  f"(hotel_display: {data['privacy']['hotel_display']}):", file=sys.stderr)
            for leak in leaks:
                print(f"  - {leak}", file=sys.stderr)
            print("  Remove it from the body, event names/notes and other places, then rebuild.",
                  file=sys.stderr)
            return 1
        pages[t["slug"]] = page

    # 3. Output folder: exactly the site files + the slugs built now.
    out_dir = args.out.resolve()
    remove, problems = plan_out_dir(out_dir, set(pages))
    if problems:
        print(f"error: {out_dir} holds things this tool didn't write, and a Pages upload would publish "
              "them. Move them out (or use a fresh --out folder):", file=sys.stderr)
        for prob in problems:
            print(f"  - {prob}", file=sys.stderr)
        return 1
    stale = sorted({p.name for p in remove if p.is_dir()})
    if args.dry_run:
        print(f"dry run: would write {', '.join(sorted(pages))} and {', '.join(SITE_FILES)} in {out_dir}")
        if stale:
            print(f"dry run: would REMOVE stale trip folder(s) not in this run: {', '.join(stale)}")
        return 0

    for path in remove:  # files first, then the (now empty) stale folders
        if is_real_file(path):
            path.unlink()
    for path in remove:
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
    for slug, page in pages.items():
        (out_dir / slug).mkdir(parents=True, exist_ok=True)
        write_file(out_dir / slug / "index.html", page)
    write_file(out_dir / "index.html", render_root_index())
    write_file(out_dir / "404.html", render_not_found())
    write_file(out_dir / "robots.txt", ROBOTS_TXT)
    write_file(out_dir / "_headers", HEADERS_TXT)
    for slug in sorted(pages):
        print(f"wrote:   {out_dir / slug / 'index.html'}")
    for name in SITE_FILES:
        print(f"         {out_dir / name}")
    if stale:
        print(f"removed: stale trip folder(s) not in this run: {', '.join(stale)}")
    print(f"\ndist now holds {len(slugs_in(out_dir))} trip(s): {', '.join(slugs_in(out_dir))}")
    print("  Each needs its own Access app before upload (README, Gate 4). An upload replaces the "
          "whole site, so any live trip not built in this run would be taken down.")
    print("\npreview (file:// may not load OSM tiles because it sends no Referer):")
    print(f"  python -m http.server -d {out_dir}")
    for slug in sorted(pages):
        print(f"  open http://localhost:8000/{slug}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
