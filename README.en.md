# Conquer Health 2026 — Benchmark Award

**Medical Science Foundation Model Hackathon** · Lunit · Aug 21–22, 2026
Team **AIM** — Juhee Park · Jiwoo Park · Kyunseung Ahn · Jangwon Lee · Kanghoun Lee

HealthBench Consensus **52.88** · **1st place out of 18 teams** · no fine-tuning

```
32.44  L2 raw, all harness layers removed
39.26  + temperature: 0                              (+6.8)
43.07  + instructions moved into the user turn       (+3.8)
49.72  + thinking ON, fallback on corrupted output   (+6.7)
52.88  + max_tokens 6144 and other limits            (+3.2)
```

*[한국어 README](README.md)*

---

## What is in this repository

| | |
|---|---|
| [`FINDINGS.md`](FINDINGS.md) | What worked · eight ideas rejected by data · how we handled noise |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Execution path, the four decisions and their evidence, how to reproduce |
| [`submission/`](submission/) | The final submitted tree (what was actually scored) |
| [`measurement/`](measurement/) | Local scorer and A/B harness |
| [`deck/`](deck/) | Presentation slides |

`submission/` contains 126 files, but **only three are on the scoring path: `app.py` · `budget.py` · `review.py`**.
The rest is debris from pipelines we tried and discarded — what we dropped and why is in `FINDINGS.md`.

---

## One-sentence summary

**Everything we added lowered the score; everything that raised it was a change in how we called the model.**

RAG · classifiers · an emergency gate · multi-call pipelines · externalized chain-of-thought · Best-of-N · draft synthesis —
all of them looked reasonable, and all of them lost under measurement. The 20.4-point gain came from four parameters.

All 18 teams had the same model, the same API, and the same 24 hours. The difference was not ideas;
it was **the measurement that told us which four actually worked.**

---

## Measurement tooling

A single dashboard submission took 25–35 minutes, which made A/B testing impossible at that pace.
We rebuilt the scoring locally from publicly available pieces.

```
conquer_val prompt-id list from the public CoEval repository  (301 items)
  +  the public HealthBench Main dataset
  =  the same 301 questions as the leaderboard · 3,410 rubric criteria

Official formula:  Σ(met × points) / Σ(max(0, points))  · averaged per question, clipped to [0,1]
```

Local 45.0 vs leaderboard 47.8 — calibrated to within 3 points.

The two things with the longest shelf life are not the score itself, but these:

- `measurement/control.py` — **noise-floor measurement.** Repeat an identical configuration to determine the smallest difference that can actually be called.
- `measurement/paired_cv.py` — **per-question win/loss instead of averages.** Twice this caught an illusion created by a single question.

---

## The data is not included

HealthBench questions and rubrics contain canary strings for training-contamination detection,
and OpenAI asks that examples not be published online.
The `measurement/` scripts are written to **reconstruct the dataset locally** —
download the id list from the public CoEval repository and the public HealthBench Main, and join them.

API keys have also been removed. Provide yours through the `LUNIT_FM_API_KEY` environment variable.

---

## License and attribution

The competition was hosted by Lunit; the model (L2), the endpoint, and the evaluation tooling (CoEval) are Lunit's property.
This repository contains only the code and analysis written by Team AIM.
