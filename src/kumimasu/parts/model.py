from __future__ import annotations

import secrets
from collections.abc import Iterator

from pydantic import BaseModel, Field

PATH_PREFIX = {"section": "s", "heading": "h", "paragraph": "p", "list": "l", "item": "i", "table": "t", "row": "r",
               "code": "c", "quote": "q", "figure": "f", "html": "x", "rule": "hr", "raw": "raw", "front_matter": "fm"}


def new_id() -> str:
    return secrets.token_hex(4)


class Part(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: str
    level: int = 0
    text: str = ""
    cells: list[str] = []
    header: bool = False
    ordered: bool = False
    start: int = 1
    tight: bool = True
    info: str = ""
    align: list[str] = []
    lines: tuple[int, int] | None = None
    span: tuple[int, int] | None = None
    children: list[Part] = []

    def walk(self) -> Iterator[Part]:
        yield self
        for c in self.children:
            yield from c.walk()


class PartDoc(BaseModel):
    source: str
    root: Part

    def parts(self) -> Iterator[Part]:
        return self.root.walk()

    def parents(self) -> dict[str, Part]:
        out: dict[str, Part] = {}
        for p in self.parts():
            for c in p.children:
                out[c.id] = p
        return out

    def paths(self) -> dict[str, str]:
        out = {self.root.id: ""}

        def rec(p: Part, prefix: str) -> None:
            counts: dict[str, int] = {}
            for c in p.children:
                tag = PATH_PREFIX.get(c.kind, c.kind)
                counts[tag] = counts.get(tag, 0) + 1
                path = f"{prefix}.{tag}{counts[tag]}" if prefix else f"{tag}{counts[tag]}"
                out[c.id] = path
                rec(c, path)

        rec(self.root, "")
        return out
