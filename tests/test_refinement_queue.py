"""Sequential dependency gates and observed draft accounting, without inference."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import load_dataset
from scripts import run_refinement_after_baseline as queue


class QueueTests(unittest.TestCase):
    def setup_inputs(self, root):
        data = root / 'data.json'
        data.write_text(json.dumps([{'prompt_id': 'synthetic',
                                    'prompt': [{'role': 'user', 'content': 'Synthetic question'}],
                                    'rubrics': []}]))
        _, metadata = load_dataset(data, 20)
        baseline = {'status': 'running', 'dataset': metadata,
                    'selected_prompt_ids': ['synthetic'],
                    'config': {'model': 'gpt-oss:20b', 'base_url': 'http://127.0.0.1:11434/v1'},
                    'model': {'name': 'gpt-oss:20b'}, 'arms': {}}
        return data, baseline

    def test_failed_dependency_makes_no_generation_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, baseline = self.setup_inputs(root)
            manifest = root / 'manifest.json'
            out = root / 'queue'
            argv = ['queue', '--data', str(data), '--baseline-manifest', str(manifest), '--out', str(out)]
            stopped = dict(baseline, status='interrupted')
            with patch.object(queue, 'ROOT', root), patch('sys.argv', argv), \
                    patch.object(queue, 'read_manifest', side_effect=[baseline, baseline, stopped]), \
                    patch.object(queue.time, 'sleep'), patch.object(queue, 'run_refinement') as inference:
                # Use the configured private results boundary.
                argv[-1] = str(root / 'results' / 'queue')
                code = queue.main()
            inference.assert_not_called()
            self.assertEqual(code, 2)
            state = json.loads((root / 'results' / 'queue' / 'queue.json').read_text())
            self.assertEqual(state['reason'], 'baseline_interrupted')

    def test_changed_configuration_cannot_start_after_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, baseline = self.setup_inputs(root)
            changed = dict(baseline, status='completed', arms={'changed': {}})
            out = root / 'results' / 'queue'
            argv = ['queue', '--data', str(data), '--baseline-manifest', str(root / 'manifest.json'), '--out', str(out)]
            with patch.object(queue, 'ROOT', root), patch('sys.argv', argv), \
                    patch.object(queue, 'read_manifest', side_effect=[baseline, changed]), \
                    patch.object(queue, 'run_refinement') as inference:
                self.assertEqual(queue.main(), 2)
            inference.assert_not_called()
            self.assertEqual(json.loads((out / 'queue.json').read_text())['reason'], 'baseline_identity_changed')

    def test_extracted_control_keeps_only_actual_draft_cost_and_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            draft = {'stage': 'draft', 'number': 1, 'answer': 'Observed draft', 'status': 'completed',
                     'finish_reason': 'stop', 'error': None, 'max_tokens': 1536,
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}, 'seconds': 2}
            revision = dict(draft, stage='revision', number=3, answer='Observed revision', seconds=4)
            row = {'prompt_id': 'synthetic', 'arm': 'bounded_refine', 'attempts': [draft, revision],
                   'answer': 'Observed revision', 'calls': 2, 'usage': {'total_tokens': 60}, 'seconds': 6}
            (run / 'answers.jsonl').write_text(json.dumps(row) + '\n')
            queue.draft_controls(run)
            control = json.loads((run / 'draft_controls.jsonl').read_text())
            self.assertEqual(control['answer'], 'Observed draft')
            self.assertEqual(control['attempts'], [draft])
            self.assertEqual(control['usage'], draft['usage'])
            self.assertEqual((control['calls'], control['seconds']), (1, 2))
            self.assertEqual(control['arm'], 'draft_control')
            self.assertEqual((run / 'draft_controls.jsonl').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
