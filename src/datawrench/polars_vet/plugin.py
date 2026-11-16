"""Optional flake8 entry point. Flake8 handles selection and noqa itself."""
import ast
import tokenize

from . import __version__
from .analysis import analyze


class PolarsVetChecker:
    name = "polars-vet"
    version = __version__

    def __init__(self, tree: ast.AST, filename: str, lines: list[str] | None = None):
        self.tree = tree
        self.filename = filename
        self.lines = lines

    def run(self):
        if self.lines is not None:
            source = "".join(self.lines)
        else:
            with tokenize.open(self.filename) as handle:
                source = handle.read()
        for diagnostic in analyze(source, self.filename, tree=self.tree, honor_noqa=False):
            yield diagnostic.line, diagnostic.column - 1, f"{diagnostic.code} {diagnostic.message}", type(self)
