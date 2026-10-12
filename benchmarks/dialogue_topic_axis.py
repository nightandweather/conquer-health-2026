"""Validate private chief-complaint annotations and emit aggregate counts.

This module contains no case content or semantic classifier. Classification is
an explicit human/Codex annotation artifact supplied separately. It reads only
the selected train input; it never reads diagnosis/clinical-note answer files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from statistics import median


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def secondary_metadata(value: str) -> tuple[str, int | None]:
    """Blank is unknown; literal none is explicitly zero, never inferred."""
    if not isinstance(value, str):
        raise ValueError("Secondary metadata must be a string")
    if not value.strip():
        return "blank_unknown", None
    if value.strip().casefold() == "none":
        return "explicit_none", 0
    items = [part.strip() for part in value.split(";") if part.strip()]
    return "recorded_items", len(items)


def _counts(values) -> dict:
    return dict(sorted(Counter(values).items()))


def summarize(input_path: Path, annotation_path: Path) -> dict:
    raw = input_path.read_bytes()
    selected = json.loads(raw)
    annotation = json.loads(annotation_path.read_text())
    if annotation["input_sha256"] != sha256(raw):
        raise ValueError("Selected input pin changed")
    codebook = annotation["codebook"]
    codes = [row["code"] for row in codebook]
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate topic code")
    by_id = {row["case_id"]: row for row in selected}
    labels = annotation["rows"]
    if len(by_id) != len(selected) or len(labels) != len(selected):
        raise ValueError("Duplicate input IDs or incomplete annotations")
    if {r["case_id"] for r in labels} != set(by_id):
        raise ValueError("Annotation IDs do not match selected input")
    if len({r["case_id"] for r in labels}) != len(labels):
        raise ValueError("Duplicate annotation IDs")
    for label in labels:
        source = by_id[label["case_id"]]
        if set(source) != {"case_id", "source_id", "dataset", "dialogue_sha256", "cc", "secondary_complaints", "age", "sex", "turns"}:
            raise ValueError("Only selected train metadata and input turns are permitted")
        if not source["case_id"].startswith("ACI-TRAIN-"):
            raise ValueError("Only selected ACI train inputs are supported")
        for key in ("source_id", "dataset", "cc"):
            if label[key] != source[key]:
                raise ValueError("Source metadata changed")
        if label["cc_sha256"] != sha256(source["cc"].encode()):
            raise ValueError("Chief complaint pin changed")
        if label["primary_topic"] not in codes or not label["rationale"].strip():
            raise ValueError("Invalid or unexplained topic")

    def group_summary(rows: list[dict]) -> dict:
        labels_here = [r for r in labels if r["case_id"] in {s["case_id"] for s in rows}]
        n = len(rows)
        topics = Counter(r["primary_topic"] for r in labels_here)
        secondary = [secondary_metadata(r["secondary_complaints"]) for r in rows]
        ages = []
        age_missing = age_invalid = 0
        for r in rows:
            if not str(r["age"]).strip():
                age_missing += 1
                continue
            try:
                age = float(r["age"])
                if not math.isfinite(age) or age < 0 or age > 130:
                    raise ValueError("Age outside accepted metadata range")
                ages.append(age)
            except (ValueError, TypeError):
                age_invalid += 1
        return {
            "encounters": n,
            "chief_complaint_nonblank": sum(bool(r["cc"].strip()) for r in rows),
            "chief_complaint_blank": sum(not r["cc"].strip() for r in rows),
            "classified": len(labels_here),
            "primary_topic_counts": {code: topics[code] for code in codes},
            "primary_topic_percent": {code: round(100 * topics[code] / n, 2) if n else None for code in codes},
            "secondary_metadata_status": _counts(state for state, _ in secondary),
            "secondary_item_count_known_only": _counts(str(count) for _, count in secondary if count is not None),
            "secondary_item_count_unknown": sum(count is None for _, count in secondary),
            "secondary_recorded_at_least_two_items": sum(count is not None and count >= 2 for _, count in secondary),
            "age_metadata": {"known": len(ages), "blank": age_missing, "invalid": age_invalid,
                             "min": min(ages) if ages else None, "max": max(ages) if ages else None,
                             "median": median(ages) if ages else None,
                             "bands": _counts("under18" if a < 18 else "18to39" if a < 40 else "40to64" if a < 65 else "65plus" for a in ages)},
            "sex_metadata": _counts(str(r["sex"]).strip().casefold() or "blank_unknown" for r in rows),
        }

    result = {
        "schema_version": 1,
        "scope": "ACI-Bench selected train dialogues only",
        "axis": "Chief-complaint-based representative topic; not diagnosis distribution",
        "annotation_method": "Codex manual census of chief-complaint metadata; not an independently adjudicated clinical label",
        "denominator": "One primary topic per selected encounter, including nonspecific complaints",
        "input_sha256": sha256(raw),
        "annotations_sha256": sha256(annotation_path.read_bytes()),
        "codebook": codebook,
        "overall": group_summary(selected),
        "by_source": {source: group_summary([r for r in selected if r["dataset"] == source])
                      for source in sorted({r["dataset"] for r in selected})},
        "limitations": [
            "Labels describe provided chief-complaint wording, not verified diagnoses or an exhaustive dialogue topic inventory.",
            "Secondary field mixes symptoms, diseases, history and procedures; semicolon items are not distinct diseases or distinct clinical topics.",
            "Blank secondary fields are unknown rather than no comorbidity. Age/sex are unvalidated source metadata.",
            "A body-part-only chief complaint remains unspecified; later dialogue/clinical-note answers are not used to infer its topic.",
            "No holdout inputs, notes, reference answers or model-generation calls were used.",
        ],
        "model_api_calls": 0,
    }
    if sum(result["overall"]["primary_topic_counts"].values()) != len(selected):
        raise ValueError("Topic denominator does not reconcile")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.input, args.annotations)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
