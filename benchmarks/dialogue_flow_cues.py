"""Descriptive train-dialogue structure and lexical review cues, never diagnoses.

Consumes a private train-only extraction with role labels preserved verbatim.
No model calls, transcript corrections, answer/note reading, or test data access.
Question-mark statistics are explicitly restricted to punctuated sources.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics


UNCERTAIN = re.compile(r"\b(?:not sure|unsure|(?:do|did|does)n't (?:know|remember)|can't remember|cannot remember|don't recall|hard to (?:say|remember|tell))\b", re.I)
CONCERN = re.compile(r"\b(?:i(?:'m| am) (?:(?:just|really|so|a little|a bit|very) )*(?:worried|concerned|scared|afraid)|it scares me)\b", re.I)
PROVIDER_CUE = re.compile(r"\b(?:i (?:will|am going to|would like to|want to) (?:order|prescribe)|i (?:examined|reviewed) your|your (?:physical exam|vitals)|my assessment and plan|i'm (?:gon na|going to) (?:order|prescribe))\b", re.I)
DOSE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|micrograms?|milligrams?|ml|milliliters?|units?)\b", re.I)


def normalized(text):
    return re.sub(r"\s+n\s*['’]t\b", "n't", text.casefold()).replace("’", "'")


def ratio(n, d):
    return {"count": n, "denominator": d, "percent": round(100*n/d, 2) if d else None}


def spread(values):
    values = sorted(values)
    return {"min": min(values), "median": statistics.median(values), "max": max(values), "sum": sum(values)}


def analyze(cases):
    if len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError('duplicate case IDs')
    rows = [(c, t) for c in cases for t in c['turns']]
    source_summary = []
    for source in sorted({c['dataset'] for c in cases}):
        group = [c for c in cases if c['dataset'] == source]
        turns = [t for c in group for t in c['turns']]
        q = [t for t in turns if '?' in t['text']]
        source_summary.append({'source': source, 'cases': len(group), 'turns': len(turns),
            'cases_with_question_mark': sum(any('?' in t['text'] for t in c['turns']) for c in group),
            'question_mark_turns_by_source_role': dict(Counter(t['role'] for t in q)),
            'question_marks': sum(t['text'].count('?') for t in turns)})
    cues, evidence = {}, []
    for name, pattern, role in [('explicit_uncertainty', UNCERTAIN, 'patient'),
                                ('first_person_concern', CONCERN, 'patient'),
                                ('provider_language_in_patient_label', PROVIDER_CUE, 'patient'),
                                ('numeric_dose_unit', DOSE, None)]:
        found = [(c, t) for c, t in rows if (role is None or t['role'] == role) and pattern.search(normalized(t['text']))]
        cues[name] = {'turns': len(found), 'case_prevalence': ratio(len({c['case_id'] for c, t in found}), len(cases)),
                      'source_role_filter': role or 'all',
                      'by_source_case_count': {source: len({c['case_id'] for c,t in found if c['dataset']==source})
                          for source in sorted({c['dataset'] for c in cases})}}
        for c,t in found:
            evidence.append({'cue': name, 'case_id': c['case_id'], 'turn': t['turn'], 'source_role': t['role'], 'text': t['text']})
    doctor_q = [t for _,t in rows if t['role']=='doctor' and '?' in t['text']]
    patient_q_follow = Counter()
    for c in cases:
        for i,t in enumerate(c['turns']):
            if t['role']=='patient' and '?' in t['text']:
                patient_q_follow[c['turns'][i+1]['role'] if i+1<len(c['turns']) else 'end_of_dialogue'] += 1
    return {
        'schema_version': 1, 'cases': len(cases), 'turns': len(rows),
        'roles_are_source_labels_not_validated_identity': True,
        'turns_by_source_role': dict(Counter(t['role'] for _,t in rows)),
        'turns_per_dialogue': spread([len(c['turns']) for c in cases]),
        'dialogues_with_more_than_30_source_segments': ratio(sum(len(c['turns'])>30 for c in cases),len(cases)),
        'not_comparable_to_nova_action_turns': True,
        'dialogue_length_buckets': dict(Counter('1–30' if len(c['turns'])<=30 else '31–60' if len(c['turns'])<=60 else '61–90' if len(c['turns'])<=90 else '91+' for c in cases)),
        'sources': source_summary,
        'multiple_question_marks_in_doctor_turn': ratio(sum(t['text'].count('?')>=2 for t in doctor_q),len(doctor_q)),
        'next_source_speaker_after_patient_question_turn': dict(patient_q_follow),
        'adjacency_does_not_establish_answered_or_answer_quality': True,
        'review_cues': cues,
        'caveats': ['Lexical cue presence, not confirmed ambiguity, error, adverse event, or unsafe care.',
                    'Unpunctuated transcripts are not question-free.',
                    'Role labels are retained; provider-language flags require source/audio adjudication.',
                    'Numeric dosage regex misses word-form quantities and non-unit descriptions.',
                    'Training data only; not NOVA hidden evaluation or a representative patient population.']
    }, evidence


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    raw = a.input.read_bytes()
    report, evidence = analyze(json.loads(raw))
    report['input_sha256'] = hashlib.sha256(raw).hexdigest()
    a.out.mkdir(exist_ok=False, parents=True)
    (a.out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (a.out/'private-cue-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
