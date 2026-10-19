"""Shared command-line entrypoint for both bundled grading skills."""
import argparse
import json
from pathlib import Path
import shutil
from . import stamp, review


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--grades",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    reports=json.loads(args.grades.read_text(encoding="utf-8"))["reports"]
    for item in reports:
        if item.get("status","success") != "success":continue
        review.valid_score(item["score"],item.get("full_score",100))
        comment="".join(str(item.get("comment","")).split())
        if not comment or len(comment)>20:raise ValueError("评语须为 1 至 20 字")
        item["comment"]=comment
    batch=stamp.stamp_batch(reports,args.output)
    legacy_zip=args.output/"已批改实验报告.zip"
    shutil.copy2(batch["zip_path"],legacy_zip)
    failures=[{"source_file":r.get("source_file"),"error":r["stamp_error"]} for r in reports if r.get("stamp_error")]
    print(json.dumps({"marked":sum(bool(r.get("marked_pdf_path")) for r in reports),"failures":failures,"zip":str(legacy_zip)},ensure_ascii=False))
    return 1 if failures else 0
