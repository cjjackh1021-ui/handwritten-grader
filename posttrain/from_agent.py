"""把真实批改 Agent 的轨迹送进 :func:`posttrain.export.export_all`。

两条入口：

- **现场**：``collect_live`` 调 :func:`graders.agent.grade_report_agent`，用模型真实
  tool-calling 对话（不是玩具策略），按文件分组后 ``export_all``。
- **落盘**：网页 Agent/多智能体批改结束时写出 ``output/<job>/agent_rollouts.json``。
  ``load_saved_groups`` 读这些文件（旧任务则回退 ``job.json`` 里的 trace），
  用 ``labels.json`` 重算奖励后再导出。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from graders import agent as grader_agent
from posttrain.export import export_all, rollout_record


def _page(needs_visual: bool):
    return SimpleNamespace(needs_visual=needs_visual, page_num=1)


def report_view(file: str, student: str, needs_visual: bool = False):
    """落盘轨迹没有 ReportInfo 时, 补一个奖励函数能读的轻量对象。"""
    return SimpleNamespace(
        source_file=file,
        student_name=student or file,
        student_id="",
        page_count=1,
        full_text="",
        pages=[_page(needs_visual)],
    )


def record_from_agent(report, criteria: str, result: dict, label: dict | None = None,
                      extra: dict | None = None) -> dict:
    """真实 agent 返回值 → export_all 的一条 rollout。优先用原始 messages。"""
    grade = {
        "score": result.get("score"),
        "comment": result.get("comment", ""),
        "reason": result.get("reason", ""),
        "dimensions": result.get("dimensions") or [],
    }
    rec = rollout_record(
        report, criteria, grade, label or {},
        result.get("trace") or [],
        messages=result.get("messages") or None,
        extra=extra,
    )
    rec["source"] = extra.get("source") if extra and extra.get("source") else "agent"
    rec["engine"] = result.get("engine", "agent")
    return rec


def collect_live(items: list[dict], choice, criteria: str, repeats: int = 1) -> list[list[dict]]:
    """对标注集逐份跑真实 agent。repeats>1 时同题多条轨迹, 才能组成组内 DPO。"""
    groups = []
    for it in items:
        report = it["report"]
        recs = []
        for k in range(max(1, repeats)):
            result = grader_agent.grade_report_agent(choice, criteria, report)
            recs.append(record_from_agent(
                report, criteria, result, it.get("label") or {},
                extra={"source": "agent-live", "repeat": k},
            ))
        groups.append(recs)
    return groups


def capture_rows(graded_list: list[dict], criteria: str) -> list[dict]:
    """从即将返回网页的结果里抽出训练行, 并删掉不该进前端的字段。"""
    rows = []
    for entry in graded_list:
        messages = entry.pop("_messages", None)
        needs_visual = bool(entry.pop("_needs_visual", False))
        trace = entry.get("trace") or []
        if entry.get("status") != "success":
            continue
        if entry.get("task_type", "lab") != "lab":  # 奖励与环境按实验报告 rubric 设计, 作业题型不混入
            continue
        if not trace and not messages:
            continue
        rows.append({
            "file": entry.get("source_file") or "",
            "student": entry.get("student_name") or "",
            "source_path": entry.get("source_path") or "",
            "needs_visual": needs_visual,
            "criteria": criteria,
            "engine": entry.get("engine", ""),
            "grade": {
                "score": entry.get("score"),
                "comment": entry.get("comment", ""),
                "reason": entry.get("reason", ""),
                "dimensions": entry.get("dimensions") or [],
            },
            "trace": trace,
            "messages": messages or [],
        })
    return rows


def write_job_rollouts(job_dir, graded_list: list[dict], criteria: str) -> int:
    """网页任务落盘。无 agent 轨迹时不写文件。"""
    rows = capture_rows(graded_list, criteria)
    if not rows:
        return 0
    path = Path(job_dir) / "agent_rollouts.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def _load_labels(dataset: str | None) -> dict:
    if not dataset:
        return {}
    path = Path(dataset)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    labels_path = path / "labels.json"
    if labels_path.is_file():
        return json.loads(labels_path.read_text(encoding="utf-8"))
    return {}


def _match_label(labels: dict, file_name: str) -> dict:
    if not labels or not file_name:
        return {}
    name = Path(file_name).name
    if name in labels:
        return labels[name]
    if file_name in labels:
        return labels[file_name]
    for key, val in labels.items():
        if Path(key).name == name:
            return val
    return {}


def _rows_from_job_json(path: Path) -> list[dict]:
    job = json.loads(path.read_text(encoding="utf-8"))
    criteria = job.get("criteria") or ""
    rows = []
    for entry in job.get("reports") or []:
        trace = entry.get("trace") or []
        if not trace:
            continue
        rows.append({
            "file": entry.get("source_file") or "",
            "student": entry.get("student_name") or "",
            "source_path": entry.get("source_path") or "",
            "needs_visual": any(s.get("type") == "vision" for s in trace),
            "criteria": criteria,
            "engine": entry.get("engine", ""),
            "grade": {
                "score": entry.get("score"),
                "comment": entry.get("comment", ""),
                "reason": entry.get("reason", ""),
                "dimensions": entry.get("dimensions") or [],
            },
            "trace": trace,
            "messages": entry.get("messages") or [],
        })
    return rows


def iter_saved_rows(jobs_root: Path) -> list[dict]:
    root = Path(jobs_root)
    rows: list[dict] = []
    if root.is_file() and root.name == "agent_rollouts.json":
        return json.loads(root.read_text(encoding="utf-8"))
    if root.is_file() and root.name == "job.json":
        return _rows_from_job_json(root)
    direct = root / "agent_rollouts.json"
    if direct.is_file():
        return json.loads(direct.read_text(encoding="utf-8"))
    direct_job = root / "job.json"
    if direct_job.is_file() and not any(root.glob("*/job.json")):
        return _rows_from_job_json(direct_job)
    seen_jobs = set()
    for path in sorted(root.glob("*/agent_rollouts.json")):
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
        seen_jobs.add(path.parent.resolve())
    for path in sorted(root.glob("*/job.json")):
        if path.parent.resolve() in seen_jobs:
            continue
        rows.extend(_rows_from_job_json(path))
    return rows


def load_saved_groups(jobs_root: Path, dataset: str | None = None) -> list[list[dict]]:
    """按文件名分组。同一份报告的多次批改放在一组, 供 RFT/DPO。"""
    labels = _load_labels(dataset)
    grouped: dict[str, list[dict]] = {}
    for row in iter_saved_rows(Path(jobs_root)):
        file_name = row.get("file") or row.get("student") or "unknown"
        view = report_view(file_name, row.get("student") or "", bool(row.get("needs_visual")))
        criteria = row.get("criteria") or ""
        label = _match_label(labels, file_name)
        rec = record_from_agent(
            view, criteria,
            {
                "score": (row.get("grade") or {}).get("score"),
                "comment": (row.get("grade") or {}).get("comment", ""),
                "reason": (row.get("grade") or {}).get("reason", ""),
                "dimensions": (row.get("grade") or {}).get("dimensions") or [],
                "trace": row.get("trace") or [],
                "messages": row.get("messages") or [],
                "engine": row.get("engine") or "agent",
            },
            label,
            extra={"source": "agent-saved"},
        )
        grouped.setdefault(file_name, []).append(rec)
    return list(grouped.values())


def export_groups(groups: list[list[dict]], out_dir: Path, rft_k: int = 1) -> dict:
    if not groups:
        raise FileNotFoundError("没有可导出的 agent 轨迹")
    return export_all(groups, out_dir, rft_k=rft_k)
