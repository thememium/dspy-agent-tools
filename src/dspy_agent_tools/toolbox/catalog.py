"""Catalog entries and local BM25 search for :class:`Toolbox`."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from dspy import Tool

_SEARCH_DESCRIPTION_LIMIT = 300

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _matches(token: str, term: str) -> bool:
    """Match a query term at token boundaries, with cheap prefix stemming."""
    if token == term:
        return True
    short, long = (token, term) if len(token) < len(term) else (term, token)
    return len(short) >= 3 and long.startswith(short)


class CatalogEntry:
    """One catalogued tool whose definition stays out of the model context."""

    __slots__ = (
        "body_tokens",
        "description",
        "name",
        "name_tokens",
        "required",
        "schema",
        "tool",
    )

    def __init__(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        tool: Tool,
    ) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.required = [key for key in schema.get("required") or ()]
        self.tool = tool
        properties = schema.get("properties") or {}
        self.name_tokens = _tokenize(name.replace("_", " ").replace("-", " "))
        self.body_tokens = _tokenize(description) + _tokenize(" ".join(properties))


def _bm25(documents: Sequence[Sequence[str]], terms: set[str]) -> list[float]:
    """Score every document with BM25 over ``terms``."""
    lengths = [len(document) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    scores = [0.0] * len(documents)
    for term in terms:
        frequencies = [
            sum(1 for token in document if _matches(token, term))
            for document in documents
        ]
        document_frequency = sum(1 for frequency in frequencies if frequency)
        if not document_frequency:
            continue
        idf = math.log(
            1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for index, frequency in enumerate(frequencies):
            if not frequency:
                continue
            norm = 1 - 0.75 + 0.75 * (lengths[index] / average if average else 1.0)
            scores[index] += idf * frequency * 2.5 / (frequency + 1.5 * norm)
    return scores


def _search(
    entries: Sequence[CatalogEntry], query: str, limit: int
) -> list[CatalogEntry]:
    """Rank entries against ``query`` by name first, description second.

    Tool descriptions repeat their domain's vocabulary (almost every GitHub
    tool says "repository"), so scoring names and descriptions as one corpus
    lets generic description hits drown out the tool name. Names are therefore
    ranked on their own, with description/parameter hits at a lower weight.
    """
    limit = max(1, min(int(limit), 50))
    terms = set(_tokenize(query))
    if not terms:
        return list(entries[:limit])

    name_scores = _bm25([entry.name_tokens for entry in entries], terms)
    body_scores = _bm25([entry.body_tokens for entry in entries], terms)
    scored = [
        (name + 0.35 * body, entry)
        for name, body, entry in zip(name_scores, body_scores, entries)
    ]
    ranked = sorted(
        ((score, entry) for score, entry in scored if score),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    return [entry for _, entry in ranked[:limit]]


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _SEARCH_DESCRIPTION_LIMIT:
        return text
    return text[: _SEARCH_DESCRIPTION_LIMIT - 3].rstrip() + "..."


def _search_text(entries: Sequence[CatalogEntry]) -> str:
    if not entries:
        return "[no matching tools]"
    lines = []
    for index, entry in enumerate(entries, 1):
        params = f"({', '.join(entry.required)})" if entry.required else ""
        lines.append(f"{index}. {entry.name}{params}: {_shorten(entry.description)}")
    return "\n".join(lines)


def _details_text(entry: CatalogEntry) -> str:
    description = entry.description.strip()
    header = f"{entry.name}: {description}" if description else entry.name
    return f"{header}\n\nInput schema:\n{json.dumps(entry.schema, indent=2)}"


def _search_records(entries: Sequence[CatalogEntry]) -> list[dict[str, Any]]:
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "required": entry.required,
        }
        for entry in entries
    ]


def _details_record(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "required": entry.required,
        "input_schema": entry.schema,
    }


def _tool_entry(tool: Tool, name_prefix: str) -> CatalogEntry:
    """Catalog one ``dspy.Tool`` using the schema DSPy derived for it."""
    properties = tool.args if isinstance(tool.args, dict) else {}
    schema = {
        "type": "object",
        "properties": dict(properties),
        "required": [
            name
            for name, spec in properties.items()
            if not (isinstance(spec, dict) and "default" in spec)
        ],
    }
    return CatalogEntry(
        name=f"{name_prefix}{tool.name}",
        description=cast(str, tool.desc or ""),
        schema=schema,
        tool=tool,
    )
