"""Spreadsheet-style expressions, so a scene can be parameterized.

Any string beginning with `=` is an expression over the document's
variables; anything else is a literal. That one rule keeps `"z"` an axis
name and `"=turn/n"` arithmetic without a per-field whitelist — a value
can be an expression anywhere a number can, in constructor params and in
event fields alike.

Expressions are evaluated from their AST against an explicit allow-list,
never with eval() on arbitrary source: a document is something you open
from someone else, so it must not be able to run code. (The script tab is
the opposite trade on purpose — it executes, as its docstring says.)
"""

from __future__ import annotations

import ast
import math

PREFIX = "="

# Everything an expression may contain. Attribute access, subscripts,
# comprehensions and lambdas are absent on purpose: no way to reach an
# object's internals, and no way to build a call target that is not one of
# the names below.
_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Call, ast.Tuple, ast.List,
)
_FUNCTIONS = {
    "abs": abs, "min": min, "max": max, "round": round,
    "sqrt": math.sqrt, "hypot": math.hypot,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "radians": math.radians, "degrees": math.degrees,
    "log": math.log, "exp": math.exp,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def is_expression(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def source_of(value):
    """The Python source inside an expression value ("=a*2" -> "a*2")."""
    return value[len(PREFIX):]


def evaluate(source, lookup):
    """Evaluate one expression's source, resolving names through `lookup`."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"cannot parse expression {source!r}: {e.msg}") from e

    def walk(node):
        if not isinstance(node, _NODES):
            raise ValueError(
                f"{type(node).__name__} is not allowed in an expression"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                name = getattr(node.func, "id", "that")
                raise ValueError(f"{name!r} is not a function an expression may call")
            if node.keywords:
                raise ValueError("expression calls take no keyword arguments")
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)

    def eval_node(node):
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, int | float) or isinstance(node.value, bool):
                raise ValueError(f"{node.value!r} is not a number")
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            return lookup(node.id)
        if isinstance(node, ast.UnaryOp):
            value = eval_node(node.operand)
            return -value if isinstance(node.op, ast.USub) else +value
        if isinstance(node, ast.BinOp):
            left, right = eval_node(node.left), eval_node(node.right)
            try:
                return _BINOPS[type(node.op)](left, right)
            except ZeroDivisionError as e:
                raise ValueError(f"division by zero in {source!r}") from e
        if isinstance(node, ast.Call):
            return _FUNCTIONS[node.func.id](*[eval_node(a) for a in node.args])
        # Tuple / List
        return [eval_node(e) for e in node.elts]

    return eval_node(tree)


_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}


def normalized(value):
    """Rewrite expressions in their canonical spacing, so that what a document
    stores is what reading its script back would produce — the script tab is a
    fixed point from the first save, not the second."""
    if is_expression(value):
        try:
            return PREFIX + ast.unparse(ast.parse(source_of(value), mode="eval"))
        except SyntaxError:
            return value  # let the build report it
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    return value


def resolve_variables(variables):
    """{name: number | "=expr"} -> {name: number}, expressions last.

    Variables may be written in terms of each other; a cycle is reported
    rather than recursed into.
    """
    resolved, resolving = {}, []

    def lookup(name):
        if name in resolved:
            return resolved[name]
        if name in resolving:
            cycle = " -> ".join([*resolving, name])
            raise ValueError(f"variable {name!r} defines itself: {cycle}")
        if name not in variables:
            raise ValueError(f"unknown variable {name!r}")
        resolving.append(name)
        try:
            value = variables[name]
            resolved[name] = (
                evaluate(source_of(value), lookup) if is_expression(value) else value
            )
        finally:
            resolving.pop()
        return resolved[name]

    for name in variables:
        lookup(name)
    return resolved


def resolve(value, lookup):
    """Substitute every expression inside a document value (lists and dicts
    included), leaving literals — axis names, op kinds — untouched."""
    if is_expression(value):
        return evaluate(source_of(value), lookup)
    if isinstance(value, list):
        return [resolve(v, lookup) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, lookup) for k, v in value.items()}
    return value
