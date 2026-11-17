"""Small adapter over sqlite-utils' existing SQLite CREATE TABLE parser.

The host lexer and top-level item splitter handle strings, comments, quoting,
and nested parentheses. Only FK timing is added here: PRAGMA foreign_key_list
does not expose it. These private helpers are isolated for upstreaming; see
docs/rebuild-check/upstream.md. No general SQL grammar is implemented here.
"""

from sqlite_utils.create_table_parser import (
    ParseError, _lex, _matching_paren, _meaningful, _split_spans, _table_body, _unquote,
)


def _columns(tokens, start):
    end = _matching_paren(tokens, start)
    return [_unquote(t.text) for t in tokens[start + 1:end] if t.text != ","], end


def foreign_key_timing(sql: str) -> list[dict]:
    body_info = _table_body(sql)
    if body_info is None:
        return []
    body, _ = body_info
    keys = []
    for item, _, _ in _split_spans(body, _lex(body)):
        tokens = _meaningful(_lex(item))
        if not tokens:
            continue
        pos = 2 if tokens[0].is_keyword("CONSTRAINT") else 0
        if pos >= len(tokens):
            raise ParseError("Constraint name without a constraint")
        if tokens[pos].is_keyword("FOREIGN"):
            columns, _ = _columns(tokens, pos + 2)
        elif any(tokens[pos].is_keyword(k) for k in ("PRIMARY", "UNIQUE", "CHECK")):
            continue
        else:
            columns = [_unquote(tokens[0].text)]
        references = []
        depth = 0
        for i, token in enumerate(tokens):
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif depth == 0 and token.is_keyword("REFERENCES"):
                references.append(i)
        for number, start in enumerate(references):
            stop = references[number + 1] if number + 1 < len(references) else len(tokens)
            parent = _unquote(tokens[start + 1].text)
            pos = start + 2
            parent_columns = []
            if pos < stop and tokens[pos].text == "(":
                parent_columns, pos = _columns(tokens, pos)
                pos += 1
            options = tokens[pos:stop]
            deferred = False
            for i, token in enumerate(options):
                if token.is_keyword("DEFERRABLE"):
                    negative = i > 0 and options[i - 1].is_keyword("NOT")
                    initially_deferred = (i + 2 < len(options) and options[i + 1].is_keyword("INITIALLY")
                                          and options[i + 2].is_keyword("DEFERRED"))
                    deferred = not negative and initially_deferred
            keys.append({"columns": columns, "parent": parent, "parent_columns": parent_columns, "deferred": deferred})
    return keys
