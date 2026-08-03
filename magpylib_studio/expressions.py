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

import numpy as np

PREFIX = "="

# Everything an expression may contain. Attribute access, subscripts,
# comprehensions and lambdas are absent on purpose: no way to reach an
# object's internals, and no way to build a call target that is not one of
# the names below.
_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Call,
    ast.Tuple,
    ast.List,
)
_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": math.sqrt,
    "hypot": math.hypot,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "radians": math.radians,
    "degrees": math.degrees,
    "log": math.log,
    "exp": math.exp,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def reference():
    """What an expression may contain, read off the allow-list that enforces
    it — so the help a UI shows cannot drift from what actually evaluates."""
    return {
        "operators": ["+", "-", "*", "/", "//", "%", "**", "( )"],
        "functions": sorted(_FUNCTIONS),
        "constants": sorted(_CONSTANTS),
        "examples": [
            "2.5",
            "gap * 2",
            "360 / n",
            "sqrt(2) * radius",
            "max(gap, 1) + 0.5",
        ],
        "note": "Names are the scene's other variables; one that does not "
        "exist yet is offered for you to create. No attributes, "
        "indexing, comparisons or calls to anything else.",
    }


def validate(source):
    """None if `source` is a usable expression, else why it is not.

    Names are *not* checked here: an expression naming a variable that does
    not exist yet is well formed, and the UI offers to create it.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        return f"not an expression: {e.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            return f"{type(node).__name__} is not allowed in an expression"
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if name not in _FUNCTIONS:
                return (
                    f"{name or 'that'!r} is not one of the functions an "
                    f"expression may call"
                )
            if node.keywords:
                return "expression calls take no keyword arguments"
        if (
            isinstance(node, ast.Constant) and not isinstance(node.value, int | float)
        ) or isinstance(getattr(node, "value", None), bool):
            return f"{getattr(node, 'value', '')!r} is not a number"
    return None


def is_expression(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def source_of(value):
    """The Python source inside an expression value ("=a*2" -> "a*2")."""
    return value[len(PREFIX) :]


def evaluate(source, lookup, functions=None):
    """Evaluate one expression's source, resolving names through `lookup`.

    `functions` swaps the table the calls go through, which is how a sampled
    run is computed for every point at once: bind the sample to an array,
    hand in the numpy spellings, and the whole column falls out of one pass.
    What may be *called* is checked against the scalar table either way, so a
    wider table cannot widen what an expression is allowed to say.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"cannot parse expression {source!r}: {e.msg}") from e

    def walk(node):
        if not isinstance(node, _NODES):
            raise ValueError(f"{type(node).__name__} is not allowed in an expression")
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
            table = functions or _FUNCTIONS
            return table[node.func.id](*[eval_node(a) for a in node.args])
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


def referenced_names(value):
    """Variable names the expressions inside a value refer to, in the order
    they are read. Function names and the built-in constants are not
    variables, so they are left out — what remains is what a document has to
    define, or a UI has to ask for.

    Nor is a sampled run's `t`, inside the template that samples over it: it
    is bound by the node, and a UI that asked the user to define it would be
    asking for the one thing the node exists to provide.
    """
    names: list[str] = []

    def visit(item, bound=()):
        if is_sampled(item):
            spec = item[SAMPLED]
            visit(spec.get("count"))
            visit(spec.get("over"))
            visit(spec.get("of"), bound=(*bound, SAMPLE))
            return
        if is_expression(item):
            try:
                tree = ast.parse(source_of(item), mode="eval")
            except SyntaxError:
                return  # the build will report it
            called = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Name)
                    and node.id not in called
                    and node.id not in _CONSTANTS
                    and node.id not in bound
                    and node.id not in names
                ):
                    names.append(node.id)
        elif isinstance(item, list):
            for entry in item:
                visit(entry, bound)
        elif isinstance(item, dict):
            for entry in item.values():
                visit(entry, bound)

    visit(value)
    return names


#: Names an expression may use that Python already has. The rest of the
#: allow-list comes from `math`, and a script that uses one has to import it.
_BUILTIN_FUNCTIONS = {"abs", "min", "max", "round"}


def math_names(value):
    """The `math` names the expressions inside a value use, sorted.

    An expression is written into the exported script verbatim, which is what
    keeps the script parametric — but `sqrt(2) * radius` only stays true to
    the document if `sqrt` is in scope when it runs. Nothing was importing it,
    so every scene using a function or a constant exported a script that
    raised NameError on its first line of geometry, including the one the
    expression help offers as its own example.

    Builtins are left out: a script gets `abs` and `max` for free, and
    importing them from `math` would be wrong for `abs` and a lie for `max`.
    """
    used: set[str] = set()

    def visit(item):
        if is_expression(item):
            try:
                tree = ast.parse(source_of(item), mode="eval")
            except SyntaxError:
                return  # the build will report it
            used.update(
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name)
                and node.id in (_FUNCTIONS.keys() | _CONSTANTS.keys())
            )
        elif isinstance(item, list):
            for entry in item:
                visit(entry)
        elif isinstance(item, dict):
            for entry in item.values():
                visit(entry)

    visit(value)
    return sorted(used - _BUILTIN_FUNCTIONS)


def math_names_in_source(source):
    """The `math` names a finished script uses, sorted.

    Read off the script rather than off the document it came from, because
    the two no longer agree: a sampled template's `cos` is written as
    `np.cos`, and importing the one from `math` would be an import nothing
    calls. An attribute is not a name, so `np.cos` contributes nothing here
    and a bare `cos` contributes itself.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # not our script to fix; the caller will notice sooner
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in (_FUNCTIONS.keys() | _CONSTANTS)
    }
    return sorted(used - _BUILTIN_FUNCTIONS)


def contains_expression(value):
    """True if a document value has an expression anywhere inside it."""
    if is_expression(value):
        return True
    if isinstance(value, list):
        return any(contains_expression(v) for v in value)
    if isinstance(value, dict):
        return any(contains_expression(v) for v in value.values())
    return False


def normalized(value):
    """Rewrite expressions in their canonical spacing, so that what a document
    stores is what reading its script back would produce — the script tab is a
    fixed point from the first save, not the second."""
    if is_expression(value):
        try:
            return PREFIX + ast.unparse(ast.parse(source_of(value), mode="eval"))
        except SyntaxError:
            return value  # let the build report it
    if is_sampled(value):
        spec = {k: normalized(v) for k, v in value[SAMPLED].items() if k != "name"}
        # A default written out is a default the script does not write, and
        # reading it back would drop it — which is the fixed point above
        # failing on a document that only said what it meant.
        if spec.get("over") == [0, 1]:
            del spec["over"]
        return {SAMPLED: spec}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    return value


class _Rename(ast.NodeTransformer):
    """One name for another, wherever it is used *as a variable*."""

    def __init__(self, old, new):
        self.old, self.new = old, new
        self.hit = False

    def visit_Call(self, node):
        # A call's target comes from the allow-list, not from the document:
        # `sqrt` in `sqrt(radius)` is the one Name here that is not a variable.
        node.args = [self.visit(arg) for arg in node.args]
        return node

    def visit_Name(self, node):
        if node.id == self.old:
            node.id = self.new
            self.hit = True
        return node


def renamed(value, old, new):
    """Every expression inside a document value, with `old` spelled `new`.

    Rewritten through the AST rather than through the text, because a variable
    is a name and not a substring: `n` occurs inside `turns`, inside `min(`
    and inside the literal `"n"`, and none of those three is the variable.

    A template's `t` belongs to the sampled node that binds it, so it is left
    alone in there — and it is not available as a destination either. Renaming
    a variable to `t` would quietly hand every use of it inside a template to
    the sample, and a rename has to leave the scene drawing what it drew, so
    that one is refused instead.
    """

    def rewrite(item, bound=()):
        if is_sampled(item):
            spec = item[SAMPLED]
            return {
                SAMPLED: {
                    key: rewrite(entry, (*bound, SAMPLE) if key == "of" else bound)
                    for key, entry in spec.items()
                }
            }
        if is_expression(item):
            if old in bound:
                return item  # the node's own sample, not the scene's variable
            try:
                tree = ast.parse(source_of(item), mode="eval")
            except SyntaxError:
                return item  # the build will report it
            rename = _Rename(old, new)
            tree = rename.visit(tree)
            if not rename.hit:
                return item
            if new in bound:
                raise ValueError(
                    f"a sampled run calls its own points {new!r}, so {old!r} "
                    f"would stop meaning the variable inside a template"
                )
            return PREFIX + ast.unparse(tree)
        if isinstance(item, list):
            return [rewrite(entry, bound) for entry in item]
        if isinstance(item, dict):
            return {key: rewrite(entry, bound) for key, entry in item.items()}
        return item

    return rewrite(value)


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


#: A run of points stated as the expression that draws them, rather than as
#: the points themselves:
#:
#:     {"sampled": {"count": "=per_turn * turns + 1", "over": [0, 1],
#:                  "of": ["=radius * cos(tau * turns * t)", …]}}
#:
#: The alternative is what this replaced — sixty rows in the document, each
#: the same expression with a different literal in it. That form cannot say
#: how many points it wants, only how many it has, so the count was the one
#: quantity in a parametric scene that no variable could reach; and it
#: exported as sixty rows written out, which is not how anyone writes it.
SAMPLED = "sampled"

#: Functions with no elementwise meaning over a whole sample, so no honest
#: place in a template. `min(a, b)` of two arrays is not the smaller of each
#: pair, and writing it as though it were would export a different scene.
_UNVECTORISABLE = {"min", "max"}

#: The same functions over a whole sample at once. These are the spellings
#: `to_script` emits, so a scene computed through this table and the script
#: it exports are doing the identical arithmetic rather than merely agreeing
#: to the precision they happen to agree to.
_ARRAY_FUNCTIONS = {
    "abs": np.abs,
    "round": np.round,
    "sqrt": np.sqrt,
    "hypot": np.hypot,
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": np.arcsin,
    "acos": np.arccos,
    "atan": np.arctan,
    "atan2": np.arctan2,
    "log": np.log,
    "exp": np.exp,
    "radians": np.radians,
    "degrees": np.degrees,
}


def called_names(values):
    """Every function an expression inside these values calls."""
    called = set()

    def visit(item):
        if is_expression(item):
            try:
                tree = ast.parse(source_of(item), mode="eval")
            except SyntaxError:
                return
            called.update(
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            )
        elif isinstance(item, list):
            for entry in item:
                visit(entry)

    visit(values)
    return called


def is_sampled(value):
    """True if a document value is a run of points stated as an expression."""
    return isinstance(value, dict) and SAMPLED in value


#: What a template calls its sample. Not a field of the node: the name is an
#: internal binding, and letting a document choose it bought nothing and cost
#: a round trip — the script has to rename it wherever the scene already uses
#: that name, and no script can say what it was called before.
SAMPLE = "t"


def expand(value, lookup):
    """A sampled node -> the points it draws.

    `count` is an expression like any other, which is the whole point of the
    node: the number of points is a thing a slider can hold. `over` is the
    span `t` runs across, and defaults to the unit interval because a fraction
    along is what most templates are written in terms of.

    Every column is computed in one pass with `t` bound to the whole sample,
    which is both how the exported script computes it — so the two agree by
    construction rather than by luck — and the difference between a slider
    that drags and one that stutters: sixteen thousand points went from half
    a second of interpreting one expression at a time to a handful of numpy
    calls.
    """
    spec = value[SAMPLED]
    if not isinstance(spec, dict) or "of" not in spec:
        raise ValueError("a sampled value needs 'of': the expression to sample")
    count = resolve(spec.get("count", 2), lookup)
    if not isinstance(count, int | float) or count != int(count) or count < 2:
        raise ValueError(
            f"a sampled value needs a whole count of 2 or more, not {count!r}"
        )
    start, stop = resolve(spec.get("over", [0, 1]), lookup)
    template = spec["of"]
    terms = template if isinstance(template, list) else [template]
    # Refused here rather than discovered at export. `min(a, b)` over a whole
    # sample is not elementwise and has no honest vectorisation, so a template
    # that calls one is a scene that could be built and never written down.
    unvectorisable = _UNVECTORISABLE & set(called_names(terms))
    if unvectorisable:
        raise ValueError(
            f"{', '.join(sorted(unvectorisable))} cannot be sampled over a run "
            "of points; use arithmetic that applies to each point on its own"
        )
    sample = np.linspace(start, stop, int(count))

    def bound(wanted):
        # The sample shadows the scene's variables rather than joining them: a
        # template is written against `t`, and a scene that happens to define
        # one should not quietly change what the template draws.
        return sample if wanted == SAMPLE else lookup(wanted)

    def column(term):
        if not is_expression(term):
            return np.full_like(sample, term)  # a column that does not vary
        drawn = np.asarray(
            evaluate(source_of(term), bound, _ARRAY_FUNCTIONS), dtype=float
        )
        # An expression naming no `t` comes back as one number for a column
        # that has to be as long as every other.
        return np.broadcast_to(drawn, sample.shape) if drawn.ndim == 0 else drawn

    # .tolist(), so what leaves here is plain floats: this goes into a document
    # that has to survive json.dumps, and a np.float64 does not.
    if not isinstance(template, list):
        return column(template).tolist()
    return np.column_stack([column(term) for term in terms]).tolist()


def resolve(value, lookup):
    """Substitute every expression inside a document value (lists and dicts
    included), leaving literals — axis names, op kinds — untouched."""
    if is_expression(value):
        return evaluate(source_of(value), lookup)
    if is_sampled(value):
        return expand(value, lookup)
    if isinstance(value, list):
        return [resolve(v, lookup) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, lookup) for k, v in value.items()}
    return value
