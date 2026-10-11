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
        paths = [self.review / 'judgments.jsonl', self.review / 'score_summary.json',
                 self.review / 'review_export.json', self.run / 'assessments.json']
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths if path.exists()}

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
            self.assertEqual(set(assessment), {'prompt_id', 'arm', 'answer_sha256', 'status', 'score', 'note'})
        encoded = json.dumps(assessments)
        self.assertNotIn('criterion', encoded)
        self.assertNotIn('sources', encoded)
        for path in self.outputs():
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.review.stat().st_mode), 0o700)

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
        candidate['flags'] = [{'category': 'grounding', 'explanation': 'Unconfirmed detail.', 'sources': []}]
        candidate['note'] = 'x' * 900
        self.write_shard()
        self.export()
        flagged = [row for row in json.loads((self.run / 'assessments.json').read_text())['answers']
                   if row['status'] == 'needs_review']
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]['score'], 60)
        self.assertEqual(len(flagged[0]['note']), 500)

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
