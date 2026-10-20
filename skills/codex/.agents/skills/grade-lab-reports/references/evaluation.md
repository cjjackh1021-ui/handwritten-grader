# Grading quality evaluation

Use `python <skill>/scripts/run.py eval --help` from any working directory. Paths supplied to the command are relative to the current directory, so prefer absolute paths. Read `<linked project>/docs/evaluation.md` for the dataset schema and workflow; the linked root is in `local.json` or `GRADER_PROJECT_DIR`.

The optional evaluation mode supports short answers, math steps, essays and laboratory reports. It exercises the project's actual grading functions and lab tool loop. It does not measure the active Codex session's grading.

1. Audit independently annotated cases before evaluating the agent.
2. Make one grouped, stratified search/validation/locked-test split and reuse it for all candidates.
3. Run baseline and candidate on matching cases/configuration. Keep failures and retry records.
4. Compare paired results, including automatic coverage, correctness, latency and token use. Record a specific hypothesis and use a bounded history.
5. Use locked-test only after validation selects a frozen candidate. Do not use those results to continue tuning.

`eval/examples/` contains synthetic fixtures with `human_verified=false`. `--replay <suite>/replay.json` runs offline without network calls. These canned responses are not model predictions. Real model evaluation is a separate explicit operation using configured provider credentials; do not send a user's reports to a provider merely because this skill was selected.

The agent receives the student's submission and public rubric. Keep `labels.private.jsonl`, expected scores, evidence anchors, judge diagnostics and held-out answers out of candidate prompts and worker inputs. The separation is enforced at input construction, not by an operating-system sandbox.

The evidence candidate is opt-in. It requires deduction rule IDs, original quotes and reasons, and flags unsupported deductions for review without changing scores. No candidate is automatically promoted into normal grading.
