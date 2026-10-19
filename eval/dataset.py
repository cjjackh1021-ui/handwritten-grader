"""评估数据集：加载「报告 + 人工标注」，并提供离线合成数据集与 mock 批改器。

数据集目录结构::

    dataset_dir/
      labels.json          # {"文件名": {"score": 85, "dimensions": [{name,score,full},...]}}
      张三_实验报告.txt
      李四_实验报告.pdf
      ...

labels.json 的 dimensions 可选; 提供后即可计算「维度命中率」。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from graders import convert, extract  # noqa: E402


def load_labels(dataset_dir: Path) -> dict:
    f = Path(dataset_dir) / "labels.json"
    if not f.is_file():
        raise FileNotFoundError(f"缺少标注文件: {f}")
    return json.loads(f.read_text(encoding="utf-8"))


def load_report(path: Path):
    """把单个文件解析为 ReportInfo。"""
    path = Path(path).resolve()
    work = _ROOT / "output" / "_work"
    work.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract.inspect_pdf(path, work, ocr_enabled=True, render_for_vision=True)
    if ext in (".txt", ".md"):
        return extract.inspect_text_file(path)
    if ext == ".docx":
        conv = convert.convert_docx(path, work / "_staging")
        if conv.pdf_path:
            return extract.inspect_pdf(Path(conv.pdf_path), work, True, True)
        if conv.text is not None:
            return extract.report_from_text(path, conv.text, conv.source_sha256)
        raise ValueError(conv.error or "DOCX 无法解析")
    raise ValueError(f"不支持的文件类型: {ext}")


def load_dataset(dataset_dir: Path) -> list[dict]:
    """返回 [{"file", "report", "label"}]，按 labels.json 中出现的文件加载。"""
    dataset_dir = Path(dataset_dir)
    labels = load_labels(dataset_dir)
    items = []
    for filename, label in labels.items():
        path = dataset_dir / filename
        if not path.is_file():
            print(f"[warn] 跳过缺失文件: {path}")
            continue
        items.append({"file": filename, "report": load_report(path), "label": label})
    return items


# ---------------------------------------------------------------------------
# 离线 mock 批改器(无需 API, 用于演示评估流水线)
# ---------------------------------------------------------------------------
def _seed(*parts) -> float:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF  # 0..1 稳定伪随机


# 各引擎的模拟噪声幅度(分)。单次调用噪声最大, agent/多智能体逐步收敛。
_MOCK_NOISE = {"simple": 9.0, "agent": 4.0, "multi": 2.5, "framework": 4.5}


def mock_grade(report, label: dict, engine: str) -> dict:
    """确定性 mock：以人工标注为中心加「引擎相关噪声」生成预测。

    仅用于**离线演示评估流水线**(证明 MAE/维度命中率能区分不同引擎), 不代表真实模型表现。
    真实评估请用 run_eval.py 传入 --provider/--api-key。
    """
    true_score = float(label.get("score", 0))
    amp = _MOCK_NOISE.get(engine, 6.0)
    # 以 report 内容+引擎为种子, 生成 [-amp, amp] 的确定性偏移
    offset = (_seed(report.source_file, engine) - 0.5) * 2 * amp
    pred = max(0, min(100, round(true_score + offset)))

    # 维度：在标注维度上加更小噪声, 使之和≈总分
    true_dims = label.get("dimensions") or []
    dims = []
    if true_dims:
        raw = []
        for i, td in enumerate(true_dims):
            ts = float(td.get("score", 0))
            doff = (_seed(report.source_file, engine, i) - 0.5) * 2 * (amp / 2)
            full = td.get("full")
            val = ts + doff
            if full is not None:
                val = max(0, min(float(full), val))
            raw.append(val)
        # 缩放使维度和≈pred
        s = sum(raw) or 1.0
        for td, val in zip(true_dims, raw):
            scaled = round(val * pred / s, 1)
            dims.append({"name": td.get("name", ""), "score": scaled,
                         "full": td.get("full"), "comment": ""})
    return {
        "score": pred,
        "comment": f"[mock-{engine}]",
        "reason": f"离线模拟({engine})",
        "dimensions": dims,
        "engine": engine,
    }
