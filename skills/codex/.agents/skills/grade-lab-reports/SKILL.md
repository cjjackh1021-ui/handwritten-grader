---
name: grade-lab-reports
description: Batch-grade Chinese laboratory-report PDF and Word files with the active Codex model, convert Word reports to marked PDFs, render red handwritten scores and comments using the companion project's handwriting-web kernel and fonts, and create a student-grouped editable Excel summary plus a ZIP. Use when a user asks Codex to grade a folder of experiment reports, include DOCX reports, mark scores directly on output PDFs, group results by student in Excel, or regenerate marked PDFs after editing the workbook. This is the Agent-native workflow; a companion multi-model Flask web app (see references/companion-webapp.md) covers API-based grading with switchable providers.
---

# Grade Lab Reports (Codex)

Grade reports with the active Codex model's own reading and vision. Do not call an external grading API and do not copy the legacy Flask application into the deliverables.

## Installed runtime

Use this skill's absolute `scripts/run.py` path for every command below. The launcher reads `local.json` and switches to the linked project's `.venv`, binding the shared renderer automatically. `GRADER_PROJECT_DIR` overrides that link. Run `python <skill>/scripts/run.py doctor` first; if system Python is unavailable, start it with `<project>/.venv/Scripts/python.exe` on Windows. The project path is recorded in this skill's `local.json`.

The project installer is `python scripts/install_codex_skill.py --skill grade-lab-reports`. Do not ask for an API Key for this workflow. Missing LibreOffice affects Word conversion; preserve PDFs and continue those, reporting the Word failure. Do not silently skip students with failed conversions or unreadable pages.

## Inputs and defaults

- Accept a folder containing PDF and DOCX reports and an optional grading-criteria file.
- Use [references/default-grading-criteria.md](references/default-grading-criteria.md) when no criteria file is supplied.
- Preserve all source files. Write each batch and revision under a new subdirectory of `output/`, or the user's requested output location. Keep staging/output outside the input directory so recursive scanning cannot re-ingest generated PDFs.
- Convert DOCX files to staging PDFs with `scripts/convert_word_reports.py`; retain the original Word path and hash as provenance. Do not run TF-IDF plagiarism or sparse-page heuristics as a grade signal. Apply the criteria's direct-zero plagiarism rule only when the user provides evidence or the batch contains clearly duplicated reports; never infer plagiarism from ordinary topical similarity.

## Workflow

1. Run `scripts/run.py doctor` to verify the project environment and renderer. No Node.js or OAI-specific tooling is required.
2. Create a separate batch workspace. For Word files, run `scripts/run.py convert --input <dir> --output <staging> --manifest <conversion.json>`. Run `scripts/run.py extract --input <dir> --work <workdir>` separately on original PDFs and converted PDFs; use different work directories. Extract every page without truncation and render pages that need visual review (`visual_review_pages`).
3. Read the complete criteria and each extracted report JSON. Inspect every rendered page listed in `visual_review_pages` with your own vision. Do not grade an image-heavy report from extracted text alone.
4. Grade each report independently. Produce UTF-8 JSON at `output/grades.json` with this shape:

```json
{
  "reports": [{
    "source_path": "/absolute/report.pdf",
    "source_sha256": "...",
    "source_file": "report.pdf",
    "student_name": "姓名",
    "student_id": "2023000000",
    "full_score": 100,
    "score": 95,
    "comment": "内容完整，实践性强",
    "reason": "按评分标准逐项说明得分与扣分。",
    "status": "success"
  }]
}
```

5. Keep `score` as an integer 0-100. Normalize whitespace in `comment` and keep it at 20 Unicode characters or fewer. Keep `student_id` as text. Store criterion-level scores in `dimensions` (name, score, full, comment) and check that their sum equals the total. For converted Word files, use the staging PDF's path/hash as `source_path`/`source_sha256`, and record the original DOCX's path/hash as `origin_path`/`origin_sha256` from the conversion manifest.
6. Run `scripts/run.py stamp --grades <batch>/grades.json --output <batch>`. It validates grades, preserves original Ink annotations, stamps each PDF, writes `batch_results.json`, and creates `已批改实验报告.zip`.
7. Run `scripts/run.py summary --input <batch>/batch_results.json --output <batch>/批改结果.xlsx`. Rows are sorted by student ID, then name, then report name so every student's reports stay contiguous. Deliver the workbook alongside the ZIP.
8. Run `scripts/run.py verify --results <batch>/batch_results.json --zip <batch>/已批改实验报告.zip`. Inspect the exported first and last/new page PNGs of every PDF. Deliver only after there is no clipping, overlap, missing Chinese text, lost original Ink annotation, or workbook defect. These checks validate export integrity, not grading accuracy.

## Placement and rendering

- The installer links the project and `run.py` binds its renderer automatically. If the renderer is missing, report it; the project's initialization command is `python scripts/setup_handwriting.py --components handwriting`. It does not use a system-font fallback.
- Use the shared handrightbeta kernel, Li Guofu handwriting font and the same project's backup font for every handwritten symbol, score and comment. The renderer checks real glyphs and fails explicitly if both fonts lack a character.
- Preserve original page content and all original annotations, including Ink. Rebuild from the originals each time. The score/comment appear on an appended feedback page, with transparent question marks beside answers when question geometry is available.
- Keep source names, hashes, orientation and provenance. Review generated page PNGs for overlap and clipping.

## Regenerate after workbook edits

Run `scripts/run.py summary --from-xlsx <edited.xlsx> --batch <old>/batch_results.json --emit <new>/grades.json` to produce revised grade JSON after validating scores and comments. Then run `scripts/run.py stamp` into the new directory, rebuild the workbook, and re-verify. Keep stable report IDs so students with identical report filenames cannot be confused. A manual total-score adjustment must not silently rewrite the original dimension scores.

## Failure handling

- Record unreadable, damaged, password-protected PDFs and failed Word conversions as isolated errors (`"status": "error"`) and continue other reports.
- Never overwrite a source PDF or Word document.
- Stop before final delivery if the ZIP omits a successful report, an original hash changes, or any comment exceeds 20 characters.

## Evaluate the grading implementation

When the user asks to evaluate or compare the companion grading agent, read [references/evaluation.md](references/evaluation.md). `scripts/run.py eval` exposes the project's independent audit, grouped splits, run and comparison commands. This optional mode evaluates the companion API-based agent; ordinary report grading above still uses the active Codex session. Offline replay validates wiring only and must not be presented as measured model accuracy.
