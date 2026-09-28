"""
canviz/compute.py
-----------------
Scripted per-send frame values.

A frame can carry a "compute" pipeline: an optional rolling counter plus an
ordered list of steps, each a *sandboxed expression* whose result is written
into a target byte / bit field (and/or stored in per-frame state). The pipeline
runs on every transmission, so checksums, counters and derived signals stay
fresh — this generalises the hard-coded E2E transform.

Security
--------
Expressions run in the backend and can arrive via imported frame sets, so they
are NOT arbitrary Python. Each expression is parsed to an AST and validated
against a strict node whitelist (no attribute access, no imports, no lambdas,
no comprehensions, no assignment, no dunder names, only calls to curated
helpers). It is then evaluated with empty builtins and a fixed namespace. All
looping lives in the bounded helpers; user expressions cannot loop. See
validate_expr() for the exact rules.

Inputs available to an expression: `data` (current bytes), `counter`, `dlc`,
`can_id`, `mem(name, default=0)`, any named byte `tables`, and the helper
functions below. No live/received bus data (by design, for now).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from canviz.e2e import crc8  # reuse the audited CRC-8 (defaults = CRC-8H2F)

MAX_EXPR_LEN = 500
MAX_STEPS = 16
MAX_CONST = 2 ** 32          # reject absurd integer literals
MAX_SHIFT = 64               # reject literal shift amounts above this


# ── Curated helper library (all pure, all bounded) ───────────────────────────

def _as_bytes(data: Any) -> list[int]:
    if isinstance(data, int):
        return [data & 0xFF]
    return [int(b) & 0xFF for b in data]


def crc16(data: Any, poly: int, init: int, xorout: int, reflect: bool = False) -> int:
    """Generic CRC-16 (MSB-first, or LSB-first when reflect=True)."""
    def _rev(x: int, bits: int) -> int:
        r = 0
        for _ in range(bits):
            r = (r << 1) | (x & 1)
            x >>= 1
        return r
    c = init & 0xFFFF
    for b in _as_bytes(data):
        b = _rev(b, 8) if reflect else b
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    if reflect:
        c = _rev(c, 16)
    return c ^ (xorout & 0xFFFF)


def sum8(data: Any) -> int:
    return sum(_as_bytes(data)) & 0xFF


def sum16(data: Any) -> int:
    return sum(_as_bytes(data)) & 0xFFFF


def xor8(data: Any) -> int:
    r = 0
    for b in _as_bytes(data):
        r ^= b
    return r & 0xFF


def twos_complement(x: int, bits: int = 8) -> int:
    return (-int(x)) & ((1 << bits) - 1)


def _lo(x: int) -> int:
    return int(x) & 0x0F


def _hi(x: int) -> int:
    return (int(x) >> 4) & 0x0F


def _byte(x: int, n: int) -> int:
    return (int(x) >> (8 * int(n))) & 0xFF


def _bit(x: int, n: int) -> int:
    return (int(x) >> int(n)) & 1


HELPERS = {
    "crc8": crc8, "crc16": crc16,
    "sum8": sum8, "sum16": sum16, "xor8": xor8,
    "twos_complement": twos_complement,
    "lo": _lo, "hi": _hi, "byte": _byte, "bit": _bit,
    "min": min, "max": max, "abs": abs,
}
ALLOWED_CALLS = set(HELPERS) | {"mem"}   # mem() is bound per-evaluation


# ── Sandbox: validate an expression's AST ─────────────────────────────────────

class ExprError(ValueError):
    """Raised when an expression is unsafe or malformed."""


_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Subscript, ast.Slice, ast.List, ast.Tuple,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def validate_expr(expr: str, names: set[str]) -> ast.Expression:
    """
    Parse and validate a single expression. Returns the AST on success,
    raises ExprError otherwise. `names` is the set of allowed variable names
    (e.g. data, counter, dlc, can_id, table names).
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ExprError("expression is empty")
    if len(expr) > MAX_EXPR_LEN:
        raise ExprError(f"expression too long (max {MAX_EXPR_LEN} chars)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error: {exc.msg}") from exc

    allowed_names = names | ALLOWED_CALLS

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"{type(node).__name__} is not allowed")
        # No power operator (cheap way to blow up CPU/memory)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise ExprError("** (power) is not allowed")
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise ExprError(f"name '{node.id}' is not allowed")
            if node.id not in allowed_names:
                raise ExprError(f"unknown name '{node.id}'")
        if isinstance(node, ast.Call):
            # Only direct calls to whitelisted helper names (no attribute calls)
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_CALLS:
                raise ExprError("only calls to built-in helper functions are allowed")
            if node.keywords and any(kw.arg is None for kw in node.keywords):
                raise ExprError("**kwargs is not allowed")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                continue
            if isinstance(node.value, int) and abs(node.value) > MAX_CONST:
                raise ExprError("integer literal too large")
            if not isinstance(node.value, (int, float, str)):
                raise ExprError(f"{type(node.value).__name__} literals are not allowed")
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.LShift, ast.RShift)):
            rhs = node.right
            if isinstance(rhs, ast.Constant) and isinstance(rhs.value, int) and rhs.value > MAX_SHIFT:
                raise ExprError(f"shift amount too large (max {MAX_SHIFT})")
    return tree


# ── Config dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Counter:
    byte: int
    bit_offset: int = 0
    width: int = 8
    step: int = 1

    @property
    def modulo(self) -> int:
        return 1 << self.width


@dataclass(frozen=True)
class Target:
    byte: int
    bit_offset: int = 0
    width: int = 8         # default: whole byte

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.bit_offset


@dataclass(frozen=True)
class Step:
    expr: str
    target: Target | None = None
    store: str | None = None


@dataclass(frozen=True)
class ComputeConfig:
    steps: list[Step]
    counter: Counter | None = None
    tables: dict[str, list[int]] = field(default_factory=dict)
    version: int = 1

    def name_set(self) -> set[str]:
        return {"data", "counter", "dlc", "can_id"} | set(self.tables)

    def validate(self, dlc: int) -> list[ast.Expression]:
        """Validate the whole pipeline for a dlc-byte frame. Returns compiled ASTs."""
        if not 0 < dlc <= 8:
            raise ExprError("dlc must be 1..8")
        if not self.steps:
            raise ExprError("compute has no steps")
        if len(self.steps) > MAX_STEPS:
            raise ExprError(f"too many steps (max {MAX_STEPS})")
        for name, tbl in self.tables.items():
            if any(not 0 <= int(b) <= 0xFF for b in tbl):
                raise ExprError(f"table '{name}' must be bytes (0-255)")
        if self.counter is not None:
            c = self.counter
            if not 0 <= c.byte < dlc:
                raise ExprError("counter byte outside frame")
            if c.bit_offset < 0 or not 1 <= c.width <= 8 or c.bit_offset + c.width > 8:
                raise ExprError("counter bits must fit in one byte")
        names = self.name_set()
        trees = []
        for i, st in enumerate(self.steps):
            if st.target is None and not st.store:
                raise ExprError(f"step {i + 1}: needs a target or a store name")
            if st.target is not None:
                t = st.target
                if not 0 <= t.byte < dlc:
                    raise ExprError(f"step {i + 1}: target byte outside frame")
                if t.bit_offset < 0 or not 1 <= t.width <= 8 or t.bit_offset + t.width > 8:
                    raise ExprError(f"step {i + 1}: target bits must fit in one byte")
            trees.append(validate_expr(st.expr, names))
        return trees


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(cfg: ComputeConfig, template: bytes, counter: int,
             state: dict, can_id: int, trees: list[ast.Expression] | None = None) -> bytes:
    """
    Run the pipeline once and return the resulting frame bytes.

    `state` is a per-frame dict, read via mem() and written via a step's
    `store`. `trees` are pre-validated ASTs (validate() output); when None the
    config is validated first.
    """
    dlc = len(template)
    if trees is None:
        trees = cfg.validate(dlc)

    d = bytearray(template)

    # Declarative rolling counter, written before the steps run
    if cfg.counter is not None:
        c = cfg.counter
        val = counter % c.modulo
        mask = ((1 << c.width) - 1) << c.bit_offset
        d[c.byte] = (d[c.byte] & ~mask) | ((val << c.bit_offset) & mask)

    def _mem(name: str, default: int = 0):
        return state.get(name, default)

    for st, tree in zip(cfg.steps, trees):
        ns = {
            "data": list(d), "counter": counter, "dlc": dlc, "can_id": can_id,
            "mem": _mem, **HELPERS, **{k: list(v) for k, v in cfg.tables.items()},
        }
        try:
            code = compile(tree, "<expr>", "eval")
            # AST-whitelisted above; builtins removed. See validate_expr().
            result = eval(code, {"__builtins__": {}}, ns)
        except ExprError:
            raise
        except Exception as exc:  # arithmetic / index errors from user input
            raise ExprError(f"error evaluating '{st.expr}': {exc}") from exc

        if st.store:
            state[st.store] = result
        if st.target is not None:
            if isinstance(result, (list, tuple)):
                raise ExprError(f"step writing byte {st.target.byte} must return a number")
            t = st.target
            d[t.byte] = (d[t.byte] & ~t.mask) | ((int(result) << t.bit_offset) & t.mask)
    return bytes(d)
