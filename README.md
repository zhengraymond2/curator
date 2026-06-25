# Curator

Curator is a macOS-first, power-user CLI for safely organizing photography and videography files.

The design center is conservative:

- generate plans before moving files
- checksum card ingests
- never hard-delete media
- never overwrite destinations
- soft-trash duplicates and junk for manual review
- respect manually curated folders after import

See [docs/design.md](docs/design.md) for the current design doc.

## Development

Create and activate the repo-local Python environment with:

```sh
source scripts/env
```

This creates `.venv/`, activates it, and installs Curator in editable mode from
`pyproject.toml`. If pyenv is available, the script uses the pinned
`.python-version`; otherwise it falls back to any compatible `python3` and keeps
dependencies isolated inside `.venv/`.

To use the pinned pyenv runtime explicitly:

```sh
pyenv install -s "$(cat .python-version)"
source scripts/env
```

Optional overrides:

```sh
CURATOR_PYTHON=/path/to/python3 source scripts/env
CURATOR_EXTRAS=raw source scripts/env
```

Run the CLI from the repo with:

```sh
python -m curator --help
```

Run tests with:

```sh
python -m unittest discover -s tests
```

Tests generate fake media only under:

```text
/Users/rzheng/dev/curator/test/
```

## Convenience Scripts

Project-specific commands live in `scripts/`. These are thin wrappers around the
CLI, useful while Curator is still run from the repo checkout.

Run the reviewed image-location dry run against any source directory with:

```sh
./scripts/review-dryrun "/Volumes/LaCie 1/CRG"
```

The first unnamed argument is required and must be the source directory. The
script writes the preview hierarchy to `SOURCE_DIR/DRYRUN2.txt` by default and
does not copy or move media.

Useful overrides:

```sh
DRY_RUN_FILE=DRYRUN3.txt ./scripts/review-dryrun "/Volumes/LaCie 1/CRG"
LIBRARY_ROOT=/path/to/library ./scripts/review-dryrun "/Volumes/LaCie 1/CRG"
PYTHON_BIN="$(which python3)" ./scripts/review-dryrun "/Volumes/LaCie 1/CRG"
```

## LLM Place Identification

The agentic place-identification stage lives in `curator.place_identification`. It samples
1-2 photos from each group, preferring the pair with the most different capture timestamps,
downsamples them into compact JPEG data URLs, then sends them to OpenRouter.

Default model: `openai/gpt-5.4-mini`.

Use `curator organize --identify-places` to fold these names into planned destination
folders. With the current schema, Curator asks for both `country_or_region` and
`place_name`, then plans paths like `Originals/Italy/Rome/...`.

Add `--review-unknown-places` to open a Quick Look sample gallery whenever the model
returns an unknown location. Close the gallery with Esc, then enter `Country/Place`
or a place name in the CLI.

Add `--review-ui` to open a local browser review flow for every place-identified
bundle. The UI shows the model guess, a `Country` textbox, a `Place` textbox, and
a gallery of every image in the bundle. Press Enter or click `Save / Continue` to
advance. The place textbox suggests previously entered places with case-insensitive
fuzzy matching; selecting an existing place reuses the same destination folder.
Each reviewed location becomes context for the next model prompt, with the most
recent country/region treated as the active trip context.

The prompt is checked in at `src/curator/prompts/prompt_001_identify_place.txt` and tells
the model to use an `unknown XYZ` fallback when the location evidence is weak.

Create a local `.env` file for the OpenRouter token:

```sh
cp .env.example .env
# then edit .env and set OPENROUTER_API_KEY
```
