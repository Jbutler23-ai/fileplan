# fileplan

fileplan previews how a folder could be organized by file extension. It lists
each proposed destination and flags names that would collide. The current
version only makes a plan; it does not move, rename, delete, or open file contents.

It's a small command-line tool for people sorting their own project files or
downloads. It uses Python 3.10 or newer and has no runtime dependencies.

## Install

```sh
git clone https://github.com/Jbutler23-ai/fileplan.git
cd fileplan
python -m pip install .
fileplan --help
```

You can also run `python fileplan.py` directly. For development, install with
`python -m pip install --editable .`.

## Try the example

```sh
python fileplan.py examples/inbox
python fileplan.py examples/inbox --recursive
python fileplan.py examples/inbox --recursive --strict --format json
```

The first command plans three files. The recursive scan also finds the files
under `assets` and `drafts`. Two files are named `notes.txt`, so both get marked
as conflicts. The last command returns exit status 1 because of those conflicts.
All example files are made-up samples.

## How destinations are chosen

A preview places each filename under `_organized/<extension>` inside the scanned
folder. For example:

```text
notes.txt           -> _organized/txt/notes.txt
photos/cover.JPG    -> _organized/jpg/cover.JPG
CHECKLIST           -> _organized/no-extension/CHECKLIST
```

Only the final extension is used, so `archive.tar.gz` goes in `gz`. The filename
is kept as written; only the extension folder is lowercased. Recursive scans
combine files from different subfolders into these groups.

A destination is marked as a conflict if it already exists, if another source
would use the same destination, or if its parent cannot be used as a directory.
Name comparisons ignore case on every platform, so the preview also catches
conflicts that would matter on a case-insensitive filesystem.

## Options

| Option | Purpose |
| --- | --- |
| `ROOT` | Folder to inspect |
| `--recursive` | Include files in subfolders |
| `--include-hidden` | Include hidden entries, except excluded metadata folders |
| `--format plain\|json` | Choose readable text or JSON; defaults to text |
| `--strict` | Return exit status 1 if any destination conflicts are found |

Without `--recursive`, only files directly inside `ROOT` are planned. Symlinks
and Windows reparse points are skipped. `_organized`, version-control metadata,
virtual environments, and common dependency/cache directories are excluded.
There are no custom rules or `.gitignore` integration yet.

The root itself must be a real directory, rather than a link or reparse point.
On Windows, extension groups that would use a reserved or invalid directory name
are marked as conflicts.

Exit status is 0 for a completed preview, 1 for conflicts with `--strict`, and 2
for invalid input or a scan error. Errors go to stderr, with no partial plan on
stdout. Hidden entries are skipped by default; the plan lists skipped entries
with their reasons.

## JSON output

```sh
fileplan ./my-folder --recursive --format json > fileplan-report.json
```

The report includes `schema_version`, `root`, `destination_root`, `entries`,
`summary`, and `skipped`. Each entry contains a relative source and destination,
the file size, its status, and a conflict reason when applicable. Paths use
forward slashes within the plan. Entries are sorted to keep results predictable.

Reports contain filenames and an absolute root path. Keep reports from private
folders out of public repositories. No network requests are made.

## Current limits

This is an early preview tool. A plan is a snapshot: files or destinations may
change after it is produced. It does not prove that a future move would succeed,
check available disk space, or identify duplicate contents. There is no apply or
undo command yet.

The planned grouping is intentionally simple. Custom rules, explicit conflict
choices, and safe execution are the next pieces; see [ROADMAP.md](ROADMAP.md).

## Development

```sh
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

The test workflow covers Windows, Linux, and macOS with Python 3.10, 3.12, and
3.14, plus installation checks. Tests use temporary sample folders.

## License

[MIT](LICENSE).
