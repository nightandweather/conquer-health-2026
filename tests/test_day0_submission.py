"""Packaging and offline behavior checks for the pinned official fallback."""

from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts import build_day0_submission as day0


class Day0SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nova-day0-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in day0.EXPECTED:
            shutil.copyfile(day0.DEFAULT_SOURCE / name, self.source / name)

    def archive(self):
        path = self.root / "candidate.zip"
        path.write_bytes(day0.archive_bytes(day0.read_sources(self.source)))
        return path

    def bad_archive(self, entries):
        path = self.root / "bad.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(path, "w", compression=ZIP_DEFLATED) as bundle:
                for name, raw, mode in entries:
                    item = ZipInfo(name)
                    item.create_system = 3
                    item.external_attr = mode << 16
                    bundle.writestr(item, raw)
        return path

    def entries(self):
        return [(name, raw, 0o100644)
                for name, raw in day0.read_sources(self.source).items()]

    def test_pinned_sources_and_exact_root_archive(self):
        captured = day0.read_sources(self.source)
        self.assertEqual(day0.validate_archive(self.archive()), captured)
        self.assertEqual(len(captured), 3)
        self.assertEqual(len(captured["requirements.txt"]), 168)
        self.assertIn(b"__version__ = '1.0.1'", captured["nova_sdk.py"])
        self.assertIn(b"REASONING_EFFORT = 'medium'", captured["run.py"])

    def test_zip_is_reproducible_and_excludes_docs(self):
        captured = day0.read_sources(self.source)
        first = day0.archive_bytes(captured)
        (self.source / "README.md").write_text("Local preparation notes are not runtime files")
        second = day0.archive_bytes(day0.read_sources(self.source))
        self.assertEqual(first, second)
        with ZipFile(self.archive()) as bundle:
            self.assertEqual(bundle.namelist(), sorted(day0.EXPECTED))

    def test_each_source_modification_is_rejected(self):
        for name in day0.EXPECTED:
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                    day0.read_sources(self.source)
                path.write_bytes(original)

    def test_source_symlink_is_rejected(self):
        path = self.source / "run.py"
        path.unlink()
        path.symlink_to(day0.DEFAULT_SOURCE / "run.py")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            day0.read_sources(self.source)

    def test_source_directory_symlink_is_rejected(self):
        link = self.root / "linked"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "regular directory"):
            day0.read_sources(link)

    def test_no_alternate_pins_or_extra_payload(self):
        captured = day0.read_sources(self.source)
        captured["extra.py"] = b"print('extra')"
        with self.assertRaisesRegex(ValueError, "three pinned"):
            day0.archive_bytes(captured)

    def test_zip_size_limit_is_enforced(self):
        with patch.object(day0, "MAX_ZIP_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "ZIP exceeds"):
                day0.archive_bytes(day0.read_sources(self.source))
        archive = self.archive()
        with patch.object(day0, "MAX_ZIP_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "ZIP exceeds"):
                day0.validate_archive(archive)

    def test_unpacked_size_and_count_limits_are_enforced(self):
        archive = self.archive()
        with patch.object(day0, "MAX_UNPACKED_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "unpacked-size limits"):
                day0.validate_archive(archive)
        with patch.object(day0, "MAX_FILES", 2):
            with self.assertRaisesRegex(ValueError, "file or unpacked-size limits"):
                day0.validate_archive(archive)

    def test_duplicate_member_is_rejected(self):
        entries = self.entries()
        entries.append(entries[0])
        with self.assertRaisesRegex(ValueError, "Duplicate ZIP member"):
            day0.validate_archive(self.bad_archive(entries))

    def test_traversal_and_non_root_member_are_rejected(self):
        for wrong in ("../run.py", "/run.py", "nested/run.py", "run\\.py"):
            with self.subTest(wrong=wrong):
                entries = self.entries()
                entries = [(wrong if name == "run.py" else name, raw, mode)
                           for name, raw, mode in entries]
                with self.assertRaisesRegex(ValueError, "three root runtime files"):
                    day0.validate_archive(self.bad_archive(entries))

    def test_zip_symlink_is_rejected(self):
        entries = [(name, raw, stat.S_IFLNK | 0o777 if name == "run.py" else mode)
                   for name, raw, mode in self.entries()]
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            day0.validate_archive(self.bad_archive(entries))

    def test_archive_contents_must_match_pins(self):
        entries = [(name, raw + b"\n" if name == "run.py" else raw, mode)
                   for name, raw, mode in self.entries()]
        with self.assertRaisesRegex(ValueError, "ZIP source SHA mismatch"):
            day0.validate_archive(self.bad_archive(entries))

    def test_existing_output_is_refused_before_write(self):
        output = self.root / "already-present"
        output.mkdir()
        sentinel = output / "day0-official-sdk101.zip"
        sentinel.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            day0.build(self.source, output)
        self.assertEqual(sentinel.read_bytes(), b"do not overwrite")
        self.assertFalse((output / "manifest.json").exists())

    def test_isolated_import_sdk_wire_and_five_consult_paths(self):
        receipt = day0.check_offline(self.archive())
        self.assertEqual(receipt["sdk_version"], "1.0.1")
        self.assertTrue(receipt["socket_access_denied"])
        self.assertFalse(receipt["credentials_present"])
        self.assertTrue(receipt["sdk_wire_user_agent_verified"])
        self.assertEqual(len(receipt["scenarios"]), 5)
        self.assertTrue(all(case["passed"] for case in receipt["scenarios"]))
        self.assertEqual(receipt["actual_http_requests"], 0)
        self.assertEqual(receipt["actual_model_calls"], 0)
        self.assertIsNone(receipt["server_validation_passed"])
        self.assertIsNone(receipt["clinical_quality_improvement"])

    def test_completed_build_records_only_offline_preparation(self):
        output = self.root / "fresh"
        manifest = day0.build(self.source, output)
        self.assertEqual(manifest["zip_sha256"], day0.sha256((output / manifest["zip_file"]).read_bytes()))
        self.assertEqual(manifest["zip_members"], sorted(day0.EXPECTED))
        self.assertEqual(manifest["server_sample_validation"], "not_run")
        self.assertEqual(manifest["official_evaluation"], "not_requested")
        self.assertEqual(manifest["clinical_quality"], "not_measured")
        self.assertFalse(manifest["guideline_or_healthbench_prompt_changes"])
        self.assertEqual((output / "manifest.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
