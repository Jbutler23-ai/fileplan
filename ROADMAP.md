# Next steps

The first version makes a read-only organization plan. Applying a plan, undoing
a run, and the record that connects them are now built. The work below is what
remains. Estimates include implementation, tests, and documentation.

## Rules that must hold

These are settled decisions, not open questions. Anything added later has to
keep them true, and each one has tests that fail if it stops being true.

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
- **Compared contents are compared again before moving.** A file whose contents
  were ever compared is re-read before it moves, on apply and on undo, and a
  change stops the run even when the size and modification time are unchanged.
  A choice to keep, skip, or rename does not exempt a file from this.
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
  half-written line cannot be removed, nothing more is written to that record.
  Either way the run is reported as not complete and not safe to undo from.

## Apply, undo, and the run record (done)

A plan can be verified, applied, and undone, and every run writes an
append-only record as it goes. A run cut off partway leaves a record that says
which file was in flight, and undo reads it to recover.

## Applying beyond Linux (2-3 hours left)

Applying needs a rename that refuses to replace. Linux and Windows both apply
and undo with the full guarantee; Windows goes through the NT API with folder
handles that also stop the folders being moved mid-run. Previewing and
verifying already work everywhere.

- Decide what macOS should do: refuse, or accept a stated weaker guarantee.
- Keep the refusal clear and tested wherever the guarantee cannot be met.

Done when every supported platform either applies with the full guarantee or
refuses in a way the user can act on.

## Custom organization rules (done)

A JSON rules file chooses folders by extension and filename pattern, first match
wins, with a stated fallback for unmatched files. Folders are validated to stay
inside `_organized`, invalid rules fail before any plan is made, plans store the
rules they were built with, and each entry names the rule that chose it.

## Duplicate and conflict choices (done)

`--check-duplicates` compares the contents of files that would share a name,
and only those, telling identical contents from a shared name. Those contents
are compared again before every move and before every undo, so a forged size
and timestamp cannot slip a changed file through. A reviewed plan can carry
keep, skip, or rename on each entry; unresolved clashes still refuse to apply.
A plan that would need one path to be both a file and a folder is a conflict.

## Settling conflicts in the terminal (done)

`--resolve` asks about each conflict at the prompt and takes keep, skip, or
rename as the answer, so the choices a plan can carry no longer have to be
written into JSON by hand. The answers only shape the plan: nothing moves
until `--apply` is given as well and the one confirming question is answered.
A keep that cannot work is refused on the spot with the reason, and the same
file is asked again with only skip and rename.

## Looking at a run (done)

`--show` reads a run record and says what that run did and where each of its
files is now, which is how to see what an interrupted run left behind: the
file that was in flight when it stopped, the ones that never started, and
whether an undo would be able to put everything back. A run that has been
undone says so, file by file, rather than calling its files unmoved, and an
undo is only called finished when its own record says it completed. What the
record says became of each file is counted from the record alone, so those
counts read the same however the files are moved afterwards. Undos, redos, and
undos of those are followed to any depth, and each is credited only with what
its own record says, so undoing and redoing again never changes a count
already shown. A file with no recorded outcome is never said to have stayed
put. Every direction it reports is physical, into the organised folder or out
of it, read from each run's own entries, so it reads correctly for an undo
record as well as an apply. Nothing is credited as moved unless a record
confirms it: a run's direction is what it set out to do, and moves that were
not confirmed or were cut off are named apart. It changes nothing.

Names in plain output are printed exactly as they are, emoji and Windows
backslashes included, and pipes and files get UTF-8, so a printed path can be
pasted back into the next command.

A record that cannot be read is named in the report with the reason instead of
being silently passed over, and so is a readable undo that cannot be traced to
a readable run because a record above it is unreadable, gone, or loops. While
either is true the report says its history may be incomplete rather than
claiming no undo record exists, and credits nothing from a run it cannot
place. A record malformed in any way is refused with a message saying what is
wrong, never a crash. That includes a record whose kind contradicts the rest of
it: an apply that names a record it undid is what a redo with a damaged kind
looks like, so it is refused and named rather than taken for an apply and
dropped from the chain along with every run after it.

## Testing status (as of 2026-11-13)

What has been run against the current code, and where:

- **Linux:** the full suite, 258 tests, passes. The 2 skips are the
  Windows-only tests.
- **Windows (real, not CI):** the full suite, 258 tests, passes with **no
  skips** when run as administrator. That run included all 7 tests that need
  to create symlinks, so the Windows link protections are now tested for real:
  a source swapped for a link mid-move is refused and left alone, a link at
  the destination is not read for comparison, a linked source is skipped, and
  undo and verification refuse links. Without administrator rights or
  Developer Mode those 7 skip, and everything else still passes.
- **A damaged redo marked as an apply** is tested on both: it is named as
  unreadable, the runs after it are named as unplaced, and the history is
  reported as incomplete.

Fixed along the way: the swapped-link test used to fail on Windows without
link rights instead of skipping, because it asked to skip from inside the
rename, and apply rightly reports anything raised there as a move whose
outcome is unknown. It now checks for link rights before the move starts.

How to run the tests on real Windows: the portable Python there has a `._pth`
file that always imports the copy in `fileplan-win\repo`, so a plain
`unittest discover` from any other folder silently tests old code. Put the
folder being tested first on `sys.path`; `run_elevated.cmd` in the latest
`%TEMP%\fileplan-applyundoing-*` copy does this, runs as administrator, and
writes `elevated.log`, whose first two lines say whether it was elevated and
whether a link could be made.

### Damaged-record fuzzer rerun (2026-11-13, done)

The fuzzer is `driver-fuzz\fuzz.py` in
`%TEMP%\fileplan-records-20261109-224507` (`fuzz2.py` beside it is
identical). It writes a record, puts each of 14 odd JSON values into every
field of every line in turn, and feeds the result through reading, `--show`,
and undo, counting any crash other than a clean refusal. That folder's own
`repo` copy is old code, so reruns use a fresh copy in
`%TEMP%\fileplan-fuzz-20261113-161814`, with the current `fileplan.py` in
`repo` and the original fuzzer copied unchanged.

1. **Original fuzzer, current code, Linux:** 392 cases, 0 crashes, but only
   2 of 393 record reads got past `load_record`. Against HEAD (before the
   redo fix) the same run had 379. The cause is the fuzzer, not the code: its
   template apply record carries `"undoing": null`, which fileplan never
   writes, and an apply that has that field is now refused as a possible
   damaged redo. So nearly every case is refused at the first step and never
   reaches `--show` or undo. **A clean result from the original fuzzer no
   longer means much;** the template needs fixing.
2. **Corrected fuzzer, `driver-fuzz\fuzz3.py`** in the new folder (the
   original is left as it was). Same mutations, but the apply template has
   no `undoing` field, each case is classed as refused at reading or run all
   the way through `--show` and undo, and 15 new cases give an apply an
   `undoing` field (each odd value plus a real-looking record name) and fail
   unless every one is refused.
3. **Corrected fuzzer, current code, Linux:** 394 cases, 0 crashes, all 15
   damaged-redo cases refused; 176 cases run all the way through, 217 are
   refused at reading, 1 later.
4. **Control, corrected fuzzer against HEAD, Linux:** 0 crashes, but all 15
   damaged-redo cases get through, so the new cases do catch the bug. 191
   run through, which is the 176 above plus exactly those 15: the fix
   refuses nothing it was not meant to.
5. **Real Windows, same code:** the original fuzzer gives the same 392
   cases, 0 crashes, 2 records read; the corrected one gives 394 cases, 0
   crashes, all 15 damaged-redo cases refused, and the same 176 / 217 / 1
   split as Linux.

Result: no crash on either system, and a redo marked as an apply is refused
whatever its link holds. The fuzzer lived outside the repo, which is how its
template drifted from real records without anyone noticing, so it is now part
of the test suite (below). The copies under `%TEMP%` are no longer needed.

### Damaged-record checks in the test suite (2026-11-13, done)

The corrected fuzzer is now `DamagedRecordFuzzTests` in
`tests/test_fileplan.py`, so CI runs it on every platform and Python version
in the matrix. It uses the same 394 cases as `fuzz3.py`, with made-up names.
Four tests:

- **The template is a usable record** (every platform): the untouched record
  reads back as a complete apply.
- **Every damaged record is read or refused cleanly** (every platform): no
  case may raise anything but a clean refusal, and all 15 applies that name
  a record they undid must be refused.
- **The template runs through `--show` and undo** (where moves work).
- **Every readable damaged record shows and undoes cleanly** (where moves
  work): every case that reads must get through the report, its JSON and
  plain output, and undo without a crash, and more than 100 cases must get
  all the way through, so a drifted template fails instead of passing
  quietly.

Checked by breaking the code on purpose, in a scratch copy, three ways; each
was caught and the failure named the case:

1. **Without the redo fix:** the 15 damaged-redo cases fail.
2. **With `"undoing": null` back in the template** (today's drift): both
   template tests fail, and the coverage floor fails with only 2 cases
   getting through.
3. **Without the check that an outcome is text** (fixed in `0fd52d8`): a
   list or dict outcome crashes `--show` with a `TypeError`, and the test
   fails naming those cases.

Results on the current code:

- **Linux, Python 3.14:** 262 tests pass (258 before plus these 4), 2
  Windows-only skips. The 4 run in about a tenth of a second.
- **Real Windows, Python 3.12:** 262 tests pass; the 7 skips are the link
  tests, run without administrator rights this time.
- **Simulated macOS** (safe moves switched off before the tests load): the
  2 reading tests run and pass, the 2 `--show` and undo tests skip.

### First CI run of these changes (2026-11-14)

Commit `6f5c6a2`, run
https://github.com/Jbutler23-ai/fileplan/actions/runs/43654829329.

- **macOS, all three Pythons:** pass. 152 skips, all expected: the 150
  tests that move files (applying is refused there) and the 2 Windows
  reserved-name tests. The new damaged-record reading tests ran and passed.
- **Windows, all three Pythons:** 6 failures, **0 skips**. GitHub's Windows
  runners can make links, so all 7 link tests ran and passed there. The 6
  failures are `--show` tests about unreadable records, which had never been
  through CI. The runner's account is `runneradmin`, so its temp folder is
  given in the 8.3 short form `C:\Users\RUNNER~1\...`, while fileplan
  reports record paths in the long form. The tests compared the two
  spellings, took them for different files, and damaged or deleted the
  wrong record. Reproduced locally by pointing the temp folder at an 8.3
  short name: the same 6 fail.
- **Ubuntu, all three Pythons:** 2 failures, 2 skips (the Windows-only
  tests). The failures are `test_apply_reports_a_file_replaced_right_after_the_move`
  and `test_apply_reports_a_source_swapped_for_a_link_as_it_moves`, and
  they have failed on Ubuntu since the first commit on 2026-11-01; every
  `main` run since then shows the same two. Both delete a file and
  immediately create another. Ubuntu's filesystem hands the freed inode
  number straight to the new file, so the identity check after the move
  sees the same file, and the swap is only caught one check later as a size
  change or a link. The run still stops and the move is still reported
  unconfirmed, but the reason says the file "is at" its destination when it
  was replaced. Not reproduced locally, where inode numbers are not reused
  that quickly; the cause follows from the order of the checks in
  `_confirm_move`.

### Fixing what CI found (2026-11-14, commit 861d516, CI green)

- **Windows:** `_ApplyFixture` now resolves its temp folders, so tests and
  fileplan spell paths the same way. A test-only fix.
- **Ubuntu:** fixed in the code, not by loosening the tests. Comparing inode
  numbers only proves identity while the file is held open, since a freed
  number can be reused. Apply and undo (both go through
  `_move_with_handles`) now hold the source open, through `_pin_source`,
  from the last check before the move until the check after it, so its
  number cannot be handed to a replacement and "refers to a different
  file" is reliable on any filesystem. On Linux the handle is `O_PATH |
  O_NOFOLLOW`, so it needs no read permission and never follows a link.
  Windows is left as it was: its file IDs carry a sequence number that
  changes when a slot is reused, and an extra handle could get in the way of
  the rename's delete access.
- **A gap this also closes:** a source swapped between the last check and
  the move used to be moved and reported unconfirmed. Holding it open shows
  the swap first, so it is now refused and nothing moves ("Source was
  replaced as it was about to be moved").
- **New tests**, Linux only: the source is held open during the move and
  during the check after it, and let go afterwards; and a source replaced
  just before it is held is refused. With the pin disabled in a scratch
  copy, both fail; the second shows the swapped file being moved.

Checked locally before pushing:

- **Linux, Python 3.14:** 264 tests pass (262 plus the 2 new), 2 skips.
- **Real Windows, Python 3.12, normal and 8.3 short-name temp folders:**
  264 pass both ways. 9 skips: the 7 link tests (no administrator rights
  this time; they ran on GitHub's Windows runners) and the 2 Linux-only
  pin tests.
- **Not reproducible locally:** Ubuntu's inode reuse itself. The Ubuntu CI
  jobs are the real check that the two long-failing tests now pass.

### Second CI run (2026-11-15, commit 861d516)

https://github.com/Jbutler23-ai/fileplan/actions/runs/43669581945. **All 9
jobs pass**, 264 tests each, on Python 3.10, 3.12, and 3.14. The Ubuntu
jobs are the first green Ubuntu jobs since the project's first CI run: the
two swap tests that had failed there since 2026-11-01 now pass, which
confirms the inode-reuse diagnosis and the fix. Skips, all expected:

- **Ubuntu:** 2, the Windows reserved-name tests.
- **Windows:** 2, the Linux-only hold tests. The 7 link tests ran and
  passed again, and the 6 `--show` tests that failed last time pass.
- **macOS:** 154, every test that moves files plus the 2 Windows-only
  tests. The damaged-record reading tests ran and passed.

The same two fixes were written twice, in two sessions working on this
branch at once; the second copy was set aside on the local branch
`backup/hold-fix-duplicate` rather than pushed over the first. Two things
from it were worth keeping and were added afterwards: a README passage on
the hold, and a Linux test that a file with no read permission still moves,
which is the reason the hold uses `O_PATH` rather than a read-only open.

Next, in order:

1. Check the CI run for the follow-up commit (README passage and the
   no-read-permission test).
2. The macOS decision above.

One gap is known and cannot be closed from the record alone: an undo whose
kind is changed to "apply" and whose link to the record it undid is removed
reads exactly like a real apply. It would also have to move files into the
organised folder, so only a redo that lost both fields gets through.

The remaining estimate is **2-3 hours**. These are proposed next steps, not
claims about past development time or a release schedule.
