# E04: final-record instruction experiment

This is a separate, unvalidated candidate based on frozen E03. Only the
`RECORD_PROMPT` assignment differs in the ten runtime files. The complete
instruction bundle is the experimental factor; an observed difference cannot
be attributed to any single sentence within it.

The changed prompt asks for grounded test rationale, follow-up timing or
conditions and warning signs, a supported disposition, grounded treatment/action plans without invented medication
or dosage, and a distinction
between counseling actually delivered and education still planned. It requires
preliminary `O.tests` to be empty and forbids invented exam turn numbers.
Insufficient information must remain explicit. It does not prescribe fixed
medical content or force unnecessary tests.

The record format parser and its fallback are unchanged. The instructions do
not enforce factuality or completeness, and synthetic fake-model outputs do not
demonstrate that gpt-oss20b follows them. Existing examples with negative findings
are retained under the existing confirmed-positive/negative/unknown rule.
The record output budget remains 4096 including reasoning; token cost and
runtime may differ with the longer prompt.

STEP, REVIEW, final-record call/retry policy, retrieval, session/action limits,
model settings, budgets, telemetry, and SDK 1.0.1 remain frozen. No retrieval is
added to the final record. The original `run.py` introductory text and comments
are preserved for byte identity, including their historical development status;
this README supplies the candidate's current experimental meaning.

Use `scripts/build_record_contract_submission.py --output NEW_EMPTY_PATH` for
the separate pinned builder. It validates all ten E03 file hashes and all bytes
outside the changed assignment, then compares isolated E03 and ZIP-candidate
synthetic dispatch with network denied. It never uploads or invokes a model.
README is excluded from the ten-file ZIP.
