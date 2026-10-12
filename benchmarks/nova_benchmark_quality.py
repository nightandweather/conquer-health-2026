"""Read-only structural audit of the organizer's three reference benchmarks.

This module never calls a model, tunes prompts, emits clinical rows, or defines a
new evaluation split. DDXPlus CSVs are streamed from their original nested ZIPs.
Only aggregate measurements and whole-file/ordered-row digests are published.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
import unicodedata
import zipfile

BENCHMARK_ARCHIVE_SHA256 = "a2b4dbc6a2bc177a54cf7c7f1a4e15f94625bfd335738737b575c6832a1060a7"
UNCOMPRESSED_BYTE_LIMIT = 4 * 1024**3
CSV_FIELD_LIMIT = 1024 * 1024
DDX_COLUMNS = {"AGE", "SEX", "INITIAL_EVIDENCE", "EVIDENCES", "PATHOLOGY", "DIFFERENTIAL_DIAGNOSIS"}
DDX_EXPECTED = {"train": 1025602, "validate": 132448, "test": 134529}
ACI_EXPECTED = {"train": 67, "valid": 20, "test1": 40, "test2": 40, "test3": 40}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).digest()


def normalize(value):
    """NFKC, Unicode case-fold and whitespace only; no clinical equivalence."""
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def file_pin(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    size = 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block); md5.update(block); size += len(block)
    return {"bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def safe_zip_member(info):
    path = PurePosixPath(info.filename)
    mode = info.external_attr >> 16
    if (not info.filename or "\\" in info.filename or path.is_absolute()
            or ".." in path.parts or stat.S_ISLNK(mode)):
        raise ValueError("unsafe ZIP member")
    if info.file_size > UNCOMPRESSED_BYTE_LIMIT:
        raise ValueError("ZIP member exceeds uncompressed byte limit")
    return not info.is_dir()


def ratio(n, d):
    return {"count": int(n), "denominator": int(d),
            "percent": round(100 * n / d, 6) if d else None}


def duplicates(signatures):
    counts = Counter(signatures)
    groups = [n for n in counts.values() if n > 1]
    return {"groups": len(groups), "excess_rows": sum(n - 1 for n in groups),
            "affected_rows": ratio(sum(groups), len(signatures))}


def overlap(left, right):
    a, b = Counter(left), Counter(right)
    common = a.keys() & b.keys()
    return {"shared_distinct_signatures": len(common),
            "left_affected_rows": ratio(sum(a[k] for k in common), len(left)),
            "right_affected_rows": ratio(sum(b[k] for k in common), len(right))}


def contradictory_inputs(input_labels):
    input_labels = list(input_labels)
    groups = defaultdict(set)
    for input_sha, label_sha in input_labels:
        groups[input_sha].add(label_sha)
    bad = {k for k, labels in groups.items() if len(labels) > 1}
    return {"same_normalized_input_different_label_groups": len(bad),
            "affected_rows": ratio(sum(k in bad for k, _ in input_labels), len(input_labels)),
            "clinical_contradiction_confirmed": False}


def audit_agentclinic(root):
    result, signatures = {}, {}
    fields = {"Objective_for_Doctor": str, "Patient_Actor": dict,
              "Physical_Examination_Findings": dict, "Correct_Diagnosis": str}
    for filename in ("agentclinic_medqa.jsonl", "agentclinic_medqa_extended.jsonl"):
        rows, parse_fail, invalid, missing, wrong = [], 0, Counter(), Counter(), Counter()
        row_sha, exact, normal, inputs, labels = hashlib.sha256(), [], [], [], []
        key_shapes, subfield_shapes, test_types = Counter(), Counter(), Counter()
        with (root / filename).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    invalid["blank_line"] += 1; continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    parse_fail += 1; continue
                rows.append(row); h = digest(row); row_sha.update(h)
                exact.append(h); normal.append(digest(normalize(row)))
                osce = row.get("OSCE_Examination") if isinstance(row, dict) else None
                if not isinstance(osce, dict):
                    invalid["OSCE_Examination_not_object"] += 1; continue
                key_shapes[tuple(sorted(osce))] += 1
                for field, typ in fields.items():
                    value = osce.get(field)
                    if value is None or value == "" or value == {}:
                        missing[field] += 1
                    elif not isinstance(value, typ):
                        wrong[field] += 1
                test = osce.get("Test_Results")
                test_types[type(test).__name__] += 1
                if test is None or test == "" or test == {}:
                    missing["Test_Results_optional"] += 1
                elif not isinstance(test, dict):
                    wrong["Test_Results"] += 1
                if isinstance(osce.get("Patient_Actor"), dict):
                    subfield_shapes[tuple(sorted(osce["Patient_Actor"]))] += 1
                label = digest(normalize(osce.get("Correct_Diagnosis")))
                clinical_input = {k: v for k, v in osce.items() if k != "Correct_Diagnosis"}
                inputs.append(digest(normalize(clinical_input))); labels.append(label)
        n = len(rows)
        signatures[filename] = {"exact": exact, "normalized": normal, "input": inputs}
        result[filename] = {"parsed_rows": n, "parse_failed_lines": parse_fail,
            "invalid_structure": dict(invalid), "missing_or_empty": {k: ratio(v, n) for k,v in missing.items()},
            "wrong_types": dict(wrong), "OSCE_key_shapes": [{"keys": list(k), "count": v} for k,v in key_shapes.items()],
            "patient_key_shapes": [{"keys": list(k), "count": v} for k,v in subfield_shapes.items()],
            "test_result_type_counts": dict(test_types), "exact_duplicates": duplicates(exact),
            "normalized_duplicates": duplicates(normal), "normalized_input_duplicates": duplicates(inputs),
            "structural_label_conflicts": contradictory_inputs(zip(inputs, labels)),
            "unique_diagnosis_labels_normalized": len(set(labels)),
            "ordered_parsed_row_sha256": row_sha.hexdigest(),
            "published_split_present": False, "stable_source_case_id_present": False}
    base, extended = signatures.values()
    result["base_extended_overlap"] = {k: overlap(base[k], extended[k]) for k in base}
    result["grain"] = "one OSCE case; extended is a superset, not an independent split"
    return result


def split_name(name):
    for label in ("test1", "test2", "test3", "train", "valid"):
        if label in name: return label
    raise ValueError("unknown ACI published split")


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        yield reader.fieldnames
        yield from reader


def audit_aci(root):
    profiles, parts, all_ids = {}, {}, {}
    challenge_notes = defaultdict(list)
    for path in sorted((root / "challenge_data").glob("*.csv")):
        if path.name.endswith("_metadata.csv"): continue
        reader = csv_rows(path); columns = next(reader); rows = list(reader); n = len(rows)
        required = ("encounter_id", "dialogue", "note")
        missing = {k: ratio(sum(not row.get(k, "").strip() for row in rows), n) for k in required}
        ids = [r.get("encounter_id") for r in rows]
        exact = [digest(r) for r in rows]
        normalized = [digest(normalize({k:r.get(k) for k in required})) for r in rows]
        dialogues = [digest(normalize(r.get("dialogue"))) for r in rows]
        labels = [digest(normalize(r.get("note"))) for r in rows]
        metadata_path = path.with_name(path.stem + "_metadata.csv")
        meta_reader = csv_rows(metadata_path); meta_columns = next(meta_reader); metadata = list(meta_reader)
        meta_ids = [r.get("encounter_id") for r in metadata]
        counts, meta_counts = Counter(ids), Counter(meta_ids)
        speaker_counts = Counter()
        for row in rows:
            speaker_counts.update(x.casefold() for x in re.findall(r"\[(doctor|patient)\]", row.get("dialogue", ""), re.I))
        label = split_name(path.name)
        parts[label] = {"ids": ids, "exact": exact, "normalized": normalized, "dialogue": dialogues}
        all_ids[label] = set(ids)
        for identifier, note_sha in zip(ids, labels):
            challenge_notes[note_sha].append((label, digest(identifier)))
        trail = hashlib.sha256(b"".join(exact)).hexdigest()
        profiles[label] = {"file": path.name, "parsed_rows": n, "expected_rows": ACI_EXPECTED[label],
            "columns": columns, "missing_required_columns": sorted(set(required) - set(columns)),
            "missing_or_empty": missing, "csv_overflow_rows": sum(None in r for r in rows),
            "csv_short_rows": sum(any(v is None for v in r.values()) for r in rows),
            "duplicate_encounter_ids": duplicates(ids), "exact_duplicates": duplicates(exact),
            "normalized_duplicates": duplicates(normalized), "normalized_dialogue_duplicates": duplicates(dialogues),
            "structural_label_conflicts": contradictory_inputs(zip(dialogues, labels)),
            "speaker_tag_counts": dict(speaker_counts),
            "rows_missing_doctor_or_patient_tag": ratio(sum(not all(re.search(r"\["+s+r"\]", r["dialogue"], re.I) for s in ("doctor","patient")) for r in rows), n),
            "metadata": {"parsed_rows": len(metadata), "columns": meta_columns,
                "duplicate_ids": duplicates(meta_ids), "unmatched_source_rows": sum(k not in meta_counts for k in ids),
                "unmatched_metadata_rows": sum(k not in counts for k in meta_ids),
                "join_output_rows": sum(counts[k] * meta_counts[k] for k in counts)},
            "ordered_parsed_row_sha256": trail}
    pairs = {f"{a}:{b}": {k: overlap(parts[a][k], parts[b][k]) for k in parts[a]}
             for a,b in itertools.combinations(parts, 2)}
    variants = []
    experiment_notes, experiment_ids, experiment_inputs = [], [], []
    experiment_split_mismatches = 0
    for path in sorted((root / "src_experiment_data").glob("*.csv")):
        if path.name.endswith("_metadata.csv"): continue
        reader = csv_rows(path); columns = next(reader); rows = list(reader)
        key = "encounter_id" if "encounter_id" in columns else "id"
        ids = [r.get(key) for r in rows]; published = all_ids[split_name(path.name)]
        notes = [digest(normalize(r.get("note"))) for r in rows]
        same_split = sum(any(label == split_name(path.name) for label,_ in challenge_notes.get(h,[])) for h in notes)
        other_split = sum(any(label != split_name(path.name) for label,_ in challenge_notes.get(h,[])) for h in notes)
        experiment_split_mismatches += other_split
        experiment_notes.extend(notes); experiment_ids.extend(digest(i) for i in ids)
        experiment_inputs.extend(digest(normalize(r.get("dialogue"))) for r in rows)
        variants.append({"file": path.name, "parsed_rows": len(rows), "id_column": key,
            "columns": columns, "duplicate_ids": duplicates(ids),
            "ids_already_in_own_challenge_split": ratio(sum(k in published for k in ids), len(ids)),
            "ids_overlapping_other_challenge_splits": sum(k in s for k in ids for label,s in all_ids.items() if label != split_name(path.name)),
            "normalized_note_matches_any_challenge": ratio(sum(h in challenge_notes for h in notes),len(notes)),
            "normalized_note_matches_own_split": ratio(same_split,len(notes)),
            "normalized_note_matches_other_split_rows": other_split,
            "normalized_note_has_multiple_challenge_matches_rows": sum(len(challenge_notes.get(h,[])) > 1 for h in notes)})
    appearances = Counter(experiment_notes)
    id_input = defaultdict(set); id_note = defaultdict(set)
    for i,n,d in zip(experiment_ids,experiment_notes,experiment_inputs):
        id_input[i].add(d); id_note[i].add(n)
    linkage = {"experiment_rows": len(experiment_notes), "distinct_experiment_note_signatures":len(appearances),
        "challenge_distinct_note_signatures":len(challenge_notes),
        "all_experiment_rows_matching_challenge":ratio(sum(h in challenge_notes for h in experiment_notes),len(experiment_notes)),
        "matched_challenge_cases":sum(len(challenge_notes.get(h,[])) for h in appearances),
        "note_signature_with_multiple_challenge_case_matches":sum(len(v)>1 for v in challenge_notes.values()),
        "experiment_note_signatures_without_challenge_match":sum(h not in challenge_notes for h in appearances),
        "variant_rows_per_matched_note_signature_distribution":{str(n):v for n,v in Counter(appearances.values()).items()},
        "published_split_mismatch_rows":experiment_split_mismatches,
        "same_experiment_id_multiple_normalized_transcripts_groups":sum(len(v)>1 for v in id_input.values()),
        "same_experiment_id_multiple_normalized_notes_groups":sum(len(v)>1 for v in id_note.values()),
        "link_basis":"normalized reference-note hash, diagnostic quality audit only; not input available to an agent",
        "note_hash_is_confirmed_patient_identity":False,
        "interpretation":"zero direct ID overlap is not independent-case evidence; variants must stay with their original split"}
    return {"grain": "one encounter dialogue-note pair; ASR versions are repeated views, not new encounters",
        "challenge_rows": sum(v["parsed_rows"] for v in profiles.values()), "published_splits": profiles,
        "cross_split_overlap": pairs, "ASR_variants": variants, "latent_variant_linkage":linkage,
        "diagnosis_ground_truth_label_present": False,
        "speaker_identity_clinically_verified": False}


def ddx_checks(row, evidences, conditions, allowed_values=None):
    errors = Counter(); parsed = {}
    for col in DDX_COLUMNS:
        if row.get(col) is None or row[col] == "": errors["missing_"+col] += 1
    try:
        age = int(row["AGE"]); parsed["age"] = age
        if age < 0 or age > 120: errors["age_outside_0_120"] += 1
    except (KeyError, ValueError, TypeError): errors["age_invalid"] += 1
    if row.get("SEX") not in {"M", "F"}: errors["sex_not_M_F"] += 1
    if row.get("PATHOLOGY") not in conditions: errors["pathology_not_in_dictionary"] += 1
    try:
        evs = ast.literal_eval(row.get("EVIDENCES", ""))
        if not isinstance(evs, list) or not all(isinstance(v,str) for v in evs): raise ValueError()
        parsed["evidences"] = evs
        if len(evs) != len(set(evs)): errors["duplicate_evidence_within_row"] += 1
        bases = {v.split("_@_", 1)[0] for v in evs}
        if row.get("INITIAL_EVIDENCE") not in bases: errors["initial_evidence_not_in_patient_evidences"] += 1
        for value in evs:
            base, _, answer = value.partition("_@_")
            if base not in evidences:
                errors["unknown_evidence_code_rows"] = 1; continue
            allowed = (allowed_values[base] if allowed_values is not None else
                       {str(v) for v in evidences[base].get("possible-values", [])})
            if answer and answer not in allowed:
                errors["answer_outside_allowed_evidence_values_rows"] = 1
        if row.get("INITIAL_EVIDENCE") not in evidences: errors["unknown_initial_evidence_code"] += 1
    except (ValueError, SyntaxError, TypeError): errors["evidences_parse_or_type_failed"] += 1
    try:
        differential = ast.literal_eval(row.get("DIFFERENTIAL_DIAGNOSIS", ""))
        if not isinstance(differential,list) or not differential: raise ValueError()
        total, names = 0., []
        for entry in differential:
            if not isinstance(entry,(list,tuple)) or len(entry) != 2: raise ValueError()
            name, probability = entry
            if not isinstance(name,str) or isinstance(probability,bool) or not isinstance(probability,(float,int)): raise ValueError()
            if name not in conditions: errors["unknown_differential_pathology_rows"] = 1
            if not math.isfinite(probability) or probability < 0 or probability > 1: errors["invalid_differential_probability_rows"] = 1
            total += probability; names.append(name)
        if abs(total - 1.) > 1e-6: errors["differential_probability_sum_not_one"] += 1
        if len(names) != len(set(names)): errors["duplicate_differential_pathology_rows"] += 1
        if row.get("PATHOLOGY") not in names: errors["ground_truth_absent_from_differential"] += 1
        parsed["differential"] = differential
    except (ValueError, SyntaxError, TypeError): errors["differential_parse_or_type_failed"] += 1
    return errors, parsed


def sql_duplicates(db, column, split=None):
    if column not in {"exact", "normalized", "input"}: raise ValueError("bad signature column")
    where, parameters = (" WHERE split=?", (split,)) if split is not None else ("", ())
    count = db.execute("SELECT COUNT(*) FROM signatures"+where, parameters).fetchone()[0]
    groups, affected, excess = db.execute(f"SELECT COUNT(*),COALESCE(SUM(n),0),COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM signatures{where} GROUP BY {column} HAVING COUNT(*)>1)",parameters).fetchone()
    return {"groups": groups, "excess_rows": excess, "affected_rows": ratio(affected,count)}


def sql_overlap(db, column, a, b):
    if column not in {"exact", "normalized", "input"}: raise ValueError("bad signature column")
    left = dict(db.execute(f"SELECT {column},COUNT(*) FROM signatures WHERE split=? GROUP BY {column}",(a,)))
    right = dict(db.execute(f"SELECT {column},COUNT(*) FROM signatures WHERE split=? GROUP BY {column}",(b,)))
    shared = left.keys() & right.keys()
    return {"shared_distinct_signatures": len(shared),
        "left_affected_rows": ratio(sum(left[k] for k in shared),sum(left.values())),
        "right_affected_rows": ratio(sum(right[k] for k in shared),sum(right.values()))}


def audit_ddx(root, scratch):
    evidences = json.loads((root / "release_evidences.json").read_bytes())
    conditions = json.loads((root / "release_conditions.json").read_bytes())
    allowed = {k: {str(v) for v in info.get("possible-values", [])} for k,info in evidences.items()}
    fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    db = sqlite3.connect(scratch)
    db.execute("PRAGMA journal_mode=OFF"); db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA cache_size=-65536"); db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA max_page_count=262144")  # 1 GiB scratch DB; source CSV never extracted.
    db.execute("CREATE TABLE signatures(split TEXT, exact BLOB, normalized BLOB, input BLOB, label BLOB)")
    profiles = {}; total_uncompressed = 0
    try:
        for split, expected in DDX_EXPECTED.items():
            path = root / f"release_{split}_patients.zip"
            with zipfile.ZipFile(path) as archive:
                members = [i for i in archive.infolist() if safe_zip_member(i)]
                if len(members) != 1: raise ValueError("DDX ZIP must contain exactly one CSV member")
                member = members[0]; total_uncompressed += member.file_size
                if total_uncompressed > UNCOMPRESSED_BYTE_LIMIT: raise ValueError("nested stream byte cap exceeded")
                errors, ages, sexes, label_counts, evidence_lengths, differential_lengths = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
                ordered = hashlib.sha256(); pending = []; n = 0; columns = []
                with io.TextIOWrapper(archive.open(member), encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream, strict=True); columns = reader.fieldnames
                    if set(columns or []) != DDX_COLUMNS: raise ValueError("unexpected DDX CSV columns")
                    for row in reader:
                        n += 1
                        if None in row: errors["csv_overflow_rows"] += 1
                        if any(v is None for v in row.values()): errors["csv_short_rows"] += 1
                        # Literal data parsing, never eval() or executable source.
                        row_errors, parsed = ddx_checks(row, evidences, conditions, allowed)
                        errors.update(row_errors)
                        h = digest(row); ordered.update(h)
                        ev = parsed.get("evidences")
                        norm = {k:normalize(v) for k,v in row.items()}
                        if ev is not None: norm["EVIDENCES"] = sorted(ev)
                        if "differential" in parsed: norm["DIFFERENTIAL_DIAGNOSIS"] = sorted(parsed["differential"])
                        features = {k:norm.get(k) for k in ("AGE","SEX","INITIAL_EVIDENCE","EVIDENCES")}
                        pending.append((split,h,digest(norm),digest(features),digest(row.get("PATHOLOGY"))))
                        if "age" in parsed: ages[parsed["age"]] += 1
                        sexes[row.get("SEX")] += 1; label_counts[digest(row.get("PATHOLOGY")).hex()] += 1
                        if ev is not None: evidence_lengths[len(ev)] += 1
                        if "differential" in parsed: differential_lengths[len(parsed["differential"])] += 1
                        if len(pending) == 10000:
                            db.executemany("INSERT INTO signatures VALUES(?,?,?,?,?)",pending); pending.clear()
                        if n % 100000 == 0: print(json.dumps({"dataset":"DDXPlus","split":split,"rows_checked":n}),flush=True)
                    db.executemany("INSERT INTO signatures VALUES(?,?,?,?,?)",pending); db.commit()
                profiles[split] = {"parsed_rows": n, "expected_rows": expected, "count_matches_documented": n==expected,
                    "columns": columns, "zip_member_uncompressed_bytes": member.file_size,
                    "zip_stream_finished_crc_checked": True, "errors": {k:ratio(v,n) for k,v in sorted(errors.items())},
                    "age_min": min(ages) if ages else None, "age_max": max(ages) if ages else None,
                    "sex_counts": dict(sexes), "unique_ground_truth_pathologies": len(label_counts),
                    "class_count_min": min(label_counts.values()) if label_counts else None,
                    "class_count_max": max(label_counts.values()) if label_counts else None,
                    "evidence_count_min": min(evidence_lengths) if evidence_lengths else None,
                    "evidence_count_max": max(evidence_lengths) if evidence_lengths else None,
                    "ordered_parsed_row_sha256": ordered.hexdigest()}
        for column in ("exact","normalized","input"):
            db.execute(f"CREATE INDEX index_{column} ON signatures({column},split)")
            for split in profiles: profiles[split][column+"_duplicates"] = sql_duplicates(db,column,split)
        cross = {f"{a}:{b}": {k:sql_overlap(db,k,a,b) for k in ("exact","normalized","input")}
                 for a,b in itertools.combinations(profiles,2)}
        conflict_groups, conflict_rows = db.execute("SELECT COUNT(*),COALESCE(SUM(n),0) FROM (SELECT COUNT(*) n FROM signatures GROUP BY input HAVING COUNT(DISTINCT label)>1)").fetchone()
        n = sum(p["parsed_rows"] for p in profiles.values())
        result = {"grain":"one rule-generated synthetic patient; no natural dialogue, exam or test results",
            "parsed_rows":n,"condition_count":len(conditions),"evidence_question_count":len(evidences),
            "evidence_answer_type_counts":dict(Counter(v.get("data_type") for v in evidences.values())),
            "dictionary_integrity":{
                "evidence_key_matches_name_count":sum(k==v.get("name") for k,v in evidences.items()),
                "distinct_question_aliases":len({v.get("code_question") for v in evidences.values()}),
                "question_alias_differs_from_evidence_key_count":sum(k!=v.get("code_question") for k,v in evidences.items()),
                "condition_symptom_references":sum(len(v.get("symptoms",{})) for v in conditions.values()),
                "condition_antecedent_references":sum(len(v.get("antecedents",{})) for v in conditions.values()),
                "unknown_condition_evidence_references":sum(k not in evidences for v in conditions.values() for field in ("symptoms","antecedents") for k in v.get(field,{})),
                "reference_key_policy":"use evidence dictionary key; code_question can be a shared question alias"},
            "published_splits":profiles,"cross_split_overlap":cross,
            "all_split_duplicates":{k:sql_duplicates(db,k) for k in ("exact","normalized","input")},
            "same_all_evidence_input_different_ground_truth": {"groups":conflict_groups,"affected_rows":ratio(conflict_rows,n),
                "clinical_contradiction_confirmed":False,"interpretation":"ambiguous identical presentations may have different diseases; flag for separate diagnostic-label audit"},
            "nested_CSVs_extracted":False,"total_streamed_uncompressed_bytes":total_uncompressed,
            "scratch_database_bytes":Path(scratch).stat().st_size,"scratch_contains_only_hashes":True}
        return result
    finally:
        db.close(); Path(scratch).unlink(missing_ok=True)


def verify_bundle(assets):
    archive_path = assets / "nova-benchmarks-2026.zip"
    if file_pin(archive_path)["sha256"] != BENCHMARK_ARCHIVE_SHA256:
        raise ValueError("official benchmark archive SHA does not match fixed source")
    root = assets / "extracted/nova-benchmarks-2026/nova-benchmarks-2026"
    pins = {}; total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if not safe_zip_member(member): continue
            total += member.file_size
            if total > UNCOMPRESSED_BYTE_LIMIT: raise ValueError("bundle byte cap exceeded")
            relative = PurePosixPath(member.filename)
            if relative.parts[0] != "nova-benchmarks-2026": raise ValueError("unexpected archive root")
            relative = Path(*relative.parts[1:]); path = root / relative
            if path.is_symlink() or not path.is_file(): raise ValueError("missing or symlink extracted benchmark file")
            expected = hashlib.sha256()
            with archive.open(member) as stream:
                for block in iter(lambda:stream.read(1024*1024),b""): expected.update(block)
            pin = file_pin(path)
            if pin["sha256"] != expected.hexdigest(): raise ValueError("extracted benchmark source differs from official archive")
            pins[str(relative)] = pin
    return root, pins


def run_audit(assets, output):
    started = datetime.now(timezone.utc).isoformat(); clock = time.monotonic()
    assets, output = Path(assets).resolve(), Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    csv.field_size_limit(CSV_FIELD_LIMIT)
    root, pins = verify_bundle(assets)
    result = {"schema_version":1,"started_at_utc":started,"official_benchmark_archive_sha256":BENCHMARK_ARCHIVE_SHA256,
        "source_files":pins,"clinical_rows_or_answers_emitted":False,"model_calls":0,
        "scope":"entire provided reference bundle; not hidden NOVA evaluation cases; English external benchmarks",
        "definitions":{"exact_duplicate":"canonical complete parsed row with literal strings preserved",
            "normalized_duplicate":"NFKC/casefold/whitespace; DDX additionally sorts literal evidence/differential lists",
            "ordered_row_pin":"SHA256 concatenation of each canonical row SHA256 in source order",
            "input_conflict":"same normalized complete input with different label; not confirmed clinical error",
            "percentage_denominator":"parsed rows in that named file or split; failed JSON lines reported separately"}}
    result["AgentClinic-MedQA"] = audit_agentclinic(root/"AgentClinic-MedQA")
    result["ACI-Bench"] = audit_aci(root/"ACI-Bench/aci-bench-corpus")
    result["DDXPlus"] = audit_ddx(root/"DDXPlus",output/"hash-only-scratch.sqlite")
    for relative, pin in pins.items():
        if file_pin(root/relative) != pin: raise ValueError("benchmark source mutated during audit")
    if file_pin(assets/"nova-benchmarks-2026.zip")["sha256"] != BENCHMARK_ARCHIVE_SHA256:
        raise ValueError("benchmark archive mutated during audit")
    result.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-clock,3),
        source_pins_verified_before_and_after=True,clinical_ground_truth_accuracy_verified=False,
        data_model_pretraining_overlap_verified=False,split_design_implemented=False)
    fd = os.open(output/"quality-summary.json",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,"w") as stream: json.dump(result,stream,ensure_ascii=False,indent=2); stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True,help="new private immutable output directory")
    args = parser.parse_args(); result = run_audit(args.assets_dir,args.output)
    print(json.dumps({"status":"structural_audit_completed","AgentClinic_extended_rows":result["AgentClinic-MedQA"]["agentclinic_medqa_extended.jsonl"]["parsed_rows"],
        "DDXPlus_rows":result["DDXPlus"]["parsed_rows"],"ACI_Bench_rows":result["ACI-Bench"]["challenge_rows"],"model_calls":0}))


if __name__ == "__main__": main()
