"""Compile declarations to SQL; all parsing and serialization belongs to SQLGlot.

Checks concern logical, written join order, before WHERE/GROUP BY/DISTINCT/LIMIT.
The database optimizer remains free to execute the SQL in another physical order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.eliminate_ctes import eliminate_ctes
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers


class ContractError(ValueError):
    """Invalid declaration or unsupported SQL, with a useful user-facing message."""


@dataclass(frozen=True)
class Check:
    id: str
    kind: str
    scope: str
    join: str | None
    description: str
    sql: str
    key_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class JoinPlan:
    scope: str
    position: int
    join_type: str
    left_aliases: tuple[str, ...]
    right_alias: str
    right_source: str
    predicate: str
    left_keys: tuple[str, ...]
    right_keys: tuple[str, ...]
    grain_before: tuple[str, ...]
    grain_after: tuple[str, ...]
    cardinality: str
    preserve: str


@dataclass(frozen=True)
class Bundle:
    version: int
    dialect: str
    sqlglot_version: str
    source_sha256: str
    contract_sha256: str
    checks: tuple[Check, ...]
    joins: tuple[JoinPlan, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_DIALECTS = {"duckdb", "sqlite", "postgres", "snowflake", "bigquery"}
_CARDINALITIES = {"1:1", "1:m", "m:1", "m:m"}


def _object(value: Any, where: str, *, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{where} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractError(f"{where} field names must be strings")
    extra = value.keys() - allowed
    if extra:
        raise ContractError(f"{where}: unknown fields {sorted(extra)}")
    return value


def _strings(value: Any, where: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(x, str) or not x.strip() for x in value)
    ):
        raise ContractError(f"{where} must be a nonempty array of column names")
    if len(set(value)) != len(value):
        raise ContractError(f"{where} contains duplicate columns")
    return value


def _parse(sql: str, dialect: str, where: str) -> exp.Expression:
    try:
        statements = [x for x in sqlglot.parse(sql, read=dialect) if x is not None]
        if len(statements) != 1:
            raise ContractError(f"{where}: exactly one SQL statement is required")
        return normalize_identifiers(statements[0], dialect=dialect)
    except (SqlglotError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"{where}: {exc}") from exc


def _column(text: str, dialect: str, where: str, *, qualified: bool) -> exp.Column:
    node = _parse(text, dialect, where)
    if (
        not isinstance(node, exp.Column)
        or isinstance(node.this, exp.Star)
        or bool(node.table) != qualified
        or node.args.get("db")
        or node.args.get("catalog")
    ):
        style = "alias.column" if qualified else "an unqualified column name"
        raise ContractError(f"{where}: {text!r} must be {style}, not an expression")
    return node


def _identifier(text: str, dialect: str, where: str) -> str:
    return _column(text, dialect, where, qualified=False).name


def _columns(
    values: Any, dialect: str, where: str, *, qualified: bool
) -> list[exp.Column]:
    columns = [
        _column(x, dialect, where, qualified=qualified) for x in _strings(values, where)
    ]
    identities = [tuple(part.name for part in c.parts) for c in columns]
    if len(set(identities)) != len(identities):
        raise ContractError(f"{where}: duplicate columns after dialect normalization")
    return columns


def _grain_expressions(values: Any, dialect: str, where: str) -> list[exp.Expression]:
    """Deterministic scalar grain expressions, parsed by SQLGlot in SELECT context."""
    expressions = []
    identities = set()
    volatile = {
        "RAND",
        "RANDOM",
        "UUID",
        "UUID_STRING",
        "GEN_RANDOM_UUID",
        "NEWID",
        "NEXTVAL",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
    }
    for text in _strings(values, where):
        parsed = _parse(f"SELECT {text}", dialect, where)
        if (
            not isinstance(parsed, exp.Select)
            or len(parsed.expressions) != 1
            or {key for key, value in parsed.args.items() if value} - {"expressions"}
        ):
            raise ContractError(
                f"{where}: each grain item must be one scalar expression"
            )
        node = parsed.expressions[0]
        for part in node.walk():
            if isinstance(
                part,
                (
                    exp.AggFunc,
                    exp.Window,
                    exp.Query,
                    exp.Alias,
                    exp.Star,
                    exp.Parameter,
                    exp.Placeholder,
                ),
            ):
                raise ContractError(
                    f"{where}: grain expressions cannot contain aggregates, windows, queries, aliases, stars or parameters"
                )
            if isinstance(part, exp.Func):
                name = part.name if isinstance(part, exp.Anonymous) else part.sql_name()
                if name.upper() in volatile:
                    raise ContractError(
                        f"{where}: grain expressions must be deterministic; {name} is not supported"
                    )
        columns = list(node.find_all(exp.Column))
        if not columns or any(
            not c.table or c.args.get("db") or c.args.get("catalog") for c in columns
        ):
            raise ContractError(
                f"{where}: grain expressions must reference qualified alias.column names"
            )
        try:
            identity = node.sql(
                dialect=dialect, identify=True, unsupported_level=ErrorLevel.RAISE
            )
        except SqlglotError as exc:
            raise ContractError(
                f"{where}: unsupported grain expression: {exc}"
            ) from exc
        if identity in identities:
            raise ContractError(
                f"{where}: duplicate grain expression after dialect normalization"
            )
        identities.add(identity)
        expressions.append(node)
    return expressions


def _grain_tables(expressions: list[exp.Expression]) -> set[str]:
    return {
        c.table for expression in expressions for c in expression.find_all(exp.Column)
    }


def _qualified_keys(source: exp.Table, columns: list[exp.Column]) -> list[exp.Column]:
    alias = source.args.get("alias")
    table_id = alias.this if alias else source.this
    return [exp.Column(this=c.this.copy(), table=table_id.copy()) for c in columns]


def _relation_identity(source: exp.Table) -> tuple[str, ...]:
    # normalize_identifiers has already applied dialect case rules. Quoting an
    # otherwise identical name must not make it a different relation identity.
    return tuple(part.name for part in source.parts)


def _source(node: exp.Expression, scope: str) -> exp.Table:
    if not isinstance(node, exp.Table) or not isinstance(node.this, exp.Identifier):
        raise ContractError(
            f"scope {scope}: FROM/JOIN sources must be tables or named CTEs; use a CTE for derived tables"
        )
    if any(
        value
        for key, value in node.args.items()
        if key not in {"this", "db", "catalog", "alias"}
    ):
        raise ContractError(
            f"scope {scope}: table modifiers are not supported: {node.sql()}"
        )
    if node.args.get("alias") and node.args["alias"].columns:
        raise ContractError(
            f"scope {scope}: column-renaming table aliases are not supported"
        )
    return node


def _conjuncts(node: exp.Expression):
    if isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    elif isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    else:
        yield node


def _join_keys(join: exp.Join, prior: set[str], right: str, scope: str):
    if (
        join.args.get("kind", "").upper() not in {"", "INNER", "OUTER"}
        or join.args.get("side", "").upper() not in {"", "LEFT", "RIGHT", "FULL"}
        or join.args.get("method")
        or join.args.get("using")
        or not join.args.get("on")
        or any(join.args.get(k) for k in ("match_condition", "pivots"))
    ):
        raise ContractError(
            f"scope {scope}, join {right}: only INNER/LEFT/RIGHT/FULL JOIN with explicit equi-ON predicates is supported"
        )
    left_keys, right_keys = [], []
    for term in _conjuncts(join.args["on"]):
        if not isinstance(term, exp.EQ):
            raise ContractError(
                f"scope {scope}, join {right}: ON must be a conjunction of qualified column = column predicates"
            )
        a, b = term.left.unnest(), term.right.unnest()
        if not all(
            isinstance(c, exp.Column)
            and c.table
            and not c.args.get("db")
            and not c.args.get("catalog")
            and not isinstance(c.this, exp.Star)
            for c in (a, b)
        ):
            raise ContractError(
                f"scope {scope}, join {right}: ON must be a conjunction of qualified column = column predicates"
            )
        if a.table == right and b.table in prior:
            a, b = b, a
        if a.table not in prior or b.table != right:
            raise ContractError(
                f"scope {scope}, join {right}: each equality must connect an earlier source to {right}"
            )
        left_keys.append(a.copy())
        right_keys.append(b.copy())

    # Repeated equalities must not produce duplicate GROUP BY / projection keys.
    def unique(columns):
        return list({c.sql(): c for c in columns}.values())

    return unique(left_keys), unique(right_keys)


def _project(relation: exp.Select, columns: list[exp.Expression]) -> exp.Select:
    query = relation.copy()
    query.set("expressions", [c.copy().as_(f"k{i}") for i, c in enumerate(columns)])
    return query


def _keys(count: int) -> list[exp.Column]:
    return [exp.column(f"k{i}") for i in range(count)]


def _uniqueness(
    relation: exp.Select, columns: list[exp.Expression], *, nullable: bool
) -> exp.Select:
    keys = _keys(len(columns))
    query = exp.select(*keys, exp.func("COUNT", exp.Star()).as_("occurrences")).from_(
        _project(relation, columns).subquery("gc_keys")
    )
    if nullable:
        query = query.where(
            exp.and_(
                *[
                    exp.Not(this=exp.Is(this=k.copy(), expression=exp.Null()))
                    for k in keys
                ]
            )
        )
    condition = exp.GT(
        this=exp.func("COUNT", exp.Star()), expression=exp.Literal.number(1)
    )
    if not nullable:
        condition = exp.or_(
            condition, *[exp.Is(this=k.copy(), expression=exp.Null()) for k in keys]
        )
    return query.group_by(*[k.copy() for k in keys]).having(condition)


def _row_count(before: exp.Select, after: exp.Select) -> exp.Select:
    def count(query, name):
        query = query.copy()
        query.set("expressions", [exp.Literal.number(1)])
        return exp.select(exp.func("COUNT", exp.Star())).from_(query.subquery(name))

    counts = exp.select(
        count(before, "gc_before").subquery().as_("expected_rows"),
        count(after, "gc_after").subquery().as_("actual_rows"),
    )
    return (
        exp.select("*")
        .from_(counts.subquery("gc_counts"))
        .where(
            exp.NEQ(
                this=exp.column("expected_rows"), expression=exp.column("actual_rows")
            )
        )
    )


def _preservation(
    before: exp.Select, after: exp.Select, columns: list[exp.Expression]
) -> exp.Select:
    """Bag comparison, by grain, catches offsetting lost and multiplied rows."""
    keys = _keys(len(columns))

    def counts(query, side):
        vals = [
            exp.func("COUNT", exp.Star()) if side == s else exp.Literal.number(0)
            for s in ("before", "after")
        ]
        return (
            exp.select(
                *[k.copy() for k in keys],
                vals[0].as_("before_rows"),
                vals[1].as_("after_rows"),
            )
            .from_(_project(query, columns).subquery(f"gc_{side}"))
            .group_by(*[k.copy() for k in keys])
        )

    union = exp.Union(
        this=counts(before, "before"), expression=counts(after, "after"), distinct=False
    )
    before_sum, after_sum = (
        exp.func("SUM", exp.column("before_rows")),
        exp.func("SUM", exp.column("after_rows")),
    )
    return (
        exp.select(
            *keys,
            before_sum.copy().as_("expected_rows"),
            after_sum.copy().as_("actual_rows"),
        )
        .from_(union.subquery("gc_bag"))
        .group_by(*[k.copy() for k in keys])
        .having(exp.NEQ(this=before_sum, expression=after_sum))
    )


def compile_contract(sql: str, contract: dict[str, Any]) -> Bundle:
    """Compile one SELECT and version-1 declarations into independent SQL checks.

    This checks supported structure, not column existence. Binding still belongs
    to the database. Unknown or omitted join declarations fail closed.
    """
    contract = _object(
        contract,
        "contract",
        allowed={"version", "dialect", "primary_keys", "scopes", "output_grain"},
    )
    if type(contract.get("version")) is not int or contract["version"] != 1:
        raise ContractError("contract.version must be 1")
    dialect = contract.get("dialect", "duckdb")
    if not isinstance(dialect, str) or dialect not in _DIALECTS:
        raise ContractError(f"dialect must be one of {sorted(_DIALECTS)}")
    tree = _parse(sql, dialect, "SQL")
    if not isinstance(tree, exp.Select):
        raise ContractError("SQL must be one SELECT, optionally with nonrecursive CTEs")
    for node in tree.walk():
        if isinstance(
            node,
            (
                exp.DML,
                exp.DDL,
                exp.Drop,
                exp.Alter,
                exp.TruncateTable,
                exp.Command,
                exp.Into,
                exp.Lock,
            ),
        ):
            raise ContractError("SQL must contain only read-only SELECT queries")
        if isinstance(node, exp.CTE) and not isinstance(
            node.this, (exp.Query, exp.Values)
        ):
            raise ContractError(
                "CTE bodies must contain only read-only SELECT queries or VALUES"
            )
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise ContractError("recursive WITH clauses are not supported")

    raw_pks = contract.get("primary_keys")
    if not isinstance(raw_pks, dict) or not raw_pks:
        raise ContractError(
            "primary_keys must map table/CTE names to nonempty column arrays"
        )
    primary_keys: dict[tuple[str, ...], list[exp.Column]] = {}
    for name, values in raw_pks.items():
        if not isinstance(name, str):
            raise ContractError("primary_keys names must be SQL relation names")
        parsed = _parse(f"SELECT * FROM {name}", dialect, "primary_keys relation")
        if not isinstance(parsed, exp.Select) or {
            key for key, value in parsed.args.items() if value
        } - {"expressions", "from_"}:
            raise ContractError(f"invalid primary_keys relation {name!r}")
        table = _source(parsed.args["from_"].this, "primary_keys")
        if table.args.get("alias"):
            raise ContractError("primary_keys relation names cannot include aliases")
        identity = _relation_identity(table)
        if identity in primary_keys:
            raise ContractError(f"duplicate primary_keys relation {name}")
        primary_keys[identity] = _columns(
            values, dialect, f"primary_keys.{name}", qualified=False
        )

    raw_scopes = contract.get("scopes")
    if not isinstance(raw_scopes, dict):
        raise ContractError(
            "scopes must be an object keyed by $root or top-level CTE name"
        )
    declarations = {}
    for name, value in raw_scopes.items():
        if not isinstance(name, str):
            raise ContractError("scope names must be strings")
        normalized = (
            name if name == "$root" else _identifier(name, dialect, "scope name")
        )
        if normalized in declarations:
            raise ContractError(f"duplicate scope {name}")
        declarations[normalized] = value

    scopes: dict[str, tuple[exp.Select, list[exp.CTE]]] = {}
    preceding: list[exp.CTE] = []
    for cte in tree.ctes:
        if cte.alias_or_name in scopes or cte.alias_or_name == "$root":
            raise ContractError(f"duplicate/reserved CTE name {cte.alias_or_name}")
        if cte.args["alias"].columns:
            raise ContractError(
                "CTE column alias lists are not supported; alias the projections instead"
            )
        if isinstance(cte.this, exp.Select):
            scopes[cte.alias_or_name] = (cte.this, list(preceding))
        preceding.append(cte)
    scopes["$root"] = (tree, list(preceding))
    if declarations.keys() - scopes.keys():
        raise ContractError(
            f"unknown scopes {sorted(declarations.keys() - scopes.keys())}"
        )
    covered_joins = {
        id(j) for query, _ in scopes.values() for j in query.args.get("joins", [])
    }
    if any(id(join) not in covered_joins for join in tree.find_all(exp.Join)):
        raise ContractError(
            "joins inside derived tables, scalar subqueries, or set operations are not supported; use a top-level SELECT CTE"
        )
    for name, (query, _) in scopes.items():
        if query.args.get("joins") and name not in declarations:
            raise ContractError(
                f"scope {name}: join-bearing SELECT needs a scope declaration"
            )

    checks: list[Check] = []
    join_plans: list[JoinPlan] = []

    def names(columns):
        return tuple(c.sql(dialect=dialect) for c in columns)

    def emit(kind, scope, join, description, query, columns=()):
        try:
            # Reuse SQLGlot's scope-aware rule rather than carrying every earlier
            # CTE through every assertion. Full structural validation happened
            # above, so unsupported joins cannot disappear through this pruning.
            query = eliminate_ctes(query)
            rendered = (
                query.sql(
                    dialect=dialect, pretty=True, unsupported_level=ErrorLevel.RAISE
                )
                + ";\n"
            )
        except SqlglotError as exc:
            raise ContractError(f"cannot generate {dialect} SQL: {exc}") from exc
        checks.append(
            Check(
                f"gc_{len(checks) + 1:03d}_{kind}",
                kind,
                scope,
                join,
                description,
                rendered,
                names(columns),
            )
        )

    for name, (query, ctes) in scopes.items():
        if name not in declarations:
            continue
        if name != "$root" and query.args.get("with_"):
            raise ContractError(
                f"scope {name}: a contracted join scope cannot own a nested WITH; keep joined CTEs at the top level"
            )
        declaration = _object(
            declarations[name], f"scope {name}", allowed={"grain", "joins"}
        )
        source_node = query.args.get("from_")
        if source_node is None:
            raise ContractError(f"scope {name}: FROM is required")
        base = _source(source_node.this, name)
        joins = query.args.get("joins", [])
        sources = [base] + [_source(j.this, name) for j in joins]
        aliases = [s.alias_or_name for s in sources]
        if len(set(aliases)) != len(aliases):
            raise ContractError(f"scope {name}: source aliases must be unique")

        join_rules = declaration.get("joins")
        if not isinstance(join_rules, dict):
            raise ContractError(
                f"scope {name}.joins must be an object keyed by right-hand alias"
            )
        rules = {}
        for alias, rule in join_rules.items():
            if not isinstance(alias, str):
                raise ContractError(f"scope {name}: join aliases must be strings")
            normalized = _identifier(alias, dialect, f"scope {name} join alias")
            if normalized in rules:
                raise ContractError(f"scope {name}: duplicate join alias {alias}")
            rules[normalized] = rule
        if set(rules) != set(aliases[1:]):
            raise ContractError(
                f"scope {name}: join declarations must exactly match {aliases[1:]}, got {list(rules)}"
            )

        def relation(source):
            rel = exp.select(exp.Literal.number(1)).from_(source.copy())
            if ctes:
                rel.set("with_", exp.With(expressions=[c.copy() for c in ctes]))
            return rel

        source_keys = {}
        checked_sources = set()
        for source in sources:
            actual = source.copy()
            actual.set("alias", None)
            identity = _relation_identity(actual)
            if identity not in primary_keys:
                raise ContractError(
                    f"scope {name}: missing primary_keys for {actual.sql(dialect=dialect)}"
                )
            keys = _qualified_keys(source, primary_keys[identity])
            source_keys[source.alias_or_name] = keys
            # Aliases of one relation share a PK property in the same lexical
            # scope. Keep alias-specific join checks, but scan that PK once.
            if identity in checked_sources:
                continue
            checked_sources.add(identity)
            emit(
                "primary_key",
                name,
                None,
                f"{actual.sql(dialect=dialect)} primary key is unique and nonnull",
                _uniqueness(relation(source), keys, nullable=False),
                keys,
            )

        grain = _grain_expressions(
            declaration.get("grain"), dialect, f"scope {name}.grain"
        )
        if _grain_tables(grain) - {base.alias_or_name}:
            raise ContractError(
                f"scope {name}: initial grain must reference only {base.alias_or_name}"
            )
        prefix = relation(base)
        emit(
            "grain",
            name,
            None,
            "input grain is unique and nonnull",
            _uniqueness(prefix, grain, nullable=False),
            grain,
        )
        prior = {base.alias_or_name}
        for index, join in enumerate(joins):
            source = sources[index + 1]
            alias = source.alias_or_name
            rule = _object(
                rules[alias],
                f"scope {name}, join {alias}",
                allowed={"cardinality", "preserve", "grain"},
            )
            cardinality, preserve = rule.get("cardinality"), rule.get("preserve")
            if not isinstance(cardinality, str) or cardinality not in _CARDINALITIES:
                raise ContractError(
                    f"scope {name}, join {alias}: cardinality must be one of {sorted(_CARDINALITIES)}"
                )
            if not isinstance(preserve, str) or preserve not in {
                "left",
                "right",
                "none",
            }:
                raise ContractError(
                    f"scope {name}, join {alias}: preserve must be left, right, or none"
                )
            left_keys, right_keys = _join_keys(join, prior, alias, name)
            right_relation = relation(source)
            if cardinality.startswith("1:"):
                emit(
                    "join_key",
                    name,
                    alias,
                    "left join keys are unique (ordinary = ignores NULL keys)",
                    _uniqueness(prefix, left_keys, nullable=True),
                    left_keys,
                )
            if cardinality.endswith(":1"):
                emit(
                    "join_key",
                    name,
                    alias,
                    "right join keys are unique (ordinary = ignores NULL keys)",
                    _uniqueness(right_relation, right_keys, nullable=True),
                    right_keys,
                )
            after = prefix.copy()
            after.append("joins", join.copy())
            if "1" in cardinality:
                # Distinct raw keys can compare equal after implicit casts or
                # collation coercion (e.g. integer 1 = strings '1' and '01').
                # Count actual matches by declared row identity as well.
                matched = after.copy()
                matched_join = matched.args["joins"][-1]
                matched_join.set("side", None)
                matched_join.set("kind", "INNER")
                if cardinality.endswith(":1"):
                    emit(
                        "join_cardinality",
                        name,
                        alias,
                        "each left grain matches at most one right row",
                        _uniqueness(matched, grain, nullable=True),
                        grain,
                    )
                if cardinality.startswith("1:"):
                    emit(
                        "join_cardinality",
                        name,
                        alias,
                        "each right primary key matches at most one left row",
                        _uniqueness(matched, source_keys[alias], nullable=True),
                        source_keys[alias],
                    )
            if preserve != "none":
                before = prefix if preserve == "left" else right_relation
                preserved_keys = grain if preserve == "left" else source_keys[alias]
                emit(
                    "row_count",
                    name,
                    alias,
                    f"join preserves the {preserve} input row count",
                    _row_count(before, after),
                )
                emit(
                    "preservation",
                    name,
                    alias,
                    f"join preserves every {preserve} input grain, including multiplicity",
                    _preservation(before, after, preserved_keys),
                    preserved_keys,
                )
            prior.add(alias)
            if "grain" in rule:
                next_grain = _grain_expressions(
                    rule["grain"], dialect, f"scope {name}, join {alias}.grain"
                )
            else:
                next_grain = source_keys[alias] if preserve == "right" else grain
            if _grain_tables(next_grain) - prior:
                raise ContractError(
                    f"scope {name}, join {alias}: grain references an unavailable source"
                )
            join_plans.append(
                JoinPlan(
                    name,
                    index + 1,
                    join.side or "INNER",
                    tuple(aliases[: index + 1]),
                    alias,
                    source.sql(dialect=dialect),
                    join.args["on"].sql(dialect=dialect),
                    names(left_keys),
                    names(right_keys),
                    names(grain),
                    names(next_grain),
                    cardinality,
                    preserve,
                )
            )
            emit(
                "grain",
                name,
                alias,
                "join output grain is unique and nonnull",
                _uniqueness(after, next_grain, nullable=False),
                next_grain,
            )
            prefix, grain = after, next_grain

    output_grain = _columns(
        contract.get("output_grain"), dialect, "output_grain", qualified=False
    )
    output = exp.select(exp.Star()).from_(tree.copy().subquery("gc_model"))
    emit(
        "output_grain",
        "$root",
        None,
        "complete model output grain is unique and nonnull",
        _uniqueness(output, output_grain, nullable=False),
        output_grain,
    )
    return Bundle(
        1,
        dialect,
        sqlglot.__version__,
        hashlib.sha256(sql.encode()).hexdigest(),
        hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        tuple(checks),
        tuple(join_plans),
    )
