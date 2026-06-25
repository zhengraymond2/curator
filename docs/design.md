# Curator Design Doc

Status: draft  
Audience: power-user local CLI workflow on macOS  
Primary safety rule: Curator never hard-deletes media.

## 1. Overview

Curator is a photography and videography file-management tool for organizing large personal media libraries across SD cards, CFexpress cards, travel SSDs, external drives, and a home server.

Curator is not intended to replace Lightroom, Finder, or manual curation. Its job is to safely graduate unprocessed media into a predictable library layout, deduplicate exact duplicate originals, preserve auditability, and make future manual curation easier.

The core product shape is a plan-first CLI:

1. Scan input roots.
2. Build a reviewable plan.
3. Apply only non-destructive copy, move, and rename operations.
4. Log every applied operation.
5. Move duplicates and junk into a soft trash area for user review.

## 2. Goals

- Detect mounted camera cards and help copy them to a travel SSD with checksum verification.
- Give visible proof that checksums were computed, completed, and verified successfully.
- Notify the user by sound after a successful ingest.
- Leave SD/CFexpress deletion and ejection entirely to the user.
- Deduplicate exact duplicate originals across all scanned roots.
- Soft-delete only, by moving duplicates or junk into `Trash/`.
- Organize unprocessed media into a canonical library layout:

```text
Library Root/
  Originals/
    Italy/
      Lago-de-Brailes/
      Rome/
      Cinque-Torri/
    Washington/
      Snoqualmie-Falls-June-2026/
      Snoqualmie-Falls-July-2026/
    Unsorted/
      103NCZ_6/
  Trash/
    Duplicates/
    Junk/
  .curator/
    catalog.sqlite
    logs/
    plans/
```

- Preserve the user's manual folder moves and renames after media has been organized.
- Support two organization modes:
  - `migration`: initial aggressive batch processing over historically messy folders.
  - `ongoing`: conservative processing of known unprocessed inputs only.
- Make the CLI self-documenting with `curator --help` and subcommand help.
- Keep the first implementation macOS-only.

## 3. Non-Goals

- Curator does not hard-delete files.
- Curator does not automatically eject cards.
- Curator does not mutate already curated albums during normal operation.
- Curator does not try to be a Lightroom replacement.
- Curator does not initially depend on cloud storage or AWS Glacier.
- Curator does not initially solve all AI classification tasks.

## 4. Operating Principles

### Plan First

Any risky operation must be represented as a plan before it is applied. A plan records intended source paths, destination paths, operation type, expected file size, fingerprint evidence, and safety checks.

### Soft Delete Only

Duplicates and junk are moved into `Trash/`, never removed from disk. The user is responsible for final deletion.

### No Overwrite

Curator refuses to overwrite existing files. If a destination path exists, Curator either chooses a conflict-free name or turns the operation into an error requiring user review.

### Filesystem Is Truth

The folder tree is the source of truth. Curator may keep a SQLite catalog and sidecar manifests for performance, auditability, and future undo tooling, but the library must remain understandable in Finder and terminal tools.

### Curated Means Hands Off

Once media has been moved into `Originals/`, Curator's normal job is done. The user may move files between albums and rename folders freely. Ongoing Curator commands must not undo those changes unless the user explicitly runs a future nuclear repair or migration command on specific folders.

### Exact Dedupe

Deduplication should be conservative. A duplicate requires an exact match on the duplicate key. Near-duplicate detection is a future feature and must never be mixed into exact dedupe.

### Auditable Transactions

Every applied operation should be logged in JSON Lines, with a human-readable summary where useful.

Example:

```json
{"time":"2026-06-24T14:42:00-07:00","op":"move","src":"/source/DCIM/DSC_1234.NEF","dest":"/library/Trash/Duplicates/run/Drive__DCIM__DSC_1234.NEF","reason":"duplicate","kept":"/library/Originals/Italy/Rome/DSC_1234.NEF"}
```

## 5. Main Workflows

### Ingest From Card To Travel SSD

The happy path:

1. User mounts SD or CFexpress card.
2. Curator detects, or the user points Curator at, the mounted card.
3. Curator identifies the likely card type and camera folders.
4. Curator creates a destination ingest folder on the travel SSD.
5. Curator copies the card structure.
6. Curator computes checksums for source files.
7. Curator computes checksums for copied destination files.
8. Curator verifies that every copied file matches.
9. Curator writes a manifest and log.
10. Curator plays a sound.
11. User manually deletes/ejects the card if desired.

The ingest copy should preserve the original card structure, including video metadata and sidecar folders:

```text
Travel SSD/
  Ingests/
    2026-06-24_CARD-Sony-A7IV_001/
      DCIM/
      PRIVATE/
      MISC/
      .curator/
        manifest.json
        checksums.sha256
        copy-log.jsonl
```

Curator may optionally organize the copied files on the SSD after successful ingest. This should happen from the verified SSD copy, not from the card.

### Organize To Library

Curator organizes unprocessed source roots into a library root:

```text
curator organize --mode ongoing --transfer copy --source /Volumes/TravelSSD/Ingests --library /Volumes/TravelSSD
curator organize --mode migration --transfer copy --source /Volumes/OldDrive --library /Volumes/HomeServer
```

Ongoing mode is conservative:

- Treats `Originals/` as curated.
- Processes unprocessed input folders only.
- Does not reorganize existing curated albums.
- May compare against existing library files for dedupe, but should avoid moving curated files to trash unless explicitly asked.

Migration mode is broader:

- Intended for the initial historical cleanup.
- May scan messy external HDDs and SSDs.
- May propose dedupe and organization across large existing folders.
- Should default to copy-first organization so source drives remain intact.
- Still produces a plan first.
- Still never hard-deletes or overwrites.

### Merge Travel SSD To Home Server

Once media is organized on the travel SSD, merging to the home server should usually be a simple checked move/copy from one `Originals/` tree into another.

Rules:

- If the destination album does not exist, move/copy it.
- If the destination album exists and contains exact duplicate files, skip or soft-trash the duplicate source copies according to plan.
- If there is a same-name file that does not match the exact duplicate key, stop and log an error.
- Edits from Lightroom/Photoshop/Premiere should generally be separate generated files, not modifications to originals.

## 6. CLI Commands

Initial command surface:

```text
curator --help
curator ingest --help
curator organize --help
curator dedupe --help
curator plan --help
curator apply --help
curator trash-report --help
curator glacier-plan --help
```

### `curator ingest`

Copies a mounted card or source folder into an ingest folder with checksum verification.

Important options:

```text
--source PATH
--dest PATH
--name NAME
--plan-only
--apply
```

### `curator organize`

Builds an organization plan from unprocessed media into `Originals/`.

Important options:

```text
--mode ongoing|migration
--transfer copy|move
--source PATH
--library PATH
--plan PATH
--dry-mode
--dry-run-file NAME
--identify-places
--review-unknown-places
--review-ui
--apply
```

`--dry-mode` writes a human-readable fake destination hierarchy to `SOURCE/DRYRUN.txt` and performs no copy/move operations. `--dry-run-file` can write a second preview such as `DRYRUN2.txt`. It is intentionally incompatible with `--apply`.

`--identify-places` samples up to two images from each bundled folder, sends those samples to the OpenRouter place-identification stage, and uses the returned `country_or_region` and `place_name` to name planned destination folders.

`--review-unknown-places` makes unknown model results interactive: Curator opens the sampled images in a macOS Quick Look gallery, the user closes it with Esc, and then enters a corrected location in the CLI.

`--review-ui` opens a local browser page that reviews every place-identified bundle sequentially. The page shows the model guess, a country textbox, a place textbox, all prepared images in the bundle, confidence, rationale, and visible evidence. Pressing Enter or clicking `Save / Continue` saves a location and advances to the next bundle. When all bundles are reviewed, Curator writes the requested dry-run file.

The review UI keeps a list of previously entered places and uses it in two ways:

- The place field offers case-insensitive fuzzy suggestions. Selecting or exactly retyping an existing place reuses the same destination folder.
- Each reviewed location is appended to the context for later model prompts. The most recent country/region is treated as the active context, so a user-entered country switch such as `Guatemala` makes later prompts prefer Guatemala context rather than older Costa Rica context.

Example `DRYRUN.txt`:

```text
Italy/
    Rome/
        DSC_0001.NEF
        DSC_0001.NEF
        DSC_0001.NEF
    Cinque Torri/
        DSC_0001.NEF
        DSC_0001.NEF

Iceland/
    Laugavegur/
        DSC_0001.NEF
        DSC_0002.NEF
        DSC_0003.NEF
```

### `curator dedupe`

Scans roots for exact duplicates and builds a soft-delete plan.

Important options:

```text
--root PATH
--library PATH
--trash PATH
--plan PATH
--apply
```

### `curator plan`

Inspects and summarizes a saved plan.

### `curator apply`

Applies a saved plan using the safety engine.

### `curator trash-report`

Summarizes soft-trash contents and reasons.

### `curator glacier-plan`

Future command for generating AWS Deep Glacier backup manifests.

## 7. Storage Model

Curator expects a library root containing:

```text
Originals/
Trash/
.curator/
```

`Originals/` is user-facing and manually editable.

`Trash/` is user-facing and manually reviewable. Curator can move items into it but does not empty it.

`.curator/` is machine-facing. It may contain:

- `catalog.sqlite`
- `logs/*.jsonl`
- `plans/*.json`
- ingest manifests
- checksum manifests

## 8. Catalog And Manifests

Curator should use SQLite for scale and auditability, especially for a 10TB+ home server.

The catalog is an index/cache, not the ultimate truth. If the user renames or moves curated albums manually, Curator should accept the filesystem as reality during later scans.

Likely tables:

- `scan_runs`
- `files`
- `fingerprints`
- `operations`
- `plans`
- `albums`
- `trash_entries`

Sidecar manifests are still useful because they travel with copied ingests and album folders.

## 9. Checksum And Verification Model

Ingest verification should use cryptographic hashes, initially SHA-256.

For each file:

1. Hash source file.
2. Copy to destination temporary path.
3. Hash destination file.
4. Compare size and SHA-256.
5. Rename temporary path into final path.
6. Record result.

The user-visible result should make it obvious that checksums were run and passed:

```text
Checksum verification: PASSED
Files copied: 1,284 / 1,284
Bytes copied: 118.4 GB
Source hashes: complete
Destination hashes: complete
Mismatches: 0
Manifest: /Volumes/TravelSSD/Ingests/.../.curator/manifest.json
```

## 10. Deduplication Algorithm

Exact duplicate key:

```text
normalized original filename
byte size
stable EXIF/media metadata
```

Ignored fields:

- filesystem created time
- filesystem modified time
- backup/copy time
- path
- volume name

If files have the same filename and byte size but conflicting stable metadata, Curator should not guess. It should error and log the conflict because this should be rare and deserves inspection.

For early scaffolding, when full EXIF parsing is not implemented, Curator may use a conservative placeholder fingerprint that includes filename, size, and SHA-256. Production exact dedupe should add stable EXIF/media metadata.

### Duplicate Trash Layout

Duplicates should be moved to:

```text
Trash/
  Duplicates/
    2026-06-24T14-42-00/
      Volumes__OldDrive__DCIM__100NCZ_6__DSC_1234.NEF
      LOG.txt
```

Path components are joined with double underscores so the original source is easy to recognize while keeping each duplicate in one folder.

`LOG.txt` should include:

- duplicate file moved to trash
- preserved file path
- matching evidence
- run ID
- timestamp

This makes it easy to open both the trashed file and the preserved copy for manual verification.

## 11. Shoot Grouping Algorithm

Initial grouping should happen within folder boundaries.

Rules:

- Sort media files in a folder by capture timestamp.
- Start a new shoot when the gap between adjacent files exceeds 60 minutes.
- Do not merge groups across folders in the initial implementation.
- Do not let a bundle skip over an existing filename in the same folder. In filename order, each bundle must be a contiguous run of existing files, so `DSC_0001`, `DSC_0002`, and `DSC_0007` cannot be bundled together when `DSC_0004` exists between them.
- Deduplication remains global across all scanned files.

This respects camera clock drift because photos with wrong clocks are usually still grouped naturally inside their original folder structure.

Future refinement:

- Treat default camera folders like `DCIM`, `NIKON_Z6`, `103NCZ_6`, and similar as weak boundaries.
- Treat obvious custom folders like `Italy` or `Sam's Wedding` as stronger semantic groupings.
- Allow user-assisted merge/split of generated groups.

## 12. Timestamp Priority

Curator should use capture metadata before filesystem metadata.

Current priority:

1. `exiftool` capture fields when `exiftool` is installed:
   `DateTimeOriginal`, `CreateDate`, `MediaCreateDate`, `TrackCreateDate`, `CreationDate`.
2. macOS image metadata via `sips -g creation`.
3. macOS Spotlight metadata via `mdls kMDItemContentCreationDate`.
4. Filesystem modified time as a weak fallback.
5. Unknown timestamp bucket in a future UI/reporting layer.

This keeps RAW/JPEG capture time, video creation time, and iPhone-style media metadata ahead of backup/copy times whenever the local tools can expose them.

## 13. Organization And Album Naming

Canonical album layout:

```text
Originals/
  Country-Or-Region/
    Human-Friendly-Place/
```

If the same place appears more than once, add enough time context to distinguish the albums:

```text
Originals/
  Washington/
    Snoqualmie-Falls-June-2026/
    Snoqualmie-Falls-July-2026/
```

Location inference should favor low-touch automation, but with reviewable evidence.

Possible evidence:

- GPS EXIF coordinates.
- Existing folder names.
- Sampled image recognition.
- LLM-generated place suggestions.
- User-provided trip hints.

Curator should generate human-friendly names rather than strict geocoder names.

For low-confidence cases, Curator can use temporary names:

```text
Originals/Unsorted/2026-06-13-Bend-Oregon-Candidate/
Originals/Unsorted/Shoot-001/
```

## 14. Junk Classification

Junk classification is a later feature.

Examples:

- random GIFs
- memes
- screenshots of old documents
- unused flight-ticket screenshots
- accidental captures

When implemented, junk should be moved to:

```text
Trash/
  Junk/
    RUN_ID/
      ...
      LOG.txt
```

The log must include the classifier or LLM reasoning.

## 15. Video Shot Classification

Video shot classification is a later feature.

Examples:

- drone pull shot
- drone bird's-eye shot
- drone rotate left
- drone rotate right
- handheld pan
- static tripod shot

This should be metadata attached to files or albums, not a reason to move files by default.

## 16. Safety And Permissions

Application-level safety is sufficient for now.

Curator should:

- refuse hard deletes
- refuse overwrites
- avoid following unexpected symlinks by default
- require explicit `--apply` for mutations
- create parent directories only inside declared destination roots
- log every move/copy
- use conflict-free names for trash entries
- support dry-run planning

For development and testing, no command should touch external volumes unless explicitly invoked by the user. Generated tests must stay under:

```text
/Users/rzheng/dev/curator/test/
```

## 17. Existing Library Migration

The initial migration is the riskiest and most powerful mode.

Recommended strategy:

1. Scan all historical source roots read-only.
2. Build a catalog.
3. Build exact duplicate groups.
4. Generate a duplicate trash plan.
5. Generate organization proposals for unprocessed folders.
6. Review the plan.
7. Apply in batches.
8. Re-scan and verify no files disappeared unexpectedly.

After migration, normal ongoing use should only process unprocessed ingest folders and travel SSD imports.

## 18. AWS Deep Glacier Backup

Future workflow:

```text
curator glacier-plan --library /Volumes/HomeServer --out .curator/plans/glacier-2026-06.json
```

The command should generate immutable backup manifests for the home server and help track what has already been archived.

This should be designed after local ingest, organization, and dedupe are trustworthy.

## 19. Open Questions

- Should Curator require `exiftool` for full production scans, or keep it optional with macOS metadata fallbacks?
- Which EXIF/media fields should become part of the exact duplicate key across RAW, JPEG, HEIC, MOV, MP4, and sidecars?
- Should ingest always preserve a raw card copy, or can successful ingest immediately organize and remove the raw ingest staging copy?
- How should sidecar files be associated with originals?
- How should Lightroom catalogs, XMP sidecars, Photoshop files, and Premiere project files be represented?
- What confidence threshold should allow automatic location naming without review?
- How should Curator detect mounted SD and CFexpress cards on macOS: Disk Arbitration, `diskutil`, filesystem heuristics, or all of the above?
- Should Curator eventually run as a restricted macOS user for stronger OS-level safety?
- What should the exact policy be when duplicate files are found inside already curated `Originals/`?
- How should NTFS read/write behavior be handled during migration before drives are reformatted as ExFAT?
