from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from .digest import assess
from .errors import StepError
from .files import atomic_write, create_new, dump_yaml
from .infounits import PATH_SEP, parse_material
from .model import Design, Interview, Project, Unit

T = TypeVar("T", bound=BaseModel)

DRAFT_NAME = re.compile(r"^draft[\w.\-]*\.md$")


def check_draft_name(name: str) -> str:
    if not DRAFT_NAME.fullmatch(name) or name.endswith(".prompt.md"):
        raise ValueError(f"下書きのファイル名は、draft で始まり .md で終わる作業ディレクトリ直下の名前にしてください: {name!r}")
    return name


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class WorkDir:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

        self.project_file = self.root / "project.yaml"
        self.units_file = self.root / "units.yaml"
        self.raw_units_file = self.root / "units.raw.yaml"
        self.interview_file = self.root / "interview.yaml"
        self.material_dir = self.root / "material"
        self._project: tuple[tuple[int, int, int], Project] | None = None

    def _yaml(self, path: Path, step: str):
        if not path.exists():
            raise StepError(f"{path.name} がありません。先に `kumimasu {step}` を実行してください")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _load(self, path: Path, model: type[T], step: str) -> T:
        return model.model_validate(self._yaml(path, step) or {})

    @property
    def round(self) -> int:
        return self.project().round if self.project_file.exists() else 1

    def project(self) -> Project:
        try:
            st = self.project_file.stat()
        except FileNotFoundError:
            self._project = None
            return self._load(self.project_file, Project, "init")
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._project is None or self._project[0] != key:
            self._project = (key, self._load(self.project_file, Project, "init"))
        return self._project[1].model_copy(deep=True)

    def suffix(self, round_: int | None = None) -> str:
        n = self.round if round_ is None else round_
        return "" if n == 1 else f".r{n}"

    @property
    def design_file(self) -> Path:
        return self.root / f"design{self.suffix()}.yaml"

    def draft_base(self, round_: int | None = None) -> str:
        return f"draft{self.suffix(round_)}.md"

    def review_draft(self) -> str:
        name = (self.project().review_draft if self.project_file.exists() else "") or self.draft_base()
        try:
            return check_draft_name(name)
        except ValueError as e:
            raise StepError(f"project.yaml の review_draft が使えません: {e}") from None

    def material_units(self) -> list[Unit]:
        return [Unit.model_validate(u) for u in self._yaml(self.units_file, "init") or []]

    def raw_units(self) -> list[Unit]:
        if not self.raw_units_file.exists():
            return []
        return [Unit.model_validate(u) for u in self._yaml(self.raw_units_file, "init") or []]

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
        return merge_clusters(self.material_units()) + self.answer_units()

    def save_project(self, p: Project) -> None:
        atomic_write(self.project_file, dump_yaml(p.model_dump()))
        self._project = None

    def save_units(self, units: list[Unit]) -> None:
        atomic_write(self.units_file, dump_yaml([u.model_dump(exclude_defaults=True) for u in units]))

    def save_raw_units(self, units: list[Unit]) -> None:
        atomic_write(self.raw_units_file, dump_yaml([u.model_dump(exclude_defaults=True) for u in units]))

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
    wd.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    wd.material_dir.mkdir(mode=0o700, exist_ok=True)
    names: list[str] = []
    for src in materials:
        name = src.name
        while name in names:
            name = f"{len(names)}-{src.name}"
        create_new(wd.material_dir / name, Path(src).read_bytes())
        names.append(name)
    units: list[Unit] = []
    for name in names:
        for u in parse_material(wd.material_dir / name):
            units.append(Unit(id=f"m{len(units) + 1}", source=name, kind=u.kind, section=u.section,
                              path=PATH_SEP.join([name, u.path] if u.path else [name]), text=u.text))
    wd.save_project(project.model_copy(update={"materials": names, "created_at": now(),
                                               "digest": assess(wd.material_dir, names, units)}))
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
