# Handoff: where things stopped (2026-11-15)

A review of the work from 2026-11-12 to 2026-11-15, where it stopped, and how
to pick it up. ROADMAP.md has the full test log for each step; this file is
the short version plus what to do next.

## Where it stopped

- **The last code commit is `ad6c975`** ("Note that main is merged and green").
  Its CI is **green on all nine jobs** (Ubuntu, Windows, macOS on Python 3.10,
  3.12, 3.14):
  https://github.com/Jbutler23-ai/fileplan/actions/runs/43708564845.
  Before this work `main` had been red on every run since 2026-11-01.
- **Resume from `/home/jbutler23/projects/fileplan` on `main`.** This file,
  `PLAN-ignore-rules.md`, and the ROADMAP pointer are saved in a local
  documentation commit after `ad6c975`. No code changed in that commit and
  nothing was pushed. Read these files before starting the next change.
- **The branches do not all match.** At the end of this session, `origin/main` and the local
  `.../oak` branch are at `ad6c975`. The local `.../elm` branch has the
  unselected handoff commit `93b6fd3`; its remote-tracking branch is at
  `ad6c975`. The remote-tracking `.../oak` branch is at `3d83050`. Leave these
  branches in place; no reset is needed to continue from `main`.
- **One thing is undecided:** `origin/hfi/f1a6cffa-25a3-4b2c-b929-79bbc1c0f39a/oak`
  still has three commits that are not in `main`:
  - `1a34276` Record the failing CI run and what is known about it
  - `a6a5038` Describe the hold across a move and test that an unreadable
    file still moves
  - `3d83050` Merge the interim CI notes from origin

  The two notes commits are superseded by the notes in `main`. `a6a5038`
  adds a test that a file without read permission still moves, which is
  worth keeping: the hold added in `861d516` deliberately needs no read
  permission, and that test would catch it if it ever did. That description
  comes from the commit message and file list; the test itself has not been
  reviewed or run yet. The test is absent from `main` and local `.../oak`,
  but is available locally through the remote-tracking `origin/.../oak`
  branch named above. Inspect `a6a5038` before deciding whether to keep it.

## What was done

In order, with the commit that landed each part:

1. **A redo whose kind was damaged to "apply" was silently dropped from
   `--show`'s history** (`6f5c6a2`). A redo moves files into the organised
   folder just as an apply does, so it read back as a normal apply, and the
   history, which only follows undos, skipped it and every run after it. An
   apply never writes an `undoing` field, so a record with both is now
   refused, named as unreadable, its later runs named as unplaced, and the
   history marked incomplete.
2. **A Windows test failed instead of skipping without link rights**
   (`6f5c6a2`). It asked to skip from inside the mocked rename, and apply
   rightly reports anything the rename raises as an unknown outcome,
   SkipTest included. It now checks for link rights first.
3. **The 7 Windows link tests were run for real** as administrator on the
   Windows machine, all passing. GitHub's Windows runners can make links,
   so they run in CI too.
4. **The damaged-record fuzzer had quietly stopped testing anything.** Its
   template carried `"undoing": null`, so after step 1 nearly every case was
   refused on reading (2 of 393 got through, against 379 before). A
   corrected copy found no crashes on Linux or Windows.
5. **The fuzzer became part of the test suite** as `DamagedRecordFuzzTests`
   (`6f5c6a2`): 394 cases, reading checked on every platform, `--show` and
   undo where moves work, plus guards that fail if the template drifts
   again. Checked by breaking the code three ways; each was caught.
6. **The first CI run of this work failed, and so had every `main` run since
   2026-11-01.** Two causes:
   - **Ubuntu**, failing since the first commit: GitHub's Ubuntu filesystem
     hands a deleted file's inode number straight to the next new file, so
     a file replaced right after a move could pass the identity check.
   - **Windows**, new tests: the runner's temp folder is given in 8.3 short
     form (`C:\Users\RUNNER~1\...`), fileplan reports long paths, and six
     tests compared the two spellings and damaged the wrong record.
7. **Both were fixed** (`861d516`):
   - Apply and undo now hold the source open (`_pin_source`) from the last
     check before the move until the check after it, so its inode number
     cannot be reused. This also means a source swapped just before the
     move is refused, where before it was moved and reported unconfirmed.
     Windows is unchanged: its file IDs change when a slot is reused.
   - The apply test fixture resolves its temp folders. Test-only.
8. **CI went green** on the branch, then on `main` after a fast-forward to
   `006b3df`, then again on `ad6c975`. A duplicate local notes commit
   (`18af345`) was dropped in favour of the notes already on `main`; it is
   still in the reflog.

## Review

What worked:

- **Checking that a test can fail.** Every new test was run against code
  broken on purpose, and that is what exposed the fuzzer's drift: a clean
  result from it had stopped meaning anything.
- **Reading the CI logs instead of guessing.** Both CI failures turned out
  to be environment behaviour (inode reuse, 8.3 short names) that never
  shows up on the local machines.

What to watch:

- **Nobody looked at CI for two weeks.** `main` was red from 2026-11-01 to
  2026-11-15. Check the latest run after every push.
- **Test helpers that live outside the repo drift.** The fuzzer did. Prefer
  putting checks in `tests/` so CI runs them.
- **The portable Windows Python imports old code by default.** Its
  `python312._pth` always puts `fileplan-win\repo` on the path, so a plain
  `unittest discover` from another folder tests stale code without saying
  so. The first Windows run in this work fell into it (27 errors from old
  code); the runs after it put the tested folder first on the path, and the
  copies were compared byte for byte with the repo.
- **Skipping from inside code under test does not work** when that code
  catches broad exceptions on purpose, as apply does around the rename.

Known gap, unchanged: an undo whose kind is changed to "apply" *and* whose
`undoing` link is removed reads exactly like a real apply. Only a redo that
lost both fields gets through.

## How to pick it up

Open `/home/jbutler23/projects/fileplan`. Check `git status` and
`git log -3 --oneline` to confirm the saved handoff before making changes.
The ignore-rules document is a proposal; its design choices are still open.

Run the tests on Linux:

```sh
python3 -m unittest discover -s tests -v
```

Run them on the Windows machine. Copy `fileplan.py`, `tests`, and
`examples` into a new folder under `%TEMP%`, then put that folder first on
the path, because of the `._pth` trap above:

```
%TEMP%\fileplan-win\py\python.exe -c "import sys, unittest; sys.path[:0] = [r'<folder>', r'<folder>\tests']; unittest.main(module=None, argv=['t', 'discover', '-s', r'<folder>\tests', '-t', r'<folder>\tests'])"
```

- **The 7 link tests** need administrator rights or Developer Mode. Without
  either they skip.
- **To mimic GitHub's 8.3 temp folder**, set `tempfile.tempdir` to a short
  name, e.g. `C:\Users\jbutl\AppData\Local\Temp\FI8C14~1`.

Check CI. The GitHub CLI is not installed. The run list is public:

```
https://api.github.com/repos/Jbutler23-ai/fileplan/actions/runs?branch=main
```

Job logs need a login. The GitHub credential git uses for pushing works for
the logs endpoint.

## Next, in order

1. **Decide on `a6a5038`** from the remote-tracking `origin/.../oak` branch
   named above: review its test, run it on Linux and Windows, and bring the
   test change onto `main` if it holds. The old CI notes are already superseded.
2. **Ignore rules**: see `PLAN-ignore-rules.md`. It ends with four
   questions to settle before building.
3. **The macOS decision** in ROADMAP.md: refuse applying (as now), or accept
   a stated weaker guarantee.

Housekeeping that can wait: the Windows test folders under `%TEMP%`
(`fileplan-applyundoing-*`, `fileplan-baseline-*`, `fileplan-fuzz-*`,
`fileplan-shortfix-*`, `fileplan-pin-*`, `fileplan-shortname-temp-folder`)
are no longer needed. `fileplan-applyundoing-*\tests` also holds two files
nobody in this work created, `fuzz_records.py` and `test_record_fuzz.py`;
look at them before deleting that folder.
