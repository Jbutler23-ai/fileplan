"""Preview how files would be grouped by extension without changing them."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import TextIO


DESTINATION_NAME = "_organized"
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


def build_plan(
    root: Path, recursive: bool = False, include_hidden: bool = False
) -> dict:
    """Return a metadata-only plan; raise FilePlanError if inspection fails.

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
                extension = path.suffix[1:].lower() or "no-extension"
                destination = Path(DESTINATION_NAME) / extension / name
                entries.append(
                    {
                        "source": relative,
                        "destination": destination.as_posix(),
                        "size_bytes": metadata.st_size,
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
    destinations: dict[str, list[dict]] = {}
    for entry in entries:
        destinations.setdefault(entry["destination"].casefold(), []).append(entry)
        parts = tuple(Path(entry["destination"]).parts)
        reason = _windows_bucket_conflict(parts[1])
        if reason is None:
            reason = snapshot.destination_conflict(root, parts)
        if reason:
            entry["status"] = "conflict"
            entry["reason"] = reason
    for group in destinations.values():
        if len(group) > 1:
            for entry in group:
                entry["status"] = "conflict"
                duplicate_reason = "Multiple source files share this destination"
                entry["reason"] = (
                    f"{entry['reason']}; {duplicate_reason}"
                    if entry["reason"]
                    else duplicate_reason
                )

    conflicts = sum(entry["status"] == "conflict" for entry in entries)
    return {
        "schema_version": 1,
        "root": str(root),
        "destination_root": str(root / DESTINATION_NAME),
        "entries": entries,
        "summary": {
            "files": len(entries),
            "ready": len(entries) - conflicts,
            "conflicts": conflicts,
            "total_bytes": sum(entry["size_bytes"] for entry in entries),
        },
        "skipped": skipped,
    }


def _plain_output(plan: dict) -> str:
    summary = plan["summary"]
    lines = [
        f"Preview for {json.dumps(plan['root'], ensure_ascii=False)}",
        f"{summary['files']} files: {summary['ready']} ready, "
        f"{summary['conflicts']} conflicts, {summary['total_bytes']} bytes",
        f"Destination: {json.dumps(plan['destination_root'], ensure_ascii=False)}",
    ]
    for entry in plan["entries"]:
        # JSON quoting keeps newlines and other filename controls on one line.
        source = json.dumps(entry["source"], ensure_ascii=False)
        destination = json.dumps(entry["destination"], ensure_ascii=False)
        line = f"[{entry['status']}] {source} -> {destination}"
        if entry["reason"]:
            line += f" ({json.dumps(entry['reason'], ensure_ascii=False)})"
        lines.append(line)
    if plan["skipped"]:
        lines.append(f"Skipped {len(plan['skipped'])} entries:")
        for entry in plan["skipped"]:
            path = json.dumps(entry["path"], ensure_ascii=False)
            lines.append(f"  {path}: {entry['reason']}")
    lines.append("Preview only. No files were changed.")
    return "\n".join(lines) + "\n"


def _write(stream: TextIO, text: str) -> None:
    encoding = getattr(stream, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(text)


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
    parser.add_argument("--format", choices=("plain", "json"), default="plain")
    parser.add_argument(
        "--strict", action="store_true", help="exit with status 1 if any conflicts are found"
    )
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.root, args.recursive, args.include_hidden)
    except (OSError, ValueError) as error:
        _write(sys.stderr, f"fileplan: {error}\n")
        return 2
    output = (
        json.dumps(plan, indent=2, ensure_ascii=True) + "\n"
        if args.format == "json"
        else _plain_output(plan)
    )
    _write(sys.stdout, output)
    return 1 if args.strict and plan["summary"]["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
