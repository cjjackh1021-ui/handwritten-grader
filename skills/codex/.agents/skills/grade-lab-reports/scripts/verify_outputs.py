#!/usr/bin/env python3
"""校验源文件完整性、盖分 PDF 与 ZIP 成员。

用法:
    python verify_outputs.py --results output/batch_results.json --zip output/已批改实验报告.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from pypdf import PdfReader


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ink_count(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        for ref in page.get("/Annots", []) or []:
            try:
                if str(ref.get_object().get("/Subtype")) == "/Ink":
                    count += 1
            except Exception:
                continue
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--zip", dest="zip_path", required=True, type=Path)
    args = ap.parse_args()
    batch = json.loads(args.results.read_text(encoding="utf-8"))
    failures, expected, checks = [], [], []
    for report in batch.get("reports", []):
        if report.get("status", "success") != "success" or not report.get("marked_pdf_path"):
            continue
        source = Path(report["source_path"])
        marked = Path(report["marked_pdf_path"])
        current = sha256(source) if source.is_file() else ""
        reader = PdfReader(str(marked), strict=False) if marked.is_file() else None
        item = {
            "source_file": report["source_file"],
            "source_unchanged": current == report.get("source_sha256", current),
            "marked_exists": marked.is_file(),
            "comment_length": len(report.get("comment", "")),
            "ink_annotations": ink_count(reader) if reader else None,
            "page_count": len(reader.pages) if reader else None,
        }
        original_ink = ink_count(PdfReader(str(source))) if source.suffix.lower() == ".pdf" and source.is_file() else 0
        if not item["marked_exists"] or not item["source_unchanged"] or item["comment_length"] > 20 or (item["ink_annotations"] or 0) < original_ink:
            failures.append(item)
        checks.append(item)
        expected.append(report.get("marked_pdf") or marked.name)
        expected.extend(Path(p).relative_to(marked.parent).as_posix() for p in report.get("marked_pngs", []))

    if not args.zip_path.is_file():
        failures.append({"zip": "missing"})
        actual = []
    else:
        with zipfile.ZipFile(args.zip_path) as archive:
            actual = sorted(archive.namelist())
        expected.append("batch_results.json")
        if actual != sorted(expected):
            failures.append({"zip_members": actual, "expected": sorted(expected)})
    print(json.dumps({"ok": not failures, "checks": checks, "zip_members": actual, "failures": failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
