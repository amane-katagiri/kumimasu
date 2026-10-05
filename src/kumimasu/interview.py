from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .errors import StepError
from .generate import DATA_NOTE_JA
from .llm import STR, arr, ask_json, obj, rows
from .model import Interview, Project, Question, Unit
from .workdir import WorkDir, merge_clusters

if TYPE_CHECKING:
    from .llm import Provider

SEARCHABLE_LABEL = {"yes": "検索で届く", "partial": "一部は検索で届く", "no": "手元だけ", None: "未判定"}
UNIT_SHOWN = 400
QUESTIONS_MIN = 4
QUESTIONS_MAX = 6


def unit_lines(units: list[Unit], with_mark: bool = True, with_context: bool = False,
               note: Callable[[Unit], str] | None = None) -> str:
    out = []
    for u in units:
        text = u.text if len(u.text) <= UNIT_SHOWN * (1 + len(u.members)) else u.text[:UNIT_SHOWN * (1 + len(u.members))] + "…"
        if u.origin == "answer":
            tag = f"（著者の回答。問い: {u.context}）" if with_context and u.context else "（著者の回答）"
        else:
            tag = f"（{SEARCHABLE_LABEL[u.searchable]}）" if with_mark else ""
        kind = "コード" if u.kind == "code" else ""
        body = f"```\n{text}\n```" if u.kind == "code" else text
        out.append(f"[{u.id}]{note(u) if note else ''}{tag}{kind}\n{body}")
    return "\n\n".join(out)


INTERVIEW_PROMPT_JA = DATA_NOTE_JA + """

あなたは技術ブログの編集者です。著者が「{topic}」（読者: {audience}、種類: {kind}）について記事を書こうとしていて、手元の材料をまだ選ばずに全部出してくれました。材料は番号付きの単位に分けてあり、それぞれに、同じ話題でウェブを調べて書いた一般的な記事にもある情報か（検索で届く）、無い情報か（手元だけ）の目印が付いています。この目印は判定者の推定で、間違っていることもあります。

記事を書く前に著者に聞く質問を {n_min}–{n_max} 個作ってください。目的は、材料からは推測できない著者の視点を引き出すことです。何を記事に入れ、何を落とし、どこを掘り下げ、読者に何を持ち帰ってもらうかを決める手がかりになる質問にします。

質問の決まり:
- 短く、具体的に。材料の具体的な単位（番号）や事柄を名指しします。「この記事で伝えたいことは何ですか」のような一般的な質問はしません。
- 1 つの質問では 1 つのことだけを聞きます。
- 次のような質問を、材料に合うものから選んで作ります（全部を使う必要はありません）。
  - 手元だけの単位について、そこで驚いたこと・引っかかったこと・予想と違ったこと
  - 読者に 1 つだけ持ち帰ってもらうなら何か
  - 一般的な記事がどれも説明している事柄（検索で届く単位）を、この記事でも説明したいか
  - 手元だけの単位のうち、いちばん大事なのはどれか
  - あえて書かないと決めていることはあるか
  - なぜこれを作った・調べたのか（動機が材料に無いとき）
- 著者の答えを先回りして書いたり、答えの候補を並べたりしません。
- why には、その質問の答えが記事の設計のどこに効くかを 1 文で書きます。units には質問が指す単位の番号を入れます（無ければ空）。

# 材料

{units}"""


def interview_schema(n_max: int) -> dict:
    return obj(questions=arr(obj(question=STR, why=STR, units=arr(STR)), n_max))


def interview_prompt(p: Project, units: list[Unit], n_min: int = QUESTIONS_MIN, n_max: int = QUESTIONS_MAX) -> str:
    return INTERVIEW_PROMPT_JA.format(topic=p.topic, audience=p.audience, kind=p.kind, n_min=n_min, n_max=n_max,
                                      units=unit_lines(units))


def parse_questions(data: dict, units: list[Unit], n_max: int = QUESTIONS_MAX) -> Interview:
    ids = {u.id for u in units}
    qs = []
    for row in rows(data, "questions")[:n_max]:
        text = str(row.get("question", "")).strip()
        if text:
            qs.append(Question(id=f"q{len(qs) + 1}", question=text, why=str(row.get("why", "")).strip(),
                               units=[u for u in row.get("units", []) if u in ids]))
    return Interview(questions=qs)


def interview(wd: WorkDir, provider: Provider, always_ask: list[str], overwrite: bool = False) -> Interview:
    if wd.interview_file.exists() and not overwrite:
        old = wd.interview()
        if any(q.answer.strip() for q in old.questions):
            raise StepError(f"{wd.interview_file} にはもう回答があります。作り直すなら --overwrite を付けてください")
    units = merge_clusters(wd.material_units())
    data = ask_json(provider, interview_prompt(wd.project(), units), interview_schema(QUESTIONS_MAX))
    iv = with_always_ask(parse_questions(data, units), always_ask)
    wd.save_interview(iv)
    return iv


def with_always_ask(iv: Interview, always: list[str]) -> Interview:
    have = {q.question.strip() for q in iv.questions}
    qs = list(iv.questions)
    for text in always:
        if text.strip() not in have:
            qs.append(Question(id=f"q{len(qs) + 1}", question=text.strip(), why="設定の interview.always_ask"))
    return Interview(questions=qs)
