"""Synthetic review-export provenance, uncertainty and tamper rejection tests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import canonical_hash
from benchmarks.healthbench_review import create_review_packets
from benchmarks.healthbench_review_results import export_reviews
from local_tools.dialogue_server import visible_assessment


class ReviewResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch('benchmarks.healthbench_review.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.run = self.root / 'results' / 'run'
        self.run.mkdir(parents=True)
        self.data = self.root / 'data.json'
        self.answers = self.run / 'answers.jsonl'
        self.manifest = self.run / 'manifest.json'
        self.review = self.run / 'review'
        self.shard = self.review / 'supplied.json'
        self.items = [{'prompt_id': f'p{index}', 'prompt': [{'role': 'user', 'content': f'Synthetic {index}'}],
                       'rubrics': [{'criterion': 'Synthetic positive criterion', 'points': 5},
                                   {'criterion': 'Synthetic negative criterion', 'points': -2}],
                       'canary': 'PRIVATE_CANARY'} for index in range(2)]
        self.data.write_text(json.dumps(self.items))
        self.dataset_sha = hashlib.sha256(self.data.read_bytes()).hexdigest()
        self.rows = [{'prompt_id': item['prompt_id'], 'arm': arm, 'answer': f'Synthetic {arm} {index}',
                      'status': 'completed', 'dataset_sha256': self.dataset_sha,
                      'prompt_sha256': canonical_hash(item['prompt'])}
                     for index, item in enumerate(self.items) for arm in ('raw_default', 'grounded_call')]
        self.write_answers(self.rows)
        self.manifest_value = {'selected_prompt_ids': ['p0', 'p1'],
                               'selected_arms': ['raw_default', 'grounded_call'],
                               'arms': {'raw_default': {}, 'grounded_call': {}}, 'status': 'running',
                               'dataset': {'sha256': self.dataset_sha, 'selected_prompt_ids': ['p0', 'p1'],
                                           'selected_prompt_sha256': {item['prompt_id']: canonical_hash(item['prompt'])
                                                                      for item in self.items}}}
        self.manifest.write_text(json.dumps(self.manifest_value))
        self.make_packets()
        self.shard_value = self.make_shard()
        self.write_shard()

    def write_answers(self, rows):
        self.answers.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def make_packets(self):
        create_review_packets(self.data, self.answers, self.manifest, self.review)

    def make_shard(self):
        private = json.loads((self.review / 'private_manifest.json').read_text())
        reviewed = []
        for packet_id, entry in sorted(private['packets'].items()):
            raw = (self.review / 'packets' / entry['packet_filename']).read_bytes()
            reviewed.append({'packet_id': packet_id, 'packet_sha256': hashlib.sha256(raw).hexdigest(),
                             'candidates': {label: {'judgments': [
                                 {'rubric_index': 0, 'criteria_met': True, 'explanation': 'Synthetic positive finding',
                                  'sources': ['https://example.org/source']},
                                 {'rubric_index': 1, 'criteria_met': True, 'explanation': 'Synthetic negative finding',
                                  'sources': []}], 'flags': [], 'note': 'Synthetic reviewer note.'}
                                            for label in ('A', 'B')}})
        return {'reviewer': 'Codex', 'official': False, 'packets': reviewed}

    def write_shard(self):
        self.shard.write_text(json.dumps(self.shard_value))

    def export(self, shards=None):
        return export_reviews(self.data, self.answers, self.manifest, self.review, shards or [self.shard])

    def outputs(self):
        paths = [self.review / 'judgments.jsonl', self.review / 'score_summary.json', self.review / 'score_bounds.json',
                 self.review / 'review_export.json', self.run / 'assessments.json']
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths if path.exists()}

    def custom_pair(self):
        pair = ('grounded_call', 'codex_guided_v3')
        suffix = 'Synthetic general candidate instructions\n'
        self.manifest_value['selected_arms'] = list(pair)
        self.manifest_value['arms'] = {
            'grounded_call': {'suffix': 'Original baseline instructions', 'prompt_revision': 'grounded-v2'},
            'codex_guided_v3': {'suffix': suffix, 'prompt_revision': '3', 'max_tokens': 2048}}
        controls = {row['prompt_id']: row for row in self.rows if row['arm'] == pair[0]}
        self.rows = [row for item in self.items for row in (
            controls[item['prompt_id']], dict(controls[item['prompt_id']], arm=pair[1],
                answer='Synthetic new candidate', instructions_name=pair[1], instructions_version='3',
                instructions_byte_sha256=hashlib.sha256(suffix.encode()).hexdigest()))]
        self.manifest.write_text(json.dumps(self.manifest_value))
        self.write_answers(self.rows)
        self.review = self.run / 'custom-review'
        self.shard = self.review / 'supplied.json'
        create_review_packets(self.data, self.answers, self.manifest, self.review, paired_arms=pair)
        self.shard_value = self.make_shard()
        self.write_shard()
        return pair

    def test_custom_pair_exports_and_scores_original_arm_names(self):
        pair = self.custom_pair()
        private = json.loads((self.review / 'private_manifest.json').read_text())
        for packet in self.shard_value['packets']:
            mapping = private['packets'][packet['packet_id']]['candidate_map']
            label = next(label for label, arm in mapping.items() if arm == pair[1])
            for judgment in packet['candidates'][label]['judgments']:
                judgment['criteria_met'] = False
        self.write_shard()
        report = self.export()
        self.assertEqual(report['boolean_judgment_count'], 8)
        summary = json.loads((self.review / 'score_summary.json').read_text())
        self.assertEqual(set(summary['arms']), set(pair))
        self.assertEqual(summary['arms'][pair[0]]['score_percent'], 60)
        self.assertEqual(summary['arms'][pair[1]]['score_percent'], 0)
        judgments = [json.loads(line) for line in (self.review / 'judgments.jsonl').read_text().splitlines()]
        self.assertEqual({row['arm'] for row in judgments}, set(pair))
        assessments = json.loads((self.run / 'assessments.json').read_text())['answers']
        for assessment in assessments:
            source = next(row for row in self.rows if row['arm'] == assessment['arm']
                          and row['prompt_id'] == assessment['prompt_id'])
            self.assertEqual(assessment['answer_sha256'], canonical_hash(source['answer']))
        self.assertEqual(report['review_provenance']['generation_manifest_byte_sha256'],
                         hashlib.sha256(self.manifest.read_bytes()).hexdigest())

    def test_custom_pair_manifest_or_order_tampering_preserves_export(self):
        pair = self.custom_pair()
        self.export()
        before = self.outputs()
        path = self.review / 'private_manifest.json'
        original = json.loads(path.read_text())
        for declared in (list(reversed(pair)), [pair[0], 'fake_arm'], [pair[0], pair[0]],
                         [pair[0]], [pair[0], pair[1], 'extra'], ','.join(pair), None):
            with self.subTest(declared=declared):
                path.write_text(json.dumps(original | {'paired_arms': declared}))
                with self.assertRaises(ValueError):
                    self.export()
                self.assertEqual(self.outputs(), before)
        path.write_text(json.dumps(original))
        self.manifest_value['arms'][pair[1]]['max_tokens'] = 4096
        self.manifest.write_text(json.dumps(self.manifest_value))
        with self.assertRaisesRegex(ValueError, 'generation identity changed'):
            self.export()
        self.assertEqual(self.outputs(), before)

    def test_custom_pair_instruction_identity_and_arm_rename_are_rejected(self):
        self.custom_pair()
        self.export()
        before = self.outputs()
        original = deepcopy(self.rows)
        mutations = [lambda row: row.update(arm='raw_default'),
                     lambda row: row.update(instructions_name='grounded_call'),
                     lambda row: row.update(instructions_version='wrong'),
                     lambda row: row.update(instructions_byte_sha256='0' * 64),
                     lambda row: row.pop('instructions_version')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = deepcopy(original)
                mutation(rows[1])
                self.write_answers(rows)
                with self.assertRaises(ValueError):
                    self.export()
                self.assertEqual(self.outputs(), before)

    def test_weights_exact_answer_hash_and_private_viewer_shape(self):
        report = self.export()
        self.assertEqual(report['boolean_judgment_count'], 8)
        self.assertEqual(report['status'], 'complete')
        summary = json.loads((self.review / 'score_summary.json').read_text())
        self.assertEqual(summary['arms']['raw_default']['score_percent'], 60)
        self.assertFalse(summary['official_full_benchmark'])
        assessments = json.loads((self.run / 'assessments.json').read_text())['answers']
        self.assertEqual(len(assessments), 4)
        for assessment in assessments:
            source = next(row for row in self.rows if row['arm'] == assessment['arm']
                          and row['prompt_id'] == assessment['prompt_id'])
            self.assertEqual(assessment['answer_sha256'], canonical_hash(source['answer']))
            self.assertEqual(assessment['score'], 60)
            self.assertEqual(assessment['status'], 'reviewed')
            self.assertEqual(assessment['quality_status'], 'no_flags_recorded')
            self.assertEqual(set(assessment), {'prompt_id', 'arm', 'answer_sha256', 'status',
                                               'quality_status', 'score', 'note'})
        encoded = json.dumps(assessments)
        self.assertNotIn('criterion', encoded)
        self.assertNotIn('sources', encoded)
        for path in self.outputs():
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.review.stat().st_mode), 0o700)

    def test_known_failure_keeps_zero_utility_separate_from_rubric_labels(self):
        self.rows[0].update(status='failed', answer='', selected_attempt=1,
                            error={'code': 'empty_answer'},
                            attempts=[{'number': 1, 'status': 'failed', 'answer': '',
                                       'error': {'code': 'empty_answer'}}])
        self.write_answers(self.rows)
        self.manifest_value.update(status='partial', finished_at='2026-01-01T00:00:00Z')
        self.manifest.write_text(json.dumps(self.manifest_value))
        self.review = self.run / 'failure-review'
        self.shard = self.review / 'supplied.json'
        create_review_packets(self.data, self.answers, self.manifest, self.review,
                              include_known_inference_failures=True)
        self.shard_value = self.make_shard()
        private = json.loads((self.review / 'private_manifest.json').read_text())
        for packet in self.shard_value['packets']:
            entry = private['packets'][packet['packet_id']]
            if entry['prompt_id'] == 'p0':
                label = next(label for label, arm in entry['candidate_map'].items() if arm == 'raw_default')
                for judgment in packet['candidates'][label]['judgments']:
                    judgment['criteria_met'] = None
        self.write_shard()
        report = self.export()
        self.assertEqual(report['boolean_judgment_count'], 6)
        self.assertEqual(report['unresolved_judgment_count'], 0)
        self.assertEqual(report['skipped_failure_rubric_count'], 2)
        self.assertEqual(report['known_inference_failure_count'], 1)
        summary = json.loads((self.review / 'score_summary.json').read_text())
        self.assertEqual(summary['arms']['raw_default']['score_percent'], 30)
        assessment = next(row for row in json.loads((self.run / 'assessments.json').read_text())['answers']
                          if row['arm'] == 'raw_default' and row['prompt_id'] == 'p0')
        self.assertEqual(assessment['score'], 0)
        self.assertEqual(assessment['quality_status'], 'inference_failure')
        visible = visible_assessment(assessment, self.rows[0])
        self.assertEqual(visible['status'], 'reviewed')
        self.assertEqual(visible['score'], 0)
        self.assertIn('생성 실패', visible['note'])
        before = self.outputs()
        self.rows[0]['error'] = {'code': 'different_failure'}
        self.rows[0]['attempts'][0]['error'] = self.rows[0]['error']
        self.write_answers(self.rows)
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.export()
        self.assertEqual(self.outputs(), before)
        for status in ('error', 'empty', 'completed', 'truncated'):
            with self.subTest(status=status):
                self.rows[0]['status'] = status
                self.write_answers(self.rows)
                with self.assertRaisesRegex(ValueError, 'recorded failed row'):
                    self.export()
                self.assertEqual(self.outputs(), before)

    def test_null_is_omitted_not_false_and_blocks_aggregate(self):
        self.shard_value['packets'][0]['candidates']['A']['judgments'][0]['criteria_met'] = None
        self.write_shard()
        report = self.export()
        self.assertEqual(report['unresolved_judgment_count'], 1)
        judgments = [json.loads(line) for line in (self.review / 'judgments.jsonl').read_text().splitlines()]
        self.assertEqual(len(judgments), 7)
        self.assertTrue(all(isinstance(row['criteria_met'], bool) for row in judgments))
        uncertain = [row for row in json.loads((self.run / 'assessments.json').read_text())['answers']
                     if row['status'] == 'uncertain']
        self.assertEqual(len(uncertain), 1)
        self.assertIsNone(uncertain[0]['score'])
        self.assertEqual(uncertain[0]['quality_status'], 'uncertain')
        summary = json.loads((self.review / 'score_summary.json').read_text())
        self.assertIsNone(summary['arms'][uncertain[0]['arm']]['score'])
        self.assertEqual(summary['status'], 'incomplete')

    def test_partial_supplied_review_keeps_full_manifest_denominator(self):
        self.shard_value['packets'] = self.shard_value['packets'][:1]
        self.write_shard()
        report = self.export()
        self.assertEqual(report['selected_count'], 2)
        summary = json.loads((self.review / 'score_summary.json').read_text())
        for arm in summary['arms'].values():
            self.assertEqual(arm['scored_count'], 1)
            self.assertEqual(arm['selected_count'], 2)
            self.assertIsNone(arm['score'])

    def test_flags_need_review_and_note_is_bounded(self):
        candidate = self.shard_value['packets'][0]['candidates']['A']
        candidate['flags'] = [
            {'category': 'grounding', 'explanation': 'y' * 800, 'sources': []},
            {'category': 'safety', 'explanation': 'Critical synthetic warning.', 'sources': []}]
        candidate['note'] = 'x' * 900
        self.write_shard()
        self.export()
        flagged = [row for row in json.loads((self.run / 'assessments.json').read_text())['answers']
                   if row['quality_status'] == 'needs_review']
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]['status'], 'reviewed')
        self.assertEqual(flagged[0]['score'], 60)
        self.assertEqual(len(flagged[0]['note']), 500)
        self.assertTrue(flagged[0]['note'].startswith('추가 검토 필요: '))
        self.assertIn('Critical synthetic warning.', flagged[0]['note'])

    def test_running_viewer_displays_completed_grade_with_flags_and_uncertainty(self):
        completed = self.shard_value['packets'][0]['candidates']['A']
        completed['flags'] = [{'category': 'rubric_issue', 'explanation': 'Needs independent review.', 'sources': []}]
        completed['note'] = 'The supplied reviewer note remains visible.'
        uncertain = self.shard_value['packets'][0]['candidates']['B']
        uncertain['judgments'][0]['criteria_met'] = None
        uncertain['flags'] = [{'category': 'grounding', 'explanation': 'Still unresolved.', 'sources': []}]
        self.write_shard()
        self.export()
        assessments = json.loads((self.run / 'assessments.json').read_text())['answers']
        for quality in ('needs_review', 'uncertain'):
            assessment = next(row for row in assessments if row['quality_status'] == quality)
            answer = next(row for row in self.rows if row['arm'] == assessment['arm']
                          and row['prompt_id'] == assessment['prompt_id'])
            visible = visible_assessment(assessment, answer)
            self.assertIsNotNone(visible)
            self.assertTrue(visible['note'].startswith('추가 검토 필요: '))
            self.assertNotIn('quality_status', visible)
            if quality == 'needs_review':
                self.assertEqual(visible['status'], 'reviewed')
                self.assertEqual(visible['score'], 60)
                self.assertIn(completed['note'], visible['note'])
            else:
                self.assertEqual(visible['status'], 'uncertain')
                self.assertIsNone(visible['score'])

    def test_invalid_shard_preserves_all_existing_output_bytes_and_times(self):
        self.export()
        before = self.outputs()
        original = deepcopy(self.shard_value)
        mutations = [
            lambda value: value.update(official=0),
            lambda value: value.update(reviewer='Another model'),
            lambda value: value['packets'][0].update(packet_sha256='0' * 64),
            lambda value: value['packets'][0].update(packet_id='unknown'),
            lambda value: value['packets'][0]['candidates'].update(C=value['packets'][0]['candidates']['A']),
            lambda value: value['packets'][0]['candidates']['A']['judgments'][0].update(criteria_met=1),
            lambda value: value['packets'][0]['candidates']['A']['judgments'].pop(),
            lambda value: value['packets'][0]['candidates']['A']['judgments'].append(
                deepcopy(value['packets'][0]['candidates']['A']['judgments'][0]))]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.shard_value = deepcopy(original)
                mutation(self.shard_value)
                self.write_shard()
                with self.assertRaises(ValueError):
                    self.export()
                self.assertEqual(self.outputs(), before)

    def test_duplicate_packet_across_shards_or_paths_is_rejected(self):
        other = self.review / 'other.json'
        other.write_bytes(self.shard.read_bytes())
        for supplied in ([self.shard, other], [self.shard, self.shard]):
            with self.subTest(paths=supplied):
                with self.assertRaisesRegex(ValueError, 'Duplicate'):
                    self.export(supplied)
                self.assertEqual(self.outputs(), {})

    def test_packet_private_mapping_and_source_answer_tampering_is_rejected(self):
        for target in ('packet', 'private', 'answer'):
            with self.subTest(target=target):
                private_path = self.review / 'private_manifest.json'
                private_raw = private_path.read_bytes()
                private = json.loads(private_raw)
                entry = next(iter(private['packets'].values()))
                packet_path = self.review / 'packets' / entry['packet_filename']
                packet_raw, answer_raw = packet_path.read_bytes(), self.answers.read_bytes()
                if target == 'packet':
                    packet = json.loads(packet_raw)
                    packet['candidates']['A']['answer'] = 'Tampered answer'
                    packet_path.write_text(json.dumps(packet))
                elif target == 'private':
                    entry['candidate_map'] = {'A': 'raw_default', 'B': 'raw_default'}
                    private_path.write_text(json.dumps(private))
                else:
                    changed = deepcopy(self.rows)
                    changed[0]['answer'] = 'Changed source answer'
                    self.write_answers(changed)
                with self.assertRaises(ValueError):
                    self.export()
                self.assertEqual(self.outputs(), {})
                private_path.write_bytes(private_raw)
                packet_path.write_bytes(packet_raw)
                self.answers.write_bytes(answer_raw)

    def test_whitespace_packet_change_requires_new_byte_hash_without_canonical_change(self):
        private = json.loads((self.review / 'private_manifest.json').read_text())
        packet_id = self.shard_value['packets'][0]['packet_id']
        path = self.review / 'packets' / private['packets'][packet_id]['packet_filename']
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'byte hash'):
            self.export()
        self.assertEqual(self.outputs(), {})

    def test_one_answers_snapshot_is_preserved_during_concurrent_append(self):
        self.write_answers(self.rows[:2])
        # Remove packets that are no longer in this synthetic snapshot and regenerate.
        for path in (self.review / 'packets').iterdir():
            path.unlink()
        (self.review / 'private_manifest.json').unlink()
        self.make_packets()
        self.shard_value = self.make_shard()
        self.write_shard()
        before = self.answers.read_bytes()
        original_read = Path.read_bytes
        reads = []
        def capture(path):
            raw = original_read(path)
            if path == self.answers:
                reads.append(raw)
                self.write_answers(self.rows)
            return raw
        with patch.object(Path, 'read_bytes', capture):
            report = self.export()
        self.assertEqual(len(reads), 1)
        self.assertEqual(report['review_provenance']['answers_snapshot_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(report['assessment_count'], 2)
        self.assertEqual(report['status'], 'incomplete')

    def test_unlisted_shards_are_not_loaded(self):
        (self.review / 'broken-unlisted.json').write_text('invalid')
        self.assertEqual(self.export()['reviewed_packet_count'], 2)

    def test_output_symlink_is_rejected_without_changing_target(self):
        outside = self.root / 'outside.json'
        outside.write_text('untouched')
        (self.run / 'assessments.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'without symlinks'):
            self.export()
        self.assertEqual(outside.read_text(), 'untouched')
        self.assertFalse((self.review / 'judgments.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
