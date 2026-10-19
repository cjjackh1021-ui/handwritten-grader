"""Run with an engine-specific interpreter; stdout is exclusively JSONL."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = sys.argv[1]
MODEL = None


def paddle(data):
    global MODEL
    from PIL import Image
    if MODEL is None:
        from paddleocr import PaddleOCR
        base = Path("models") / "paddle"
        MODEL = PaddleOCR(device="cpu", text_detection_model_name="PP-OCRv6_medium_det",
                          text_recognition_model_name="PP-OCRv6_medium_rec",
                          text_detection_model_dir=str(base / "det"), text_recognition_model_dir=str(base / "rec"),
                          use_doc_orientation_classify=False, use_doc_unwarping=False,
                          use_textline_orientation=False)
    with Image.open(data["image"]) as image:
        width, height = image.size
    result = next(iter(MODEL.predict(data["image"]))).json
    if isinstance(result, str):
        result = json.loads(result)
    result = result.get("res", result)
    blocks = []
    for i, (text, score, poly) in enumerate(zip(result["rec_texts"], result["rec_scores"], result["rec_polys"])):
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        blocks.append({"id": f"paddle-{i}", "text": text, "confidence": float(score), "kind": "text",
                       "order": i, "polygon": [[p[0]/width,p[1]/height] for p in poly],
                       "bbox": [min(xs)/width, min(ys)/height, max(xs)/width, max(ys)/height]})
    return {"engine": "paddle", "model": "PP-OCRv6_medium", "blocks": blocks,
            "text": "\n".join(b["text"] for b in blocks)}


def chandra(data):
    global MODEL
    import torch
    from PIL import Image
    if torch.cuda.is_available(): torch.cuda.reset_peak_memory_stats()
    if MODEL is None:
        if not torch.cuda.is_available():
            raise RuntimeError("Chandra 需要可用 CUDA GPU；增强识别未完成")
        free, _ = torch.cuda.mem_get_info()
        minimum = int(os.getenv("CHANDRA_MIN_FREE_MB", "10000"))
        if free < minimum * 1024**2:
            raise RuntimeError(f"Chandra 可用显存不足（需 {minimum} MiB），增强识别未完成")
        os.environ["MODEL_CHECKPOINT"] = str(ROOT / "models" / "chandra" / "weights")
        os.environ["TORCH_DEVICE"] = "cuda:0"
        os.environ["TORCH_ATTN"] = "sdpa"
        from chandra.model import InferenceManager
        MODEL = InferenceManager(method="hf")
    from chandra.model.schema import BatchInputItem
    with Image.open(data["image"]) as image:
        image = image.convert("RGB")
        width, height = image.size
        result = MODEL.generate([BatchInputItem(image=image, prompt_type="ocr_layout")],
                                max_output_tokens=8192, include_images=False, include_headers_footers=True)[0]
    if result.error or result.token_count >= 8192:
        raise RuntimeError("Chandra 输出失败或达到长度限制，需要复核")
    blocks = []
    from bs4 import BeautifulSoup
    for i, chunk in enumerate(result.chunks):
        x0, y0, x1, y1 = chunk["bbox"]
        blocks.append({"id": f"chandra-{i}", "text": BeautifulSoup(chunk["content"], "html.parser").get_text(" "),
                       "order": i, "html": chunk["content"],
                       "confidence": None, "kind": chunk["label"],
                       "bbox": [x0/width, y0/height, x1/width, y1/height]})
    return {"engine": "chandra", "model": "chandra-ocr-2", "blocks": blocks, "text": result.markdown, "html": result.html,
            "runtime": {"gpu_peak_allocated_mb": round(torch.cuda.max_memory_allocated()/1024**2),
                        "gpu_peak_reserved_mb": round(torch.cuda.max_memory_reserved()/1024**2)}}


def handwriting(data):
    from PIL import Image, ImageFont, ImageOps
    from fontTools.ttLib import TTFont
    from handright import Template, handwrite
    manifest = json.loads((ROOT / "assets/handwriting/manifest.json").read_text(encoding="utf-8"))
    paths = [ROOT / "assets/handwriting" / f["file"] for f in manifest["fonts"]]
    if data.get("style") == "kaishu":
        paths.reverse()
    text = data.get("text", "")
    size = int(data.get("size", 96))
    seed = data.get("seed", "")
    random.seed(int(hashlib.sha256(str(seed).encode()).hexdigest()[:16], 16))
    def glyphs(value):
        # Validate every glyph; never use a system font or a missing-glyph box.
        for path in paths:
            with TTFont(path) as font:
                cmap = font.getBestCmap()
            probe = ImageFont.truetype(str(path), size=size)
            if all(c.isspace() or (ord(c) in cmap and probe.getmask(c).getbbox()) for c in value):
                return path
        raise ValueError("项目手写字体缺字: " + value)
    def render(value, font_size):
        path = glyphs(value)
        font = ImageFont.truetype(str(path), size=font_size)
        background = Image.new("RGB", (max(256, int(font_size*(len(value)+2)*1.5)), font_size*4), "white")
        template = Template(background=background, font=font, line_spacing=font_size*2,
                            left_margin=font_size, right_margin=font_size, top_margin=font_size,
                            bottom_margin=0, word_spacing=2, line_spacing_sigma=0,
                            font_size_sigma=1, word_spacing_sigma=1, perturb_x_sigma=1,
                            perturb_y_sigma=1, perturb_theta_sigma=0.02,
                            strikethrough_probability=0, ink_depth_sigma=3)
        pages = list(handwrite(value, template, seed=int(hashlib.sha256(str(seed).encode()).hexdigest()[:16], 16)))
        if len(pages) != 1:
            raise ValueError("手写内容超出批注画布")
        alpha = ImageOps.invert(pages[0].convert("L"))
        box = alpha.getbbox()
        if not box:
            raise ValueError("手写渲染为空")
        alpha = alpha.crop(box)
        result = Image.new("RGBA", alpha.size, (205, 30, 35, 0))
        result.putalpha(alpha)
        return result
    if data.get("mark") == "partial":
        tick, cross = render("✓", size), render("×", int(size*.55))
        image = Image.new("RGBA", (tick.width+cross.width//3, tick.height+cross.height//2))
        image.alpha_composite(tick, (0, cross.height//2))
        image.alpha_composite(cross, (image.width-cross.width, 0))
    else:
        text = {"correct": "✓", "incorrect": "×"}.get(data.get("mark"), text)
        image = render(text, size)
    destination = Path(data["output"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return {"path": str(destination), "width": image.width, "height": image.height}


for line in sys.stdin:
    saved_stdout = os.dup(1)
    try:
        # Paddle native libraries write directly to fd 1, bypassing Python stdout.
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            result = {"paddle": paddle, "chandra": chandra, "handwriting": handwriting}[ENGINE](json.loads(line))
        reply = {"ok": True, "result": result}
    except Exception as exc:
        if ENGINE == "chandra":
            MODEL = None
            import gc
            gc.collect()
            if "torch" in sys.modules and sys.modules["torch"].cuda.is_available(): sys.modules["torch"].cuda.empty_cache()
        reply = {"ok": False, "error": f"{ENGINE}: {type(exc).__name__}: {exc}"[:1000]}
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
    print(json.dumps(reply, ensure_ascii=False), flush=True)
