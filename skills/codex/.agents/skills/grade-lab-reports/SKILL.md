---
name: grade-lab-reports
description: Batch-grade Chinese laboratory-report PDF and Word files with the active Codex model, convert Word reports to marked PDFs, render red handwritten scores and comments using the companion project's handwriting-web kernel and fonts, and create a student-grouped editable Excel summary plus a ZIP. Use when a user asks Codex to grade a folder of experiment reports, include DOCX reports, mark scores directly on output PDFs, group results by student in Excel, or regenerate marked PDFs after editing the workbook. This is the Agent-native workflow; a companion multi-model Flask web app (see references/companion-webapp.md) covers API-based grading with switchable providers.
---

# Grade Lab Reports (Codex)

Grade reports with the active Codex model's own reading and vision. Do not call an external grading API and do not copy the legacy Flask application into the deliverables.

## Inputs and defaults

- Accept a folder containing PDF and DOCX reports and an optional grading-criteria file.
- Use [references/default-grading-criteria.md](references/default-grading-criteria.md) when no criteria file is supplied.
- Preserve all source files. Write deliverables under the current repository's `output/` directory.
- Convert DOCX files to staging PDFs with `scripts/convert_word_reports.py`; retain the original Word path and hash as provenance. Do not run TF-IDF plagiarism or sparse-page heuristics as a grade signal. Apply the criteria's direct-zero plagiarism rule only when the user provides evidence or the batch contains clearly duplicated reports; never infer plagiarism from ordinary topical similarity.

## Workflow

1. Use the bundled Python runtime with `pdfplumber`, `pypdf`, `reportlab`, and `openpyxl` available. No Node.js or OAI-specific tooling is required.
2. Create a batch workspace under `tmp/pdfs/`. Convert DOCX inputs to staging PDFs, then run `scripts/extract_reports.py --input <dir> --work <workdir>` on original and converted PDFs to extract every page without truncation and render pages that need visual review (`visual_review_pages`).
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
    "score": 95,
    "comment": "内容完整，实践性强",
    "reason": "按评分标准逐项说明得分与扣分。",
    "status": "success"
  }]
}
```

5. Keep `score` as an integer 0-100. Normalize whitespace in `comment` and keep it at 20 Unicode characters or fewer. Keep `student_id` as text.
6. Run `scripts/stamp_pdfs.py --grades output/grades.json --output output`. It validates grades, preserves original Ink annotations, stamps each PDF, writes `output/batch_results.json`, and creates `output/已批改实验报告.zip`.
7. Run `scripts/build_summary.py --input output/batch_results.json --output output/批改结果.xlsx`. Rows are sorted by student ID, then name, then report name so every student's reports stay contiguous.
8. Run `scripts/verify_outputs.py --results output/batch_results.json --zip output/已批改实验报告.zip`. Re-render the modified first and last/new pages of every PDF and inspect them. Deliver only after there is no clipping, overlap, missing Chinese text, lost original Ink annotation, or workbook defect.

## Placement and rendering

- Initialize the companion project's renderer with `python scripts/setup_handwriting.py --components handwriting` from its root. The wrapper finds the project in its ancestors; when this skill is installed separately, set `GRADER_PROJECT_DIR` to that checkout. It does not use a system-font fallback.
- Use the shared handrightbeta kernel, Li Guofu handwriting font and the same project's backup font for every handwritten symbol, score and comment. The renderer checks real glyphs and fails explicitly if both fonts lack a character.
- Preserve original page content and all original annotations, including Ink. Rebuild from the originals each time. The score/comment appear on an appended feedback page, with transparent question marks beside answers when question geometry is available.
- Keep source names, hashes, orientation and provenance. Review generated page PNGs for overlap and clipping.

## Regenerate after workbook edits

Run `scripts/build_summary.py --from-xlsx output/批改结果.xlsx --batch output/batch_results.json --emit output/grades.json` to produce revised grade JSON after validating scores and comments. Then re-run `stamp_pdfs.py`, rebuild the workbook, and re-verify.

## Failure handling

- Record unreadable, damaged, password-protected PDFs and failed Word conversions as isolated errors (`"status": "error"`) and continue other reports.
- Never overwrite a source PDF or Word document.
- Stop before final delivery if the ZIP omits a successful report, an original hash changes, or any comment exceeds 20 characters.
