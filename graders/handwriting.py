"""Cached transparent handwriting rendered by the isolated handwriting-web kernel."""
from pathlib import Path
import hashlib
import json
from . import config, local_workers


def render(directory, text="", mark=None, seed="", style="liguofu", size=96):
    if style not in ("liguofu", "kaishu"):
        raise ValueError("未知手写样式")
    manifest = config.BASE_DIR / "assets/handwriting/manifest.json"
    if not manifest.is_file():
        raise local_workers.WorkerError("手写字体尚未初始化，请运行 scripts/setup_handwriting.py --components handwriting")
    payload = dict(text=text, mark=mark, seed=str(seed), style=style, size=size, version=2)
    digest = hashlib.sha256(manifest.read_bytes()+json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    output = Path(directory) / "handwriting" / f"{digest}.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.is_file():
        local_workers.request("handwriting", {**payload, "output": str(output.resolve())})
    return output
