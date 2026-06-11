---
description: Generate a dated HTML status page (project/status/status-YYYY-MM-DD.html) summarising maptoposter — what it does, how to run it, available themes, and the AGENTS.md automation vision vs. what's actually been built.
---

Generate a project status HTML report for maptoposter.

## What to read first (do all reads in parallel)

1. `README.md` — usage, options, themes, resolution guide, architecture overview
2. `AGENTS.md` — the personal automation vision: locations.yaml, generate_trip.py, vacation poster workflow
3. `project/diary/` — list files, read all diary entries (short)
4. Run `git log --oneline -8` for recent commits
5. Run `ls themes/` to confirm the theme count and names
6. Run `ls posters/ 2>/dev/null | head -10 || echo "posters dir empty"` to see any generated posters
7. Run `ls scripts/ 2>/dev/null || echo "no scripts dir"` to see if automation scripts exist yet

## Output

Create a single self-contained HTML file at:

```
project/status/status-YYYY-MM-DD.html
```

where `YYYY-MM-DD` is today's date. No external dependencies — all CSS and SVG inline.

## Section order and content

The page answers: "What does this tool do, how do I generate a poster, and what's the gap between the current state and the vacation-poster vision?"

### 1. Header bar
- Tool name: **maptoposter**
- Tagline: Generate minimalist city map posters from OpenStreetMap data
- Type badge: "Personal Utility — Run When Needed"
- Origin badge: "Fork of originalankur/maptoposter"
- Current date
- Palette: slate `#1e293b` + teal `#0d9488` + amber `#d97706` — clean, minimal

### 2. Snapshot pills
- Language: Python + uv
- Data source: OpenStreetMap (via OSMnx + Nominatim geocoding)
- Output: PNG poster in `posters/` directory
- Themes: count from `ls themes/` (17 themes in the base fork)
- Automation layer: derive from `ls scripts/` — "Not built" if no scripts dir
- Last code change: derive from `git log --oneline -1`

### 3. What It Does — One Paragraph
A tool to generate minimalist, print-quality city map posters. Give it a city and country, it fetches OpenStreetMap road/water/parks data via OSMnx, renders it with matplotlib using a chosen colour theme, and saves a PNG. Supports 17 built-in themes, multilingual city labels (Japanese, Arabic, Thai, etc.), custom coordinates, and configurable size/resolution. The poster output goes to `posters/` named `{city}_{theme}_{timestamp}.png`.

### 4. How to Generate a Poster (quick reference)

This is the most important section — what to do when you want a poster.

**Single poster (default terracotta theme):**
```bash
uv run ./create_map_poster.py --city "Edmonton" --country "Canada"
```

**With a specific theme:**
```bash
uv run ./create_map_poster.py --city "Paris" --country "France" --theme noir --distance 10000
```

**All 17 themes at once (for comparison):**
```bash
uv run ./create_map_poster.py --city "Tokyo" --country "Japan" --all-themes
```

**List available themes:**
```bash
uv run ./create_map_poster.py --list-themes
```

**Custom size (A4 print):**
```bash
uv run ./create_map_poster.py --city "Venice" --country "Italy" --theme blueprint -W 8.3 -H 11.7
```

**Ambiguous location — override coordinates:**
```bash
uv run ./create_map_poster.py --city "Stanley Park" --country "Canada" -lat 49.3043 -long -123.1443 --theme forest
```

**Multilingual labels:**
```bash
uv run ./create_map_poster.py --city "Tokyo" --country "Japan" --display-city "東京" --display-country "日本" --font-family "Noto Sans JP" --theme japanese_ink
```

### 5. Available Themes

Pull from `ls themes/`. Show as a compact two-column grid:

| Theme | Style |
|---|---|
| `terracotta` | Mediterranean warmth (default) |
| `noir` | Pure black, white roads |
| `midnight_blue` | Navy + gold roads |
| `blueprint` | Architectural blueprint |
| `neon_cyberpunk` | Dark + electric pink/cyan |
| `warm_beige` | Vintage sepia |
| `pastel_dream` | Soft muted pastels |
| `japanese_ink` | Minimalist ink wash |
| `emerald` | Dark green |
| `forest` | Deep greens + sage |
| `ocean` | Blues + teals |
| `sunset` | Warm oranges + pinks |
| `autumn` | Burnt oranges + reds |
| `copper_patina` | Oxidized copper |
| `monochrome_blue` | Single blue family |
| `gradient_roads` | Smooth gradient shading |
| `contrast_zones` | High contrast density |

### 6. Distance Guide (quick reference)

| Distance | Best for |
|---|---|
| 4000–6000m | Small/dense cities (Venice, Amsterdam) |
| 8000–12000m | Medium cities, downtown focus (Paris, Barcelona) |
| 15000–20000m | Large metros, full city view (Tokyo, Mumbai) |

Default: 18000m.

### 7. The Automation Vision vs. Current State

AGENTS.md documents an intended personal automation layer — this section shows the gap honestly.

**The vision** (from AGENTS.md — not yet built):
```
data/
  locations.yaml          ← vacation locations catalogue
scripts/
  generate_one.py         ← one poster from CLI args
  generate_trip.py        ← all posters for one trip ID
  generate_all.py         ← every poster in locations.yaml
  validate_locations.py   ← check location data
output/
  2024-vancouver/
    vancouver-canada-emerald.png
  2025-italy/
    venice-italy-blueprint.png
```

**What actually exists:**
Derive from `ls scripts/` and `ls posters/` outputs:
- If `scripts/` doesn't exist: "No automation scripts built. The base engine works — the trip/location layer is planned but not started."
- If some scripts exist: list them
- If `posters/` has files: show the count

**Conclusion:** The tool works as a one-off CLI command. The vacation-poster batch workflow (AGENTS.md) is a good next session goal if poster generation becomes a regular activity.

### 8. Gotchas

- **Requires internet**: Nominatim (geocoding) and OSMnx (road data) both hit the internet on each run. No offline mode.
- **Large dist = slow**: `--distance 20000` fetches a lot of OSM data. Use 150 DPI instead of 300 for quick previews.
- **Ambiguous cities**: Small towns or landmarks may not geocode correctly. Use `--latitude`/`--longitude` to override.
- **Generated posters aren't committed**: `posters/` is gitignored — regenerate from the command when needed.
- **Nominatim rate limits**: If running `--all-themes` on multiple cities in a loop, add delays to avoid rate-limiting.

### 9. Next Session — What to Do

Two paths depending on intent:

**Path A — Just want a poster now:**
Run the command above with your chosen city and theme. Done in 30–60 seconds.

**Path B — Build the vacation automation layer:**
1. Create `data/locations.yaml` with 3–5 known trip locations
2. Write `scripts/generate_trip.py` to batch-generate from the YAML
3. Test with one trip first
4. Read AGENTS.md in full — it has the full schema and script expectations already written

### 10. Footer
"Generated YYYY-MM-DD · maptoposter · Personal Python utility · derived from README.md, AGENTS.md, diary, git log"

## Visual style

- Background: `#f9fafb`; cards: white with `1px solid #e5e7eb` and light shadow
- Palette: slate `#1e293b`, teal `#0d9488`, amber `#d97706`, green `#16a34a`
- Header: slim slate bar — utility tool, not a product
- "How to Generate" section: code block style, prominent — the most useful content
- Themes: compact two-column table — reference only
- Automation vision section: two-column "vision vs. reality" layout — teal for vision, amber for current state
- Gotchas: amber-left-border compact cards
- No external fonts or images — system font stack only
- Keep the page compact — this is a run-when-needed utility

## Rules

- Write the file directly — do not ask for confirmation first
- Check `ls scripts/` to determine whether the automation layer has been started — do not assume it hasn't
- Check `ls posters/` for any generated output — if present, note it; if empty, say so
- The "How to Generate" section is the most valuable part — keep it complete and copy-paste ready
- After writing the file, confirm the path and list the sections included

## Also write STATUS-SUMMARY.md

After writing the HTML file, write (or overwrite) a summary file at `project/status/STATUS-SUMMARY.md` (create the directory if it doesn't exist).

Use this exact format — YAML frontmatter only, no markdown body:

```
---
name: maptoposter
tagline: <one sentence — what this project is, derived from the files you just read>
group: Utilities
profile: Utility
priority: 12
status: <one sentence — the most important thing about current state right now>
generated: <today's date YYYY-MM-DD>
---
```

- `tagline`: purpose of the project — stable, changes rarely
- `status`: current state — stable/active/last run date
- Overwrite every run — no date suffix, always one file
