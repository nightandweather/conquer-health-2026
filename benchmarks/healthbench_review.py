"""Prepare private, blinded HealthBench review packets without grading or API calls.

Run from the repository root::

    .venv/bin/python benchmarks/healthbench_review.py --data data.json \
        --answers results/run/answers.jsonl --manifest results/run/manifest.json \
        --out results/run/review

Only nonempty completed/truncated raw_default and grounded_call pairs become
packets. The original prompt and rubric criteria belong in private packets;
arm identities and inference metrics do not. Keep the private_manifest.json
mapping away from reviewers. Repeating the command adds newly completed pairs
and refuses changes to previously packaged content. No score is produced.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

if __package__:
    from .healthbench import canonical_hash, load_dataset
    from .healthbench_score import _answer_index, _data_index, _manifest_selection
else:
    from healthbench import canonical_hash, load_dataset
    from healthbench_score import _answer_index, _data_index, _manifest_selection


ROOT = Path(__file__).resolve().parents[1]
PAIRED_ARMS = ("raw_default", "grounded_call")
REVIEWABLE = {"completed", "truncated"}
REVIEWER_COUNT = 4


def _json_object(raw: bytes, label: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _answer_rows(raw: bytes) -> list[dict]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError("answers is not valid UTF-8") from exc
    return [_json_object(line.encode("utf-8"), f"answers line {number}")
            for number, line in enumerate(lines, 1) if line.strip()]


def _output_path(out: Path) -> Path:
    private_root = (ROOT / "results").resolve()
    absolute = Path(os.path.abspath(out))
    try:
        resolved = absolute.resolve()
        resolved.relative_to(private_root)
    except (ValueError, RuntimeError) as exc:
        raise ValueError("--out must be inside this repository's private results/ directory") from exc
    if resolved == private_root:
        raise ValueError("--out must name a dedicated directory inside results/")
    # Reject symlink aliases even if they happen to resolve within results/.
    for part in (absolute, *absolute.parents):
        if part.is_symlink():
            raise ValueError("Review output cannot use symlinks")
        if part == private_root:
            break
    return resolved


def _read_existing(path: Path) -> dict | None:
    if path.is_symlink():
        raise ValueError("Review artifacts cannot be symlinks")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{path.name} must be a regular file")
    return _json_object(path.read_bytes(), path.name)


def _atomic_json(path: Path, value: dict, *, replace: bool) -> None:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Linking a completed temporary file publishes it atomically and
            # exclusively; an existing immutable packet is never overwritten.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(prompt_id: str, dataset_sha256: str) -> str:
    return canonical_hash([prompt_id, dataset_sha256])


def _packet(item: dict, rows: dict, dataset_sha256: str, reviewer_index: int) -> tuple[dict, dict]:
    packet_id = _identity(item["prompt_id"], dataset_sha256)
    order = PAIRED_ARMS if int(packet_id[-1], 16) % 2 == 0 else tuple(reversed(PAIRED_ARMS))
    mapping = dict(zip(("A", "B"), order))
    candidates = {label: {"answer": rows[(arm, item["prompt_id"])]["answer"],
                          "status": rows[(arm, item["prompt_id"])]["status"]}
                  for label, arm in mapping.items()}
    payload = {"schema_version": 1, "packet_id": packet_id, "prompt_id": item["prompt_id"],
               "prompt": deepcopy(item["prompt"]),
               "rubrics": [{"rubric_index": index, "criterion": rubric["criterion"],
                            "points": rubric["points"]}
                           for index, rubric in enumerate(item["rubrics"])],
               "candidates": candidates}
    packet = payload | {"content_sha256": canonical_hash(payload)}
    private = {"prompt_id": item["prompt_id"], "prompt_sha256": canonical_hash(item["prompt"]),
               "packet_filename": packet_id + ".json", "packet_sha256": canonical_hash(packet),
               "candidate_map": mapping,
               "candidate_sha256": {label: canonical_hash(value) for label, value in candidates.items()},
               "reviewer_index": reviewer_index}
    return packet, private


def create_review_packets(data: Path, answers: Path, manifest: Path, out: Path) -> dict:
    """Validate a generation snapshot, then append immutable blinded packets.

    Validation of every input and existing packet completes before any output
    writes. Each packet retains the SHA of the first whole answers snapshot in
    which it was created; later appended records do not change that provenance.
    Invoke serially when generation is in progress; retry a partial JSONL read.
    """
    out = _output_path(Path(out))
    items, dataset = load_dataset(Path(data), limit=0)
    _data_index(items)  # Validate finite signed points and positive denominators.
    data_rows = {item["prompt_id"]: item for item in items}
    manifest_value = _json_object(Path(manifest).read_bytes(), "manifest")
    snapshot = Path(answers).read_bytes()
    snapshot_sha = hashlib.sha256(snapshot).hexdigest()
    rows = _answer_rows(snapshot)
    answer_kinds = _answer_index(rows, data_rows)
    selected, _, arms = _manifest_selection(manifest_value, data_rows, answer_kinds)
    if arms != set(PAIRED_ARMS):
        raise ValueError("Manifest and answers must declare exactly raw_default and grounded_call")
    if "selected_arms" in manifest_value:
        declared = manifest_value["selected_arms"]
        if (not isinstance(declared, list) or len(declared) != 2
                or any(not isinstance(arm, str) for arm in declared) or set(declared) != set(PAIRED_ARMS)):
            raise ValueError("manifest.selected_arms must declare exactly the paired arms")
    generation_data = manifest_value.get("dataset")
    if not isinstance(generation_data, dict) or generation_data.get("sha256") != dataset["sha256"]:
        raise ValueError("Manifest dataset SHA does not match the dataset file")
    selected = sorted(selected)
    selected_set = set(selected)
    if "selected_prompt_ids" in generation_data:
        dataset_selection = generation_data["selected_prompt_ids"]
        if (not isinstance(dataset_selection, list)
                or any(not isinstance(prompt_id, str) for prompt_id in dataset_selection)
                or sorted(dataset_selection) != selected):
            raise ValueError("Manifest dataset selected IDs disagree with the selection")
    prompt_hashes = generation_data.get("selected_prompt_sha256")
    expected_hashes = {prompt_id: canonical_hash(data_rows[prompt_id]["prompt"]) for prompt_id in selected}
    if prompt_hashes != expected_hashes:
        raise ValueError("Manifest selected prompt SHAs do not match the dataset prompts")
    row_index = {}
    for row in rows:
        if row.get("arm") not in PAIRED_ARMS or row["prompt_id"] not in selected_set:
            raise ValueError("Answers include an arm or ID outside the paired selection")
        if row.get("dataset_sha256") != dataset["sha256"]:
            raise ValueError("Answer dataset SHA does not match the dataset file")
        if row.get("prompt_sha256") != expected_hashes[row["prompt_id"]]:
            raise ValueError("Answer prompt SHA does not match its dataset prompt")
        row_index[(row["arm"], row["prompt_id"])] = row

    scope = {"dataset_sha256": dataset["sha256"], "selected_prompt_ids": selected,
             "selected_prompt_sha256": expected_hashes, "arms": manifest_value.get("arms")}
    expected_manifest = {"schema_version": 1, "review_kind": "blinded qualitative rubric review; ungraded",
                         "dataset_sha256": dataset["sha256"], "generation_scope_sha256": canonical_hash(scope),
                         "selected_prompt_ids": selected, "reviewer_count": REVIEWER_COUNT,
                         "reviewer_assignment": "sorted original manifest index modulo 4", "packets": {}}
    previous = _read_existing(out / "private_manifest.json")
    if previous is not None:
        if any(previous.get(key) != value for key, value in expected_manifest.items() if key != "packets"):
            raise ValueError("Existing review manifest belongs to a different generation selection or configuration")
        entries = previous.get("packets")
        if not isinstance(entries, dict):
            raise ValueError("Existing review packet mapping is invalid")
    else:
        entries = {}
    packets_dir = out / "packets"
    if packets_dir.is_symlink() or (packets_dir.exists() and not packets_dir.is_dir()):
        raise ValueError("packets must be a regular directory")

    planned, pending = {}, []
    for index, prompt_id in enumerate(selected):
        unavailable = []
        for arm in PAIRED_ARMS:
            row = row_index.get((arm, prompt_id))
            if row is None:
                unavailable.append({"arm": arm, "reason": "missing_answer"})
            else:
                text = row.get("answer")
                nonempty = isinstance(text, str) and bool(text.strip())
                if row["status"] not in REVIEWABLE or not nonempty:
                    unavailable.append({"arm": arm, "reason": "empty_answer" if not nonempty
                                        else "status_not_reviewable", "status": row["status"]})
        if unavailable:
            pending.append({"prompt_id": prompt_id, "unavailable": unavailable})
            continue
        packet, private = _packet(data_rows[prompt_id], row_index, dataset["sha256"], index % REVIEWER_COUNT)
        planned[packet["packet_id"]] = (packet, private)

    # Refuse all candidate/content mutations before creating directories/files.
    for packet_id, entry in entries.items():
        if packet_id not in planned or not isinstance(entry, dict):
            raise ValueError("A previously packaged pair is missing, changed, or no longer reviewable")
        packet, private = planned[packet_id]
        if any(entry.get(key) != value for key, value in private.items()):
            raise ValueError("A previously packaged candidate or mapping changed")
        first_sha = entry.get("first_source_answers_sha256")
        if (not isinstance(first_sha, str) or len(first_sha) != 64
                or any(character not in "0123456789abcdef" for character in first_sha)):
            raise ValueError("Existing first answers snapshot SHA is invalid")
        entry_content = {key: value for key, value in entry.items() if key != "entry_sha256"}
        if entry.get("entry_sha256") != canonical_hash(entry_content):
            raise ValueError("Existing private packet metadata changed")
        saved = _read_existing(packets_dir / private["packet_filename"])
        if saved is None or canonical_hash(saved) != private["packet_sha256"]:
            raise ValueError("A previously packaged review file is missing or changed")
    if packets_dir.exists():
        allowed = {entry["packet_filename"] for entry in entries.values()}
        if any(path.name not in allowed for path in packets_dir.iterdir()):
            raise ValueError("Unmapped review files exist; use an empty dedicated output directory")
    for packet_id, (_, private) in planned.items():
        path = packets_dir / private["packet_filename"]
        if packet_id not in entries and (path.exists() or path.is_symlink()):
            raise ValueError("A new packet path already exists without a mapping")
    old_progress = _read_existing(out / "progress.json")
    progress = {"schema_version": 1, "status": "ready" if not pending else "pending_answers",
                "selected_count": len(selected), "expected_answer_count": len(selected) * len(PAIRED_ARMS),
                "observed_answer_count": len(rows), "ready_packet_count": len(planned),
                "new_packet_count": len(set(planned) - set(entries)), "pending_count": len(pending),
                "answers_snapshot_sha256": snapshot_sha, "pending": pending, "grading_status": "not_graded"}

    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    packets_dir.mkdir(exist_ok=True)
    packets_dir.chmod(0o700)
    new_entries = deepcopy(entries)
    for packet_id, (packet, private) in planned.items():
        if packet_id not in entries:
            _atomic_json(packets_dir / private["packet_filename"], packet, replace=False)
            entry = private | {"first_source_answers_sha256": snapshot_sha}
            new_entries[packet_id] = entry | {"entry_sha256": canonical_hash(entry)}
    private_manifest = expected_manifest | {"packets": new_entries}
    if private_manifest != previous:
        _atomic_json(out / "private_manifest.json", private_manifest, replace=True)
    if progress != old_progress:
        _atomic_json(out / "progress.json", progress, replace=True)
    return progress


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("data", "answers", "manifest", "out"):
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        progress = create_review_packets(args.data, args.answers, args.manifest, args.out)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Review packets: {progress['ready_packet_count']}/{progress['selected_count']} ready; "
          f"{progress['new_packet_count']} added; {progress['pending_count']} pending; not graded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
