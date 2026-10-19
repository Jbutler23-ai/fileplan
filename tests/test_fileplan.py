import contextlib
import io
import json
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

        self.assertEqual(plan["schema_version"], 1)
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
        self.assertEqual(plan["summary"], {"files": 4, "ready": 4, "conflicts": 0, "total_bytes": 10})
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
        self.assertEqual(plan["summary"], {"files": 3, "ready": 1, "conflicts": 2, "total_bytes": 16})

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
        self.assertEqual(plan["summary"], {"files": 0, "ready": 0, "conflicts": 0, "total_bytes": 0})
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


if __name__ == "__main__":
    unittest.main()
