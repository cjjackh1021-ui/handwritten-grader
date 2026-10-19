---
name: grade-lab-reports
description: Batch-grade Chinese laboratory-report PDF and Word files with Claude's own reading and vision. Convert Word reports to marked PDFs, render red handwritten scores and comments using the companion project's handwriting-web kernel and fonts, and create a student-grouped editable Excel summary plus a ZIP. Use when a user asks Claude Code to grade a folder of experiment reports, include DOCX reports, mark scores directly on output PDFs, group results by student in Excel, or regenerate marked PDFs after editing the workbook. This is the Agent-native workflow; a companion multi-model Flask web app (references/companion-webapp.md) covers API-based grading with switchable providers.
---

# Grade Lab Reports (Claude Code)

Grade reports with your own reading and vision. Do not call an external grading API. All helper scripts are pure Python and require no Node.js or OpenAI-specific tooling.

## Prerequisites

Install the Python dependencies once (the same set the companion web app uses):

```bash
pip install pdfplumber pypdf reportlab openpyxl python-docx
```

DOCX conversion additionally needs LibreOffice (`soffice`) on PATH; without it, grade DOCX from extracted text and skip stamping for those files.

## Inputs and defaults

- Accept a folder containing PDF and DOCX reports and an optional grading-criteria file.
- Use [references/default-grading-criteria.md](references/default-grading-criteria.md) when no criteria file is supplied.
- Preserve all source files. Write deliverables under the working directory's `output/`.
- Do not use TF-IDF or sparse-page heuristics as a grade signal. Apply the criteria's direct-zero plagiarism rule only with real evidence or clearly duplicated reports.

## Workflow

1. If there are DOCX inputs, run `scripts/convert_word_reports.py --input <dir> --output tmp/pdfs --manifest tmp/docx.json` to make staging PDFs (keeps source hashes).
2. Run `scripts/extract_reports.py --input <pdf_dir> --work tmp/work` on original and converted PDFs. It extracts full text and renders pages listed in `visual_review_pages`.
3. Read the criteria and each `report.json`. Open every rendered page image with your vision before scoring image-heavy reports; never grade them from text alone.
4. Write `output/grades.json`:

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

   `score` is an integer 0-100; `comment` is 20 Unicode characters or fewer with whitespace normalized; `student_id` stays text.
5. Run `scripts/stamp_pdfs.py --grades output/grades.json --output output`. It preserves original Ink annotations, stamps the red handwritten score plus comment, writes `output/batch_results.json`, and builds `output/已批改实验报告.zip`.
6. Run `scripts/build_summary.py --input output/batch_results.json --output output/批改结果.xlsx` for a student-grouped workbook.
7. Run `scripts/verify_outputs.py --results output/batch_results.json --zip output/已批改实验报告.zip`. Re-render the changed first and last/new pages and inspect them; deliver only when there is no clipping, overlap, missing Chinese text, lost original Ink annotation, or ZIP/workbook defect.

## Placement and rendering

- Initialize the companion project's renderer with `python scripts/setup_handwriting.py --components handwriting` from its root. The wrapper finds the project in its ancestors; when this skill is installed separately, set `GRADER_PROJECT_DIR` to that checkout. It does not use a system-font fallback.
- Use the shared handrightbeta kernel, Li Guofu handwriting font and the same project's backup font for every handwritten symbol, score and comment. The renderer checks real glyphs and fails explicitly if both fonts lack a character.
- Preserve original page content and all original annotations, including Ink. Rebuild from the originals each time. The score/comment appear on an appended feedback page, with transparent question marks beside answers when question geometry is available.
- Keep source names, hashes, orientation and provenance. Review generated page PNGs for overlap and clipping.

## Regenerate after workbook edits

`scripts/build_summary.py --from-xlsx output/批改结果.xlsx --batch output/batch_results.json --emit output/grades.json`, then re-run `stamp_pdfs.py`, rebuild the workbook, and re-verify.

## Failure handling

- Record unreadable/encrypted PDFs and failed Word conversions as `"status": "error"` and continue.
- Never overwrite a source file.
- Stop before delivery if the ZIP omits a successful report, a source hash changes, or any comment exceeds 20 characters.
