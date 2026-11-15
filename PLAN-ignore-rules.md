# Plan: ignore rules

A proposal, not built yet. It lets a user keep chosen files and folders out
of a plan, such as partial downloads, `Thumbs.db`, or a folder they want left
alone. Nothing here changes how a move, an undo, or a run record works.

## What exists today

`build_plan` already leaves some entries out, each listed under `skipped`
with a reason, and none of it can be configured:

- symbolic links and reparse points
- the `_organized` folder itself
- a fixed set of metadata, dependency, and cache folders
  (`IGNORED_DIRECTORIES`: `.git`, `.hg`, `.svn`, `.venv`, `venv`, `env`,
  `node_modules`, `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`)
- hidden entries, unless `--include-hidden`
- folders, unless `--recursive`, and anything that is not a regular file

The rules file (`--rules`, schema version 1) only chooses folders. It is
checked completely before any plan is made, unknown keys are refused, and a
plan stores its normalised rules, so `--verify` and `--apply` rebuild with
exactly the same rules.

## Recommendation

**Add an `ignore` list to the rules file**, as schema version 2.

- It reuses everything the rules file already has: checked before any scan,
  refused with a clear message when wrong, and stored in the plan, so verify
  and apply honour the same ignores without new code paths.
- A fileplan that only knows version 1 refuses a version 2 file or plan
  cleanly, by its schema check, rather than ignoring the new key.
- Version 1 files keep working unchanged.

```json
{
  "schema_version": 2,
  "ignore": ["*.part", "*.crdownload", "thumbs.db", "keep-as-is/", "drafts/old-*"],
  "rules": [
    {"name": "images", "extensions": ["jpg", "png"], "folder": "images"}
  ],
  "fallback": "by-extension"
}
```

Alternatives considered:

- **A `.fileplanignore` file in the scanned folder**, read automatically. It
  is familiar from `.gitignore`, but it would let a file inside the target
  folder silently change what a preview shows, and every other setting here
  is explicit and stored in the plan. It is also a dotfile, so it is hidden
  and easy to forget. Not recommended.
- **A `--ignore PATTERN` option** for one-off use. Could come later, stored
  in the plan's options the same way; not needed for the first version.

## How patterns match

These are kept close to the existing `patterns` in rules, so there is one
way to write a pattern:

- **Shell-style wildcards** (`*`, `?`, `[...]`), matched **without regard to
  case** on every platform, like rule patterns and destination checks.
- **A pattern without a slash matches a name at any depth**, file or folder:
  `thumbs.db` ignores every `Thumbs.db`.
- **A pattern with a slash matches a path from the scanned folder**, one
  folder level per part: `drafts/old-*` ignores `drafts/old-notes.txt` but
  not `drafts/2026/old-notes.txt`. Matching part by part means `*` never
  crosses a folder boundary, which plain `fnmatch` on the whole path would
  allow.
- **A trailing slash means folders only**: `keep-as-is/` ignores a folder
  named that, and not a file.
- **An ignored folder is not entered**, so nothing under it is planned or
  even listed. It appears once under `skipped`.

Left out of the first version, on purpose:

- **Negation (`!pattern`).** It cannot bring back a file under a folder that
  was never entered, which is the most common surprise with `.gitignore`.
- **`**` for any depth.** A name pattern already matches at any depth;
  path-anchored `**` can come later if a real need shows up.
- **Un-ignoring the built-in folders** such as `node_modules`. A separate
  feature if wanted.

## Rules that must hold

- **Ignoring only removes entries from a plan.** It never moves, creates, or
  deletes anything, and an ignored file stays exactly where it is.
- **Ignore wins over folder rules.** An ignored file never reaches
  `_choose_folder`.
- **Ignore applies to sources only.** It must never hide a file that already
  sits at a destination inside `_organized`, so it can never make a
  destination look free when it is taken. This needs its own test.
- **Every ignored entry is reported**, never dropped silently: under
  `skipped` with the reason `Ignored by "<pattern>"`, naming the pattern that
  matched.
- **The order of skip reasons stays fixed**: link, then `_organized`, then
  built-in folders, then ignore rules, then hidden. An entry that is both
  hidden and ignored is reported as ignored.

## Checking patterns up front

Refused with a message naming the pattern, before any scan:

- not a list of strings, or an empty string
- a backslash, a colon, a leading slash, an empty part (`a//b`), a `..`
  part, or a control character
- the same pattern listed twice

Accepted but pointless, and said so in the docs rather than refused: a
pattern that only matches `_organized` or a built-in folder, which are
skipped anyway.

## Where the code changes

- `_RULES_KEYS` gains `ignore`; `_normalise_rules` accepts schema version 1
  or 2, rejects `ignore` in version 1, validates the list, and stores it
  normalised (lower-cased for matching, original kept for messages).
- A small `_ignored_by(relative, is_directory, rules)` returns the matching
  pattern or None.
- In `build_plan`'s scan loop, one new branch after the built-in folder
  check: if `_ignored_by` matches, record the skip and do not descend.
- Plain output counts ignored entries in the summary and lists them with the
  other skipped entries. JSON keeps `skipped[].reason` and adds
  `skipped[].ignored_by`.
- `examples/rules.json` and the README "Custom rules" section gain an
  `ignore` example.

No change to apply, undo, `--show`, or run records.

## Tests

All with made-up names and contents, as everywhere else in the suite:

- a name pattern ignores a file at the top level and in a subfolder
- a name pattern ignores a folder, which is then not entered
- a path pattern is anchored to the scanned folder, and `*` does not cross
  a folder boundary
- a trailing slash matches folders and not files
- matching ignores case on every platform
- each invalid pattern is refused with a message naming it
- a version 1 rules file still works, and `ignore` in version 1 is refused
- a saved plan stores the ignore list, and `--verify` and `--apply` honour
  it
- an ignored name never hides an existing file at a destination, so a
  conflict is still reported
- an entry that is hidden and ignored is reported as ignored
- the example rules file still shapes the example inbox as documented

CI runs these on Ubuntu, Windows, and macOS. They need no file moves, so
they run on macOS as well.

## Estimate

About 2-3 hours including tests and docs. A proposed next step, not a claim
about past development time or a release schedule.

## Questions to settle before building

1. **Rules-file key, as recommended, or a `.fileplanignore` file?**
2. **Is the narrow pattern language enough**, or is `.gitignore`
   compatibility (negation, `**`) wanted from the start?
3. **Should ignored entries be listed one by one** in plain output, or only
   counted, with the list in JSON?
4. **Should a `--ignore` option come in the same change**, or later?
