from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

Kind = Literal["実用", "読み物", "調査"]
Searchable = Literal["yes", "partial", "no"]
Use = Literal["deep", "mention", "drop"]

KINDS: tuple[str, ...] = get_args(Kind)
USES: tuple[str, ...] = get_args(Use)

Stage = Literal["interview", "design", "drafting", "review", "done"]
STAGES: tuple[str, ...] = get_args(Stage)
USE_LABEL = {"deep": "掘り下げる", "mention": "触れる", "drop": "書かない"}
REGISTER_LABEL = {"keitai": "敬体（です・ます）", "joutai": "常体（だ・である）"}
SOURCES: tuple[str, ...] = ("human-ui", "agent-chat", "auto", "agent")


class Project(BaseModel):
    topic: str
    audience: str
    kind: Kind = "実用"
    length: int = 4000
    materials: list[str] = []
    created_at: str = ""
    stage: Stage = "interview"
    round: int = 1
    review_draft: str = ""
    config: dict = {}


class Unit(BaseModel):
    id: str
    origin: Literal["material", "answer"] = "material"
    source: str = ""
    kind: str = "prose"
    section: str = ""
    text: str
    searchable: Searchable | None = None
    found_in: list[str] = []
    cluster: str = ""
    members: list[str] = []
    context: str = ""

    @property
    def firsthand(self) -> bool:
        return self.origin == "answer" or self.searchable in ("no", "partial")


class Question(BaseModel):
    id: str
    question: str
    context: str = ""
    why: str = ""
    units: list[str] = []
    answer: str = ""
    source: str = ""


class Interview(BaseModel):
    questions: list[Question] = []


class UnitUse(BaseModel):
    id: str
    use: Use
    why: str = ""


class Rule(BaseModel):
    text: str
    on: bool = True


DropList = Literal["topics", "full", "none"]


class Conflict(BaseModel):
    id: str
    by: list[str]
    level: Literal["yes", "partial"]
    note: str = ""

    def message(self) -> str:
        how = "出る" if self.level == "yes" else "一部出る"
        what = f"「{self.note}」" if self.note else "書かないにした内容"
        return f"{what}が{how}（原因: {'・'.join(self.by)} / 書かない側: {self.id}）"


class Skip(BaseModel):
    label: str
    units: list[str] = []
    why: str = ""


class Aside(BaseModel):
    id: str
    where: str = ""
    why: str = ""


class Design(BaseModel):
    purpose: str = ""
    kind: Kind = "実用"
    takeaways: list[str] = Field(default_factory=list)
    units: list[UnitUse] = []
    order: list[str] = []
    formality: Literal["keitai", "joutai"] = "keitai"
    target_length: int = 4000
    forms: list[str] = []
    form_prefs: str = ""
    rules: list[Rule] = []
    avoid: list[str] = []
    avoid_proposed: list[str] = []
    research: list[str] = []
    drop_list: DropList = "topics"
    conflicts: list[Conflict] = []
    skip: list[Skip] = []
    aside: list[Aside] = []

    def active_rules(self) -> list[str]:
        return [r.text for r in self.rules if r.on and r.text.strip()]

    def aside_ids(self) -> set[str]:
        return {a.id for a in self.aside}

    def unit_ids(self) -> list[str]:
        return [u.id for u in self.units]

    def use_of(self, unit_id: str) -> Use | None:
        return next((u.use for u in self.units if u.id == unit_id), None)

    def live_conflicts(self) -> list[Conflict]:
        """Uses may change after review."""
        out = []
        for c in self.conflicts:
            by = [b for b in c.by if self.use_of(b) in ("deep", "mention")]
            if self.use_of(c.id) == "drop" and by:
                out.append(c.model_copy(update={"by": by}))
        return out
