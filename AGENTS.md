# AGENTS.md for MapToPoster Vacation Poster Project

This Markdown file contains a complete starter `AGENTS.md` for a personal variation of [`originalankur/maptoposter`](https://github.com/originalankur/maptoposter.git), focused on generating map posters for vacation locations visited by Rob and Lucie.

The important principle: this project should use MapToPoster as a rendering engine. It should not become a refactor, redesign, or generic travel app. That way lies madness, and probably a half-built UI nobody asked for.

---

# AGENTS.md

## Project purpose

This project is a personal automation layer around `originalankur/maptoposter`.

The goal is **not** to improve, redesign, or generalize the original MapToPoster project. The goal is to use it as a map-poster generation engine for creating posters of vacation locations visited by Rob and Lucie.

The main work in this repository should be small Bash and Python scripts that:

- define vacation locations in a structured format
- call the existing `create_map_poster.py` script
- generate map posters in consistent styles
- organize outputs by trip, year, country, city, and theme
- make it easy to regenerate posters later

Treat the upstream project as mostly read-only unless the user explicitly asks for changes to the base library.

## Important context

The upstream project is a Python tool that generates minimalist city map posters using commands like:

```bash
uv run ./create_map_poster.py --city "Paris" --country "France"
```

It supports options such as:

- `--city`
- `--country`
- `--latitude`
- `--longitude`
- `--display-city`
- `--display-country`
- `--country-label`
- `--theme`
- `--distance`
- `--width`
- `--height`
- `--all-themes`
- `--list-themes`
- `--font-family`

Prefer using the existing CLI interface instead of modifying internals.

## Primary objective

Build a repeatable workflow for generating posters for personal travel locations.

A good end state is something like:

```text
data/
  locations.yaml

scripts/
  generate_one.py
  generate_trip.py
  generate_all.py
  generate_all.sh
  validate_locations.py

output/
  2024-vancouver/
    vancouver-canada-terracotta.png
  2025-italy/
    venice-italy-blueprint.png
```

The exact structure can evolve, but keep it simple.

## Non-goals

Do not do these unless explicitly asked:

- Do not rewrite `create_map_poster.py`.
- Do not convert the project into a web app.
- Do not add a database.
- Do not add authentication.
- Do not add a UI.
- Do not introduce a complex plugin system.
- Do not restructure the upstream project.
- Do not create a generic vacation-planning platform.
- Do not chase scope creep dressed up as architecture.

This is a scripting and automation project. Keep it boring.

## Preferred workflow

When asked to make changes:

1. Inspect the existing files first.
2. Identify whether the change belongs in:
   - a new script
   - a data file
   - documentation
   - a small wrapper around the existing CLI
3. Prefer adding scripts over changing upstream code.
4. Make the smallest useful change.
5. Explain how to run it.
6. Include example commands.

## Coding style

### Python

Use Python for structured logic:

- reading YAML, JSON, or CSV location files
- validating location data
- building commands
- running poster generation
- organizing outputs
- dry-run previews

Python code should be:

- clear
- typed where useful
- simple
- readable by a non-specialist
- defensive about missing fields and bad paths

Prefer standard library modules unless a dependency is clearly worth it.

Good modules to use:

```python
pathlib
subprocess
argparse
dataclasses
typing
json
csv
shutil
```

If YAML is used, prefer `PyYAML`, but check whether it is already available before adding it.

### Bash

Use Bash for lightweight orchestration only.

Bash scripts should:

- use `set -euo pipefail`
- resolve paths relative to the script location
- quote variables
- print clear progress messages
- fail loudly and usefully

Do not put complicated data processing in Bash. That is Python’s job.

## Data files

Vacation locations should live in a structured data file, probably:

```text
data/locations.yaml
```

A useful schema might be:

```yaml
trips:
  - id: "2024-vancouver"
    label: "Vancouver Birthday Weekend"
    year: 2024
    locations:
      - city: "Vancouver"
        country: "Canada"
        display_city: "Vancouver"
        display_country: "Canada"
        theme: "emerald"
        distance: 18000
        width: 12
        height: 16
```

Support optional latitude and longitude when a city name is ambiguous or when the desired poster centre is not the city centre:

```yaml
      - city: "Capilano Suspension Bridge"
        country: "Canada"
        latitude: 49.3429
        longitude: -123.1149
        display_city: "Capilano"
        display_country: "Canada"
        theme: "forest"
```

Do not assume all travel locations are major cities. Some may be parks, islands, neighbourhoods, coastal towns, or landmarks.

## Script expectations

### `generate_one.py`

Should generate one poster from command-line arguments.

Example:

```bash
python scripts/generate_one.py \
  --city "Venice" \
  --country "Italy" \
  --theme "blueprint"
```

### `generate_trip.py`

Should generate all posters for one trip ID from `data/locations.yaml`.

Example:

```bash
python scripts/generate_trip.py --trip 2025-italy
```

### `generate_all.py`

Should generate every poster defined in the locations file.

Example:

```bash
python scripts/generate_all.py
```

### Dry run support

Where practical, scripts should support:

```bash
--dry-run
```

This should print the commands that would be run without generating files.

## Command execution

When calling MapToPoster, prefer `uv` if the project is already using it:

```bash
uv run ./create_map_poster.py --city "Paris" --country "France"
```

If `uv` is unavailable or the environment is clearly using a virtual environment, use:

```bash
python create_map_poster.py --city "Paris" --country "France"
```

Do not assume the user has global packages installed.

## Output organization

Generated posters should be copied or moved into a predictable output folder.

Recommended pattern:

```text
output/<trip-id>/<city-slug>-<country-slug>-<theme>.png
```

Examples:

```text
output/2024-vancouver/vancouver-canada-emerald.png
output/2025-italy/venice-italy-blueprint.png
```

Use safe lowercase slugs:

- lowercase
- spaces become hyphens
- remove punctuation where practical

## Safety and overwrite behaviour

Scripts should not silently overwrite generated posters unless explicitly requested.

Support:

```bash
--overwrite
```

Default behaviour should be:

- skip existing output files
- print a message
- continue processing other locations

## Error handling

Handle common failures clearly:

- missing `data/locations.yaml`
- invalid trip ID
- missing city or country
- failed poster generation command
- no output file produced
- ambiguous location requiring latitude/longitude
- missing dependency

Error messages should explain what the user should do next.

## Documentation

When adding scripts, update or create simple documentation.

Useful files:

```text
README.md
docs/usage.md
```

Documentation should include:

- setup instructions
- where locations are defined
- how to generate one poster
- how to generate one trip
- how to generate all posters
- how to use dry run
- how to troubleshoot geocoding issues

Keep documentation practical. No brochure copy.

## Testing

For Python scripts, prefer lightweight tests where useful.

Focus tests on:

- loading location data
- validating required fields
- slug generation
- command construction
- output path generation

Do not write brittle tests that depend on live geocoding or downloading OpenStreetMap data.

## Design principles

- Use the existing MapToPoster CLI.
- Keep scripts small and composable.
- Make regeneration easy.
- Make the location list human-editable.
- Avoid clever abstractions.
- Prefer boring names.
- Preserve the upstream project unless explicitly told otherwise.

## When uncertain

Ask before making broad architectural changes.

For small implementation choices, make a reasonable decision and document it briefly.

Bias toward:

- fewer files
- fewer dependencies
- fewer moving parts
- more useful scripts
- more repeatable commands

---

# Suggested Project Structure

A practical structure for this personal project:

```text
maptoposter-vacations/
  AGENTS.md
  README.md
  create_map_poster.py
  themes/
  fonts/
  data/
    locations.yaml
  scripts/
    generate_one.py
    generate_trip.py
    generate_all.py
    validate_locations.py
  output/
    .gitkeep
```

The exact upstream project layout may differ. Do not force this structure onto the original project if it conflicts badly. The key idea is to keep personal automation separate from the upstream map-generation code.

---

# Suggested `.gitignore` Additions

Add this to `.gitignore` so generated posters do not pollute the repo:

```gitignore
output/*
!output/.gitkeep
posters/*
```

If you want to commit a few final poster images later, do that deliberately in a separate curated folder such as:

```text
final-posters/
```

Do not accidentally commit every generated experiment. That is how repos become attics.

---

# Recommended Location Data Approach

Keep the vacation list in YAML rather than hardcoded Python.

Reason: you will likely fiddle with:

- city names
- display labels
- themes
- distances
- poster sizes
- exact coordinates
- trip groupings

Hardcoding that in Python makes casual edits annoying. YAML keeps the travel catalogue human-editable and easy for Codex to extend.

A starter `data/locations.yaml` could look like this:

```yaml
trips:
  - id: "2024-vancouver"
    label: "Vancouver Birthday Weekend"
    year: 2024
    locations:
      - city: "Vancouver"
        country: "Canada"
        display_city: "Vancouver"
        display_country: "Canada"
        theme: "emerald"
        distance: 18000
        width: 12
        height: 16

      - city: "Stanley Park"
        country: "Canada"
        latitude: 49.3043
        longitude: -123.1443
        display_city: "Stanley Park"
        display_country: "Vancouver"
        theme: "forest"
        distance: 8000
        width: 12
        height: 16
```

Start small. Add three to five known places first, generate posters, then refine the schema only when the pain is real. Premature architecture is just procrastination wearing a hard hat.
