"""Tiny, safe reader for JavaScript call arguments embedded in Alpine ``x-data`` attributes.

Only literals are understood (strings, numbers, booleans, null/undefined, and
JSON-ish object/array literals); nothing is ever evaluated.
"""

from __future__ import annotations

import json
import re
from typing import Any

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_QUOTES = {"'", '"', "`"}
_NUMBER_RE = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_UNQUOTED_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)")


def call_arguments(source: str, function: str) -> list[str] | None:
    """Raw argument source strings of the first ``function(...)`` call in ``source``, or ``None``."""
    match = re.search(rf"(?<![\w$.]){re.escape(function)}\s*\(", source)
    if match is None:
        return None
    args: list[str] = []
    depth = 0
    quote = ""
    start = match.end()
    i = start
    while i < len(source):
        char = source[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in _QUOTES:
            quote = char
        elif char in _OPENERS:
            depth += 1
        elif char in ")]}":
            if depth == 0:
                if char != ")":
                    return None
                tail = source[start:i].strip()
                if tail or args:
                    args.append(tail)
                return args
            depth -= 1
        elif char == "," and depth == 0:
            args.append(source[start:i].strip())
            start = i + 1
        i += 1
    return None  # unbalanced


def literal(source: str) -> Any:
    """Value of a JS literal; object literals become dicts when they can be read as JSON, else raw text."""
    text = source.strip()
    if text in ("", "null", "undefined"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if _NUMBER_RE.match(text):
        number = float(text)
        return int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] == text[0]:
        return unescape_string(text[1:-1])
    if text[:1] in "{[":
        return _object_literal(text)
    return text


def unescape_string(body: str) -> str:
    """Decode JS string escapes (``\\/``, ``\\'``, ``\\uXXXX``, ``\\xHH``, ``\\n`` …)."""
    if "\\" not in body:
        return body
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\" or i + 1 >= len(body):
            out.append(char)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2 : i + 6]):
            out.append(chr(int(body[i + 2 : i + 6], 16)))
            i += 6
        elif nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", body[i + 2 : i + 4]):
            out.append(chr(int(body[i + 2 : i + 4], 16)))
            i += 4
        else:
            out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
    return "".join(_join_surrogates(out))


def _join_surrogates(chars: list[str]) -> list[str]:
    text = "".join(chars)
    try:
        return [text.encode("utf-16", "surrogatepass").decode("utf-16")]
    except UnicodeError:
        return [text]


def _object_literal(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Relaxed JS: unquoted keys and single-quoted strings.
    relaxed = _UNQUOTED_KEY_RE.sub(r'\1"\2"\3', text)
    relaxed = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(unescape_string(m.group(1))), relaxed)
    relaxed = re.sub(r",\s*([}\]])", r"\1", relaxed)
    try:
        return json.loads(relaxed)
    except ValueError:
        return text
