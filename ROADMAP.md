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
wrong, never a crash.

The remaining estimate is **2-3 hours**. These are proposed next steps, not
claims about past development time or a release schedule.
