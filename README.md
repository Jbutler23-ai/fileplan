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
combine files from different subfolders into these groups. A rules file can
choose different folders; see [Custom rules](#custom-rules).

A destination is marked as a conflict if it already exists, if another source
would use the same destination, if its parent cannot be used as a directory, or
if the plan would need the same path to be both a file and a folder, such as
`docs/a.txt` alongside `docs/a.txt/b.md`. Name comparisons ignore case on every
platform, so the preview also catches conflicts that would matter on a
case-insensitive filesystem.

## Options

| Option | Purpose |
| --- | --- |
| `ROOT` | Folder to inspect |
| `--recursive` | Include files in subfolders |
| `--include-hidden` | Include hidden entries, except excluded metadata folders |
| `--rules RULES` | Choose folders by extension and filename pattern from a JSON file |
| `--check-duplicates` | Compare the contents of files that would share a destination |
| `--format plain\|json` | Choose readable text or JSON; defaults to text |
| `--strict` | Return exit status 1 if any problems are found |
| `--verify PLAN` | Compare a saved plan with the folder now; still moves nothing |
| `--apply` | Move the files in a verified plan; nothing moves without it |
| `--undo RECORD` | Put the files in a run record back where they were |

Without `--recursive`, only files directly inside `ROOT` are planned. Symlinks
and Windows reparse points are skipped. `_organized`, version-control metadata,
virtual environments, and common dependency/cache directories are excluded.
There is no `.gitignore` integration yet.

The root itself must be a real directory, rather than a link or reparse point.
On Windows, extension groups that would use a reserved or invalid directory name
are marked as conflicts.

Exit status is 0 for a completed preview, 1 for conflicts with `--strict`, and 2
for invalid input or a scan error. Errors go to stderr, with no partial plan on
stdout. Hidden entries are skipped by default; the plan lists skipped entries
with their reasons.

## Custom rules

A JSON rules file chooses the folder under `_organized` for each file. Rules
only ever choose the folder: the filename is never changed, and every folder
stays inside `_organized`.

```sh
fileplan examples/inbox --recursive --rules examples/rules.json
```

```json
{
  "schema_version": 1,
  "rules": [
    {"name": "invoices", "extensions": ["pdf"], "patterns": ["invoice-*"],
     "folder": "finance/invoices"},
    {"name": "images", "extensions": [".jpg", "jpeg", "png"], "folder": "images"},
    {"name": "readmes", "patterns": ["readme*"], "folder": "notes"}
  ],
  "fallback": "by-extension"
}
```

Each rule has a unique `name`, a `folder`, and at least one of `extensions` and
`patterns`. Rules are tried in the order written, and the first rule whose
every condition holds chooses the folder. `extensions` match the final
extension without regard to case; a leading dot is allowed, and the word
`no-extension` matches files that have none. `patterns` are shell-style
wildcards (`*`, `?`, `[...]`) matched against the filename only, without regard
to case. When a rule lists both, a file must satisfy both. A folder may be
nested, such as `finance/invoices`.

Files no rule matches follow `fallback`: `"by-extension"`, the default, groups
them by extension as usual; `{"folder": "other"}` sends them all to one folder.
Each entry in the plan records which rule chose it under `rule`, which is
`null` for extension grouping and `"fallback"` for the fallback folder; the
plain output shows it after each line.

A rules file is checked completely before any plan is made, and an invalid one
is refused with exit status 2 and a message naming the rule and the problem.
Folders are refused if they could leave `_organized`: a leading slash, `..`, a
backslash, a colon, an empty part, or a control character. A folder may not be
named `.fileplan`, which holds the run records. Unknown keys are refused too,
so a misspelled key cannot silently do nothing.

A plan stores the rules it was built with, so `--verify` and `--apply` use
exactly those rules and `--rules` is refused alongside them. A saved plan whose
stored rules are invalid is refused before anything is rebuilt. A plan whose
destination does not match what its own rules would choose is reported as a
`mismatch`, and apply refuses it.

## Resolving conflicts

Two files that would land on the same name are both marked as conflicts, and
the plan says why. Two things help decide what to do about them.

### Comparing contents

By default no file is ever opened. With `--check-duplicates`, the contents of
files that would share a destination, with each other or with a file already
there, are read and compared. Nothing else is read. Each compared entry gains
`sha256`, `identical_to`, and `differs_from`, and its reason says which files
hold the same bytes and which merely share a name:

```text
[conflict] "one/notes.txt" -> "_organized/txt/notes.txt" ("Multiple source files share this destination; Contents identical to two/notes.txt; Contents differ from three/notes.txt")
```

A file is checked before and after it is read; one that changes meanwhile is
reported as such with no hash. Links are never followed.

Once a file's contents have been compared, they are compared again every time
before it moves. `--verify` re-reads it even if a choice has since taken it out
of every conflict, and `--apply` reads it once more through the same folder
handle immediately before the move. A file whose bytes changed is reported as
`changed` and the run is refused, **even when its size and modification time
are identical to what the plan recorded**. The hash travels into the run
record, so `--undo` applies the same test to the stored file before putting it
back. This is the only check that cannot be fooled by a forged timestamp, and
it applies only to files that were compared; everything else is still judged on
size and modification time.

A plan remembers whether contents were compared, so `--verify` repeats the
comparison the same way and `--check-duplicates` is refused alongside a saved
plan.

### Choosing what happens

A reviewed plan can carry a `choice` on any entry: `keep`, `skip`, or
`rename`. Edit the saved JSON, then verify and apply as usual.

| Choice | Effect |
| --- | --- |
| `keep` | This file goes to the planned name. The others sharing it must step aside |
| `skip` | This file is left where it is and is not moved |
| `rename` | This file is moved to the first free numbered name, such as `notes (2).txt` |

Two files that share a name are resolved by one `keep` and one `skip` or
`rename`, or by renaming both. A file whose destination already exists can be
skipped or renamed, but not kept, since nothing is ever overwritten. Any clash
not resolved by choices stays a conflict, and `--apply` refuses the plan.

A renamed file's number is chosen when the plan is verified or applied,
skipping names taken on disk or by other entries in the plan, and the output
shows it. The plan keeps the name the file was first given under
`renamed_from`. Entries skipped by choice are reported as `skipped`, are not
counted as problems, are not moved, and do not appear in the run record, so an
undo leaves them alone. A skipped file is still checked: the choice to skip it
was made about the file as it was, so if it has changed it is reported as
`changed` and the run is refused rather than the choice being honoured blindly.

## JSON output

```sh
fileplan ./my-folder --recursive --format json > fileplan-report.json
```

The report includes `schema_version`, `root`, `destination_root`, `options`,
`entries`, `summary`, and `skipped`. Each entry contains a relative source and
destination, the file size, its modification time in nanoseconds, its status,
the rule that chose the destination, the choice made for it if any, and a
conflict reason when applicable. The size and modification time are what
`--verify` later compares against. `options` records the scan settings, the
rules in force, and whether contents were compared, so the same plan can be
rechecked the same way. Paths use forward slashes within the
plan. Entries are sorted to keep results predictable.

Reports contain filenames and an absolute root path. Keep reports from private
folders out of public repositories. No network requests are made.

## Checking a saved plan

A plan describes one moment. Before acting on one, `--verify` rescans the folder
and reports what no longer matches. It reads the same metadata as a preview and
changes nothing.

```sh
fileplan ./my-folder --recursive --format json > fileplan-report.json
fileplan ./my-folder --verify fileplan-report.json
fileplan ./my-folder --verify fileplan-report.json --strict
```

Each planned file is reported as one of:

| Status | Meaning |
| --- | --- |
| `unchanged` | Same size and modification time, and the destination is still free |
| `changed` | The file was written to after the plan was made |
| `missing` | The file is gone, or is no longer something the scan will plan |
| `mismatch` | The plan names a destination the current rules would not pick |
| `conflict` | The file is unchanged but its destination is no longer free |
| `new` | The file appeared after the plan was made and is not in it |

Each entry also carries a `destination_conflict` field, which is the conflict
reason or null. It is set on `changed` and `new` files whose destination is
blocked, not only on entries with the `conflict` status. The `conflicts` total
in the summary counts every entry with a destination conflict, so a changed
file whose destination now exists, or a new file whose destination is already
claimed, is included.

The `reason` text joins every finding for an entry, so a changed or new file
also shows its destination conflict there. The scan options come from the plan,
so `--recursive` and `--include-hidden` are rejected alongside `--verify`. A
plan is refused if it is not valid JSON, not this schema version, or was made
for a different folder. With `--strict`, exit status is 1 when anything other
than `unchanged` is reported.

Size and modification time show that a file looks untouched; they do not prove
it. A file can be rewritten with the same size and timestamp, and clocks and
timestamp resolution differ between filesystems. Nothing stops a file from
changing in the moment after the check.

## Rules that must hold

Applying and undoing follow a small set of settled rules. Every change to the
tool must keep them true, and each has tests that fail if it stops holding.
[ROADMAP.md](ROADMAP.md) states them in full.

- **Nothing moves unless it is asked for.** Previewing and verifying never
  change a file, create a folder, or write a record.
- **One way to move a file.** A single no-replace rename through folder handles
  opened without following links. If that is unavailable, stop; never fall back
  to a method that could replace a file or follow a link.
- **Never overwrite, never leave the folder.** Destinations are refused if
  taken, compared without case, and every path is checked to stay inside the
  selected folder.
- **Never delete to tidy up.** A name is only removed when it has been shown to
  be the file we put there. A failed step reports every name the file now has
  and leaves them all alone.
- **Stop on anything unexpected.** Missing, changed, mismatched, conflicting, or
  unknown all stop the run rather than guessing.
- **Say what happened, including when unsure.** Every planned file is reported
  as moved, moved but unconfirmed, not moved, unknown, or not attempted. A file
  that moved is never reported as not moved.
- **Confirm a file at its original name before calling it restored.** Undo
  reports a file as put back only after finding it under its original name,
  reached by path from the selected folder, matching the size and modification
  time in the record. An entry that merely looks already restored is checked
  the same way before it is passed over.
- **A damaged record is an incomplete record.** If an entry cannot be written
  whole, it is rolled back so the file never holds a broken line. If a
  half-written line cannot be removed, nothing more is written to that record,
  and the run is reported as not complete and not safe to undo from.

## Applying a plan

Nothing is moved unless `--apply` is given. It takes a plan that has already
been reviewed, so it is always used together with `--verify`:

```sh
fileplan ./my-folder --recursive --format json > fileplan-report.json
fileplan ./my-folder --verify fileplan-report.json
fileplan ./my-folder --verify fileplan-report.json --apply
```

Applying runs in two stages:

1. The whole plan is verified first. If any entry is missing, changed,
   mismatched, conflicting, or new, the run is refused and no file is moved.
2. Each file is then checked again immediately before its own move. Its size,
   modification time, and type must still match the plan, and its destination
   must still be free. The first problem stops the run.

Every planned file is listed with what became of it, so a run that stops
partway says exactly where things stand:

| Outcome | Meaning |
| --- | --- |
| `moved` | The file is at its destination and was confirmed there |
| `moved, unconfirmed` | The move happened, but a look afterwards found a problem |
| `not moved` | Nothing happened; the file is still where it was |
| `unknown` | The move failed in a way that leaves its result unclear |
| `not attempted` | The run stopped before reaching this file |

The closing line counts each outcome, and anything unconfirmed or unknown is
called out for you to look at. Files already moved stay where they were put,
and `--undo` puts them back.

### Record of each run

Every run that reaches the point of moving files writes a record as it goes,
in a new file under `_organized/.fileplan/` inside the scanned folder. The
output names the file. It is one JSON object per line:

- `start`, with the folder, the plan file, the scan options, and the count.
- `intent`, written and flushed to disk before each file is moved, with the
  source, destination, size, and modification time the plan recorded.
- `done`, written as soon as that file's outcome is known.
- `end`, with whether the run completed, where it stopped, and every outcome.

A run that is cut off leaves a record whose last line is an `intent` with no
`done`, which says exactly which file was in flight. If the record cannot be
started, nothing is moved. If a file's `intent` cannot be written, that file is
not moved and the run stops. If a `done` cannot be written after a move, the
run stops and says so. The record is created with a fresh name every time,
readable only by you, and no existing file is ever reused or overwritten.
Previews and verification never write a record, and `_organized` is excluded
from scans, so records never appear in a plan.

Each entry is written whole or not at all: a write that fails partway is rolled
back so the file never holds a broken line. A record is counted as complete
only when its closing entry was written and no damage occurred; if a
half-written line cannot be rolled back, the record is reported as damaged,
nothing more is written to it, and it is therefore incomplete. A run whose record is incomplete is not reported as
completed, because such a record cannot be relied on to undo the run. Reading a
record back applies the same rule: one with no closing entry, or with an
unfinished last line, is reported as incomplete before the undo begins.

## Undoing a run

`--undo` reads a record and puts each file back under its original name:

```sh
fileplan ./my-folder --undo ./my-folder/_organized/.fileplan/run-....jsonl
```

It works on a completed run and on one that was cut off partway, so it doubles
as recovery. Every entry is inspected first, from what is actually on disk
rather than from what the record claims, and nothing moves unless all of them
can be put back safely:

| Outcome | Meaning |
| --- | --- |
| `restored` | Put back under its original name |
| `restored, unconfirmed` | The move happened, but a look afterwards found a problem |
| `nothing to do` | Already at its original name, so it never moved |
| `not restored` | Nothing happened; the file is still in the organised folder |
| `unknown` | The move failed in a way that leaves its result unclear |
| `blocked` | Cannot be put back safely, so the whole undo was refused |

An entry is blocked when a file sits in both places at once, when it is in
neither, or when the file no longer matches the size and modification time that
were recorded, which means it is not the file that was moved. That applies to
both ends: a file already sitting under its original name is only passed over
once it has been confirmed to be the recorded file, and it is checked again
when its turn comes rather than trusted from the first pass.

A file is reported as restored only after it has been found under its original
name, reached by path from the selected folder, matching the size and
modification time in the record. Anything short of that is reported as
`restored, unconfirmed` with the reason. Files are put back newest first, using
the same single no-replace rename, so nothing is overwritten and nothing leaves
the folder. The undo writes its own record, so an undo can itself be undone.

A record is refused if it is not valid, was made for a different folder, or
names a path that could leave the folder. If its last line is unfinished, which
is what a run cut off mid-write leaves behind, the undo says so and works from
the whole entries before it.

In JSON the same information is under `results`, one entry per planned file
with its `outcome` and `reason`, alongside a `summary` that counts them.
`summary.moved` counts every file that left its original name, confirmed or
not; `summary.moved_confirmed` counts only the confirmed ones.

A destination is never overwritten, and a folder swapped for a link after the
check is never entered. There is exactly one change fileplan makes to disk: the
Linux `renameat2` system call with its no-replace flag. It gives the file its
new name and removes the old one in a single step, and the kernel refuses the
whole step if the new name has been taken in the meantime. The file therefore
has exactly one name at every moment, and nothing at the destination can be
replaced. Every other step acts through directory handles that were opened
without following links, so each step works on the folder that was checked
rather than on whatever its name points to now.

The checks sit on both sides of that one step. Immediately before it, the
source is looked at once more and the destination folder's position under the
scanned folder is confirmed once more. Immediately after it, the folder's
position is confirmed again, the old name must be gone, and the new name must
refer to the checked file with the size and modification time the plan
recorded. Anything unexpected stops the run with a report; none of these looks
changes anything on disk, and fileplan never deletes.

When that call is unavailable, the run stops; no other method is tried. Only
Linux offers it, so applying is refused up front on other systems. A
filesystem that does not support the no-replace flag, including some network
and FUSE filesystems, stops on the first file with a message saying so. Moving
onto another drive or filesystem stops the same way. Previewing and verifying
work everywhere.

One gap remains and cannot be closed with the tools Python provides: a folder
moved in the instant between the final position check and the rename receives
the file. The file keeps its one name, nothing is replaced, and the report says
where it now is, but it is no longer under the scanned folder.

Names are compared without case, so a move is refused on a case-insensitive
filesystem when only the capitalisation differs. Destination folders are
created as needed, stepping down from the scanned folder, and a move is refused
if any of them is a link or is not a directory. Every destination must sit
inside `_organized` within the scanned folder; a plan that names a path outside
it, or that uses `..`, an absolute path, or a backslash, is rejected before
anything is inspected.

Exit status is 0 when every file moved and the record was finished, 1 when
the run was refused or stopped, and 2 for invalid input. `--undo` uses the
same three.

A source that changes, a name that appears, or a folder that becomes a link in
the moment between the check and the move stops the run, within what size and
modification time can show. Nothing is copied, nothing is deleted, and no
fallback method is used when the safe one is unavailable.

## Current limits

A plan is a snapshot: files or destinations may change after it is produced.
`--verify` reports what no longer matches and `--apply` rechecks each file
again, but neither proves a move will succeed. The tool does not check
available disk space. Matching names are never treated as matching contents;
`--check-duplicates` is how contents are compared, and only when asked.

`--undo` puts a run back, but it cannot recover a file that something else has
replaced or deleted in the meantime; those entries are reported and the undo is
refused rather than guessing.

See [ROADMAP.md](ROADMAP.md) for what remains.

## Development

```sh
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

The test workflow covers Windows, Linux, and macOS with Python 3.10, 3.12, and
3.14, plus installation checks. Tests use temporary sample folders. Outside
Linux, the apply tests that move files are skipped and the refusal is tested
instead.

## License

[MIT](LICENSE).
