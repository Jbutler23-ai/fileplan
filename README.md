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
| `--resolve` | Settle each naming conflict at the prompt: keep, skip, or rename |
| `--show [RECORD]` | Say what a run did and where its files are now; changes nothing |

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
`rename`. Answer them at the prompt with `--resolve`, or edit the saved JSON
and then verify and apply as usual.

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

### Answering in the terminal

`--resolve` asks about each conflict in turn and takes the same three answers,
so nothing has to be written into JSON by hand:

```sh
fileplan ./my-folder --recursive --resolve --apply
```

```text
3 naming conflicts to settle in "/home/you/my-folder".

Conflict 1 of 3: "one/notes.txt" -> "_organized/txt/notes.txt"
  Multiple source files share this destination
  Also wanting this name: "three/notes.txt", "two/notes.txt"
  [k] keep this name   [s] skip this file   [r] rename to a free number   [q] stop
Choose for "one/notes.txt" [k/s/r/q]: k
```

Answer with the first letter or the whole word, in any case; anything else is
asked again. The questions and answers go to stderr, so `--format json` still
writes a clean plan to stdout.

Keep is checked the moment it is given. Only one file can have a name and
nothing is ever overwritten, so keep cannot work when the name already exists
on disk, when another file has already kept it, or when something else blocks
it. Then the prompt says why and asks about the same file again, offering only
skip and rename:

```text
Choose for "notes.txt" [k/s/r/q]: k
  Keep cannot work: Destination already exists: _organized/txt/notes.txt. Nothing is ever overwritten, so choose skip or rename instead.
  [s] skip this file   [r] rename to a free number   [q] stop
Choose for "notes.txt" [s/r/q]: r
```

Each answer is remembered and the plan is built from the folder again after
every round, so a later question reflects the answers already given. If an
answer still did not settle its file once the folder is looked at again, the
prompt says so and asks about that file again without that answer. Skip
always settles a file, so the questions always end with every conflict
settled, unless you stop them.

On its own, `--resolve` only previews: the settled plan is printed and nothing
is moved, and the answers are not saved unless the plan is written out with
`--format json`. With `--apply`, the settled moves are listed and one question
is asked before anything happens:

```text
Settled 3 of them:
  keep    "one/notes.txt" -> "_organized/txt/notes.txt"
  skip    "three/notes.txt" (left where it is)
  rename  "two/notes.txt" -> "_organized/txt/notes (2).txt"
2 files to move, 1 to leave alone.
Move them now? [y/N]:
```

Only `y` or `yes` moves anything. Anything else, an empty answer, the end of
input, `q` at any question, or Ctrl-C leaves the folder untouched and exits
with status 1. The plan is verified against the folder once more as it is
applied, exactly as a saved plan is, and the run writes the same record, so it
can be undone the same way. `--resolve` makes a new preview, so it cannot be
combined with `--verify` or `--undo`.

## Names in the output

Plain output prints every name and path between double quotes, exactly as it
is, so one can be copied straight into the next command. Emoji, accents, and
Windows backslashes appear as they are:

```text
Record: "C:\Users\you\inbox 📥 ✨\_organized\.fileplan\run-20261108T221532355668Z-19508.jsonl"
```

Only a name holding something that cannot be shown safely is written with
backslash escapes instead, and then it reads exactly like a JSON string: a
newline or other control character, a bidirectional control such as U+202E
that could make one name look like another, or bytes that were not valid text.
So is a name containing a double quote, or, outside Windows, a backslash, so
that a backslash escape always means the name was escaped. Windows allows
neither character in a name, so there a backslash is only ever a separator.

When output goes to a pipe or a file it is written as UTF-8, so names arrive
whole whatever the system's code page. A Windows console already shows
Unicode and is left as it is, and `PYTHONIOENCODING`, if set, decides instead.
Windows PowerShell 5.1 decodes what it captures or pipes from a program using
the console's code page, which has no emoji, so run this once in the session
first:

```powershell
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
```

`--format json` writes plain ASCII with every name escaped, so it reads the
same everywhere.

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

- **Nothing moves unless it is asked for.** Previewing, verifying, and
  looking at a run never change a file, create a folder, or write a record.
  Answering conflicts in the terminal only shapes the plan; `--apply` and a
  yes to the one confirming question are still what move a file.
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

- `start`, with the folder, the plan file, the scan options, how many files
  were planned, and how many of those a choice leaves alone.
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

### Seeing what a run did

`--show` reads a record and says what that run did and where each of its files
is now. It is how to find out what an interrupted run left behind, since a run
that is cut off never gets to print anything:

```sh
fileplan ./my-folder --show
fileplan ./my-folder --show ./my-folder/_organized/.fileplan/run-....jsonl
```

With no path it uses the newest record in the folder, which is usually the run
that just stopped. Nothing is moved, created, or written.

```text
Run record "/home/you/my-folder/_organized/.fileplan/run-20261108T161936140045Z-6412.jsonl"
From applying a plan in "/home/you/my-folder", started 2026-11-08T16:19:36.140121+00:00
It set out to move files into the organised folder.
The run was cut off: the record has no closing entry, so it never reached the end.
Planned 5 files; the record covers 3. 2 files were never started.
The record confirms 2 of its 3 files moved, and 1 was left with no outcome.
[moved] "a.txt" -> "_organized/txt/a.txt" (the file is there now)
[moved] "b.txt" -> "_organized/txt/b.txt" (the file is there now)
[no outcome] "c.txt" -> "_organized/txt/c.txt" (the file is at "c.txt" now; the record has no outcome for it, so whether it moved is not known)
In flight when it stopped: "c.txt". The line above says where that file is now.
--undo on this record would move 2 files.
Look only. No files were changed.
```

Each line gives what the record says happened and, separately, where the file
is on disk now. The two can differ, and that is the point: a run cut off
between moving a file and writing its outcome leaves an entry with no outcome
whose file is already at its destination, while one cut off before the move
leaves an entry with no outcome whose file is still at its original name. The
report says where such a file is, and nothing more: with no outcome written,
where the file is now does not show whether it ever moved. The file named by
`In flight when it stopped` is the one that was being moved at that moment.

A run that has since been undone is reported as undone, never as if its files
had not moved. `--show` finds the undo's own record in the same folder and
says which files it put back:

```text
[moved] "a.txt" -> "_organized/txt/a.txt" (put back at "a.txt" by the undo in "run-20261108T171656410772Z-6501.jsonl")
Undone since by "run-20261108T171656410772Z-6501.jsonl", which finished; its record confirms it moved 3 of this run's 3 files out of the organised folder.
Nothing is left to undo: every file is back where this run found it, outside the organised folder.
```

Starting an undo is not the same as finishing one. An undo is only called
finished when its own record is complete and says it completed. One that was
cut off, or whose record never closed, or that stopped early is reported as
not finished, with how many moves its record confirms:

```text
[moved] "b.txt" -> "_organized/txt/b.txt" (the file is there now; the undo in "run-....jsonl" was cut off while moving it out of the organised folder)
[moved] "c.txt" -> "_organized/txt/c.txt" (put back at "c.txt" by the undo in "run-....jsonl")
An undo was started in "run-....jsonl" but its record was never closed, so it did not finish; its record confirms it moved 2 of this run's 4 files out of the organised folder, and it was moving 1 more when it stopped, with no outcome written.
--undo on this record would move 2 files and leave 2 alone.
```

A file is only called put back when the undo confirmed it and the file is at
its original name now. A file the undo moved but could not confirm, or tried
and failed to move, is reported that way. So is one that was put back and
has been moved into the organised folder again since.

`did not move` is only said when the record itself says the file did not
move. A file with no recorded outcome is never said to have stayed, wherever
it is found. A file the record says moved, found back at its original name
with no undo record to explain it, is reported as back with that said plainly.
A file whose outcome was `unknown` stays unknown: the run could not tell
whether it moved, so neither does this.

A record in the folder that cannot be read is named in the report with the
reason, rather than silently left out. So is a readable undo whose chain of
undone records cannot be traced to a readable run, because a record above it
cannot be read, is no longer in the folder, or loops: it may be an undo or
redo of this run, so it is named with what broke the trace, but nothing is
counted from it, since its place in the chain would be a guess. Either way
the report stops short of claiming that no undo record exists: a file found
back at its original name is said to have no *known* undo record to say why,
the JSON report lists each unreadable record under `unreadable_records` and
each untraceable run under `unplaced_runs`, and its summary says
`"history_complete": false`. A record that is malformed in any way is refused
with what is wrong with it; it never stops `--show`, `--undo`, or anything
else with a crash. That includes a record whose kind contradicts the rest of
it, such as an apply that names a record it undid: a redo whose kind was
damaged to `apply` looks just like that, so it is named as unreadable, along
with the runs after it, rather than taken for an apply and dropped from the
chain.

### Undo, redo, and undo again

An undo can itself be undone, which moves its files back into the organised
folder: a redo. That can be undone too, and so on. `--show` follows the whole
chain from the run it is shown, however deep, and lists every run in it in the
order they happened:

```text
Undone since by "run-A.jsonl", which finished; its record confirms it moved 4 of this run's 4 files out of the organised folder.
"run-A.jsonl" was itself undone by "run-B.jsonl", which finished; its record confirms it moved 4 of this run's 4 files into the organised folder.
"run-B.jsonl" was itself undone by "run-C.jsonl", which finished; its record confirms it moved 4 of this run's 4 files out of the organised folder.
```

Every direction in the report is physical: into the organised folder or out
of it, read from that run's own entries. That matters when the record being
shown is itself an undo, which moved files out; undoing it moves them back in,
and the report says so, rather than assuming the record shown is an apply. The
header says which way the record shown set out to move its files, and for an
undo, which record it undid:

```text
From undoing "run-20261108T221318025177Z-1833453.jsonl" in "/home/you/my-folder"
It set out to move files out of the organised folder.
...
Undone since by "run-....jsonl", which finished; its record confirms it moved 2 of this run's 2 files into the organised folder.
Nothing is left to undo: every file is back where this run found it, in the organised folder.
```

Each run's count comes from that run's own record, which is fixed once that
run ends. A later undo or redo adds a line; it never changes a line already
there, so a count shown once reads the same however many undos and redos
follow.

### Only what a record confirms

Nothing in the report is credited as moved unless a record confirms it. The
header says which way a run *set out* to move files, since a run can be cut
off or fail before moving any. Each count of moves is a count of confirmed
moves; a move a run made but could not confirm, and the move a run was making
when it was cut off, are named separately rather than added in:

```text
The record confirms 1 of its 2 files moved, and 1 moved but could not be confirmed.
An undo was started in "run-....jsonl" but stopped early at "c.txt", so it did not finish; its record confirms it moved 1 of this run's 4 files out of the organised folder, and 1 more it could not confirm.
[moved] "c.txt" -> "_organized/txt/c.txt" (the file is at "c.txt"; the undo in "run-....jsonl" recorded moving it out of the organised folder but could not confirm it)
```

### Counts that do not drift

The counts work the same way. What the record says became of its files is
read from the record alone, so it reads the same however the files are moved
afterwards; undoing a run adds to a second set of counts rather than changing
the first:

```text
The record confirms 4 of its 4 files moved.
The record confirms 4 of its 4 files moved; since then undo records confirm 4 were moved back out of the organised folder.
```

In JSON, `recorded_moved`, `recorded_not_moved`, `recorded_unknown`, and
`no_outcome` come from the record and always add up to `entries`. A file is
counted under `recorded_moved` only if the record says it left its original
name, and `recorded_moved_unconfirmed` says how many of those the run could
not confirm; an `unknown` outcome is counted apart, never as moved and never as
stayed. `put_back` counts files any undo in the chain confirmed putting back,
and `moved_again` counts files any redo confirmed moving back in; both come
from those runs' records, so they only ever grow. `back_without_undo` counts
files the record says moved that are at their original name with no later run
to account for them. `at_destination`, `at_source`, and `unclear` say where
the files are now, and are the only counts that follow the disk.

The report's `moves` is the way the record shown set out to move its files: `in` to the
organised folder or `out` of it. `undo_runs` lists every later run, oldest
first, with the record it undid (`undoing`), its `depth` in the chain,
`reverses` (true when it moved files back to where the record shown found
them, false when it moved them where that record left them), its physical
`direction` (`in` or `out`, from its own entries), `completed`,
`record_complete`, `stopped_at`, `files`, the number of this run's files its
record confirms it moved, `unconfirmed`, the number it moved without
confirming, and `no_outcome`, the number it was moving when it stopped. Each entry gives `undo_record`,
`undo_direction`, `undo_reverses`, `undo_depth`, and `undo_outcome` for the
latest later run that tried it, `undone_by` only when a reversing run
confirmed moving it back and it is there now, and `redone_by` only when a
repeating run confirmed moving it again and it is there now.
`unreadable_records` lists every record in the folder that could not be read,
each with the `problem`. `unplaced_runs` lists every readable undo that could
not be traced to a readable run, each with the record it undid (`undoing`),
its physical `direction`, whether it `completed`, the `broken_link` where the
trace stopped (absent for a loop), and the `problem`. The summary's
`history_complete` is false while either list is not empty, since any of
those records could change what the chain and the counts above would say.

Files are looked at with the same checks an undo would make, so the last line
is also a forecast: how many `--undo` would move, and how many it would leave
alone. That includes reading the contents of any file whose contents were
compared when its plan was made, since the record carries the hash; no other
file is opened. An entry whose file is in both places at once, in neither, or no longer
the file that was recorded is reported as in doubt, and `--undo` would be
refused until it is settled. Files that a choice left alone are counted as
such rather than as files the run never reached.

`--show` reads a record and nothing else, so it cannot be combined with
`--apply`, `--undo`, `--resolve`, `--rules`, `--check-duplicates`,
`--recursive`, or `--include-hidden`. A record made for a different folder is
refused. Exit status is 0, or 2 if the record cannot be read; with `--strict`
it is 1 while the run is incomplete or anything is in doubt. `--format json`
gives the same facts under `entries` and `summary`.

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
the folder. The undo writes its own record, so an undo can itself be undone,
and that undo (a redo) undone in turn. An undo record moves every file the
same way, out of the organised folder or back into it; one that mixes the two
is refused.
If every file was dealt with but that record could not be closed, the undo
says so and is not reported as complete, and exits with status 1.

A record is refused if it is not valid, was made for a different folder, or
names a path that could leave the folder. If its last line is unfinished, which
is what a run cut off mid-write leaves behind, the undo says so and works from
the whole entries before it.

In JSON the same information is under `results`, one entry per planned file
with its `outcome` and `reason`, alongside a `summary` that counts them.
`summary.moved` counts every file that left its original name, confirmed or
not; `summary.moved_confirmed` counts only the confirmed ones.

A destination is never overwritten, and a folder swapped for a link after the
check is never entered. There is exactly one change fileplan makes to disk: a
rename that refuses to replace. On Linux that is the `renameat2` system call
with its no-replace flag; on Windows it is `NtSetInformationFile` with
`FileRenameInformation` and `ReplaceIfExists` turned off. Either one gives the
file its new name and removes the old one in a single step, and the system
refuses the whole step if the new name has been taken in the meantime. The file therefore
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

"The checked file" is decided by device and inode number, which only
identifies a file for as long as it is held open: a deleted file gives up its
number, and Linux can hand it straight to the next file created. So on Linux
the source is held open, without following links and without needing read
permission, from the look just before the move until the look just after, and
a replacement can never share its number. A source swapped in the instant
before the move is seen at that point and refused, so it is not moved at all.
Windows needs no such hold: its file IDs change when a slot is reused.

When that call is unavailable, the run stops; no other method is tried. Only
Linux and Windows offer it, so applying is refused up front on other systems,
including macOS. A filesystem that does not support the no-replace rename,
including some network and FUSE filesystems, stops on the first file with a
message saying so. On Windows, a file that another program holds open stops
the run the same way, and the file is left where it is. Moving
onto another drive or filesystem stops the same way. Previewing and verifying
work everywhere.

On Linux one gap remains and cannot be closed with the tools Python provides: a
folder moved in the instant between the final position check and the rename
receives the file. The file keeps its one name, nothing is replaced, and the
report says where it now is, but it is no longer under the scanned folder.
Windows closes this gap: fileplan opens each folder it works in without
allowing deletion, and while it holds that handle Windows refuses to rename,
move, or delete the folder. On Windows a link or junction found under the old
name at the moment of the move is refused rather than moved.

On Windows the run record is created with an access list that grants only its
owner and the system, the counterpart of mode 0600 elsewhere.

Names are compared without case, so a move is refused on a case-insensitive
filesystem when only the capitalisation differs. Destination folders are
created as needed, stepping down from the scanned folder, and a move is refused
if any of them is a link or is not a directory. Every destination must sit
inside `_organized` within the scanned folder; a plan that names a path outside
it, or that uses `..`, an absolute path, or a backslash, is rejected before
anything is inspected.

Exit status is 0 when every file moved and the record was finished, 1 when
the run was refused or stopped, and 2 for invalid input. `--undo` uses the
same three, and so does `--resolve --apply`, where stopping at a question or
answering anything but yes counts as refused.

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
3.14, plus installation checks. Tests use temporary sample folders. The apply
and undo tests run on Linux and Windows; on macOS they are skipped and the
refusal is tested instead. On Windows, the tests that create symlinks need
administrator rights or Developer Mode, and are skipped without them.

## License

[MIT](LICENSE).
