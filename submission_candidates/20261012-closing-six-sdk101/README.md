# E05: earlier closing hint

Clinically unvalidated candidate copied from frozen E03. Of the ten runtime files, only
one byte differs: the `next_step` closing-explanation hint starts when remaining
turns are at most **6**, instead of at most **2**. The total remains 30 turns.
E04's record instructions are not included; E03 RECORD, STEP, REVIEW, SDK,
retrieval, settings, budgets and telemetry are preserved byte for byte.

The hint does not force an action or guarantee that the Doctor explains
anything. It replaces the old examination hint on remaining turns 6 through 3,
which could reduce useful information gathering or examinations. Seven remaining
turns still uses the old examination hint; two and one use the same closing hint
as E03. Earlier and later actions remain controlled by the same draft and review
path. Clinical quality, token use and execution time must be evaluated separately.

The separate builder pins all ten E03 files, proves the exact one-byte runtime
change, and compares isolated baseline and actual ZIP dispatch across remaining
turns 7, 6, 5, 4, 3, 2 and 1 with synthetic clients and network blocked. It never
uploads or calls a model. Synthetic outcomes do not demonstrate clinical
compliance. README is excluded from the ten-file ZIP. The original historical
run.py introductory text is retained because all unrelated bytes are frozen.

Synthetic validation and independent package audit passed. Submitted once for
sample validation at 20:59 KST on 2026-10-12; first observed status was queued.
See [experiment record](../../benchmarks/NOVA_CLOSING_SIX_PROTOTYPE.md) for results
and limitations. Do not resubmit the fixed ZIP.
