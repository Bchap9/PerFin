# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regular expressions from fingerprint data, restricted so they cannot backtrack badly.

Fingerprints are data: shipped YAML and, through "remember this layout",
rows a user creates. Their patterns go through :func:`compile_safe`, which
accepts only a small, linear-time subset of Python's syntax and refuses
everything else with :class:`UnsafePatternError`:

* literals, escapes (``\\d \\w \\s \\D \\W \\S \\b \\B`` and escaped
  punctuation), ``.``, character classes ``[...]`` (no nested ``[``),
  anchors ``^ $``, plain, non-capturing and named groups and ``|``;
* quantifiers ``? * + {m} {m,} {m,n}`` (``n <= 100``); **every quantifier is
  compiled possessive** (``*`` becomes ``*+`` ...), so a quantifier never
  gives back what it consumed and a match attempt is linear in the input;
* no quantifier on a group that contains a quantifier or an alternation (no
  ``(a+)+``, ``(a|aa)*``), no backreferences, lookarounds, conditionals,
  inline flags, lazy or already-possessive quantifiers, atomic groups or
  ``\\N``/``\\x``/octal escapes; at most :data:`MAX_PATTERN_CHARS`
  characters and :data:`MAX_QUANTIFIERS` quantifiers.

Callers additionally run patterns only over bounded text (one line of at
most :data:`MAX_SUBJECT_CHARS` characters), so the worst case per pattern is
quadratic in a small constant. Possessive quantifiers change meaning where a
pattern relied on giving characters back: ``.+ to .+`` never matches; write
``[^t]+ to .+`` or name the separator explicitly.
"""

from __future__ import annotations

import re
from typing import Final

MAX_PATTERN_CHARS: Final = 256
MAX_QUANTIFIERS: Final = 12
MAX_REPEAT: Final = 100
#: Fingerprint patterns only ever see one line, cut to this length.
MAX_SUBJECT_CHARS: Final = 300

_CLASS_ESCAPES: Final = frozenset("dwsDWS")
_ANCHOR_ESCAPES: Final = frozenset("bB")
#: Escaped characters that stand for themselves.
_LITERAL_ESCAPES: Final = frozenset("\\.^$|?*+()[]{}-/#&~ :,'\"@%!=<>_;")
_BRACE: Final = re.compile(r"\{(\d{1,3})(,(\d{0,3}))?\}")
_GROUP_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}>")


class UnsafePatternError(ValueError):
    """The pattern is outside the accepted subset (the message says why)."""


class _Group:
    __slots__ = ("has_alternation", "has_quantifier")

    def __init__(self) -> None:
        self.has_quantifier = False
        self.has_alternation = False


def _read_class(pattern: str, i: int) -> int:
    """Index just past the character class starting at ``pattern[i] == '['``."""
    j = i + 1
    if j < len(pattern) and pattern[j] == "^":
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    while j < len(pattern):
        c = pattern[j]
        if c == "\\":
            if j + 1 >= len(pattern):
                raise UnsafePatternError("pattern ends inside an escape")
            nxt = pattern[j + 1]
            if nxt not in _CLASS_ESCAPES and nxt not in _LITERAL_ESCAPES:
                raise UnsafePatternError(f"escape \\{nxt} is not allowed")
            j += 2
            continue
        if c == "[":
            raise UnsafePatternError("nested '[' in a character class (escape it)")
        if c == "]":
            return j + 1
        j += 1
    raise UnsafePatternError("unterminated character class")


def rewrite_possessive(pattern: str) -> str:
    """Validate ``pattern`` and return it with every quantifier made possessive."""
    if not pattern:
        raise UnsafePatternError("empty pattern")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise UnsafePatternError(f"pattern longer than {MAX_PATTERN_CHARS} characters")
    out: list[str] = []
    stack: list[_Group] = [_Group()]
    quantifiers = 0
    # What the next quantifier would apply to: None (nothing), "atom" or a closed group.
    last: str | _Group | None = None
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise UnsafePatternError("pattern ends inside an escape")
            nxt = pattern[i + 1]
            if nxt in _ANCHOR_ESCAPES:
                last = None
            elif nxt in _CLASS_ESCAPES or nxt in _LITERAL_ESCAPES:
                last = "atom"
            elif nxt.isdigit():
                raise UnsafePatternError("backreferences are not allowed")
            else:
                raise UnsafePatternError(f"escape \\{nxt} is not allowed")
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if c == "[":
            j = _read_class(pattern, i)
            out.append(pattern[i:j])
            last = "atom"
            i = j
            continue
        if c == "(":
            if pattern.startswith("(?:", i):
                out.append("(?:")
                i += 3
            elif pattern.startswith("(?P<", i):
                name = _GROUP_NAME.match(pattern, i + 4)
                if name is None:
                    raise UnsafePatternError("bad group name")
                out.append(pattern[i : name.end()])
                i = name.end()
            elif pattern.startswith("(?", i):
                raise UnsafePatternError(
                    "lookarounds, inline flags, atomic groups, conditionals and "
                    "backreferences are not allowed"
                )
            else:
                out.append("(")
                i += 1
            stack.append(_Group())
            last = None
            continue
        if c == ")":
            if len(stack) == 1:
                raise UnsafePatternError("unbalanced ')'")
            group = stack.pop()
            parent = stack[-1]
            parent.has_quantifier |= group.has_quantifier
            parent.has_alternation |= group.has_alternation
            out.append(")")
            last = group
            i += 1
            continue
        if c == "|":
            stack[-1].has_alternation = True
            out.append("|")
            last = None
            i += 1
            continue
        if c in "?*+{":
            if c == "{":
                brace = _BRACE.match(pattern, i)
                if brace is None:
                    raise UnsafePatternError("bad repetition (write \\{ for a literal brace)")
                low = int(brace.group(1))
                high = brace.group(3)
                if low > MAX_REPEAT or (high and int(high) > MAX_REPEAT):
                    raise UnsafePatternError(f"repetition counts above {MAX_REPEAT}")
                if high and int(high) < low:
                    raise UnsafePatternError("repetition range is reversed")
                token = brace.group(0)
                end = brace.end()
            else:
                token = c
                end = i + 1
            if last is None:
                raise UnsafePatternError("quantifier without something to repeat")
            if isinstance(last, _Group) and (last.has_quantifier or last.has_alternation):
                raise UnsafePatternError(
                    "a quantified group may not contain a quantifier or an alternation"
                )
            if end < n and pattern[end] in "?+":
                raise UnsafePatternError("lazy and possessive quantifiers are not allowed")
            quantifiers += 1
            if quantifiers > MAX_QUANTIFIERS:
                raise UnsafePatternError(f"more than {MAX_QUANTIFIERS} quantifiers")
            stack[-1].has_quantifier = True
            out.append(token + "+")
            last = None
            i = end
            continue
        if c == "]":
            raise UnsafePatternError("unbalanced ']' (escape it)")
        if c == "}":
            raise UnsafePatternError("unbalanced '}' (escape it)")
        last = None if c in "^$" else "atom"
        out.append(c)
        i += 1
    if len(stack) != 1:
        raise UnsafePatternError("unbalanced '('")
    return "".join(out)


def compile_safe(pattern: str, *, ignore_case: bool = True) -> re.Pattern[str]:
    """Compile a fingerprint pattern (see the module docstring for the rules)."""
    rewritten = rewrite_possessive(pattern)
    try:
        return re.compile(rewritten, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise UnsafePatternError(f"invalid pattern: {exc.msg}") from exc


def search_line(compiled: re.Pattern[str], line: str) -> re.Match[str] | None:
    """``compiled.search`` over ``line`` cut to :data:`MAX_SUBJECT_CHARS`."""
    return compiled.search(line[:MAX_SUBJECT_CHARS])
