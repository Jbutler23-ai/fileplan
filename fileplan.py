"""Plan, check, apply, and undo grouping files by extension.

A preview never changes anything. Applying and undoing are governed by rules
that every change to this file must keep true; ROADMAP.md states them in full
and the tests fail if any stops holding:

- Nothing moves unless it is asked for.
- One way to move a file: a no-replace rename through link-safe folder handles,
  or stop.
- Never overwrite, never leave the selected folder.
- Never delete to tidy up.
- Stop on anything unexpected.
- Compared contents are compared again before moving.
- Say what happened to every file, including when unsure.
- Confirm a file at its original name before calling it restored.
- A damaged record is an incomplete record.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
from typing import TextIO
import unicodedata


DESTINATION_NAME = "_organized"
PLAN_SCHEMA_VERSION = 2
APPLICATION_SCHEMA_VERSION = 1
VERIFICATION_SCHEMA_VERSION = 1
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"}
)


class FilePlanError(ValueError):
    """The requested directory cannot be fully inspected."""


def _is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _is_hidden(name: str, metadata: os.stat_result) -> bool:
    return name.startswith(".") or bool(
        getattr(metadata, "st_file_attributes", 0) & _HIDDEN
    )


def _path_order(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _windows_bucket_conflict(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        return f"Destination folder name is reserved on Windows: {name}"
    if name.endswith((".", " ")) or any(
        character in '<>:"/\\|?*' or ord(character) < 32 for character in name
    ):
        return f"Destination folder name is invalid on Windows: {name}"
    return None


class _DirectorySnapshot:
    """Cache directory metadata so destination checks do not rescan folders."""

    def __init__(self) -> None:
        self._directories: dict[Path, list[tuple[str, os.stat_result]]] = {}
        self._name_indexes: dict[Path, dict[str, list[tuple[str, os.stat_result]]]] = {}

    def children(self, directory: Path) -> list[tuple[str, os.stat_result]]:
        if directory not in self._directories:
            try:
                with os.scandir(directory) as iterator:
                    children = [
                        (entry.name, entry.stat(follow_symlinks=False))
                        for entry in iterator
                    ]
            except OSError as error:
                raise FilePlanError(
                    f"Cannot inspect {directory}: {error.strerror or error}"
                ) from error
            children.sort(key=lambda child: _path_order(child[0]))
            self._directories[directory] = children
            index: dict[str, list[tuple[str, os.stat_result]]] = {}
            for name, metadata in children:
                index.setdefault(name.casefold(), []).append((name, metadata))
            self._name_indexes[directory] = index
        return self._directories[directory]

    def existing(
        self, root: Path, parts: tuple[str, ...]
    ) -> tuple[Path, os.stat_result] | None:
        """Return the on-disk path and metadata at parts, or None if nothing is there."""
        candidates = [root]
        for index, part in enumerate(parts):
            next_candidates = []
            for directory in candidates:
                self.children(directory)
                for name, metadata in self._name_indexes[directory].get(part.casefold(), ()):
                    found = directory / name
                    if index == len(parts) - 1:
                        return found, metadata
                    if stat.S_ISDIR(metadata.st_mode) and not _is_link(metadata):
                        next_candidates.append(found)
            candidates = next_candidates
            if not candidates:
                return None
        return None

    def destination_conflict(self, root: Path, parts: tuple[str, ...]) -> str | None:
        candidates = [root]
        for index, part in enumerate(parts):
            next_candidates = []
            for directory in candidates:
                self.children(directory)
                matches = self._name_indexes[directory].get(part.casefold(), ())
                for name, metadata in matches:
                    existing = directory / name
                    relative = existing.relative_to(root).as_posix()
                    if index == len(parts) - 1:
                        return f"Destination already exists: {relative}"
                    if _is_link(metadata):
                        return f"Destination parent is a link or reparse point: {relative}"
                    if not stat.S_ISDIR(metadata.st_mode):
                        return f"Destination parent is not a directory: {relative}"
                    next_candidates.append(existing)
            candidates = next_candidates
            if not candidates:
                return None
        return None


RULES_SCHEMA_VERSION = 1
NO_EXTENSION = "no-extension"
FALLBACK_RULE = "fallback"
_RULE_KEYS = frozenset({"name", "folder", "extensions", "patterns"})
_RULES_KEYS = frozenset({"schema_version", "rules", "fallback"})


def _folder_problem(folder: object) -> str | None:
    """Say why a rule folder could leave _organized, or None if it cannot."""
    if not isinstance(folder, str) or not folder:
        return "folder must be a non-empty string"
    parts = _relative_parts(folder)
    if parts is None:
        return (
            f"folder {folder!r} must be a relative path with no '..', leading "
            "slash, backslash, or colon"
        )
    for part in parts:
        if any(ord(character) < 32 for character in part):
            return f"folder {folder!r} contains a control character"
    if parts[0].casefold() == JOURNAL_DIRECTORY.casefold():
        return f"folder {folder!r} would share its name with the run records"
    return None


def _normalise_rules(data: object, where: str) -> dict:
    """Validate a rules document and return it in the form plans store."""

    def reject(detail: str) -> None:
        raise FilePlanError(f"{where}: {detail}")

    if not isinstance(data, dict):
        reject("the top level is not an object")
    unknown = set(data) - _RULES_KEYS
    if unknown:
        reject(f"unknown key {sorted(unknown)[0]!r}; allowed keys are schema_version, rules, fallback")
    if data.get("schema_version") != RULES_SCHEMA_VERSION:
        reject(f"schema_version must be {RULES_SCHEMA_VERSION}, found {data.get('schema_version')!r}")
    rules = data.get("rules")
    if not isinstance(rules, list):
        reject("rules must be a list")
    seen: set[str] = set()
    normalised = []
    for position, rule in enumerate(rules, start=1):
        label = f"rule {position}"
        if not isinstance(rule, dict):
            reject(f"{label} is not an object")
        unknown = set(rule) - _RULE_KEYS
        if unknown:
            reject(f"{label}: unknown key {sorted(unknown)[0]!r}; allowed keys are name, folder, extensions, patterns")
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            reject(f"{label} needs a name")
        if name == FALLBACK_RULE:
            reject(f"{label}: the name {FALLBACK_RULE!r} is reserved for unmatched files")
        if name in seen:
            reject(f"{label}: the name {name!r} is used twice")
        seen.add(name)
        label = f"rule {name!r}"
        problem = _folder_problem(rule.get("folder"))
        if problem:
            reject(f"{label}: {problem}")
        folder = "/".join(_relative_parts(rule["folder"]))
        extensions = rule.get("extensions", [])
        patterns = rule.get("patterns", [])
        if not isinstance(extensions, list) or not all(isinstance(e, str) for e in extensions):
            reject(f"{label}: extensions must be a list of strings")
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            reject(f"{label}: patterns must be a list of strings")
        cleaned = []
        for extension in extensions:
            value = extension[1:] if extension.startswith(".") else extension
            value = value.lower()
            if not value or any(c in value for c in "./\\") or value != value.strip():
                reject(f"{label}: {extension!r} is not an extension; use letters like 'jpg' or {NO_EXTENSION!r}")
            cleaned.append(value)
        for pattern in patterns:
            if not pattern or "/" in pattern or "\\" in pattern:
                reject(f"{label}: pattern {pattern!r} must be a filename pattern without slashes")
        if not cleaned and not patterns:
            reject(f"{label} needs at least one of extensions or patterns")
        normalised.append(
            {"name": name, "folder": folder, "extensions": cleaned, "patterns": list(patterns)}
        )
    fallback = data.get("fallback", "by-extension")
    if fallback == "by-extension":
        pass
    elif isinstance(fallback, dict) and set(fallback) == {"folder"}:
        problem = _folder_problem(fallback["folder"])
        if problem:
            reject(f"fallback: {problem}")
        fallback = {"folder": "/".join(_relative_parts(fallback["folder"]))}
    else:
        reject("fallback must be \"by-extension\" or an object with only a folder")
    return {"schema_version": RULES_SCHEMA_VERSION, "rules": normalised, "fallback": fallback}


def load_rules(path: Path) -> dict:
    """Read a rules file; raise FilePlanError before any plan is made if invalid."""
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as error:
        raise FilePlanError(f"Cannot read {path}: {error.strerror or error}") from error
    try:
        document = json.loads(data)
    except ValueError as error:
        raise FilePlanError(f"{path} is not valid JSON: {error}") from error
    return _normalise_rules(document, f"{path} is not a usable rules file")


def _choose_folder(
    name: str, extension: str, rules: dict | None
) -> tuple[tuple[str, ...], str | None]:
    """Pick the folder under _organized for one file and say which rule chose it.

    Rules are tried in order and the first whose every condition holds wins.
    With no rules, or when nothing matches and the fallback is by extension,
    the folder is the lowercased extension, as it always was.
    """
    group = extension or NO_EXTENSION
    if rules is None:
        return (group,), None
    lowered = name.casefold()
    for rule in rules["rules"]:
        if rule["extensions"] and group not in rule["extensions"]:
            continue
        if rule["patterns"] and not any(
            fnmatch.fnmatchcase(lowered, pattern.casefold()) for pattern in rule["patterns"]
        ):
            continue
        return tuple(rule["folder"].split("/")), rule["name"]
    fallback = rules["fallback"]
    if fallback == "by-extension":
        return (group,), None
    return tuple(fallback["folder"].split("/")), FALLBACK_RULE


CHOICES = ("keep", "skip", "rename")
_HASH_CHUNK = 1024 * 1024


def _add_reason(entry: dict, reason: str) -> None:
    entry["status"] = "conflict"
    entry["reason"] = f"{entry['reason']}; {reason}" if entry["reason"] else reason


def _assess_destinations(entries: list[dict], root: Path, snapshot: _DirectorySnapshot) -> None:
    """Mark every destination problem among the entries that will be moved.

    Besides a destination that exists, is shared, or sits under something that
    is not a folder, this catches a plan that needs one path to be both a file
    and a folder, such as docs/a.txt alongside docs/a.txt/b.md.
    """
    destinations: dict[str, list[dict]] = {}
    folders_needed: dict[str, list[dict]] = {}
    for entry in entries:
        destinations.setdefault(entry["destination"].casefold(), []).append(entry)
        parts = tuple(Path(entry["destination"]).parts)
        for depth in range(2, len(parts)):
            prefix = "/".join(parts[:depth]).casefold()
            folders_needed.setdefault(prefix, []).append(entry)
        reason = None
        for part in parts[1:-1]:
            reason = _windows_bucket_conflict(part)
            if reason:
                break
        if reason is None:
            reason = snapshot.destination_conflict(root, parts)
        if reason:
            _add_reason(entry, reason)
    for group in destinations.values():
        if len(group) > 1:
            for entry in group:
                _add_reason(entry, "Multiple source files share this destination")
    for key, group in destinations.items():
        needing = folders_needed.get(key)
        if not needing:
            continue
        sources = ", ".join(sorted(item["source"] for item in needing))
        for entry in group:
            _add_reason(entry, f"Destination is also needed as a folder by {sources}")
        for item in needing:
            _add_reason(
                item, f"Destination folder {group[0]['destination']} is also a planned file"
            )


def _renamed_destination(
    original: str, taken: set[str], root: Path, snapshot: _DirectorySnapshot
) -> str:
    """Pick the first numbered name that is free both on disk and in the plan."""
    path = Path(original)
    for number in range(2, 100_000):
        candidate = (path.parent / f"{path.stem} ({number}){path.suffix}").as_posix()
        if candidate.casefold() in taken:
            continue
        if snapshot.existing(root, tuple(Path(candidate).parts)) is not None:
            continue
        return candidate
    raise FilePlanError(f"No free numbered name could be found for {original}")


def _apply_choices(entries: list[dict], root: Path, snapshot: _DirectorySnapshot) -> None:
    """Honour keep, skip, and rename, then assess the entries that will move."""
    active = []
    for entry in entries:
        entry["status"] = "ready"
        entry["reason"] = None
        if entry.get("renamed_from"):
            entry["destination"] = entry["renamed_from"]
        entry["renamed_from"] = None
        if entry["choice"] == "skip":
            entry["status"] = "skipped"
            entry["reason"] = "Skipped by choice"
        else:
            active.append(entry)
    taken = {entry["destination"].casefold() for entry in active if entry["choice"] != "rename"}
    for entry in active:
        if entry["choice"] == "rename":
            original = entry["destination"]
            entry["destination"] = _renamed_destination(original, taken, root, snapshot)
            entry["renamed_from"] = original
            taken.add(entry["destination"].casefold())
    _assess_destinations(active, root, snapshot)


# O_BINARY matters on Windows: without it os.open uses text mode, where a read
# stops at the first Ctrl-Z byte and two different files could hash the same.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class _ShortRead(OSError):
    """Fewer bytes were read than the file holds."""


def _digest(fd: int, expected_size: int) -> str:
    """Hash every byte from fd; refuse if the count differs from expected_size."""
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = os.read(fd, _HASH_CHUNK)
        if not chunk:
            break
        count += len(chunk)
        digest.update(chunk)
    if count != expected_size:
        raise _ShortRead(
            errno.EIO, f"read {count} of {expected_size} bytes, so contents were not compared"
        )
    return digest.hexdigest()


def _hash_file(
    path: Path, label: str, size_bytes: int, modified_ns: int
) -> tuple[str | None, str | None]:
    """Hash one file's contents, refusing links and noticing a change meanwhile."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        return None, f"Cannot read {label} for the duplicate check: {error.strerror or error}"
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, f"{label} is not a regular file"
        if (before.st_size, before.st_mtime_ns) != (size_bytes, modified_ns):
            return None, f"{label} changed before the duplicate check"
        digest = _digest(fd, before.st_size)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            return None, f"{label} changed during the duplicate check"
        return digest, None
    except OSError as error:
        return None, f"Cannot read {label} for the duplicate check: {error.strerror or error}"
    finally:
        os.close(fd)


def _add_content_checks(entries: list[dict], root: Path, snapshot: _DirectorySnapshot) -> None:
    """Compare the contents of files that would land on the same name.

    Only files already in a destination conflict are read: those sharing a
    destination with each other, or with a file that is already there.
    """
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["status"] != "skipped":
            groups.setdefault(entry["destination"].casefold(), []).append(entry)
    for group in groups.values():
        found = snapshot.existing(root, tuple(Path(group[0]["destination"]).parts))
        existing_label = existing_digest = existing_problem = None
        if found is not None:
            existing_path, metadata = found
            existing_label = existing_path.relative_to(root).as_posix()
            if stat.S_ISREG(metadata.st_mode) and not _is_link(metadata):
                existing_digest, existing_problem = _hash_file(
                    existing_path, existing_label, metadata.st_size, metadata.st_mtime_ns
                )
            else:
                existing_problem = f"{existing_label} is not a regular file"
        if len(group) < 2 and existing_digest is None:
            if existing_problem and found is not None:
                for entry in group:
                    _add_reason(entry, f"Existing destination could not be compared: {existing_problem}")
            continue
        for entry in group:
            digest, problem = _hash_file(
                root.joinpath(*entry["source"].split("/")),
                entry["source"],
                entry["size_bytes"],
                entry["modified_ns"],
            )
            entry["sha256"] = digest
            if problem:
                _add_reason(entry, problem)
        for entry in group:
            if entry.get("sha256") is None:
                continue
            identical = [
                other["source"]
                for other in group
                if other is not entry and other.get("sha256") == entry["sha256"]
            ]
            differs = [
                other["source"]
                for other in group
                if other is not entry
                and other.get("sha256") is not None
                and other["sha256"] != entry["sha256"]
            ]
            if existing_digest is not None:
                (identical if existing_digest == entry["sha256"] else differs).append(
                    existing_label
                )
            entry["identical_to"] = identical
            entry["differs_from"] = differs
            notes = []
            if identical:
                notes.append("Contents identical to " + ", ".join(identical))
            if differs:
                notes.append("Contents differ from " + ", ".join(differs))
            if existing_problem:
                notes.append(f"Existing destination could not be compared: {existing_problem}")
            if notes:
                entry["reason"] = (
                    f"{entry['reason']}; " if entry["reason"] else ""
                ) + "; ".join(notes)


def build_plan(
    root: Path,
    recursive: bool = False,
    include_hidden: bool = False,
    rules: dict | None = None,
    check_duplicates: bool = False,
    choices: dict[str, str | None] | None = None,
) -> dict:
    """Return a plan; raise FilePlanError if inspection fails.

    ``rules`` is a normalised rules document from load_rules, or None for the
    default grouping by extension. ``choices`` maps a source to keep, skip, or
    rename, as written into a reviewed plan. With ``check_duplicates`` the
    contents of files that would share a destination are compared; otherwise
    no file is opened. The plan records every option it was built with.

    Destinations are compared without case sensitivity on every platform. A
    returned plan describes this scan only; files may change after it finishes.
    """
    root = Path(os.path.abspath(Path(root).expanduser()))
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise FilePlanError(
            f"Cannot inspect {root}: {error.strerror or error}"
        ) from error
    if _is_link(root_metadata):
        raise FilePlanError("The root must not be a symbolic link or reparse point")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise FilePlanError(f"Not a directory: {root}")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise FilePlanError(f"Cannot resolve {root}: {error.strerror or error}") from error

    snapshot = _DirectorySnapshot()
    entries = []
    skipped = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for name, metadata in snapshot.children(directory):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            is_directory = stat.S_ISDIR(metadata.st_mode)
            reason = None
            if _is_link(metadata):
                reason = "Symbolic link or reparse point"
            elif is_directory and name.casefold() == DESTINATION_NAME:
                reason = "Destination directory"
            elif is_directory and name.casefold() in IGNORED_DIRECTORIES:
                reason = "Metadata, dependency, or cache directory"
            elif not include_hidden and _is_hidden(name, metadata):
                reason = "Hidden entry"
            elif is_directory:
                if recursive:
                    pending.append(path)
                else:
                    reason = "Directory (use --recursive to include its files)"
            elif stat.S_ISREG(metadata.st_mode):
                folder, rule = _choose_folder(name, path.suffix[1:].lower(), rules)
                destination = Path(DESTINATION_NAME).joinpath(*folder) / name
                entries.append(
                    {
                        "source": relative,
                        "destination": destination.as_posix(),
                        "rule": rule,
                        "choice": (choices or {}).get(relative),
                        "renamed_from": None,
                        "size_bytes": metadata.st_size,
                        "modified_ns": metadata.st_mtime_ns,
                        "status": "ready",
                        "reason": None,
                    }
                )
            else:
                reason = "Not a regular file"
            if reason:
                skipped.append({"path": relative, "reason": reason})

    entries.sort(key=lambda entry: _path_order(entry["source"]))
    skipped.sort(key=lambda entry: _path_order(entry["path"]))
    for entry in entries:
        if entry["choice"] not in (None, *CHOICES):
            raise FilePlanError(
                f"The choice for {entry['source']} must be one of {', '.join(CHOICES)}"
            )
    _apply_choices(entries, root, snapshot)
    if check_duplicates:
        _add_content_checks(entries, root, snapshot)

    conflicts = sum(entry["status"] == "conflict" for entry in entries)
    skipped_by_choice = sum(entry["status"] == "skipped" for entry in entries)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "root": str(root),
        "destination_root": str(root / DESTINATION_NAME),
        "options": {
            "recursive": bool(recursive),
            "include_hidden": bool(include_hidden),
            "rules": rules,
            "check_duplicates": bool(check_duplicates),
        },
        "entries": entries,
        "summary": {
            "files": len(entries),
            "ready": len(entries) - conflicts - skipped_by_choice,
            "conflicts": conflicts,
            "skipped": skipped_by_choice,
            "total_bytes": sum(entry["size_bytes"] for entry in entries),
        },
        "skipped": skipped,
    }


def _relative_parts(value: str) -> tuple[str, ...] | None:
    """Split a plan path, or return None if it could escape the selected root."""
    if not value or value.startswith("/") or "\\" in value or ":" in value:
        return None
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def load_plan(path: Path) -> dict:
    """Return a saved plan from disk; raise FilePlanError if it is unusable."""
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as error:
        raise FilePlanError(f"Cannot read {path}: {error.strerror or error}") from error
    try:
        plan = json.loads(data)
    except ValueError as error:
        raise FilePlanError(f"{path} is not valid JSON: {error}") from error
    _validate_plan(plan, path)
    return plan


def _validate_plan(plan: object, path: Path) -> None:
    def reject(detail: str) -> None:
        raise FilePlanError(f"{path} is not a usable plan: {detail}")

    if not isinstance(plan, dict):
        reject("the top level is not an object")
    version = plan.get("schema_version")
    if version != PLAN_SCHEMA_VERSION:
        reject(f"schema_version must be {PLAN_SCHEMA_VERSION}, found {version!r}")
    if not isinstance(plan.get("root"), str) or not plan["root"]:
        reject("root must be a non-empty string")
    options = plan.get("options")
    if not isinstance(options, dict) or any(
        not isinstance(options.get(name), bool)
        for name in ("recursive", "include_hidden")
    ):
        reject("options must record recursive and include_hidden as true or false")
    if options.get("rules") is not None:
        options["rules"] = _normalise_rules(options["rules"], f"{path} options.rules")
    if "check_duplicates" in options and not isinstance(options["check_duplicates"], bool):
        reject("options.check_duplicates must be true or false")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        reject("entries must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            reject("every entry must be an object")
        for name in ("source", "destination"):
            if not isinstance(entry.get(name), str) or not entry[name]:
                reject(f"every entry needs a non-empty {name}")
        for name in ("size_bytes", "modified_ns"):
            value = entry.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                reject(f"entry {name} must be a whole number")
        if _relative_parts(entry["source"]) is None:
            reject(f"the source {entry['source']} is not a safe relative path")
        destination = _relative_parts(entry["destination"])
        if destination is None or len(destination) < 2 or destination[0] != DESTINATION_NAME:
            reject(
                f"the destination {entry['destination']} is not a path inside "
                f"{DESTINATION_NAME}"
            )
        if "rule" in entry and entry["rule"] is not None and not isinstance(entry["rule"], str):
            reject("entry rule must be text or null")
        digest = entry.get("sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            reject(f"the sha256 for {entry['source']} must be 64 hex characters")
        if entry.get("choice") not in (None, *CHOICES):
            reject(
                f"the choice for {entry['source']} must be one of "
                f"{', '.join(CHOICES)}, found {entry.get('choice')!r}"
            )
        if entry["source"] in seen:
            reject(f"the source {entry['source']} is listed more than once")
        seen.add(entry["source"])


def _skip_reason(source: str, skipped: dict[str, str]) -> str | None:
    """Return why a source left the plan, checking its parent folders too."""
    candidate = source
    while candidate:
        if candidate in skipped:
            return skipped[candidate]
        candidate = candidate.rpartition("/")[0]
    return None


def _change_reason(saved: dict, current: dict) -> str:
    if saved["size_bytes"] != current["size_bytes"]:
        return (
            f"Size changed from {saved['size_bytes']} to "
            f"{current['size_bytes']} bytes"
        )
    return "Contents were modified after the plan was made"


def verify_plan(plan: dict, root: Path, plan_path: Path | None = None) -> dict:
    """Compare a saved plan with the folder now; change nothing on disk.

    Sources are matched by size and modification time, so this reports what a
    scan can see rather than proving two files hold the same contents. Each
    entry carries its destination conflict separately from its status, and the
    summary's conflict total counts every blocked destination, including those
    on changed or new files.
    """
    options = plan["options"]
    root_path = Path(os.path.abspath(Path(root).expanduser()))
    current = build_plan(
        root,
        options["recursive"],
        options["include_hidden"],
        options.get("rules"),
        options.get("check_duplicates", False),
        {entry["source"]: entry.get("choice") for entry in plan["entries"]},
    )
    if os.path.normcase(current["root"]) != os.path.normcase(plan["root"]):
        raise FilePlanError(
            f"The plan was made for {plan['root']}, not {current['root']}"
        )
    root_path = Path(current["root"])

    present = {entry["source"]: entry for entry in current["entries"]}
    for saved in plan["entries"]:
        found = present.get(saved["source"])
        if not saved.get("sha256") or found is None or found.get("sha256") is not None:
            continue
        # The plan compared this file's contents, so compare them again even
        # if a choice has since taken it out of every conflict.
        found["sha256"], found["hash_problem"] = _hash_file(
            root_path.joinpath(*saved["source"].split("/")),
            saved["source"],
            found["size_bytes"],
            found["modified_ns"],
        )
    skipped = {entry["path"]: entry["reason"] for entry in current["skipped"]}
    results = []
    for saved in plan["entries"]:
        source = saved["source"]
        found = present.get(source)
        reasons = []
        conflict = None
        if found is None:
            status = "missing"
            reason = _skip_reason(source, skipped)
            reasons.append(
                f"Source is no longer planned: {reason}"
                if reason
                else "Source file is missing"
            )
        else:
            # A skipped file is checked like any other. The choice to skip it
            # was made about the file as it was, so if it has changed the
            # choice has to be made again rather than quietly honoured.
            status = "skipped" if found["status"] == "skipped" else "unchanged"
            if status == "skipped":
                reasons.append("Skipped by choice")
            if (saved["size_bytes"], saved["modified_ns"]) != (
                found["size_bytes"],
                found["modified_ns"],
            ):
                status = "changed"
                reasons.append(_change_reason(saved, found))
            elif saved.get("sha256") and found.get("sha256") is None:
                status = "changed"
                reasons.append(
                    found.get("hash_problem") or "Contents could not be compared again"
                )
            elif saved.get("sha256") and saved["sha256"] != found["sha256"]:
                status = "changed"
                reasons.append(
                    "Contents changed since they were compared, even though the "
                    "size and modification time did not"
                )
            # A renamed entry may still carry the name it was first given.
            accepted = {found["destination"], found.get("renamed_from") or found["destination"]}
            if status == "skipped":
                pass
            elif saved["destination"] not in accepted:
                if status == "unchanged":
                    status = "mismatch"
                reasons.append(
                    f"The plan sends this file to {saved['destination']}, but the "
                    f"current rules give {found['destination']}"
                )
            if found["status"] == "conflict":
                conflict = found["reason"]
                if status == "unchanged":
                    status = "conflict"
                reasons.append(conflict)
            if status == "changed" and "Skipped by choice" in reasons:
                reasons.remove("Skipped by choice")
        results.append(
            {
                "source": source,
                "destination": saved["destination"],
                "effective_destination": found["destination"] if found else None,
                "choice": saved.get("choice"),
                "status": status,
                "reason": "; ".join(reasons) or None,
                "destination_conflict": conflict,
            }
        )

    planned = {entry["source"] for entry in plan["entries"]}
    for source, entry in present.items():
        if source not in planned:
            conflict = entry["reason"] if entry["status"] == "conflict" else None
            reasons = ["File appeared after the plan was made"]
            if conflict:
                reasons.append(conflict)
            results.append(
                {
                    "source": source,
                    "destination": entry["destination"],
                    "effective_destination": entry["destination"],
                    "choice": None,
                    "status": "new",
                    "reason": "; ".join(reasons),
                    "destination_conflict": conflict,
                }
            )

    results.sort(key=lambda entry: _path_order(entry["source"]))
    counts = {
        name: 0
        for name in (
            "unchanged", "changed", "mismatch", "missing", "conflict", "new", "skipped",
        )
    }
    for entry in results:
        counts[entry["status"]] += 1
    conflicts = sum(entry["destination_conflict"] is not None for entry in results)
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "kind": "verification",
        "plan": str(plan_path) if plan_path is not None else None,
        "root": current["root"],
        "destination_root": current["destination_root"],
        "options": {
            "recursive": options["recursive"],
            "include_hidden": options["include_hidden"],
            "rules": options.get("rules"),
            "check_duplicates": options.get("check_duplicates", False),
        },
        "entries": results,
        "summary": {
            "entries": len(results),
            "unchanged": counts["unchanged"],
            "changed": counts["changed"],
            "mismatch": counts["mismatch"],
            "missing": counts["missing"],
            "conflicts": conflicts,
            "new": counts["new"],
            "skipped": counts["skipped"],
            "problems": len(results) - counts["unchanged"] - counts["skipped"],
        },
    }


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = (
    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_OPENED_A_LINK = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("ELOOP", "EMLINK"))
    if code is not None
)


# The only change fileplan makes to disk is one rename that refuses to
# replace: on Linux the renameat2 system call with RENAME_NOREPLACE, on Windows
# the NT rename with ReplaceIfExists off (see _WindowsFiles). It gives the file
# its new name and removes the old one in a single step, and the system refuses
# if the new name is taken. Every other step acts relative to folders opened
# without following links. When a platform cannot do exactly this, applying
# stops rather than trying another method.
_RENAME_NOREPLACE = 1
# Kernel system-call numbers, used only when the C library lacks the wrapper.
_RENAMEAT2_SYSCALLS = {
    "x86_64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "riscv64": 276,
    "loongarch64": 276,
    "armv6l": 382,
    "armv7l": 382,
    "ppc64": 357,
    "ppc64le": 357,
    "s390x": 347,
}


def _load_renameat2():
    """Return a callable for renameat2, or None where it is unavailable."""
    if sys.platform != "linux":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
    except (ImportError, OSError):
        return None
    try:
        wrapper = libc.renameat2
    except AttributeError:
        wrapper = None
    if wrapper is not None:
        wrapper.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        wrapper.restype = ctypes.c_int

        def call(src_fd: int, src: bytes, dst_fd: int, dst: bytes, flags: int) -> int:
            return wrapper(src_fd, src, dst_fd, dst, flags)

    else:
        number = _RENAMEAT2_SYSCALLS.get(platform.machine())
        if number is None:
            return None
        syscall = libc.syscall
        syscall.restype = ctypes.c_long

        def call(src_fd: int, src: bytes, dst_fd: int, dst: bytes, flags: int) -> int:
            return syscall(
                ctypes.c_long(number),
                ctypes.c_int(src_fd),
                ctypes.c_char_p(src),
                ctypes.c_int(dst_fd),
                ctypes.c_char_p(dst),
                ctypes.c_uint(flags),
            )

    call.get_errno = ctypes.get_errno  # type: ignore[attr-defined]
    return call


_RENAMEAT2 = _load_renameat2()


class _WindowsFiles:
    """The same handle-relative steps as on Linux, through the Windows NT API.

    Python's os module cannot open a name relative to a folder handle on
    Windows, but the NT layer underneath always could. Every handle opened here
    is wrapped as an ordinary C runtime descriptor, so os.fstat, os.read,
    os.write, os.dup and os.close keep working on it unchanged.

    - A folder is opened relative to its parent's handle, with
      FILE_OPEN_REPARSE_POINT so a junction or link is opened as itself and
      then refused, never entered.
    - Folder handles are opened without FILE_SHARE_DELETE. While fileplan holds
      one, Windows refuses to let anything rename, move, or delete that folder,
      so the folder checked is the folder used.
    - The move is NtSetInformationFile(FileRenameInformation) with
      ReplaceIfExists false and the destination folder's handle as its root:
      one step, refused by the filesystem if the new name is taken.
    """

    _SYNCHRONIZE = 0x00100000
    _DELETE = 0x00010000
    _FILE_READ_DATA = 0x0001
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_GENERIC_READ = 0x00120089
    _FILE_GENERIC_WRITE = 0x00120116
    _SHARE_READ, _SHARE_WRITE, _SHARE_DELETE = 1, 2, 4
    _FILE_OPEN, _FILE_CREATE = 1, 2
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _FILE_RENAME_INFORMATION = 10
    _FILE_FULL_DIRECTORY_INFO = 14
    _FILE_FULL_DIRECTORY_RESTART_INFO = 15
    _ERROR_NO_MORE_FILES = 18
    # The run record's access list, the Windows form of mode 0600: full access
    # for the file's owner and the system, nothing inherited, no one else.
    PRIVATE_FILE_SDDL = "D:P(A;;FA;;;OW)(A;;FA;;;SY)"
    # NTSTATUS values mapped to the errno the rest of fileplan already handles.
    _STATUS_ERRNO = {
        0xC0000035: errno.EEXIST,  # OBJECT_NAME_COLLISION
        0xC0000034: errno.ENOENT,  # OBJECT_NAME_NOT_FOUND
        0xC000003A: errno.ENOENT,  # OBJECT_PATH_NOT_FOUND
        0xC0000103: errno.ENOTDIR,  # NOT_A_DIRECTORY
        0xC00000BA: errno.EISDIR,  # FILE_IS_A_DIRECTORY
        0xC00000D4: errno.EXDEV,  # NOT_SAME_DEVICE
        0xC0000022: errno.EACCES,  # ACCESS_DENIED
        0xC0000043: errno.EACCES,  # SHARING_VIOLATION
        0xC0000056: errno.EACCES,  # DELETE_PENDING
        0xC0000121: errno.EACCES,  # CANNOT_DELETE
        0xC0000002: errno.EINVAL,  # NOT_IMPLEMENTED
        0xC000000D: errno.EINVAL,  # INVALID_PARAMETER
        0xC0000010: errno.EINVAL,  # INVALID_DEVICE_REQUEST
        0xC00000BB: errno.EINVAL,  # NOT_SUPPORTED
    }

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        self._ctypes = ctypes
        self._msvcrt = msvcrt
        self._ntdll = ctypes.WinDLL("ntdll")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

        class RenameInformation(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", ctypes.c_ubyte),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.ULONG),
                ("FileName", ctypes.c_wchar * 1),
            ]

        self._UnicodeString = UnicodeString
        self._ObjectAttributes = ObjectAttributes
        self._IoStatusBlock = IoStatusBlock
        self._RenameInformation = RenameInformation

        nt = self._ntdll
        nt.NtCreateFile.restype = ctypes.c_ulong
        nt.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
            ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ]
        nt.NtSetInformationFile.restype = ctypes.c_ulong
        nt.NtSetInformationFile.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(IoStatusBlock), ctypes.c_void_p,
            wintypes.ULONG, ctypes.c_int,
        ]
        nt.RtlNtStatusToDosError.restype = wintypes.ULONG
        nt.RtlNtStatusToDosError.argtypes = [ctypes.c_ulong]
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ]
        k32 = self._kernel32
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]

    @staticmethod
    def _utf16(name: str) -> bytes:
        """The name as Windows stores it: UTF-16, keeping any lone surrogate."""
        return name.encode("utf-16-le", "surrogatepass")

    def _error(self, status: int, name: str | None) -> OSError:
        code = self._STATUS_ERRNO.get(status, errno.EIO)
        message = self._ctypes.FormatError(
            self._ntdll.RtlNtStatusToDosError(status)
        ).strip()
        return OSError(code, message or f"NTSTATUS {status:#010x}", name)

    def _create(
        self, parent: int | None, name: str, access: int, share: int,
        disposition: int, options: int, security: object = None,
    ) -> int:
        """NtCreateFile relative to parent's handle; return the raw HANDLE."""
        ctypes = self._ctypes
        # Windows measures names in UTF-16 code units, not characters. An emoji
        # or any other character outside the Basic Multilingual Plane takes two
        # units, so len(name) * 2 would cut such names short.
        size = len(self._utf16(name))
        text = self._UnicodeString(size, size, name)
        root = self._msvcrt.get_osfhandle(parent) if parent is not None else None
        attributes = self._ObjectAttributes(
            ctypes.sizeof(self._ObjectAttributes), root, ctypes.pointer(text),
            self._OBJ_CASE_INSENSITIVE, security, None,
        )
        handle = ctypes.c_void_p()
        status_block = self._IoStatusBlock()
        status = self._ntdll.NtCreateFile(
            ctypes.cast(ctypes.byref(handle), ctypes.POINTER(ctypes.wintypes.HANDLE)),
            access | self._SYNCHRONIZE, ctypes.byref(attributes),
            ctypes.byref(status_block), None, 0, share, disposition,
            options | self._FILE_SYNCHRONOUS_IO_NONALERT | self._FILE_OPEN_REPARSE_POINT,
            None, 0,
        )
        if status != 0:
            raise self._error(status, name)
        return handle.value

    def _wrap(self, handle: int, flags: int, refuse_links: bool, name: str) -> int:
        """Turn a raw HANDLE into a C runtime descriptor; refuse reparse points."""
        try:
            fd = self._msvcrt.open_osfhandle(handle, flags)
        except OSError:
            self._kernel32.CloseHandle(handle)
            raise
        if refuse_links:
            try:
                attributes = os.fstat(fd).st_file_attributes
            except OSError:
                os.close(fd)
                raise
            if attributes & _REPARSE_POINT:
                os.close(fd)
                raise OSError(errno.ELOOP, "Is a link or reparse point", name)
        return fd

    def _directory(self, parent: int | None, name: str, disposition: int) -> int:
        return self._create(
            parent, name,
            self._FILE_LIST_DIRECTORY | self._FILE_TRAVERSE | self._FILE_READ_ATTRIBUTES,
            self._SHARE_READ | self._SHARE_WRITE,  # never SHARE_DELETE: pins it
            disposition, self._FILE_DIRECTORY_FILE,
        )

    def open_root(self, path: str | os.PathLike) -> int:
        full = os.path.abspath(os.fspath(path))
        if full.startswith("\\\\?\\"):
            nt_path = "\\??\\" + full[4:]
        elif full.startswith("\\\\"):
            nt_path = "\\??\\UNC\\" + full[2:]
        else:
            nt_path = "\\??\\" + full
        handle = self._directory(None, nt_path, self._FILE_OPEN)
        return self._wrap(handle, os.O_RDONLY, True, full)

    def open_dir_at(self, parent: int, name: str) -> int:
        handle = self._directory(parent, name, self._FILE_OPEN)
        return self._wrap(handle, os.O_RDONLY, True, name)

    def mkdir_at(self, parent: int, name: str) -> None:
        handle = self._directory(parent, name, self._FILE_CREATE)
        self._kernel32.CloseHandle(handle)

    def stat_at(self, parent: int, name: str) -> os.stat_result:
        """Like os.stat(follow_symlinks=False): a link is described, not followed."""
        handle = self._create(
            parent, name, self._FILE_READ_ATTRIBUTES,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
            self._FILE_OPEN, 0,
        )
        fd = self._wrap(handle, os.O_RDONLY, False, name)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def open_read_at(self, parent: int, name: str) -> int:
        handle = self._create(
            parent, name, self._FILE_READ_DATA | self._FILE_READ_ATTRIBUTES,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
            self._FILE_OPEN, self._FILE_NON_DIRECTORY_FILE,
        )
        return self._wrap(handle, os.O_RDONLY | os.O_BINARY, True, name)

    def create_new_at(self, parent: int, name: str) -> int:
        """A new file for appending; FileExistsError if the name is taken."""
        ctypes = self._ctypes
        descriptor = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self.PRIVATE_FILE_SDDL, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            handle = self._create(
                parent, name, self._FILE_GENERIC_READ | self._FILE_GENERIC_WRITE,
                self._SHARE_READ, self._FILE_CREATE, self._FILE_NON_DIRECTORY_FILE,
                descriptor,
            )
        finally:
            self._kernel32.LocalFree(descriptor)
        return self._wrap(handle, os.O_APPEND | os.O_BINARY, True, name)

    def list_names(self, fd: int) -> list[str]:
        ctypes = self._ctypes
        handle = self._msvcrt.get_osfhandle(fd)
        buffer = ctypes.create_string_buffer(64 * 1024)
        names: list[str] = []
        info_class = self._FILE_FULL_DIRECTORY_RESTART_INFO
        while True:
            if not self._kernel32.GetFileInformationByHandleEx(
                handle, info_class, buffer, len(buffer)
            ):
                code = ctypes.get_last_error()
                if code == self._ERROR_NO_MORE_FILES:
                    return [name for name in names if name not in (".", "..")]
                raise ctypes.WinError(code)
            info_class = self._FILE_FULL_DIRECTORY_INFO
            raw = buffer.raw
            offset = 0
            while True:
                # FILE_FULL_DIR_INFO: NextEntryOffset @0, FileNameLength @60, FileName @68
                following = int.from_bytes(raw[offset : offset + 4], "little")
                length = int.from_bytes(raw[offset + 60 : offset + 64], "little")
                names.append(
                    raw[offset + 68 : offset + 68 + length].decode(
                        "utf-16-le", "surrogatepass"
                    )
                )
                if not following:
                    break
                offset += following

    def rename_noreplace(
        self, src_fd: int, src_name: str, dst_fd: int, dst_name: str
    ) -> None:
        ctypes = self._ctypes
        handle = self._create(
            src_fd, src_name, self._DELETE | self._FILE_READ_ATTRIBUTES,
            self._SHARE_READ | self._SHARE_WRITE | self._SHARE_DELETE,
            self._FILE_OPEN, self._FILE_NON_DIRECTORY_FILE,
        )
        fd = self._wrap(handle, os.O_RDONLY, True, src_name)
        try:
            encoded = self._utf16(dst_name)
            offset = self._RenameInformation.FileName.offset
            buffer = ctypes.create_string_buffer(
                max(offset + len(encoded) + 2, ctypes.sizeof(self._RenameInformation))
            )
            info = self._RenameInformation.from_buffer(buffer)
            info.ReplaceIfExists = 0
            info.RootDirectory = self._msvcrt.get_osfhandle(dst_fd)
            info.FileNameLength = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
            del info
            status_block = self._IoStatusBlock()
            status = self._ntdll.NtSetInformationFile(
                self._msvcrt.get_osfhandle(fd), ctypes.byref(status_block),
                buffer, len(buffer), self._FILE_RENAME_INFORMATION,
            )
        finally:
            os.close(fd)
        if status != 0:
            error = self._error(status, src_name)
            raise OSError(error.errno, error.strerror, src_name, None, dst_name)

    def handle_path(self, fd: int) -> str | None:
        ctypes = self._ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        length = self._kernel32.GetFinalPathNameByHandleW(
            self._msvcrt.get_osfhandle(fd), buffer, len(buffer), 0
        )
        if not 0 < length < len(buffer):
            return None
        path = buffer.value
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        if path.startswith("\\\\?\\"):
            return path[4:]
        return path


def _load_windows_files() -> _WindowsFiles | None:
    if sys.platform != "win32":
        return None
    try:
        return _WindowsFiles()
    except (ImportError, OSError, AttributeError):
        return None


_WINDOWS = _load_windows_files()


# Handle-relative steps. On Linux each is the os call with dir_fd; on Windows
# each is the matching NT call above. Both refuse to follow links.
def _open_root(path: str | os.PathLike) -> int:
    if _WINDOWS is not None:
        return _WINDOWS.open_root(path)
    return os.open(path, _DIRECTORY_FLAGS)


def _open_dir_at(dir_fd: int, name: str) -> int:
    if _WINDOWS is not None:
        return _WINDOWS.open_dir_at(dir_fd, name)
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)


def _mkdir_at(dir_fd: int, name: str) -> None:
    if _WINDOWS is not None:
        return _WINDOWS.mkdir_at(dir_fd, name)
    os.mkdir(name, dir_fd=dir_fd)


def _stat_at(dir_fd: int, name: str) -> os.stat_result:
    if _WINDOWS is not None:
        return _WINDOWS.stat_at(dir_fd, name)
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_read_at(dir_fd: int, name: str) -> int:
    if _WINDOWS is not None:
        return _WINDOWS.open_read_at(dir_fd, name)
    return os.open(name, _READ_FLAGS, dir_fd=dir_fd)


def _create_new_at(dir_fd: int, name: str) -> int:
    if _WINDOWS is not None:
        return _WINDOWS.create_new_at(dir_fd, name)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | os.O_EXCL
        | _O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    return os.open(name, flags, 0o600, dir_fd=dir_fd)


def _list_names(directory: Path | int) -> list[str]:
    if _WINDOWS is not None and isinstance(directory, int):
        return _WINDOWS.list_names(directory)
    with os.scandir(directory) as iterator:
        return [entry.name for entry in iterator]


def _rename_noreplace(src_fd: int, src_name: str, dst_fd: int, dst_name: str) -> None:
    """Rename within open folders, refusing to replace anything at the new name."""
    if _WINDOWS is not None:
        return _WINDOWS.rename_noreplace(src_fd, src_name, dst_fd, dst_name)
    result = _RENAMEAT2(
        src_fd, os.fsencode(src_name), dst_fd, os.fsencode(dst_name), _RENAME_NOREPLACE
    )
    if result != 0:
        code = _RENAMEAT2.get_errno()
        raise OSError(code, os.strerror(code), src_name, None, dst_name)


_NOREPLACE_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, name, None) for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")
    )
    if code is not None
)
_SAFE_MOVE_SUPPORTED = _WINDOWS is not None or (
    _RENAMEAT2 is not None
    and bool(_O_DIRECTORY and _O_NOFOLLOW)
    and {os.open, os.stat, os.mkdir} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)
SAFE_MOVE_UNAVAILABLE = (
    "Applying is not supported on this platform yet: it needs Linux or Windows, "
    "which can move a file in one step while refusing to replace anything"
)


def _source_mismatch(metadata: os.stat_result, saved: dict) -> str | None:
    """Return why this metadata no longer matches the plan, or None."""
    if _is_link(metadata):
        return "Source became a symbolic link or reparse point"
    if not stat.S_ISREG(metadata.st_mode):
        return "Source is no longer a regular file"
    if metadata.st_size != saved["size_bytes"]:
        return (
            f"Size changed from {saved['size_bytes']} to "
            f"{metadata.st_size} bytes"
        )
    if metadata.st_mtime_ns != saved["modified_ns"]:
        return "Contents were modified after the plan was made"
    return None


def _destination_taken(directory: Path | int, name: str) -> str | None:
    """Return why a destination name is taken, comparing names without case."""
    wanted = name.casefold()
    try:
        for found in _list_names(directory):
            if found.casefold() == wanted:
                return f"Destination already exists: {found}"
    except FileNotFoundError:
        return "Destination folder disappeared after the check"
    except OSError as error:
        return f"Cannot inspect the destination folder: {error.strerror or error}"
    return None


def _open_failure(label: str, part: str, error: OSError, dir_fd: int) -> str:
    if isinstance(error, NotADirectoryError) or error.errno in _OPENED_A_LINK:
        # Opening a link with O_NOFOLLOW reports ELOOP or, when the link leads
        # to a directory and O_DIRECTORY is set, ENOTDIR. Look to tell them apart.
        try:
            metadata = _stat_at(dir_fd, part)
        except OSError:
            metadata = None
        if metadata is None or _is_link(metadata):
            return f"{label} folder is a link or reparse point: {part}"
        return f"{label} folder is not a directory: {part}"
    return f"Cannot open {label.lower()} folder {part}: {error.strerror or error}"


def _open_chain(
    root_fd: int, parts: tuple[str, ...], create: bool, label: str
) -> tuple[int | None, str | None]:
    """Open each folder in turn, never following links; return the last one.

    The caller must close the returned handle. The root handle is left open.
    """
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                opened = _open_dir_at(current, part)
            except FileNotFoundError:
                if not create:
                    return None, f"{label} folder is missing: {part}"
                try:
                    _mkdir_at(current, part)
                except FileExistsError:
                    pass  # Created by someone else just now; the open below checks it.
                except OSError as error:
                    return None, f"Cannot create {part}: {error.strerror or error}"
                try:
                    opened = _open_dir_at(current, part)
                except OSError as error:
                    return None, _open_failure(label, part, error, current)
            except OSError as error:
                return None, _open_failure(label, part, error, current)
            os.close(current)
            current = opened
        result, current = current, None
        return result, None
    finally:
        if current is not None:
            os.close(current)


def _folder_changed(
    root_fd: int, parts: tuple[str, ...], expected_fd: int, label: str
) -> str | None:
    """Return why the folder opened earlier is no longer the one the plan names."""
    opened, reason = _open_chain(root_fd, parts, create=False, label=label)
    if reason:
        return reason
    try:
        now = os.fstat(opened)
        then = os.fstat(expected_fd)
    except OSError as error:
        return f"Cannot inspect the {label.lower()} folder: {error.strerror or error}"
    finally:
        os.close(opened)
    if (now.st_dev, now.st_ino) != (then.st_dev, then.st_ino):
        return f"{label} folder changed after the check"
    return None


def _digest_at(
    dir_fd: int, name: str, expected: os.stat_result, label: str
) -> tuple[str | None, str | None]:
    """Hash the file at name, which must still be the one already checked."""
    try:
        fd = _open_read_at(dir_fd, name)
    except OSError as error:
        return None, f"Cannot read {label} to check its contents: {error.strerror or error}"
    try:
        before = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            return None, f"{label} was replaced before its contents could be checked"
        if not stat.S_ISREG(before.st_mode):
            return None, f"{label} is no longer a regular file"
        digest = _digest(fd, before.st_size)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            return None, f"{label} changed while its contents were being checked"
        return digest, None
    except OSError as error:
        return None, f"Cannot read {label} to check its contents: {error.strerror or error}"
    finally:
        os.close(fd)


def _stat_source(
    dir_fd: int, name: str, saved: dict
) -> tuple[os.stat_result | None, str | None]:
    try:
        metadata = _stat_at(dir_fd, name)
    except FileNotFoundError:
        return None, "Source file is missing"
    except OSError as error:
        return None, f"Cannot inspect the source: {error.strerror or error}"
    return metadata, _source_mismatch(metadata, saved)


def _handle_path(fd: int) -> str | None:
    """Best effort: where does this open folder live now? None if unknown."""
    if _WINDOWS is not None:
        try:
            return _WINDOWS.handle_path(fd)
        except OSError:
            return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl

        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return raw.split(b"\0", 1)[0].decode(errors="backslashreplace") or None
    except (ImportError, AttributeError, OSError):
        return None


def _where_now(destination_fd: int, destination_parts: tuple[str, ...]) -> str:
    """Say where the moved file can be found when its folder has moved."""
    where = _handle_path(destination_fd)
    if where is None:
        return (
            f"The file was moved and keeps the name {destination_parts[-1]}, but "
            "its folder is no longer where the plan says"
        )
    return f"The file is now at {os.path.join(where, destination_parts[-1])}"


def _confirm_move(
    root_fd: int,
    source_fd: int,
    source_name: str,
    source: str,
    checked: os.stat_result,
    destination_fd: int,
    destination_parts: tuple[str, ...],
    saved: dict,
) -> str | None:
    """Look at everything once more after the move. Nothing here changes disk.

    The move itself was one atomic step that refused to replace, so the file
    had exactly one name at every moment. These looks report anything that
    happened to it in the instant after, so the run can stop and say so.
    """
    destination_name = destination_parts[-1]
    planned = "/".join(destination_parts)
    identity = (checked.st_dev, checked.st_ino)

    reason = _folder_changed(
        root_fd, destination_parts[:-1], destination_fd, "Destination"
    )
    if reason:
        return f"{reason} as the file was moved. " + _where_now(
            destination_fd, destination_parts
        )

    try:
        _stat_at(source_fd, source_name)
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"Cannot look at the old name again: {error.strerror or error}"
    else:
        return (
            f"Something else appeared under the old name as the file was moved: "
            f"{source}. The file itself is at {planned}; nothing was removed"
        )

    try:
        landed = _stat_at(destination_fd, destination_name)
    except FileNotFoundError:
        return (
            f"The file was removed by something else right after the move: "
            f"{planned}. fileplan removed nothing"
        )
    except OSError as error:
        return f"Cannot confirm the destination: {error.strerror or error}"
    if (landed.st_dev, landed.st_ino) != identity:
        return (
            f"The destination name now refers to a different file: {planned}. "
            "Something else replaced the moved file; fileplan removed nothing"
        )
    mismatch = _source_mismatch(landed, saved)
    if mismatch:
        return f"The file changed right after the move: {mismatch}. It is at {planned}"
    return None


def _move_with_handles(
    root: Path,
    source_parts: tuple[str, ...],
    destination_parts: tuple[str, ...],
    saved: dict,
    progress: dict,
) -> str | None:
    """Move through directory handles with one no-replace rename; else stop.

    progress["outcome"] records whether the rename has happened, so a problem
    found afterwards is reported as a file that moved rather than one that
    stayed put.
    """
    try:
        root_fd = _open_root(root)
    except OSError as error:
        return f"Cannot open the selected folder: {error.strerror or error}"
    source_fd = destination_fd = None
    try:
        source_fd, reason = _open_chain(root_fd, source_parts[:-1], False, "Source")
        if reason:
            return reason
        source_name = source_parts[-1]
        destination_name = destination_parts[-1]
        checked, reason = _stat_source(source_fd, source_name, saved)
        if reason:
            return reason
        destination_fd, reason = _open_chain(
            root_fd, destination_parts[:-1], True, "Destination"
        )
        if reason:
            return reason
        reason = _destination_taken(destination_fd, destination_name)
        if reason:
            return reason
        # Look at the source and the folder's position one last time, right
        # before the move, so the gap is as small as it can be.
        checked, reason = _stat_source(source_fd, source_name, saved)
        if reason:
            return reason
        # A file whose contents were compared is compared again here. Size and
        # modification time can both be forged or coincide, so for these files
        # the bytes themselves decide whether the move goes ahead.
        recorded = saved.get("sha256")
        if recorded:
            label = "/".join(source_parts)
            digest, reason = _digest_at(source_fd, source_name, checked, label)
            if reason:
                return reason
            if digest != recorded:
                return (
                    f"Contents of {label} changed since they were compared, even "
                    "though the size and modification time did not"
                )
        reason = _folder_changed(
            root_fd, destination_parts[:-1], destination_fd, "Destination"
        )
        if reason:
            return reason
        try:
            _rename_noreplace(source_fd, source_name, destination_fd, destination_name)
        except FileExistsError:
            return f"Destination already exists: {destination_name}"
        except OSError as error:
            # No other method is tried. A plain rename could replace a file
            # that appeared in the meantime, so the run stops here instead.
            if error.errno in _NOREPLACE_UNSUPPORTED:
                return (
                    "This filesystem cannot move a file while refusing to replace "
                    f"one, so the file was left alone: {error.strerror or error}"
                )
            return _folder_changed(
                root_fd, destination_parts[:-1], destination_fd, "Destination"
            ) or f"Cannot move the file: {error.strerror or error}"
        except Exception as error:  # noqa: BLE001 - the result is genuinely unknown
            progress["outcome"] = "unknown"
            return (
                "The move failed in a way fileplan does not recognise, so "
                f"whether it happened is unknown: {error!r}"
            )
        # The rename returned, so the file is at its destination. Anything
        # found below describes a file that has already moved.
        progress["outcome"] = "moved"
        try:
            return _confirm_move(
                root_fd,
                source_fd,
                source_name,
                "/".join(source_parts),
                checked,
                destination_fd,
                destination_parts,
                saved,
            )
        except Exception as error:  # noqa: BLE001 - report, never hide
            return (
                "The file moved, but checking it afterwards failed: "
                f"{error!r}"
            )
    finally:
        for handle in (source_fd, destination_fd, root_fd):
            if handle is not None:
                os.close(handle)


def _outcome(saved: dict, outcome: str, reason: str | None) -> dict:
    return {
        "source": saved["source"],
        "destination": saved["destination"],
        "outcome": outcome,
        "reason": reason,
    }


def _move_one(root: Path, saved: dict) -> dict:
    """Check this one file and its destination, move it, and say what happened.

    The outcome is one of: "moved", the file is at its destination and was
    confirmed there; "moved-unconfirmed", the move happened but a later look
    found a problem; "not-moved", nothing happened and the file is where it
    was; "unknown", the move failed in a way that leaves its result unclear.
    """
    source_parts = _relative_parts(saved["source"])
    destination_parts = _relative_parts(saved["destination"])
    if source_parts is None or destination_parts is None:
        return _outcome(
            saved, "not-moved", "The plan uses a path that could leave the selected folder"
        )
    if not _SAFE_MOVE_SUPPORTED:
        return _outcome(saved, "not-moved", SAFE_MOVE_UNAVAILABLE)
    progress = {"outcome": "not-moved"}
    reason = _move_with_handles(
        root, source_parts, destination_parts, saved, progress
    )
    outcome = progress["outcome"]
    if outcome == "moved" and reason:
        outcome = "moved-unconfirmed"
    return _outcome(saved, outcome, reason)


JOURNAL_DIRECTORY = ".fileplan"
JOURNAL_SCHEMA_VERSION = 1


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _journal_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"run-{stamp}-{os.getpid()}"


class _Journal:
    """Append-only record of one apply run, one JSON object per line.

    Each record is written and flushed to disk before the step it describes
    is taken (intent) or as soon as the result is known (done). A run that is
    cut off therefore leaves a record whose last line is an intent with no
    matching done, which says exactly which file was in flight.
    """

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path
        self.damaged = False

    def _roll_back(self, size: int | None) -> None:
        """Remove a half-written line so the record holds only whole entries."""
        if size is None:
            self.damaged = True
            return
        try:
            if os.fstat(self.fd).st_size != size:
                os.ftruncate(self.fd, size)
                os.fsync(self.fd)
        except OSError:
            # The partial line could not be removed; say so rather than hide it.
            self.damaged = True

    def write(self, record: dict) -> None:
        """Append one whole entry, or leave the record exactly as it was."""
        if self.damaged:
            raise OSError(
                errno.EIO,
                "The record holds a half-written entry that could not be removed, "
                "so nothing more is written to it",
            )
        line = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
        try:
            size = os.fstat(self.fd).st_size
        except OSError:
            size = None
        try:
            view = memoryview(line)
            while view:
                written = os.write(self.fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "The record could not be written")
                view = view[written:]
            os.fsync(self.fd)
        except OSError:
            self._roll_back(size)
            raise

    def close(self) -> None:
        os.close(self.fd)


def _open_journal(root_fd: int, root: Path) -> tuple[_Journal | None, str | None]:
    """Create a new record file under _organized/.fileplan; never reuse one."""
    directory_fd, reason = _open_chain(
        root_fd, (DESTINATION_NAME, JOURNAL_DIRECTORY), True, "Record"
    )
    if reason:
        return None, reason
    try:
        base = _journal_name()
        for attempt in range(100):
            name = f"{base}.jsonl" if attempt == 0 else f"{base}-{attempt}.jsonl"
            try:
                fd = _create_new_at(directory_fd, name)
            except FileExistsError:
                continue
            except OSError as error:
                return None, f"Cannot create the record {name}: {error.strerror or error}"
            path = root / DESTINATION_NAME / JOURNAL_DIRECTORY / name
            return _Journal(fd, str(path)), None
        return None, "Cannot create the record: too many records share this name"
    finally:
        os.close(directory_fd)


def _start_journal(
    root: Path, header: dict
) -> tuple[_Journal | None, str | None]:
    try:
        root_fd = _open_root(root)
    except OSError as error:
        return None, f"Cannot open the selected folder: {error.strerror or error}"
    try:
        journal, reason = _open_journal(root_fd, root)
    finally:
        os.close(root_fd)
    if reason:
        return None, reason
    try:
        journal.write(
            {
                "record": "start",
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "time": _timestamp(),
                **header,
            }
        )
    except OSError as error:
        journal.close()
        return None, f"Cannot write the record: {error.strerror or error}"
    return journal, None


def apply_plan(plan: dict, root: Path, plan_path: Path | None = None) -> dict:
    """Move the files in a plan, but only while everything still matches.

    The whole plan is verified first and nothing moves if any entry needs
    review. Each source and destination is then checked again immediately
    before its own move, and the first problem stops the run with every
    remaining file left where it is. Destinations are never overwritten and
    always sit inside the scanned folder.

    Every planned file appears in "results" with what became of it, so a run
    that stops partway says which files moved, which did not, and which it
    cannot tell either way. The same facts are written as they happen to a
    record file under _organized/.fileplan, so a run that is cut off leaves a
    record on disk; "record" in the report says where it is.
    """
    if not _SAFE_MOVE_SUPPORTED:
        raise FilePlanError(SAFE_MOVE_UNAVAILABLE)
    report = verify_plan(plan, root, plan_path)
    root_path = Path(report["root"])
    applied = report["summary"]["problems"] == 0
    entries = plan["entries"]
    results: list[dict] = []
    stopped = None
    record = {"path": None, "complete": False, "damaged": False, "warning": None}

    def stop_at(result: dict, index: int) -> None:
        nonlocal stopped
        stopped = {
            "source": result["source"],
            "destination": result["destination"],
            "outcome": result["outcome"],
            "reason": result["reason"],
        }
        results.extend(
            _outcome(later, "not-attempted", "The run stopped before this file")
            for later in entries[index + 1 :]
        )

    if not applied:
        results = [
            _outcome(saved, "not-attempted", "The plan was refused before any move")
            for saved in entries
        ]
    elif entries:
        journal, reason = _start_journal(
            root_path,
            {
                "kind": "apply",
                "root": str(root_path),
                "plan": str(plan_path) if plan_path is not None else None,
                "plan_schema_version": plan["schema_version"],
                "options": plan["options"],
                "planned": len(plan["entries"]),
                # Files a choice leaves alone are planned but never moved, so
                # a reader can tell them from files the run never reached.
                "skipped": sum(
                    1 for entry in report["entries"] if entry["status"] == "skipped"
                ),
            },
        )
        if journal is None:
            first = _outcome(
                entries[0],
                "not-attempted",
                f"The record could not be started, so nothing was moved: {reason}",
            )
            results.append(first)
            stop_at(first, 0)
        else:
            record["path"] = journal.path
            try:
                verified = {entry["source"]: entry for entry in report["entries"]}
                for index, saved in enumerate(entries):
                    check = verified[saved["source"]]
                    if check["status"] == "skipped":
                        results.append(_outcome(saved, "skipped", "Skipped by choice"))
                        continue
                    # A renamed entry moves to the name chosen at verification.
                    saved = dict(saved, destination=check["effective_destination"])
                    try:
                        journal.write(
                            {
                                "record": "intent",
                                "time": _timestamp(),
                                "index": index,
                                "source": saved["source"],
                                "destination": saved["destination"],
                                "size_bytes": saved["size_bytes"],
                                "modified_ns": saved["modified_ns"],
                                "sha256": saved.get("sha256"),
                            }
                        )
                    except OSError as error:
                        result = _outcome(
                            saved,
                            "not-moved",
                            "The record could not be written, so this file was "
                            f"not moved: {error.strerror or error}",
                        )
                        results.append(result)
                        stop_at(result, index)
                        break
                    result = _move_one(root_path, saved)
                    results.append(result)
                    try:
                        journal.write(
                            {
                                "record": "done",
                                "time": _timestamp(),
                                "index": index,
                                "source": saved["source"],
                                "outcome": result["outcome"],
                                "reason": result["reason"],
                            }
                        )
                    except OSError as error:
                        note = (
                            "the record could not be written afterwards: "
                            f"{error.strerror or error}"
                        )
                        result["reason"] = (
                            f"{result['reason']}; {note}"
                            if result["reason"]
                            else f"The file moved and was confirmed, but {note}"
                        )
                    if result["reason"]:
                        stop_at(result, index)
                        break
                try:
                    journal.write(
                        {
                            "record": "end",
                            "time": _timestamp(),
                            "completed": stopped is None,
                            "stopped_at": stopped["source"] if stopped else None,
                            "outcomes": {
                                entry["source"]: entry["outcome"] for entry in results
                            },
                        }
                    )
                    record["complete"] = not journal.damaged
                except OSError as error:
                    record["warning"] = (
                        "The closing record could not be written, so this run is "
                        f"not finished: {error.strerror or error}"
                    )
                if journal.damaged:
                    record["damaged"] = True
                    record["warning"] = (
                        (record["warning"] + "; ") if record["warning"] else ""
                    ) + "a half-written entry could not be removed from the record"
            finally:
                journal.close()
    counts = {
        name: 0
        for name in (
            "moved", "moved-unconfirmed", "not-moved", "unknown", "not-attempted", "skipped",
        )
    }
    for result in results:
        counts[result["outcome"]] += 1
    planned = len(entries)
    return {
        "schema_version": APPLICATION_SCHEMA_VERSION,
        "kind": "application",
        "plan": str(plan_path) if plan_path is not None else None,
        "root": report["root"],
        "destination_root": report["destination_root"],
        "record": record,
        "results": results,
        "stopped": stopped,
        "summary": {
            "planned": planned,
            # Files no longer at their source, confirmed or not.
            "moved": counts["moved"] + counts["moved-unconfirmed"],
            "moved_confirmed": counts["moved"],
            "moved_unconfirmed": counts["moved-unconfirmed"],
            "not_moved": counts["not-moved"],
            "unknown": counts["unknown"],
            "not_attempted": counts["not-attempted"],
            "skipped": counts["skipped"],
            "applied": applied,
            "completed": (
                applied
                and stopped is None
                and (record["complete"] or not entries)
                and not record["damaged"]
            ),
            "record_complete": (
                (record["complete"] and not record["damaged"]) or not entries
            ),
            "problems": report["summary"]["problems"],
        },
        "verification": report,
    }


UNDO_SCHEMA_VERSION = 1


def _inspect_parts(
    root_fd: int, parts: tuple[str, ...]
) -> tuple[os.stat_result | None, str | None]:
    """Look at one path below the root without following any link.

    Returns (metadata, reason). Both None means the path is simply not there.
    """
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                opened = _open_dir_at(current, part)
            except FileNotFoundError:
                return None, None  # A parent is gone, so the file is not here.
            except OSError as error:
                return None, _open_failure("Folder", part, error, current)
            os.close(current)
            current = opened
        try:
            return _stat_at(current, parts[-1]), None
        except FileNotFoundError:
            return None, None
        except OSError as error:
            return None, f"Cannot inspect {parts[-1]}: {error.strerror or error}"
    finally:
        os.close(current)


def _organised_side(
    source: tuple[str, ...], destination: tuple[str, ...]
) -> str | None:
    """Say which end of a recorded move sits inside the organised folder."""
    def inside(parts: tuple[str, ...]) -> bool:
        return len(parts) >= 2 and parts[0] == DESTINATION_NAME

    if inside(destination) and not inside(source):
        return "destination"
    if inside(source) and not inside(destination):
        return "source"
    return None


def load_record(path: Path) -> dict:
    """Read a run record; raise FilePlanError if it cannot be trusted."""
    try:
        text = Path(path).expanduser().read_bytes().decode("utf-8")
    except OSError as error:
        raise FilePlanError(f"Cannot read {path}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise FilePlanError(f"{path} is not a readable record: {error}") from error

    def reject(detail: str) -> None:
        raise FilePlanError(f"{path} is not a usable record: {detail}")

    lines = text.splitlines()
    if not lines:
        reject("the file is empty")
    parsed = []
    truncated = False
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            if number == len(lines):
                # A run cut off mid-write can leave one unfinished last line.
                truncated = True
                break
            reject(f"line {number} is not valid JSON")
        if not isinstance(item, dict) or not isinstance(item.get("record"), str):
            reject(f"line {number} is not a record")
        parsed.append(item)
    if not parsed or parsed[0]["record"] != "start":
        reject("it does not begin with a start record")
    start = parsed[0]
    if start.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        reject(
            f"schema_version must be {JOURNAL_SCHEMA_VERSION}, "
            f"found {start.get('schema_version')!r}"
        )
    if not isinstance(start.get("root"), str) or not start["root"]:
        reject("the start record has no root")

    # An apply always moves into the organised folder. An undo moves the other
    # way from the run it undoes: out of the folder for an apply, back into it
    # for an undo (a redo). Either way every file in one record moves the same
    # way, and the first entry says which.
    kind_expected = {"apply": "destination"}.get(start.get("kind"))
    entries: dict[int, dict] = {}
    order: list[int] = []
    for item in parsed[1:]:
        kind = item["record"]
        if kind == "intent":
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                reject("an intent record has no index")
            if index in entries:
                reject(f"index {index} appears twice")
            for name in ("source", "destination"):
                if not isinstance(item.get(name), str) or not item[name]:
                    reject(f"an intent record has no {name}")
            for name in ("size_bytes", "modified_ns"):
                value = item.get(name)
                if not isinstance(value, int) or isinstance(value, bool):
                    reject(f"an intent record has no {name}")
            source = _relative_parts(item["source"])
            destination = _relative_parts(item["destination"])
            if source is None:
                reject(f"the source {item['source']} is not a safe path")
            if destination is None:
                reject(f"the destination {item['destination']} is not a safe path")
            # Exactly one end of every move lives in the organised folder, so a
            # record can never describe a move between two ordinary names.
            stored = _organised_side(source, destination)
            if stored is None:
                reject(
                    f"neither {item['source']} nor {item['destination']} is a file "
                    f"inside {DESTINATION_NAME}"
                )
            if kind_expected is None and start.get("kind") == "undo":
                kind_expected = stored
            if start.get("kind") == "undo" and stored != kind_expected:
                reject(
                    "every file in an undo record must move the same way, but "
                    f"{item['source']} does not"
                )
            if kind_expected is not None and stored != kind_expected:
                reject(
                    f"a {start['kind']} record must move "
                    + (
                        f"into {DESTINATION_NAME}"
                        if kind_expected == "destination"
                        else f"out of {DESTINATION_NAME}"
                    )
                    + f", but {item['source']} does not"
                )
            if item.get("sha256") is not None and not isinstance(item["sha256"], str):
                reject("an intent record has an unusable sha256")
            entries[index] = {
                "source": item["source"],
                "destination": item["destination"],
                "size_bytes": item["size_bytes"],
                "modified_ns": item["modified_ns"],
                "sha256": item.get("sha256"),
                "outcome": None,
                "finished": False,
            }
            order.append(index)
        elif kind == "done":
            index = item.get("index")
            if index not in entries:
                reject("a done record has no matching intent")
            entries[index]["outcome"] = item.get("outcome")
            entries[index]["finished"] = True
    end = next((item for item in parsed if item["record"] == "end"), None)

    def typed(value: object, kind: type):
        """Keep a described field only if it is the type it should be.

        These fields are reported, never acted on, so a record that is odd in
        one of them still reads back rather than being refused.
        """
        if kind is int and isinstance(value, bool):
            return None  # True is an int, but never a count.
        return value if isinstance(value, kind) else None

    planned = typed(start.get("planned"), int)
    if planned is not None and planned < 0:
        planned = None
    passed_over = typed(start.get("skipped"), int)
    if passed_over is None or passed_over < 0:
        passed_over = 0
    return {
        "path": str(path),
        "root": start["root"],
        "plan": start.get("plan"),
        "kind": typed(start.get("kind"), str),
        "started": typed(start.get("time"), str),
        "planned": planned,
        "passed_over": passed_over,
        "entries": [entries[index] for index in order],
        "finished": end is not None,
        "truncated": truncated,
        "complete": end is not None and not truncated,
        "run_completed": typed(end.get("completed"), bool) if end else None,
        "stopped_at": typed(end.get("stopped_at"), str) if end else None,
        # An undo names the record it undid, which is how --show finds out
        # that a run's files were put back rather than never moved.
        "undoing": typed(start.get("undoing"), str),
        "outcomes": typed(end.get("outcomes"), dict) if end else None,
    }


def _confirm_at_original(root: Path, entry: dict) -> str | None:
    """Return why the file is not at its original name, or None if it is.

    This walks down from the root by path, so it also catches a folder on the
    way that has been swapped for a link since the move was made.
    """
    parts = _relative_parts(entry["source"])
    if parts is None:
        return "The record uses a path that could leave the folder"
    try:
        root_fd = _open_root(root)
    except OSError as error:
        return f"Cannot open the selected folder: {error.strerror or error}"
    try:
        metadata, reason = _inspect_parts(root_fd, parts)
    finally:
        os.close(root_fd)
    if reason:
        return reason
    if metadata is None:
        return f"nothing is at {entry['source']}"
    return _source_mismatch(metadata, entry)


def _restore_state(root_fd: int, entry: dict, root: Path) -> tuple[str, str | None]:
    """Decide from what is on disk whether this entry can be put back."""
    source = _relative_parts(entry["source"])
    destination = _relative_parts(entry["destination"])
    if source is None or destination is None:
        return "blocked", "The record uses a path that could leave the folder"
    stored, reason = _inspect_parts(root_fd, destination)
    if reason:
        return "blocked", reason
    original, reason = _inspect_parts(root_fd, source)
    if reason:
        return "blocked", reason
    if stored is not None and original is not None:
        return "blocked", (
            f"A file is in both places: {entry['source']} and {entry['destination']}"
        )
    if stored is None and original is None:
        return "blocked", (
            f"The file is in neither place: {entry['source']} nor {entry['destination']}"
        )
    if original is not None:
        mismatch = _source_mismatch(original, entry)
        if mismatch:
            return "blocked", (
                f"{entry['source']} is not the file that was moved: {mismatch}"
            )
        return "skip", "Already at its original name"
    mismatch = _source_mismatch(stored, entry)
    if mismatch:
        return "blocked", f"{entry['destination']} is not the file that was moved: {mismatch}"
    if entry.get("sha256"):
        digest, problem = _hash_file(
            root.joinpath(*destination),
            entry["destination"],
            entry["size_bytes"],
            entry["modified_ns"],
        )
        if problem:
            return "blocked", problem
        if digest != entry["sha256"]:
            return "blocked", (
                f"Contents of {entry['destination']} changed since they were "
                "compared, even though the size and modification time did not"
            )
    return "restore", None


def _restore_one(root: Path, entry: dict) -> dict:
    """Move one file back, with the same checks and the same no-replace rename."""
    stored = _relative_parts(entry["destination"])
    original = _relative_parts(entry["source"])
    result = {
        "source": entry["source"],
        "destination": entry["destination"],
        "outcome": "not-restored",
        "reason": None,
    }
    if stored is None or original is None:
        result["reason"] = "The record uses a path that could leave the folder"
        return result
    progress = {"outcome": "not-moved"}
    # The move runs in reverse: out of the organised folder, back to the
    # original name. Everything else is identical to applying a plan.
    reason = _move_with_handles(root, stored, original, entry, progress)
    outcome = {
        "moved": "restored",
        "not-moved": "not-restored",
        "unknown": "unknown",
    }[progress["outcome"]]
    if outcome == "restored" and reason:
        outcome = "restored-unconfirmed"
    if outcome == "restored":
        # Last word: look for the file under its original name before calling
        # it restored. Nothing is reported as put back on trust.
        problem = _confirm_at_original(root, entry)
        if problem:
            outcome = "restored-unconfirmed"
            reason = f"the file could not be confirmed afterwards: {problem}"
    result["outcome"] = outcome
    if reason:
        result["reason"] = f"Putting {entry['destination']} back: {reason}"
    return result


def undo_record(record: dict, root: Path, record_path: Path | None = None) -> dict:
    """Put the files in a run record back where they were.

    Every entry is inspected first and nothing moves if any of them cannot be
    put back safely. Each file is then checked again immediately before its
    own move, which is the same single no-replace rename used to apply a plan,
    so nothing is overwritten and nothing leaves the folder. The undo writes
    its own record as it goes.
    """
    if not _SAFE_MOVE_SUPPORTED:
        raise FilePlanError(SAFE_MOVE_UNAVAILABLE)
    root_path = Path(os.path.abspath(Path(root).expanduser()))
    try:
        root_path = root_path.resolve(strict=True)
    except OSError as error:
        raise FilePlanError(
            f"Cannot resolve {root_path}: {error.strerror or error}"
        ) from error
    if os.path.normcase(str(root_path)) != os.path.normcase(record["root"]):
        raise FilePlanError(
            f"The record was made for {record['root']}, not {root_path}"
        )

    entries = record["entries"]
    try:
        root_fd = _open_root(root_path)
    except OSError as error:
        raise FilePlanError(
            f"Cannot open the selected folder: {error.strerror or error}"
        ) from error
    try:
        states = [_restore_state(root_fd, entry, root_path) for entry in entries]
    finally:
        os.close(root_fd)

    blocked = [
        {
            "source": entry["source"],
            "destination": entry["destination"],
            "outcome": "blocked",
            "reason": reason,
        }
        for entry, (state, reason) in zip(entries, states)
        if state == "blocked"
    ]
    new_record = {"path": None, "complete": False, "damaged": False, "warning": None}
    results: list[dict] = []
    stopped = None

    def stop_at(result: dict, position: int) -> None:
        nonlocal stopped
        stopped = dict(result)
        results.extend(
            {
                "source": later["source"],
                "destination": later["destination"],
                "outcome": "not-attempted",
                "reason": "The undo stopped before this file",
            }
            for later in reversed(entries[:position])
        )

    # Newest first, so a run is unwound in the order it was made.
    ordered = list(reversed(list(enumerate(entries))))
    applied = not blocked
    if not applied:
        results = [
            {
                "source": entry["source"],
                "destination": entry["destination"],
                "outcome": "blocked" if state == "blocked" else "not-attempted",
                "reason": reason if state == "blocked" else "The undo was refused",
            }
            for entry, (state, reason) in zip(entries, states)
        ]
    elif ordered:
        journal, reason = _start_journal(
            root_path,
            {
                "kind": "undo",
                "root": str(root_path),
                "undoing": str(record_path) if record_path else record["path"],
                "planned": len(entries),
                # Entries already at their original name are passed over, so
                # they never reach the record either.
                "skipped": sum(1 for state, _ in states if state == "skip"),
            },
        )
        if journal is None:
            first = {
                "source": entries[-1]["source"],
                "destination": entries[-1]["destination"],
                "outcome": "not-attempted",
                "reason": f"The record could not be started, so nothing was moved: {reason}",
            }
            results.append(first)
            stop_at(first, len(entries) - 1)
        else:
            new_record["path"] = journal.path
            try:
                for position, entry in ordered:
                    state, note = states[position]
                    if state == "skip":
                        problem = _confirm_at_original(root_path, entry)
                        if problem:
                            result = {
                                "source": entry["source"],
                                "destination": entry["destination"],
                                "outcome": "not-restored",
                                "reason": "This file looked already put back, but "
                                f"could not be confirmed: {problem}",
                            }
                            results.append(result)
                            stop_at(result, position)
                            break
                        results.append(
                            {
                                "source": entry["source"],
                                "destination": entry["destination"],
                                "outcome": "skipped",
                                "reason": note,
                            }
                        )
                        continue
                    try:
                        journal.write(
                            {
                                "record": "intent",
                                "time": _timestamp(),
                                "index": position,
                                "source": entry["destination"],
                                "destination": entry["source"],
                                "size_bytes": entry["size_bytes"],
                                "modified_ns": entry["modified_ns"],
                                "sha256": entry.get("sha256"),
                            }
                        )
                    except OSError as error:
                        result = {
                            "source": entry["source"],
                            "destination": entry["destination"],
                            "outcome": "not-restored",
                            "reason": "The record could not be written, so this "
                            f"file was not moved: {error.strerror or error}",
                        }
                        results.append(result)
                        stop_at(result, position)
                        break
                    result = _restore_one(root_path, entry)
                    results.append(result)
                    try:
                        journal.write(
                            {
                                "record": "done",
                                "time": _timestamp(),
                                "index": position,
                                "source": entry["destination"],
                                "outcome": result["outcome"],
                                "reason": result["reason"],
                            }
                        )
                    except OSError as error:
                        note = (
                            "the record could not be written afterwards: "
                            f"{error.strerror or error}"
                        )
                        result["reason"] = (
                            f"{result['reason']}; {note}"
                            if result["reason"]
                            else f"The file was put back, but {note}"
                        )
                    if result["reason"]:
                        stop_at(result, position)
                        break
                try:
                    journal.write(
                        {
                            "record": "end",
                            "time": _timestamp(),
                            "completed": stopped is None,
                            "stopped_at": stopped["source"] if stopped else None,
                            "outcomes": {
                                item["source"]: item["outcome"] for item in results
                            },
                        }
                    )
                    new_record["complete"] = not journal.damaged
                except OSError as error:
                    new_record["warning"] = (
                        "The closing record could not be written, so this undo "
                        f"is not finished: {error.strerror or error}"
                    )
                if journal.damaged:
                    new_record["damaged"] = True
                    new_record["warning"] = (
                        (new_record["warning"] + "; ") if new_record["warning"] else ""
                    ) + "a half-written entry could not be removed from the record"
            finally:
                journal.close()

    counts = {
        name: 0
        for name in (
            "restored",
            "restored-unconfirmed",
            "skipped",
            "not-restored",
            "unknown",
            "not-attempted",
            "blocked",
        )
    }
    for item in results:
        counts[item["outcome"]] += 1
    return {
        "schema_version": UNDO_SCHEMA_VERSION,
        "kind": "undo",
        "undoing": record["path"],
        "root": str(root_path),
        "record": new_record,
        "results": results,
        "stopped": stopped,
        "truncated": record["truncated"],
        "undone_record_complete": record["complete"],
        "summary": {
            "entries": len(entries),
            "restored": counts["restored"] + counts["restored-unconfirmed"],
            "restored_confirmed": counts["restored"],
            "restored_unconfirmed": counts["restored-unconfirmed"],
            "skipped": counts["skipped"],
            "not_restored": counts["not-restored"],
            "unknown": counts["unknown"],
            "not_attempted": counts["not-attempted"],
            "blocked": counts["blocked"],
            "record_complete": (
                (new_record["complete"] and not new_record["damaged"]) or not ordered
            ),
            "applied": applied,
            "completed": (
                applied
                and stopped is None
                and (new_record["complete"] or not ordered)
                and not new_record["damaged"]
            ),
        },
    }


RUN_SCHEMA_VERSION = 1
RUN_UNAVAILABLE = (
    "Looking at a run needs the same folder handles as applying, so it needs "
    "Linux or Windows"
)


def latest_record(root: Path) -> Path:
    """Return the newest run record in a folder; raise if there is none."""
    root_path = Path(os.path.abspath(Path(root).expanduser()))
    folder = root_path / DESTINATION_NAME / JOURNAL_DIRECTORY
    try:
        names = [name for name in _list_names(folder) if name.endswith(".jsonl")]
    except FileNotFoundError:
        names = []
    except OSError as error:
        raise FilePlanError(
            f"Cannot look for run records in {folder}: {error.strerror or error}"
        ) from error
    if not names:
        raise FilePlanError(
            f"No run records in {folder}, so there is no run to look at"
        )
    # A record is named for the time its run started, so the last name sorts
    # newest. Ties within the same microsecond keep the higher process id.
    return folder / max(names)


_PUT_BACK = frozenset({"restored", "restored-unconfirmed"})
# What a recorded outcome says about whether the file left its original name.
# "unknown" is in neither: the run could not tell, and nothing here may decide
# for it. These come from the record alone and never change with what is on
# disk afterwards.
_RECORD_SAYS_MOVED = frozenset(
    {"moved", "moved-unconfirmed", "restored", "restored-unconfirmed"}
)
_RECORD_SAYS_STAYED = frozenset({"not-moved", "not-restored"})


def _later_runs(record_name: str, root: Path) -> list[dict]:
    """Find every undo that follows from a record, at any depth, oldest first.

    An undo of the record puts its files back; an undo of that undo (a redo)
    moves them back in again; an undo of the redo puts them back again, and so
    on. Each result names its record, the record it undid, its depth in that
    chain, which way it moved files, how it ended, and the outcome it recorded
    for each of this run's files, keyed by the name the file had in the record
    it undid. A file a run was moving when it was cut off is "no-outcome".

    Everything here is read from records, never from where files are now, and
    each record is fixed once its run ends. So a later undo or redo can add to
    what is reported, but nothing it does can change what an earlier run is
    credited with. A record that cannot be read is passed over.
    """
    folder = root / DESTINATION_NAME / JOURNAL_DIRECTORY
    try:
        names = sorted(name for name in _list_names(folder) if name.endswith(".jsonl"))
    except OSError:
        return []
    children: dict[str, list[tuple[str, dict]]] = {}
    for name in names:
        try:
            other = load_record(folder / name)
        except FilePlanError:
            continue
        if other["kind"] != "undo" or not other["undoing"]:
            continue
        # Records are named uniquely and live in the one folder, so the name
        # identifies the record even if the undo was given a relative path.
        parent = os.path.normcase(Path(other["undoing"]).name)
        children.setdefault(parent, []).append((name, other))

    found = []
    seen = {os.path.normcase(record_name)}
    pending = [(record_name, 1)]
    while pending:
        parent_name, depth = pending.pop()
        for name, other in children.get(os.path.normcase(parent_name), ()):
            if os.path.normcase(name) in seen:
                continue
            seen.add(os.path.normcase(name))
            outcomes: dict[str, str] = {}
            for entry in other["entries"]:
                # An undo moves each file from where the record it undid left
                # it back to where that record found it.
                outcomes[entry["destination"]] = (
                    entry["outcome"] if isinstance(entry["outcome"], str) else "no-outcome"
                )
            for source, outcome in (other["outcomes"] or {}).items():
                if isinstance(source, str) and isinstance(outcome, str):
                    outcomes.setdefault(source, outcome)
            found.append(
                {
                    "record": str(folder / name),
                    "undoing": str(folder / parent_name),
                    "depth": depth,
                    # Odd depths reverse the run being shown; even depths
                    # repeat it. This is what matching files up relies on.
                    "reverses": bool(depth % 2),
                    # Which way this run physically moved files, read from its
                    # own entries: "in" to the organised folder or "out" of it.
                    # None only for a run that names no files; filled in below.
                    "direction": _moves(other["entries"]),
                    "outcomes": outcomes,
                    "record_complete": other["complete"],
                    "truncated": other["truncated"],
                    "completed": bool(other["complete"] and other["run_completed"]),
                    "stopped_at": other["stopped_at"],
                }
            )
            pending.append((name, depth + 1))
    found.sort(key=lambda run: Path(run["record"]).name)
    return found


def _moves(entries: list[dict]) -> str | None:
    """Which way a record physically moved its files, read from its entries.

    "in" means into the organised folder, "out" means out of it. Every entry
    in one record moves the same way, which reading the record checks.
    """
    for entry in entries:
        source = _relative_parts(entry["source"])
        destination = _relative_parts(entry["destination"])
        if source is None or destination is None:
            continue
        side = _organised_side(source, destination)
        if side == "destination":
            return "in"
        if side == "source":
            return "out"
    return None


def _flip(direction: str | None) -> str | None:
    return {"in": "out", "out": "in"}.get(direction)


def _outcome_for(run: dict, entry: dict) -> str | None:
    """What a later run recorded for one of this run's files.

    A run that reverses this one keys each file by where this run found it; a
    run that repeats this one keys it by where this run left it. That holds
    whichever way this run moved files, so it depends only on the relationship.
    """
    key = entry["source"] if run["reverses"] else entry["destination"]
    return run["outcomes"].get(key)


def inspect_run(record: dict, root: Path, record_path: Path | None = None) -> dict:
    """Say what a run record describes and where each of its files is now.

    Nothing is moved, created, or written. Every entry is looked at on disk
    with the same checks an undo would make, so the report also says what an
    undo would be able to do. A run that was cut off leaves an entry with no
    outcome, which is the file that was in flight when it stopped.
    """
    if not _SAFE_MOVE_SUPPORTED:
        raise FilePlanError(RUN_UNAVAILABLE)
    root_path = Path(os.path.abspath(Path(root).expanduser()))
    try:
        root_path = root_path.resolve(strict=True)
    except OSError as error:
        raise FilePlanError(
            f"Cannot resolve {root_path}: {error.strerror or error}"
        ) from error
    if os.path.normcase(str(root_path)) != os.path.normcase(record["root"]):
        raise FilePlanError(
            f"The record was made for {record['root']}, not {root_path}"
        )

    entries = record["entries"]
    try:
        root_fd = _open_root(root_path)
    except OSError as error:
        raise FilePlanError(
            f"Cannot open the selected folder: {error.strerror or error}"
        ) from error
    try:
        states = [_restore_state(root_fd, entry, root_path) for entry in entries]
    finally:
        os.close(root_fd)

    # A record names the two ends of each move, and one of them is always
    # inside the organised folder. Saying which end holds the file now works
    # the same way for a run that applied a plan and one that undid it.
    where = {"restore": "destination", "skip": "source", "blocked": "unclear"}
    shown = str(record_path) if record_path is not None else record["path"]
    later = _later_runs(Path(shown).name, root_path)
    # Which way the shown run moved its files. An apply always moves them in;
    # an undo moves them whichever way its entries say.
    moves = _moves(entries) or ("in" if record["kind"] == "apply" else None)
    for run in later:
        if run["direction"] is None:
            run["direction"] = _flip(moves) if run["reverses"] else moves
    results = []
    outcomes: dict[str, int] = {}
    counts = {"destination": 0, "source": 0, "unclear": 0}
    # "said" is the record's own account of whether each file moved; it is
    # fixed the moment the run ends.
    said = {"moved": 0, "unconfirmed": 0, "stayed": 0, "unknown": 0, "no-outcome": 0}
    # Read from the later runs' records alone, like "said": each is fixed
    # once its run ends, so any number of undos and redos can add to these
    # but none can change what an earlier one is credited with.
    put_back_by_any = 0
    moved_again_by_any = 0
    # The one count here that comes from the disk: files the record says
    # moved, found at their original name, with no later run to account for it.
    back_unexplained = 0
    for entry, (state, reason) in zip(entries, states):
        counts[where[state]] += 1
        outcomes[entry["outcome"] or "no-outcome"] = (
            outcomes.get(entry["outcome"] or "no-outcome", 0) + 1
        )
        # The latest later run that tried this file, and the latest one that
        # confirmed moving it, in whichever direction.
        tried = confirmed = None
        tried_outcome = None
        for run in later:
            outcome = _outcome_for(run, entry)
            # "skipped" means that run found the file already where it would
            # have put it, so it tried nothing; an earlier run's word stands.
            if outcome not in (None, "not-attempted", "skipped"):
                tried, tried_outcome = run, outcome
            if outcome == "restored":
                confirmed = run
        if any(r["reverses"] and _outcome_for(r, entry) == "restored" for r in later):
            put_back_by_any += 1
        if any(not r["reverses"] and _outcome_for(r, entry) == "restored" for r in later):
            moved_again_by_any += 1
        # Credited with where the file is only when its record confirms the
        # move and the file is found there now.
        undone_by = (
            confirmed["record"]
            if confirmed and confirmed["reverses"] and where[state] == "source"
            else None
        )
        redone_by = (
            confirmed["record"]
            if confirmed and not confirmed["reverses"] and where[state] == "destination"
            else None
        )
        recorded = entry["outcome"]
        if recorded is None:
            said["no-outcome"] += 1
        elif recorded in _RECORD_SAYS_MOVED:
            said["moved"] += 1
            if recorded not in ("moved", "restored"):
                # It left its name, but the look afterwards found a problem.
                said["unconfirmed"] += 1
        elif recorded in _RECORD_SAYS_STAYED:
            said["stayed"] += 1
        else:
            said["unknown"] += 1
        if (
            where[state] == "source"
            and recorded in _RECORD_SAYS_MOVED
            and not undone_by
            and not (tried and tried["reverses"])
        ):
            back_unexplained += 1
        undo_record = tried["record"] if tried else None
        undo_outcome = tried_outcome
        undo_direction = tried["direction"] if tried else None
        undo_reverses = tried["reverses"] if tried else None
        undo_depth = tried["depth"] if tried else None
        results.append(
            {
                "source": entry["source"],
                "destination": entry["destination"],
                "recorded": entry["outcome"],
                "finished": entry["finished"],
                "undone_by": undone_by,
                "undo_record": undo_record,
                "undo_outcome": undo_outcome,
                "undo_direction": undo_direction,
                "undo_reverses": undo_reverses,
                "undo_depth": undo_depth,
                "redone_by": redone_by,
                "where": where[state],
                "undo": {
                    "restore": "would-move",
                    "skip": "nothing-to-do",
                    "blocked": "blocked",
                }[state],
                "reason": reason if state == "blocked" else None,
            }
        )
    unfinished = [item["source"] for item in results if not item["finished"]]
    planned = record["planned"]
    passed_over = record["passed_over"]
    never_started = (
        max(planned - passed_over - len(entries), 0) if planned is not None else None
    )
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "kind": "run",
        "record": shown,
        "run": record["kind"],
        # Which way the shown run moved files: "in" to the organised folder or
        # "out" of it. Every direction in this report is physical like this.
        "moves": moves,
        "undoing": record["undoing"],
        # Every undo that follows from this run, at any depth, oldest first.
        # Each count comes from that run's own record and never changes.
        "undo_runs": [
            {
                "record": run["record"],
                "undoing": run["undoing"],
                "depth": run["depth"],
                "reverses": run["reverses"],
                "direction": run["direction"],
                "completed": run["completed"],
                "record_complete": run["record_complete"],
                "truncated": run["truncated"],
                "stopped_at": run["stopped_at"],
                # Only moves its record confirms. Moves it made but could not
                # confirm, and the move it was making when cut off, are
                # counted apart, so nothing is credited that was not confirmed.
                "files": sum(
                    1 for entry in entries if _outcome_for(run, entry) == "restored"
                ),
                "unconfirmed": sum(
                    1
                    for entry in entries
                    if _outcome_for(run, entry) == "restored-unconfirmed"
                ),
                "no_outcome": sum(
                    1 for entry in entries if _outcome_for(run, entry) == "no-outcome"
                ),
            }
            for run in later
        ],
        "root": str(root_path),
        "plan": record["plan"],
        "started": record["started"],
        "entries": results,
        # The last entry with no outcome is the file that was in flight when a
        # cut-off run stopped. In a finished record it means the run could not
        # write down what happened, which is just as unsettled.
        "cut_off_at": unfinished[-1] if unfinished and not record["complete"] else None,
        "outcomes": outcomes,
        "summary": {
            "entries": len(entries),
            "planned": planned,
            "passed_over": passed_over,
            "never_started": never_started,
            "no_outcome": len(unfinished),
            # From the record alone; these do not move when the files do.
            "recorded_moved": said["moved"],
            # Of those, the ones moved without the move being confirmed.
            "recorded_moved_unconfirmed": said["unconfirmed"],
            "recorded_not_moved": said["stayed"],
            "recorded_unknown": said["unknown"],
            # What has become of those files since the run ended.
            "put_back": put_back_by_any,
            "moved_again": moved_again_by_any,
            "back_without_undo": back_unexplained,
            "at_destination": counts["destination"],
            "at_source": counts["source"],
            "unclear": counts["unclear"],
            "record_complete": record["complete"],
            "truncated": record["truncated"],
            "run_completed": record["run_completed"],
            "stopped_at": record["stopped_at"],
            "undo_possible": counts["unclear"] == 0,
        },
    }


# Every direction shown is physical: "in" is into the organised folder and
# "out" is out of it, read from each run's own entries. Whether a run reversed
# or repeated the one being shown is kept apart and never decides the words.
_MOVE = {"in": "into the organised folder", "out": "out of the organised folder"}
_PLACE = {"in": "in the organised folder", "out": "outside the organised folder"}


def _fell_short(direction: str | None, outcome: str | None) -> str | None:
    """What a later run that tried a file, but did not settle it, is said to have done."""
    where = f" {_MOVE[direction]}" if direction in _MOVE else ""
    return {
        "no-outcome": f"was cut off while moving it{where}",
        "not-restored": f"did not move it{where}",
        "unknown": f"could not tell whether it moved it{where}",
        # Its record says the move happened but the look afterwards failed,
        # so say what it recorded rather than that the move worked.
        "restored-unconfirmed": f"recorded moving it{where} but could not confirm it",
        "restored": f"moved it{where}, but it has been moved here again since",
    }.get(outcome)


def _how_it_stopped(run: dict) -> str:
    """Say why a run that did not finish did not; never called for one that did."""
    if run["truncated"] or not run["record_complete"]:
        # A kill and a failed final write look the same here: no closing entry.
        return "its record was never closed"
    if run["stopped_at"]:
        return f"stopped early at {_quote(run['stopped_at'])}"
    return "stopped early"


def _later_run_line(run: dict, entries: int) -> str:
    """Say how one later undo of this run ended, and what its record credits it with.

    The count comes from that run's own record, so it reads the same however
    many undos and redos follow, and the direction comes from that run's own
    entries. Only a run whose record says it completed is called finished.
    """
    name = _quote(Path(run["record"]).name)
    undid = _quote(Path(run["undoing"]).name)
    where = f" {_MOVE[run['direction']]}" if run["direction"] in _MOVE else ""
    did = (
        f"its record confirms it moved {run['files']} of this run's {entries} "
        f"files{where}"
    )
    if run["unconfirmed"]:
        did += f", and {run['unconfirmed']} more it could not confirm"
    if run["no_outcome"]:
        did += (
            f", and it was moving {run['no_outcome']} more when it stopped, "
            "with no outcome written"
        )
    if run["completed"]:
        if run["depth"] == 1:
            return f"Undone since by {name}, which finished; {did}."
        return f"{undid} was itself undone by {name}, which finished; {did}."
    what = "An undo" if run["depth"] == 1 else f"An undo of {undid}"
    return (
        f"{what} was started in {name} but {_how_it_stopped(run)}, so it did "
        f"not finish; {did}."
    )


def _where_note(item: dict, source: str) -> str:
    """Say where a file is now and which later run, if any, accounts for it.

    Never contradicts the record: a file is only said not to have moved when
    the record says so, and a file with no recorded outcome is never said to
    have stayed put. Directions are physical, from the later run's own entries.
    """
    name = _quote(Path(item["undo_record"]).name) if item["undo_record"] else None
    direction = item["undo_direction"]
    outcome = item["undo_outcome"]
    note = _fell_short(direction, outcome) if name else None
    if item["where"] == "destination":
        if item["redone_by"]:
            redo = _quote(Path(item["redone_by"]).name)
            return f"the file is there now; the undo in {redo} moved it here again"
        if note and not (item["undo_reverses"] is False and outcome == "restored"):
            return f"the file is there now; the undo in {name} {note}"
        return "the file is there now"
    if item["where"] == "source":
        if item["undone_by"]:
            undo = _quote(Path(item["undone_by"]).name)
            return f"put back at {source} by the undo in {undo}"
        if note and outcome == "no-outcome":
            return (
                f"the file is at {source}; the undo in {name} {note}, so that "
                "undo never confirmed it"
            )
        if note:
            return f"the file is at {source}; the undo in {name} {note}"
        if item["recorded"] in _RECORD_SAYS_MOVED:
            # The record says it moved, so never claim it did not.
            return (
                f"the file is back at {source}; this run moved it, but no "
                "undo record says it was put back"
            )
        if item["recorded"] in _RECORD_SAYS_STAYED:
            return f"the file is still at {source}; the record says it did not move"
        if item["recorded"]:
            # An unknown outcome stays unknown.
            return (
                f"the file is at {source}; this run could not tell whether it "
                "moved, and nothing since says either"
            )
        # No outcome was ever written for this file. Finding it at its
        # original name now does not show it never moved: it may have moved
        # and been put back by something with no record. Say only what is known.
        return (
            f"the file is at {source} now; the record has no outcome for it, "
            "so whether it moved is not known"
        )
    reason = f"in doubt: {_quote(item['reason'])}"
    if note:
        reason += f"; the undo in {name} {note}"
    return reason


def _were(count: int) -> str:
    return "was" if count == 1 else "were"


def _record_account(summary: dict, moves: str | None = None) -> str:
    """State what the record says became of its files, then what has happened
    since. The first half is read from the record only, so it reads the same
    whether or not the run has since been undone."""
    entries = summary["entries"]
    confirmed = summary["recorded_moved"] - summary["recorded_moved_unconfirmed"]
    said = f"The record confirms {confirmed} of its {entries} files moved"
    rest = []
    if summary["recorded_moved_unconfirmed"]:
        count = summary["recorded_moved_unconfirmed"]
        rest.append(f"{count} moved but could not be confirmed")
    if summary["recorded_not_moved"]:
        rest.append(f"{summary['recorded_not_moved']} did not")
    if summary["recorded_unknown"]:
        rest.append(
            f"{summary['recorded_unknown']} could not be told either way"
        )
    if summary["no_outcome"]:
        count = summary["no_outcome"]
        rest.append(f"{count} {_were(count)} left with no outcome")
    if rest:
        said += ", " + ", ".join(rest[:-1] + [f"and {rest[-1]}"])
    since = []
    if summary["put_back"]:
        count = summary["put_back"]
        back = _MOVE.get(_flip(moves))
        since.append(
            f"{count} {_were(count)} moved back" + (f" {back}" if back else "")
        )
    if summary["moved_again"]:
        count = summary["moved_again"]
        again = _MOVE.get(moves)
        since.append(
            f"{count} {_were(count)} moved" + (f" {again}" if again else "")
            + " again by undoing an undo"
        )
    if summary["back_without_undo"]:
        count = summary["back_without_undo"]
        since.append(
            f"{count} {'is' if count == 1 else 'are'} back with no undo record "
            "to say why"
        )
    if not since:
        return said + "."
    # Every "since" count below comes from undo records confirming the move,
    # except files found back with no undo record, which come from the disk.
    confirmed_since = [part for part in since if "no undo record" not in part]
    if confirmed_since:
        since[0] = "undo records confirm " + since[0]
    if len(since) > 1:
        since = since[:-1] + [f"and {since[-1]}"]
    return said + "; since then " + (", " if len(since) > 2 else " ").join(since) + "."


def _plain_run_output(report: dict) -> str:
    summary = report["summary"]
    if report["run"] == "undo" and report["undoing"]:
        kind = f"undoing {_quote(Path(report['undoing']).name)}"
    else:
        kind = {"apply": "applying a plan", "undo": "undoing a run"}.get(
            report["run"], "a run"
        )
    lines = [
        f"Run record {_quote(report['record'])}",
        f"From {kind} in {_quote(report['root'])}"
        + (f", started {report['started']}" if report["started"] else ""),
    ]
    if report["moves"] in _MOVE:
        # Which way this run set out to move files, since an undo can go
        # either way. What it confirms is said below, file by file.
        lines.append(f"It set out to move files {_MOVE[report['moves']]}.")
    if report["plan"]:
        lines.append(f"Plan: {_quote(report['plan'])}")
    if summary["truncated"]:
        lines.append(
            "The run was cut off: the record ends with an unfinished line, so "
            "it may be missing its last entry."
        )
    elif not summary["record_complete"]:
        lines.append(
            "The run was cut off: the record has no closing entry, so it never "
            "reached the end."
        )
    elif summary["run_completed"]:
        lines.append("The run finished and the record is complete.")
    else:
        stopped = summary["stopped_at"]
        lines.append(
            "The run stopped early"
            + (f" at {_quote(stopped)}" if stopped else "")
            + ", and the record is complete."
        )
    if summary["planned"] is not None:
        covered = (
            f"Planned {summary['planned']} files; the record covers "
            f"{summary['entries']}."
        )
        if summary["passed_over"]:
            aside = summary["passed_over"]
            covered += (
                f" {aside} {'was' if aside == 1 else 'were'} left alone on purpose."
            )
        if summary["never_started"]:
            started = summary["never_started"]
            covered += (
                f" {started} {'file was' if started == 1 else 'files were'} "
                "never started."
            )
        lines.append(covered)
    if report["entries"]:
        lines.append(_record_account(summary, report["moves"]))
    for item in report["entries"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        source = _quote(item["source"])
        destination = _quote(item["destination"])
        label = item["recorded"] or "no outcome"
        line = f"[{label}] {source} -> {destination} ({_where_note(item, source)})"
        lines.append(line)
    if report["cut_off_at"]:
        lines.append(
            f"In flight when it stopped: {_quote(report['cut_off_at'])}."
            " The line above says where that file is now."
        )
    for run in report["undo_runs"]:
        lines.append(_later_run_line(run, summary["entries"]))
    if not report["entries"]:
        lines.append("The record names no files, so nothing was moved.")
    elif report["undo_runs"] and not summary["at_destination"] and not summary["unclear"]:
        lines.append(
            "Nothing is left to undo: every file is back where this run found it"
            + (f", {_PLACE[_flip(report['moves'])]}." if report["moves"] else ".")
        )
    elif summary["unclear"]:
        lines.append(
            f"{summary['unclear']} of {summary['entries']} entries are in doubt, "
            "so --undo would be refused until they are settled."
        )
    else:
        lines.append(
            f"--undo on this record would move {summary['at_destination']} "
            f"file{'' if summary['at_destination'] == 1 else 's'}"
            + (
                f" and leave {summary['at_source']} alone."
                if summary["at_source"]
                else "."
            )
        )
    lines.append("Look only. No files were changed.")
    return "\n".join(lines) + "\n"


def _plain_undo_output(report: dict) -> str:
    summary = report["summary"]
    lines = [
        f"Undoing {_quote(report['undoing'])} in "
        f"{_quote(report['root'])}"
    ]
    if report["truncated"]:
        lines.append(
            "The record is incomplete: it ends with an unfinished line, so it "
            "may be missing its last entry."
        )
    elif not report["undone_record_complete"]:
        lines.append(
            "The record is incomplete: it has no closing entry, so the run it "
            "describes was cut off."
        )
    if report["record"]["path"]:
        lines.append(f"Record: {_quote(report['record']['path'])}")
    if report["record"]["warning"]:
        lines.append(f"Record incomplete: {report['record']['warning']}")
    if not summary["applied"]:
        lines.append(
            f"Refused: {summary['blocked']} of {summary['entries']} entries cannot "
            "be put back safely."
        )
    labels = {
        "restored": "restored",
        "restored-unconfirmed": "restored, unconfirmed",
        "skipped": "nothing to do",
        "not-restored": "not restored",
        "unknown": "unknown",
        "not-attempted": "not attempted",
        "blocked": "blocked",
    }
    for item in report["results"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        stored = _quote(item["destination"])
        line = f"[{labels[item['outcome']]}] {stored}"
        if item["outcome"] in ("restored", "restored-unconfirmed"):
            line += f" -> {_quote(item['source'])}"
        if item["reason"] and item["outcome"] != "not-attempted":
            line += f" ({_quote(item['reason'])})"
        lines.append(line)
    if not summary["applied"]:
        lines.append("Nothing was moved. Look at the blocked entries above.")
        return "\n".join(lines) + "\n"
    if summary["completed"]:
        lines.append(
            f"Put back {summary['restored']} of {summary['entries']} files"
            + (
                f"; {summary['skipped']} needed nothing."
                if summary["skipped"]
                else "."
            )
        )
        return "\n".join(lines) + "\n"
    if report["stopped"] is None:
        # Every file was dealt with, but the undo's own record never closed,
        # so it is not reported as finished.
        lines.append(
            f"Put back {summary['restored']} of {summary['entries']} files, but "
            "the record of this undo is not finished, so the undo is not "
            "complete and that record cannot be relied on to reverse it."
        )
        return "\n".join(lines) + "\n"
    counted = [f"{summary['restored_confirmed']} put back"]
    if summary["restored_unconfirmed"]:
        counted.append(f"{summary['restored_unconfirmed']} put back but unconfirmed")
    if summary["unknown"]:
        counted.append(f"{summary['unknown']} in an unknown state")
    if summary["skipped"]:
        counted.append(f"{summary['skipped']} needed nothing")
    counted.append(f"{summary['not_restored']} not put back")
    if summary["not_attempted"]:
        counted.append(f"{summary['not_attempted']} not attempted")
    where = (
        _quote(report["stopped"]["destination"])
        if report["stopped"]
        else "the end"
    )
    lines.append(
        f"Stopped at {where}. Of {summary['entries']} entries: "
        + ", ".join(counted)
        + "."
    )
    return "\n".join(lines) + "\n"


def _plain_output(plan: dict) -> str:
    summary = plan["summary"]
    lines = [
        f"Preview for {_quote(plan['root'])}",
        f"{summary['files']} files: {summary['ready']} ready, "
        f"{summary['conflicts']} conflicts, "
        + (f"{summary['skipped']} skipped by choice, " if summary.get("skipped") else "")
        + f"{summary['total_bytes']} bytes",
        f"Destination: {_quote(plan['destination_root'])}",
    ]
    if plan["options"].get("check_duplicates"):
        lines.append("Duplicate check: contents of files that would share a name were compared")
    rules = plan["options"].get("rules")
    if rules is not None:
        fallback = rules["fallback"]
        unmatched = (
            "go by extension"
            if fallback == "by-extension"
            else f"go to {_quote(fallback['folder'])}"
        )
        count = len(rules["rules"])
        lines.append(
            f"Rules: {count} custom rule{'s' if count != 1 else ''}; unmatched files {unmatched}"
        )
    for entry in plan["entries"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        source = _quote(entry["source"])
        destination = _quote(entry["destination"])
        line = f"[{entry['status']}] {source} -> {destination}"
        if entry.get("rule"):
            line += f" by rule {_quote(entry['rule'])}"
        if entry.get("choice") == "rename" and entry.get("renamed_from"):
            line += f", renamed by choice from {_quote(entry['renamed_from'])}"
        elif entry.get("choice") == "keep":
            line += ", kept by choice"
        if entry["reason"]:
            line += f" ({_quote(entry['reason'])})"
        lines.append(line)
    if plan["skipped"]:
        lines.append(f"Skipped {len(plan['skipped'])} entries:")
        for entry in plan["skipped"]:
            path = _quote(entry["path"])
            lines.append(f"  {path}: {entry['reason']}")
    lines.append("Preview only. No files were changed.")
    return "\n".join(lines) + "\n"


def _plain_verification_output(report: dict) -> str:
    summary = report["summary"]
    source = report["plan"] or "the saved plan"
    lines = [
        f"Verification of {_quote(source)} against "
        f"{_quote(report['root'])}",
        f"{summary['entries']} entries: {summary['unchanged']} unchanged, "
        f"{summary['changed']} changed, {summary['missing']} missing, "
        f"{summary['conflicts']} conflicts, {summary['new']} new"
        + (f", {summary['skipped']} skipped by choice" if summary.get("skipped") else ""),
    ]
    for entry in report["entries"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        path = _quote(entry["source"])
        line = f"[{entry['status']}] {path}"
        effective = entry.get("effective_destination")
        if effective and effective != entry["destination"]:
            line += f" -> {_quote(effective)}"
        if entry["reason"]:
            line += f" ({_quote(entry['reason'])})"
        lines.append(line)
    if summary["problems"]:
        lines.append(f"{summary['problems']} entries need review before any move.")
    else:
        lines.append("The plan still matches this folder.")
    lines.append("Check only. No files were changed.")
    return "\n".join(lines) + "\n"


def _plain_application_output(report: dict) -> str:
    summary = report["summary"]
    # A plan settled in the terminal was never saved, so it has no path.
    source = (
        _quote(report["plan"])
        if report["plan"]
        else "the plan just settled here"
    )
    lines = [
        f"Applying {source} to {_quote(report['root'])}"
    ]
    if not summary["applied"]:
        lines.append(
            f"Refused: {summary['problems']} of {summary['planned']} entries need "
            "review first."
        )
        for entry in report["verification"]["entries"]:
            if entry["status"] != "unchanged":
                # JSON quoting keeps newlines and other filename controls on one line.
                path = _quote(entry["source"])
                reason = _quote(entry["reason"])
                lines.append(f"  [{entry['status']}] {path} ({reason})")
        lines.append("Nothing was moved. Make a new plan once these are settled.")
        return "\n".join(lines) + "\n"
    if report["record"]["path"]:
        lines.append(f"Record: {_quote(report['record']['path'])}")
    if report["record"]["warning"]:
        lines.append(f"Record incomplete: {report['record']['warning']}")
    if report["stopped"] and report["stopped"]["outcome"] == "not-attempted":
        reason = _quote(report["stopped"]["reason"])
        lines.append(f"Stopped before any move: {reason}")
    labels = {
        "moved": "moved",
        "moved-unconfirmed": "moved, unconfirmed",
        "not-moved": "not moved",
        "unknown": "unknown",
        "not-attempted": "not attempted",
        "skipped": "skipped by choice",
    }
    for entry in report["results"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        path = _quote(entry["source"])
        line = f"[{labels[entry['outcome']]}] {path}"
        if entry["outcome"] in ("moved", "moved-unconfirmed"):
            line += f" -> {_quote(entry['destination'])}"
        if entry["reason"] and entry["outcome"] != "not-attempted":
            line += f" ({_quote(entry['reason'])})"
        lines.append(line)
    if summary["completed"]:
        lines.append(
            f"Moved {summary['moved']} of {summary['planned']} files."
            + (f" {summary['skipped']} skipped by choice." if summary.get("skipped") else "")
        )
        return "\n".join(lines) + "\n"
    if report["stopped"] is None:
        lines.append(
            f"Moved {summary['moved']} of {summary['planned']} files, but the "
            "record of this run is not finished, so it cannot be relied on to "
            "undo the run."
        )
        return "\n".join(lines) + "\n"
    counted = [f"{summary['moved_confirmed']} moved"]
    if summary["moved_unconfirmed"]:
        counted.append(f"{summary['moved_unconfirmed']} moved but unconfirmed")
    if summary["unknown"]:
        counted.append(f"{summary['unknown']} in an unknown state")
    counted.append(f"{summary['not_moved']} not moved")
    if summary.get("skipped"):
        counted.append(f"{summary['skipped']} skipped by choice")
    if summary["not_attempted"]:
        counted.append(f"{summary['not_attempted']} not attempted")
    stopped_at = _quote(report["stopped"]["source"])
    noun = "file" if summary["planned"] == 1 else "files"
    lines.append(
        f"Stopped at {stopped_at}. Of {summary['planned']} {noun}: "
        + ", ".join(counted)
        + "."
    )
    if summary["moved_unconfirmed"] or summary["unknown"]:
        lines.append(
            "Look at the files above that are unconfirmed or unknown before "
            "planning again."
        )
    return "\n".join(lines) + "\n"


# Characters never printed as they are: control characters, line and
# paragraph separators, lone surrogates (bytes in a filename that were not
# valid text), and bidirectional controls that could make one name read as
# another. Joiners and variation selectors, which emoji are built from, are
# printed as they are.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _unsafe(character: str) -> bool:
    return (
        unicodedata.category(character) in ("Cc", "Cs", "Zl", "Zp")
        or character in _BIDI_CONTROLS
    )


def _quote(text: str) -> str:
    """Quote a name or path for the terminal so it can be copied back in.

    Almost every name is printed exactly as it is between double quotes, emoji
    and Windows backslashes included, so a path in the output can be pasted
    straight into the next command. Only a name holding something that cannot
    be shown safely is written with backslash escapes instead: a control
    character such as a newline, a bidirectional control, or bytes that were
    not valid text. So is a name containing a double quote, or, outside
    Windows, a backslash, since either would make the escapes ambiguous.
    Windows forbids both in names, so there a backslash is only ever a
    separator. Seeing a backslash escape therefore always means the name was
    escaped, and an escaped name reads exactly like JSON.
    """
    plain = (
        '"' not in text
        and (os.sep == "\\" or "\\" not in text)
        and not any(_unsafe(character) for character in text)
    )
    if plain:
        return f'"{text}"'
    escaped = []
    for character in text:
        if character in _ESCAPES:
            escaped.append(_ESCAPES[character])
        elif _unsafe(character):
            code = ord(character)
            escaped.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _speak_utf8(stream: TextIO) -> None:
    """Write UTF-8 to a pipe or file, so names arrive exactly as they are.

    A Windows console already takes Unicode. A pipe or a redirect to a file
    otherwise falls back to the local code page, such as cp1252, which has no
    emoji, so names would arrive as escapes and could not be pasted back in.
    PYTHONIOENCODING, when set, is left to decide.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    encoding = (getattr(stream, "encoding", None) or "").replace("_", "-").lower()
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None and encoding not in ("utf-8", "utf8"):
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass  # Keep writing in the stream's own encoding; _write copes.


def _write(stream: TextIO, text: str) -> None:
    encoding = getattr(stream, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(text)


class _NoAnswer(Exception):
    """There is nothing more to read, so no answer was given."""


# Both the whole word and its first letter are accepted, in any case.
_CHOICE_WORDS = {
    "k": "keep", "keep": "keep",
    "s": "skip", "skip": "skip",
    "r": "rename", "rename": "rename",
    "q": "quit", "quit": "quit", "stop": "quit",
}
_YES_NO = {"y": "yes", "yes": "yes", "n": "no", "no": "no", "q": "no", "quit": "no"}


def _ask(
    question: str,
    words: dict[str, str],
    reader: TextIO,
    writer: TextIO,
    empty: str | None = None,
) -> str:
    """Ask until one of the words is given; raise _NoAnswer at the end of input.

    Nothing is ever assumed from an unreadable answer: an empty line means
    only what ``empty`` says it means, and anything else is asked again.
    """
    while True:
        _write(writer, question)
        writer.flush()
        line = reader.readline()
        if not line:
            raise _NoAnswer
        text = line.strip().casefold()
        if not text:
            if empty is not None:
                return empty
            continue
        answer = words.get(text)
        if answer is not None:
            return answer
        wanted = sorted(set(words.values()))
        _write(writer, f"Please answer {', '.join(wanted)}.\n")


_OPTION_LABELS = {
    "keep": "[k] keep this name",
    "skip": "[s] skip this file",
    "rename": "[r] rename to a free number",
}


def _show_conflict(
    entry: dict,
    plan: dict,
    number: int,
    total: int,
    ruled_out: dict[str, str],
    writer: TextIO,
) -> None:
    """Describe one conflict, and any answer already shown not to work."""
    # JSON quoting keeps newlines and other filename controls on one line.
    source = _quote(entry["source"])
    destination = _quote(entry["destination"])
    lines = ["", f"Conflict {number} of {total}: {source} -> {destination}"]
    if entry["reason"]:
        lines.append(f"  {entry['reason']}")
    others = [
        other["source"]
        for other in plan["entries"]
        if other is not entry
        and other["status"] != "skipped"
        and other["destination"].casefold() == entry["destination"].casefold()
    ]
    if others:
        lines.append(
            "  Also wanting this name: "
            + ", ".join(_quote(name) for name in others)
        )
    for answer, why in ruled_out.items():
        lines.append(f"  {answer.capitalize()} did not work for this file: {why}")
    _write(writer, "\n".join(lines) + "\n")


def _ask_choice(
    source: str, ruled_out: dict[str, str], reader: TextIO, writer: TextIO
) -> str:
    """Ask for keep, skip, or rename, offering only answers that can still work.

    Raises _NoAnswer at the end of input. Skip is never ruled out, since a
    file left where it is cannot clash with anything.
    """
    options = [option for option in ("keep", "skip", "rename") if option not in ruled_out]
    _write(
        writer,
        "  " + "   ".join(_OPTION_LABELS[option] for option in options) + "   [q] stop\n",
    )
    letters = "/".join(option[0] for option in options) + "/q"
    # JSON quoting keeps newlines and other filename controls on one line.
    quoted = _quote(source)
    while True:
        answer = _ask(f"Choose for {quoted} [{letters}]: ", _CHOICE_WORDS, reader, writer)
        if answer not in ruled_out:
            return answer
        _write(
            writer,
            f"  {answer.capitalize()} cannot work for this file: {ruled_out[answer]}. "
            f"Please answer {', '.join(options)}, or quit.\n",
        )


def _keep_blocker(plan: dict, source: str, choices: dict[str, str]) -> str | None:
    """Say why keeping this file's planned name cannot work, or None if it can.

    Only one file can have a name, and nothing is ever overwritten, so keeping
    works only if the name is free once every other file wanting it steps
    aside. That is tried against the answers given so far, looking only at
    the destination folders; nothing is moved or written.
    """
    entries = [dict(entry, choice=choices.get(entry["source"])) for entry in plan["entries"]]
    target = next(entry for entry in entries if entry["source"] == source)
    name = (target["renamed_from"] or target["destination"]).casefold()
    for entry in entries:
        wanted = (entry["renamed_from"] or entry["destination"]).casefold()
        if entry is target or wanted != name or entry["choice"] in ("skip", "rename"):
            continue
        if entry["choice"] == "keep":
            return f"{entry['source']} already keeps this name"
        entry["choice"] = "skip"  # Suppose the others step aside, as they must.
    target["choice"] = "keep"
    _apply_choices(entries, Path(plan["root"]), _DirectorySnapshot())
    return target["reason"] if target["status"] == "conflict" else None


def resolve_conflicts(
    root: Path,
    recursive: bool = False,
    include_hidden: bool = False,
    rules: dict | None = None,
    check_duplicates: bool = False,
    reader: TextIO | None = None,
    writer: TextIO | None = None,
) -> tuple[dict, bool]:
    """Settle naming conflicts by asking in the terminal; change nothing.

    Returns the plan and whether the questions were seen through to the end.
    Each answer is keep, skip, or rename, exactly as a reviewed plan can carry
    them, so this is the same mechanism without the JSON.

    A keep is checked as soon as it is given. If the name is taken on disk,
    already kept by another file, or blocked some other way, the reason is
    shown and the same file is asked again with only skip and rename. The plan
    is then built again from the folder after every round, so each answer is
    judged against what is on disk then; an answer that still did not settle
    its file is ruled out and the file is asked about again. Skip always
    settles a file, so the questions always come to an end.
    """
    reader = sys.stdin if reader is None else reader
    writer = sys.stderr if writer is None else writer
    choices: dict[str, str] = {}
    ruled_out: dict[str, dict[str, str]] = {}
    plan = build_plan(root, recursive, include_hidden, rules, check_duplicates, choices)
    while True:
        conflicts = [entry for entry in plan["entries"] if entry["status"] == "conflict"]
        if not conflicts:
            return plan, True
        total = len(conflicts)
        _write(
            writer,
            f"{total} naming conflict{'' if total == 1 else 's'} to settle in "
            f"{_quote(plan['root'])}.\n",
        )
        for number, entry in enumerate(conflicts, start=1):
            source = entry["source"]
            barred = ruled_out.setdefault(source, {})
            _show_conflict(entry, plan, number, total, barred, writer)
            while True:
                try:
                    answer = _ask_choice(source, barred, reader, writer)
                except _NoAnswer:
                    _write(writer, "\nNo answer was given, so nothing was settled.\n")
                    return plan, False
                if answer == "quit":
                    _write(writer, "Stopped. Nothing was changed.\n")
                    return plan, False
                if answer == "keep":
                    blocker = _keep_blocker(plan, source, choices)
                    if blocker:
                        barred["keep"] = blocker
                        _write(
                            writer,
                            f"  Keep cannot work: {blocker}. Nothing is ever "
                            "overwritten, so choose skip or rename instead.\n",
                        )
                        continue
                choices[source] = answer
                break
        plan = build_plan(
            root, recursive, include_hidden, rules, check_duplicates, choices
        )
        for entry in plan["entries"]:
            source = entry["source"]
            answer = choices.get(source)
            if entry["status"] == "conflict" and answer in ("keep", "rename"):
                ruled_out.setdefault(source, {})[answer] = entry["reason"]
                _write(
                    writer,
                    f"\n{answer.capitalize()} did not settle "
                    f"{_quote(source)}: {entry['reason']}. "
                    "It will be asked about again.\n",
                )


def _confirm_plan(plan: dict, reader: TextIO, writer: TextIO) -> bool:
    """Show what the answers settled, then ask once before anything moves."""
    settled = [entry for entry in plan["entries"] if entry["choice"]]
    lines = [""]
    if settled:
        lines.append(f"Settled {len(settled)} of them:")
        for entry in settled:
            # JSON quoting keeps newlines and other filename controls on one line.
            source = _quote(entry["source"])
            destination = _quote(entry["destination"])
            if entry["choice"] == "skip":
                lines.append(f"  skip    {source} (left where it is)")
            else:
                lines.append(f"  {entry['choice']:<7} {source} -> {destination}")
    summary = plan["summary"]
    lines.append(
        f"{summary['ready']} file{'' if summary['ready'] == 1 else 's'} to move"
        + (f", {summary['skipped']} to leave alone" if summary["skipped"] else "")
        + "."
    )
    _write(writer, "\n".join(lines) + "\n")
    try:
        return _ask("Move them now? [y/N]: ", _YES_NO, reader, writer, empty="no") == "yes"
    except _NoAnswer:
        _write(writer, "\nNo answer was given, so nothing was moved.\n")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview files grouped by extension. Does not change any files."
    )
    parser.add_argument("root", type=Path, help="directory to inspect")
    parser.add_argument(
        "--recursive", action="store_true", help="include files in subdirectories"
    )
    parser.add_argument(
        "--include-hidden", action="store_true", help="include hidden files and folders"
    )
    parser.add_argument(
        "--rules",
        type=Path,
        metavar="RULES",
        help="JSON file of custom folders by extension and filename pattern",
    )
    parser.add_argument(
        "--check-duplicates",
        action="store_true",
        help="compare the contents of files that would share a destination",
    )
    parser.add_argument("--format", choices=("plain", "json"), default="plain")
    parser.add_argument(
        "--strict", action="store_true", help="exit with status 1 if any conflicts are found"
    )
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="PLAN",
        help="check a saved JSON plan against the folder; still changes nothing",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move the files in a verified plan; without it nothing is moved",
    )
    parser.add_argument(
        "--undo",
        type=Path,
        metavar="RECORD",
        help="put the files in a run record back where they were",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="settle each naming conflict in the terminal: keep, skip, or rename",
    )
    parser.add_argument(
        "--show",
        nargs="?",
        const="",
        metavar="RECORD",
        help="say what a run record did and where its files are now, which is "
        "how to see what an interrupted run left behind; with no RECORD, the "
        "newest record in the folder. Changes nothing",
    )
    args = parser.parse_args(argv)
    _speak_utf8(sys.stdout)
    _speak_utf8(sys.stderr)
    try:
        if args.rules is not None and (args.verify is not None or args.undo is not None):
            raise FilePlanError(
                "--rules shapes a new preview; a saved plan or record already "
                "carries its own rules, so remove --rules"
            )
        if args.check_duplicates and (args.verify is not None or args.undo is not None):
            raise FilePlanError(
                "--check-duplicates shapes a new preview; a saved plan already records "
                "whether contents were compared, so remove --check-duplicates"
            )
        if args.show is not None:
            for given, name in (
                (args.apply, "--apply"),
                (args.verify is not None, "--verify"),
                (args.undo is not None, "--undo"),
                (args.resolve, "--resolve"),
                (args.rules is not None, "--rules"),
                (args.check_duplicates, "--check-duplicates"),
                (args.recursive, "--recursive"),
                (args.include_hidden, "--include-hidden"),
            ):
                if given:
                    raise FilePlanError(
                        f"--show reads a run record by itself; remove {name}"
                    )
            record_path = Path(args.show) if args.show else latest_record(args.root)
            report = inspect_run(load_record(record_path), args.root, record_path)
            problems = report["summary"]["unclear"] or not report["summary"][
                "record_complete"
            ]
        elif args.resolve and (args.verify is not None or args.undo is not None):
            raise FilePlanError(
                "--resolve settles the conflicts in a new preview; a saved plan "
                "or record already carries its own choices, so remove --resolve"
            )
        elif args.resolve:
            rules = load_rules(args.rules) if args.rules is not None else None
            try:
                plan, settled = resolve_conflicts(
                    args.root,
                    args.recursive,
                    args.include_hidden,
                    rules,
                    args.check_duplicates,
                    sys.stdin,
                    sys.stderr,
                )
            except KeyboardInterrupt:
                _write(sys.stderr, "\nStopped. Nothing was changed.\n")
                return 1
            if not settled:
                return 1
            if not args.apply:
                report = plan
                problems = report["summary"]["conflicts"]
            else:
                left = plan["summary"]["conflicts"]
                if left:
                    _write(
                        sys.stderr,
                        f"{left} conflict{'' if left == 1 else 's'} "
                        f"{'is' if left == 1 else 'are'} still unsettled, so the "
                        "plan will be refused and nothing moved.\n",
                    )
                elif plan["summary"]["ready"]:
                    try:
                        moving = _confirm_plan(plan, sys.stdin, sys.stderr)
                    except KeyboardInterrupt:
                        _write(sys.stderr, "\nStopped. Nothing was moved.\n")
                        return 1
                    if not moving:
                        _write(sys.stderr, "Nothing was moved.\n")
                        return 1
                else:
                    _write(sys.stderr, "There is nothing to move.\n")
                report = apply_plan(plan, args.root, None)
        elif args.undo is not None:
            if args.apply or args.verify is not None:
                raise FilePlanError(
                    "--undo works on a run record by itself; remove --apply and --verify"
                )
            if args.recursive or args.include_hidden:
                raise FilePlanError(
                    "--undo uses the record as written; remove --recursive and "
                    "--include-hidden"
                )
            report = undo_record(load_record(args.undo), args.root, args.undo)
        elif args.verify is None:
            if args.apply:
                raise FilePlanError(
                    "--apply needs a reviewed plan: add --verify PLAN, or "
                    "--resolve to settle the conflicts in the terminal first"
                )
            rules = load_rules(args.rules) if args.rules is not None else None
            report = build_plan(
                args.root, args.recursive, args.include_hidden, rules, args.check_duplicates
            )
            problems = report["summary"]["conflicts"]
        else:
            if args.recursive or args.include_hidden:
                raise FilePlanError(
                    "--verify reuses the scan options saved in the plan; "
                    "remove --recursive and --include-hidden"
                )
            plan = load_plan(args.verify)
            if args.apply:
                report = apply_plan(plan, args.root, args.verify)
            else:
                report = verify_plan(plan, args.root, args.verify)
                problems = report["summary"]["problems"]
    except (OSError, ValueError) as error:
        _write(sys.stderr, f"fileplan: {error}\n")
        return 2
    if args.format == "json":
        output = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    elif args.show is not None:
        output = _plain_run_output(report)
    elif args.undo is not None:
        output = _plain_undo_output(report)
    elif args.apply:
        output = _plain_application_output(report)
    elif args.verify is None:
        output = _plain_output(report)
    else:
        output = _plain_verification_output(report)
    _write(sys.stdout, output)
    if args.apply or args.undo is not None:
        return 0 if report["summary"]["completed"] else 1
    return 1 if args.strict and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
