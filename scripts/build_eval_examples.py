"""Build clearly synthetic protocol fixtures, never human-accuracy evidence."""
import copy
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.suites import write_json


def main():
    root = ROOT / "eval" / "examples"
    specs = {
        "short": dict(question="简述光合作用的条件和产物", reference="需要光照、叶绿体；产生有机物和氧气",
                      full_score=10, points_text="光照 | 3\n叶绿体 | 3\n产物 | 4"),
        "math": dict(question="解方程 2x+1=5", reference="2x=4，x=2", full_score=6,
                     points_text="移项 | 2\n除法 | 2\n答案 | 2", final_answer="x=2"),
        "essay": dict(question="写一段关于成长的短文", full_score=10,
                      points_text="内容 | 4\n结构 | 3\n语言 | 3", word_limit=0),
        "lab": dict(full_score=100),
    }
    answers = {
        "short": ("需要光照和叶绿体，产生有机物和氧气。", "需要光照，产生有机物和氧气。", "需要光照和[关键字无法辨认]，产生有机物和氧气。"),
        "math": ("2x=4，x=2", "2x=4，x=3", "2x=4，x=[无法辨认]"),
        "essay": ("我在一次失败后学会了坚持。第二天，我又开始练习。", "我在一次失败后学会了坚持。我我开心。", "我学会了[关键句无法辨认]。"),
        "lab": ("实验原理正确。测量三次并记录数据，比较重复性和误差。", "实验原理正确。只测了一次，没有重复性分析。", "实验原理正确。关键测量结果[无法辨认]。"),
    }
    names = {"short": ["光照", "叶绿体", "产物"], "math": ["移项", "除法", "答案"],
             "essay": ["内容", "结构", "语言"], "lab": ["原理", "数据"]}
    fulls = {"short": [3, 3, 4], "math": [2, 2, 2], "essay": [4, 3, 3], "lab": [40, 60]}
    partials = {"short": [3, 0, 4], "math": [2, 0, 0], "essay": [4, 3, 0], "lab": [40, 10]}
    reasons = {
        "short": [("r2", "missing", "")],
        "math": [("r2", "arithmetic", "x=3"), ("r3", "wrong_answer", "x=3")],
        "essay": [("r3", "language", "我我开心")],
        "lab": [("r2", "insufficient_data", "只测了一次")],
    }
    for kind, spec in specs.items():
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        manifest = dict(id=f"synthetic-{kind}-v1", task_type=kind, subject="协议回归示例",
                        rubric_version="synthetic-v1", judge_version="evidence-v1", input_mode="text",
                        description="人工构造的功能测试材料；没有真实学生数据，也不是人工准确率验收。")
        write_json(directory / "suite.json", manifest)
        cases, labels, replies = [], [], {}
        for group in range(1, 4):
            for state in ("full", "partial", "review"):
                cid = f"{kind}-g{group}-{state}"
                idx = ("full", "partial", "review").index(state)
                rules = [{"id": f"r{i+1}", "name": n, "full": f,
                          "reason_codes": ["missing", "arithmetic", "wrong_answer", "language", "insufficient_data"]}
                         for i, (n, f) in enumerate(zip(names[kind], fulls[kind]))]
                inp = dict(student_id=cid, student_name="合成测试作答", answer=answers[kind][idx], rules=rules)
                if kind == "lab":
                    inp["criteria"] = "原理40分，数据60分。数据不足须说明证据；关键记录无法辨认则复核。"
                else:
                    inp["task"] = dict(task_type=kind, **spec)
                cases.append(dict(id=cid, group_id=f"family-{group}", stratum="text", input=inp))
                scores = partials[kind] if state == "partial" else fulls[kind]
                deductions = [{"rule_id": rid, "reason_code": reason, "evidence_any": [quote]}
                              for rid, reason, quote in reasons[kind]] if state == "partial" else []
                labels.append(dict(id=cid, score=sum(scores), dimensions={r["id"]: s for r, s in zip(rules, scores)},
                                   review_required=state == "review", human_verified=False, deductions=deductions))
                raw = dict(score=sum(scores), comment="测试评语", reason="依据公开评分项判断",
                           deductions=[{"rule_id": d["rule_id"], "reason_code": d["reason_code"],
                                        "evidence": d["evidence_any"][0], "reason": "对应评分项存在该问题"} for d in deductions],
                           review_required=state == "review", review_reason="关键字迹无法辨认" if state == "review" else "")
                if kind == "short":
                    raw["points"] = [dict(point=r["name"], full=r["full"], score=s, evidence=inp["answer"]) for r, s in zip(rules, scores)]
                elif kind == "math":
                    raw.update(steps=[dict(name=r["name"], full=r["full"], score=s, correct=s == r["full"], note="对照步骤") for r, s in zip(rules, scores)],
                               student_final_answer="x=3" if state == "partial" else "x=2", final_correct=state != "partial")
                else:
                    raw["dimensions"] = [dict(name=r["name"], full=r["full"], score=s, comment="分项判断") for r, s in zip(rules, scores)]
                    if kind == "essay":
                        raw.update(highlights=["有具体经历"], suggestions=["核对句子表达"], off_topic=False)
                if kind == "lab":
                    def call(name, args):
                        return {"role": "assistant", "content": "", "tool_calls": [{
                            "id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
                    replies[cid] = [call("get_report_info", {}), call("read_text", {}), call("submit_grade", raw)]
                else:
                    replies[cid] = [raw]
        for file, data in [("cases.jsonl", cases), ("labels.private.jsonl", labels)]:
            (directory / file).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in data) + "\n", encoding="utf-8")
        write_json(directory / "replay.json", replies)
    write_json(root / "baseline.json", {"name": "baseline", "prompt_addendum": "", "require_evidence": False})
    write_json(root / "evidence.json", {"name": "evidence-v1", "prompt_addendum": "区分学生未写与字迹无法确认；不要猜测关键符号。", "require_evidence": True})
    print(root)


if __name__ == "__main__":
    main()
