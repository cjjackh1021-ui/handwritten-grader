"""自动识别模式离线测试: 路由桩按 system prompt 区分「拆题/答案卷/解题/批改」调用。

运行: python tests/test_autodetect.py   (也兼容 pytest)
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graders import assignments, autodetect, extract, providers  # noqa: E402

CHOICE = providers.ModelChoice(provider="deepseek", api_key="sk-test", use_vision=False)

PAPER = """期中测试  姓名：王五
一、选择题（每小题3分）
1. 下列属于质数的是（ ）A.4 B.6 C.7 D.9     答：C
2. 下列说法正确的有（ ）A... B... C... D...   答：A
二、填空题
3. 3的平方是____    答：9
三、解答题（12分）
4. 解方程 2x+1=5    解：2x=4，x=3
四、作文（40分）
5. 以「春天」为题写一篇不少于300字的短文。   春天来了……
"""

SEGMENT = {
    "doc_type": "exam", "subject": "综合", "student_name": "王五", "student_id": "",
    "questions": [
        {"no": "1", "type": "choice", "question": "下列属于质数的是 A.4 B.6 C.7 D.9", "student_answer": "C", "full_score": 3, "pages": [1]},
        {"no": "2", "type": "choice", "question": "下列说法正确的有（多选）", "student_answer": "A", "full_score": 3, "pages": [1]},
        {"no": "3", "type": "fill", "question": "3的平方是____", "student_answer": "9", "full_score": None, "pages": [1]},
        {"no": "4", "type": "math", "question": "解方程 2x+1=5", "student_answer": "2x=4，x=3", "full_score": 12, "pages": [1]},
        {"no": "5", "type": "essay", "question": "以「春天」为题写一篇不少于300字的短文", "student_answer": "春天来了" * 20,
         "full_score": 40, "pages": [1], "word_limit": 300, "genre": "记叙文"},
        {"no": "6", "type": "short", "question": "简述光合作用", "student_answer": "", "full_score": 5, "pages": [1]},
    ],
}

KEY = {"questions": [
    {"no": "1", "answer": "C"}, {"no": "2", "answer": "AC"}, {"no": "3", "answer": "9"},
    {"no": "4", "answer": "2x=4, x=2", "final_answer": "x=2", "points": "移项 | 4\n求解 | 4\n作答 | 4", "full_score": 12},
    {"no": "6", "answer": "光能转化为化学能", "points": "", "full_score": 5},
]}

MATH_GRADE = {"steps": [{"name": "移项", "full": 4, "score": 4, "correct": True},
                        {"name": "求解", "full": 4, "score": 0, "correct": False, "note": "4÷2 算错"},
                        {"name": "作答", "full": 4, "score": 2, "correct": False}],
              "student_final_answer": "x=3", "reference_final_answer": "x=2", "final_correct": False,
              "score": 6, "comment": "第2步计算错误", "reason": "除法算错"}


def essay_grade(full=40):
    dims, _ = assignments.TaskSpec(task_type="essay", question="q", full_score=full).points()
    return {"off_topic": False, "dimensions": [{"name": d["name"], "full": d["full"], "score": d["full"] * 0.75} for d in dims],
            "highlights": ["比喻生动"], "suggestions": ["结尾点题"], "score": full * 0.75,
            "comment": "语言生动", "reason": "内容充实"}


class Router:
    def __init__(self, segment=SEGMENT, key=KEY, lab_score=None):
        self.segment, self.key = segment, key
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()

    def hit(self, name):
        with self.lock:
            self.calls[name] = self.calls.get(name, 0) + 1

    def __call__(self, choice, system, user, images=None, max_tokens=None):
        if system == autodetect.SEGMENT_SYSTEM:
            self.hit("segment")
            assert max_tokens == autodetect.SEGMENT_MAX_TOKENS
            return json.dumps(self.segment, ensure_ascii=False)
        if system == autodetect.ANSWER_KEY_SYSTEM:
            self.hit("key")
            return json.dumps(self.key, ensure_ascii=False)
        if system == autodetect.SOLVE_OBJECTIVE_SYSTEM:
            self.hit("solve_objective")
            return json.dumps({"answers": [{"no": "1", "answer": "C"}, {"no": "2", "answer": "A"}, {"no": "3", "answer": "9"}]})
        if system == autodetect.SOLVE_SUBJECTIVE_SYSTEM:
            self.hit("solve_subjective")
            return json.dumps({"reference": "2x=4, x=2", "final_answer": "x=2",
                               "points": [{"name": "移项", "score": 4}, {"name": "求解", "score": 4}, {"name": "作答", "score": 4}]})
        if system == assignments.SYSTEM_PROMPTS["math"]:
            self.hit("grade_math")
            return json.dumps(MATH_GRADE, ensure_ascii=False)
        if system == assignments.SYSTEM_PROMPTS["essay"]:
            self.hit("grade_essay")
            return json.dumps(essay_grade(), ensure_ascii=False)
        if "实验报告批改老师" in system:
            self.hit("lab")
            return json.dumps({"score": 85, "comment": "完整", "reason": "ok", "dimensions": []})
        raise AssertionError("未预期的调用: " + system[:40])


def patch(router):
    original = providers.complete
    providers.complete = router
    return original


def text_report(text, name="paper.txt"):
    tmp = Path(tempfile.mkdtemp()) / name
    tmp.write_text(text, encoding="utf-8")
    return extract.inspect_text_file(tmp)


def lab_grader(report, others):
    from graders import grading
    return grading.grade_report(CHOICE, "原理 100分", report)


# ---------------------------------------------------------------------------
def test_heuristic():
    assert autodetect.heuristic_doc_type("实验目的：... 实验原理：... 实验步骤：...")[0] == "lab"
    assert autodetect.heuristic_doc_type(PAPER)[0] is None
    assert autodetect.norm_no("第 17(1) 题") == "17(1)" and autodetect.norm_no("二、3.") == "二、3"


def test_objective_rules():
    q = lambda t, a: {"type": t, "student_answer": a}  # noqa: E731
    g = autodetect.grade_objective
    assert g(q("choice", "C"), {"answer": "C"}, 3)["score"] == 3
    assert g(q("choice", "A"), {"answer": "AC"}, 4)["comment"] == "少选" and g(q("choice", "A"), {"answer": "AC"}, 4)["score"] == 2
    assert g(q("choice", "AB"), {"answer": "AC"}, 4)["score"] == 0
    assert g(q("judge", "√"), {"answer": "对"}, 2)["score"] == 2
    assert g(q("judge", "×"), {"answer": "正确"}, 2)["score"] == 0
    assert g(q("fill", "1/2"), {"answer": "0.5"}, 3)["score"] == 3
    assert g(q("fill", "叶绿体"), {"answer": "叶绿素"}, 3) is None  # 文字不同交给模型


def test_lab_detected_without_model_call():
    router = Router()
    original = patch(router)
    try:
        ctx = autodetect.AutoContext(choice=CHOICE, lab_grader=lab_grader)
        r = autodetect.grade_auto(ctx, text_report("实验目的：测电阻\n实验原理：欧姆定律\n实验步骤：..."))
    finally:
        providers.complete = original
    assert r["task_type"] == "lab" and r["detected"]["by"] == "keywords" and r["score"] == 85
    assert "segment" not in router.calls


def test_exam_with_answer_key():
    router = Router()
    original = patch(router)
    try:
        key = text_report("1.C 2.AC 3.9 4.x=2", "参考答案.txt")
        ctx = autodetect.AutoContext(choice=CHOICE, lab_grader=lab_grader, key_reports=[key])
        report = text_report(PAPER)
        r = autodetect.grade_auto(ctx, report)
    finally:
        providers.complete = original
    qs = {q["no"]: q for q in r["details"]["questions"]}
    assert qs["1"]["score"] == 3 and qs["1"]["graded_by"] == "program"
    assert qs["2"]["score"] == 1.5 and qs["2"]["comment"] == "少选"
    assert qs["3"]["score"] == 3 and qs["3"]["default_score"]          # 卷面没标分, 用默认 3 分
    assert qs["4"]["score"] == 6 and qs["4"]["details"]["final_correct"] is False
    assert qs["5"]["score"] == 30 and qs["5"]["reference_source"] == "none"
    assert qs["6"]["score"] == 0 and qs["6"]["comment"] == "未作答"    # 未作答不调模型
    assert all(q["reference_source"] == "key" for n, q in qs.items() if n != "5")
    assert r["score"] == int(3 + 1.5 + 3 + 6 + 30 + 0 + 0.5) and r["full_score"] == 3 + 3 + 3 + 12 + 40 + 5
    assert r["task_type"] == "exam" and report.student_name == "王五"
    assert "第2,4,5,6题需订正" in r["comment"] and len(r["comment"]) <= 20
    assert router.calls.get("solve_objective") is None and router.calls.get("solve_subjective") is None
    assert router.calls["key"] == 1


def test_exam_without_key_solves_once_per_batch():
    router = Router()
    original = patch(router)
    try:
        ctx = autodetect.AutoContext(choice=CHOICE, lab_grader=lab_grader)
        reports = [text_report(PAPER, f"s{i}.txt") for i in range(4)]
        threads = [threading.Thread(target=lambda rp=rp: results.append(autodetect.grade_auto(ctx, rp))) for rp in reports]
        results: list = []
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        providers.complete = original
    assert len(results) == 4
    assert router.calls["segment"] == 4
    assert router.calls["solve_objective"] == 1 and router.calls["solve_subjective"] == 1  # 第6题未作答, 不解题
    q = {x["no"]: x for x in results[0]["details"]["questions"]}
    assert q["2"]["reference_source"] == "ai" and q["2"]["score"] == 3  # AI 参考答案为 A
    assert results[0]["details"]["ai_reference_count"] >= 4


def test_segment_says_lab():
    seg = {"doc_type": "lab_report", "questions": []}
    router = Router(segment=seg)
    original = patch(router)
    try:
        ctx = autodetect.AutoContext(choice=CHOICE, lab_grader=lab_grader)
        r = autodetect.grade_auto(ctx, text_report("课程设计总结：本次设计了一个系统……"))
    finally:
        providers.complete = original
    assert r["task_type"] == "lab" and r["detected"]["by"] == "model"


def test_flask_auto_mode():
    import app as webapp

    router = Router()
    original = patch(router)
    client = webapp.app.test_client()
    files = [
        (io.BytesIO("1.C 2.AC 3.9 4.x=2".encode()), "期中/参考答案.txt"),
        (io.BytesIO(PAPER.encode()), "期中/2023000001张三.txt"),
        (io.BytesIO(PAPER.encode()), "期中/2023000002李四.txt"),
        (io.BytesIO("实验目的：测电阻\n实验原理：欧姆定律\n实验步骤：连接电路".encode()), "期中/2023000003赵六.txt"),
    ]
    data = {"task_type": "auto", "provider": "deepseek", "api_key": "sk-test", "use_vision": "false",
            "ocr_enabled": "false", "grading_criteria": "", "engine": "simple",
            "reports": [(f, n) for f, n in files]}
    try:
        resp = client.post("/grade", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        events = [json.loads(x[5:]) for x in resp.get_data(as_text=True).split("\n\n") if x.startswith("data:")]
    finally:
        providers.complete = original
    errors = [e for e in events if e["type"] == "error"]
    assert not errors, errors
    done = next(e for e in events if e["type"] == "done")
    by_file = {r["source_file"]: r for r in done["results"]}
    assert set(by_file) == {"2023000001张三.txt", "2023000002李四.txt", "2023000003赵六.txt"}  # 答案卷不算作答
    assert by_file["2023000003赵六.txt"]["task_type"] == "lab"
    assert by_file["2023000001张三.txt"]["task_type"] == "exam" and by_file["2023000001张三.txt"]["full_score"] == 66
    assert router.calls["key"] == 1
    stage = next(e for e in events if e["type"] == "stage" and e["stage"] == "grade")
    assert "参考答案.txt" in stage["message"]
    shutil.rmtree(webapp.JOBS[done["job_id"]]["job_dir"], ignore_errors=True)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
