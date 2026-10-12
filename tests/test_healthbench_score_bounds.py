"""Signed weighting, unresolved labels and unavailable answer bounds tests."""

import unittest

from benchmarks.healthbench_score import build_summary
from benchmarks.healthbench_score_bounds import build_score_bounds


class ScoreBoundsTests(unittest.TestCase):
    def setUp(self):
        self.data = [{'prompt_id': name, 'prompt': [{'role': 'user', 'content': 'Synthetic'}],
                      'rubrics': [{'criterion': 'Positive', 'points': 5},
                                  {'criterion': 'Negative', 'points': -10}]}
                     for name in ('p0', 'p1')]
        self.answers = [{'arm': 'a', 'prompt_id': name, 'status': 'completed', 'answer': 'Synthetic'}
                        for name in ('p0', 'p1')]
        self.manifest = {'selected_prompt_ids': ['p0', 'p1'], 'arms': ['a']}

    def bounds(self, judgments, answers=None):
        return build_score_bounds(self.data, self.answers if answers is None else answers,
                                  judgments, self.manifest)['arms']['a']

    def judgment(self, name, number, value):
        return {'arm': 'a', 'prompt_id': name, 'rubric_index': number, 'criteria_met': value}

    def test_signed_unknown_bounds_preserve_missing_labels(self):
        bounds = self.bounds([self.judgment('p0', 0, True)])
        self.assertEqual(bounds['unresolved_criterion_count'], 3)
        self.assertEqual(bounds['lower_raw_mean'], -1.5)
        self.assertEqual(bounds['upper_raw_mean'], 1)
        self.assertEqual(bounds['lower_score_percent'], 0)
        self.assertEqual(bounds['upper_score_percent'], 100)

    def test_clip_after_mean_and_exact_matches_existing_scorer(self):
        judgments = [self.judgment('p0', 0, True), self.judgment('p0', 1, False),
                     self.judgment('p1', 0, False), self.judgment('p1', 1, True)]
        bounds = self.bounds(judgments)
        measured = build_summary(self.data, self.answers, judgments, self.manifest)['arms']['a']
        self.assertEqual(bounds['status'], 'exact')
        self.assertEqual(bounds['lower_raw_mean'], -.5)
        self.assertEqual(bounds['upper_raw_mean'], measured['raw_mean'])
        self.assertEqual(bounds['lower_score_percent'], measured['score_percent'])
        self.assertEqual(bounds['upper_score_percent'], 0)

    def test_known_failure_zero_without_any_criterion_labels(self):
        answers = [dict(row, status='failed', answer='') for row in self.answers]
        bounds = self.bounds([], answers)
        self.assertEqual(bounds['status'], 'exact')
        self.assertEqual(bounds['known_inference_failure_count'], 2)
        self.assertEqual(bounds['unresolved_criterion_count'], 0)
        self.assertEqual(bounds['upper_score_percent'], 0)

    def test_missing_or_blocked_answer_prevents_bounds(self):
        for answers in (self.answers[:1], [self.answers[0], dict(self.answers[1], status='blocked', answer='')]):
            with self.subTest(answers=answers):
                bounds = self.bounds([], answers)
                self.assertEqual(bounds['status'], 'unavailable')
                self.assertIsNone(bounds['lower_score_percent'])
                self.assertIsNone(bounds['upper_score_percent'])

    def test_null_and_duplicate_labels_rejected_instead_of_coerced(self):
        for judgments in ([self.judgment('p0', 0, None)],
                          [self.judgment('p0', 0, True)] * 2):
            with self.subTest(judgments=judgments), self.assertRaises(ValueError):
                self.bounds(judgments)
