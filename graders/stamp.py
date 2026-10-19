"""Rebuild handwritten overlays from originals; preserve PDF content and annotations."""
from __future__ import annotations
import io
import json
import re
import zipfile
from pathlib import Path
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from . import annotations, handwriting
from .geometry import display_geometry


def normalize_comment(comment):
    return re.sub(r"\s+", "", str(comment or ""))[:20] or "请认真订正"


def _draw_image(c, path, x, y, max_width, max_height):
    with Image.open(path) as image:
        scale = min(max_width/image.width, max_height/image.height)
        w, h = image.width*scale, image.height*scale
        c.drawImage(ImageReader(image), x, y+max_height-h, width=w, height=h, mask="auto")


def _source_writer(item):
    writer = PdfWriter()
    pages = item.get("pages", [])
    if not pages:
        path = Path(item["source_path"])
        if path.suffix.lower() != ".pdf": return writer, []
        reader = PdfReader(path)
        if reader.metadata: writer.add_metadata({k:str(v) for k,v in reader.metadata.items()})
        for page in reader.pages: writer.add_page(page)
        return writer, []
    for p in pages:
        meta = p.get("metadata", {})
        source = Path(meta.get("source_path") or item["source_path"])
        if source.suffix.lower() == ".pdf":
            reader = PdfReader(source)
            if not writer.pages and reader.metadata: writer.add_metadata({k:str(v) for k,v in reader.metadata.items()})
            writer.add_page(reader.pages[int(meta.get("source_page", p["page_num"]))-1])
        elif p.get("render_path"):
            with Image.open(p["render_path"]) as image:
                w, h = float(meta.get("width_pt", image.width*.48)), float(meta.get("height_pt", image.height*.48))
                buffer = io.BytesIO()
                c = canvas.Canvas(buffer, pagesize=(w, h))
                c.drawImage(ImageReader(image), 0, 0, w, h)
                c.save()
            writer.add_page(PdfReader(buffer).pages[0])
        else:
            raise ValueError(f"第 {p['page_num']} 页没有可导出的原图")
    return writer, pages


def stamp_entry(item, output, work_dir):
    writer, pages = _source_writer(item)
    if not writer.pages: return {}
    questions = item.get("details", {}).get("questions", [])
    style = item.get("handwriting_style", "liguofu")
    seed = item.get("report_id", item["source_path"])
    placements = []
    for index, target in enumerate(writer.pages):
        if index >= len(pages): continue
        page = pages[index]
        width, height, matrix = display_geometry(target)
        page["metadata"].update(width_pt=width, height_pt=height)
        occupied = [b["bbox"] for b in page.get("ocr", {}).get("blocks", []) if annotations.valid_box(b.get("bbox"))]
        occupied.extend(r["bbox"] for q in questions for r in q.get("regions", []) if r["page"] == page["page_num"])
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=(width, height))
        changed = False
        for q in questions if item.get("annotate_questions", True) else []:
            regions = q.get("regions", [])
            if not regions or max(r["page"] for r in regions) != page["page_num"]: continue
            mark = annotations.mark_for(q)
            if not mark: continue
            placement = annotations.find_placement(q, page, occupied)
            if not placement:
                q.update(position_review=True, mark=None)
                if "找不到安全落笔位置，请手动调整" not in q.setdefault("review_reasons", []):
                    q["review_reasons"].append("找不到安全落笔位置，请手动调整")
                continue
            q["placement"] = placement
            occupied.append(placement["bbox"])
            box = placement["bbox"]
            asset = handwriting.render(work_dir, mark=mark, seed=f"{seed}:{q['id']}:{mark}", style=style)
            _draw_image(c, asset, box[0]*width, (1-box[3])*height, (box[2]-box[0])*width, (box[3]-box[1])*height)
            placements.append({"question_id": q["id"], **placement, "mark": mark})
            changed = True
        c.save()
        if changed: target.merge_transformed_page(PdfReader(buffer).pages[0], matrix)
    annotations.summarize(item)
    # A separate feedback page leaves every student stroke unobscured.
    lines = ["批改草稿" if item.get("review_required") or item.get("status") == "error" else "批改结果",
             f"{'暂计' if item.get('score_provisional') else '得分'} {item.get('score', 0):g} / {item.get('full_score', 100):g}",
             "评语："+normalize_comment(item.get("comment"))]
    if item.get("manual_total_adjustment"): lines.append("总分经人工调整，逐题判定保持原值")
    for q in questions:
        if q.get("review_required"): lines.append(f"第{q['no']}题待复核")
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(595, 842))
    y = 770
    for i, line in enumerate(lines):
        if y < 70: c.showPage(); y = 770
        path = handwriting.render(work_dir, text=line, seed=f"{seed}:summary:{i}:{line}", style=style)
        _draw_image(c, path, 36, y-40, 523, 40)
        y -= 65
    c.save()
    for p in PdfReader(buffer).pages: writer.add_page(p)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.pdf")
    writer.write(temporary)
    temporary.replace(output)
    import pypdfium2 as pdfium
    png_dir = output.parent / output.stem
    png_dir.mkdir(exist_ok=True)
    pngs = []
    with pdfium.PdfDocument(str(output)) as document:
        for i in range(len(document)):
            dest = png_dir / f"page-{i+1:03d}.png"
            document[i].render(scale=150/72, draw_annots=True).to_pil().save(dest)
            pngs.append(str(dest.resolve()))
    return {"marked_pdf_path": str(output.resolve()), "marked_pdf_name": output.name, "marked_pdf": output.name,
            "marked_pngs": pngs, "stamp_error": "", "placements": placements}


def stamp_pdf(source, output, score, comment, **kwargs):
    item = dict(source_path=str(source), score=score, comment=comment, full_score=kwargs.get("full_score", 100))
    stamp_entry(item, Path(output), Path(output).parent / "_work")
    return str(Path(output).resolve())


def stamp_batch(reports, job_dir):
    job_dir = Path(job_dir).resolve()
    marked = job_dir / "marked"
    marked.mkdir(parents=True, exist_ok=True)
    for item in reports:
        item.setdefault("report_id", annotations.stable_id(item["source_path"]))
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(item.get("source_file", "作答")).stem)[:80]
        output = marked / f"{stem}-{item['report_id']}-批改.pdf"
        try:
            item.update(stamp_entry(item, output, job_dir / "_work"))
        except Exception as exc:
            item.update(stamp_error=str(exc), marked_pdf_path="", marked_pdf_name="", marked_pngs=[])
    batch_path = job_dir / "batch_results.json"
    batch_path.write_text(json.dumps({"schema_version": 2, "reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = job_dir / "已批改作业.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in reports:
            for filename in [item.get("marked_pdf_path", ""), *item.get("marked_pngs", [])]:
                if filename and Path(filename).is_file(): archive.write(filename, Path(filename).relative_to(marked))
        archive.write(batch_path, batch_path.name)
    return {"zip_path": str(zip_path.resolve()), "batch_results_path": str(batch_path.resolve())}
