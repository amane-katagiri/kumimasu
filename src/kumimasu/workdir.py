from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from .payload import InfoUnit, info_units
from .model import Design, Interview, Project, Unit

T = TypeVar("T", bound=BaseModel)

PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".text", ""}
CODE_CHUNK_LINES = 40


class StepError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def dump_yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)


def code_units(text: str) -> list[tuple[str, str]]:
    lines = text.rstrip("\n").splitlines()
    out = []
    for i in range(0, len(lines), CODE_CHUNK_LINES):
        chunk = "\n".join(lines[i:i + CODE_CHUNK_LINES])
        if chunk.strip():
            out.append(("code", chunk))
    return out


def parse_material(path: Path) -> list[InfoUnit | tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in PROSE_SUFFIXES:
        return list(info_units(text))
    return code_units(text)


class WorkDir:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

        self.project_file = self.root / "project.yaml"
        self.units_file = self.root / "units.yaml"
        self.interview_file = self.root / "interview.yaml"
        self.material_dir = self.root / "material"

    def _load(self, path: Path, model: type[T], step: str) -> T:
        if not path.exists():
            raise StepError(f"{path} がありません。先に `kumimasu {step}` を実行してください")
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    @property
    def round(self) -> int:
        return self.project().round if self.project_file.exists() else 1

    def suffix(self, round_: int | None = None) -> str:
        n = self.round if round_ is None else round_
        return "" if n == 1 else f".r{n}"

    @property
    def design_file(self) -> Path:
        return self.root / f"design{self.suffix()}.yaml"

    def draft_base(self, round_: int | None = None) -> str:
        return f"draft{self.suffix(round_)}.md"

    def review_draft(self) -> str:
        """The draft the final check works on: the one the agent handed over, else this round's first draft."""
        return (self.project().review_draft if self.project_file.exists() else "") or self.draft_base()

    def project(self) -> Project:
        return self._load(self.project_file, Project, "init")

    def material_units(self) -> list[Unit]:
        if not self.units_file.exists():
            raise StepError(f"{self.units_file} がありません。先に `kumimasu init` を実行してください")
        return [Unit.model_validate(u) for u in yaml.safe_load(self.units_file.read_text(encoding="utf-8")) or []]

    def interview(self) -> Interview:
        return self._load(self.interview_file, Interview, "interview")

    def design(self) -> Design:
        return self._load(self.design_file, Design, "design")

    def answer_units(self) -> list[Unit]:
        if not self.interview_file.exists():
            return []
        return [Unit(id=q.id, origin="answer", source="interview.yaml", text=q.answer.strip(), context=q.question)
                for q in self.interview().questions if q.answer.strip()]

    def units(self) -> list[Unit]:
        """Material units with duplicate clusters merged into their representative, then the answers."""
        return merge_clusters(self.material_units()) + self.answer_units()

    def save_project(self, p: Project) -> None:
        atomic_write(self.project_file, dump_yaml(p.model_dump()))

    def save_units(self, units: list[Unit]) -> None:
        atomic_write(self.units_file, dump_yaml([u.model_dump(exclude_defaults=True) for u in units]))

    def save_interview(self, iv: Interview) -> None:
        atomic_write(self.interview_file, dump_yaml(iv.model_dump()))

    def save_design(self, d: Design) -> None:
        atomic_write(self.design_file, dump_yaml(d.model_dump()))

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        atomic_write(path, text)
        return path

    def write_json(self, name: str, data) -> Path:
        return self.write(name, json.dumps(data, ensure_ascii=False, indent=1) + "\n")

    def is_plain_file(self, name: str) -> bool:
        path = self.root / name
        return path.is_file() and not path.is_symlink() and path.resolve().parent == self.root.resolve()

    def read(self, name: str) -> str:
        path = self.root / name
        if path.is_symlink():
            raise StepError(f"{name} はシンボリックリンクなので読みません")
        if not path.exists():
            raise StepError(f"{name} がありません")
        return path.read_text(encoding="utf-8")


def init_workdir(root: Path, project: Project, materials: list[Path]) -> tuple[WorkDir, list[Unit]]:
    wd = WorkDir(root)
    if wd.project_file.exists():
        raise StepError(f"{wd.project_file} はすでにあります。別のディレクトリを指定してください")
    wd.material_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for src in materials:
        name = src.name
        while name in names:
            name = f"{len(names)}-{src.name}"
        shutil.copyfile(src, wd.material_dir / name)
        names.append(name)
    units: list[Unit] = []
    for name in names:
        for u in parse_material(wd.material_dir / name):
            kind, text, section = (u.kind, u.text, u.section) if isinstance(u, InfoUnit) else (u[0], u[1], "")
            units.append(Unit(id=f"m{len(units) + 1}", source=name, kind=kind, section=section, text=text))
    wd.save_project(project.model_copy(update={"materials": names, "created_at": now()}))
    wd.save_units(units)
    return wd, units


def merge_clusters(units: list[Unit]) -> list[Unit]:
    members: dict[str, list[Unit]] = {}
    for u in units:
        if u.cluster and u.cluster != u.id:
            members.setdefault(u.cluster, []).append(u)
    out = []
    for u in units:
        if u.cluster and u.cluster != u.id:
            continue
        ms = members.get(u.id, [])
        if ms:
            u = u.model_copy(update={"text": "\n".join([u.text, *(m.text for m in ms)]), "members": [m.id for m in ms],
                                     "found_in": sorted({*u.found_in, *(x for m in ms for x in m.found_in)})})
        out.append(u)
    return out


def as_info_units(units: list[Unit]) -> tuple[list[InfoUnit], dict[int, str]]:
    infos, ids = [], {}
    for i, u in enumerate(units, 1):
        infos.append(InfoUnit(id=i, kind=u.kind if u.kind in ("prose", "quote", "item", "row", "code") else "prose",
                              text=u.text, start=0, end=0, section=u.section))
        ids[i] = u.id
    return infos, ids
