#!/usr/bin/env python3
"""提取完整 PDF 文本并渲染需视觉复核的页面 (无需 Poppler，走 pdfplumber)。

用法:
    python extract_reports.py --input <pdf目录> --work <工作目录>

输出:
    <work>/manifest.json                 批次清单
    <work>/reports/<key>/report.json      单份逐页 JSON, 含 visual_review_pages
    <work>/rendered/<key>/page-XXXX.png    需复核页渲染图
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

import pdfplumber
from pypdf import PdfReader


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value.strip()


def extract_student_info(pdf_path: Path, text: str) -> tuple[str, str]:
    stem = pdf_path.stem
    parent_hint = " ".join(parent.name for parent in pdf_path.parents[:3])
    parent_identity = re.search(
        r"(?:^|\s)(\d{8,12})[-_ ]*([\u3400-\u9fff·]{2,8})(?:\s|$)", parent_hint
    )
    student_id = ""
    id_match = parent_identity or re.search(r"学号\s*[：:]\s*(\d{8,12})", text) \
        or re.search(r"(?<!\d)(\d{8,12})(?!\d)", stem) \
        or re.search(r"(?<!\d)(\d{8,12})(?!\d)", parent_hint) \
        or re.search(r"(?<!\d)(\d{8,12})(?!\d)", text)
    if id_match:
        student_id = id_match.group(1)
    name = ""
    if parent_identity:
        name = parent_identity.group(2)
    else:
        nm = re.search(r"姓名\s*[：:]\s*([\u3400-\u9fff·]{2,8})", text)
        if nm:
            name = nm.group(1)
    if not name:
        tail = re.sub(r"^\d{8,12}[-_ ]*", "", stem)
        tail = re.sub(r"[-_ ]*(实验报告|报告)$", "", tail)
        if re.fullmatch(r"[\u3400-\u9fff·]{2,8}", tail):
            name = tail
    return name or stem, student_id


def render_page(page, out_path: Path, resolution: int = 150) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.to_image(resolution=resolution).original.save(str(out_path))
    return str(out_path.resolve())


def inspect_pdf(pdf_path: Path, work_dir: Path) -> dict:
    source_hash = sha256_file(pdf_path)
    key = f"{pdf_path.stem}-{source_hash[:10]}"
    report_dir = work_dir / "reports" / key
    render_dir = work_dir / "rendered" / key
    report_dir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(pdf_path), strict=False)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError("PDF 已加密且需要密码")

    pages, parts, visual = [], [], []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for num, page in enumerate(pdf.pages, start=1):
            text = clean_text(page.extract_text() or "")
            image_count = len(page.images or [])
            reasons = []
            if len(text) < 80:
                reasons.append("文字过少")
            if image_count >= 3:
                reasons.append("图片密集")
            elif image_count and len(text) < 220:
                reasons.append("图片页且文字有限")
            render_path = ""
            if reasons:
                render_path = render_page(page, render_dir / f"page-{num:04d}.png")
                visual.append({"page_num": num, "reasons": reasons, "render_path": render_path})
            pages.append({"page_num": num, "text": text, "char_count": len(text),
                          "image_count": image_count, "render_path": render_path})
            parts.append(f"【第{num}页】\n{text}")

    full_text = "\n\n".join(parts)
    name, student_id = extract_student_info(pdf_path, full_text)
    report = {
        "source_path": str(pdf_path.resolve()), "source_sha256": source_hash,
        "source_file": pdf_path.name, "student_name": name, "student_id": student_id,
        "page_count": len(pages), "text_char_count": len(full_text),
        "visual_review_pages": visual, "pages": pages,
    }
    (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "full_text.txt").write_text(full_text, encoding="utf-8")
    summary = {k: report[k] for k in ("source_path", "source_sha256", "source_file",
                                      "student_name", "student_id", "page_count",
                                      "text_char_count", "visual_review_pages")}
    summary["report_json"] = str((report_dir / "report.json").resolve())
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    args = ap.parse_args()
    input_dir = args.input.expanduser().resolve()
    work_dir = args.work.expanduser().resolve()
    if not input_dir.is_dir():
        ap.error(f"input directory not found: {input_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    manifest = {"input_dir": str(input_dir), "reports": [], "errors": []}
    for pdf in pdfs:
        try:
            manifest["reports"].append(inspect_pdf(pdf, work_dir))
        except Exception as exc:  # noqa: BLE001
            manifest["errors"].append({"source_path": str(pdf.resolve()), "source_file": pdf.name, "error": str(exc)})
    (work_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(work_dir / "manifest.json"),
                      "reports": len(manifest["reports"]), "errors": len(manifest["errors"])}, ensure_ascii=False))
    return 0 if manifest["reports"] else 2


if __name__ == "__main__":
    sys.exit(main())
