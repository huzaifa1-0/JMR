"""Boolean query parser (spec section 4).

Accepts the syntax researchers actually type:

    ("medical billing" OR "revenue cycle") AND ("remote" OR "work from home")
    ("software engineer" OR developer) NOT internship

and compiles it to a validated FTS5 MATCH expression.  Parsing rather than
string-substitution matters for two reasons: we can give precise syntax errors
with a caret, and we never pass raw user text into SQLite's matcher (which has
its own operators -- `^`, `*`, `NEAR` -- that would otherwise be injectable).

Grammar
    expr    := or_expr
    or_expr := and_expr ( OR and_expr )*
    and_expr:= not_expr ( (AND | <implicit>) not_expr )*
    not_expr:= unary ( NOT unary )*
    unary   := '(' expr ')' | PHRASE | TERM
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.errors import BooleanSyntaxError

# --------------------------------------------------------------------------
# Tokeniser
# --------------------------------------------------------------------------
TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<phrase>"[^"]*")
    | (?P<op>\b(?:AND|OR|NOT)\b)
    | (?P<amp>&&|\|\||!)
    | (?P<term>[^\s()"]+)
""", re.VERBOSE)

SYMBOL_OPS = {"&&": "AND", "||": "OR", "!": "NOT"}


@dataclass
class Token:
    kind: str      # lparen rparen phrase op term
    value: str
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(text):
        m = TOKEN_RE.match(text, i)
        if not m:
            raise BooleanSyntaxError(
                f"Unexpected character at position {i + 1}.",
                hint=_caret(text, i))
        i = m.end()
        kind = m.lastgroup
        val = m.group()
        if kind == "ws":
            continue
        if kind == "amp":
            tokens.append(Token("op", SYMBOL_OPS[val], m.start()))
        elif kind == "phrase":
            inner = val[1:-1].strip()
            if not inner:
                raise BooleanSyntaxError(
                    "Empty quoted phrase.",
                    hint='Remove the empty "" or put a term inside it.')
            tokens.append(Token("phrase", inner, m.start()))
        elif kind == "op":
            tokens.append(Token("op", val.upper(), m.start()))
        else:
            tokens.append(Token(kind, val, m.start()))
    return tokens


def _caret(text: str, pos: int) -> str:
    return f"{text}\n{' ' * pos}^"


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------
@dataclass
class Term:
    value: str
    phrase: bool = False


@dataclass
class BinOp:
    op: str          # AND | OR | NOT
    left: object
    right: object


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    # -- helpers
    def peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def eat(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_op(self, name: str) -> bool:
        tok = self.peek()
        if tok and tok.kind == "op" and tok.value == name:
            self.eat()
            return True
        return False

    # -- grammar
    def parse(self):
        if not self.tokens:
            return None
        node = self.parse_or()
        if self.i < len(self.tokens):
            tok = self.tokens[self.i]
            raise BooleanSyntaxError(
                f"Unexpected '{tok.value}' at position {tok.pos + 1}.",
                hint=_caret(self.text, tok.pos))
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.expect_op("OR"):
            right = self.parse_and()
            node = BinOp("OR", node, right)
        return node

    def parse_and(self):
        node = self.parse_not()
        while True:
            tok = self.peek()
            if tok is None:
                break
            if tok.kind == "op" and tok.value == "AND":
                self.eat()
                node = BinOp("AND", node, self.parse_not())
            elif tok.kind in ("term", "phrase", "lparen"):
                # implicit AND: `remote billing` means `remote AND billing`
                node = BinOp("AND", node, self.parse_not())
            else:
                break
        return node

    def parse_not(self):
        node = self.parse_unary()
        while self.expect_op("NOT"):
            node = BinOp("NOT", node, self.parse_unary())
        return node

    def parse_unary(self):
        tok = self.peek()
        if tok is None:
            raise BooleanSyntaxError(
                "The expression ends with an operator.",
                hint="Every AND / OR / NOT needs a term after it.")
        if tok.kind == "op":
            if tok.value == "NOT":
                raise BooleanSyntaxError(
                    "NOT cannot start an expression or follow another operator.",
                    hint=('Write it as: something NOT excluded. '
                          'For example: developer NOT internship'))
            raise BooleanSyntaxError(
                f"'{tok.value}' needs a term before it.",
                hint=_caret(self.text, tok.pos))
        if tok.kind == "lparen":
            self.eat()
            node = self.parse_or()
            closing = self.peek()
            if closing is None or closing.kind != "rparen":
                raise BooleanSyntaxError(
                    "Unclosed parenthesis.",
                    hint="Add a matching ')' to close the group.")
            self.eat()
            return node
        if tok.kind == "rparen":
            raise BooleanSyntaxError(
                f"Unmatched ')' at position {tok.pos + 1}.",
                hint=_caret(self.text, tok.pos))
        self.eat()
        return Term(tok.value, phrase=(tok.kind == "phrase"))


# --------------------------------------------------------------------------
# Compilation to FTS5
# --------------------------------------------------------------------------
_FTS_UNSAFE = re.compile(r'["*^:{}()\[\]]')


def _escape(value: str) -> str:
    """Always emit a quoted FTS5 string -- neutralises every FTS5 operator."""
    cleaned = _FTS_UNSAFE.sub(" ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise BooleanSyntaxError(
            f"'{value}' contains no searchable characters.",
            hint="Remove punctuation-only terms from the expression.")
    return '"' + cleaned + '"'


def compile_node(node) -> str:
    if node is None:
        return ""
    if isinstance(node, Term):
        return _escape(node.value)
    if isinstance(node, BinOp):
        left = compile_node(node.left)
        right = compile_node(node.right)
        return f"({left} {node.op} {right})"
    raise BooleanSyntaxError("Could not interpret the expression.")


def compile_boolean(text: str | None) -> str | None:
    """User expression -> FTS5 MATCH string. None when input is blank."""
    if not text or not text.strip():
        return None
    tree = Parser(text.strip()).parse()
    if tree is None:
        return None
    return compile_node(tree)


def collect_terms(node, acc: list[str] | None = None) -> list[str]:
    """Positive terms, for result highlighting."""
    acc = acc if acc is not None else []
    if isinstance(node, Term):
        acc.append(node.value)
    elif isinstance(node, BinOp):
        collect_terms(node.left, acc)
        if node.op != "NOT":
            collect_terms(node.right, acc)
    return acc


def explain(text: str) -> dict:
    """Used by the UI to validate an expression as the user types."""
    try:
        tree = Parser(text.strip()).parse() if text.strip() else None
    except BooleanSyntaxError as exc:
        return {"valid": False, "message": exc.message, "hint": exc.hint}
    return {
        "valid": True,
        "compiled": compile_node(tree) if tree else None,
        "terms": collect_terms(tree),
    }
