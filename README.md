# Curator

Curator organizes photos and videos from a source volume into a destination
library with a reviewed, copy-first workflow.

## Setup

From a fresh clone:

```sh
make
```

Then open a new terminal and confirm:

```sh
curator --help
```

`make` creates `.venv/`, installs Curator editable, installs a global
`~/bin/curator` wrapper, and adds `~/bin` to `~/.zshrc` if needed. The wrapper
uses this checkout's `.venv` and refreshes it when setup metadata changes.

## Run

Interactive:

```sh
curator
```

Select `1. ingestion`, then enter the Source and Destination folders. Curator
creates `Export YYYY-MM-DD HH:MM` inside the destination folder. If Curator
detects one likely source drive and one likely destination drive, press Enter at
each folder prompt to accept them.

Direct:

```sh
curator --source /Volumes/mySD --dest /Volumes/myHD
```

Curator opens the browser review, writes `SOURCE/DRYRUN.txt` after `Looks good`,
then waits for `Commit`. Commit copies directly from Source into
`DEST/Originals/` and validates the destination against the original files.

For place identification, create `.env`:

```sh
cp .env.example .env
# set OPENROUTER_API_KEY in .env
```

## Make Targets

```sh
make              # setup venv, global wrapper, and PATH
make test         # run unit tests
make compile      # byte-compile src and tests
make clean        # remove Python/build caches
make clean-venv   # remove .venv
make uninstall-global
```

See [DESIGN.md](DESIGN.md) for implementation details and design notes.
