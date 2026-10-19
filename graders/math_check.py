"""数学最终答案的程序化核验。

模型判「最终答案对不对」容易被表述差异带偏（如 ``1/2`` 与 ``0.5``、``x=3`` 与 ``3``、
``2√3`` 与 ``sqrt(12)``）。这里先做规范化，再按优先级比较：

1. 装了 sympy：符号化简后判等（支持含字母的表达式）；
2. 否则：受限 AST 数值求值（只允许数字、四则、乘方、sqrt/pi/e）；
3. 都不行：规范化后的文本比较（选择题字母、文字结论）。

多解（``x=1或x=2``、``1, 2``）按无序集合比较。学生写了几位小数时，允许最后一位的误差
（``12.56`` 对 ``4π``），并在结果里标记 ``approx``。
"""

from __future__ import annotations

import ast
import math
import operator
import re
import unicodedata

try:  # 可选依赖
    import sympy
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )

    _SYMPY = True
    _TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
except Exception:  # noqa: BLE001
    _SYMPY = False

_SUPERSCRIPTS = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
                 "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
_PREFIX = re.compile(r"^(答案|答|解得|解|所以|因此|故|∴|结果|最终答案)[:：,，]?")
_SPLIT = re.compile(r"或|,|，|、|;|；|\band\b|\bor\b")
_TRAILING = re.compile(r"[\u3400-\u9fff。．.，,；;！!？?\s]+$")
_CJK = re.compile(r"[\u3400-\u9fff]")
_ABS_TOL = 1e-9
_REL_TOL = 1e-6


def _replace_superscripts(s: str) -> str:
    return re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+",
                  lambda m: "**" + "".join(_SUPERSCRIPTS[c] for c in m.group()), s)


def normalize(expr: str) -> str:
    """把一条答案规范成可解析的表达式文本。"""
    s = _replace_superscripts(str(expr or "").strip())
    s = unicodedata.normalize("NFKC", s)
    s = (s.replace("×", "*").replace("÷", "/").replace("·", "*").replace("−", "-")
         .replace("–", "-").replace("—", "-").replace("π", "pi").replace("√", "sqrt")
         .replace("^", "**").replace("（", "(").replace("）", ")"))
    s = re.sub(r"\s+", "", s)
    s = _PREFIX.sub("", s)
    if "=" in s:
        s = s.rsplit("=", 1)[1]
    # 去掉「12.56平方厘米」这类尾部单位; 纯文字结论(如「增大」)保留
    stripped = _TRAILING.sub("", s)
    # 只有剩下的部分是纯数学式时才算「尾部单位」; 「在R上是增函数」这类文字结论整句保留
    if stripped and not _CJK.search(stripped):
        s = stripped
    else:
        s = re.sub(r"[。．.，,；;！!？?]+$", "", s)
    s = re.sub(r"sqrt(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
    s = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", s)
    return s


def split_answers(expr: str) -> list[str]:
    parts = [normalize(p) for p in _SPLIT.split(str(expr or ""))]
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# 数值求值（无 sympy 时）
# ---------------------------------------------------------------------------
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_NAMES = {"pi": math.pi, "e": math.e}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "sin": math.sin, "cos": math.cos,
          "tan": math.tan, "ln": math.log, "log": math.log10}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("指数过大")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS and len(node.args) == 1 and not node.keywords):
        return float(_FUNCS[node.func.id](_eval_node(node.args[0])))
    raise ValueError("不支持的表达式")


def _numeric(s: str) -> float:
    s = re.sub(r"(\d)([a-zA-Z(])", r"\1*\2", s)
    s = s.replace(")(", ")*(")
    value = _eval_node(ast.parse(s, mode="eval"))
    if isinstance(value, complex) or math.isnan(value) or math.isinf(value):
        raise ValueError("非实数结果")
    return float(value)


def _decimals(raw: str) -> int:
    m = re.search(r"\.(\d+)", raw)
    return len(m.group(1)) if m else 0


def _close(a: float, b: float, student_raw: str) -> tuple[bool, bool]:
    """返回 (相等, 是否近似相等)。"""
    if math.isclose(a, b, rel_tol=_REL_TOL, abs_tol=_ABS_TOL):
        return True, False
    k = _decimals(student_raw)
    if k and abs(a - b) <= 10 ** (-k) + _ABS_TOL:
        return True, True
    return False, False


_MATH_WORDS = {"sqrt", "asin", "acos", "atan", "sinh", "cosh", "tanh", "log10", "exp"}


def _is_wordy(s: str) -> bool:
    """含汉字或英文单词(非函数名)的是文字结论, 不能当代数式比较, 否则「增函数」会被当成符号。"""
    if _CJK.search(s):
        return True
    return any(w.lower() not in _MATH_WORDS for w in re.findall(r"[A-Za-z]{4,}", s))


def _compare_one(student: str, reference: str) -> dict:
    """单个答案比较。返回 {comparable, equal, approx, method}。"""
    if _is_wordy(student) or _is_wordy(reference):
        if student and reference:
            return {"comparable": True, "equal": student.lower() == reference.lower(),
                    "approx": False, "method": "text"}
        return {"comparable": False, "equal": None, "approx": False, "method": "none"}
    try:
        sv, rv = _numeric(student), _numeric(reference)
        eq, approx = _close(sv, rv, student)
        return {"comparable": True, "equal": eq, "approx": approx, "method": "numeric"}
    except Exception:  # noqa: BLE001
        pass
    if _SYMPY:
        try:
            se = parse_expr(student, transformations=_TRANSFORMS)
            re_ = parse_expr(reference, transformations=_TRANSFORMS)
            diff = sympy.simplify(se - re_)
            return {"comparable": True, "equal": bool(diff == 0), "approx": False, "method": "sympy"}
        except Exception:  # noqa: BLE001
            pass
    if student and reference:
        return {"comparable": True, "equal": student.lower() == reference.lower(),
                "approx": False, "method": "text"}
    return {"comparable": False, "equal": None, "approx": False, "method": "none"}


def check_answer(student: str, reference: str) -> dict:
    """比较学生最终答案与参考答案, 返回可写进轨迹的核验结果。"""
    s_parts, r_parts = split_answers(student), split_answers(reference)
    out = {
        "student": str(student or ""),
        "reference": str(reference or ""),
        "student_norm": s_parts,
        "reference_norm": r_parts,
        "comparable": False,
        "equal": None,
        "approx": False,
        "method": "none",
        "note": "",
    }
    if not s_parts or not r_parts:
        out["note"] = "学生或参考最终答案为空, 无法程序核验"
        return out
    if len(s_parts) != len(r_parts):
        wordy = any(_is_wordy(p) for p in s_parts + r_parts)
        out.update({"comparable": True, "equal": False, "method": "text" if wordy else "count",
                    "note": f"解的个数不同({len(s_parts)} vs {len(r_parts)})"})
        return out

    remaining = list(r_parts)
    methods, approx = set(), False
    for sp in s_parts:
        matched = None
        for i, rp in enumerate(remaining):
            res = _compare_one(sp, rp)
            if not res["comparable"]:
                out["note"] = f"无法解析: {sp} / {rp}"
                return out
            if res["equal"]:
                matched = i
                methods.add(res["method"])
                approx = approx or res["approx"]
                break
        if matched is None:
            res = _compare_one(sp, remaining[0])
            out.update({"comparable": True, "equal": False, "method": res["method"],
                        "note": f"{sp} 与参考答案不一致"})
            return out
        remaining.pop(matched)
    out.update({"comparable": True, "equal": True, "approx": approx,
                "method": "+".join(sorted(methods)),
                "note": "近似相等(按学生给出的小数位)" if approx else ""})
    return out
