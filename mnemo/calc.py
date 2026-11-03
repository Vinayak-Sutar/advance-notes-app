"""Exact arithmetic for questions asked in plain words.

Small local models are unreliable at arithmetic -- gemma3:4b will happily say
23 x 17 is 381. So before a question reaches the model, any sums in it are
worked out here and handed over as facts. The model still writes the answer;
it just doesn't have to do the maths.

Only a tiny, safe subset is evaluated: numbers, + - * / // % ** and brackets,
"x" or "×" for times, "÷" for divide, "^" for power, "N% of M" and
"square root of N". Nothing is ever passed to eval().
"""
from __future__ import annotations

import ast
import math
import operator
import re

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}

# A number: "1,250" or "3.5". The thousands commas must be real groups, so a
# comma that just ends a clause ("80, and...") isn't swallowed.
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
# A run of numbers joined by operators, with at least one operator in it.
_EXPR = re.compile(
    rf"[(\s]*-?{_NUM}(?:\s*(?:\*\*|//|[-+*/x×÷^%])\s*[(\s]*-?{_NUM}[)\s]*)+", re.I)
_PERCENT_OF = re.compile(rf"({_NUM})\s*(?:%|percent)\s+of\s+({_NUM})", re.I)
_SQRT = re.compile(rf"(?:square\s+root|sqrt)\s*(?:of\s*)?\(?\s*({_NUM})\s*\)?", re.I)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("not arithmetic")


def _fmt(value: float) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"{value:,}"


def evaluate(expr: str) -> float | None:
    """The value of a plain arithmetic expression, or None if it isn't one."""
    src = (expr.replace(",", "").replace("×", "*").replace("÷", "/")
           .replace("^", "**"))
    src = re.sub(r"(?<=\d)\s*[xX]\s*(?=[\d(])", "*", src)
    try:
        value = _eval(ast.parse(src.strip(), mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return value


def facts(text: str) -> list[str]:
    """Worked results for every sum in `text`, as "expression = result" lines."""
    out: list[str] = []
    for m in _PERCENT_OF.finditer(text):
        pct, base = float(m[1].replace(",", "")), float(m[2].replace(",", ""))
        out.append(f"{m[1]}% of {m[2]} = {_fmt(pct / 100 * base)}")
    for m in _SQRT.finditer(text):
        n = float(m[1].replace(",", ""))
        out.append(f"square root of {m[1]} = {_fmt(math.sqrt(n))}")
    for m in _EXPR.finditer(text):
        raw = m.group(0).strip()
        # A bare date or phone-like run ("2026-10-25") isn't a sum.
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            continue
        value = evaluate(raw)
        if value is not None:
            out.append(f"{raw} = {_fmt(value)}")
    return out
