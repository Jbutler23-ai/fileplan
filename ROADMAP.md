# Next steps

The first version makes a read-only organization plan. The work below would turn
that preview into a tool that can carry out a plan and recover from problems.
Estimates include implementation, tests, and documentation; they exclude the
current preview, packaging, and initial test setup.

## Apply a reviewed plan (7-10 hours)

- Define a saved-plan format with enough source metadata to detect stale plans.
- Recheck sources and destinations immediately before each move.
- Refuse overwrites and changes outside the selected root.
- Record completed operations so interrupted work can be inspected.
- Test files changing between preview and execution, permission errors, and
  partial completion.

Done when a reviewed plan can be applied without overwriting a destination and
failures leave an accurate record of what happened.

## Undo and interrupted-operation recovery (5-7 hours)

- Restore completed moves using the operation record.
- Refuse to overwrite files created or edited after a move.
- Add a recovery command that explains unfinished operations.
- Test restart behavior and recovery on Windows and Unix.

Done when an interrupted operation can be inspected and safely reversed where
the original state is still available.

## Custom organization rules (5-7 hours)

- Add a project configuration file for extension groups and filename patterns.
- Define rule priority and a clear fallback for unmatched files.
- Validate destination paths and reject rules that escape the selected root.
- Show which rule produced each destination.

Done when the same saved configuration produces understandable, repeatable
plans and invalid rules fail before any changes are attempted.

## Duplicate and conflict choices (5-8 hours)

- Add optional content hashing for suspected duplicates.
- Separate identical contents from files that only share a name.
- Allow explicit keep, skip, or rename choices in a reviewed plan.
- Test large files and destinations that change during a duplicate check.

Done when users can resolve conflicts deliberately without losing a file or
treating matching names as proof of matching contents.

The remaining estimate is **22-32 hours**. These are proposed next steps for a new
project, not claims about past development time or a release schedule.
