"""Validate private HealthBench dev-only four-axis annotations and aggregate.

Only a previously exported development-input file is accepted. No reference
rubrics, generated responses, or held-out examples are opened by this module.
Semantic labels must be provided in a separate, pinned annotation artifact.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_digest(prompt: list[dict]) -> str:
    return digest(json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def summarize(input_path: Path, annotation_path: Path) -> dict:
    source_bytes = input_path.read_bytes()
    source = json.loads(source_bytes)
    annotations = json.loads(annotation_path.read_text())
    if annotations["input_sha256"] != digest(source_bytes):
        raise ValueError("Selected input pin changed")
    rows = source["rows"]
    labels = annotations["rows"]
    allowed = {f"HB-V{i:04d}" for i in range(20)}
    if len(rows) != 20 or {r["case_id"] for r in rows} != allowed:
        raise ValueError("Exact existing dev20 boundary required")
    if len(labels) != 20 or {r["case_id"] for r in labels} != allowed:
        raise ValueError("Exact one-annotation-per-dev-case boundary required")
    selected = {r["case_id"]: r for r in rows}
    codebook = annotations["codebook"]
    sets = {axis: {entry["code"] for entry in entries} for axis, entries in codebook.items()}
    if any(len(sets[axis]) != len(entries) for axis, entries in codebook.items()):
        raise ValueError("Duplicate codebook entries")
    for label in labels:
        row = selected[label["case_id"]]
        if set(row) != {"case_id", "prompt"}:
            raise ValueError("Only input prompt fields permitted")
        if any(set(m) != {"role", "content"} or m["role"] not in {"user", "assistant"}
               or not isinstance(m["content"], str) for m in row["prompt"]):
            raise ValueError("Invalid input message")
        if not row["prompt"] or row["prompt"][-1]["role"] != "user":
            raise ValueError("Final message must be a user request")
        if label["prompt_sha256"] != prompt_digest(row["prompt"]):
            raise ValueError("Prompt pin changed")
        if label["final_user_text"] != row["prompt"][-1]["content"]:
            raise ValueError("Final user text changed")
        for axis in ("purpose", "topic"):
            if label[axis] not in sets[axis]:
                raise ValueError("Unknown semantic label")
        if len(set(label["cues"])) != len(label["cues"]) or not set(label["cues"]) <= sets["cues"]:
            raise ValueError("Unknown or repeated cue")
        flow = "assistant_history_present" if any(m["role"] == "assistant" for m in row["prompt"]) else "single_user_input"
        if label["flow"] != flow or not label["rationale"].strip():
            raise ValueError("Flow mismatch or missing rationale")
    n = len(rows)
    counts = {axis: {entry["code"]: sum(label[axis] == entry["code"] for label in labels)
                     for entry in codebook[axis]} for axis in ("purpose", "topic", "flow")}
    cue_counts = {entry["code"]: sum(entry["code"] in label["cues"] for label in labels)
                  for entry in codebook["cues"]}
    if any(sum(value.values()) != n for value in counts.values()):
        raise ValueError("Exclusive-axis denominator does not reconcile")
    return {
        "schema_version": 1,
        "scope": "Previously used HealthBench dev20 input prompts only; not the 180-case evaluation split",
        "unit": "One case / final user request; original assistant context retained for flow and source-attribution cues",
        "input_sha256": digest(source_bytes),
        "source_file_sha256": source["source_sha256"],
        "annotations_sha256": digest(annotation_path.read_bytes()),
        "cases": n,
        "method": "Codex manual census for purpose/topic/cues; deterministic role counts for flow; not independently adjudicated clinical labels",
        "codebook": codebook,
        "exclusive_counts": counts,
        "exclusive_percent": {axis: {code: round(100 * count / n, 2) for code, count in value.items()}
                              for axis, value in counts.items()},
        "multi_label_cue_counts": cue_counts,
        "multi_label_cue_percent": {code: round(100 * count / n, 2) for code, count in cue_counts.items()},
        "cases_with_any_cue": sum(bool(label["cues"]) for label in labels),
        "input_message_roles": dict(sorted(Counter(m["role"] for row in rows for m in row["prompt"]).items())),
        "input_message_count_histogram": dict(sorted(Counter(str(len(row["prompt"])) for row in rows).items())),
        "limitations": [
            "These cases were already used in development. They are neither new independent evaluation data nor a representative HealthBench sample.",
            "User is a role label, not proof that the speaker is the patient. Documentation and clinician-style requests remain in the denominator.",
            "Purpose uses the final user's intended request, including requests without a question mark. It is not directly comparable to question-mark-turn counts in ACI.",
            "Cue labels identify review attention points. They are not verified clinical errors, trap questions, difficulty labels, or medical recommendations.",
            "Multiple cues may apply to one case; cue percentages may exceed 100 in sum. Topics and purposes have one representative label each.",
            "Original assistant history is input context, not a new candidate answer and not verified patient evidence.",
        ],
        "model_api_calls": 0,
        "generated_answers_read": 0,
        "reference_rubrics_read": 0,
        "heldout_individual_contents_analyzed": 0,
    }


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
