from __future__ import annotations

import hashlib

import yaml

from .files import atomic_write, dump_yaml
from .textutil import norm
from .workdir import WorkDir

KEEP_FILE = "keep.yaml"


def text_hash(text: str) -> str:
    return hashlib.sha1(norm(text).encode()).hexdigest()[:16]


class KeepStore:
    """Sentences marked 残す, remembered by text hash across drafts and reviews."""

    def __init__(self, wd: WorkDir) -> None:
        self.path = wd.root / KEEP_FILE

    def rows(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        return {r["hash"]: r for r in yaml.safe_load(self.path.read_text(encoding="utf-8")) or []}

    def hashes(self) -> set[str]:
        return set(self.rows())

    def update(self, texts: dict[str, bool]) -> None:
        rows = self.rows()
        for text, keep in texts.items():
            h = text_hash(text)
            if keep:
                rows[h] = {"hash": h, "text": text}
            else:
                rows.pop(h, None)
        atomic_write(self.path, dump_yaml(list(rows.values())))
