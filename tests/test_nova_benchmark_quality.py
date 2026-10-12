"""Synthetic parser, leakage, provenance and privacy checks; no model or HTTP."""
import csv
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from benchmarks import nova_benchmark_quality as quality


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidences = {"E_1":{"possible-values":[],"data_type":"B"},
                          "E_2":{"possible-values":["V_1",2],"data_type":"M"}}
        self.conditions = {"SyntheticOne":{},"SyntheticTwo":{}}
        self.row = {"AGE":"42","SEX":"F","INITIAL_EVIDENCE":"E_1",
            "EVIDENCES":"['E_1', 'E_2_@_V_1']","PATHOLOGY":"SyntheticOne",
            "DIFFERENTIAL_DIAGNOSIS":"[['SyntheticOne', 0.75], ['SyntheticTwo', 0.25]]"}

    def test_normalization_is_unicode_whitespace_not_clinical_equivalence(self):
        self.assertEqual(quality.normalize("  Ａ\n b  "),"a b")
        self.assertNotEqual(quality.normalize("drug-one"),quality.normalize("drug-two"))
        self.assertNotEqual(quality.normalize("2 mg"),quality.normalize("20 mg"))

    def test_duplicate_denominators_count_all_affected_and_excess(self):
        result = quality.duplicates([1,1,1,2,3,3])
        self.assertEqual((result["groups"],result["excess_rows"]),(2,3))
        self.assertEqual(result["affected_rows"],quality.ratio(5,6))

    def test_overlap_counts_multiplicities_without_claiming_unique_patients(self):
        result = quality.overlap([1,1,2],[1,3,1,1])
        self.assertEqual(result["shared_distinct_signatures"],1)
        self.assertEqual(result["left_affected_rows"],quality.ratio(2,3))
        self.assertEqual(result["right_affected_rows"],quality.ratio(3,4))

    def test_input_conflict_generator_is_flag_not_clinical_truth(self):
        result = quality.contradictory_inputs(iter([(1,1),(1,2),(2,1)]))
        self.assertEqual(result["same_normalized_input_different_label_groups"],1)
        self.assertFalse(result["clinical_contradiction_confirmed"])
        self.assertEqual(result["affected_rows"],quality.ratio(2,3))

    def test_valid_ddx_and_precomputed_allowed_values_agree(self):
        a, parsed = quality.ddx_checks(self.row,self.evidences,self.conditions)
        b, _ = quality.ddx_checks(self.row,self.evidences,self.conditions,{"E_1":set(),"E_2":{"V_1","2"}})
        self.assertFalse(a); self.assertEqual(a,b); self.assertEqual(parsed["age"],42)

    def test_ddx_unknown_evidence_and_outside_value_are_separate(self):
        row = dict(self.row,EVIDENCES="['E_1', 'E_7', 'E_2_@_V_7']")
        errors,_ = quality.ddx_checks(row,self.evidences,self.conditions)
        self.assertEqual(errors["unknown_evidence_code_rows"],1)
        self.assertEqual(errors["answer_outside_allowed_evidence_values_rows"],1)

    def test_initial_symptom_must_be_present_and_known(self):
        row = dict(self.row,INITIAL_EVIDENCE="E_9")
        errors,_ = quality.ddx_checks(row,self.evidences,self.conditions)
        self.assertEqual(errors["initial_evidence_not_in_patient_evidences"],1)
        self.assertEqual(errors["unknown_initial_evidence_code"],1)

    def test_literal_parser_rejects_executable_and_wrong_types(self):
        for value in ("__import__('os').system('unexpected')", "{'E_1':1}","[42]"):
            errors,_ = quality.ddx_checks(dict(self.row,EVIDENCES=value),self.evidences,self.conditions)
            self.assertEqual(errors["evidences_parse_or_type_failed"],1)

    def test_probability_sum_bounds_and_ground_truth_membership(self):
        row = dict(self.row,DIFFERENTIAL_DIAGNOSIS="[['SyntheticTwo', 1.1]]")
        errors,_ = quality.ddx_checks(row,self.evidences,self.conditions)
        self.assertEqual(errors["invalid_differential_probability_rows"],1)
        self.assertEqual(errors["differential_probability_sum_not_one"],1)
        self.assertEqual(errors["ground_truth_absent_from_differential"],1)

    def test_age_sex_and_label_validation(self):
        errors,_ = quality.ddx_checks(dict(self.row,AGE="-1",SEX="unknown",PATHOLOGY="not-in-dictionary"),self.evidences,self.conditions)
        self.assertEqual(errors["age_outside_0_120"],1)
        self.assertEqual(errors["sex_not_M_F"],1)
        self.assertEqual(errors["pathology_not_in_dictionary"],1)

    def test_zip_rejects_traversal_symlink_backslash_and_size(self):
        for name in ("../escape","/absolute","folder\\escape"):
            with self.assertRaises(ValueError):quality.safe_zip_member(zipfile.ZipInfo(name))
        member=zipfile.ZipInfo("regular");member.external_attr=0o120777<<16
        with self.assertRaises(ValueError):quality.safe_zip_member(member)
        member=zipfile.ZipInfo("large");member.file_size=quality.UNCOMPRESSED_BYTE_LIMIT+1
        with self.assertRaises(ValueError):quality.safe_zip_member(member)

    def test_ddx_streams_all_splits_crc_and_deletes_hash_scratch(self):
        root = self.root/"DDXPlus";root.mkdir()
        (root/"release_evidences.json").write_text(json.dumps(self.evidences))
        (root/"release_conditions.json").write_text(json.dumps(self.conditions))
        for split in ("train","validate","test"):
            path=self.root/"one.csv"
            with path.open("w",newline="") as stream:
                writer=csv.DictWriter(stream,fieldnames=sorted(self.row));writer.writeheader();writer.writerow(self.row)
            with zipfile.ZipFile(root/f"release_{split}_patients.zip","w",compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(path,arcname=f"release_{split}_patients")
        scratch=self.root/"scratch.sqlite"
        result=quality.audit_ddx(root,scratch)
        self.assertEqual(result["parsed_rows"],3)
        self.assertEqual(result["cross_split_overlap"]["train:test"]["exact"]["shared_distinct_signatures"],1)
        self.assertFalse(result["nested_CSVs_extracted"]);self.assertFalse(scratch.exists())
        self.assertNotIn("SyntheticOne",json.dumps(result))

    def test_agentclinic_optional_tests_and_superset_are_not_independent_split(self):
        root=self.root/"AgentClinic";root.mkdir()
        a={"OSCE_Examination":{"Objective_for_Doctor":"SYNTHETIC_SECRET_QUESTION", "Patient_Actor":{"History":"SYNTHETIC_SECRET_PATIENT"},
            "Physical_Examination_Findings":{"vital":"SYNTHETIC_SECRET_VALUE"},"Correct_Diagnosis":"SYNTHETIC_SECRET_LABEL"}}
        for name,n in (("agentclinic_medqa.jsonl",1),("agentclinic_medqa_extended.jsonl",2)):
            (root/name).write_text("\n".join(json.dumps(a) for _ in range(n))+"\n")
        result=quality.audit_agentclinic(root)
        self.assertEqual(result["base_extended_overlap"]["exact"]["shared_distinct_signatures"],1)
        self.assertEqual(result["agentclinic_medqa_extended.jsonl"]["missing_or_empty"]["Test_Results_optional"],quality.ratio(2,2))
        self.assertFalse(result["agentclinic_medqa_extended.jsonl"]["published_split_present"])
        self.assertNotIn("SYNTHETIC_SECRET",json.dumps(result))

    def test_source_extracted_mutation_rejected_even_if_archive_pin_valid(self):
        assets=self.root/"assets";assets.mkdir()
        root=assets/"extracted/nova-benchmarks-2026/nova-benchmarks-2026";root.mkdir(parents=True)
        (root/"README.md").write_text("changed")
        path=assets/"nova-benchmarks-2026.zip"
        with zipfile.ZipFile(path,"w") as archive:archive.writestr("nova-benchmarks-2026/README.md","original")
        with patch.object(quality,"BENCHMARK_ARCHIVE_SHA256",quality.file_pin(path)["sha256"]):
            with self.assertRaisesRegex(ValueError,"differs from official"):quality.verify_bundle(assets)

    def test_aci_note_hash_catches_repeated_case_despite_disjoint_id_namespaces(self):
        root=self.root/"ACI"; challenge=root/"challenge_data"; variants=root/"src_experiment_data"
        challenge.mkdir(parents=True); variants.mkdir()
        def write(path, columns, rows):
            with path.open("w",newline="") as stream:
                writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(rows)
        for label in ("train","valid","test1","test2","test3"):
            write(challenge/f"{label}.csv",["encounter_id","dialogue","note"],[{"encounter_id":f"challenge-{label}","dialogue":"[doctor] hello [patient] hi", "note":f"synthetic private note {label}"}])
            write(challenge/f"{label}_metadata.csv",["encounter_id"],[{"encounter_id":f"challenge-{label}"}])
        for variation in ("asr","asrcorr"):
            write(variants/f"train_aci_{variation}.csv",["id","dialogue","note"],[{"id":"unrelated-id-namespace","dialogue":f"synthetic private dialogue {variation}","note":"synthetic private note train"}])
        result=quality.audit_aci(root); linkage=result["latent_variant_linkage"]
        self.assertEqual(linkage["all_experiment_rows_matching_challenge"],quality.ratio(2,2))
        self.assertEqual(linkage["matched_challenge_cases"],1)
        self.assertEqual(linkage["variant_rows_per_matched_note_signature_distribution"],{"2":1})
        self.assertEqual(linkage["same_experiment_id_multiple_normalized_transcripts_groups"],1)
        self.assertEqual(linkage["published_split_mismatch_rows"],0)
        self.assertFalse(linkage["note_hash_is_confirmed_patient_identity"])
        self.assertNotIn("synthetic private note train",json.dumps(result))

    def test_archive_pin_invalid_rejected_without_data_audit(self):
        path=self.root/"nova-benchmarks-2026.zip";path.write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError,"fixed source"):quality.verify_bundle(self.root)

    def test_output_must_be_new_immutable_folder(self):
        existing=self.root/"existing";existing.mkdir()
        with self.assertRaises(FileExistsError):quality.run_audit(self.root,existing)

    def test_sql_counts_match_independent_python_counts(self):
        db=sqlite3.connect(":memory:");self.addCleanup(db.close)
        db.execute("CREATE TABLE signatures(split TEXT, exact BLOB, normalized BLOB, input BLOB, label BLOB)")
        rows=[("a",b"1",b"1",b"1",b"a"),("a",b"1",b"1",b"1",b"a"),("b",b"1",b"1",b"1",b"a"),("b",b"2",b"2",b"2",b"b")]
        db.executemany("INSERT INTO signatures VALUES(?,?,?,?,?)",rows)
        self.assertEqual(quality.sql_duplicates(db,"exact","a"),quality.duplicates([b"1",b"1"]))
        self.assertEqual(quality.sql_overlap(db,"exact","a","b"),quality.overlap([b"1",b"1"],[b"1",b"2"]))
        with self.assertRaises(ValueError):quality.sql_duplicates(db,"exact; DROP TABLE signatures")


if __name__=="__main__":unittest.main()
