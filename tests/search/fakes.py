"""Fake ddgs client shared by the search tests."""

import types
from typing import ClassVar

DDG_REDIRECT = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal&rut=abc"


class FakeDDGS:
    calls: ClassVar[list[tuple[str, dict]]] = []
    timeouts: ClassVar[list[int | None]] = []

    def __init__(self, timeout=None):
        FakeDDGS.timeouts.append(timeout)

    def text(self, query, **kwargs):
        FakeDDGS.calls.append(("text", {"query": query, **kwargs}))
        return [
            {"title": "Result One", "href": DDG_REDIRECT, "body": "Body one"},
            {
                "title": "Result Two",
                "href": "https://plain.example.com",
                "body": "Body two",
            },
            {"href": "https://sparse.example.com"},  # missing keys -> defaults
        ]

    def news(self, query, **kwargs):
        FakeDDGS.calls.append(("news", {"query": query, **kwargs}))
        return [
            {
                "title": "News One",
                "url": DDG_REDIRECT,
                "body": "News body",
                "source": "Example",
                "date": "2026-10-08",
            },
            {"url": "https://plain.example.com"},  # missing keys -> defaults
        ]


class FakeDdgsModule(types.ModuleType):
    DDGS = FakeDDGS
