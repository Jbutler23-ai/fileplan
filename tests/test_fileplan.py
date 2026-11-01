import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import fileplan


class FileplanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, content=b"sample\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fileplan.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def entries_by_source(self, plan):
        return {entry["source"]: entry for entry in plan["entries"]}

    def test_basic_plan_groups_by_last_extension(self):
        self.write("report.PDF", b"1234")
        self.write("backup.tar.gz", b"123")
        self.write("LICENSE", b"12")
        self.write("notes.txt", b"1")

        plan = fileplan.build_plan(self.root)

        self.assertEqual(plan["schema_version"], fileplan.PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["root"], str(self.root.resolve()))
        self.assertEqual(plan["destination_root"], str(self.root.resolve() / "_organized"))
        entries = self.entries_by_source(plan)
        self.assertEqual(
            {source: entry["destination"] for source, entry in entries.items()},
            {
                "report.PDF": "_organized/pdf/report.PDF",
                "backup.tar.gz": "_organized/gz/backup.tar.gz",
                "LICENSE": "_organized/no-extension/LICENSE",
                "notes.txt": "_organized/txt/notes.txt",
            },
        )
        self.assertEqual(plan["summary"], {"files": 4, "ready": 4, "conflicts": 0, "skipped": 0, "total_bytes": 10})
        for entry in entries.values():
            self.assertEqual(entry["status"], "ready")
            self.assertIsNone(entry["reason"])
            self.assertIsInstance(entry["size_bytes"], int)

    def test_preview_does_not_read_contents_or_change_files(self):
        self.write("photo.jpg", b"\x00\xff\x01binary\x00")
        self.write("subdir/notes.txt", b"keep these notes")
        self.write("_organized/txt/existing.txt", b"already organized")
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        before_dirs = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_dir()}

        with mock.patch("builtins.open", side_effect=AssertionError("file contents were opened")), mock.patch.object(
            Path, "open", side_effect=AssertionError("file contents were opened")
        ):
            plan = fileplan.build_plan(self.root, recursive=True)

        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        after_dirs = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_dir()}
        self.assertEqual(after, before)
        self.assertEqual(after_dirs, before_dirs)
        self.assertEqual(plan["summary"]["files"], 2)

    def test_recursive_is_opt_in(self):
        self.write("top.txt")
        self.write("nested/inside.csv")
        shallow = fileplan.build_plan(self.root)
        recursive = fileplan.build_plan(self.root, recursive=True)
        self.assertEqual(set(self.entries_by_source(shallow)), {"top.txt"})
        self.assertEqual(set(self.entries_by_source(recursive)), {"top.txt", "nested/inside.csv"})

    def test_hidden_files_are_opt_in_and_metadata_stays_excluded(self):
        self.write("visible.txt")
        self.write(".env")
        self.write(".drafts/note.txt")
        for directory in (".git", "node_modules", "__pycache__", ".venv", "_organized"):
            self.write(f"{directory}/ignored.txt")

        normal = fileplan.build_plan(self.root, recursive=True)
        hidden = fileplan.build_plan(self.root, recursive=True, include_hidden=True)
        self.assertEqual(set(self.entries_by_source(normal)), {"visible.txt"})
        self.assertEqual(set(self.entries_by_source(hidden)), {"visible.txt", ".env", ".drafts/note.txt"})
        skipped = {entry["path"] for entry in hidden["skipped"]}
        self.assertTrue({".git", "node_modules", "__pycache__", ".venv", "_organized"}.issubset(skipped))
        self.assertTrue(all(entry["reason"] for entry in hidden["skipped"]))

    def test_nested_duplicate_names_are_both_conflicts(self):
        self.write("one/report.txt", b"first")
        self.write("two/report.txt", b"second")
        self.write("three/other.txt", b"other")
        plan = fileplan.build_plan(self.root, recursive=True)
        entries = self.entries_by_source(plan)
        for source in ("one/report.txt", "two/report.txt"):
            self.assertEqual(entries[source]["status"], "conflict")
            self.assertTrue(entries[source]["reason"])
        self.assertEqual(entries["three/other.txt"]["status"], "ready")
        self.assertEqual(plan["summary"], {"files": 3, "ready": 1, "conflicts": 2, "skipped": 0, "total_bytes": 16})

    def test_case_variants_cannot_share_a_destination(self):
        self.write("one/Report.TXT")
        self.write("two/report.txt")
        plan = fileplan.build_plan(self.root, recursive=True)
        self.assertEqual(plan["summary"]["conflicts"], 2)
        self.assertTrue(all(entry["status"] == "conflict" for entry in plan["entries"]))

    def test_existing_target_is_a_conflict_even_when_contents_match(self):
        self.write("report.txt", b"same")
        self.write("_organized/txt/report.txt", b"same")
        entry = fileplan.build_plan(self.root)["entries"][0]
        self.assertEqual(entry["status"], "conflict")
        self.assertTrue(entry["reason"])

    def test_existing_case_variant_target_is_a_conflict(self):
        self.write("Report.txt")
        self.write("_organized/TXT/report.TXT")
        plan = fileplan.build_plan(self.root)
        self.assertEqual(plan["summary"]["conflicts"], 1)

    def test_file_blocking_destination_directory_is_a_conflict(self):
        self.write("report.txt")
        self.write("_organized/txt", b"this is a file")
        plan = fileplan.build_plan(self.root)
        self.assertEqual(plan["summary"]["conflicts"], 1)
        self.assertTrue(plan["entries"][0]["reason"])

    def test_existing_directory_at_target_is_a_conflict(self):
        self.write("report.txt")
        (self.root / "_organized/txt/report.txt").mkdir(parents=True)
        plan = fileplan.build_plan(self.root)
        self.assertEqual(plan["summary"]["conflicts"], 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows reserves device names")
    def test_windows_reserved_extension_folder_is_a_conflict(self):
        self.write("report.CON")
        plan = fileplan.build_plan(self.root)
        self.assertEqual(plan["summary"]["conflicts"], 1)
        self.assertTrue(plan["entries"][0]["reason"])

    def test_empty_directory_has_valid_empty_plan(self):
        plan = fileplan.build_plan(self.root)
        self.assertEqual(plan["entries"], [])
        self.assertEqual(plan["summary"], {"files": 0, "ready": 0, "conflicts": 0, "skipped": 0, "total_bytes": 0})
        self.assertFalse((self.root / "_organized").exists())

    def test_repeated_runs_are_deterministic(self):
        for filename in ("z.txt", "b/image.png", "a.txt", "A/other.csv"):
            self.write(filename)
        first = fileplan.build_plan(self.root, recursive=True)
        second = fileplan.build_plan(self.root, recursive=True)
        self.assertEqual(first, second)
        sources = [entry["source"] for entry in first["entries"]]
        self.assertEqual(sources, sorted(sources, key=lambda name: (name.casefold(), name)))

    def test_json_cli_is_one_json_document_and_honors_flags(self):
        self.write("top.txt")
        self.write("nested/.hidden.csv")
        code, stdout, stderr = self.run_cli(str(self.root), "--recursive", "--include-hidden", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        plan = json.loads(stdout)
        self.assertEqual(plan["summary"]["files"], 2)
        self.assertEqual(set(self.entries_by_source(plan)), {"top.txt", "nested/.hidden.csv"})

    def test_strict_status_only_fails_when_conflicts_exist(self):
        self.write("report.txt")
        clean, _, clean_error = self.run_cli(str(self.root), "--strict")
        self.assertEqual((clean, clean_error), (0, ""))
        self.write("_organized/txt/report.txt")
        ordinary, _, ordinary_error = self.run_cli(str(self.root))
        strict, stdout, strict_error = self.run_cli(str(self.root), "--strict", "--format", "json")
        self.assertEqual((ordinary, ordinary_error), (0, ""))
        self.assertEqual((strict, strict_error), (1, ""))
        self.assertEqual(json.loads(stdout)["summary"]["conflicts"], 1)

    def test_invalid_root_reports_error_without_stdout(self):
        file_root = self.write("file.txt")
        for root in (self.root / "missing", file_root):
            with self.subTest(root=root):
                code, stdout, stderr = self.run_cli(str(root), "--format", "json")
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.strip())
                self.assertNotIn("Traceback", stderr)

    def test_unreadable_directory_does_not_return_partial_success(self):
        self.write("report.txt")
        with mock.patch.object(fileplan.os, "scandir", side_effect=PermissionError("access denied")):
            code, stdout, stderr = self.run_cli(str(self.root), "--format", "json")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("access denied", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_non_ascii_paths_work_with_legacy_console_encoding(self):
        self.write("notes-\u65e5\u672c\u8a9e.txt")
        for output_format in ("plain", "json"):
            with self.subTest(output_format=output_format):
                buffer = io.BytesIO()
                output = io.TextIOWrapper(buffer, encoding="cp1252")
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = fileplan.main([str(self.root), "--format", output_format])
                output.flush()
                text = buffer.getvalue().decode("cp1252")
                output.detach()
                self.assertEqual(code, 0)
                self.assertEqual(errors.getvalue(), "")
                self.assertTrue(text)
                if output_format == "json":
                    self.assertEqual(json.loads(text)["entries"][0]["source"], "notes-\u65e5\u672c\u8a9e.txt")

    def test_real_source_symlink_is_skipped(self):
        target = self.write("real.txt")
        link = self.root / "linked.txt"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        plan = fileplan.build_plan(self.root, recursive=True)
        self.assertEqual(set(self.entries_by_source(plan)), {"real.txt"})
        self.assertIn("linked.txt", {entry["path"] for entry in plan["skipped"]})

    def test_source_links_and_windows_junctions_are_not_traversed(self):
        regular = self.write("report.txt")
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            with self.subTest(mode=mode, attributes=attributes):
                link = SimpleNamespace(
                    name="linked",
                    stat=mock.Mock(return_value=SimpleNamespace(st_mode=mode, st_size=0, st_file_attributes=attributes)),
                )
                entry = SimpleNamespace(name=regular.name, stat=mock.Mock(return_value=regular.stat()))
                with mock.patch.object(fileplan.os, "scandir") as scandir:
                    scandir.return_value.__enter__.return_value = [link, entry]
                    plan = fileplan.build_plan(self.root, recursive=True, include_hidden=True)
                self.assertEqual(set(self.entries_by_source(plan)), {"report.txt"})
                self.assertIn("linked", {item["path"] for item in plan["skipped"]})
                scandir.assert_called_once_with(self.root.resolve())
                link.stat.assert_called_once_with(follow_symlinks=False)

    def test_destination_link_or_junction_blocks_the_plan(self):
        regular = self.write("report.txt")
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            with self.subTest(mode=mode, attributes=attributes):
                link = SimpleNamespace(
                    name="_organized",
                    stat=mock.Mock(return_value=SimpleNamespace(st_mode=mode, st_size=0, st_file_attributes=attributes)),
                )
                entry = SimpleNamespace(name=regular.name, stat=mock.Mock(return_value=regular.stat()))
                with mock.patch.object(fileplan.os, "scandir") as scandir:
                    scandir.return_value.__enter__.return_value = [entry, link]
                    plan = fileplan.build_plan(self.root, recursive=True)
                self.assertEqual(plan["summary"]["conflicts"], 1)
                self.assertTrue(plan["entries"][0]["reason"])
                scandir.assert_called_once_with(self.root.resolve())

    def test_example_runs_through_the_command_line(self):
        script = Path(fileplan.__file__).resolve()
        example = script.parent / "examples" / "inbox"
        result = subprocess.run(
            [sys.executable, str(script), str(example), "--recursive", "--strict", "--format", "json"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        plan = json.loads(result.stdout)
        self.assertEqual(plan["summary"]["files"], 5)
        self.assertEqual(plan["summary"]["ready"], 3)
        self.assertEqual(plan["summary"]["conflicts"], 2)
        self.assertFalse((example / "_organized").exists())


class VerificationTests(unittest.TestCase):
    """Checks on --verify. Every sample file below is made up for the test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        plans = tempfile.TemporaryDirectory()
        self.addCleanup(plans.cleanup)
        # Saved plans live outside the scanned folder so they are not planned.
        self.plans = Path(plans.name)

    def write(self, name, content=b"pretend contents\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def save_plan(self, name="saved-plan.json", **options):
        plan = fileplan.build_plan(self.root, **options)
        path = self.plans / name
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def save_payload(self, payload, name="broken-plan.json"):
        path = self.plans / name
        path.write_text(payload, encoding="utf-8")
        return path

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fileplan.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def verify(self, plan_path):
        plan = fileplan.load_plan(plan_path)
        return fileplan.verify_plan(plan, self.root, plan_path)

    def entries_by_source(self, report):
        return {entry["source"]: entry for entry in report["entries"]}

    def test_plan_records_scan_options_and_modification_times(self):
        path = self.write("packing-list.txt", b"socks\n")

        plan = fileplan.build_plan(self.root, recursive=True)

        self.assertEqual(plan["schema_version"], fileplan.PLAN_SCHEMA_VERSION)
        self.assertEqual(
            plan["options"],
            {"recursive": True, "include_hidden": False, "rules": None, "check_duplicates": False},
        )
        self.assertEqual(plan["entries"][0]["modified_ns"], path.stat().st_mtime_ns)

    def test_verification_passes_when_nothing_has_changed(self):
        self.write("soup-recipe.md", b"# pretend recipe\n")
        self.write("moodboard.PNG", b"not really an image")
        plan_path = self.save_plan()

        report = self.verify(plan_path)

        self.assertEqual(report["kind"], "verification")
        self.assertEqual(report["plan"], str(plan_path))
        self.assertEqual(report["root"], str(self.root.resolve()))
        self.assertEqual(
            report["summary"],
            {
                "entries": 2,
                "unchanged": 2,
                "changed": 0,
                "mismatch": 0,
                "missing": 0,
                "conflicts": 0,
                "new": 0,
                "skipped": 0,
                "problems": 0,
            },
        )
        for entry in report["entries"]:
            self.assertEqual(entry["status"], "unchanged")
            self.assertIsNone(entry["reason"])
            self.assertIsNone(entry["destination_conflict"])

    def test_verification_flags_a_file_whose_size_changed(self):
        self.write("quarterly-notes.txt", b"short")
        plan_path = self.save_plan()
        self.write("quarterly-notes.txt", b"a much longer pretend draft")

        report = self.verify(plan_path)

        entry = self.entries_by_source(report)["quarterly-notes.txt"]
        self.assertEqual(entry["status"], "changed")
        self.assertIn("5", entry["reason"])
        self.assertIn("27", entry["reason"])
        self.assertEqual(report["summary"]["changed"], 1)
        self.assertEqual(report["summary"]["problems"], 1)

    def test_verification_flags_a_file_touched_without_a_size_change(self):
        path = self.write("timesheet.csv", b"monday,1\n")
        plan_path = self.save_plan()
        # A made-up timestamp, far enough from now to differ on any filesystem.
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

        entry = self.entries_by_source(self.verify(plan_path))["timesheet.csv"]

        self.assertEqual(entry["status"], "changed")
        self.assertEqual(entry["reason"], "Contents were modified after the plan was made")

    def test_verification_flags_a_missing_file(self):
        self.write("receipt.pdf", b"pretend receipt")
        self.write("itinerary.txt", b"pretend itinerary")
        plan_path = self.save_plan()
        (self.root / "receipt.pdf").unlink()

        report = self.verify(plan_path)

        entries = self.entries_by_source(report)
        self.assertEqual(entries["receipt.pdf"]["status"], "missing")
        self.assertEqual(entries["receipt.pdf"]["reason"], "Source file is missing")
        self.assertEqual(entries["itinerary.txt"]["status"], "unchanged")
        self.assertEqual(report["summary"]["missing"], 1)

    def test_verification_explains_a_source_replaced_by_a_link(self):
        self.write("agenda.txt", b"pretend agenda")
        target = self.write("elsewhere/real-agenda.txt", b"pretend agenda")
        plan_path = self.save_plan()
        replaced = self.root / "agenda.txt"
        replaced.unlink()
        try:
            replaced.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        entry = self.entries_by_source(self.verify(plan_path))["agenda.txt"]

        self.assertEqual(entry["status"], "missing")
        self.assertIn("link", entry["reason"].casefold())

    def test_verification_explains_a_source_under_a_skipped_folder(self):
        self.write("archive/old-letter.txt", b"pretend letter")
        self.write("elsewhere/decoy.txt", b"pretend decoy")
        plan_path = self.save_plan(recursive=True)
        (self.root / "archive/old-letter.txt").unlink()
        (self.root / "archive").rmdir()
        try:
            (self.root / "archive").symlink_to(self.root / "elsewhere")
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        entry = self.entries_by_source(self.verify(plan_path))["archive/old-letter.txt"]

        self.assertEqual(entry["status"], "missing")
        self.assertIn("link", entry["reason"].casefold())

    def test_verification_flags_a_file_that_appeared_after_the_plan(self):
        self.write("budget.csv", b"pretend budget")
        plan_path = self.save_plan()
        self.write("surprise-memo.md", b"pretend memo")

        report = self.verify(plan_path)

        entry = self.entries_by_source(report)["surprise-memo.md"]
        self.assertEqual(entry["status"], "new")
        self.assertEqual(entry["destination"], "_organized/md/surprise-memo.md")
        self.assertIsNone(entry["destination_conflict"])
        self.assertEqual(report["summary"]["new"], 1)
        self.assertEqual(report["summary"]["unchanged"], 1)
        self.assertEqual(report["summary"]["conflicts"], 0)

    def test_new_file_with_a_blocked_destination_is_counted_as_a_conflict(self):
        self.write("wishlist.txt", b"pretend wishlist")
        plan_path = self.save_plan()
        self.write("gift-ideas.txt", b"pretend gift ideas")
        self.write("_organized/txt/gift-ideas.txt", b"something got there first")

        report = self.verify(plan_path)

        entry = self.entries_by_source(report)["gift-ideas.txt"]
        self.assertEqual(entry["status"], "new")
        self.assertIn("File appeared after the plan was made", entry["reason"])
        self.assertIn("Destination already exists", entry["reason"])
        self.assertEqual(
            entry["destination_conflict"],
            "Destination already exists: _organized/txt/gift-ideas.txt",
        )
        self.assertEqual(report["summary"]["new"], 1)
        self.assertEqual(report["summary"]["conflicts"], 1)
        self.assertEqual(report["summary"]["problems"], 1)

    def test_new_file_colliding_with_a_planned_file_marks_both(self):
        self.write("grocery-list.txt", b"pretend groceries")
        plan_path = self.save_plan(recursive=True)
        self.write("drafts/grocery-list.txt", b"pretend second list")

        report = self.verify(plan_path)

        entries = self.entries_by_source(report)
        self.assertEqual(entries["grocery-list.txt"]["status"], "conflict")
        self.assertEqual(entries["drafts/grocery-list.txt"]["status"], "new")
        for source in ("grocery-list.txt", "drafts/grocery-list.txt"):
            self.assertEqual(
                entries[source]["destination_conflict"],
                "Multiple source files share this destination",
            )
        self.assertEqual(report["summary"]["conflicts"], 2)
        self.assertEqual(report["summary"]["new"], 1)
        self.assertEqual(report["summary"]["unchanged"], 0)
        self.assertEqual(report["summary"]["problems"], 2)

    def test_verification_flags_a_destination_that_now_exists(self):
        self.write("playlist.txt", b"pretend playlist")
        plan_path = self.save_plan()
        self.write("_organized/txt/playlist.txt", b"something got there first")

        report = self.verify(plan_path)

        entry = self.entries_by_source(report)["playlist.txt"]
        self.assertEqual(entry["status"], "conflict")
        self.assertIn("Destination already exists", entry["reason"])
        self.assertEqual(report["summary"]["conflicts"], 1)

    def test_verification_reports_a_changed_source_and_its_conflict_together(self):
        self.write("sketch.txt", b"tiny")
        plan_path = self.save_plan()
        self.write("sketch.txt", b"a longer pretend sketch")
        self.write("_organized/txt/sketch.txt", b"something got there first")

        report = self.verify(plan_path)

        entry = self.entries_by_source(report)["sketch.txt"]
        self.assertEqual(entry["status"], "changed")
        self.assertIn("Size changed", entry["reason"])
        self.assertIn("Destination already exists", entry["reason"])
        self.assertEqual(
            entry["destination_conflict"],
            "Destination already exists: _organized/txt/sketch.txt",
        )
        self.assertEqual(report["summary"]["changed"], 1)
        self.assertEqual(report["summary"]["conflicts"], 1)
        self.assertEqual(report["summary"]["problems"], 1)

    def test_verification_reuses_the_saved_scan_options(self):
        self.write("top-level.txt", b"pretend file")
        self.write("drafts/nested-idea.txt", b"pretend nested file")
        shallow = self.save_plan("shallow.json")
        deep = self.save_plan("deep.json", recursive=True)

        shallow_report = self.verify(shallow)
        deep_report = self.verify(deep)

        self.assertEqual(shallow_report["options"]["recursive"], False)
        self.assertEqual(set(self.entries_by_source(shallow_report)), {"top-level.txt"})
        self.assertEqual(shallow_report["summary"]["problems"], 0)
        self.assertEqual(
            set(self.entries_by_source(deep_report)),
            {"top-level.txt", "drafts/nested-idea.txt"},
        )

    def test_verification_changes_nothing_on_disk(self):
        self.write("ledger.csv", b"pretend ledger")
        self.write("drafts/plan-b.md", b"pretend draft")
        plan_path = self.save_plan(recursive=True)
        (self.root / "ledger.csv").write_bytes(b"a longer pretend ledger")
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        before_dirs = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_dir()
        }

        report = self.verify(plan_path)

        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        after_dirs = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_dir()
        }
        self.assertEqual(after, before)
        self.assertEqual(after_dirs, before_dirs)
        self.assertFalse((self.root / "_organized").exists())
        self.assertEqual(report["summary"]["changed"], 1)

    def test_verification_json_is_one_document_and_strict_reports_problems(self):
        self.write("flyer.txt", b"pretend flyer")
        plan_path = self.save_plan()

        clean, clean_out, clean_error = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--strict"
        )
        (self.root / "flyer.txt").unlink()
        ordinary, _, ordinary_error = self.run_cli(str(self.root), "--verify", str(plan_path))
        strict, stdout, strict_error = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--strict", "--format", "json"
        )

        self.assertEqual((clean, clean_error), (0, ""))
        self.assertIn("No files were changed", clean_out)
        self.assertEqual((ordinary, ordinary_error), (0, ""))
        self.assertEqual((strict, strict_error), (1, ""))
        report = json.loads(stdout)
        self.assertEqual(report["summary"]["missing"], 1)
        self.assertEqual(report["entries"][0]["status"], "missing")

    def test_verification_rejects_a_plan_made_for_another_folder(self):
        self.write("manual.txt", b"pretend manual")
        plan = fileplan.build_plan(self.root)
        plan["root"] = str(self.plans / "some-other-folder")
        plan_path = self.save_payload(json.dumps(plan), "foreign-plan.json")

        code, stdout, stderr = self.run_cli(str(self.root), "--verify", str(plan_path))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("some-other-folder", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_verification_rejects_a_damaged_plan_file(self):
        self.write("manual.txt", b"pretend manual")
        good = json.loads(self.save_plan().read_text(encoding="utf-8"))
        wrong_version = dict(good, schema_version=99)
        no_options = {key: value for key, value in good.items() if key != "options"}
        bad_option = dict(good, options={"recursive": "yes", "include_hidden": False})
        bad_size = dict(good, entries=[dict(good["entries"][0], size_bytes="twelve")])
        no_source = dict(good, entries=[{"destination": "_organized/txt/manual.txt"}])
        repeated = dict(good, entries=[good["entries"][0], good["entries"][0]])
        payloads = {
            "not json at all": "{not json",
            "a list": "[]",
            "wrong version": json.dumps(wrong_version),
            "missing options": json.dumps(no_options),
            "non-boolean option": json.dumps(bad_option),
            "non-numeric size": json.dumps(bad_size),
            "entry without a source": json.dumps(no_source),
            "repeated source": json.dumps(repeated),
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                path = self.save_payload(payload, f"damaged-{len(label)}-{label[:4]}.json")
                code, stdout, stderr = self.run_cli(
                    str(self.root), "--verify", str(path), "--format", "json"
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.strip())
                self.assertNotIn("Traceback", stderr)

    def test_verification_rejects_a_missing_plan_file(self):
        self.write("manual.txt", b"pretend manual")

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(self.plans / "never-saved.json")
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)

    def test_verification_rejects_scan_flags_that_would_contradict_the_plan(self):
        self.write("manual.txt", b"pretend manual")
        plan_path = self.save_plan()

        for flag in ("--recursive", "--include-hidden"):
            with self.subTest(flag=flag):
                code, stdout, stderr = self.run_cli(
                    str(self.root), "--verify", str(plan_path), flag
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("--verify", stderr)

    def test_preview_still_runs_unchanged_when_verify_is_not_used(self):
        self.write("handout.txt", b"pretend handout")

        code, stdout, stderr = self.run_cli(str(self.root), "--format", "json")

        self.assertEqual((code, stderr), (0, ""))
        plan = json.loads(stdout)
        self.assertEqual(plan["entries"][0]["destination"], "_organized/txt/handout.txt")
        self.assertNotIn("kind", plan)


class _ApplyFixture(unittest.TestCase):
    """Shared helpers for --apply checks. Every sample file is made up."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        plans = tempfile.TemporaryDirectory()
        self.addCleanup(plans.cleanup)
        # Saved plans live outside the scanned folder so they are not planned.
        self.plans = Path(plans.name)

    def write(self, name, content=b"pretend contents\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def save_plan(self, name="saved-plan.json", **options):
        plan = fileplan.build_plan(self.root, **options)
        path = self.plans / name
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def save_payload(self, payload, name="edited-plan.json"):
        path = self.plans / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fileplan.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def apply(self, plan_path):
        return fileplan.apply_plan(fileplan.load_plan(plan_path), self.root, plan_path)

    def make_outside_folder(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        return Path(outside.name)

    def link_directory(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

    def tree(self):
        record_folder = self.root / "_organized" / fileplan.JOURNAL_DIRECTORY
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and record_folder not in path.parents
        }


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and hard links"
)
class ApplyTests(_ApplyFixture):
    """Moves and mid-run stops; only where the one safe method exists."""

    def test_nothing_moves_without_the_apply_flag(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        before = self.tree()

        code, stdout, stderr = self.run_cli(str(self.root), "--verify", str(plan_path))

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("No files were changed", stdout)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "_organized").exists())

    def test_apply_moves_every_file_when_the_plan_still_matches(self):
        self.write("field-notes.txt", b"pretend notes")
        self.write("logo.PNG", b"not really an image")
        self.write("packing-list", b"pretend list")
        plan_path = self.save_plan()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Moved 3 of 3 files", stdout)
        self.assertEqual(
            self.tree(),
            {
                "_organized/txt/field-notes.txt": b"pretend notes",
                "_organized/png/logo.PNG": b"not really an image",
                "_organized/no-extension/packing-list": b"pretend list",
            },
        )

    def test_apply_reports_each_move_and_leaves_the_source_folder_empty(self):
        self.write("one-sample.txt", b"pretend one")
        self.write("two-sample.md", b"pretend two")
        plan_path = self.save_plan()

        report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertTrue(report["summary"]["applied"])
        self.assertIsNone(report["stopped"])
        self.assertEqual(
            {entry["source"] for entry in report["results"]},
            {"one-sample.txt", "two-sample.md"},
        )
        self.assertTrue(
            all(entry["outcome"] == "moved" for entry in report["results"])
        )
        self.assertEqual(
            report["summary"]["planned"], report["summary"]["moved"]
        )
        self.assertEqual(report["summary"]["moved_confirmed"], 2)
        self.assertFalse((self.root / "one-sample.txt").exists())
        self.assertFalse((self.root / "two-sample.md").exists())

    def test_apply_refuses_the_whole_plan_when_one_file_changed(self):
        self.write("steady.txt", b"pretend steady")
        self.write("edited.txt", b"short")
        plan_path = self.save_plan()
        self.write("edited.txt", b"a much longer pretend draft")
        before = self.tree()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Refused", stdout)
        self.assertIn("Nothing was moved", stdout)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "_organized").exists())

    def test_apply_refuses_the_whole_plan_when_one_file_is_missing(self):
        self.write("kept.txt", b"pretend kept")
        self.write("vanishing.txt", b"pretend vanishing")
        plan_path = self.save_plan()
        (self.root / "vanishing.txt").unlink()

        report = self.apply(plan_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertEqual(report["summary"]["moved"], 0)
        self.assertTrue(
            all(entry["outcome"] == "not-attempted" for entry in report["results"])
        )
        self.assertTrue((self.root / "kept.txt").exists())

    def test_apply_refuses_the_whole_plan_when_a_destination_conflicts(self):
        self.write("safe-one.txt", b"pretend one")
        self.write("blocked.txt", b"pretend blocked")
        plan_path = self.save_plan()
        self.write("_organized/txt/blocked.txt", b"something got there first")
        before = self.tree()

        report = self.apply(plan_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertEqual(self.tree(), before)
        self.assertTrue((self.root / "safe-one.txt").exists())

    def test_apply_refuses_the_whole_plan_when_a_new_file_appeared(self):
        self.write("planned.txt", b"pretend planned")
        plan_path = self.save_plan()
        self.write("unplanned.txt", b"pretend unplanned")
        before = self.tree()

        report = self.apply(plan_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertEqual(report["verification"]["summary"]["new"], 1)
        self.assertEqual(self.tree(), before)

    def after_destination_check(self, disturb):
        """Run disturb() once, right after the first free-destination check.

        That is the gap between checking and moving, so these tests show what
        happens when the folder changes in exactly that moment.
        """
        original = fileplan._destination_taken
        fired = []

        def hooked(directory, name):
            reason = original(directory, name)
            if reason is None and not fired:
                fired.append(name)
                disturb()
            return reason

        return mock.patch.object(fileplan, "_destination_taken", side_effect=hooked)

    def test_apply_refuses_to_overwrite_a_file_that_appears_after_the_check(self):
        self.write("first-sample.txt", b"pretend first")
        self.write("second-sample.txt", b"pretend second")
        plan_path = self.save_plan()
        intruder = self.root / "_organized/txt/first-sample.txt"

        def disturb():
            intruder.write_bytes(b"something got there first")

        with self.after_destination_check(disturb):
            report = self.apply(plan_path)

        self.assertFalse(report["summary"]["completed"])
        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["stopped"]["source"], "first-sample.txt")
        self.assertIn("Destination already exists", report["stopped"]["reason"])
        self.assertEqual(intruder.read_bytes(), b"something got there first")
        self.assertEqual((self.root / "first-sample.txt").read_bytes(), b"pretend first")
        self.assertTrue((self.root / "second-sample.txt").exists())

    def test_apply_stops_when_a_source_changes_after_the_check(self):
        self.write("first-sample.txt", b"pretend first")
        self.write("second-sample.txt", b"pretend second")
        plan_path = self.save_plan()

        def disturb():
            (self.root / "first-sample.txt").write_bytes(b"a longer pretend first file")

        with self.after_destination_check(disturb):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["stopped"]["source"], "first-sample.txt")
        self.assertIn("Size changed", report["stopped"]["reason"])
        self.assertEqual(
            (self.root / "first-sample.txt").read_bytes(), b"a longer pretend first file"
        )
        self.assertFalse((self.root / "_organized/txt/first-sample.txt").exists())

    def test_apply_stops_when_the_destination_folder_becomes_a_link_after_the_check(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        outside = self.make_outside_folder()
        folder = self.root / "_organized/txt"

        def disturb():
            folder.rmdir()
            self.link_directory(folder, outside)

        with self.after_destination_check(disturb):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertIn("is a link or reparse point", report["stopped"]["reason"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_apply_stops_when_the_destination_folder_is_renamed_and_replaced_by_a_link(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        outside = self.make_outside_folder()
        folder = self.root / "_organized/txt"
        moved_aside = self.root / "_organized/txt-moved"

        def disturb():
            folder.rename(moved_aside)
            self.link_directory(folder, outside)

        with self.after_destination_check(disturb):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertIn("is a link or reparse point", report["stopped"]["reason"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(moved_aside.iterdir()), [])
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")


    def test_apply_stops_where_the_rename_is_refused(self):
        self.write("first-sample.txt", b"pretend first")
        self.write("second-sample.txt", b"pretend second")
        plan_path = self.save_plan()
        refusal = OSError(errno.EPERM, "Operation not permitted")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=refusal):
            report = self.apply(plan_path)

        self.assertFalse(report["summary"]["completed"])
        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertEqual(report["stopped"]["source"], "first-sample.txt")
        self.assertIn("Operation not permitted", report["stopped"]["reason"])
        self.assertEqual((self.root / "first-sample.txt").read_bytes(), b"pretend first")
        self.assertEqual((self.root / "second-sample.txt").read_bytes(), b"pretend second")
        self.assertEqual(list((self.root / "_organized/txt").iterdir()), [])

    def test_apply_stops_where_the_filesystem_cannot_refuse_to_replace(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        refusal = OSError(errno.EINVAL, "Invalid argument")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=refusal):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertIn("cannot move a file while refusing to replace", report["stopped"]["reason"])
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_apply_stops_across_filesystems(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        refusal = OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=refusal):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertIn("Invalid cross-device link", report["stopped"]["reason"])
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_apply_uses_nothing_but_the_no_replace_rename(self):
        self.write("first-sample.txt", b"pretend first")
        self.write("drafts/second-sample.md", b"pretend second")
        plan_path = self.save_plan(recursive=True)
        forbidden = {name: AssertionError(f"{name} must not be used") for name in
                     ("rename", "replace", "link", "unlink", "remove", "rmdir", "symlink")}

        with contextlib.ExitStack() as stack:
            for name, error in forbidden.items():
                stack.enter_context(mock.patch.object(fileplan.os, name, side_effect=error))
            report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertEqual(
            (self.root / "_organized/txt/first-sample.txt").read_bytes(), b"pretend first"
        )
        self.assertEqual(
            (self.root / "_organized/md/second-sample.md").read_bytes(), b"pretend second"
        )
        self.assertFalse((self.root / "first-sample.txt").exists())

    def test_kernel_refuses_a_file_that_appears_in_the_final_instant(self):
        # The scan says the name is free, then the file appears before the
        # rename. Only the kernel can refuse at that point, and it does.
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        intruder = self.root / "_organized/txt/tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            intruder.write_bytes(b"something got there first")
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertIn("Destination already exists", report["stopped"]["reason"])
        self.assertEqual(intruder.read_bytes(), b"something got there first")
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_apply_reports_a_file_replaced_right_after_the_move(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        landed = self.root / "_organized/txt/tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            landed.unlink()
            landed.write_bytes(b"someone else's file")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("refers to a different file", report["stopped"]["reason"])
        self.assertIn("removed nothing", report["stopped"]["reason"])
        self.assertEqual(landed.read_bytes(), b"someone else's file")

    def test_apply_reports_a_source_swapped_for_a_link_as_it_moves(self):
        self.write("tidy-me.txt", b"pretend contents")
        decoy = self.write("elsewhere/decoy.txt", b"pretend decoy")
        plan_path = self.save_plan()
        source = self.root / "tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            source.unlink()
            try:
                source.symlink_to(decoy)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("refers to a different file", report["stopped"]["reason"])
        moved_link = self.root / "_organized/txt/tidy-me.txt"
        self.assertTrue(moved_link.is_symlink())
        self.assertEqual(decoy.read_bytes(), b"pretend decoy")

    def test_apply_stops_before_moving_when_the_folder_is_moved_outside(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        outside = self.make_outside_folder()
        folder = self.root / "_organized/txt"

        def disturb():
            folder.rename(outside / "txt")

        with self.after_destination_check(disturb):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertIn("missing", report["stopped"]["reason"])
        self.assertEqual(list((outside / "txt").iterdir()), [])
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_apply_reports_where_the_file_went_when_the_folder_moves_as_it_moves(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        outside = self.make_outside_folder()
        folder = self.root / "_organized/txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            folder.rename(outside / "txt")
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        reason = report["stopped"]["reason"]
        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("Destination folder is missing", reason)
        self.assertEqual((outside / "txt" / "tidy-me.txt").read_bytes(), b"pretend contents")
        self.assertFalse((self.root / "tidy-me.txt").exists())
        if Path("/proc/self/fd").exists():
            self.assertIn(str(outside / "txt" / "tidy-me.txt"), reason)

    def test_apply_reports_a_file_that_appears_under_the_old_name(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        source = self.root / "tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            source.write_bytes(b"someone else's replacement")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("appeared under the old name", report["stopped"]["reason"])
        self.assertEqual(source.read_bytes(), b"someone else's replacement")
        self.assertEqual(
            (self.root / "_organized/txt/tidy-me.txt").read_bytes(), b"pretend contents"
        )

    def test_apply_reports_a_file_removed_by_something_else_after_the_move(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        landed = self.root / "_organized/txt/tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            landed.unlink()

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        reason = report["stopped"]["reason"]
        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("removed by something else", reason)
        self.assertIn("fileplan removed nothing", reason)

    def test_apply_reports_a_file_that_grows_right_after_the_move(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        landed = self.root / "_organized/txt/tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            with landed.open("ab") as handle:
                handle.write(b" plus a late addition")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("Size changed", report["stopped"]["reason"])
        self.assertEqual(landed.read_bytes(), b"pretend contents plus a late addition")

    def test_apply_reports_a_file_touched_right_after_the_move(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        landed = self.root / "_organized/txt/tidy-me.txt"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            # A made-up timestamp, far from now, so it differs on any filesystem.
            os.utime(landed, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_unconfirmed"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "moved-unconfirmed")
        self.assertIn("Contents were modified", report["stopped"]["reason"])
        self.assertEqual(landed.read_bytes(), b"pretend contents")

    def test_apply_stops_and_reports_a_failed_move(self):
        self.write("stubborn.txt", b"pretend stubborn")
        plan_path = self.save_plan()

        with mock.patch.object(
            fileplan, "_rename_noreplace", side_effect=PermissionError("access denied")
        ):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("[not moved]", stdout)
        self.assertIn("access denied", stdout)
        self.assertIn("0 moved, 1 not moved", stdout)
        self.assertTrue((self.root / "stubborn.txt").exists())

    def test_report_names_what_moved_what_did_not_and_what_was_not_tried(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt", "d-fourth.txt"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            real_rename(*args)
            if len(calls) == 2:
                # The second file moves, then something disturbs it afterwards.
                landed = self.root / "_organized/txt/b-second.txt"
                with landed.open("ab") as handle:
                    handle.write(b" plus a late addition")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn('[moved] "a-first.txt"', stdout)
        self.assertIn('[moved, unconfirmed] "b-second.txt"', stdout)
        self.assertIn('[not attempted] "c-third.txt"', stdout)
        self.assertIn('[not attempted] "d-fourth.txt"', stdout)
        self.assertIn(
            "1 moved, 1 moved but unconfirmed, 0 not moved, 2 not attempted", stdout
        )
        self.assertIn("unconfirmed or unknown", stdout)
        # Both of the first two files really did leave their original names.
        self.assertFalse((self.root / "a-first.txt").exists())
        self.assertFalse((self.root / "b-second.txt").exists())
        self.assertTrue((self.root / "c-third.txt").exists())
        self.assertTrue((self.root / "d-fourth.txt").exists())

    def test_report_counts_every_outcome_in_the_json(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("something unrecognised went wrong")
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        outcomes = {e["source"]: e["outcome"] for e in report["results"]}
        self.assertEqual(
            outcomes,
            {
                "a-first.txt": "moved",
                "b-second.txt": "unknown",
                "c-third.txt": "not-attempted",
            },
        )
        self.assertEqual(report["summary"]["moved"], 1)
        self.assertEqual(report["summary"]["moved_confirmed"], 1)
        self.assertEqual(report["summary"]["unknown"], 1)
        self.assertEqual(report["summary"]["not_attempted"], 1)
        self.assertEqual(report["summary"]["not_moved"], 0)
        self.assertEqual(report["stopped"]["outcome"], "unknown")
        self.assertFalse(report["summary"]["completed"])

    def test_unknown_outcome_is_shown_and_flagged_in_plain_text(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()

        with mock.patch.object(
            fileplan, "_rename_noreplace", side_effect=RuntimeError("unrecognised")
        ):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn('[unknown] "tidy-me.txt"', stdout)
        self.assertIn("1 in an unknown state", stdout)
        self.assertIn("unconfirmed or unknown", stdout)

    def test_every_planned_file_appears_in_the_results(self):
        for name in ("one.txt", "two.md", "three.json"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()

        report = self.apply(plan_path)

        self.assertEqual(len(report["results"]), report["summary"]["planned"])
        self.assertEqual(
            {e["source"] for e in report["results"]}, {"one.txt", "two.md", "three.json"}
        )
        self.assertTrue(all(e["reason"] is None for e in report["results"]))


class ApplyRefusalTests(_ApplyFixture):
    """Refusals that must hold on every platform, moving nothing."""

    def test_apply_is_refused_outright_where_the_safe_move_is_unavailable(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        before = self.tree()

        with mock.patch.object(fileplan, "_SAFE_MOVE_SUPPORTED", False):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("not supported", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "_organized").exists())

    def test_apply_refuses_a_plan_that_points_outside_the_folder(self):
        self.write("ordinary.txt", b"pretend contents")
        good = json.loads(self.save_plan().read_text(encoding="utf-8"))
        escapes = {
            "parent destination": dict(
                good,
                entries=[dict(good["entries"][0], destination="../escaped.txt")],
            ),
            "absolute destination": dict(
                good,
                entries=[dict(good["entries"][0], destination="/tmp/escaped.txt")],
            ),
            "destination outside the folder": dict(
                good,
                entries=[dict(good["entries"][0], destination="elsewhere/escaped.txt")],
            ),
            "traversal inside destination": dict(
                good,
                entries=[
                    dict(good["entries"][0], destination="_organized/../../escaped.txt")
                ],
            ),
            "parent source": dict(
                good, entries=[dict(good["entries"][0], source="../outside.txt")]
            ),
            "absolute source": dict(
                good, entries=[dict(good["entries"][0], source="/etc/hostname")]
            ),
        }
        for label, payload in escapes.items():
            with self.subTest(payload=label):
                path = self.save_payload(payload, f"escape-{len(label)}-{label[:4]}.json")
                code, stdout, stderr = self.run_cli(
                    str(self.root), "--verify", str(path), "--apply"
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn("Traceback", stderr)
                self.assertTrue((self.root / "ordinary.txt").exists())

    def test_apply_needs_a_saved_plan(self):
        self.write("tidy-me.txt", b"pretend contents")
        before = self.tree()

        code, stdout, stderr = self.run_cli(str(self.root), "--apply")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--apply", stderr)
        self.assertEqual(self.tree(), before)

    def test_apply_is_rejected_with_scan_flags(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        before = self.tree()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply", "--recursive"
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(self.tree(), before)


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and the no-replace rename"
)
class RecordTests(_ApplyFixture):
    """The record written as files move. Every sample file is made up."""

    def records(self, report):
        path = Path(report["record"]["path"])
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def record_files(self):
        folder = self.root / "_organized" / ".fileplan"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_a_completed_run_records_start_each_step_and_end(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.md", b"pretend second")
        plan_path = self.save_plan()

        report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertTrue(report["record"]["complete"])
        self.assertIsNone(report["record"]["warning"])
        records = self.records(report)
        self.assertEqual(
            [r["record"] for r in records],
            ["start", "intent", "done", "intent", "done", "end"],
        )
        start = records[0]
        self.assertEqual(start["schema_version"], fileplan.JOURNAL_SCHEMA_VERSION)
        self.assertEqual(start["root"], str(self.root.resolve()))
        self.assertEqual(start["plan"], str(plan_path))
        self.assertEqual(start["planned"], 2)
        self.assertEqual(
            start["options"],
            {"recursive": False, "include_hidden": False, "rules": None, "check_duplicates": False},
        )
        self.assertEqual(records[1]["source"], "a-first.txt")
        self.assertEqual(records[1]["destination"], "_organized/txt/a-first.txt")
        self.assertEqual(records[1]["size_bytes"], len(b"pretend first"))
        self.assertIsInstance(records[1]["modified_ns"], int)
        self.assertEqual(records[2]["outcome"], "moved")
        self.assertIsNone(records[2]["reason"])
        end = records[-1]
        self.assertTrue(end["completed"])
        self.assertIsNone(end["stopped_at"])
        self.assertEqual(end["outcomes"], {"a-first.txt": "moved", "b-second.md": "moved"})
        for record in records:
            self.assertIn("time", record)

    def test_a_stopped_run_records_what_happened_to_each_file(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            real_rename(*args)
            if len(calls) == 2:
                landed = self.root / "_organized/txt/b-second.txt"
                with landed.open("ab") as handle:
                    handle.write(b" plus a late addition")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.apply(plan_path)

        records = self.records(report)
        self.assertEqual(
            [r["record"] for r in records],
            ["start", "intent", "done", "intent", "done", "end"],
        )
        self.assertEqual(records[2]["outcome"], "moved")
        self.assertEqual(records[4]["outcome"], "moved-unconfirmed")
        self.assertIn("Size changed", records[4]["reason"])
        end = records[-1]
        self.assertFalse(end["completed"])
        self.assertEqual(end["stopped_at"], "b-second.txt")
        self.assertEqual(end["outcomes"]["c-third.txt"], "not-attempted")
        # No intent was ever written for the file that was not reached.
        self.assertNotIn("c-third.txt", [r.get("source") for r in records[1:-1]])

    def test_a_run_cut_off_mid_move_leaves_an_intent_with_no_done(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            if len(calls) == 2:
                raise KeyboardInterrupt  # the process is killed mid-move
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan_path)

        files = self.record_files()
        self.assertEqual(len(files), 1)
        records = [json.loads(line) for line in files[0].read_text().splitlines()]
        self.assertEqual([r["record"] for r in records], ["start", "intent", "done", "intent"])
        self.assertEqual(records[-1]["source"], "b-second.txt")
        # The record alone says which file was in flight when the run was cut off.
        self.assertFalse((self.root / "a-first.txt").exists())
        self.assertTrue((self.root / "b-second.txt").exists())
        self.assertTrue((self.root / "c-third.txt").exists())

    def test_nothing_moves_when_the_record_cannot_be_started(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        # Something unrelated already occupies the record folder's name.
        self.write("_organized/.fileplan", b"not a folder")
        before = self.tree()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Stopped before any move", stdout)
        self.assertIn("record could not be started", stdout)
        self.assertIn("not a directory", stdout)
        self.assertIn('[not attempted] "tidy-me.txt"', stdout)
        self.assertEqual(self.tree(), before)

    def test_a_file_is_not_moved_when_its_intent_cannot_be_recorded(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        plan_path = self.save_plan()
        real_write = fileplan._Journal.write
        calls = []

        def write(self, record):
            calls.append(record["record"])
            if record["record"] == "intent" and record["source"] == "b-second.txt":
                raise OSError(errno.ENOSPC, "No space left on device")
            real_write(self, record)

        with mock.patch.object(fileplan._Journal, "write", write):
            report = self.apply(plan_path)

        outcomes = {e["source"]: e["outcome"] for e in report["results"]}
        self.assertEqual(outcomes, {"a-first.txt": "moved", "b-second.txt": "not-moved"})
        self.assertIn("No space left on device", report["stopped"]["reason"])
        self.assertTrue((self.root / "b-second.txt").exists())
        self.assertFalse(report["summary"]["completed"])

    def test_the_run_stops_and_says_so_when_a_done_record_fails(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        plan_path = self.save_plan()
        real_write = fileplan._Journal.write

        def write(self, record):
            if record["record"] == "done" and record["source"] == "a-first.txt":
                raise OSError(errno.EIO, "Input/output error")
            real_write(self, record)

        with mock.patch.object(fileplan._Journal, "write", write):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn('[moved] "a-first.txt"', stdout)
        self.assertIn("record could not be written afterwards", stdout)
        self.assertIn('[not attempted] "b-second.txt"', stdout)
        self.assertFalse((self.root / "a-first.txt").exists())
        self.assertTrue((self.root / "b-second.txt").exists())

    def test_a_failed_closing_record_is_reported_as_a_warning(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        real_write = fileplan._Journal.write

        def write(self, record):
            if record["record"] == "end":
                raise OSError(errno.EIO, "Input/output error")
            real_write(self, record)

        with mock.patch.object(fileplan._Journal, "write", write):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply"
            )

        # The file moved, but the record is unfinished, so the run is not clean.
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Record incomplete", stdout)
        self.assertIn("Input/output error", stdout)
        self.assertIn("cannot be relied on to undo", stdout)
        self.assertFalse((self.root / "tidy-me.txt").exists())

    def test_a_failed_entry_leaves_no_half_written_line(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        plan_path = self.save_plan()
        real_write = os.write
        state = {"short": False}

        def short_write(fd, data):
            # Write a few bytes of the second intent, then fail: the classic
            # out-of-space partial write.
            if not state["short"] and b'"intent"' in bytes(data) and b"b-second" in bytes(data):
                state["short"] = True
                real_write(fd, bytes(data)[:12])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(fileplan.os, "write", side_effect=short_write):
            report = self.apply(plan_path)

        self.assertTrue(state["short"])
        path = Path(report["record"]["path"])
        lines = path.read_text(encoding="utf-8").splitlines()
        # Every line is a whole JSON object; the partial one was rolled back.
        for line in lines:
            json.loads(line)
        self.assertEqual(
            [json.loads(line)["record"] for line in lines],
            ["start", "intent", "done", "end"],
        )
        self.assertFalse(report["record"]["damaged"])
        self.assertFalse(report["summary"]["completed"])
        self.assertEqual(report["summary"]["not_moved"], 1)
        self.assertTrue((self.root / "b-second.txt").exists())

    def test_a_damaged_record_is_never_reported_as_complete(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        real_write = os.write

        def short_write(fd, data):
            if b'"done"' in bytes(data):
                real_write(fd, bytes(data)[:7])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(
            fileplan.os, "write", side_effect=short_write
        ), mock.patch.object(
            fileplan.os, "ftruncate", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            report = self.apply(plan_path)

        self.assertTrue(report["record"]["damaged"])
        self.assertFalse(report["summary"]["record_complete"])
        self.assertFalse(report["summary"]["completed"])

    def test_a_damaged_record_is_not_marked_complete_anywhere_in_the_report(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        plan_path = self.save_plan()
        real_write = os.write

        def short_write(fd, data):
            # The first done entry is cut off partway and cannot be rolled back.
            if b'"done"' in bytes(data) and b"a-first" in bytes(data):
                real_write(fd, bytes(data)[:7])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(
            fileplan.os, "write", side_effect=short_write
        ), mock.patch.object(
            fileplan.os, "ftruncate", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            code, stdout, stderr = self.run_cli(
                str(self.root), "--verify", str(plan_path), "--apply", "--format", "json"
            )

        report = json.loads(stdout)
        self.assertEqual((code, stderr), (1, ""))
        self.assertTrue(report["record"]["damaged"])
        self.assertFalse(report["record"]["complete"])
        self.assertFalse(report["summary"]["record_complete"])
        self.assertFalse(report["summary"]["completed"])
        self.assertIn("half-written entry", report["record"]["warning"])

    def test_nothing_more_is_written_to_a_damaged_record(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        plan_path = self.save_plan()
        real_write = os.write

        def short_write(fd, data):
            if b'"done"' in bytes(data) and b"a-first" in bytes(data):
                real_write(fd, bytes(data)[:7])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(
            fileplan.os, "write", side_effect=short_write
        ), mock.patch.object(
            fileplan.os, "ftruncate", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            report = self.apply(plan_path)

        raw = Path(report["record"]["path"]).read_bytes()
        # The file ends exactly where the damage happened; the closing entry
        # was refused rather than glued onto the half-written line.
        self.assertTrue(raw.endswith(b'{"recor'))
        self.assertNotIn(b'"end"', raw)
        lines = raw.split(b"\n")
        for whole in lines[:-1]:
            json.loads(whole)
        self.assertEqual(
            [json.loads(line)["record"] for line in lines[:-1]], ["start", "intent"]
        )
        self.assertIn("closing record could not be written", report["record"]["warning"])

    def test_a_partial_line_that_cannot_be_rolled_back_is_reported(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        real_write = os.write

        def short_write(fd, data):
            if b'"intent"' in bytes(data):
                real_write(fd, bytes(data)[:9])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(
            fileplan.os, "write", side_effect=short_write
        ), mock.patch.object(
            fileplan.os, "ftruncate", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            report = self.apply(plan_path)

        self.assertTrue(report["record"]["damaged"])
        self.assertIn("half-written entry", report["record"]["warning"])
        self.assertFalse(report["summary"]["completed"])
        self.assertTrue((self.root / "tidy-me.txt").exists())

    def test_each_run_gets_its_own_record_and_never_reuses_a_file(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        folder = self.root / "_organized" / ".fileplan"
        folder.mkdir(parents=True)
        (folder / "run-fixed.jsonl").write_bytes(b"someone else's record\n")

        with mock.patch.object(fileplan, "_journal_name", return_value="run-fixed"):
            report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertEqual(Path(report["record"]["path"]).name, "run-fixed-1.jsonl")
        self.assertEqual((folder / "run-fixed.jsonl").read_bytes(), b"someone else's record\n")
        self.assertEqual([p.name for p in self.record_files()], ["run-fixed-1.jsonl", "run-fixed.jsonl"])

    def test_the_record_is_private_to_the_user(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()

        report = self.apply(plan_path)

        mode = stat.S_IMODE(Path(report["record"]["path"]).stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_no_record_is_written_without_apply_or_for_an_empty_plan(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        self.run_cli(str(self.root), "--verify", str(plan_path))
        self.run_cli(str(self.root))
        self.assertEqual(self.record_files(), [])
        self.assertFalse((self.root / "_organized").exists())

        (self.root / "tidy-me.txt").unlink()
        empty = self.save_plan("empty.json")
        report = self.apply(empty)
        self.assertTrue(report["summary"]["completed"])
        self.assertIsNone(report["record"]["path"])
        self.assertFalse((self.root / "_organized").exists())

    def test_a_refused_run_writes_no_record(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        self.write("tidy-me.txt", b"a much longer pretend draft")

        report = self.apply(plan_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertIsNone(report["record"]["path"])
        self.assertEqual(self.record_files(), [])

    def test_the_record_is_shown_in_both_outputs(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Record: ", stdout)
        self.assertIn(".fileplan", stdout)

    def test_later_previews_do_not_plan_the_record(self):
        self.write("tidy-me.txt", b"pretend contents")
        self.apply(self.save_plan())
        self.write("another.txt", b"pretend another")

        plan = fileplan.build_plan(self.root, recursive=True, include_hidden=True)

        self.assertEqual({e["source"] for e in plan["entries"]}, {"another.txt"})


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and the no-replace rename"
)
class UndoTests(_ApplyFixture):
    """Putting a run back. Every sample file below is made up for the test."""

    def apply_and_record(self, **options):
        plan_path = self.save_plan(**options)
        report = self.apply(plan_path)
        return report, Path(report["record"]["path"])

    def undo(self, record_path):
        return fileplan.undo_record(
            fileplan.load_record(record_path), self.root, record_path
        )

    def test_a_completed_run_is_put_back_exactly(self):
        samples = {
            "field-notes.txt": b"pretend notes",
            "drafts/early-idea.md": b"pretend early idea",
            "packing-list": b"pretend list",
        }
        for name, body in samples.items():
            self.write(name, body)
        before = self.tree()
        _, record_path = self.apply_and_record(recursive=True)
        self.assertNotEqual(self.tree(), before)

        report = self.undo(record_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertEqual(report["summary"]["restored"], 3)
        self.assertEqual(report["summary"]["blocked"], 0)
        self.assertEqual(self.tree(), before)

    def test_undo_runs_from_the_command_line_and_reports_each_file(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn('[restored] "_organized/txt/tidy-me.txt" -> "tidy-me.txt"', stdout)
        self.assertIn("Put back 1 of 1 files", stdout)
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_undo_recovers_a_run_that_was_cut_off_partway(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt"):
            self.write(name, b"pretend contents")
        before = self.tree()
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            if len(calls) == 2:
                raise KeyboardInterrupt  # the process is killed mid-run
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan_path)
        record_path = self.record_files()[0]

        report = self.undo(record_path)

        # The file that was in flight never moved, so it needs nothing.
        outcomes = {item["source"]: item["outcome"] for item in report["results"]}
        self.assertEqual(outcomes, {"a-first.txt": "restored", "b-second.txt": "skipped"})
        self.assertTrue(report["summary"]["completed"])
        self.assertEqual(self.tree(), before)

    def record_files(self):
        folder = self.root / "_organized" / fileplan.JOURNAL_DIRECTORY
        return sorted(p for p in folder.iterdir()) if folder.exists() else []

    def test_undo_refuses_everything_when_one_file_cannot_be_put_back(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        _, record_path = self.apply_and_record()
        # Someone has taken one of the original names in the meantime.
        self.write("b-second.txt", b"someone else's file")
        before = self.tree()

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Refused", stdout)
        self.assertIn("in both places", stdout)
        self.assertIn("Nothing was moved", stdout)
        self.assertEqual(self.tree(), before)

    def test_undo_refuses_a_stored_file_that_is_not_the_one_that_moved(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        stored = self.root / "_organized/txt/tidy-me.txt"
        stored.write_bytes(b"a different pretend file entirely")
        before = self.tree()

        report = self.undo(record_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertEqual(report["results"][0]["outcome"], "blocked")
        self.assertIn("is not the file that was moved", report["results"][0]["reason"])
        self.assertEqual(self.tree(), before)

    def test_undo_refuses_when_the_file_is_in_neither_place(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        (self.root / "_organized/txt/tidy-me.txt").unlink()

        report = self.undo(record_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertIn("in neither place", report["results"][0]["reason"])

    def test_undo_never_overwrites_a_file_that_appears_during_the_run(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        _, record_path = self.apply_and_record()
        real_rename = fileplan._rename_noreplace
        calls = []
        intruder = self.root / "a-first.txt"

        def rename(*args):
            calls.append(args)
            if len(calls) == 2:
                intruder.write_bytes(b"something got there first")
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.undo(record_path)

        self.assertFalse(report["summary"]["completed"])
        self.assertIn("Destination already exists", report["stopped"]["reason"])
        self.assertEqual(intruder.read_bytes(), b"something got there first")
        self.assertEqual(
            (self.root / "_organized/txt/a-first.txt").read_bytes(), b"pretend first"
        )

    def test_undo_puts_files_back_newest_first(self):
        for name in ("a-first.txt", "b-second.txt", "c-third.txt"):
            self.write(name, b"pretend contents")
        _, record_path = self.apply_and_record()

        report = self.undo(record_path)

        self.assertEqual(
            [item["source"] for item in report["results"]],
            ["c-third.txt", "b-second.txt", "a-first.txt"],
        )

    def test_undo_writes_its_own_record(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()

        report = self.undo(record_path)

        written = Path(report["record"]["path"])
        self.assertNotEqual(written, record_path)
        records = [json.loads(line) for line in written.read_text().splitlines()]
        self.assertEqual([r["record"] for r in records], ["start", "intent", "done", "end"])
        self.assertEqual(records[0]["kind"], "undo")
        self.assertEqual(records[0]["undoing"], str(record_path))
        self.assertEqual(records[1]["source"], "_organized/txt/tidy-me.txt")
        self.assertEqual(records[1]["destination"], "tidy-me.txt")
        self.assertTrue(records[-1]["completed"])

    def test_an_undo_can_itself_be_undone(self):
        self.write("tidy-me.txt", b"pretend contents")
        applied, record_path = self.apply_and_record()
        organised = self.tree()

        first = self.undo(record_path)
        second = self.undo(Path(first["record"]["path"]))

        self.assertTrue(second["summary"]["completed"])
        self.assertEqual(self.tree(), organised)

    def test_undo_rejects_a_record_for_another_folder(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        lines = record_path.read_text().splitlines()
        start = json.loads(lines[0])
        start["root"] = str(self.plans / "somewhere-else")
        record_path.write_text("\n".join([json.dumps(start)] + lines[1:]) + "\n")

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("somewhere-else", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_undo_rejects_a_damaged_record(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        good = record_path.read_text().splitlines()
        payloads = {
            "empty": "",
            "no start": "\n".join(good[1:]),
            "not json in the middle": "\n".join([good[0], "{oops", good[2]]),
            "wrong version": json.dumps(dict(json.loads(good[0]), schema_version=99)),
            "escaping source": "\n".join(
                [good[0], json.dumps(dict(json.loads(good[1]), source="../outside.txt"))]
            ),
            "escaping destination": "\n".join(
                [
                    good[0],
                    json.dumps(dict(json.loads(good[1]), destination="../../escaped.txt")),
                ]
            ),
            "destination outside organised": "\n".join(
                [
                    good[0],
                    json.dumps(dict(json.loads(good[1]), destination="elsewhere/x.txt")),
                ]
            ),
            "done with no intent": "\n".join(
                [good[0], json.dumps({"record": "done", "index": 7, "outcome": "moved"})]
            ),
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                broken = self.plans / f"broken-{len(label)}-{label[:4]}.jsonl"
                broken.write_text(payload, encoding="utf-8")
                code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(broken))
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.strip())
                self.assertNotIn("Traceback", stderr)

    def test_undo_warns_about_a_record_with_an_unfinished_last_line(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        _, record_path = self.apply_and_record()
        text = record_path.read_text()
        record_path.write_text(text + '{"record": "inte')

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("unfinished line", stdout)
        self.assertEqual(self.tree(), {"a-first.txt": b"pretend first",
                                       "b-second.txt": b"pretend second"})

    def test_undo_is_rejected_with_other_flags(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        _, record_path = self.apply_and_record()
        for extra in (("--apply",), ("--verify", str(plan_path)), ("--recursive",)):
            with self.subTest(flags=extra):
                code, stdout, stderr = self.run_cli(
                    str(self.root), "--undo", str(record_path), *extra
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("--undo", stderr)

    def test_a_file_is_not_called_restored_unless_it_is_at_its_original_name(self):
        # The move succeeds, but the path to the original name is swapped for
        # a link, so the file is not where the record says it should be.
        self.write("drafts/early-idea.md", b"pretend early idea")
        _, record_path = self.apply_and_record(recursive=True)
        outside = self.make_outside_folder()
        drafts = self.root / "drafts"
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            for leftover in drafts.iterdir():
                leftover.rename(outside / leftover.name)
            drafts.rmdir()
            self.link_directory(drafts, outside)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.undo(record_path)

        item = report["results"][0]
        self.assertEqual(item["outcome"], "restored-unconfirmed")
        self.assertIn("link or reparse point", item["reason"])
        self.assertFalse(report["summary"]["completed"])
        self.assertEqual(report["summary"]["restored_confirmed"], 0)

    def test_confirming_the_original_name_accepts_only_the_recorded_file(self):
        entry = {
            "source": "drafts/early-idea.md",
            "destination": "_organized/md/early-idea.md",
            "size_bytes": len(b"pretend early idea"),
            "modified_ns": 0,
        }
        target = self.write("drafts/early-idea.md", b"pretend early idea")
        entry["modified_ns"] = target.stat().st_mtime_ns

        self.assertIsNone(fileplan._confirm_at_original(self.root, entry))

        # A file of a different size is not the one that was moved.
        target.write_bytes(b"a much longer pretend early idea")
        self.assertIn("Size changed", fileplan._confirm_at_original(self.root, entry))

        # Nor is one written at a different moment.
        target.write_bytes(b"pretend early idea")
        os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        self.assertIn("modified", fileplan._confirm_at_original(self.root, entry))

        # Nothing there at all.
        target.unlink()
        self.assertIn("nothing is at", fileplan._confirm_at_original(self.root, entry))

        # A link standing in for the file is not the file.
        decoy = self.write("elsewhere/decoy.md", b"pretend early idea")
        try:
            target.symlink_to(decoy)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        self.assertIn("link", fileplan._confirm_at_original(self.root, entry).casefold())

    def test_confirming_the_original_name_rejects_a_path_through_a_link(self):
        entry = {
            "source": "drafts/early-idea.md",
            "destination": "_organized/md/early-idea.md",
            "size_bytes": len(b"pretend early idea"),
            "modified_ns": 0,
        }
        outside = self.make_outside_folder()
        (outside / "early-idea.md").write_bytes(b"pretend early idea")
        entry["modified_ns"] = (outside / "early-idea.md").stat().st_mtime_ns
        self.link_directory(self.root / "drafts", outside)

        problem = fileplan._confirm_at_original(self.root, entry)

        self.assertIsNotNone(problem)
        self.assertIn("link or reparse point", problem)

    def test_undo_refuses_when_the_original_name_holds_a_different_file(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        # The stored copy is gone and something else now sits at the old name.
        (self.root / "_organized/txt/tidy-me.txt").unlink()
        self.write("tidy-me.txt", b"a different pretend file entirely")
        before = self.tree()

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("[blocked]", stdout)
        self.assertIn("is not the file that was moved", stdout)
        self.assertIn("Nothing was moved", stdout)
        self.assertEqual(self.tree(), before)

    def test_an_entry_that_looked_already_put_back_is_checked_again(self):
        self.write("a-first.txt", b"pretend first")
        self.write("b-second.txt", b"pretend second")
        _, record_path = self.apply_and_record()
        # a-first is already back before the undo starts, so the gate marks it
        # as needing nothing. Undo works newest first, so b-second moves first.
        (self.root / "_organized/txt/a-first.txt").rename(self.root / "a-first.txt")
        real_rename = fileplan._rename_noreplace

        def rename(*args):
            real_rename(*args)
            (self.root / "a-first.txt").write_bytes(b"swapped out behind our back")

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            report = self.undo(record_path)

        outcomes = {item["source"]: item["outcome"] for item in report["results"]}
        self.assertEqual(outcomes["b-second.txt"], "restored")
        self.assertEqual(outcomes["a-first.txt"], "not-restored")
        self.assertIn("looked already put back", report["stopped"]["reason"])
        self.assertIn("Size changed", report["stopped"]["reason"])
        self.assertFalse(report["summary"]["completed"])

    def test_the_undo_report_says_whether_its_own_record_is_complete(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()

        good = self.undo(record_path)
        self.assertTrue(good["summary"]["record_complete"])

        self.write("tidy-me-again.txt", b"pretend contents")
        _, second_record = self.apply_and_record()
        real_write = fileplan._Journal.write

        def write(self, record):
            if record["record"] == "end":
                raise OSError(errno.EIO, "Input/output error")
            real_write(self, record)

        with mock.patch.object(fileplan._Journal, "write", write):
            bad = self.undo(second_record)

        self.assertFalse(bad["summary"]["record_complete"])
        self.assertFalse(bad["summary"]["completed"])

    def test_undo_says_a_record_without_a_closing_entry_is_incomplete(self):
        for name in ("a-first.txt", "b-second.txt"):
            self.write(name, b"pretend contents")
        plan_path = self.save_plan()
        real_rename = fileplan._rename_noreplace
        calls = []

        def rename(*args):
            calls.append(args)
            if len(calls) == 2:
                raise KeyboardInterrupt
            real_rename(*args)

        with mock.patch.object(fileplan, "_rename_noreplace", side_effect=rename):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan_path)
        cut_off = self.record_files()[0]

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(cut_off))

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("The record is incomplete", stdout)
        self.assertIn("no closing entry", stdout)

    def test_a_damaged_undo_record_is_not_marked_complete(self):
        self.write("tidy-me.txt", b"pretend contents")
        _, record_path = self.apply_and_record()
        real_write = os.write

        def short_write(fd, data):
            if b'"done"' in bytes(data):
                real_write(fd, bytes(data)[:7])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(
            fileplan.os, "write", side_effect=short_write
        ), mock.patch.object(
            fileplan.os, "ftruncate", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            report = self.undo(record_path)

        self.assertTrue(report["record"]["damaged"])
        self.assertFalse(report["record"]["complete"])
        self.assertFalse(report["summary"]["record_complete"])
        self.assertFalse(report["summary"]["completed"])
        # The file itself was put back; only the record of it is unreliable.
        self.assertEqual((self.root / "tidy-me.txt").read_bytes(), b"pretend contents")

    def test_undo_of_a_refused_run_has_nothing_to_do(self):
        self.write("tidy-me.txt", b"pretend contents")
        plan_path = self.save_plan()
        self.write("tidy-me.txt", b"a much longer pretend draft")
        report = self.apply(plan_path)
        self.assertIsNone(report["record"]["path"])
        self.assertEqual(self.record_files(), [])


class RulesTests(unittest.TestCase):
    """Custom folders by extension and pattern. Every sample is made up."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        aside = tempfile.TemporaryDirectory()
        self.addCleanup(aside.cleanup)
        self.aside = Path(aside.name)

    def write(self, name, content=b"pretend contents\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def rules_file(self, document, name="rules.json"):
        path = self.aside / name
        path.write_text(json.dumps(document) if not isinstance(document, str) else document)
        return path

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fileplan.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def destinations(self, plan):
        return {e["source"]: (e["destination"], e["rule"]) for e in plan["entries"]}

    SAMPLE = {
        "schema_version": 1,
        "rules": [
            {"name": "invoices", "extensions": ["pdf"], "patterns": ["invoice-*"], "folder": "finance/invoices"},
            {"name": "images", "extensions": [".JPG", "jpeg", "png"], "folder": "images"},
            {"name": "readmes", "patterns": ["readme*"], "folder": "notes"},
            {"name": "bare", "extensions": ["no-extension"], "folder": "loose"},
        ],
    }

    def test_rules_choose_folders_and_name_the_rule_that_chose(self):
        self.write("holiday.JPG", b"not really an image")
        self.write("invoice-0042.pdf", b"pretend invoice")
        self.write("brochure.pdf", b"pretend brochure")
        self.write("README", b"pretend readme")
        self.write("CHECKLIST", b"pretend list")
        self.write("notes.txt", b"pretend notes")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))

        plan = fileplan.build_plan(self.root, rules=rules)

        self.assertEqual(
            self.destinations(plan),
            {
                "holiday.JPG": ("_organized/images/holiday.JPG", "images"),
                "invoice-0042.pdf": ("_organized/finance/invoices/invoice-0042.pdf", "invoices"),
                "brochure.pdf": ("_organized/pdf/brochure.pdf", None),
                "README": ("_organized/notes/README", "readmes"),
                "CHECKLIST": ("_organized/loose/CHECKLIST", "bare"),
                "notes.txt": ("_organized/txt/notes.txt", None),
            },
        )
        self.assertEqual(plan["options"]["rules"], rules)
        self.assertEqual(plan["summary"]["conflicts"], 0)

    def test_first_matching_rule_wins(self):
        self.write("invoice-7.png", b"pretend scan")
        rules = fileplan.load_rules(
            self.rules_file(
                {
                    "schema_version": 1,
                    "rules": [
                        {"name": "scans", "patterns": ["invoice-*"], "folder": "scans"},
                        {"name": "images", "extensions": ["png"], "folder": "images"},
                    ],
                }
            )
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(self.destinations(plan)["invoice-7.png"], ("_organized/scans/invoice-7.png", "scans"))

    def test_a_rule_with_both_conditions_needs_both(self):
        self.write("invoice-1.pdf", b"pretend")
        self.write("invoice-2.txt", b"pretend")
        self.write("report.pdf", b"pretend")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, rules=rules)
        got = self.destinations(plan)
        self.assertEqual(got["invoice-1.pdf"][1], "invoices")
        self.assertIsNone(got["invoice-2.txt"][1])
        self.assertIsNone(got["report.pdf"][1])

    def test_patterns_ignore_case_and_match_the_filename_only(self):
        self.write("drafts/ReadMe-old.TXT", b"pretend")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, recursive=True, rules=rules)
        self.assertEqual(
            self.destinations(plan)["drafts/ReadMe-old.TXT"],
            ("_organized/notes/ReadMe-old.TXT", "readmes"),
        )

    def test_fallback_folder_collects_unmatched_files(self):
        self.write("mystery.xyz", b"pretend")
        self.write("photo.png", b"pretend")
        rules = fileplan.load_rules(
            self.rules_file(
                {
                    "schema_version": 1,
                    "rules": [{"name": "images", "extensions": ["png"], "folder": "images"}],
                    "fallback": {"folder": "other/unsorted"},
                }
            )
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(
            self.destinations(plan),
            {
                "mystery.xyz": ("_organized/other/unsorted/mystery.xyz", "fallback"),
                "photo.png": ("_organized/images/photo.png", "images"),
            },
        )

    def test_the_filename_itself_is_never_changed_by_a_rule(self):
        self.write("Keep My Case.PDF", b"pretend")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(plan["entries"][0]["destination"], "_organized/pdf/Keep My Case.PDF")

    def test_rules_from_different_folders_can_still_collide_and_are_flagged(self):
        self.write("one/report.pdf", b"pretend one")
        self.write("two/invoice-report.pdf", b"pretend two")
        self.write("_organized/finance/invoices/invoice-report.pdf", b"already here")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, recursive=True, rules=rules)
        by_source = {e["source"]: e for e in plan["entries"]}
        self.assertEqual(by_source["two/invoice-report.pdf"]["status"], "conflict")
        self.assertEqual(by_source["one/report.pdf"]["status"], "ready")

    def test_preview_output_names_the_rules(self):
        self.write("holiday.jpg", b"pretend")
        self.write("notes.txt", b"pretend")
        rules_path = self.rules_file(self.SAMPLE)

        code, stdout, stderr = self.run_cli(str(self.root), "--rules", str(rules_path))

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Rules: 4 custom rules; unmatched files go by extension", stdout)
        self.assertIn('"holiday.jpg" -> "_organized/images/holiday.jpg" by rule "images"', stdout)
        self.assertIn('"notes.txt" -> "_organized/txt/notes.txt"\n', stdout)

    def test_a_plan_without_rules_is_unchanged(self):
        self.write("notes.txt", b"pretend")
        plan = fileplan.build_plan(self.root)
        self.assertIsNone(plan["options"]["rules"])
        self.assertIsNone(plan["entries"][0]["rule"])
        self.assertEqual(plan["entries"][0]["destination"], "_organized/txt/notes.txt")

    def test_verify_rebuilds_with_the_rules_saved_in_the_plan(self):
        self.write("holiday.jpg", b"pretend")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, rules=rules)
        plan_path = self.aside / "plan.json"
        plan_path.write_text(json.dumps(plan))

        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)

        self.assertEqual(report["summary"]["problems"], 0)
        self.assertEqual(report["options"]["rules"], rules)

    def test_verify_flags_a_plan_destination_the_rules_would_not_choose(self):
        self.write("holiday.jpg", b"pretend")
        rules = fileplan.load_rules(self.rules_file(self.SAMPLE))
        plan = fileplan.build_plan(self.root, rules=rules)
        plan["entries"][0]["destination"] = "_organized/jpg/holiday.jpg"
        plan_path = self.aside / "plan.json"
        plan_path.write_text(json.dumps(plan))

        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)

        self.assertEqual(report["entries"][0]["status"], "mismatch")
        self.assertIn("_organized/images/holiday.jpg", report["entries"][0]["reason"])

    def test_a_saved_plan_with_invalid_rules_is_refused(self):
        self.write("holiday.jpg", b"pretend")
        plan = fileplan.build_plan(self.root)
        plan["options"]["rules"] = {"schema_version": 1, "rules": [{"name": "x", "folder": "../out", "extensions": ["jpg"]}]}
        plan_path = self.aside / "plan.json"
        plan_path.write_text(json.dumps(plan))

        code, stdout, stderr = self.run_cli(str(self.root), "--verify", str(plan_path))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("options.rules", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_rules_flag_is_rejected_with_verify_or_undo(self):
        self.write("notes.txt", b"pretend")
        plan_path = self.aside / "plan.json"
        plan_path.write_text(json.dumps(fileplan.build_plan(self.root)))
        rules_path = self.rules_file(self.SAMPLE)
        for extra in (("--verify", str(plan_path)), ("--undo", str(plan_path))):
            with self.subTest(flags=extra):
                code, stdout, stderr = self.run_cli(str(self.root), "--rules", str(rules_path), *extra)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("--rules", stderr)

    def test_invalid_rules_files_fail_before_any_plan_is_made(self):
        self.write("notes.txt", b"pretend")
        base = {"name": "r", "extensions": ["txt"], "folder": "text"}
        bad = {
            "not json": "{nope",
            "a list": [],
            "wrong version": {"schema_version": 2, "rules": []},
            "unknown top key": {"schema_version": 1, "rules": [], "fallbak": "by-extension"},
            "rules not a list": {"schema_version": 1, "rules": {}},
            "rule not an object": {"schema_version": 1, "rules": ["x"]},
            "unknown rule key": {"schema_version": 1, "rules": [dict(base, extension=["txt"])]},
            "no name": {"schema_version": 1, "rules": [dict(base, name="")]},
            "reserved name": {"schema_version": 1, "rules": [dict(base, name="fallback")]},
            "duplicate name": {"schema_version": 1, "rules": [base, dict(base, folder="other")]},
            "no conditions": {"schema_version": 1, "rules": [{"name": "r", "folder": "x"}]},
            "empty extension": {"schema_version": 1, "rules": [dict(base, extensions=[""])]},
            "dotted extension": {"schema_version": 1, "rules": [dict(base, extensions=["tar.gz"])]},
            "extension with slash": {"schema_version": 1, "rules": [dict(base, extensions=["a/b"])]},
            "pattern with slash": {"schema_version": 1, "rules": [dict(base, patterns=["drafts/*"])]},
            "empty pattern": {"schema_version": 1, "rules": [dict(base, patterns=[""])]},
            "folder escapes": {"schema_version": 1, "rules": [dict(base, folder="../escape")]},
            "folder absolute": {"schema_version": 1, "rules": [dict(base, folder="/etc")]},
            "folder backslash": {"schema_version": 1, "rules": [dict(base, folder="a\\b")]},
            "folder drive": {"schema_version": 1, "rules": [dict(base, folder="c:x")]},
            "folder dot": {"schema_version": 1, "rules": [dict(base, folder="a/./b")]},
            "folder empty part": {"schema_version": 1, "rules": [dict(base, folder="a//b")]},
            "folder trailing slash": {"schema_version": 1, "rules": [dict(base, folder="a/")]},
            "folder control char": {"schema_version": 1, "rules": [dict(base, folder="a\u0001b")]},
            "folder is the record folder": {"schema_version": 1, "rules": [dict(base, folder=".fileplan")]},
            "folder under record folder": {"schema_version": 1, "rules": [dict(base, folder=".FilePlan/x")]},
            "bad fallback string": {"schema_version": 1, "rules": [], "fallback": "elsewhere"},
            "bad fallback object": {"schema_version": 1, "rules": [], "fallback": {"folder": "x", "extra": 1}},
            "fallback escapes": {"schema_version": 1, "rules": [], "fallback": {"folder": "../x"}},
        }
        for label, document in bad.items():
            with self.subTest(rules=label):
                path = self.rules_file(document, f"bad-{len(label)}-{label[:5].replace(' ', '_')}.json")
                code, stdout, stderr = self.run_cli(str(self.root), "--rules", str(path))
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.strip())
                self.assertNotIn("Traceback", stderr)
        with self.subTest(rules="missing file"):
            code, stdout, stderr = self.run_cli(str(self.root), "--rules", str(self.aside / "nope.json"))
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")

    def test_extensions_are_normalised_and_rules_are_stored_normalised(self):
        rules = fileplan.load_rules(
            self.rules_file(
                {"schema_version": 1, "rules": [{"name": "img", "extensions": [".JPG", "Png"], "folder": "images"}]}
            )
        )
        self.assertEqual(rules["rules"][0]["extensions"], ["jpg", "png"])
        self.assertEqual(rules["fallback"], "by-extension")
        self.assertEqual(rules["rules"][0]["patterns"], [])

    def test_an_empty_rule_list_with_a_fallback_folder_puts_everything_there(self):
        self.write("a.txt", b"pretend")
        self.write("b.png", b"pretend")
        rules = fileplan.load_rules(
            self.rules_file({"schema_version": 1, "rules": [], "fallback": {"folder": "everything"}})
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(
            {e["destination"] for e in plan["entries"]},
            {"_organized/everything/a.txt", "_organized/everything/b.png"},
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows reserves device names")
    def test_a_nested_folder_with_a_reserved_name_is_a_conflict_on_windows(self):
        self.write("notes.txt", b"pretend")
        rules = fileplan.load_rules(
            self.rules_file({"schema_version": 1, "rules": [{"name": "r", "extensions": ["txt"], "folder": "ok/CON"}]})
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(plan["summary"]["conflicts"], 1)

    def test_the_example_rules_file_is_valid_and_shapes_the_example_inbox(self):
        here = Path(fileplan.__file__).resolve().parent
        rules = fileplan.load_rules(here / "examples" / "rules.json")
        plan = fileplan.build_plan(here / "examples" / "inbox", recursive=True, rules=rules)
        by_source = {e["source"]: e for e in plan["entries"]}
        self.assertEqual(by_source["outline.md"]["destination"], "_organized/writing/outline.md")
        self.assertEqual(by_source["assets/colors.json"]["rule"], "assets")
        self.assertIsNone(by_source["notes.txt"]["rule"])


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and the no-replace rename"
)
class RulesApplyTests(_ApplyFixture):
    """Applying and undoing with nested rule folders. Every sample is made up."""

    def test_apply_and_undo_round_trip_with_nested_rule_folders(self):
        self.write("invoice-9.pdf", b"pretend invoice")
        self.write("holiday.jpg", b"pretend photo")
        self.write("misc.dat", b"pretend data")
        before = self.tree()
        rules_path = self.plans / "rules.json"
        rules_path.write_text(json.dumps(RulesTests.SAMPLE | {"fallback": {"folder": "other/unsorted"}}))
        rules = fileplan.load_rules(rules_path)
        plan_path = self.plans / "plan.json"
        plan_path.write_text(json.dumps(fileplan.build_plan(self.root, rules=rules)))

        applied = self.apply(plan_path)

        self.assertTrue(applied["summary"]["completed"])
        self.assertEqual(
            self.tree(),
            {
                "_organized/finance/invoices/invoice-9.pdf": b"pretend invoice",
                "_organized/images/holiday.jpg": b"pretend photo",
                "_organized/other/unsorted/misc.dat": b"pretend data",
            },
        )

        record = fileplan.load_record(Path(applied["record"]["path"]))
        undone = fileplan.undo_record(record, self.root, Path(applied["record"]["path"]))

        self.assertTrue(undone["summary"]["completed"])
        self.assertEqual(self.tree(), before)


class ConflictChoiceTests(unittest.TestCase):
    """File-versus-folder clashes, duplicate checks, and choices. All made up."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        aside = tempfile.TemporaryDirectory()
        self.addCleanup(aside.cleanup)
        self.aside = Path(aside.name)

    def write(self, name, content=b"pretend contents\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def rules(self, *rules, fallback="by-extension"):
        path = self.aside / f"rules-{len(rules)}-{abs(hash(repr(rules))) % 10_000}.json"
        path.write_text(json.dumps({"schema_version": 1, "rules": list(rules), "fallback": fallback}))
        return fileplan.load_rules(path)

    def save(self, plan, name="plan.json"):
        path = self.aside / name
        path.write_text(json.dumps(plan))
        return path

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fileplan.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def by_source(self, plan):
        return {e["source"]: e for e in plan["entries"]}

    # --- a path that would need to be both a file and a folder ---------------

    def test_a_file_and_a_folder_at_the_same_path_are_both_conflicts(self):
        self.write("a.txt", b"pretend text")
        self.write("b.md", b"pretend markdown")
        rules = self.rules(
            {"name": "text", "extensions": ["txt"], "folder": "docs"},
            {"name": "inside", "extensions": ["md"], "folder": "docs/a.txt"},
        )

        plan = fileplan.build_plan(self.root, rules=rules)

        got = self.by_source(plan)
        self.assertEqual(got["a.txt"]["destination"], "_organized/docs/a.txt")
        self.assertEqual(got["b.md"]["destination"], "_organized/docs/a.txt/b.md")
        self.assertEqual(got["a.txt"]["status"], "conflict")
        self.assertEqual(got["b.md"]["status"], "conflict")
        self.assertIn("also needed as a folder by b.md", got["a.txt"]["reason"])
        self.assertIn("_organized/docs/a.txt is also a planned file", got["b.md"]["reason"])
        self.assertEqual(plan["summary"]["conflicts"], 2)

    def test_file_versus_folder_clash_ignores_case(self):
        self.write("A.TXT", b"pretend text")
        self.write("b.md", b"pretend markdown")
        rules = self.rules(
            {"name": "text", "extensions": ["txt"], "folder": "docs"},
            {"name": "inside", "extensions": ["md"], "folder": "docs/a.txt"},
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(plan["summary"]["conflicts"], 2)

    def test_a_deeper_folder_clash_is_caught_too(self):
        self.write("a.txt", b"pretend text")
        self.write("deep.md", b"pretend markdown")
        rules = self.rules(
            {"name": "text", "extensions": ["txt"], "folder": "docs"},
            {"name": "inside", "extensions": ["md"], "folder": "docs/a.txt/more/levels"},
        )
        plan = fileplan.build_plan(self.root, rules=rules)
        self.assertEqual(plan["summary"]["conflicts"], 2)

    def test_default_grouping_cannot_produce_a_file_versus_folder_clash(self):
        self.write("txt", b"a file literally named txt")
        self.write("notes.txt", b"pretend notes")
        plan = fileplan.build_plan(self.root)
        got = self.by_source(plan)
        self.assertEqual(got["txt"]["destination"], "_organized/no-extension/txt")
        self.assertEqual(plan["summary"]["conflicts"], 0)

    # --- keep, skip, rename ----------------------------------------------------

    def test_skip_resolves_a_shared_destination(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        plan = fileplan.build_plan(
            self.root, recursive=True, choices={"two/notes.txt": "skip"}
        )
        got = self.by_source(plan)
        self.assertEqual(got["one/notes.txt"]["status"], "ready")
        self.assertEqual(got["two/notes.txt"]["status"], "skipped")
        self.assertEqual(got["two/notes.txt"]["reason"], "Skipped by choice")
        self.assertEqual(
            plan["summary"], {"files": 2, "ready": 1, "conflicts": 0, "skipped": 1, "total_bytes": 22}
        )

    def test_rename_gives_a_numbered_name_and_records_the_original(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        plan = fileplan.build_plan(
            self.root, recursive=True, choices={"two/notes.txt": "rename"}
        )
        got = self.by_source(plan)
        self.assertEqual(got["one/notes.txt"]["status"], "ready")
        self.assertEqual(got["two/notes.txt"]["status"], "ready")
        self.assertEqual(got["two/notes.txt"]["destination"], "_organized/txt/notes (2).txt")
        self.assertEqual(got["two/notes.txt"]["renamed_from"], "_organized/txt/notes.txt")
        self.assertIsNone(got["one/notes.txt"]["renamed_from"])

    def test_rename_skips_numbers_taken_on_disk_or_by_other_renames(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        self.write("three/notes.txt", b"pretend three")
        self.write("_organized/txt/Notes (2).TXT", b"already here")
        plan = fileplan.build_plan(
            self.root,
            recursive=True,
            choices={"two/notes.txt": "rename", "three/notes.txt": "rename"},
        )
        got = self.by_source(plan)
        self.assertEqual(got["three/notes.txt"]["destination"], "_organized/txt/notes (3).txt")
        self.assertEqual(got["two/notes.txt"]["destination"], "_organized/txt/notes (4).txt")
        self.assertEqual(plan["summary"]["conflicts"], 0)

    def test_rename_keeps_a_compound_extension_readable(self):
        self.write("one/backup.tar.gz", b"pretend one")
        self.write("two/backup.tar.gz", b"pretend two")
        plan = fileplan.build_plan(
            self.root, recursive=True, choices={"two/backup.tar.gz": "rename"}
        )
        self.assertEqual(
            self.by_source(plan)["two/backup.tar.gz"]["destination"],
            "_organized/gz/backup.tar (2).gz",
        )

    def test_keep_marks_the_winner_and_needs_the_others_to_step_aside(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        both_keep = fileplan.build_plan(
            self.root, recursive=True, choices={"one/notes.txt": "keep", "two/notes.txt": "keep"}
        )
        self.assertEqual(both_keep["summary"]["conflicts"], 2)
        keep_and_skip = fileplan.build_plan(
            self.root, recursive=True, choices={"one/notes.txt": "keep", "two/notes.txt": "skip"}
        )
        got = self.by_source(keep_and_skip)
        self.assertEqual(got["one/notes.txt"]["status"], "ready")
        self.assertEqual(got["two/notes.txt"]["status"], "skipped")
        keep_and_nothing = fileplan.build_plan(
            self.root, recursive=True, choices={"one/notes.txt": "keep"}
        )
        self.assertEqual(keep_and_nothing["summary"]["conflicts"], 2)

    def test_keep_cannot_claim_a_destination_that_already_exists(self):
        self.write("notes.txt", b"pretend notes")
        self.write("_organized/txt/notes.txt", b"already here")
        plan = fileplan.build_plan(self.root, choices={"notes.txt": "keep"})
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "conflict")
        self.assertIn("Destination already exists", entry["reason"])

    def test_rename_steps_around_a_destination_that_already_exists(self):
        self.write("notes.txt", b"pretend notes")
        self.write("_organized/txt/notes.txt", b"already here")
        plan = fileplan.build_plan(self.root, choices={"notes.txt": "rename"})
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "ready")
        self.assertEqual(entry["destination"], "_organized/txt/notes (2).txt")

    def test_an_unknown_choice_is_refused(self):
        self.write("notes.txt", b"pretend notes")
        with self.assertRaises(fileplan.FilePlanError):
            fileplan.build_plan(self.root, choices={"notes.txt": "maybe"})
        plan = fileplan.build_plan(self.root)
        plan["entries"][0]["choice"] = "overwrite"
        code, stdout, stderr = self.run_cli(str(self.root), "--verify", str(self.save(plan)))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("keep, skip, rename", stderr)

    def test_preview_output_shows_choices(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        self.write("three/notes.txt", b"pretend three")
        plan = fileplan.build_plan(
            self.root,
            recursive=True,
            choices={"one/notes.txt": "keep", "two/notes.txt": "rename", "three/notes.txt": "skip"},
        )
        text = fileplan._plain_output(plan)
        self.assertIn('"one/notes.txt" -> "_organized/txt/notes.txt", kept by choice', text)
        self.assertIn('-> "_organized/txt/notes (2).txt", renamed by choice from "_organized/txt/notes.txt"', text)
        self.assertIn('[skipped] "three/notes.txt" -> "_organized/txt/notes.txt" ("Skipped by choice")', text)
        self.assertIn("1 skipped by choice", text)

    def test_verify_honours_choices_written_into_a_saved_plan(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        self.write("three/notes.txt", b"pretend three")
        plan = fileplan.build_plan(self.root, recursive=True)
        self.assertEqual(plan["summary"]["conflicts"], 3)
        # The user reviews the plan and writes in a choice for each.
        choices = {"one/notes.txt": "keep", "two/notes.txt": "rename", "three/notes.txt": "skip"}
        for entry in plan["entries"]:
            entry["choice"] = choices[entry["source"]]
        plan_path = self.save(plan)

        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)

        got = {e["source"]: e for e in report["entries"]}
        self.assertEqual(got["one/notes.txt"]["status"], "unchanged")
        self.assertEqual(got["two/notes.txt"]["status"], "unchanged")
        self.assertEqual(got["two/notes.txt"]["effective_destination"], "_organized/txt/notes (2).txt")
        self.assertEqual(got["three/notes.txt"]["status"], "skipped")
        self.assertEqual(report["summary"]["problems"], 0)
        self.assertEqual(report["summary"]["skipped"], 1)
        text = fileplan._plain_verification_output(report)
        self.assertIn('[unchanged] "two/notes.txt" -> "_organized/txt/notes (2).txt"', text)

    def test_verify_still_flags_a_tampered_destination_on_a_renamed_entry(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        plan = fileplan.build_plan(self.root, recursive=True)
        for entry in plan["entries"]:
            if entry["source"] == "two/notes.txt":
                entry["choice"] = "rename"
                entry["destination"] = "_organized/txt/sneaky.txt"
            else:
                entry["choice"] = "keep"
        plan_path = self.save(plan)
        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)
        got = {e["source"]: e for e in report["entries"]}
        self.assertEqual(got["two/notes.txt"]["status"], "mismatch")

    # --- duplicate check ------------------------------------------------------

    def test_duplicate_check_tells_identical_contents_from_a_shared_name(self):
        self.write("one/notes.txt", b"the very same pretend text")
        self.write("two/notes.txt", b"the very same pretend text")
        self.write("three/notes.txt", b"something else entirely")
        plan = fileplan.build_plan(self.root, recursive=True, check_duplicates=True)
        got = self.by_source(plan)
        self.assertEqual(got["one/notes.txt"]["sha256"], got["two/notes.txt"]["sha256"])
        self.assertNotEqual(got["one/notes.txt"]["sha256"], got["three/notes.txt"]["sha256"])
        self.assertEqual(got["one/notes.txt"]["identical_to"], ["two/notes.txt"])
        self.assertEqual(got["one/notes.txt"]["differs_from"], ["three/notes.txt"])
        self.assertIn("Contents identical to two/notes.txt", got["one/notes.txt"]["reason"])
        self.assertIn("Contents differ from three/notes.txt", got["one/notes.txt"]["reason"])
        self.assertEqual(got["three/notes.txt"]["identical_to"], [])
        self.assertTrue(plan["options"]["check_duplicates"])
        self.assertEqual(plan["summary"]["conflicts"], 3)

    def test_duplicate_check_compares_against_an_existing_destination(self):
        self.write("notes.txt", b"pretend notes")
        self.write("_organized/txt/notes.txt", b"pretend notes")
        self.write("report.txt", b"pretend report")
        self.write("_organized/txt/report.txt", b"a different report")
        plan = fileplan.build_plan(self.root, check_duplicates=True)
        got = self.by_source(plan)
        self.assertEqual(got["notes.txt"]["identical_to"], ["_organized/txt/notes.txt"])
        self.assertEqual(got["report.txt"]["differs_from"], ["_organized/txt/report.txt"])

    def test_only_conflicting_files_are_read_during_a_duplicate_check(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        self.write("alone.md", b"pretend alone")
        read = []
        real = fileplan._hash_file

        def counting(path, label, *rest):
            read.append(label)
            return real(path, label, *rest)

        with mock.patch.object(fileplan, "_hash_file", side_effect=counting):
            plan = fileplan.build_plan(self.root, recursive=True, check_duplicates=True)

        self.assertEqual(sorted(read), ["one/notes.txt", "two/notes.txt"])
        self.assertNotIn("sha256", self.by_source(plan)["alone.md"])

    def test_without_the_flag_no_file_is_read_even_when_conflicting(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        with mock.patch.object(fileplan, "_hash_file", side_effect=AssertionError("read a file")):
            plan = fileplan.build_plan(self.root, recursive=True)
        self.assertFalse(plan["options"]["check_duplicates"])
        self.assertNotIn("sha256", plan["entries"][0])

    def test_duplicate_check_handles_a_large_file(self):
        big = (b"pretend-block-" * 37)[:512] * (6 * 1024)  # 3 MiB, several chunks
        self.write("one/video.bin", big)
        self.write("two/video.bin", big)
        plan = fileplan.build_plan(self.root, recursive=True, check_duplicates=True)
        got = self.by_source(plan)
        self.assertEqual(got["one/video.bin"]["sha256"], hashlib.sha256(big).hexdigest())
        self.assertEqual(got["one/video.bin"]["identical_to"], ["two/video.bin"])

    def test_a_file_that_changes_during_the_duplicate_check_is_flagged(self):
        target = self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        real_digest = fileplan._digest

        def digest(fd):
            result = real_digest(fd)
            if os.fstat(fd).st_size == len(b"pretend one"):
                with target.open("ab") as handle:
                    handle.write(b" and then some")
            return result

        with mock.patch.object(fileplan, "_digest", side_effect=digest):
            plan = fileplan.build_plan(self.root, recursive=True, check_duplicates=True)

        got = self.by_source(plan)
        self.assertIsNone(got["one/notes.txt"]["sha256"])
        self.assertIn("changed during the duplicate check", got["one/notes.txt"]["reason"])
        self.assertEqual(got["one/notes.txt"]["status"], "conflict")
        self.assertIsNotNone(got["two/notes.txt"]["sha256"])

    def test_a_link_at_the_destination_is_not_read_for_comparison(self):
        self.write("notes.txt", b"pretend notes")
        decoy = self.write("elsewhere/decoy.txt", b"pretend notes")
        (self.root / "_organized/txt").mkdir(parents=True)
        try:
            (self.root / "_organized/txt/notes.txt").symlink_to(decoy)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        plan = fileplan.build_plan(self.root, check_duplicates=True)
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "conflict")
        self.assertNotIn("sha256", entry)
        self.assertIn("could not be compared", entry["reason"])

    def test_duplicate_check_flag_round_trips_through_verify(self):
        self.write("one/notes.txt", b"same pretend text")
        self.write("two/notes.txt", b"same pretend text")
        code, stdout, stderr = self.run_cli(
            str(self.root), "--recursive", "--check-duplicates", "--format", "json"
        )
        self.assertEqual((code, stderr), (0, ""))
        plan = json.loads(stdout)
        self.assertIn("Contents identical", plan["entries"][0]["reason"])
        plan_path = self.save(plan)
        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)
        self.assertTrue(report["options"]["check_duplicates"])
        self.assertEqual(report["summary"]["problems"], 2)

    def test_duplicate_check_flag_is_rejected_with_verify_or_undo(self):
        self.write("notes.txt", b"pretend notes")
        plan_path = self.save(fileplan.build_plan(self.root))
        for extra in (("--verify", str(plan_path)), ("--undo", str(plan_path))):
            with self.subTest(flags=extra):
                code, stdout, stderr = self.run_cli(str(self.root), "--check-duplicates", *extra)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("--check-duplicates", stderr)

    def test_plain_output_mentions_the_duplicate_check(self):
        self.write("notes.txt", b"pretend notes")
        code, stdout, stderr = self.run_cli(str(self.root), "--check-duplicates")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Duplicate check:", stdout)


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and the no-replace rename"
)
class ChoiceApplyTests(_ApplyFixture):
    """Applying and undoing a plan with choices. Every sample is made up."""

    def test_apply_moves_the_kept_and_renamed_files_and_leaves_the_skipped_one(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        self.write("three/notes.txt", b"pretend three")
        before = self.tree()
        plan = fileplan.build_plan(self.root, recursive=True)
        choices = {"one/notes.txt": "keep", "two/notes.txt": "rename", "three/notes.txt": "skip"}
        for entry in plan["entries"]:
            entry["choice"] = choices[entry["source"]]
        plan_path = self.plans / "plan.json"
        plan_path.write_text(json.dumps(plan))

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn('[moved] "one/notes.txt" -> "_organized/txt/notes.txt"', stdout)
        self.assertIn('[moved] "two/notes.txt" -> "_organized/txt/notes (2).txt"', stdout)
        self.assertIn('[skipped by choice] "three/notes.txt"', stdout)
        self.assertIn("Moved 2 of 3 files. 1 skipped by choice.", stdout)
        self.assertEqual(
            self.tree(),
            {
                "_organized/txt/notes.txt": b"pretend one",
                "_organized/txt/notes (2).txt": b"pretend two",
                "three/notes.txt": b"pretend three",
            },
        )

        record_path = self.record_files()[0]
        records = [json.loads(line) for line in record_path.read_text().splitlines()]
        self.assertNotIn("three/notes.txt", [r.get("source") for r in records])
        self.assertEqual(
            [r["destination"] for r in records if r["record"] == "intent"],
            ["_organized/txt/notes.txt", "_organized/txt/notes (2).txt"],
        )

        undone = fileplan.undo_record(fileplan.load_record(record_path), self.root, record_path)
        self.assertTrue(undone["summary"]["completed"])
        self.assertEqual(self.tree(), before)

    def record_files(self):
        folder = self.root / "_organized" / fileplan.JOURNAL_DIRECTORY
        return sorted(p for p in folder.iterdir()) if folder.exists() else []

    def test_apply_refuses_a_plan_whose_clash_is_left_unresolved(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("two/notes.txt", b"pretend two")
        before = self.tree()
        plan = fileplan.build_plan(self.root, recursive=True)
        for entry in plan["entries"]:
            if entry["source"] == "one/notes.txt":
                entry["choice"] = "keep"
        plan_path = self.plans / "plan.json"
        plan_path.write_text(json.dumps(plan))

        report = self.apply(plan_path)

        self.assertFalse(report["summary"]["applied"])
        self.assertEqual(self.tree(), before)


@unittest.skipUnless(
    fileplan._SAFE_MOVE_SUPPORTED, "needs directory handles and the no-replace rename"
)
class ComparedContentsTests(_ApplyFixture):
    """A compared file is compared again before it moves. All samples made up."""

    def forge(self, path, content):
        """Rewrite a file keeping its size and modification time identical."""
        before = path.stat()
        self.assertEqual(len(content), before.st_size)
        path.write_bytes(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def compared_plan(self, choices=None, name="plan.json"):
        plan = fileplan.build_plan(self.root, recursive=True, check_duplicates=True)
        for entry in plan["entries"]:
            entry["choice"] = (choices or {}).get(entry["source"])
        path = self.plans / name
        path.write_text(json.dumps(plan))
        return path

    def record_files(self):
        folder = self.root / "_organized" / fileplan.JOURNAL_DIRECTORY
        return sorted(p for p in folder.iterdir()) if folder.exists() else []

    def test_apply_stops_when_compared_contents_change_behind_identical_metadata(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})
        self.forge(self.root / "one/notes.txt", b"a quite different text")
        before = self.tree()

        code, stdout, stderr = self.run_cli(
            str(self.root), "--verify", str(plan_path), "--apply"
        )

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Refused", stdout)
        self.assertIn("Contents changed since they were compared", stdout)
        self.assertIn("size and modification time did not", stdout)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "_organized/txt").exists())

    def test_a_skipped_file_is_still_compared_and_still_stops_the_run(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        # two is skipped, so it is in no conflict at all any more.
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "skip"})
        self.forge(self.root / "two/notes.txt", b"a quite different text")

        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)

        entry = {e["source"]: e for e in report["entries"]}["two/notes.txt"]
        self.assertEqual(entry["status"], "changed")
        self.assertIn("Contents changed since they were compared", entry["reason"])
        self.assertEqual(report["summary"]["problems"], 1)

    def test_a_renamed_file_out_of_every_conflict_is_still_compared(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})
        self.forge(self.root / "two/notes.txt", b"a quite different text")

        report = fileplan.verify_plan(fileplan.load_plan(plan_path), self.root, plan_path)

        entry = {e["source"]: e for e in report["entries"]}["two/notes.txt"]
        self.assertEqual(entry["status"], "changed")
        self.assertIn("Contents changed", entry["reason"])

    def test_the_move_itself_refuses_a_file_changed_after_verification(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "skip"})
        plan = fileplan.load_plan(plan_path)
        target = self.root / "one/notes.txt"
        real_verify = fileplan.verify_plan

        def verify(*args, **kwargs):
            report = real_verify(*args, **kwargs)
            # Verification passed; the file is forged in the gap before the move.
            self.forge(target, b"a quite different text")
            return report

        with mock.patch.object(fileplan, "verify_plan", side_effect=verify):
            report = fileplan.apply_plan(plan, self.root, plan_path)

        self.assertEqual(report["summary"]["moved"], 0)
        self.assertEqual(report["results"][0]["outcome"], "not-moved")
        self.assertIn("Contents of one/notes.txt changed", report["stopped"]["reason"])
        self.assertEqual(target.read_bytes(), b"a quite different text")
        self.assertFalse((self.root / "_organized/txt/notes.txt").exists())

    def test_an_unchanged_compared_file_still_moves(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})

        report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])
        self.assertEqual(
            self.tree(),
            {
                "_organized/txt/notes.txt": b"the first pretend text",
                "_organized/txt/notes (2).txt": b"the first pretend text",
            },
        )

    def test_the_recorded_hash_travels_into_the_run_record(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})
        expected = hashlib.sha256(b"the first pretend text").hexdigest()

        self.apply(plan_path)

        records = [json.loads(line) for line in self.record_files()[0].read_text().splitlines()]
        intents = [r for r in records if r["record"] == "intent"]
        self.assertEqual([r["sha256"] for r in intents], [expected, expected])

    def test_undo_refuses_a_stored_file_whose_contents_were_forged(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})
        applied = self.apply(plan_path)
        record_path = Path(applied["record"]["path"])
        self.forge(self.root / "_organized/txt/notes.txt", b"a quite different text")
        before = self.tree()

        code, stdout, stderr = self.run_cli(str(self.root), "--undo", str(record_path))

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("[blocked]", stdout)
        self.assertIn("changed since they were compared", stdout)
        self.assertIn("Nothing was moved", stdout)
        self.assertEqual(self.tree(), before)

    def test_undo_restores_normally_when_contents_are_untouched(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        before = self.tree()
        plan_path = self.compared_plan({"one/notes.txt": "keep", "two/notes.txt": "rename"})
        applied = self.apply(plan_path)

        record_path = Path(applied["record"]["path"])
        undone = fileplan.undo_record(fileplan.load_record(record_path), self.root, record_path)

        self.assertTrue(undone["summary"]["completed"])
        self.assertEqual(self.tree(), before)

    def test_a_plan_without_comparison_reads_nothing_during_apply(self):
        self.write("one/notes.txt", b"pretend one")
        self.write("alone.md", b"pretend alone")
        plan = fileplan.build_plan(self.root, recursive=True)
        plan_path = self.plans / "plain.json"
        plan_path.write_text(json.dumps(plan))

        with mock.patch.object(
            fileplan, "_digest", side_effect=AssertionError("contents were read")
        ):
            report = self.apply(plan_path)

        self.assertTrue(report["summary"]["completed"])

    def test_a_tampered_hash_in_a_saved_plan_is_refused(self):
        self.write("one/notes.txt", b"the first pretend text")
        self.write("two/notes.txt", b"the first pretend text")
        plan_path = self.compared_plan()
        plan = json.loads(plan_path.read_text())
        plan["entries"][0]["sha256"] = "nonsense"
        plan_path.write_text(json.dumps(plan))

        code, stdout, stderr = self.run_cli(str(self.root), "--verify", str(plan_path))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("64 hex characters", stderr)


if __name__ == "__main__":
    unittest.main()
