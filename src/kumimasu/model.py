from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["実用", "読み物", "調査"]
Searchable = Literal["yes", "partial", "no"]
Use = Literal["deep", "mention", "drop"]
Land = Literal["bare", "author"]
FollowupState = Literal["asked", "answered", "skipped", "none"]

KINDS: tuple[str, ...] = get_args(Kind)
USES: tuple[str, ...] = get_args(Use)
LANDS: tuple[str, ...] = get_args(Land)

Stage = Literal["interview", "design", "drafting", "review", "done"]
STAGES: tuple[str, ...] = get_args(Stage)
USE_LABEL = {"deep": "掘り下げる", "mention": "触れる", "drop": "書かない"}
REGISTER_LABEL = {"keitai": "敬体（です・ます）", "joutai": "常体（だ・である）"}
SOURCES: tuple[str, ...] = ("human-ui", "agent-chat", "auto", "agent")


class DigestState(BaseModel):
    recommended: bool = False
    reasons: list[str] = []
    stats: dict = {}
    done_at: str = ""
    units_before: int = 0
    units_after: int = 0
    calls: int = 0


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
    digest: DigestState = Field(default_factory=lambda: DigestState())


class Unit(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    origin: Literal["material", "digest", "answer"] = "material"
    source: str = ""
    kind: str = "prose"
    section: str = ""
    path: str = ""
    from_units: list[str] = Field(default=[], alias="from")
    text: str
    searchable: Searchable | None = None
    found_in: list[str] = []
    cluster: str = ""
    members: list[str] = []
    context: str = ""

    @property
    def is_material(self) -> bool:
        return self.origin != "answer"

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
    promoted_for: str = ""
    land: Land | None = None
    label: str = ""
    note: str = ""
    followup: str = ""
    followup_state: FollowupState | None = None


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


class Term(BaseModel):
    term: str
    used_by: list[str] = []
    defined_by: list[str] = []
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
    terms: list[Term] = []
    note_limit: int = 5
    followup_limit: int = 2
    max_material_ratio: float = 2.0
    chars_per_mention: int = 150

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
