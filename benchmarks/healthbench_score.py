"""Score saved HealthBench judgments without making model or API calls.

Usage (run from the repository root)::

    .venv/bin/python benchmarks/healthbench_score.py --data data.jsonl \
        --answers answers.jsonl --judgments judgments.jsonl --out summary.json
    # A generation manifest can fix the selection, including missing answers:
    .venv/bin/python benchmarks/healthbench_score.py --data data.jsonl \
        --answers answers.jsonl --judgments judgments.jsonl \
        --manifest manifest.json --out summary.json

Data: a JSON array, {"examples": [...]}, or JSONL objects, each containing
``prompt_id`` (unique nonempty string), ``prompt`` (nonempty role/content list),
and ``rubrics`` (nonempty list of criterion/points objects). Points must be
finite, nonzero numbers, with a positive total available in each example.
Answers: JSONL objects with ``arm``, ``prompt_id``, ``answer`` (string), and
``status``. Additional metrics are allowed and ignored. completed/complete/
success/ok/length/truncated answers require all rubric judgments. Empty answers
and failed/error/empty records represent known inference failures and receive
zero, including the full positive denominator. blocked means the experiment
could not run: it receives no score and blocks the arm's aggregate.
Judgments: JSONL objects with ``arm``, ``prompt_id``, ``rubric_index`` (zero-based
integer), and ``criteria_met`` (JSON boolean). Optional explanation/metrics
are ignored. Error records, invalid status values, duplicate keys, out-of-range
indices, and judgments without a corresponding gradable answer are rejected.
No unknown judgment is converted into false or removed from the denominator.
Manifest: JSON object with ``selected_prompt_ids`` (unique list of IDs), and
optionally ``arms`` (list of names, list of objects with arm/name, or a mapping
keyed by arm). Without a manifest, selection is the union of answer IDs.

The summary contains identifiers and numerical diagnostics, not prompts,
rubric text, answers, or grader explanations. It is a supplied-judgment result,
not a certified official full-benchmark result. Both raw per-example scores and
unclipped means are retained; clipping occurs only after the final mean.
"""

from __future__ import annotations

import argparse
from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Any


# HealthBench formula and aggregation attribution (OpenAI, MIT):
# https://github.com/openai/simple-evals/blob/main/healthbench_eval.py
# https://github.com/openai/simple-evals/blob/main/LICENSE
SCORING_REFERENCE = "https://github.com/openai/simple-evals/blob/main/healthbench_eval.py"
GRADABLE = {"completed", "complete", "success", "ok", "length", "truncated"}
FAILURES = {"failed", "error", "empty"}
ANSWER_STATUSES = GRADABLE | FAILURES | {"blocked"}


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _objects(values: Any, label: str) -> list[dict]:
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise ValueError(f"{label} must contain JSON objects")
    return values


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number}: expected a JSON object")
        rows.append(row)
    return rows


def read_data(path: Path) -> list[dict]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return read_jsonl(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return read_jsonl(path)
    if isinstance(value, dict) and "examples" in value:
        value = value["examples"]
    elif isinstance(value, dict):
        value = [value]
    return _objects(value, "data")


def _data_index(data: list[dict]) -> dict[str, dict]:
    index = {}
    for row in _objects(data, "data"):
        prompt_id = _name(row.get("prompt_id"), "data.prompt_id")
        if prompt_id in index:
            raise ValueError(f"Duplicate data prompt_id: {prompt_id}")
        prompt = row.get("prompt")
        if not isinstance(prompt, list) or not prompt:
            raise ValueError(f"{prompt_id}: prompt must be a nonempty message list")
        for message in prompt:
            if (not isinstance(message, dict) or not isinstance(message.get("content"), str)
                    or not isinstance(message.get("role"), str)):
                raise ValueError(f"{prompt_id}: invalid prompt message")
        rubrics = row.get("rubrics")
        if not isinstance(rubrics, list) or not rubrics:
            raise ValueError(f"{prompt_id}: rubrics must be a nonempty list")
        points = []
        for rubric in rubrics:
            if not isinstance(rubric, dict):
                raise ValueError(f"{prompt_id}: invalid rubric")
            _name(rubric.get("criterion"), f"{prompt_id}.criterion")
            point = rubric.get("points")
            if isinstance(point, bool) or not isinstance(point, (int, float)):
                raise ValueError(f"{prompt_id}: rubric points must be finite nonzero numbers")
            try:
                point = float(point)
            except OverflowError as exc:
                raise ValueError(f"{prompt_id}: rubric points must be finite nonzero numbers") from exc
            if not math.isfinite(point) or point == 0:
                raise ValueError(f"{prompt_id}: rubric points must be finite nonzero numbers")
            points.append(point)
        try:
            denominator = math.fsum(point for point in points if point > 0)
        except OverflowError as exc:
            raise ValueError(f"{prompt_id}: positive rubric denominator overflow") from exc
        if not math.isfinite(denominator) or denominator <= 0:
            raise ValueError(f"{prompt_id}: positive rubric denominator must be finite and nonzero")
        index[prompt_id] = {"points": points, "denominator": denominator}
    if not index:
        raise ValueError("The dataset is empty")
    return index


def _manifest_selection(manifest: dict | None, data: dict, answers: dict) -> tuple[list[str], str, set[str]]:
    arms = {key[0] for key in answers}
    if manifest is None:
        selected = sorted({key[1] for key in answers})
        source = "answer_prompt_id_union"
    else:
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        ids = manifest.get("selected_prompt_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("manifest.selected_prompt_ids must be a nonempty list")
        selected = [_name(value, "manifest.selected_prompt_ids") for value in ids]
        if len(selected) != len(set(selected)):
            raise ValueError("Duplicate manifest selected_prompt_ids")
        if any(prompt_id not in data for prompt_id in selected):
            raise ValueError("Manifest selects an unknown dataset prompt_id")
        selected_set = set(selected)
        if any(prompt_id not in selected_set for _, prompt_id in answers):
            raise ValueError("Answers include a prompt_id outside the manifest selection")
        declared_arms = manifest.get("arms", [])
        if isinstance(declared_arms, dict):
            declared_arms = list(declared_arms)
        if not isinstance(declared_arms, list):
            raise ValueError("manifest.arms must be a list or mapping")
        names = [_name(value.get("arm", value.get("name")) if isinstance(value, dict) else value,
                       "manifest.arm") for value in declared_arms]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate manifest arms")
        if names and not arms.issubset(set(names)):
            raise ValueError("Answers include an arm outside the manifest")
        arms.update(names)
        source = "generation_manifest"
    if not selected or not arms:
        raise ValueError("No evaluation selection or arms were provided")
    return selected, source, arms


def _answer_index(rows: list[dict], data: dict) -> dict[tuple[str, str], dict]:
    index = {}
    for row in _objects(rows, "answers"):
        arm = _name(row.get("arm"), "answers.arm")
        prompt_id = _name(row.get("prompt_id"), "answers.prompt_id")
        if prompt_id not in data:
            raise ValueError(f"Orphan answer prompt_id: {prompt_id}")
        key = (arm, prompt_id)
        if key in index:
            raise ValueError(f"Duplicate answer: {arm}/{prompt_id}")
        status = row.get("status")
        if not isinstance(status, str) or status not in ANSWER_STATUSES:
            raise ValueError(f"{arm}/{prompt_id}: unknown answer status")
        answer = row.get("answer", "")
        if answer is None and status in FAILURES | {"blocked"}:
            answer = ""
        if not isinstance(answer, str):
            raise ValueError(f"{arm}/{prompt_id}: answer must be a string")
        if "answer" not in row and status in GRADABLE:
            raise ValueError(f"{arm}/{prompt_id}: answer field is required")
        if status == "blocked" and answer.strip():
            raise ValueError(f"{arm}/{prompt_id}: blocked records cannot contain an answer")
        kind = "blocked" if status == "blocked" else (
            "known_inference_failure" if status in FAILURES or not answer.strip() else "gradable")
        index[key] = {"status": status, "kind": kind}
    return index


def _judgment_index(rows: list[dict], data: dict, answers: dict) -> dict[tuple[str, str, int], bool]:
    index = {}
    for row in _objects(rows, "judgments"):
        arm = _name(row.get("arm"), "judgments.arm")
        prompt_id = _name(row.get("prompt_id"), "judgments.prompt_id")
        if prompt_id not in data or (arm, prompt_id) not in answers:
            raise ValueError(f"Orphan judgment: {arm}/{prompt_id}")
        if "error" in row or ("status" in row and (
                not isinstance(row["status"], str) or row["status"] not in GRADABLE - {"length", "truncated"})):
            raise ValueError(f"{arm}/{prompt_id}: error or unsuccessful judgment")
        if answers[(arm, prompt_id)]["kind"] != "gradable":
            raise ValueError(f"{arm}/{prompt_id}: judgment has no gradable completion")
        number = row.get("rubric_index")
        if (isinstance(number, bool) or not isinstance(number, int)
                or not 0 <= number < len(data[prompt_id]["points"])):
            raise ValueError(f"{arm}/{prompt_id}: invalid rubric_index")
        label = row.get("criteria_met")
        if not isinstance(label, bool):
            raise ValueError(f"{arm}/{prompt_id}/{number}: criteria_met must be a JSON boolean")
        key = (arm, prompt_id, number)
        if key in index:
            raise ValueError(f"Duplicate judgment: {arm}/{prompt_id}/{number}")
        index[key] = label
    return index


def _case_score(arm: str, prompt_id: str, data: dict, answers: dict, judgments: dict) -> dict:
    rubric = data[prompt_id]
    answer = answers.get((arm, prompt_id))
    row = {"prompt_id": prompt_id, "answer_status": answer["status"] if answer else None,
           "positive_points_possible": rubric["denominator"], "expected_criteria": len(rubric["points"]),
           "judged_criteria": 0, "missing_rubric_indices": [], "points_earned": None, "score": None}
    if answer is None:
        row["status"] = "missing_answer"
    elif answer["kind"] == "blocked":
        row["status"] = "blocked"
    elif answer["kind"] == "known_inference_failure":
        row.update(status="known_inference_failure", points_earned=0.0, score=0.0)
    else:
        missing = [number for number in range(len(rubric["points"]))
                   if (arm, prompt_id, number) not in judgments]
        row["missing_rubric_indices"] = missing
        row["judged_criteria"] = len(rubric["points"]) - len(missing)
        if missing:
            row["status"] = "incomplete_judgments"
        else:
            numerator = math.fsum(point for number, point in enumerate(rubric["points"])
                                  if judgments[(arm, prompt_id, number)])
            score = numerator / rubric["denominator"]
            if not math.isfinite(score):
                raise ValueError(f"{arm}/{prompt_id}: nonfinite score")
            row.update(status="scored", points_earned=numerator, score=score)
    return row


def _arm_summary(rows: list[dict]) -> dict:
    counts = Counter(row["status"] for row in rows)
    status = "blocked" if counts["blocked"] else (
        "incomplete" if any(row["score"] is None for row in rows) else "complete")
    mean = math.fsum(row["score"] for row in rows) / len(rows) if status == "complete" else None
    score = min(1.0, max(0.0, mean)) if mean is not None else None
    minimum = min(row["score"] for row in rows) if mean is not None else None
    maximum = max(row["score"] for row in rows) if mean is not None else None
    return {"status": status, "selected_count": len(rows), "scored_count": counts["scored"],
            "known_inference_failure_count": counts["known_inference_failure"],
            "blocked_count": counts["blocked"], "missing_answer_count": counts["missing_answer"],
            "incomplete_judgments_count": counts["incomplete_judgments"],
            "raw_mean": mean, "score": score, "score_percent": score * 100 if score is not None else None,
            "raw_min": minimum, "raw_max": maximum,
            "raw_min_prompt_ids": [row["prompt_id"] for row in rows if row["score"] == minimum]
            if minimum is not None else [],
            "raw_max_prompt_ids": [row["prompt_id"] for row in rows if row["score"] == maximum]
            if maximum is not None else [], "cases": rows}


def _pair_summary(left: str, right: str, arms: dict, selected: list[str]) -> dict:
    first = {row["prompt_id"]: row for row in arms[left]["cases"]}
    second = {row["prompt_id"]: row for row in arms[right]["cases"]}
    pairs = [(first[key]["score"], second[key]["score"]) for key in selected]
    available = [(a, b) for a, b in pairs if a is not None and b is not None]
    complete = arms[left]["status"] == arms[right]["status"] == "complete"
    status = "complete" if complete else (
        "blocked" if "blocked" in {arms[left]["status"], arms[right]["status"]} else "incomplete")
    deltas = [a - b for a, b in available]
    ties = sum(math.isclose(delta, 0.0, abs_tol=1e-12) for delta in deltas)
    wins = sum(delta > 0 and not math.isclose(delta, 0.0, abs_tol=1e-12) for delta in deltas)
    cases = []
    for key in selected:
        a, b = first[key]["score"], second[key]["score"]
        delta = a - b if a is not None and b is not None else None
        outcome = ("tie" if math.isclose(delta, 0.0, abs_tol=1e-12) else "win" if delta > 0 else "loss") \
            if delta is not None else (
                "blocked" if "blocked" in {first[key]["status"], second[key]["status"]} else "incomplete")
        cases.append({"prompt_id": key, "left_score": a, "right_score": b,
                      "raw_delta": delta, "outcome": outcome})
    return {"left_arm": left, "right_arm": right, "status": status,
            "delta_direction": "left_minus_right", "expected_pairs": len(selected),
            "scored_pairs": len(available), "missing_pairs": len(selected) - len(available),
            "wins": wins if complete else None,
            "losses": len(deltas) - wins - ties if complete else None, "ties": ties if complete else None,
            "paired_delta": math.fsum(deltas) / len(deltas) if complete else None,
            "raw_delta_min": min(deltas) if complete else None,
            "raw_delta_max": max(deltas) if complete else None, "cases": cases}


def build_summary(data_rows: list[dict], answer_rows: list[dict], judgment_rows: list[dict],
                  manifest: dict | None = None) -> dict:
    data = _data_index(data_rows)
    answers = _answer_index(answer_rows, data)
    selected, selection_source, arm_names = _manifest_selection(manifest, data, answers)
    judgments = _judgment_index(judgment_rows, data, answers)
    arms = {arm: _arm_summary([_case_score(arm, key, data, answers, judgments) for key in selected])
            for arm in sorted(arm_names)}
    status = "blocked" if any(arm["status"] == "blocked" for arm in arms.values()) else (
        "complete" if all(arm["status"] == "complete" for arm in arms.values()) else "incomplete")
    return {"schema_version": 1, "status": status, "evaluation": "saved_healthbench_rubric_judgments",
            "official_full_benchmark": False,
            "notice": "Supplied-judgment diagnostic; a small selected sample is not an official full HealthBench score.",
            "scoring_reference": SCORING_REFERENCE, "dataset_count": len(data), "selected_count": len(selected),
            "selection_source": selection_source, "selected_prompt_ids": selected,
            "covers_entire_input_dataset": len(selected) == len(data),
            "score_rule": "sum(points for criteria_met) / sum(all positive points); mean, then clip to [0,1]",
            "inference_failure_rule": "failed/error/empty completions receive zero; blocked experiments receive no score",
            "arms": arms, "pairs": [_pair_summary(left, right, arms, selected)
                                      for left, right in combinations(sorted(arms), 2)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
        summary = build_summary(read_data(args.data), read_jsonl(args.answers), read_jsonl(args.judgments), manifest)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"HealthBench scoring failed: {exc}\n")
    print(f"Saved {summary['status']} summary: {args.out}")


if __name__ == "__main__":
    main()
