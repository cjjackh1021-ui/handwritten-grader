"""报告内容提取与页面渲染。

融合两套来源：
- 本地 app.py 的 PDF 文本 + 内嵌图片 OCR + 问题页检测；
- skill 的 extract_reports.py 的姓名/学号抽取、逐页 JSON、需视觉复核页渲染。

页面渲染统一走 pdfplumber ``page.to_image``（无需额外安装 Poppler），
渲染出的 PNG 供多模态视觉评分使用。
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field, replace, asdict
from pathlib import Path

import pdfplumber

from . import config

# pytesseract 延迟到需要时才导入/配置
_TESSERACT_READY: bool | None = None


def _ensure_tesseract() -> bool:
    global _TESSERACT_READY
    if _TESSERACT_READY is not None:
        return _TESSERACT_READY
    cmd = config.find_tesseract()
    if not cmd:
        _TESSERACT_READY = False
        return False
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = cmd
        _TESSERACT_READY = True
    except Exception:
        _TESSERACT_READY = False
    return _TESSERACT_READY


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


# ---------------------------------------------------------------------------
# 姓名 / 学号 抽取（融合 skill 的父目录线索与本地的文件名/正文匹配）
# ---------------------------------------------------------------------------
def extract_student_info(path: Path, text: str) -> tuple[str, str]:
    stem = path.stem
    parent_hint = " ".join(parent.name for parent in path.parents[:3])
    parent_identity = re.search(
        r"(?:^|\s)(\d{8,12})[-_ ]*([\u3400-\u9fff·]{2,8})(?:\s|$)", parent_hint
    )

    student_id = ""
    id_match = parent_identity
    if not id_match:
        id_match = re.search(r"学号\s*[：:]\s*(\d{8,12})", text)
    if not id_match:
        id_match = re.search(r"(?<!\d)(\d{8,12})(?!\d)", stem)
    if not id_match:
        id_match = re.search(r"(?<!\d)(\d{8,12})(?!\d)", parent_hint)
    if not id_match:
        id_match = re.search(r"(?<!\d)(\d{8,12})(?!\d)", text)
    if id_match:
        student_id = id_match.group(1)

    name = ""
    if parent_identity:
        name = parent_identity.group(2)
    else:
        name_match = re.search(r"姓名\s*[：:]\s*([\u3400-\u9fff·]{2,8})", text)
        if name_match:
            name = name_match.group(1)
    if not name:
        tail = re.sub(r"^\d{8,12}[-_ ]*", "", stem)
        tail = re.sub(r"[-_ ]*(实验报告|报告)$", "", tail)
        if re.fullmatch(r"[\u3400-\u9fff·]{2,8}", tail):
            name = tail
    return name or stem, student_id


# ---------------------------------------------------------------------------
# 页面渲染 & 内嵌图片 OCR
# ---------------------------------------------------------------------------
def render_page(page, out_path: Path, resolution: int = 150) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # PDFium renders existing Ink annotations as well as page content.
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(getattr(page.pdf.stream, "name", page.pdf.stream)) as document:
        image = document[page.page_number-1].render(scale=resolution/72, draw_annots=True).to_pil()
        image.save(str(out_path))
    return str(out_path.resolve())


def ocr_page(page, resolution: int = 150) -> str:
    if not _ensure_tesseract():
        return ""
    import pytesseract

    texts = []
    for img_obj in getattr(page, "images", None) or []:
        try:
            crop = page.within_bbox(
                (img_obj["x0"], img_obj["top"], img_obj["x1"], img_obj["bottom"])
            )
            if crop and crop.width and crop.height:
                pil = crop.to_image(resolution=resolution).original
                texts.append(pytesseract.image_to_string(pil, lang="chi_sim+eng"))
        except Exception:
            continue
    return "\n".join(t for t in texts if t.strip())


# ---------------------------------------------------------------------------
# 单份报告
# ---------------------------------------------------------------------------
@dataclass
class PageInfo:
    page_num: int
    text: str
    char_count: int
    image_count: int
    needs_visual: bool
    reasons: list[str] = field(default_factory=list)
    render_path: str = ""
    ocr_text: str = ""
    # 问题页检测（沿用本地逻辑）
    has_issue: bool = False
    issue_reason: str = ""
    metadata: dict = field(default_factory=dict)
    ocr: dict = field(default_factory=dict)


@dataclass
class ReportInfo:
    source_path: str
    source_sha256: str
    source_file: str
    student_name: str
    student_id: str
    page_count: int
    full_text: str
    pages: list[PageInfo]
    origin_path: str = ""       # 若来自 DOCX，此处记录原始 docx 路径
    origin_sha256: str = ""
    error: str = ""


def _detect_issue(text: str, image_count: int) -> tuple[bool, str]:
    """问题页检测：仅当无图片且内容极少时标记（沿用本地规则）。"""
    if image_count > 0:
        return False, ""
    if re.match(r"^\s*\d+\s*([/|]\s*\d+\s*)?$", text):
        return True, "仅包含页码"
    if len(text) < 20:
        return True, "空白页或无实质内容"
    if 20 <= len(text) < 50:
        return True, "内容极少（可能不完整）"
    return False, ""


def inspect_pdf(
    pdf_path: Path,
    work_dir: Path,
    ocr_enabled: bool = True,
    render_for_vision: bool = True,
    ocr_mode: str | None = None,
) -> ReportInfo:
    pdf_path = pdf_path.resolve()
    source_hash = sha256_file(pdf_path)
    from pypdf import PdfReader
    from . import geometry
    original_pdf = PdfReader(str(pdf_path))
    key = f"{pdf_path.stem}-{source_hash[:10]}"
    render_dir = work_dir / "rendered" / key

    pages: list[PageInfo] = []
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            # PDFium displays CropBox; native words must use exactly that same view.
            if page.cropbox and tuple(page.cropbox) != tuple(page.bbox):
                page = page.crop(page.cropbox)
            text = clean_text(page.extract_text() or "")
            image_count = len(page.images or [])

            reasons: list[str] = []
            if len(text) < 80:
                reasons.append("文字过少")
            if image_count >= 3:
                reasons.append("图片密集")
            elif image_count and len(text) < 220:
                reasons.append("图片页且文字有限")
            needs_visual = bool(reasons)

            ocr_text = ""
            render_path = ""
            if render_for_vision or ocr_enabled:
                try:
                    render_path = render_page(
                        page, render_dir / f"page-{page_num:04d}.png"
                    )
                except Exception:
                    render_path = ""

            from . import ocr
            recognized = {}
            if ocr_enabled and needs_visual and render_path:
                recognized = ocr.recognize(render_path, work_dir, ocr_mode)
                ocr_text = clean_text(recognized.get("text", ""))
            if not needs_visual:
                words = page.extract_words()
                x0, top, x1, bottom = page.bbox
                recognized = {"mode": "native", "issues": [], "blocks": [
                    {"id": f"native-{i}", "engine": "native", "text": w["text"], "confidence": None,
                     "bbox": [(w['x0']-x0)/(x1-x0), (w['top']-top)/(bottom-top),
                              (w['x1']-x0)/(x1-x0), (w['bottom']-top)/(bottom-top)]}
                    for i, w in enumerate(words)]}

            has_issue, issue_reason = _detect_issue(text, image_count)

            combined = text
            if ocr_text:
                combined = f"{text}\n[图片OCR]\n{ocr_text}"

            pages.append(
                PageInfo(
                    page_num=page_num,
                    text=combined,
                    char_count=len(text),
                    image_count=image_count,
                    needs_visual=needs_visual,
                    reasons=reasons,
                    render_path=render_path,
                    ocr_text=ocr_text,
                    has_issue=has_issue,
                    issue_reason=issue_reason,
                    metadata={"source_path": str(pdf_path), "source_page": page_num,
                              "width_pt": page.width, "height_pt": page.height, "rotation": page.rotation,
                              "crop_box": list(page.bbox), "media_box": list(page.mediabox),
                              "normalized_to_pdf": geometry.normalized_to_pdf(original_pdf.pages[page_num-1]),
                              "coordinate_system": "normalized_top_left", "dpi": 150},
                    ocr=recognized,
                )
            )
            parts.append(f"【第{page_num}页】\n{combined}")

    full_text = "\n\n".join(parts)
    name, student_id = extract_student_info(pdf_path, full_text)
    return ReportInfo(
        source_path=str(pdf_path),
        source_sha256=source_hash,
        source_file=pdf_path.name,
        student_name=name,
        student_id=student_id,
        page_count=len(pages),
        full_text=full_text,
        pages=pages,
    )


def inspect_text_file(path: Path) -> ReportInfo:
    path = path.resolve()
    raw = path.read_text(encoding="utf-8", errors="ignore")
    text = clean_text(raw)
    name, student_id = extract_student_info(path, text)
    page = PageInfo(
        page_num=1,
        text=text,
        char_count=len(text),
        image_count=0,
        needs_visual=False,
    )
    return ReportInfo(
        source_path=str(path),
        source_sha256=sha256_file(path),
        source_file=path.name,
        student_name=name,
        student_id=student_id,
        page_count=1,
        full_text=text,
        pages=[page],
    )


def report_from_text(source_path: Path, text: str, origin_sha: str = "") -> ReportInfo:
    """由降级提取的纯文本 (如无 LibreOffice 的 DOCX) 构造 ReportInfo。"""
    source_path = source_path.resolve()
    text = clean_text(text)
    name, student_id = extract_student_info(source_path, text)
    page = PageInfo(
        page_num=1, text=text, char_count=len(text), image_count=0, needs_visual=False
    )
    return ReportInfo(
        source_path=str(source_path),
        source_sha256=origin_sha,
        source_file=source_path.name,
        student_name=name,
        student_id=student_id,
        page_count=1,
        full_text=text,
        pages=[page],
    )


def _ocr_image(image) -> str:
    if not _ensure_tesseract():
        return ""
    import pytesseract

    for lang in ("chi_sim+eng", "eng"):
        try:
            return pytesseract.image_to_string(image, lang=lang)
        except Exception:  # noqa: BLE001  # 缺语言包时降级
            continue
    return ""


def inspect_image(path: Path, work_dir: Path, ocr_enabled: bool = True, ocr_mode: str | None = None) -> ReportInfo:
    """拍照/扫描的作答图片: 整张图作为需视觉复核的一页, OCR 文本作为无视觉模型时的回退。"""
    from PIL import Image, ImageOps

    path = path.resolve()
    source_hash = sha256_file(path)
    out = work_dir / "rendered" / f"{path.stem}-{source_hash[:10]}" / "page-0001.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as im:
        original_size, orientation = im.size, im.getexif().get(274, 1)
        im = ImageOps.exif_transpose(im).convert("RGB")  # 手机照片按 EXIF 摆正
        im.save(out)
        width, height = im.size
    from . import ocr
    recognized = ocr.recognize(out, work_dir, ocr_mode) if ocr_enabled else {}
    from . import geometry
    text = clean_text(recognized.get("text", ""))
    page = PageInfo(
        page_num=1, text=text, char_count=len(text), image_count=1, needs_visual=True,
        reasons=["图片作答"], render_path=str(out.resolve()), ocr_text=text,
        metadata={"source_path": str(path), "source_page": 1, "original_size": original_size,
                  "width_px": width, "height_px": height, "exif_orientation": orientation,
                  "normalized_to_original": geometry.normalized_to_original(*original_size, orientation),
                  "width_pt": width*72/150, "height_pt": height*72/150,
                  "coordinate_system": "normalized_top_left", "dpi": 150}, ocr=recognized,
    )
    name, student_id = extract_student_info(path, text)
    return ReportInfo(
        source_path=str(path), source_sha256=source_hash, source_file=path.name,
        student_name=name, student_id=student_id, page_count=1,
        full_text=f"【第1页】\n{text}" if text else "", pages=[page],
    )


def merge_reports(infos: list[ReportInfo], folder: Path) -> ReportInfo:
    """把同一学生文件夹里的多个文件(如作文的多张照片)合成一份作答, 页码顺延。"""
    if len(infos) == 1:
        return infos[0]
    pages: list[PageInfo] = []
    parts: list[str] = []
    for info in infos:
        for p in info.pages:
            n = len(pages) + 1
            pages.append(replace(p, page_num=n))
            parts.append(f"【第{n}页】\n{p.text}")
    full_text = "\n\n".join(parts)
    digest = hashlib.sha256("".join(i.source_sha256 for i in infos).encode()).hexdigest()
    name, student_id = extract_student_info(folder, full_text)
    return ReportInfo(
        source_path=str(folder.resolve()), source_sha256=digest, source_file=folder.name,
        student_name=name, student_id=student_id, page_count=len(pages),
        full_text=full_text, pages=pages,
    )


SUPPORTED_EXT = {".pdf", ".txt", ".md", ".docx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def is_supported(filename: str, allow_images: bool = False) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in SUPPORTED_EXT or (allow_images and ext in IMAGE_EXT)


def report_from_dict(data: dict) -> ReportInfo:
    fields = ReportInfo.__dataclass_fields__
    values = {k: v for k, v in data.items() if k in fields and k != "pages"}
    values["pages"] = [PageInfo(**{k: v for k, v in p.items() if k in PageInfo.__dataclass_fields__})
                       for p in data.get("pages", [])]
    return ReportInfo(**values)
