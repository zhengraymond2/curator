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

Run the CLI from the repo with:

```sh
PYTHONPATH=src python3 -m curator --help
```

Run tests with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Tests generate fake media only under:

```text
/Users/rzheng/dev/curator/test/
```

## Current Commands

```sh
curator ingest --help
curator organize --help
curator dedupe --help
curator plan --help
curator apply --help
curator trash-report --help
curator glacier-plan --help
```

During development, prefer the module form:

```sh
PYTHONPATH=src python3 -m curator ingest --source test/runtime/card --dest test/runtime/ssd
PYTHONPATH=src python3 -m curator organize --mode migration --transfer copy --source test/runtime/source --library test/runtime/library
PYTHONPATH=src python3 -m curator organize --mode migration --transfer copy --source test/runtime/source --library test/runtime/library --dry-mode
```

Add `--apply` only after reviewing the generated plan.

`--dry-mode` writes a fake destination hierarchy to `SOURCE/DRYRUN.txt` and applies no copy/move operations.

## LLM Place Identification

The agentic place-identification stage lives in `curator.place_identification`. It samples
1-2 photos from each group, preferring the pair with the most different capture timestamps,
downsamples them into compact JPEG data URLs, then sends them to OpenRouter.

Default model: `openai/gpt-5.4-mini`.

The prompt is checked in at `src/curator/prompts/prompt_001_identify_place.txt` and tells
the model to use an `unknown XYZ` fallback when the location evidence is weak.

Create a local `.env` file for the OpenRouter token:

```sh
cp .env.example .env
# then edit .env and set OPENROUTER_API_KEY
```
