"""Possible score ranges for saved judgments with unresolved criteria.

These are mathematical uncertainty bounds, not confidence intervals or a
measured benchmark score. No missing label is assigned false. Known inference
failures retain declared zero utility; missing/blocked answers prevent bounds.
"""

from __future__ import annotations

from collections import Counter
import math

from .healthbench_score import (_answer_index, _data_index, _judgment_index,
                               _manifest_selection, SCORING_REFERENCE)


def build_score_bounds(data_rows: list[dict], answer_rows: list[dict],
                       judgment_rows: list[dict], manifest: dict | None = None) -> dict:
    data = _data_index(data_rows)
    answers = _answer_index(answer_rows, data)
    selected, source, names = _manifest_selection(manifest, data, answers)
    judgments = _judgment_index(judgment_rows, data, answers)
    arms = {}
    for arm in sorted(names):
        cases = []
        for prompt_id in selected:
            answer = answers.get((arm, prompt_id))
            rubric = data[prompt_id]
            case = {'prompt_id': prompt_id, 'lower_raw_score': None, 'upper_raw_score': None,
                    'unresolved_criteria': 0}
            if answer is None or answer['kind'] == 'blocked':
                case['status'] = 'missing_answer' if answer is None else 'blocked'
            elif answer['kind'] == 'known_inference_failure':
                case.update(status='known_inference_failure', lower_raw_score=0.0, upper_raw_score=0.0)
            else:
                known, lower_unknown, upper_unknown = [], [], []
                for number, point in enumerate(rubric['points']):
                    key = (arm, prompt_id, number)
                    if key in judgments:
                        known.append(point if judgments[key] else 0.0)
                    else:
                        case['unresolved_criteria'] += 1
                        lower_unknown.append(min(point, 0.0))
                        upper_unknown.append(max(point, 0.0))
                lower = math.fsum(known + lower_unknown) / rubric['denominator']
                upper = math.fsum(known + upper_unknown) / rubric['denominator']
                if not math.isfinite(lower) or not math.isfinite(upper):
                    raise ValueError('Nonfinite score bounds')
                case.update(status='bounded' if case['unresolved_criteria'] else 'exact',
                            lower_raw_score=lower, upper_raw_score=upper)
            cases.append(case)
        counts = Counter(case['status'] for case in cases)
        unavailable = counts['missing_answer'] + counts['blocked']
        unresolved = sum(case['unresolved_criteria'] for case in cases)
        lower_mean = upper_mean = lower_percent = upper_percent = None
        if not unavailable:
            # HealthBench clips the mean, never each signed per-case score.
            lower_mean = math.fsum(case['lower_raw_score'] for case in cases) / len(cases)
            upper_mean = math.fsum(case['upper_raw_score'] for case in cases) / len(cases)
            lower_percent = min(1.0, max(0.0, lower_mean)) * 100
            upper_percent = min(1.0, max(0.0, upper_mean)) * 100
        arms[arm] = {'status': 'unavailable' if unavailable else 'bounded' if unresolved else 'exact',
                     'selected_count': len(cases), 'unresolved_criterion_count': unresolved,
                     'known_inference_failure_count': counts['known_inference_failure'],
                     'missing_answer_count': counts['missing_answer'], 'blocked_count': counts['blocked'],
                     'lower_raw_mean': lower_mean, 'upper_raw_mean': upper_mean,
                     'lower_score_percent': lower_percent, 'upper_score_percent': upper_percent,
                     'cases': cases}
    return {'schema_version': 1, 'evaluation': 'possible_scores_from_unresolved_saved_judgments',
            'official_full_benchmark': False, 'is_confidence_interval': False,
            'notice': 'Possible score range only; unresolved criteria remain unjudged. Not a measured score.',
            'scoring_reference': SCORING_REFERENCE, 'selection_source': source,
            'selected_count': len(selected), 'arms': arms}
