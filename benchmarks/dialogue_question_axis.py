#!/usr/bin/env python3
"""Question-purpose descriptive audit on supplied dialogue-only input.

The sampling unit is a speaker turn containing literal '?', NOT a validated
question. No clinical answers, generated model calls or reference notes are used.
Private manual annotations are optional external JSON, never embedded in code.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re

VERSION = 'question-purpose-rules-v1'
DOCTOR_LABELS = {
 'symptom_assessment': '증상·현재 상태 파악',
 'treatment_medication': '복약·치료 확인',
 'history_context': '과거력·사회력·가족력·생활 확인',
 'tests_procedures': '검사·진찰·절차 확인',
 'patient_agenda': '환자 질문·걱정·방문 목적 확인',
 'social_greeting': '인사·관계 형성',
 'confirmation_consent': '설명 후 확인·동의',
 'other_unclassified': '기타·규칙 미분류',
}
PATIENT_LABELS = {
 'social_greeting': '인사·관계 형성',
 'understanding_condition': '증상·질환·의학 용어 이해',
 'treatment_medication': '치료·약물·보충제 문의',
 'tests_results_procedures': '검사·결과·시술 문의',
 'followup_daily_life': '다음 단계·재진·일상 관리 문의',
 'logistics_access_research': '예약·비용·접근·연구 참여 문의',
 'confirmation_acknowledgment': '확인·맞장구·꼬리표',
 'other_nonclinical': '기타 비임상·인용 의문',
}
# Match question-ending segments, not all preceding narrative in the turn.
# Deliberately transparent lexical proxies: abbreviated or implicit references
# can be missed, and single segments can trigger multiple categories.
PATTERNS = {
 'patient_agenda': r'\b(?:any|other|more)\s+(?:questions?|concerns?|issues?)\b|\bwhat (?:can|could) i (?:do|help)|\bhow can i help|\b(?:brings|brought) you|\bwhat.*(?:talk|discuss).*about|\banything else',
 'treatment_medication': r'\b(?:medication|medications|meds|medicine|medicines|pills?|taking|take|taken|dose|dosage|milligrams?|mg|therapy|treatment|treatments|prescription|prescribed|ibuprofen|tylenol|insulin|antibiotics?|inhaler|supplements?|allerg(?:ic|ies|y)|surgery|surgeries|operations?)\b',
 'history_context': r'\b(?:history|family|mother|father|parents?|siblings?|brothers?|sisters?|grandmother|grandfather|work|job|occupation|smok(?:e|ing|ed)|tobacco|alcohol|drinks?|drinking|diet|eating|eat|exercise|active|sleep|sleeping|married|children|kids|sexually|sexual|travel|traveled|pregnan(?:t|cy)|periods?|menstrual)\b',
 'tests_procedures': r'\b(?:tests?|labs?|laboratory|results?|blood work|bloodwork|scan|mri|ct|ultrasound|x-?rays?|radiograph|biopsy|physical exam|examination|exam|mammogram|colonoscopy|echocardiogram|stress test|screening|procedure|procedures)\b',
 'symptom_assessment': r'\b(?:symptoms?|pain|hurt|hurts|hurting|sore|soreness|swelling|swollen|numbness|tingling|weakness|fever|chills|cough|breath|breathing|wheez(?:e|ing)|nausea|vomit(?:ing)?|diarrhea|constipation|bleed(?:ing)?|blood|rash|itch(?:ing)?|dizz(?:y|iness)|faint|headache|tired|fatigue|weight|appetite|vision|hearing|urina(?:te|tion)|urinary|bowel|bladder|sweats?|injur(?:y|ed)|depress(?:ion|ed)|anx(?:ious|iety)|suicid\w*|harm yourself|feel(?:ing)?|bother(?:ing)?|started|start|began|begin|worse|better|happened|happening|how long|how often|how severe|rate.*scale)\b',
 'social_greeting': r'\bhow (?:are|have) you(?: (?:doing|been|today))?\b|\bhow(?:\x27s| is) it going\b|\bhow about you\b',
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
PRIORITY = ('patient_agenda', 'symptom_assessment', 'treatment_medication', 'history_context', 'tests_procedures', 'social_greeting', 'confirmation_consent', 'other_unclassified')
TAG = re.compile(r'^\s*(?:(?:uh|um|so|and|but|well)\s*[, ]\s*)?(?:okay|ok|right|all right|correct|you know|does that (?:make sense|sound (?:good|okay))|is that (?:okay|right|correct)|sound good|you know what i mean)\s*$', re.I)

def doctor_rule(text):
    segments = [m.group(1).strip() for m in re.finditer(r'([^.!?]*)\?', text)]
    labels = set()
    for segment in segments:
        if TAG.fullmatch(segment):
            labels.add('confirmation_consent')
            continue
        labels.update(k for k, p in COMPILED.items() if p.search(segment))
    if not labels:
        labels.add('other_unclassified')
    ordered = [label for label in PRIORITY if label in labels]
    return {'primary': ordered[0], 'labels': ordered, 'question_segments': segments}

def digest(value):
    return hashlib.sha256(value).hexdigest()

def selected_turns(cases):
    result=[]
    case_ids=set()
    if not isinstance(cases,list): raise ValueError('Dialogue source must be a list')
    for case in cases:
        if case['case_id'] in case_ids: raise ValueError('Duplicate case ID')
        case_ids.add(case['case_id'])
        turn_ids=set()
        for i, turn in enumerate(case['turns']):
            if turn['turn'] in turn_ids: raise ValueError('Duplicate turn ID')
            turn_ids.add(turn['turn'])
            if turn['role'] not in {'doctor','patient'}: raise ValueError('Unknown speaker role')
            if not isinstance(turn['text'],str): raise ValueError('Turn text must be string')
            if not isinstance(turn['turn'],int) or turn['turn']<1: raise ValueError('Invalid turn number')
            if '?' not in turn['text']:
                continue
            result.append({
                'id': f"{case['case_id']}:{turn['turn']:03d}",
                'case_id': case['case_id'], 'dataset': case.get('dataset','unknown'), 'turn': turn['turn'],
                'role': turn['role'], 'text': turn['text'],
                'text_sha256': digest(turn['text'].encode()),
                'question_marks': turn['text'].count('?'),
                'previous_turn': case['turns'][i-1] if i else None,
                'next_turn': case['turns'][i+1] if i+1<len(case['turns']) else None,
            })
    return result

def frequencies(rows, role, codebook):
    subset = [r for r in rows if r['role']==role]
    primary=Counter(r['primary'] for r in subset)
    multi=Counter(label for r in subset for label in r['labels'])
    return {
      'turn_denominator': len(subset),
      'primary': [{'code': k, 'name_ko': name, 'count': primary[k], 'percent_of_role_proxy_turns': round(100*primary[k]/len(subset),3) if subset else None, 'percent_of_all_proxy_turns':round(100*primary[k]/len(rows),3) if rows else None} for k,name in codebook.items()],
      'multilabel': dict(multi),
      'multiple_labels_turns':sum(len(r['labels'])>1 for r in subset),
      'label_assignment_count':sum(multi.values()),
      'primary_sum_check':sum(primary.values())==len(subset),
    }

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', required=True,type=Path)
    ap.add_argument('--out',required=True,type=Path, help='New immutable output directory')
    ap.add_argument('--patient-labels',type=Path)
    ap.add_argument('--doctor-audit',type=Path)
    args=ap.parse_args()
    source=args.source.read_bytes(); cases=json.loads(source)
    rows=selected_turns(cases)
    doctors=[r for r in rows if r['role']=='doctor']
    patients=[r for r in rows if r['role']=='patient']
    annotations={}
    if args.patient_labels:
        annotation_list=json.loads(args.patient_labels.read_text())['annotations']
        annotations={r['id']:r for r in annotation_list}
        if len(annotations)!=len(annotation_list): raise ValueError('Duplicate patient annotation ID')
        if set(annotations)!={r['id'] for r in patients}: raise ValueError('Patient annotation coverage mismatch')
    for row in doctors:
        row.update(doctor_rule(row['text']))
    for row in patients:
        if annotations:
            a=annotations[row['id']]
            if a['text_sha256']!=row['text_sha256']: raise ValueError('Changed patient source')
            if a['primary'] not in PATIENT_LABELS or a['primary'] not in a['labels'] or not set(a['labels'])<=set(PATIENT_LABELS): raise ValueError('Invalid annotation')
            row.update({k:a[k] for k in ('primary','labels','proxy_kind','reason')})
        else:
            row.update(primary='unreviewed',labels=['unreviewed'])
    sample=random.Random(20261012).sample(doctors,min(60,len(doctors)))
    packet=[{k:r[k] for k in ('id','role','text','text_sha256','previous_turn','next_turn')} for r in sample]
    audit=None
    if args.doctor_audit:
        manual=json.loads(args.doctor_audit.read_text())['annotations']
        mapped={r['id']:r for r in manual}
        if len(mapped)!=len(manual): raise ValueError('Duplicate audit annotation ID')
        if set(mapped)!={r['id'] for r in sample}: raise ValueError('Audit coverage mismatch')
        checks=[]
        for r in sample:
            a=mapped[r['id']]
            if a['text_sha256']!=r['text_sha256']: raise ValueError('Changed audit source')
            if a['primary'] not in DOCTOR_LABELS or not set(a['labels'])<=set(DOCTOR_LABELS) or a['primary'] not in a['labels']: raise ValueError('Invalid audit labels')
            checks.append({'id':r['id'],'rule_primary':r['primary'],'manual_primary':a['primary'],'primary_agrees':r['primary']==a['primary'],'exact_label_set_agrees':set(r['labels'])==set(a['labels'])})
        audit={'sample_size':len(checks),'seed':20261012,'primary_agreement_count':sum(c['primary_agrees'] for c in checks),'exact_label_set_agreement_count':sum(c['exact_label_set_agrees'] for c in checks),'manual_primary_counts':dict(Counter(r['primary'] for r in manual)),'manual_primary_percent':{k:round(100*v/len(manual),3) for k,v in Counter(r['primary'] for r in manual).items()},'checks':checks,'limitation':'A single Codex annotator audited frozen rules using hidden predictions; no independent second-annotator adjudication or clinical accuracy claim. Rules were not tuned against this sample.'}
    summary={
      'schema_version':1,'method_version':VERSION,'source_sha256':digest(source),'classifier_sha256':digest(Path(__file__).read_bytes()),
      'population':'Supplied dialogue-only ACI-Bench training subset; not NOVA hidden/sample cases',
      'unit':'speaker turn containing literal question mark; not semantic question count',
      'source_groups': {group: {'cases':len([c for c in cases if c.get('dataset','unknown')==group]),'turns':sum(len(c['turns']) for c in cases if c.get('dataset','unknown')==group),'question_proxy_turns':sum(r['dataset']==group for r in rows),'proxy_by_role':dict(Counter(r['role'] for r in rows if r['dataset']==group))} for group in sorted({c.get('dataset','unknown') for c in cases})},
      'punctuation_limitation':'The question-purpose distribution only describes question-mark-preserving dialogue groups. A zero question-mark count does not mean no questions were asked.',
      'cases':len(cases),'all_turns':sum(len(c['turns']) for c in cases),'all_question_proxy_turns':len(rows),'proxy_turns_by_role':dict(Counter(r['role'] for r in rows)),
      'doctor':frequencies(rows,'doctor',DOCTOR_LABELS),
      'patient':frequencies(rows,'patient',PATIENT_LABELS if annotations else {'unreviewed':'미검토'}),
      'patient_proxy_kind':dict(Counter(r.get('proxy_kind','unreviewed') for r in patients)),
      'patient_direct_or_clarification':{'turns':sum(r.get('proxy_kind') in {'direct_information_request','clarification_request'} for r in patients),'primary_counts':dict(Counter(r['primary'] for r in patients if r.get('proxy_kind') in {'direct_information_request','clarification_request'}))},
      'patient_question_proxy_cases':len({r['case_id'] for r in patients}),
      'patient_question_proxy_turns_by_case':dict(Counter(r['case_id'] for r in patients)),
      'audit': {k:v for k,v in audit.items() if k!='checks'} if audit else None,
      'warning':'Purpose is descriptive. Rule labels are approximate. Patient categories are manually assigned. No diagnosis or clinical quality score. Multiple labels may exceed 100%. Case clustering and scripted dialogue style prevent generalization to patient prevalence. No test/validation/reference notes used.'
    }
    args.out.mkdir(parents=True,exist_ok=False)
    for name,value in [('question-summary.json',summary),('question-private-rows.json',rows),('question-doctor-audit-packet.json',{'rules_frozen_sha256':summary['classifier_sha256'],'seed':20261012,'rows':packet})]:
        (args.out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    if audit: (args.out/'question-audit-results.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'out':str(args.out),'cases':len(cases),'question_proxy_turns':len(rows),'roles':summary['proxy_turns_by_role'],'audit':summary['audit']},ensure_ascii=False))
if __name__=='__main__': main()
