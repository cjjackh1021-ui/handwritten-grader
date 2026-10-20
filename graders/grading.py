"""评分编排：prompt 构造、单份评分（含视觉回退）、抄袭检测、问题页汇总。"""

from __future__ import annotations

from pathlib import Path

from . import providers, eval_policy
from .extract import ReportInfo

SYSTEM_PROMPT = """你是一位经验丰富的实验报告批改老师。请严格按照用户提供的评分标准打分，并给出详细的给分依据。
只返回一个 JSON 对象，字段如下：
{
  "score": 整数总分(0-100),
  "comment": "简短评语(20字以内, 用于写在报告上)",
  "reason": "给分依据, 简要说明各维度得分与扣分/加分理由, 80字以内",
  "dimensions": [
    {"name": "维度名称(取自评分标准)", "score": 该维度得分, "full": 该维度满分, "comment": "该维度简评(可选)"}
  ]
}
要求：dimensions 覆盖评分标准里的每个维度；各维度 score 之和应等于总分 score；full 为该维度满分。"""

MAX_TEXT_CHARS = 8000
MAX_VISION_IMAGES = 4


def _visual_images(report: ReportInfo) -> list[str]:
    imgs = []
    for page in report.pages:
        if page.needs_visual and page.render_path and Path(page.render_path).is_file():
            imgs.append(page.render_path)
        if len(imgs) >= MAX_VISION_IMAGES:
            break
    return imgs


def build_user_prompt(criteria: str, report: ReportInfo, with_images: bool) -> str:
    truncated = report.full_text[:MAX_TEXT_CHARS]
    note = ""
    if with_images:
        note = (
            "\n注意：本报告包含图片密集页，随附了这些页面的截图，"
            "请结合图片内容（图表、代码截图、测试结果等）综合评分。"
        )
    return (
        f"学生：{report.student_name}\n"
        f"评分标准：\n{criteria}\n\n"
        f"报告文字内容：\n{truncated}{note}\n\n"
        "请根据评分标准给出分数、简短评语(20字内)和给分依据。"
    )


def grade_report(choice: providers.ModelChoice, criteria: str, report: ReportInfo) -> dict:
    """对单份报告评分。带图先走视觉，失败回退纯文本(含OCR)。"""
    images = _visual_images(report) if choice.supports_vision else []
    preset = providers.config.PROVIDERS.get(choice.provider, {})

    if images:
        try:
            user = build_user_prompt(criteria, report, with_images=True)
            # 视觉请求优先使用该 provider 的 vision_model（若用户未显式指定模型）
            vchoice = choice
            if not choice.model and preset.get("vision_model"):
                vchoice = providers.ModelChoice(
                    provider=choice.provider,
                    api_key=choice.api_key,
                    model=preset["vision_model"],
                    base_url=choice.base_url,
                    use_vision=True,
                )
            result = providers.grade(vchoice, SYSTEM_PROMPT + eval_policy.instructions(), user, images)
            return eval_policy.finalize(result, result, report.full_text)
        except providers.ProviderError:
            pass  # 回退纯文本

    user = build_user_prompt(criteria, report, with_images=False)
    result = providers.grade(choice, SYSTEM_PROMPT + eval_policy.instructions(), user, images=None)
    return eval_policy.finalize(result, result, report.full_text)


def format_dimensions(dims: list[dict]) -> str:
    """把分维度得分压成一行紧凑字符串: "原理18/18; 设计20/22; ..."。"""
    parts = []
    for d in dims or []:
        name = d.get("name", "")
        score = d.get("score")
        full = d.get("full")
        if score is None:
            continue
        s = f"{name}{_fmt_num(score)}"
        if full is not None:
            s += f"/{_fmt_num(full)}"
        parts.append(s)
    return "; ".join(parts)


def _fmt_num(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


def problem_pages(report: ReportInfo) -> tuple[str, str]:
    pages, reasons = [], []
    for page in report.pages:
        if page.has_issue:
            pages.append(str(page.page_num))
            reasons.append(f"第{page.page_num}页:{page.issue_reason}")
    return (", ".join(pages) if pages else "无", "; ".join(reasons) if reasons else "无")


# ---------------------------------------------------------------------------
# 抄袭检测（可选，沿用本地 TF-IDF）
# ---------------------------------------------------------------------------
def check_plagiarism(texts: list[dict], threshold: float = 0.8) -> dict[str, dict]:
    """texts: [{"key": 唯一键, "student": 名, "text": 文本}]，返回 key -> 结果。"""
    if len(texts) < 2:
        return {t["key"]: {"plagiarism": False, "similar_with": []} for t in texts}
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    docs = [t["text"] or "" for t in texts]
    vectorizer = TfidfVectorizer(stop_words=None, token_pattern=r"(?u)\b\w+\b")
    result: dict[str, dict] = {}
    try:
        matrix = vectorizer.fit_transform(docs)
        sim = cosine_similarity(matrix)
    except Exception:
        return {t["key"]: {"plagiarism": False, "similar_with": []} for t in texts}
    for i, item in enumerate(texts):
        similar = []
        for j, other in enumerate(texts):
            if i != j and sim[i][j] > threshold:
                similar.append({"student": other["student"], "similarity": round(float(sim[i][j]), 3)})
        result[item["key"]] = {"plagiarism": bool(similar), "similar_with": similar}
    return result
