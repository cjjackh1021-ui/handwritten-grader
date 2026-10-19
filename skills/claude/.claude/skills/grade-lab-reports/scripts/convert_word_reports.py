#!/usr/bin/env python3
"""把 DOCX 报告转换为 staging PDF, 保留来源溯源。

用法:
    python convert_word_reports.py --input <dir> --output <staging_dir> --manifest <json>

依赖 LibreOffice/soffice。找不到时以非零码退出并在 manifest 记录错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                 "/usr/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if Path(cand).is_file():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    args = ap.parse_args()
    root = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    soffice = find_soffice()
    if not soffice:
        raise SystemExit("LibreOffice/soffice 不可用")

    reports, errors = [], []
    for source in sorted(root.rglob("*.docx")):
        target_dir = output / source.parent.relative_to(root)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{source.stem}.pdf"
        try:
            completed = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(target_dir), str(source)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            if completed.returncode or not target.is_file():
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Word 转 PDF 失败")
            reports.append({"source_path": str(source), "source_sha256": sha256(source),
                            "source_file": source.name, "source_format": "docx",
                            "source_pdf_path": str(target), "source_pdf_sha256": sha256(target)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"source_path": str(source), "source_file": source.name, "error": str(exc)})

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps({"reports": reports, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"reports": len(reports), "errors": len(errors), "manifest": str(args.manifest)}, ensure_ascii=False))
    return 0 if reports else 2


if __name__ == "__main__":
    raise SystemExit(main())
