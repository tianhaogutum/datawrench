"""Small, conservative AST dataflow engine; no imports of target code or Polars."""
from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, asdict

RULES = {
    "PLV001": "Eager file read immediately converted to lazy; use a scan reader",
    "PLV002": "Trivial Python element lambda; use a native Polars expression",
    "PLV003": "Python iteration over materialized rows; prefer expressions",
    "PLV004": "with_columns in a loop; batch independent expressions outside the loop",
    "PLV005": "Repeated collection or collect().lazy(); keep the pipeline lazy",
    "PLV006": "Selection after collecting a scan; move it before collect for pushdown",
    "PLV007": "Polars to pandas to Polars round trip; stay in Polars where possible",
    "PLV008": "Pandas-style boolean frame indexing; use filter (indexing may fail)",
    "PLV009": "Full sort followed by head; consider top_k/bottom_k, then sort the result",
    "PLV010": "concat inside a loop; accumulate frames and concatenate once",
    "PLV011": "Selection immediately after eager file read; consider a scan and collect",
}
READERS = {"read_csv", "read_parquet", "read_ipc", "read_ndjson"}
SCANNERS = {name.replace("read_", "scan_") for name in READERS}
FRAMES = {"frame", "scan", "lazy", "collected_scan"}
EAGER = {"frame", "collected_scan"}
LAZY = {"scan", "lazy"}
VALUE = {"expr", "series"}
EXPR_METHODS = {
    "alias", "cast", "map_elements", "apply", "sort", "head", "tail", "slice",
    "sum", "mean", "min", "max", "count", "first", "last", "unique", "filter",
    "fill_null", "fill_nan", "is_null", "is_not_null", "is_in", "abs", "round",
    "clip", "shift", "diff", "over", "then", "otherwise", "drop_nulls", "reverse",
}
SERIES_METHODS = {
    "alias", "cast", "map_elements", "apply", "sort", "head", "tail", "slice",
    "unique", "filter", "fill_null", "fill_nan", "is_null", "is_not_null",
    "is_in", "abs", "round", "clip", "shift", "diff", "drop_nulls", "reverse",
}
FRAME_METHODS = {
    "select", "filter", "with_columns", "with_column", "sort", "head", "tail",
    "slice", "drop", "rename", "cast", "fill_null", "fill_nan", "unique",
    "drop_nulls", "explode", "unnest", "reverse", "clone", "top_k", "bottom_k",
}


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    code: str
    message: str
    edits: tuple[Edit, ...] = ()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["fixable"] = bool(result.pop("edits"))
        return result


@dataclass(frozen=True)
class Suppression:
    path: str
    line: int
    code: str
    reason: str
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResult:
    diagnostics: list[Diagnostic]
    suppressions: list[Suppression]
    suppression_status: str = 'complete'


@dataclass(frozen=True)
class Directive:
    token: tokenize.TokenInfo
    codes: frozenset[str]
    reason: str


class Source:
    def __init__(self, text: str, path: str):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.noqa: dict[int, set[str] | None] = {}
        self.suppressions: dict[int, Directive] = {}
        self.suppression_errors: list[Diagnostic] = []
        self.tokenization_failed = False
        self.path = path
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type != tokenize.COMMENT:
                    continue
                if re.match(r"#\s*polars-vet\b", token.string):
                    self.parse_suppression(token)
                    continue
                match = re.match(r"#\s*noqa\b(?:\s*:\s*([A-Za-z0-9_, ]+))?", token.string, re.I)
                if match:
                    self.noqa[token.start[0]] = (
                        {c.strip().upper() for c in match[1].split(",")} if match[1] else None
                    )
        except (tokenize.TokenError, IndentationError):
            self.tokenization_failed = True  # ast.parse normally reports the failure.

    def comment_error(self, token: tokenize.TokenInfo, message: str) -> Diagnostic:
        return Diagnostic(self.path, token.start[0], token.start[1] + 1,
                          token.end[0], token.end[1] + 1, "PLV000", message)

    def parse_suppression(self, token: tokenize.TokenInfo):
        match = re.fullmatch(
            r"#\s*polars-vet:\s*ignore\[\s*(PLV[0-9]{3}(?:\s*,\s*PLV[0-9]{3})*)\s*\]\s+--\s+(\S.*)",
            token.string,
        )
        if not match:
            message = "Malformed suppression; expected # polars-vet: ignore[PLV001, ...] -- nonempty reason"
        else:
            codes = [code.strip() for code in match[1].split(",")]
            if set(codes) - RULES.keys():
                message = "Unknown suppression rule code; expected exact PLV001 through PLV011"
            elif len(set(codes)) != len(codes):
                message = "Duplicate suppression rule code"
            else:
                self.suppressions[token.start[0]] = Directive(token, frozenset(codes), match[2].strip())
                return
        self.suppression_errors.append(self.comment_error(token, message))

    def inventory(self, selected: set[str], used: set[tuple[int, str]] | None) -> list[Suppression]:
        # None means analysis failed: never report partial usage as authoritative.
        return [Suppression(self.path, line, code, directive.reason,
                            'disabled' if code not in selected else
                            'unknown' if used is None else
                            'used' if (line, code) in used else 'unused')
                for line, directive in sorted(self.suppressions.items())
                for code in sorted(directive.codes)]

    def offset(self, line: int, byte_column: int) -> int:
        # Python AST columns count UTF-8 bytes, while edits use character offsets.
        prefix = self.lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8")
        return self.starts[line - 1] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        return self.offset(node.lineno, node.col_offset), self.offset(node.end_lineno, node.end_col_offset)

    def segment(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.text[start:end]


def method(node: ast.AST, name: str | set[str]) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and (node.func.attr == name if isinstance(name, str) else node.func.attr in name))


def target_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}


class LocalNames(ast.NodeVisitor):
    """Bindings in one lexical scope, without descending into nested scopes."""
    def __init__(self):
        self.names: set[str] = set()
        self.external: set[str] = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node):
        self.names.update(a.asname or a.name.split(".")[0] for a in node.names)

    def visit_ImportFrom(self, node):
        self.names.update(a.asname or a.name for a in node.names)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_ListComp(self, node):
        # Targets belong to the comprehension; walrus bindings in expressions
        # still belong to this lexical scope.
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Global(self, node):
        self.external.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.names.add(node.rest)
        self.generic_visit(node)


class Analyzer(ast.NodeVisitor):
    def __init__(self, source: Source, path: str, selected: set[str]):
        self.source = source
        self.path = path
        # Keep builtin proof in the same environment as imports/assignments so
        # every existing invalidation (including merges/star imports) applies.
        self.env: dict[str, str] = {
            "enumerate": "builtin:enumerate", "zip": "builtin:zip", "iter": "builtin:iter",
        }
        self.loops = 0
        self.diagnostics: list[Diagnostic] = []
        self.selected = selected
        self.used_suppressions: set[tuple[int, str]] = set()

    def symbol(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.env.get(node.id, "")
        if isinstance(node, ast.Attribute) and self.symbol(node.value) == "module":
            return "fn:" + node.attr
        return ""

    def kind(self, node: ast.AST | None) -> str:
        if node is None:
            return ""
        symbol = self.symbol(node)
        if symbol:
            return "" if symbol.startswith("builtin:") else symbol
        if isinstance(node, ast.Subscript) and self.kind(node.value) in EAGER:
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                return "series"
            return ""
        if isinstance(node, (ast.BinOp, ast.Compare, ast.UnaryOp, ast.BoolOp)):
            return "expr" if any(self.kind(n) == "expr" for n in ast.iter_child_nodes(node)) else ""
        if not isinstance(node, ast.Call):
            return ""
        function = self.symbol(node.func).removeprefix("fn:")
        if function in READERS or function in {"DataFrame", "from_dict", "from_records", "from_pandas"}:
            return "frame"
        if function in SCANNERS:
            return "scan"
        if function == "LazyFrame":
            return "lazy"
        if function in {"col", "lit", "all", "len", "when", "sum", "mean"}:
            return "expr"
        if function == "Series":
            return "series"
        if function == "concat" and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            kinds = {self.kind(n) for n in node.args[0].elts}
            if kinds and kinds <= FRAMES:
                return "scan" if kinds == {"scan"} else ("lazy" if kinds <= LAZY else "frame")
        if isinstance(node.func, ast.Attribute):
            receiver = self.kind(node.func.value)
            name = node.func.attr
            if receiver in FRAMES:
                if name in FRAME_METHODS:
                    return receiver
                if name == "lazy" and receiver in EAGER:
                    return "lazy"
                if name == "collect" and receiver in LAZY:
                    return "collected_scan" if receiver == "scan" else "frame"
                if name == "to_pandas" and receiver in EAGER:
                    return "pandas"
                if name in {"iter_rows", "rows", "to_dicts"} and receiver in EAGER:
                    return "rows"
                if name in {"get_column", "to_series"} and receiver in EAGER:
                    return "series"
            if ((receiver == "expr" and name in EXPR_METHODS)
                    or (receiver == "series" and name in SERIES_METHODS)):
                return receiver
            if isinstance(node.func.value, ast.Attribute):
                namespace = node.func.value
                if namespace.attr in {"str", "dt", "list", "arr", "name", "struct"}:
                    base = self.kind(namespace.value)
                    if base in VALUE:
                        return base
        return ""

    def emit(self, node: ast.AST, code: str, detail: str = "", edits: tuple[Edit, ...] = ()):
        if code not in self.selected:
            return
        suppression = self.source.suppressions.get(node.lineno)
        if suppression and code in suppression.codes:
            self.used_suppressions.add((node.lineno, code))
            return
        if node.lineno in self.source.noqa:
            codes = self.source.noqa[node.lineno]
            if codes is None or code in codes:
                return
        start, end = self.source.span(node)
        column = start - self.source.starts[node.lineno - 1] + 1
        end_column = end - self.source.starts[node.end_lineno - 1] + 1
        self.diagnostics.append(Diagnostic(self.path, node.lineno, column, node.end_lineno,
                                           end_column, code, detail or RULES[code], edits))

    def reader_fix(self, outer: ast.Call, read: ast.Call) -> tuple[Edit, ...]:
        """Only literal local paths and a small shared, literal keyword whitelist."""
        reader = self.symbol(read.func).removeprefix("fn:")
        if reader not in READERS or outer.args or outer.keywords:
            return ()
        keywords = {kw.arg: kw.value for kw in read.keywords}
        if None in keywords or len(keywords) != len(read.keywords):
            return ()
        if len(read.args) == 1 and "source" not in keywords:
            source = read.args[0]
        elif not read.args and "source" in keywords:
            source = keywords.pop("source")
        else:
            return ()
        if not isinstance(source, ast.Constant) or not isinstance(source.value, str):
            return ()
        path = source.value
        if not path or re.search(r"[*?\[\]{}\x00]", path) or re.match(r"\w+:", path) or path.startswith(("//", "~")):
            return ()
        common = {
            "read_csv": {"has_header", "separator", "quote_char", "comment_prefix", "skip_rows", "n_rows", "try_parse_dates"},
            "read_parquet": {"n_rows", "use_statistics", "parallel"},
            "read_ipc": {"n_rows"},
            "read_ndjson": {"n_rows", "infer_schema_length"},
        }[reader]
        if not keywords.keys() <= common or any(not isinstance(v, ast.Constant) for v in keywords.values()):
            return ()
        scanner = reader.replace("read_", "scan_")
        if isinstance(read.func, ast.Attribute) and self.symbol(read.func.value) == "module":
            replacement_func = self.source.segment(read.func.value) + "." + scanner
        else:
            aliases = [name for name, value in self.env.items() if value == "fn:" + scanner]
            modules = [name for name, value in self.env.items() if value == "module"]
            if aliases:
                replacement_func = sorted(aliases)[0]
            elif modules:
                replacement_func = sorted(modules)[0] + "." + scanner
            else:
                return ()
        start, end = self.source.span(outer)
        rstart, rend = self.source.span(read)
        fstart, fend = self.source.span(read.func)
        if self.source.text[start:rstart].strip() or not re.fullmatch(r"\s*\.lazy\s*\(\s*\)", self.source.text[rend:end]):
            return ()  # Do not discard comments or wrapper parentheses.
        replacement = self.source.text[rstart:fstart] + replacement_func + self.source.text[fend:rend]
        return (Edit(start, end, replacement),)

    def lambda_hint(self, node: ast.Lambda) -> str:
        args = node.args
        if (len(args.posonlyargs) + len(args.args) != 1 or args.vararg or args.kwarg
                or args.kwonlyargs or args.defaults):
            return ""
        name = (args.posonlyargs + args.args)[0].arg
        body = node.body
        if isinstance(body, ast.BinOp) and isinstance(body.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
            operands = (body.left, body.right)
            if (any(isinstance(n, ast.Name) and n.id == name for n in operands)
                    and any(isinstance(n, ast.Constant) and type(n.value) in {int, float} for n in operands)):
                return "use native arithmetic (review dtype, null and overflow semantics)"
        if (isinstance(body, ast.Call) and isinstance(body.func, ast.Attribute)
                and isinstance(body.func.value, ast.Name) and body.func.value.id == name
                and body.func.attr in {"upper", "lower", "strip"} and not body.args and not body.keywords):
            return {"upper": "use .str.to_uppercase()", "lower": "use .str.to_lowercase()", "strip": "use .str.strip_chars()"}[body.func.attr]
        return ""

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            recv = node.func.value
            kind = self.kind(recv)
            name = node.func.attr
            if name == "lazy" and method(recv, "collect") and kind in EAGER:
                self.emit(node, "PLV005")
            if name == "lazy" and isinstance(recv, ast.Call) and self.symbol(recv.func) in {"fn:" + n for n in READERS}:
                self.emit(node, "PLV001", edits=self.reader_fix(node, recv))
            if name in {"map_elements", "apply"} and kind in VALUE:
                fn = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "function"), None)
                if isinstance(fn, ast.Lambda) and (hint := self.lambda_hint(fn)):
                    self.emit(node, "PLV002", RULES["PLV002"] + "; " + hint)
            if name == "with_columns" and kind in FRAMES and self.loops:
                self.emit(node, "PLV004")
            if name == "collect" and kind in LAZY and self.loops:
                self.emit(node, "PLV005")
            if name in {"select", "filter"} and kind == "collected_scan":
                self.emit(node, "PLV006")
            if name in {"select", "filter"} and isinstance(recv, ast.Call) and self.symbol(recv.func) in {"fn:" + n for n in READERS}:
                self.emit(node, "PLV011")
            if name == "head" and method(recv, "sort") and kind in FRAMES | VALUE:
                self.emit(node, "PLV009", RULES["PLV009"] + "; check null ordering and ties; no automatic rewrite")
        symbol = self.symbol(node.func)
        if symbol == "fn:concat" and self.loops:
            self.emit(node, "PLV010")
        if symbol in {"fn:from_pandas", "fn:DataFrame"} and node.args and self.kind(node.args[0]) == "pandas":
            self.emit(node, "PLV007")
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if self.kind(node.value) in EAGER and isinstance(node.slice, (ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp)):
            # Require the mask to reference a series from this exact frame.
            if any(isinstance(n, ast.Subscript) and ast.dump(n.value) == ast.dump(node.value)
                   and self.kind(n) == "series" for n in ast.walk(node.slice)):
                self.emit(node, "PLV008")
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.env[alias.asname or alias.name.split(".")[0]] = "module" if alias.name == "polars" else ""

    def visit_ImportFrom(self, node):
        if any(alias.name == "*" for alias in node.names):
            self.env.clear()  # An unknown export set could shadow every known binding.
            return
        for alias in node.names:
            if alias.name != "*":
                self.env[alias.asname or alias.name] = "fn:" + alias.name if node.module == "polars" and not node.level else ""

    def bind(self, target, kind=""):
        if isinstance(target, ast.Attribute):
            root = target.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and self.env.get(root.id) == "module":
                for name, value in list(self.env.items()):
                    if value == "module":
                        self.env[name] = ""  # All aliases refer to the same module.
        for name in target_names(target):
            self.env[name] = kind if isinstance(target, ast.Name) else ""

    def visit_Assign(self, node):
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Tuple)
                and len(node.targets[0].elts) == 2
                and all(isinstance(target, ast.Name) for target in node.targets[0].elts)
                and node.targets[0].elts[0].id != node.targets[0].elts[1].id
                and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
                and not any(isinstance(value, ast.Starred) for value in node.value.elts)):
            kinds = []
            # Capture RHS values in order, before either target is bound. This
            # preserves swaps and prevents later sibling walruses changing proof.
            for value in node.value.elts:
                self.visit(value)
                kinds.append("rows" if self.kind(value) == "rows" else "")
            for target, kind in zip(node.targets[0].elts, kinds):
                self.visit(target)
                self.bind(target, kind)
            return
        self.visit(node.value)
        kind = self.kind(node.value)
        for target in node.targets:
            self.visit(target)
            self.bind(target, kind)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
        self.bind(node.target, self.kind(node.value))

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        self.bind(node.target)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.bind(node.target, self.kind(node.value))

    def visit_Delete(self, node):
        for target in node.targets:
            self.bind(target)

    def scoped(self, body, args=None):
        old, loops = self.env.copy(), self.loops
        self.loops = 0
        local = LocalNames()
        for statement in body:
            local.visit(statement)
        for name in local.names | local.external:
            self.env[name] = ""
        if args:
            for arg in args.posonlyargs + args.args + args.kwonlyargs:
                self.env[arg.arg] = ""
            if args.vararg:
                self.env[args.vararg.arg] = ""
            if args.kwarg:
                self.env[args.kwarg.arg] = ""
        for statement in body:
            self.visit(statement)
        self.env, self.loops = old, loops

    def visit_FunctionDef(self, node):
        for value in node.decorator_list + node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.env[node.name] = ""
        self.scoped(node.body, node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.env[node.name] = ""
        # Class bodies and methods are not part of the enclosing loop execution.
        self.scoped(node.body)

    def visit_Lambda(self, node):
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.scoped([node.body], node.args)

    def visit_YieldFrom(self, node):
        rows = node.value
        # Delegation consumes the value directly; loop wrappers do not apply.
        if ((isinstance(rows, ast.Name) or method(rows, {"iter_rows", "rows", "to_dicts"}))
                and self.kind(rows) == "rows"):
            self.emit(rows, "PLV003")
        self.visit(rows)

    def loop_iter(self, node):
        sources = [node]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "enumerate" and self.env.get("enumerate") == "builtin:enumerate"
                and len(node.args) in {1, 2}
                and not any(isinstance(arg, ast.Starred) for arg in node.args)
                and (not node.keywords or
                     (len(node.args) == 1 and len(node.keywords) == 1 and node.keywords[0].arg == "start"))):
            sources = [node.args[0]]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "zip" and self.env.get("zip") == "builtin:zip"
                and len(node.args) in {2, 3}
                and (not node.keywords or
                     (len(node.keywords) == 1 and node.keywords[0].arg == "strict"
                      and isinstance(node.keywords[0].value, ast.Constant)
                      and isinstance(node.keywords[0].value.value, bool)))
                and not any(isinstance(arg, ast.Starred) for arg in node.args)):
            sources = node.args
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "iter" and self.env.get("iter") == "builtin:iter"
                and len(node.args) == 1 and not node.keywords
                and not isinstance(node.args[0], ast.Starred)):
            sources = [node.args[0]]
        for rows in sources:
            if ((isinstance(rows, ast.Name) or method(rows, {"iter_rows", "rows", "to_dicts"}))
                    and self.kind(rows) == "rows"):
                self.emit(rows, "PLV003")
        self.visit(node)

    @staticmethod
    def merge(left, right):
        return {key: value for key, value in left.items() if right.get(key) == value}

    def visit_For(self, node):
        self.loop_iter(node.iter)
        before = self.env.copy()
        self.bind(node.target)
        self.loops += 1
        for statement in node.body:
            self.visit(statement)
        self.loops -= 1
        self.env = self.merge(before, self.env)
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        before = self.env.copy()
        self.loops += 1
        self.visit(node.test)
        for statement in node.body:
            self.visit(statement)
        self.loops -= 1
        self.env = self.merge(before, self.env)
        for statement in node.orelse:
            self.visit(statement)

    def comprehension(self, node):
        old, loops = self.env.copy(), self.loops
        # Python evaluates the first iterable outside the comprehension scope.
        self.loop_iter(node.generators[0].iter)
        for generator in node.generators:
            self.bind(generator.target)
        for index, generator in enumerate(node.generators):
            if index:
                self.loop_iter(generator.iter)
            self.bind(generator.target)
            self.loops += 1
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.env, self.loops = old, loops

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension

    def visit_If(self, node):
        self.visit(node.test)
        before = self.env.copy()
        for statement in node.body:
            self.visit(statement)
        left = self.env.copy()
        self.env = before
        for statement in node.orelse:
            self.visit(statement)
        self.env = self.merge(left, self.env)

    def visit_Try(self, node):
        # Complex control flow: invalidate all bindings written anywhere in try.
        names = LocalNames()
        names.generic_visit(node)
        for name in names.names:
            self.env[name] = ""
        for statement in node.body + node.orelse + node.finalbody:
            self.visit(statement)
        for handler in node.handlers:
            if handler.name:
                self.env[handler.name] = ""
            for statement in handler.body:
                self.visit(statement)
        for name in names.names:
            self.env[name] = ""

    visit_TryStar = visit_Try

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.bind(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_Match(self, node):
        self.visit(node.subject)
        before = self.env.copy()
        merged = before.copy()  # Include the no-match path, conservatively.
        for case in node.cases:
            self.env = before.copy()
            for pattern in ast.walk(case.pattern):
                for field in ("name", "rest"):
                    name = getattr(pattern, field, None)
                    if isinstance(name, str):
                        self.env[name] = ""
            if case.guard:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
            merged = self.merge(merged, self.env)
        self.env = merged


def analyze(text: str, path: str = "<string>", *, tree: ast.AST | None = None,
            honor_noqa: bool = True, report_unused_suppressions: bool = False,
            selected: set[str] | None = None) -> list[Diagnostic]:
    """Analyze selected rules; PLV000 errors remain active regardless of selection.

    Reasoned directives always apply. honor_noqa controls only legacy noqa, so
    flake8 can handle its own noqa policy. Unused checks cover selected rules only.
    """
    return analyze_with_suppressions(text, path, tree=tree, honor_noqa=honor_noqa,
                                     report_unused_suppressions=report_unused_suppressions,
                                     selected=selected).diagnostics


def analyze_with_suppressions(text: str, path: str = "<string>", *, tree: ast.AST | None = None,
                              honor_noqa: bool = True, report_unused_suppressions: bool = False,
                              selected: set[str] | None = None) -> AnalysisResult:
    """Analyze once and include valid reasoned directives with per-code usage.

    Reasons are audit data; callers must explicitly opt in to disclosing them.
    Failed validation/analysis leaves selected directive usage unknown.
    """
    source = Source(text, path)
    selected = set(RULES) if selected is None else selected
    if not honor_noqa:
        source.noqa.clear()
    try:
        tree = tree if tree is not None else ast.parse(text, filename=path)
        # ast.parse alone accepts contextual errors such as return outside a function.
        # Compile for syntax validation only; the resulting code is never executed.
        compile(tree, path, "exec", dont_inherit=True)
        if source.tokenization_failed:
            # A caller-supplied AST must not make a partial token inventory complete.
            raise SyntaxError("Tokenization could not finish")
    except (SyntaxError, ValueError, RecursionError) as exc:
        line = getattr(exc, "lineno", None) or 1
        column = getattr(exc, "offset", None) or 1
        # Do not echo source lines (they can contain secrets).
        diagnostics = [Diagnostic(path, line, column, line, column + 1, "PLV000", "Python syntax error: " + (getattr(exc, "msg", None) or type(exc).__name__))]
        return AnalysisResult(diagnostics, source.inventory(selected, None), 'syntax-error')
    visitor = Analyzer(source, path, selected)
    try:
        visitor.visit(tree)
    except RecursionError:
        diagnostics = [Diagnostic(path, 1, 1, 1, 2, "PLV000", "Python syntax nesting exceeds analysis limit")]
        return AnalysisResult(diagnostics, source.inventory(selected, None), 'analysis-error')
    diagnostics = visitor.diagnostics + source.suppression_errors
    if report_unused_suppressions:
        for line, directive in source.suppressions.items():
            for code in sorted(directive.codes & selected):
                if (line, code) not in visitor.used_suppressions:
                    diagnostics.append(source.comment_error(directive.token, "Unused suppression for " + code))
    return AnalysisResult(sorted(diagnostics, key=lambda d: (d.line, d.column, d.code, d.message)),
                          source.inventory(selected, visitor.used_suppressions),
                          'invalid-directive' if source.suppression_errors else 'complete')


def fix_source(text: str, diagnostics: list[Diagnostic]) -> str:
    edits = sorted({edit for d in diagnostics for edit in d.edits}, key=lambda e: e.start)
    for left, right in zip(edits, edits[1:]):
        if left.end > right.start:
            raise ValueError("overlapping fixes; refusing to rewrite")
    for edit in reversed(edits):
        text = text[:edit.start] + edit.replacement + text[edit.end:]
    return text
