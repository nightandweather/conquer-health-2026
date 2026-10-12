"""A displayed review must belong to the exact displayed answer."""
import json
import unittest

from benchmarks.healthbench import canonical_hash
from local_tools.dialogue_server import visible_assessment


class AssessmentTests(unittest.TestCase):
    def test_only_matching_review_and_public_display_fields_are_returned(self):
        row = {'answer': 'Synthetic saved answer.'}
        value = {'answer_sha256': canonical_hash(row['answer']), 'status': 'reviewed',
                 'score': 50, 'note': 'Synthetic review note.', 'rubrics': 'hidden',
                 'canary': 'hidden', 'private_path': 'hidden', 'api_key': 'hidden'}
        self.assertEqual(visible_assessment(value, row), {
            'status': 'reviewed', 'score': 50, 'note': 'Synthetic review note.'})
        self.assertNotIn('hidden', json.dumps(visible_assessment(value, row)))
        self.assertIsNone(visible_assessment(value, {'answer': 'Different answer.'}))

    def test_unknown_or_invalid_score_cannot_be_displayed_as_a_grade(self):
        row = {'answer': 'Synthetic saved answer.'}
        base = {'answer_sha256': canonical_hash(row['answer']), 'status': 'reviewed'}
        for score in (True, -1, 101, float('nan'), float('inf'), '100'):
            with self.subTest(score_type=type(score).__name__):
                self.assertIsNone(visible_assessment(base | {'score': score}, row))
        self.assertIsNone(visible_assessment(base | {'status': 'pending', 'score': 100}, row))
        self.assertEqual(visible_assessment(base | {'status': 'uncertain', 'score': None}, row),
                         {'status': 'uncertain', 'score': None, 'note': ''})


if __name__ == '__main__':
    unittest.main()
