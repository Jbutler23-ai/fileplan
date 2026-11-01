# Next steps

The first version makes a read-only organization plan. Applying a plan, undoing
a run, and the record that connects them are now built. The work below is what
remains. Estimates include implementation, tests, and documentation.

## Rules that must hold

These are settled decisions, not open questions. Anything added later has to
keep them true, and each one has tests that fail if it stops being true.

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

## Applying beyond Linux (6-9 hours)

Applying needs a rename that refuses to replace, which only Linux offers today.
Previewing and verifying already work everywhere.

- Find out whether Windows can give the same guarantee, and at what cost.
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

The remaining estimate is **6-9 hours**. These are proposed next steps, not
claims about past development time or a release schedule.
