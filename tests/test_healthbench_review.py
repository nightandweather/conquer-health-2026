"""Synthetic review-packet isolation, pairing, provenance and immutability tests."""

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import canonical_hash
from benchmarks.healthbench_review import create_review_packets


class HealthBenchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.patch = patch("benchmarks.healthbench_review.ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.data = self.root / "data.json"
        self.answers = self.root / "answers.jsonl"
        self.manifest = self.root / "manifest.json"
        self.out = self.root / "results" / "review"
        self.examples = [{"prompt_id": prompt_id,
                          "prompt": [{"role": "assistant", "content": "Earlier synthetic statement"},
                                     {"role": "user", "content": f"Synthetic question {index}"}],
                          "rubrics": [{"criterion": "Synthetic positive condition", "points": 5,
                                       "references": "PRIVATE_REFERENCE"},
                                      {"criterion": "Synthetic harmful condition", "points": -2}],
                          "canary": "PRIVATE_CANARY", "references": "PRIVATE_REFERENCE"}
                         for index, prompt_id in enumerate(("p0", "p1", "p2", "p3", "p4"))]
        self.data.write_text(json.dumps(self.examples), encoding="utf-8")
        self.dataset_sha = hashlib.sha256(self.data.read_bytes()).hexdigest()
        self.manifest_value = {
            "selected_prompt_ids": [row["prompt_id"] for row in reversed(self.examples)],
            "selected_arms": ["raw_default", "grounded_call"],
            "dataset": {"sha256": self.dataset_sha,
                        "selected_prompt_sha256": {row["prompt_id"]: canonical_hash(row["prompt"])
                                                   for row in self.examples}},
            "arms": {"raw_default": {}, "grounded_call": {}}, "status": "running"}
        self.manifest.write_text(json.dumps(self.manifest_value), encoding="utf-8")
        self.rows = [self.answer(row, arm) for row in self.examples for arm in ("raw_default", "grounded_call")]
        self.write_answers(self.rows)

    def answer(self, row, arm, **changes):
        return {"prompt_id": row["prompt_id"], "arm": arm,
                "dataset_sha256": self.dataset_sha, "prompt_sha256": canonical_hash(row["prompt"]),
                "answer": "Synthetic first candidate" if arm == "raw_default" else "Synthetic second candidate",
                "status": "completed", "seconds": 111, "usage": {"total_tokens": 999},
                "model": "PRIVATE_MODEL", "canary": "PRIVATE_CANARY", "references": "PRIVATE_REFERENCE"} | changes

    def write_answers(self, rows):
        self.answers.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def run_packets(self):
        return create_review_packets(self.data, self.answers, self.manifest, self.out)

    def private(self):
        return json.loads((self.out / "private_manifest.json").read_text())

    def snapshot(self):
        return {str(path.relative_to(self.out)): path.read_bytes() for path in self.out.rglob("*") if path.is_file()}

    def test_isolation_preserves_actual_history_rubrics_and_blinded_answers(self):
        result = self.run_packets()
        self.assertEqual((result["ready_packet_count"], result["expected_answer_count"]), (5, 10))
        private = self.private()
        entry = next(value for value in private["packets"].values() if value["prompt_id"] == "p0")
        raw = (self.out / "packets" / entry["packet_filename"]).read_text()
        packet = json.loads(raw)
        self.assertEqual(packet["prompt"], self.examples[0]["prompt"])
        self.assertEqual(packet["rubrics"], [
            {"rubric_index": 0, "criterion": "Synthetic positive condition", "points": 5},
            {"rubric_index": 1, "criterion": "Synthetic harmful condition", "points": -2}])
        self.assertEqual(set(packet["candidates"]), {"A", "B"})
        for excluded in ("raw_default", "grounded_call", "PRIVATE_CANARY", "PRIVATE_REFERENCE",
                         "PRIVATE_MODEL", "seconds", "total_tokens", "criteria_met", "score"):
            self.assertNotIn(excluded, raw)
        for label, arm in entry["candidate_map"].items():
            source = next(row for row in self.rows if row["prompt_id"] == "p0" and row["arm"] == arm)
            self.assertEqual(packet["candidates"][label]["answer"], source["answer"])
        content = {key: value for key, value in packet.items() if key != "content_sha256"}
        self.assertEqual(packet["content_sha256"], canonical_hash(content))

    def test_blinding_and_assignment_are_deterministic_despite_row_order(self):
        self.run_packets()
        first = self.private()
        self.write_answers(list(reversed(self.rows)))
        self.manifest_value["selected_prompt_ids"].reverse()
        self.manifest_value["status"] = "completed"
        self.manifest.write_text(json.dumps(self.manifest_value), encoding="utf-8")
        second = self.run_packets()
        self.assertEqual(second["new_packet_count"], 0)
        self.assertEqual(first, self.private())
        entries = sorted(first["packets"].values(), key=lambda entry: entry["prompt_id"])
        self.assertEqual([entry["reviewer_index"] for entry in entries], [0, 1, 2, 3, 0])

    def test_incremental_snapshots_add_pairs_without_rewriting_old_packet_or_provenance(self):
        self.write_answers(self.rows[:2])
        first_sha = hashlib.sha256(self.answers.read_bytes()).hexdigest()
        first = self.run_packets()
        self.assertEqual((first["ready_packet_count"], first["pending_count"]), (1, 4))
        old_private = self.private()
        old_entry = next(iter(old_private["packets"].values()))
        old_file = self.out / "packets" / old_entry["packet_filename"]
        old_raw, old_mtime = old_file.read_bytes(), old_file.stat().st_mtime_ns
        self.write_answers(self.rows)
        second = self.run_packets()
        self.assertEqual((second["new_packet_count"], second["pending_count"]), (4, 0))
        self.assertEqual(self.private()["packets"][next(iter(old_private["packets"]))], old_entry)
        self.assertEqual(old_entry["first_source_answers_sha256"], first_sha)
        self.assertEqual((old_file.read_bytes(), old_file.stat().st_mtime_ns), (old_raw, old_mtime))

    def test_changed_existing_candidate_refuses_every_write_including_new_pair(self):
        self.write_answers(self.rows[:2])
        self.run_packets()
        before = self.snapshot()
        changed = [row.copy() for row in self.rows]
        changed[0]["answer"] = "Changed answer"
        self.write_answers(changed)
        with self.assertRaisesRegex(ValueError, "candidate or mapping changed"):
            self.run_packets()
        self.assertEqual(self.snapshot(), before)

    def test_changed_packet_and_missing_previously_packaged_pair_refuse_writes(self):
        self.run_packets()
        self.write_answers(self.rows[2:])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "no longer reviewable"):
            self.run_packets()
        self.assertEqual(self.snapshot(), before)
        self.write_answers(self.rows)
        path = next((self.out / "packets").iterdir())
        packet = json.loads(path.read_text())
        packet["candidates"]["A"]["answer"] = "Tampered packet"
        path.write_text(json.dumps(packet), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "review file is missing or changed"):
            self.run_packets()
        self.assertEqual(self.snapshot(), before)

    def test_first_snapshot_metadata_mutation_is_rejected(self):
        self.run_packets()
        private = self.private()
        entry = next(iter(private["packets"].values()))
        entry["first_source_answers_sha256"] = "0" * 64
        (self.out / "private_manifest.json").write_text(json.dumps(private), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "private packet metadata changed"):
            self.run_packets()
        self.assertEqual(self.snapshot(), before)

    def test_failed_empty_missing_are_pending_but_nonempty_truncation_is_packaged(self):
        rows = [row.copy() for row in self.rows[:8]]
        rows[1].update(status="failed", answer=None)
        rows[3]["answer"] = "   "
        rows[5]["status"] = "truncated"
        rows[7]["status"] = "blocked"
        rows[7]["answer"] = ""
        self.write_answers(rows)
        result = self.run_packets()
        self.assertEqual((result["ready_packet_count"], result["pending_count"]), (1, 4))
        self.assertEqual(next(iter(self.private()["packets"].values()))["reviewer_index"], 2)
        self.assertEqual(next(iter(self.private()["packets"].values()))["prompt_id"], "p2")
        self.assertEqual(result["grading_status"], "not_graded")

    def test_dataset_prompt_sha_arm_selection_and_duplicates_are_validated_before_writes(self):
        for mutation in ({"dataset_sha256": "0" * 64}, {"prompt_sha256": "0" * 64},
                         {"prompt_id": "unknown"}, {"arm": "lunit_call"}):
            with self.subTest(mutation=mutation):
                self.write_answers([self.rows[0] | mutation] + self.rows[1:])
                with self.assertRaises(ValueError):
                    self.run_packets()
                self.assertFalse(self.out.exists())
        self.write_answers(self.rows + self.rows[:1])
        with self.assertRaisesRegex(ValueError, "Duplicate answer"):
            self.run_packets()
        self.assertFalse(self.out.exists())

    def test_manifest_sha_prompt_sha_and_extra_declared_arm_are_rejected(self):
        for mutation in (
            {"dataset": self.manifest_value["dataset"] | {"sha256": "0" * 64}},
            {"dataset": self.manifest_value["dataset"] | {"selected_prompt_sha256": {}}},
            {"arms": self.manifest_value["arms"] | {"lunit_call": {}}},
            {"selected_arms": [{}, "grounded_call"]},
            {"dataset": self.manifest_value["dataset"] | {"selected_prompt_ids": [{}, "p0"]}},
        ):
            with self.subTest(mutation=mutation):
                self.manifest.write_text(json.dumps(self.manifest_value | mutation), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_packets()
                self.assertFalse(self.out.exists())

    def test_hash_filenames_are_safe_for_hostile_ids_and_files_are_private(self):
        hostile = "../../unwanted"
        self.examples[0]["prompt_id"] = hostile
        self.data.write_text(json.dumps(self.examples), encoding="utf-8")
        self.dataset_sha = hashlib.sha256(self.data.read_bytes()).hexdigest()
        self.manifest_value["selected_prompt_ids"] = [row["prompt_id"] for row in self.examples]
        self.manifest_value["dataset"] = {
            "sha256": self.dataset_sha,
            "selected_prompt_sha256": {row["prompt_id"]: canonical_hash(row["prompt"]) for row in self.examples}}
        self.manifest.write_text(json.dumps(self.manifest_value), encoding="utf-8")
        self.write_answers([self.answer(row, arm) for row in self.examples for arm in ("raw_default", "grounded_call")])
        self.run_packets()
        for path in self.out.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            if path.parent.name == "packets":
                self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
        self.assertFalse((self.root / "unwanted").exists())

    def test_outside_output_and_symlink_packet_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "private results"):
            create_review_packets(self.data, self.answers, self.manifest, self.root / "public")
        self.run_packets()
        path = next((self.out / "packets").iterdir())
        path.unlink()
        path.symlink_to(self.data)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.run_packets()
        self.assertEqual(self.snapshot(), before)

    def test_direct_cli_supports_imports_and_refuses_public_output(self):
        script = Path(__file__).resolve().parents[1] / "benchmarks" / "healthbench_review.py"
        process = subprocess.run([sys.executable, "-B", str(script), "--data", str(self.data),
                                  "--answers", str(self.answers), "--manifest", str(self.manifest),
                                  "--out", str(self.root / "public")], capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn("private results/ directory", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
