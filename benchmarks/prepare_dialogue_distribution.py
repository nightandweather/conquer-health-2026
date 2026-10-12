"""Extract the frozen supplied ACI train dialogues for descriptive analysis.

Never emits clinical-note targets or reads other splits. Output contains private
dialogue text and must remain outside public Git. Does not call any model.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re

SOURCE_SHA256 = {
    'train.csv': '6c778d4ac5e6cc6f1964786f9286e8d765c210f22ed6b57f83aff8497409cea4',
    'train_metadata.csv': '7da650e223f04ff6bf1666cb62d52ebd83ce71ecfc4b2311cbe64d9f3ab19d83',
}


def extract(source_dir):
    path = Path(source_dir) / 'train.csv'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256['train.csv']:
        raise ValueError('Different supplied training file')
    meta_path = Path(source_dir) / 'train_metadata.csv'
    meta_raw = meta_path.read_bytes()
    if hashlib.sha256(meta_raw).hexdigest() != SOURCE_SHA256['train_metadata.csv']:
        raise ValueError('Different supplied training metadata')
    metadata_rows = list(csv.DictReader(io.StringIO(meta_raw.decode())))
    metadata = {r['encounter_id']: r for r in metadata_rows}
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    if len(rows) != 67 or len(metadata) != len(metadata_rows):
        raise ValueError('Unexpected train population or duplicate metadata IDs')
    if len({r['encounter_id'] for r in rows}) != 67 or set(metadata) != {r['encounter_id'] for r in rows}:
        raise ValueError('Source/metadata encounter mismatch')
    out = []
    for i,row in enumerate(rows):
        spans = list(re.finditer(r'\[(doctor|patient)\]',row['dialogue'],re.I))
        if not spans or row['dialogue'][:spans[0].start()].strip():
            raise ValueError('Unparsed dialogue prefix or no speaker labels')
        turns = []
        for j,span in enumerate(spans):
            text = row['dialogue'][span.end():spans[j+1].start() if j+1<len(spans) else len(row['dialogue'])].strip()
            turns.append({'turn':j+1, 'role':span.group(1).lower(), 'text':text, 'question_marks':text.count('?')})
        meta = metadata[row['encounter_id']]
        out.append({'case_id':f'ACI-TRAIN-{i:03d}', 'source_id':row['encounter_id'], 'dataset':row['dataset'],
            'dialogue_sha256':hashlib.sha256(row['dialogue'].encode()).hexdigest(), 'cc':meta['cc'],
            'secondary_complaints':meta['2nd_complaints'], 'age':meta['patient_age'], 'sex':meta['patient_gender'], 'turns':turns})
    return out, {path.name:hashlib.sha256(raw).hexdigest(), meta_path.name:hashlib.sha256(meta_raw).hexdigest()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source-dir',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a = p.parse_args()
    out,pins = extract(a.source_dir)
    a.out.mkdir(parents=True,exist_ok=False)
    (a.out/'train-only-input.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
    (a.out/'manifest.json').write_text(json.dumps({'source_pins':pins,'train_cases':67,'note_fields_emitted':0,'model_calls':0},indent=2))


if __name__ == '__main__':
    main()
