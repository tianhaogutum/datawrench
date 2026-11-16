"""Conservative contract type compatibility, using SQLGlot ASTs only (no SQL execution)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlglot import Dialect, exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

DIALECTS = ("duckdb", "snowflake", "bigquery", "postgres", "clickhouse", "databricks", "redshift")
MAX_TYPE_LENGTH = 8192
MAX_TYPE_DEPTH = 64


class TypeParseError(ValueError):
    pass


@dataclass(frozen=True)
class TypeNode:
    kind: str
    params: tuple[str, ...] = ()
    children: tuple[TypeNode, ...] = ()
    fields: tuple[tuple[str, TypeNode], ...] = ()
    nullable: bool = False

    def display(self) -> str:
        value = self.kind
        if self.fields:
            value += "<" + ", ".join(f"{k}: {v.display()}" for k, v in self.fields) + ">"
        elif self.children:
            value += "<" + ", ".join(c.display() for c in self.children) + ">"
        elif self.params:
            value += "(" + ",".join(self.params) + ")"
        return f"Nullable({value})" if self.nullable else value


@dataclass
class TypeDelta:
    path: str
    classification: str
    before: str | None
    after: str | None
    breaking: bool
    rule: str
    reason: str
    remediation: str
    path_segments: list[dict[str, str]]
    before_type: str | None
    after_type: str | None
    certainty: str = "known"


@dataclass
class TypeComparison:
    classification: str
    breaking: bool = False
    details: list[TypeDelta] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


ALIASES = {"VARCHAR": "STRING", "TEXT": "STRING", "NVARCHAR": "STRING",
           "FLOAT": "FLOAT", "DOUBLE": "FLOAT", "LIST": "ARRAY"}
# Deliberate policy: float/float64/double are contract aliases, not a claim about
# engine storage precision. REAL/Float32 also follow SQLGlot's FLOAT mapping.
INTEGER_BITS = {"TINYINT": (True, 8), "SMALLINT": (True, 16), "INT": (True, 32),
                "BIGINT": (True, 64), "INT128": (True, 128), "INT256": (True, 256),
                "UTINYINT": (False, 8), "USMALLINT": (False, 16), "UINT": (False, 32),
                "UBIGINT": (False, 64), "UINT128": (False, 128), "UINT256": (False, 256)}


def _node(ast: exp.DataType, depth: int = 0) -> TypeNode:
    if depth > MAX_TYPE_DEPTH:
        raise TypeParseError("type nesting exceeds 64")
    if not isinstance(ast, exp.DataType) or not isinstance(ast.this, exp.DType):
        raise TypeParseError("not a supported data type AST")
    if ast.this in {exp.DType.USERDEFINED, exp.DType.UNKNOWN}:
        raise TypeParseError("user-defined or unknown type")
    # These attributes carry semantics not represented in this first version.
    if any(v for k, v in ast.args.items() if k not in {"this", "expressions", "nested", "nullable"}):
        raise TypeParseError("unsupported type attributes (e.g. fixed array size or collation)")
    kind = ALIASES.get(ast.this.value, ast.this.value)
    nullable = bool(ast.args.get("nullable"))
    args = ast.expressions
    if kind == "LOWCARDINALITY":
        if len(args) != 1:
            raise TypeParseError("LowCardinality requires one element type")
        child = _node(args[0], depth + 1)
        return TypeNode(child.kind, child.params, child.children, child.fields, nullable or child.nullable)
    if kind == "STRUCT":
        fields = []
        for f in args:
            if not isinstance(f, exp.ColumnDef) or not isinstance(f.this, exp.Identifier):
                raise TypeParseError("only named STRUCT/Tuple fields are supported")
            if any(v for k, v in f.args.items() if k not in {"this", "kind"}):
                raise TypeParseError("unsupported STRUCT field attributes")
            # Exact names are conservative; avoid folding quoted or dialect-specific names.
            name = f.this.sql() if f.this.args.get("quoted") else f.name
            fields.append((name, _node(f.args.get("kind"), depth + 1)))
        if len({name for name, _ in fields}) != len(fields):
            raise TypeParseError("duplicate STRUCT field names")
        return TypeNode(kind, fields=tuple(fields), nullable=nullable)
    if kind in {"ARRAY", "MAP"}:
        if len(args) != (2 if kind == "MAP" else 1):
            raise TypeParseError(f"{kind} needs explicit element types")
        return TypeNode(kind, children=tuple(_node(x, depth + 1) for x in args), nullable=nullable)
    params = []
    for param in args:
        if not isinstance(param, exp.DataTypeParam) or not isinstance(param.this, exp.Literal):
            raise TypeParseError("unsupported type parameter expression")
        if any(v for k, v in param.args.items() if k != "this"):
            raise TypeParseError("unsupported type parameter modifiers (e.g. BYTE/CHAR units)")
        params.append(param.this.sql())
    if kind == "DECIMAL":
        if not 1 <= len(params) <= 2:
            # Unspecified DECIMAL is kept opaque; dialect defaults supplied by SQLGlot
            # (e.g. Snowflake) remain explicit.
            if params:
                raise TypeParseError("DECIMAL needs precision and optional scale")
        else:
            try:
                precision, scale = int(params[0]), int(params[1]) if len(params) > 1 else 0
            except ValueError as e:
                raise TypeParseError("invalid decimal precision/scale") from e
            if precision < 1 or scale < 0 or scale > precision:
                raise TypeParseError("invalid decimal precision/scale")
            params = [str(precision), str(scale)]
    if kind == "STRING" and params:
        if len(params) != 1 or not params[0].isdigit() or int(params[0]) < 1:
            raise TypeParseError("invalid string length")
    return TypeNode(kind, tuple(params), nullable=nullable)


def parse_type(value: str, dialect: str) -> TypeNode:
    if dialect not in DIALECTS:
        raise TypeParseError(f"unsupported dialect: {dialect}")
    if not isinstance(value, str) or not value.strip():
        raise TypeParseError("missing data_type")
    if len(value) > MAX_TYPE_LENGTH:
        raise TypeParseError("data_type exceeds 8192 characters")
    # Bound parser recursion before handing input to SQLGlot.
    depth = 0
    for ch in value:
        if ch in "(<[":
            depth += 1
            if depth > MAX_TYPE_DEPTH:
                raise TypeParseError("type nesting exceeds 64")
        elif ch in ")>]":
            depth -= 1
    try:
        d = Dialect.get_or_raise(dialect)
        tokens = d.tokenize(value)
        # Extend the declared cross-dialect FLOAT64 alias to nested types too.
        # Quoted identifiers and string literals keep their original tokens.
        for token in tokens:
            if token.token_type == TokenType.VAR and token.text.upper() == "FLOAT64":
                token.token_type = TokenType.DOUBLE
        if any(t.comments or t.text == ";" for t in tokens):
            raise TypeParseError("comments and SQL statements are not type strings")
        parser = d.parser()
        # DuckDB intentionally discards VARCHAR(n) limits during transpilation.
        # Contract policy preserves declared limits, including inside STRUCTs.
        parser.TYPE_CONVERTERS = {k: v for k, v in parser.TYPE_CONVERTERS.items() if k != exp.DType.TEXT}
        asts = parser.parse_into(exp.DataType, tokens, value)
        if len(asts) != 1 or not isinstance(asts[0], exp.DataType):
            raise TypeParseError("expected exactly one type")
        return _node(asts[0])
    except (ParseError, TokenError, RecursionError, ValueError) as e:
        if isinstance(e, TypeParseError):
            raise
        # Don't echo arbitrary SQL or parser excerpts in errors.
        raise TypeParseError("SQLGlot could not parse a complete supported type") from e


def _scalar(a: TypeNode, b: TypeNode) -> str:
    if a.kind in INTEGER_BITS and b.kind in INTEGER_BITS and not a.params and not b.params:
        def bounds(k: str) -> tuple[int, int]:
            signed, bits = INTEGER_BITS[k]
            return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        low_a, high_a = bounds(a.kind)
        low_b, high_b = bounds(b.kind)
        if low_b <= low_a and high_b >= high_a:
            return "widening"
        if low_a <= low_b and high_a >= high_b:
            return "narrowing"
        return "incompatible"
    if a.kind == b.kind == "DECIMAL" and a.params and b.params:
        pa, sa = map(int, a.params)
        pb, sb = map(int, b.params)
        if pb - sb >= pa - sa and sb >= sa:
            return "widening"
        if pa - sa >= pb - sb and sa >= sb:
            return "narrowing"
    if a.kind == b.kind == "STRING":
        la = int(a.params[0]) if a.params else float("inf")
        lb = int(b.params[0]) if b.params else float("inf")
        return "widening" if lb > la else "narrowing"
    return "incompatible"


def _scalar_explanation(a: TypeNode, b: TypeNode, classification: str) -> tuple[str, str, str]:
    if a.kind == b.kind == "DECIMAL" and a.params and b.params:
        pa, sa = map(int, a.params)
        pb, sb = map(int, b.params)
        return ("decimal_capacity",
                f"Decimal integer digits {pa - sa} -> {pb - sb}; fractional digits {sa} -> {sb}. "
                f"Both capacities must be retained or increased for widening; this change is {classification}.",
                "Retain the original integer and fractional capacity, or publish a new model version "
                "after validating value ranges and rounding requirements.")
    if a.kind in INTEGER_BITS and b.kind in INTEGER_BITS and not a.params and not b.params:
        return ("integer_range", f"The declared integer range change is {classification}; widening requires "
                "the new range to contain the entire old range.",
                "Retain a type that covers the original integer range, or publish a new model version "
                "after validating value ranges.")
    if a.kind == b.kind == "STRING":
        return ("string_capacity", f"The declared string length change is {classification}; an unbounded "
                "string has no declared length limit.",
                "Retain the original string capacity, or publish a new model version after checking lengths.")
    return ("scalar_incompatible", "No supported compatibility rule proves this type or parameter change safe.",
            "Retain the original type, or publish a new model version and migrate consumers after "
            "validating the conversion in the target warehouse.")


def _delta(path: str, classification: str, a: TypeNode | None, b: TypeNode | None, breaking: bool,
           rule: str, reason: str, remediation: str, segments: list[dict[str, str]]) -> TypeDelta:
    before, after = a.display() if a else None, b.display() if b else None
    return TypeDelta(path, classification, before, after, breaking, rule, reason, remediation,
                     segments, before, after)


def _diff(a: TypeNode, b: TypeNode, path: str, strict: bool, nested: bool = False,
          segments: list[dict[str, str]] | None = None) -> list[TypeDelta]:
    segments = [] if segments is None else segments
    if a == b:
        return []
    result = []
    if a.nullable != b.nullable:
        cls = "widening" if b.nullable else "narrowing"
        adjusted = TypeNode(a.kind, a.params, a.children, a.fields, b.nullable)
        rule = "nullability_added" if b.nullable else "nullability_removed"
        reason = ("Adding Nullable allows nulls and is widening." if b.nullable else
                  "Removing Nullable excludes previously permitted null values and is narrowing.")
        if b.nullable and (nested or strict):
            rule = "nested_type_invariant" if nested else "strict_widening"
            reason += (" Nested nullability changes are breaking under the conservative contract policy." if nested
                       else " Strict widening mode treats widening as breaking.")
        delta = _delta(path + ".nullable", cls, a, adjusted, nested or strict or not b.nullable, rule, reason,
                       "Retain the original nullability, or publish a new model version and migrate null handling.",
                       segments + [{"kind": "nullability"}])
        # Preserve the v1 boolean values; explicit type fields describe this atomic step.
        delta.before, delta.after = str(a.nullable), str(b.nullable)
        result.append(delta)
        a = adjusted
    if a == b:
        return result
    if a.kind == b.kind == "STRUCT":
        left, right = dict(a.fields), dict(b.fields)
        for name in sorted(left.keys() | right.keys()):
            p = f"{path}.{name}"
            child_segments = segments + [{"kind": "field", "name": name}]
            if name not in left:
                result.append(_delta(p, "field_added", None, right[name], False, "struct_field_added",
                                     "Adding a named STRUCT field retains existing fields and is allowed.",
                                     "No compatibility action required; verify consumers accept the added field.", child_segments))
            elif name not in right:
                result.append(_delta(p, "field_removed", left[name], None, True, "struct_field_removed",
                                     "Removing a named STRUCT field removes a consumer-visible contract member.",
                                     "Restore the field, or publish a new model version and migrate field references.", child_segments))
            else:
                result.extend(_diff(left[name], right[name], p, strict, nested=True, segments=child_segments))
        # Ordering is observable for positional consumers; additions alone are allowed.
        if [k for k in left if k in right] != [k for k in right if k in left]:
            result.append(_delta(path, "field_reordered", a, b, True, "struct_field_order",
                                 "Reordering surviving STRUCT fields can break positional consumers.",
                                 "Preserve the existing field order, or publish a new model version and migrate positional access.", segments))
    elif a.kind == b.kind and a.kind in {"ARRAY", "MAP"}:
        labels = ("element",) if a.kind == "ARRAY" else ("key", "value")
        for label, x, y in zip(labels, a.children, b.children):
            kind = "array_element" if a.kind == "ARRAY" else "map_" + label
            result.extend(_diff(x, y, f"{path}.{label}", strict, nested=True,
                                segments=segments + [{"kind": kind}]))
    else:
        cls = _scalar(a, b)
        rule, reason, remediation = _scalar_explanation(a, b, cls)
        if cls == "widening" and (nested or strict):
            rule = "nested_type_invariant" if nested else "strict_widening"
            reason += (" Nested retyping is breaking even when the scalar change is widening." if nested
                       else " Strict widening mode treats widening as breaking.")
            remediation = ("Retain the original type, or publish a new model version and migrate consumers." if nested
                           else "Retain the original type, or review the widening and omit --strict-widening if policy allows it.")
        elif cls == "widening":
            remediation = "No compatibility action required under the default widening policy; verify warehouse behavior."
        result.append(_delta(path, cls, a, b, nested or strict or cls != "widening",
                             rule, reason, remediation, segments))
    return result


def _normalized_raw(value: str | None) -> str:
    # Conservative: trim outer whitespace only. Case and quoted literal whitespace
    # can be significant for opaque/user-defined types.
    return value.strip() if isinstance(value, str) else ""


def compare_types(before: str | None, after: str | None, dialect: str = "duckdb", *, strict_widening: bool = False) -> TypeComparison:
    parsed = []
    warnings = []
    for label, value in (("base", before), ("head", after)):
        try:
            parsed.append(parse_type(value, dialect))
        except TypeParseError as e:
            parsed.append(None)
            warnings.append(f"{label} data_type unparseable: {e}; using conservative string comparison")
    if warnings:
        equal = _normalized_raw(before) == _normalized_raw(after) and bool(_normalized_raw(before))
        return TypeComparison("unparseable", not equal,
                              [TypeDelta("type", "unparseable", before, after, not equal, "unparseable_type",
                                         "At least one type is unparseable; cannot determine compatibility. "
                                         + ("Equal raw strings do not prove safety." if equal else
                                            "Different or missing raw types are conservatively breaking."),
                                         "Check the dialect and use supported type syntax; review opaque types manually. "
                                         "Use --fail-on any to reject uncertain comparisons in CI.",
                                         [], before, after, "unknown")], warnings)
    details = _diff(parsed[0], parsed[1], "type", strict_widening)
    cls = "identical" if not details else details[0].classification
    if details and (len(details) > 1 or details[0].path != "type"):
        cls = "nested_change"
    return TypeComparison(cls, any(x.breaking for x in details), details)
