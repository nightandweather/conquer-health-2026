import json
import os
from pathlib import Path
import sqlite3
import stat
import unittest
from unittest.mock import patch

from nova import guideline_prepare as p
from nova import guidelines as g
from tests import test_guidelines as fixtures


class GuidelinePrepareTests(unittest.TestCase):
    # Reuse the existing strictly synthetic catalog/Markdown fixture, without
    # inheriting or rerunning the existing module's test suite.
    setUp = fixtures.GuidelineTests.setUp
    tearDown = fixtures.GuidelineTests.tearDown
    add = fixtures.GuidelineTests.add
    catalog = fixtures.GuidelineTests.catalog

    def prepare(self, root=None):
        with patch.dict(os.environ, {"NOVA_GUIDELINES_DIR": str(self.source)}):
            return p.prepare_offline_guidelines(self.output if root is None else root)

    def test_actual_synthetic_build_private_receipts_and_no_network(self):
        before = {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        with patch("socket.socket", side_effect=AssertionError("No network")) as socket_call:
            result = self.prepare()
        socket_call.assert_not_called()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["aggregate"]["document_count"], 2)
        self.assertEqual(result["aggregate"]["chunk_count"], 3)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["resource_limits_enforced"])
        self.assertFalse(result["nova_server_validated"])
        self.assertFalse(result["clinical_quality_verified"])
        self.assertEqual(set(result["sqlite_capabilities"]),
                         {"sqlite_version", "fts5", "json_extract", "materialized_cte"})
        work = Path(result["work_dir"])
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(Path(result["index_path"]).stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(Path(result["receipt_path"]).stat().st_mode), 0o600)
        self.assertEqual(json.loads(Path(result["receipt_path"]).read_text()), result)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertNotIn("Synthetic aspirin", json.dumps(result))
        self.assertNotIn("aspirin evidence", json.dumps(result))

    def test_environment_output_root_and_no_implicit_fallback(self):
        with patch.dict(os.environ, {"NOVA_GUIDELINES_DIR": str(self.source),
                                     "NOVA_OUTPUT_DIR": str(self.output)}, clear=True):
            result = p.prepare_offline_guidelines()
        self.assertEqual(Path(result["work_dir"]).parent, self.output)
        with patch.dict(os.environ, {"NOVA_GUIDELINES_DIR": str(self.source)}, clear=True):
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                p.prepare_offline_guidelines()
        self.assertEqual(caught.exception.stage, "configuration")
        self.assertIsNone(caught.exception.work_dir)

    def test_three_missing_sqlite_capabilities_fail_before_build(self):
        real_connect = sqlite3.connect
        for phrase in ("fts5", "json_extract", "MATERIALIZED"):
            with self.subTest(phrase=phrase):
                class LimitedConnection:
                    def __init__(self):
                        self.con = real_connect(":memory:")

                    def execute(self, query):
                        if phrase in query:
                            raise sqlite3.OperationalError("synthetic feature unavailable: " + phrase)
                        return self.con.execute(query)

                    def close(self):
                        self.con.close()

                with patch.object(p.sqlite3, "connect", return_value=LimitedConnection()), \
                        patch.object(g, "build_index") as build:
                    with self.assertRaises(p.GuidelinePreparationError) as caught:
                        self.prepare()
                build.assert_not_called()
                error = caught.exception
                self.assertEqual(error.stage, "sqlite-capabilities")
                self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
                receipt = json.loads(Path(error.evidence_path).read_text())
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["exception_type"], "OperationalError")
                self.assertFalse((Path(error.work_dir) / "guidelines.sqlite").exists())

    def test_every_prepare_allocates_unique_directory_without_reusing_database(self):
        first = self.prepare()
        raw = Path(first["index_path"]).read_bytes()
        second = self.prepare()
        self.assertNotEqual(first["work_dir"], second["work_dir"])
        self.assertEqual(Path(first["index_path"]).read_bytes(), raw)
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_name_collision_never_reuses_directory_or_overwrites_receipts(self):
        class FixedId:
            hex = "0" * 32
        with patch.object(p.uuid, "uuid4", return_value=FixedId()):
            first = self.prepare()
            before = {path.name: path.read_bytes() for path in Path(first["work_dir"]).iterdir()}
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.stage, "allocate-private-directory")
        self.assertIsInstance(caught.exception.__cause__, FileExistsError)
        after = {path.name: path.read_bytes() for path in Path(first["work_dir"]).iterdir()}
        self.assertEqual(before, after)

    def test_missing_source_configuration_or_directory_never_builds(self):
        for env in ({}, {"NOVA_GUIDELINES_DIR": str(self.base / "missing")}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(g, "build_index") as build:
                with self.assertRaises(p.GuidelinePreparationError):
                    p.prepare_offline_guidelines(self.output)
                build.assert_not_called()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_output_root_is_not_created(self):
        missing = self.base / "missing-output"
        with self.assertRaises(p.GuidelinePreparationError) as caught:
            self.prepare(missing)
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        self.assertFalse(missing.exists())

    def test_unwritable_output_reports_original_error_without_fallback(self):
        with patch.object(p.os, "mkdir", side_effect=PermissionError("synthetic write denied")), \
                patch.object(g, "build_index") as build:
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self.assertIsNone(caught.exception.work_dir)
        self.assertEqual(list(self.output.iterdir()), [])
        build.assert_not_called()

    def test_read_only_source_is_not_modified(self):
        for path in self.source.rglob("*"):
            if path.is_file():
                path.chmod(0o400)
        self.source.chmod(0o500)
        try:
            result = self.prepare()
            self.assertEqual(result["aggregate"]["document_count"], 2)
            self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o500)
        finally:
            self.source.chmod(0o700)

    def test_source_and_output_symlinks_are_rejected(self):
        source_link = self.base / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        output_link = self.base / "output-link"
        output_link.symlink_to(self.output, target_is_directory=True)
        with patch.dict(os.environ, {"NOVA_GUIDELINES_DIR": str(source_link)}):
            with self.assertRaises(p.GuidelinePreparationError):
                p.prepare_offline_guidelines(self.output)
        with self.assertRaises(p.GuidelinePreparationError):
            self.prepare(output_link)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_noncanonical_output_and_in_corpus_root_rejected_before_allocation(self):
        inside = self.source / "scratch"
        inside.mkdir()
        for root in ("relative", str(self.output) + "/../private", str(self.output) + "/", inside):
            with self.subTest(root=root), self.assertRaises(p.GuidelinePreparationError):
                self.prepare(root)
        self.assertEqual(list(inside.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_normal_and_sticky_shared_output_roots_get_private_child(self):
        for mode in (0o755, 0o1777):
            with self.subTest(mode=mode):
                self.output.chmod(mode)
                result = self.prepare()
                self.assertEqual(stat.S_IMODE(Path(result["work_dir"]).stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), mode)

    def test_shared_writable_root_without_sticky_bit_rejected(self):
        self.output.chmod(0o777)
        with self.assertRaises(p.GuidelinePreparationError) as caught:
            self.prepare()
        self.assertIn("sticky bit", str(caught.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_build_failure_keeps_failed_receipt_and_original_exception(self):
        with patch.object(g, "build_index", side_effect=ValueError("synthetic invalid source")):
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        error = caught.exception
        self.assertEqual(error.stage, "build-index")
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertEqual(str(error.__cause__), "synthetic invalid source")
        receipt = json.loads(Path(error.evidence_path).read_text())
        self.assertEqual(receipt["stage"], "build-index")
        self.assertFalse((Path(error.work_dir) / "prepare-complete.json").exists())

    def test_returned_count_mismatch_is_rejected_after_status_validation(self):
        real_status = g.index_status
        def wrong_status(path):
            value = real_status(path)
            value["document_count"] += 1
            return value
        with patch.object(g, "index_status", side_effect=wrong_status):
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.stage, "verify-index")
        self.assertIn("aggregate mismatch", str(caught.exception))
        self.assertTrue((Path(caught.exception.work_dir) / "guidelines.sqlite").is_file())

    def test_failure_receipt_write_error_does_not_hide_build_cause(self):
        real_write = p._write_receipt
        def fail_failure_receipt(fd, name, value):
            if name == "prepare-failed.json":
                raise OSError("synthetic evidence disk full")
            return real_write(fd, name, value)
        with patch.object(g, "build_index", side_effect=ValueError("synthetic build failure")), \
                patch.object(p, "_write_receipt", side_effect=fail_failure_receipt):
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        self.assertEqual(str(caught.exception.__cause__), "synthetic build failure")
        self.assertIsNone(caught.exception.evidence_path)
        self.assertIn("disk full", caught.exception.evidence_write_error)

    def test_replaced_work_directory_fails_and_does_not_claim_false_evidence_path(self):
        real_build = g.build_index
        displaced = self.base / "displaced"
        def replace_after_build(source, database):
            built = real_build(source, database)
            database.parent.rename(displaced)
            database.parent.mkdir(mode=0o700)
            return built
        with patch.object(g, "build_index", side_effect=replace_after_build):
            with self.assertRaises(p.GuidelinePreparationError) as caught:
                self.prepare()
        self.assertIn("directory was replaced", str(caught.exception))
        self.assertIsNone(caught.exception.evidence_path)
        self.assertTrue((displaced / "prepare-failed.json").is_file())
        self.assertFalse((Path(caught.exception.work_dir) / "prepare-complete.json").exists())


if __name__ == "__main__":
    unittest.main()
