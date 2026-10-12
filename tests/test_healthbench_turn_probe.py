"""Synthetic turn-by-turn consultation checks; no Git, HTTP or real cases.

Production dataset pins are patched only inside disposable fixtures. Every
question, rubric, answer and patient quote below is invented for these checks.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import healthbench_turn_probe as probe
from benchmarks.healthbench import DEFAULT_MODEL, GROUNDED_SUFFIX, canonical_hash


REAL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = 'SYNTHETIC_TURN_PROBE_TEMPLATE\n{{ messages }}\n '


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def response(answer='Synthetic doctor asks a relevant question.', finish='stop', usage=True):
    value = {'choices': [{'message': {'content': answer,
                                     'reasoning': 'HIDDEN_REASONING_MUST_NOT_BE_SAVED'},
                          'finish_reason': finish}]}
    if usage:
        value['usage'] = {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}
    return value


class FakeTransport:
    """Capture literal requests and permit deterministic pre/post pin failures."""
    def __init__(self, digest, responses=None, *, on_metadata=None, on_template=None, on_complete=None):
        self.digest = digest
        self.responses = list(responses if responses is not None else [response()] * 20)
        self.payloads, self.metadata_calls, self.template_queries = [], 0, 0
        self.template_requests = []
        self.metadata_result = None
        self.template_result = {'template': TEMPLATE}
        self.on_metadata, self.on_template, self.on_complete = on_metadata, on_template, on_complete

    def model_metadata(self, model):
        self.metadata_calls += 1
        if self.on_metadata:
            self.on_metadata(self)
        if isinstance(self.metadata_result, BaseException):
            raise self.metadata_result
        if self.metadata_result is not None:
            return deepcopy(self.metadata_result)
        return {'name': model, 'digest': self.digest, 'metadata_status': 'available'}

    def request(self, method, url, body, *, authenticated):
        self.template_queries += 1
        self.template_requests.append((method, url, deepcopy(body), authenticated))
        if self.on_template:
            self.on_template(self)
        if isinstance(self.template_result, BaseException):
            raise self.template_result
        return deepcopy(self.template_result)

    def complete(self, payload):
        self.payloads.append(deepcopy(payload))
        if self.on_complete:
            self.on_complete(self)
        value = self.responses[len(self.payloads) - 1]
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)


class ProductionTurnProbePinTests(unittest.TestCase):
    def test_production_scope_and_pins_are_fixed(self):
        self.assertEqual(probe.CASE_IDS, ('HB-V0000', 'HB-V0004', 'HB-V0015', 'HB-V0016'))
        self.assertEqual(probe.DATASET_SHA256,
                         'cd3516c601da6f67fc7b6db3439efdf2659a91534c09c6bc1c5d363889ed4033')
        self.assertEqual(probe.GROUNDED_SHA256, sha(GROUNDED_SUFFIX.encode('utf-8')))
        self.assertEqual(probe.MODEL_DIGEST,
                         'f38aa0c53da5f8c49d08c43a99df24ff53167fe68e24664a7777288e7656fdfe')
        self.assertEqual(probe.TEMPLATE_SHA256,
                         'fa6710a93d78da62641e192361344be7a8c0a1c3737f139cf89f20ce1626b99c')


class TurnProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / 'private_data' / 'synthetic_roles.json'
        self.plan_path = self.root / 'results' / 'synthetic-plan.json'
        self.out = self.root / 'results' / 'synthetic-turn-probe'
        self.data.parent.mkdir(parents=True)
        self.plan_path.parent.mkdir(parents=True)
        self.items = []
        for number in range(200):
            messages = [{'role': 'user', 'content':
                         f'Synthetic original user {number}: I saw a small mark yesterday.\n It is unchanged today. ' }]
            if number == 0:
                messages.extend([
                    {'role': 'assistant', 'content': 'ASSISTANT_ONLY_UNCONFIRMED_FACT'},
                    {'role': 'user', 'content': 'Synthetic follow-up: no additional details were provided.'}])
            if number >= 20:
                messages = [{'role': 'user', 'content': 'HOLDOUT_PROMPT_MUST_NOT_ENTER_MODEL'}]
            self.items.append({'prompt_id': f'HB-V{number:04d}', 'prompt': messages,
                               'rubrics': [{'criterion': 'RUBRIC_SENTINEL_MUST_NOT_ENTER_MODEL', 'points': 1}],
                               'reference': 'REFERENCE_SENTINEL_MUST_NOT_ENTER_MODEL',
                               'canary': 'CANARY_SENTINEL_MUST_NOT_ENTER_MODEL'})
        self.write(self.data, self.items)
        self.execution = self.root / 'benchmarks' / 'healthbench_turn_probe.py'
        self.execution.parent.mkdir(parents=True)
        self.execution.write_bytes(Path(probe.__file__).read_bytes())
        for relative in probe.SOURCE_MODULES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REAL_ROOT / relative).read_bytes())
        self.plan = {'schema_version': 1, 'data_path': str(self.data),
                     'dataset_sha256': sha(self.data.read_bytes()),
                     'grounded_suffix_sha256': sha(GROUNDED_SUFFIX.encode('utf-8')),
                     'model_digest': probe.MODEL_DIGEST, 'template_sha256': sha(TEMPLATE.encode('utf-8')),
                     'execution_module_sha256': sha(self.execution.read_bytes()),
                     'source_module_sha256': {relative: sha((self.root / relative).read_bytes())
                                              for relative in probe.SOURCE_MODULES},
                     'case_ids': list(probe.CASE_IDS)}
        self.write(self.plan_path, self.plan)
        for mocked in (patch.object(probe, 'ROOT', self.root),
                       patch.object(probe, 'EXECUTION_PATH', self.execution),
                       patch.object(probe, 'DATASET_SHA256', self.plan['dataset_sha256']),
                       patch.object(probe, 'TEMPLATE_SHA256', self.plan['template_sha256']),
                       patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No HTTP permitted'))):
            mocked.start()
            self.addCleanup(mocked.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def prepare(self):
        return probe.prepare(self.plan_path, self.out)

    def patient(self, case_id, *, utterance=None, quotes=None, synthetic=False, **overrides):
        item = self.items[int(case_id.removeprefix('HB-V'))]
        quote = item['prompt'][0]['content']
        value = {'utterance': utterance if utterance is not None else quote,
                 'source_quotes': quotes if quotes is not None else [quote],
                 'synthetic_question': synthetic, 'no_new_facts': True, 'validated_by': 'root'}
        value.update(overrides)
        path = self.root / 'results' / f'patient-{case_id}.json'
        self.write(path, value)
        return path

    def row(self, case_id='HB-V0000', turn=1):
        return json.loads((self.out / 'cases' / case_id / f'turn-{turn:02d}.json').read_bytes())

    def test_prepare_is_zero_call_private_and_existing_directory_is_never_overwritten(self):
        with patch.object(probe, 'Transport', side_effect=AssertionError('No transport at preparation')):
            summary = self.prepare()
        self.assertEqual(summary['status'], 'prepared')
        self.assertEqual((summary['case_count'], summary['maximum_generation_calls']), (4, 20))
        before = {path.relative_to(self.out): path.read_bytes() for path in self.out.rglob('*') if path.is_file()}
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            self.prepare()
        self.assertEqual(before, {path.relative_to(self.out): path.read_bytes()
                                  for path in self.out.rglob('*') if path.is_file()})
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_plan_model_template_dataset_execution_and_source_pins_are_required(self):
        mutations = [('dataset_sha256', 'd' * 64), ('grounded_suffix_sha256', 'd' * 64),
                     ('model_digest', 'd' * 64), ('template_sha256', 'd' * 64),
                     ('execution_module_sha256', 'd' * 64), ('case_ids', ['HB-V0000', 'HB-V0000'])]
        for key, value in mutations:
            with self.subTest(key=key):
                candidate = deepcopy(self.plan)
                candidate[key] = value
                self.write(self.plan_path, candidate)
                with self.assertRaises((ValueError, OSError, RuntimeError)):
                    self.prepare()
                self.assertFalse(self.out.exists())
        candidate = deepcopy(self.plan)
        candidate['source_module_sha256'][probe.SOURCE_MODULES[0]] = 'd' * 64
        self.write(self.plan_path, candidate)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_optional_case_plan_requires_both_pins_and_change_permanently_blocks_zero_call_run(self):
        case_plan = self.root / 'results' / 'synthetic-case-plan.json'
        self.write(case_plan, {'case_ids': list(probe.CASE_IDS), 'notes': 'Synthetic root-authored case plan.'})
        case_plan.chmod(0o600)
        original = case_plan.read_bytes()
        pin = sha(original)
        invalid = [{'case_plan_path': str(case_plan)}, {'case_plan_sha256': pin},
                   {'case_plan_path': str(case_plan), 'case_plan_sha256': 'd' * 64}]
        fake = FakeTransport(probe.MODEL_DIGEST)
        for number, fields in enumerate(invalid):
            with self.subTest(invalid=number):
                self.write(self.plan_path, {**self.plan, **fields})
                with patch.object(probe, 'Transport', side_effect=AssertionError('Preparation must not create transport')):
                    with self.assertRaises((ValueError, OSError, RuntimeError)):
                        self.prepare()
                self.assertFalse(self.out.exists())
                self.assertEqual(fake.payloads, [])
        self.write(self.plan_path, {**self.plan, 'case_plan_path': str(case_plan), 'case_plan_sha256': pin})
        self.assertEqual(self.prepare()['status'], 'prepared')
        manifest = json.loads((self.out / 'manifest.json').read_bytes())
        self.assertIn({'path': str(case_plan), 'byte_sha256': pin}, manifest['source_files'])
        case_plan.write_text('Changed synthetic case plan.', encoding='utf-8')
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            probe.step(self.out, 'HB-V0000', transport=fake)
        state = json.loads((self.out / 'state.json').read_bytes())
        self.assertEqual((state['status'], state['calls']), ('blocked', 0))
        self.assertEqual(state['stop_reason'], 'source_pin_changed')
        self.assertEqual((fake.payloads, fake.metadata_calls), ([], 0))
        case_plan.write_bytes(original)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            probe.step(self.out, 'HB-V0000', transport=fake)
        restored_state = json.loads((self.out / 'state.json').read_bytes())
        self.assertEqual((restored_state['status'], restored_state['calls']), ('blocked', 0))
        self.assertEqual((fake.payloads, fake.metadata_calls), ([], 0))

    def test_literal_original_history_and_settings_are_sent_without_evaluation_fields(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        result = probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual((result['status'], result['case_status'], result['calls']),
                         ('completed', 'awaiting_patient', 1))
        payload = fake.payloads[0]
        self.assertEqual(payload['model'], DEFAULT_MODEL)
        self.assertEqual((payload['temperature'], payload['reasoning_effort'], payload['max_tokens']),
                         (0, 'medium', 2048))
        self.assertEqual(payload['messages'][0]['role'], 'system')
        self.assertIn(GROUNDED_SUFFIX, payload['messages'][0]['content'])
        for original in self.items[0]['prompt']:
            self.assertIn(original, payload['messages'])
        serialized = json.dumps(fake.payloads)
        for sentinel in ('RUBRIC_SENTINEL', 'REFERENCE_SENTINEL', 'CANARY_SENTINEL', 'HOLDOUT_PROMPT'):
            self.assertNotIn(sentinel, serialized)
        saved = self.row()
        self.assertEqual(saved['answer'], response()['choices'][0]['message']['content'])
        self.assertNotIn('HIDDEN_REASONING_MUST_NOT_BE_SAVED', json.dumps(saved))
        self.assertEqual(saved['request_sha256'], canonical_hash(payload))
        self.assertEqual(saved['input_sha256'], canonical_hash(payload['messages']))
        self.assertEqual(saved['digest_before'], probe.MODEL_DIGEST)
        self.assertEqual(saved['template_before_sha256'], self.plan['template_sha256'])
        self.assertEqual((saved['attempts'], saved['retries']), (1, 0))
        post = json.loads((self.out / 'cases' / 'HB-V0000' / 'turn-01-post.json').read_bytes())
        self.assertEqual(post['digest_after'], probe.MODEL_DIGEST)
        self.assertEqual(post['template_after_sha256'], self.plan['template_sha256'])
        self.assertTrue(post['model_identity_verified'])
        self.assertTrue(post['runtime_template_verified'])

    def test_patient_audit_exact_original_user_quotes_and_preserved_assistant_reply(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        first = probe.step(self.out, 'HB-V0000', transport=fake)
        patient = self.patient('HB-V0000')
        result = probe.step(self.out, 'HB-V0000', patient, transport=fake)
        self.assertEqual((result['doctor_turn'], result['calls']), (2, 2))
        second = fake.payloads[1]['messages']
        self.assertIn({'role': 'assistant', 'content': first['answer']}, second)
        self.assertIn({'role': 'user', 'content': json.loads(patient.read_text())['utterance']}, second)
        saved = self.row(turn=2)
        self.assertEqual(saved['patient_turn']['utterance'], json.loads(patient.read_text())['utterance'])
        self.assertEqual(saved['patient_turn']['source_quotes'], json.loads(patient.read_text())['source_quotes'])
        self.assertEqual(saved['patient_turn']['validated_by'], 'root')

    def test_invalid_patient_quote_assistant_quote_or_missing_root_audit_blocks_without_new_call(self):
        changes = [dict(quotes=['invented unsupported quote']), dict(quotes=['ASSISTANT_ONLY_UNCONFIRMED_FACT']),
                   dict(no_new_facts=False), dict(validated_by='untrusted'), dict(utterance=''),
                   dict(quotes=[], synthetic=False)]
        for number, changeset in enumerate(changes):
            with self.subTest(number=number):
                if number:
                    self.out = self.root / 'results' / f'patient-invalid-{number}'
                self.prepare()
                fake = FakeTransport(probe.MODEL_DIGEST)
                probe.step(self.out, 'HB-V0000', transport=fake)
                path = self.patient('HB-V0000', **changeset)
                with self.assertRaises((ValueError, OSError, RuntimeError)):
                    probe.step(self.out, 'HB-V0000', path, transport=fake)
                self.assertEqual(len(fake.payloads), 1)
                self.assertFalse((self.out / 'cases' / 'HB-V0000' / 'turn-02.json').exists())

    def test_audited_synthetic_question_needs_no_new_patient_fact_or_fake_quote(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        probe.step(self.out, 'HB-V0000', transport=fake)
        patient = self.patient('HB-V0000', utterance='Could you clarify the plan?', quotes=[], synthetic=True)
        result = probe.step(self.out, 'HB-V0000', patient, transport=fake)
        self.assertEqual(result['calls'], 2)
        self.assertIn({'role': 'user', 'content': 'Could you clarify the plan?'}, fake.payloads[1]['messages'])

    def test_third_turn_preserves_patient_and_doctor_history_in_original_order(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST, [response('Doctor reply one.'), response('Doctor reply two.'),
                                                 response('Doctor reply three.')])
        first = probe.step(self.out, 'HB-V0000', transport=fake)
        second_patient = self.patient('HB-V0000', utterance='Could you clarify the plan?', quotes=[], synthetic=True)
        second = probe.step(self.out, 'HB-V0000', second_patient, transport=fake)
        third_patient = self.patient('HB-V0000', utterance='What should I watch for?', quotes=[], synthetic=True)
        probe.step(self.out, 'HB-V0000', third_patient, transport=fake)
        expected = [*fake.payloads[0]['messages'],
                    {'role': 'assistant', 'content': first['answer']},
                    {'role': 'user', 'content': 'Could you clarify the plan?'},
                    {'role': 'assistant', 'content': second['answer']},
                    {'role': 'user', 'content': 'What should I watch for?'}]
        self.assertEqual(fake.payloads[2]['messages'], expected)

    def test_pending_patient_duplicate_start_unknown_and_unselected_cases_never_make_new_calls(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        for case_id in ('HB-V0020', 'HB-V0001', 'not-a-case'):
            with self.subTest(case_id=case_id), self.assertRaises((ValueError, OSError, RuntimeError)):
                probe.step(self.out, case_id, transport=fake)
        self.assertEqual(fake.payloads, [])
        probe.step(self.out, 'HB-V0000', transport=fake)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_unfinalized_reserved_request_is_not_retried_even_if_dispatch_is_unknown(self):
        self.prepare()
        state_path = self.out / 'state.json'
        state = json.loads(state_path.read_bytes())
        state.update(reserved_calls=1, calls=0, used_request_sha256=['a' * 64],
                     pending={'case_id': 'HB-V0000', 'doctor_turn': 1, 'request_sha256': 'a' * 64})
        self.write(state_path, state)
        fake = FakeTransport(probe.MODEL_DIGEST)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual((len(fake.payloads), fake.metadata_calls), (0, 0))

    def test_saved_receipt_and_post_identity_sidecar_cannot_be_changed_before_next_turn(self):
        for number, filename in enumerate(('turn-01.json', 'turn-01-post.json')):
            with self.subTest(filename=filename):
                self.out = self.root / 'results' / f'evidence-tamper-{number}'
                self.prepare()
                fake = FakeTransport(probe.MODEL_DIGEST)
                probe.step(self.out, 'HB-V0000', transport=fake)
                path = self.out / 'cases' / 'HB-V0000' / filename
                changed = json.loads(path.read_bytes())
                changed['tamper_sentinel'] = True
                self.write(path, changed)
                with self.assertRaises((ValueError, OSError, RuntimeError)):
                    probe.step(self.out, 'HB-V0000', self.patient('HB-V0000'), transport=fake)
                self.assertEqual(len(fake.payloads), 1)

    def test_real_transport_constructor_receives_fixed_ninety_second_timeout(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        with patch.object(probe, 'Transport', return_value=fake) as constructor:
            result = probe.step(self.out, 'HB-V0000')
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args[2], 90)
        self.assertEqual(result['calls'], 1)

    def test_fixed_four_case_budget_and_case0004_stops_after_first_answer(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        counts = []
        for case_id in probe.CASE_IDS:
            result = probe.step(self.out, case_id, transport=fake)
            for turn in range(2, 6):
                if case_id == 'HB-V0004':
                    break
                result = probe.step(self.out, case_id, self.patient(case_id), transport=fake)
            counts.append(result['doctor_turn'])
            self.assertEqual(result['case_status'], 'stopped')
            with self.assertRaises((ValueError, OSError, RuntimeError)):
                probe.step(self.out, case_id, self.patient(case_id), transport=fake)
        self.assertEqual(counts, [5, 1, 5, 5])
        self.assertEqual(len(fake.payloads), 16)
        self.assertEqual(result['calls'], 16)
        self.assertLessEqual(result['calls'], 20)
        for payload in fake.payloads:
            self.assertEqual(payload['max_tokens'], 2048)
        rows = list(self.out.glob('cases/*/turn-[0-9][0-9].json'))
        self.assertEqual(len(rows), 16)

    def test_explicit_stop_preserves_existing_answer_without_extra_generation(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST)
        probe.step(self.out, 'HB-V0000', transport=fake)
        before = (self.out / 'cases' / 'HB-V0000' / 'turn-01.json').read_bytes()
        stopped = probe.step(self.out, 'HB-V0000', stop=True, transport=fake)
        self.assertEqual(stopped['case_status'], 'stopped')
        self.assertEqual(len(fake.payloads), 1)
        self.assertEqual(before, (self.out / 'cases' / 'HB-V0000' / 'turn-01.json').read_bytes())

    def test_model_digest_template_and_metadata_failure_before_call_keep_zero_generations(self):
        setups = [('digest', lambda fake: setattr(fake, 'digest', 'b' * 64)),
                  ('template', lambda fake: setattr(fake, 'template_result', {'template': 'CHANGED'})),
                  ('metadata', lambda fake: setattr(fake, 'metadata_result', RuntimeError('SECRET_ERROR_SENTINEL')))]
        for name, configure in setups:
            with self.subTest(name=name):
                self.out = self.root / 'results' / f'pre-{name}'
                self.prepare()
                fake = FakeTransport(probe.MODEL_DIGEST)
                configure(fake)
                result = probe.step(self.out, 'HB-V0000', transport=fake)
                self.assertEqual((result['status'], result['calls']), ('blocked', 0))
                self.assertEqual(fake.payloads, [])
                for path in self.out.rglob('*.json'):
                    self.assertNotIn('SECRET_ERROR_SENTINEL', path.read_text())

    def test_data_execution_and_support_source_changed_after_prepare_block_before_generation(self):
        paths = (self.data, self.execution, self.root / probe.SOURCE_MODULES[0])
        originals = {path: path.read_bytes() for path in paths}
        for number, path in enumerate(paths):
            with self.subTest(number=number):
                for target, original in originals.items():
                    target.write_bytes(original)
                self.out = self.root / 'results' / f'changed-before-{number}'
                self.prepare()
                path.write_text('CHANGED_SINCE_PREPARATION', encoding='utf-8')
                fake = FakeTransport(probe.MODEL_DIGEST)
                try:
                    result = probe.step(self.out, 'HB-V0000', transport=fake)
                except (ValueError, OSError, RuntimeError):
                    pass
                else:
                    self.assertEqual((result['status'], result['calls']), ('blocked', 0))
                self.assertEqual(fake.payloads, [])

    def test_post_response_model_template_source_change_keeps_actual_answer_and_blocks_further_calls(self):
        raw = '  Synthetic received answer.\n한글 '
        changes = [('digest', lambda fake: setattr(fake, 'digest', 'b' * 64)),
                   ('template', lambda fake: setattr(fake, 'template_result', {'template': 'CHANGED'})),
                   ('source', lambda fake: self.data.write_text('SOURCE MUTATED'))]
        original_data = self.data.read_bytes()
        for name, changed in changes:
            with self.subTest(name=name):
                self.data.write_bytes(original_data)
                self.out = self.root / 'results' / f'post-{name}'
                self.prepare()
                fake = FakeTransport(probe.MODEL_DIGEST, [response(raw)], on_complete=changed)
                result = probe.step(self.out, 'HB-V0000', transport=fake)
                self.assertEqual(result['calls'], 1)
                self.assertEqual(result['run_status'], 'blocked')
                saved = self.row()
                self.assertEqual(saved['answer'], raw)
                with self.assertRaises((ValueError, OSError, RuntimeError)):
                    probe.step(self.out, 'HB-V0015', transport=fake)
                self.assertEqual(len(fake.payloads), 1)

    def test_unknown_usage_is_null_and_never_zero_in_record(self):
        self.prepare()
        fake = FakeTransport(probe.MODEL_DIGEST, [response(usage=False)])
        probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(self.row()['usage'], {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None})

    def test_received_answer_with_invalid_envelope_is_preserved_without_completion_or_retry(self):
        self.prepare()
        literal = '  ACTUAL RECEIVED CONTENT\n한글 '
        malformed = response(literal, finish=[])
        fake = FakeTransport(probe.MODEL_DIGEST, [malformed])
        result = probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual((result['status'], result['calls'], result['case_status']), ('failed', 1, 'stopped'))
        saved = self.row()
        self.assertEqual(saved['raw_receipt']['content'], literal)
        self.assertTrue(saved['raw_receipt']['provider_response_received'])
        self.assertEqual(saved['usage'], {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30})
        self.assertEqual(len(fake.payloads), 1)

    def test_empty_truncated_and_transport_failure_are_one_attempt_without_retry(self):
        variants = [('empty', response('', 'stop'), 'failed'),
                    ('truncated', response('Partial exact answer', 'length'), 'truncated'),
                    ('transport', RuntimeError('SECRET_TRANSPORT_ERROR'), 'blocked')]
        for name, value, expected in variants:
            with self.subTest(name=name):
                self.out = self.root / 'results' / f'failure-{name}'
                self.prepare()
                fake = FakeTransport(probe.MODEL_DIGEST, [value])
                result = probe.step(self.out, 'HB-V0000', transport=fake)
                self.assertEqual((result['status'], result['calls'], result['case_status']),
                                 (expected, 1, 'stopped'))
                self.assertEqual(len(fake.payloads), 1)
                saved = self.row()
                if name == 'truncated':
                    self.assertEqual(saved['answer'], 'Partial exact answer')
                self.assertNotIn('SECRET_TRANSPORT_ERROR', json.dumps(saved))
                with self.assertRaises((ValueError, OSError, RuntimeError)):
                    probe.step(self.out, 'HB-V0000', self.patient('HB-V0000'), transport=fake)
                self.assertEqual(len(fake.payloads), 1)


if __name__ == '__main__':
    unittest.main()
