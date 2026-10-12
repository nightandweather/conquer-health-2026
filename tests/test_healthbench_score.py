"""Synthetic arithmetic and validation cases; no benchmark text or model calls."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from benchmarks.healthbench_score import build_summary


def example(prompt_id="p1", points=(2, 1, -3)):
    return {"prompt_id": prompt_id, "prompt": [{"role": "user", "content": "Synthetic input"}],
            "rubrics": [{"criterion": f"Synthetic condition {i}", "points": point}
                        for i, point in enumerate(points)]}


def answer(arm="A", prompt_id="p1", text="Synthetic output", status="completed"):
    return {"arm": arm, "prompt_id": prompt_id, "answer": text, "status": status}


def grades(arm="A", prompt_id="p1", labels=(True, False, True)):
    return [{"arm": arm, "prompt_id": prompt_id, "rubric_index": i, "criteria_met": label}
            for i, label in enumerate(labels)]


class HealthBenchScoreTests(unittest.TestCase):
    def test_positive_and_negative_criteria_keep_signed_points(self):
        arm = build_summary([example()], [answer()], grades())["arms"]["A"]
        self.assertAlmostEqual(arm["cases"][0]["score"], -1 / 3)
        self.assertEqual(arm["cases"][0]["positive_points_possible"], 3)
        self.assertAlmostEqual(arm["raw_mean"], -1 / 3)
        self.assertEqual(arm["score"], 0)

    def test_clip_after_mean_not_per_example(self):
        data = [example("p1", (1, -2)), example("p2", (1, -2))]
        answers = [answer(prompt_id=key) for key in ("p1", "p2")]
        judgments = grades(prompt_id="p1", labels=(True, True)) + grades(prompt_id="p2", labels=(True, False))
        arm = build_summary(data, answers, judgments)["arms"]["A"]
        self.assertEqual([row["score"] for row in arm["cases"]], [-1, 1])
        self.assertEqual(arm["raw_mean"], 0)
        self.assertEqual(arm["score"], 0)  # Per-example clipping would incorrectly give 0.5.
        self.assertEqual((arm["raw_min"], arm["raw_max"]), (-1, 1))

    def test_missing_positive_judgment_never_shrinks_denominator(self):
        arm = build_summary([example(points=(1, 9))], [answer()], grades(labels=(True,)))["arms"]["A"]
        self.assertEqual(arm["status"], "incomplete")
        self.assertIsNone(arm["score"])
        self.assertIsNone(arm["raw_mean"])
        self.assertEqual(arm["cases"][0]["positive_points_possible"], 10)
        self.assertEqual(arm["cases"][0]["missing_rubric_indices"], [1])
        self.assertIsNone(arm["cases"][0]["score"])

    def test_missing_negative_judgment_also_blocks_score(self):
        arm = build_summary([example(points=(1, -1))], [answer()], grades(labels=(True,)))["arms"]["A"]
        self.assertIsNone(arm["score"])

    def test_empty_failed_and_error_answers_are_known_zeroes(self):
        for text, status in [("   ", "completed"), ("", "empty"), ("", "failed"), ("", "error")]:
            with self.subTest(status=status):
                arm = build_summary([example()], [answer(text=text, status=status)], [])["arms"]["A"]
                self.assertEqual(arm["status"], "complete")
                self.assertEqual(arm["score"], 0)
                self.assertEqual(arm["known_inference_failure_count"], 1)
                self.assertEqual(arm["cases"][0]["positive_points_possible"], 3)

    def test_blocked_experiment_gets_no_performance_score(self):
        arm = build_summary([example()], [answer(text="", status="blocked")], [])["arms"]["A"]
        self.assertEqual(arm["status"], "blocked")
        self.assertIsNone(arm["score"])
        self.assertEqual(arm["blocked_count"], 1)
        self.assertEqual(arm["known_inference_failure_count"], 0)

    def test_truncated_answer_is_graded_as_saved(self):
        arm = build_summary([example(points=(1,))], [answer(status="truncated")], grades(labels=(True,)))["arms"]["A"]
        self.assertEqual(arm["score"], 1)
        self.assertEqual(arm["known_inference_failure_count"], 0)

    def test_duplicate_data_answers_and_judgments_are_rejected(self):
        for data, answers, judgments in [
            ([example(), example()], [answer()], grades()),
            ([example()], [answer(), answer()], grades()),
            ([example()], [answer()], grades() + grades()[:1]),
        ]:
            with self.subTest(data_count=len(data), answers_count=len(answers), judgments_count=len(judgments)):
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    build_summary(data, answers, judgments)

    def test_orphan_nonboolean_error_and_invalid_index_are_rejected(self):
        mutations = [
            {"prompt_id": "unknown"}, {"arm": "unknown"}, {"criteria_met": "false"},
            {"criteria_met": 1}, {"error": "synthetic grader failure"}, {"status": "failed"},
            {"status": {"unexpected": "object"}},
            {"rubric_index": True}, {"rubric_index": -1}, {"rubric_index": 3},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row = grades()[0] | mutation
                with self.assertRaises(ValueError):
                    build_summary([example()], [answer()], [row])

    def test_unknown_or_unhashable_answer_status_is_rejected(self):
        for status in ("unknown", {"unexpected": "object"}, None):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    build_summary([example()], [answer(status=status)], [])

    def test_two_arm_pairing_uses_unclipped_per_example_deltas(self):
        data = [example("p1", (1, -2)), example("p2", (1, -2))]
        answers = [answer(arm, key) for arm in ("A", "B") for key in ("p1", "p2")]
        judgments = (grades("A", "p1", (True, True)) + grades("A", "p2", (True, False))
                     + grades("B", "p1", (False, False)) + grades("B", "p2", (True, False)))
        pair = build_summary(data, answers, judgments)["pairs"][0]
        self.assertEqual(pair["status"], "complete")
        self.assertEqual((pair["wins"], pair["losses"], pair["ties"]), (0, 1, 1))
        self.assertEqual(pair["paired_delta"], -0.5)
        self.assertEqual([row["outcome"] for row in pair["cases"]], ["loss", "tie"])

    def test_manifest_and_union_detect_missing_arm_answers(self):
        data = [example("p1", (1,)), example("p2", (1,)), example("unused", (1,))]
        answers = [answer("A", "p1"), answer("A", "p2"), answer("B", "p1")]
        judgments = grades("A", "p1", (True,)) + grades("A", "p2", (True,)) + grades("B", "p1", (True,))
        summary = build_summary(data, answers, judgments)
        self.assertEqual(summary["selected_count"], 2)
        self.assertEqual(summary["dataset_count"], 3)
        self.assertEqual(summary["arms"]["B"]["missing_answer_count"], 1)
        self.assertIsNone(summary["pairs"][0]["paired_delta"])
        manifest = {"selected_prompt_ids": ["p1", "p2"], "arms": ["A", "B", "C"]}
        declared = build_summary(data, answers, judgments, manifest)
        self.assertEqual(declared["selection_source"], "generation_manifest")
        self.assertEqual(declared["arms"]["C"]["missing_answer_count"], 2)

    def test_generator_manifest_mapping_preserves_blocked_and_unattempted_arms(self):
        manifest = {"status": "blocked", "selected_prompt_ids": ["p1", "p2"],
                    "arms": {"raw_default": {"max_tokens": 2048}, "lunit_call": {"max_tokens": 6144}}}
        summary = build_summary([example("p1"), example("p2")],
                                [answer("raw_default", "p1", text="", status="blocked")], [], manifest)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["arms"]["raw_default"]["status"], "blocked")
        self.assertEqual(summary["arms"]["lunit_call"]["status"], "incomplete")
        for arm in summary["arms"].values():
            self.assertIsNone(arm["score"])
            self.assertIsNone(arm["raw_mean"])
            self.assertEqual(arm["known_inference_failure_count"], 0)
        self.assertIsNone(summary["pairs"][0]["paired_delta"])
        self.assertIsNone(summary["pairs"][0]["wins"])

    def test_output_contains_no_benchmark_or_completion_text(self):
        summary = build_summary([example()], [answer()], grades())
        serialized = json.dumps(summary)
        self.assertNotIn("Synthetic input", serialized)
        self.assertNotIn("Synthetic output", serialized)
        self.assertNotIn("Synthetic condition", serialized)
        self.assertFalse(summary["official_full_benchmark"])

    def test_cli_accepts_json_dataset_and_writes_summary(self):
        script = Path(__file__).resolve().parents[1] / "benchmarks/healthbench_score.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, answers, judgments, output = [root / name for name in ("data.json", "answers.jsonl", "judgments.jsonl", "out.json")]
            data.write_text(json.dumps([example(points=(1,))]), encoding="utf-8")
            answers.write_text(json.dumps(answer()) + "\n", encoding="utf-8")
            judgments.write_text(json.dumps(grades(labels=(True,))[0]) + "\n", encoding="utf-8")
            process = subprocess.run([sys.executable, "-B", str(script), "--data", str(data), "--answers", str(answers),
                                      "--judgments", str(judgments), "--out", str(output)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(output.read_text())["arms"]["A"]["score"], 1)


if __name__ == "__main__":
    unittest.main()
