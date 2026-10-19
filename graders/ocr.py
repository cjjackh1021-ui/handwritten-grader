"""Local OCR with provenance, normalized top-left coordinates and content cache."""
from __future__ import annotations
import difflib
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from . import config, local_workers

MODES = ("off", "tesseract", "paddle", "hybrid")
_LOCK = threading.RLock()


def mode(value=None):
    value = value or os.getenv("HANDWRITING_OCR_MODE", "hybrid")
    if value not in MODES:
        raise ValueError("未知 OCR 模式")
    return value


def valid_box(box):
    try:
        x0, y0, x1, y1 = map(float, box)
        return 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    except (TypeError, ValueError):
        return False


def tesseract(image_path):
    from PIL import Image
    import pytesseract
    command = config.find_tesseract()
    if not command:
        raise RuntimeError("Tesseract 未安装")
    pytesseract.pytesseract.tesseract_cmd = command
    with Image.open(image_path) as im:
        width, height = im.size
        for lang in ("chi_sim+eng", "eng"):
            try:
                data = pytesseract.image_to_data(im, lang=lang, output_type=pytesseract.Output.DICT)
                break
            except pytesseract.TesseractError:
                if lang == "eng": raise
    blocks = []
    for i, text in enumerate(data["text"]):
        if not text.strip(): continue
        x, y, w, h = [data[k][i] for k in ("left", "top", "width", "height")]
        if not w or not h: continue
        blocks.append({"id": f"tesseract-{i}", "text": text, "confidence": max(0, float(data["conf"][i]))/100,
                       "bbox": [x/width, y/height, (x+w)/width, (y+h)/height], "kind": "text"})
    return {"engine": "tesseract", "model": "tesseract", "text": "\n".join(b["text"] for b in blocks), "blocks": blocks}


def recognize(image_path, work_dir, selected=None):
    selected = mode(selected)
    if selected == "off": return {"mode": selected, "results": [], "blocks": [], "text": "", "issues": []}
    engines = ["paddle", "chandra"] if selected == "hybrid" else [selected]
    results, issues = [], []
    digest = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
    cache = Path(work_dir) / "ocr_cache"
    cache.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        for engine in engines:
            identity = config.BASE_DIR / "models" / engine / "ready.json"
            version = hashlib.sha256(identity.read_bytes()).hexdigest() if identity.exists() else "missing"
            adapter = hashlib.sha256((config.BASE_DIR / "workers" / "runner.py").read_bytes()).hexdigest()[:8]
            target = cache / f"{digest}-{engine}-{version[:12]}-{adapter}-v2.json"
            started = time.monotonic()
            try:
                if target.is_file():
                    result = json.loads(target.read_text(encoding="utf-8"))
                    result = {**result, "cached": True}
                else:
                    result = tesseract(image_path) if engine == "tesseract" else local_workers.request(engine, {"image": str(image_path)})
                    for block in result["blocks"]:
                        if not valid_box(block.get("bbox")):
                            raise ValueError(f"{engine} 返回了无效区域")
                        block["engine"] = engine
                    result["seconds"] = round(time.monotonic()-started, 3)
                    target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                results.append(result)
            except Exception as exc:
                issues.append({"kind": "engine_unavailable", "engine": engine, "message": str(exc)[:500]})
        if not results and selected != "tesseract":
            try:
                results.append(tesseract(image_path))
            except Exception as exc:
                issues.append({"kind": "engine_unavailable", "engine": "tesseract", "message": str(exc)[:300]})
    blocks = [b for result in results for b in result["blocks"]]
    low = [b["id"] for b in blocks if b.get("confidence") is not None and b["confidence"] < .85]
    if low:
        issues.append({"kind": "low_confidence", "block_ids": low, "message": "OCR 存在低置信度文字"})
    if len(results) == 2:
        a, b = ["".join(r["text"].split()) for r in results]
        if difflib.SequenceMatcher(None, a, b).ratio() < .85:
            issues.append({"kind": "disagreement", "message": "两种 OCR 的转写存在差异，请对照原图"})
    text = next((r["text"] for r in results if r["engine"] == "paddle"), results[0]["text"] if results else "")
    return {"mode": selected, "results": results, "blocks": blocks, "text": text, "issues": issues,
            "enhancement_complete": not any(i["kind"] == "engine_unavailable" for i in issues)}
