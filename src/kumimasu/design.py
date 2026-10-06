from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import StepError
from .generate import DATA_NOTE_JA
from .interview import strip_unit_refs, unit_lines
from .land import propose_land
from .llm import STR, arr, ask_json, enum, obj, rows, strings
from .model import USES, Aside, Conflict, Design, Project, Rule, Skip, Unit, UnitUse
from .terms import find_terms, promote_definitions
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider

DESIGN_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます。読者は{audience}、記事の種類は「{kind}」、長さは {length} 字くらいです。下に、著者の材料（番号付きの単位）と、編集者の質問への著者の回答があります。材料の目印「検索で届く」は、同じ話題でウェブを調べて書いた一般的な記事にもある情報、「手元だけ」はそこに無い情報です。目印は推定で、間違っていることもあります。回答の単位は [q1] のような番号です。

記事の設計を提案してください。

- purpose: この記事が読者に何をもたらすかを 1–2 文で。記事の種類（{kind}）に合わせます。
- takeaways: 読者が持ち帰るものを 1–3 個、大事な順に。それぞれ 1 文。著者の回答が「これを持ち帰ってほしい」と言っていれば、それを最優先にします。
- units: すべての単位（材料と回答）について use を決めます。
  - deep: 記事の中心として掘り下げる。著者の回答が強調したもの、著者にしか書けない手元だけの材料。全体の 1–3 割まで。
  - mention: 一言か短い段落で触れる。読者が文脈を追うのに要るもの。
  - drop: 書かない。検索で届く一般的な説明は、文脈として要らなければ drop にします。著者が書かないと言ったものも drop。
  why には理由を短く書きます。
- order: 話の順番についての緩い手がかりを 0–4 個（例:「q2 の動機から始める」）。節の一覧は作りません。
- forms: 内容が並び・手順・比較・コードのときに使う形（表・コード・番号付きリスト・箇条書き）を、どの単位に使うかと合わせて 0–4 個。ここに挙げた形は書き手が必ず使います（書き手はほかの所にも表やリストを足せます）。
- research: 記事のために、ウェブで確かめるか補うとよい一般的な事柄を 0–5 個。調査役にはこの一覧と、題・読者・ねらい・持ち帰りだけが渡り、材料と回答は渡りません。だから材料の文・著者の体験・著者の環境やプロジェクトに固有の名前は写さず、それぞれ 40 字以内の名詞句か問いにします。

限界・未確認・注意の但し書きの単位は、使う単位の主張の読み方を変えるときだけ deep か mention にし、why にどの主張の読み方をどう変えるかを書きます。読み方を変えない保守的な断り（標本が少ない、探索的、未検証の可能性など）は drop にします。

著者の回答のうち、選び方の指示（「伝えた方がいい」「絞ってよい」「いらない」など）だけで材料としての事実を含まないものは、その指示をほかの単位の use に反映し、回答そのものは drop にします。

材料に無いことは足さないでください。{forms}

# 材料と回答

{units}"""


TAKEAWAYS_MAX = 3
AVOID_MAX = 6
RESEARCH_MAX = 5
RESEARCH_CHARS = 60


def design_schema() -> dict:
    return obj(purpose=STR, takeaways=arr(STR, TAKEAWAYS_MAX), units=arr(obj(id=STR, use=enum(*USES), why=STR)),
               order=arr(STR), forms=arr(STR), research=arr(STR, RESEARCH_MAX))


def design_prompt(p: Project, units: list[Unit], forms: str = "") -> str:
    return DESIGN_PROMPT_JA.format(topic=p.topic, audience=p.audience, kind=p.kind, length=p.length,
                                   units=unit_lines(units, with_context=True),
                                   forms=f"\n\n著者の形の好み（forms の参考）: {forms}" if forms else "")


def default_use(u: Unit) -> str:
    if u.origin == "answer":
        return "mention"
    return "drop" if u.searchable == "yes" else "mention"


def parse_design(data: dict, p: Project, units: list[Unit], rules: list[Rule]) -> Design:
    chosen: dict[str, UnitUse] = {}
    ids = {u.id for u in units}
    for row in rows(data, "units"):
        uid, use = row.get("id"), row.get("use")
        if uid in ids and use in USES and uid not in chosen:
            chosen[uid] = UnitUse(id=uid, use=use, why=str(row.get("why", "")))
    uses = [chosen.get(u.id) or UnitUse(id=u.id, use=default_use(u), why="既定（提案に無かった）") for u in units]
    return Design(purpose=str(data.get("purpose", "")).strip(), kind=p.kind,
                  takeaways=[t for t in strings(data, "takeaways") if t][:TAKEAWAYS_MAX], units=uses,
                  order=strings(data, "order"), target_length=p.length, forms=strings(data, "forms"),
                  rules=[r.model_copy() for r in rules],
                  research=[t for t in strings(data, "research") if 0 < len(t) <= RESEARCH_CHARS][:RESEARCH_MAX])


@dataclass(frozen=True)
class DesignDefaults:
    register: str
    drop_list: str
    avoid: list[str]
    skip_max: int
    aside_max: int
    forms: str
    rules: list[Rule]
    max_material_ratio: float
    chars_per_mention: int


def design(wd: WorkDir, provider: Provider, judge: Provider, defaults: DesignDefaults, overwrite: bool = False) -> Design:
    if wd.design_file.exists() and not overwrite:
        raise StepError(f"{wd.design_file} はすでにあります。作り直すなら --overwrite を付けてください")
    p = wd.project()
    units = wd.units()
    if any(u.searchable is None for u in units if u.is_material):
        raise StepError("目印の無い単位があります。先に `kumimasu mark` を実行してください")
    d = parse_design(ask_json(provider, design_prompt(p, units, defaults.forms), design_schema()), p, units,
                     defaults.rules)
    d = d.model_copy(update={"formality": defaults.register, "drop_list": defaults.drop_list,
                             "form_prefs": defaults.forms, "max_material_ratio": defaults.max_material_ratio,
                             "chars_per_mention": defaults.chars_per_mention})
    d = propose_noise(d, p, units, baseline_text(wd), provider, defaults.skip_max, defaults.aside_max)
    d = promote_definitions(d.model_copy(update={"terms": find_terms(d, p, units, judge)}))
    d = propose_land(d, p, units, judge)
    d = find_conflicts(d, p, units, provider)
    d = d.model_copy(update={"avoid": merge_avoid(defaults.avoid, d.avoid)})
    wd.save_design(d)
    return d


def merge_avoid(always: list[str], proposed: list[str]) -> list[str]:
    return list(dict.fromkeys([*always, *proposed]))


def baseline_text(wd: WorkDir) -> str:
    path = wd.root / "baseline" / "W.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


NOISE_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます（読者: {audience}、種類: {kind}）。下に、同じ話題でウェブを調べて書いた一般的な記事（W）と、著者の材料と回答（番号付きの単位、いまの use 付き）があります。

人が書いた記事には、一般的な記事なら必ずある説明が抜けていたり、本題に要らない話が混じっていたりします。それは書き手が「読者はこれを知っている」「ここで自分は引っかかった」と選んだ結果です。この記事の設計にも、その選択を少しだけ入れます。

(a) skip（{skip_max} 個まで、0 個でもよい）: W のような一般的な記事が前提や背景として必ず説明していて、この記事では説明しないことにする事柄。読者は知っているか、自分で調べられるものにします。
  - 選ぶのは、目印が「検索で届く」「一部は検索で届く」の単位か、材料には無いが書き手が書き足しそうな説明です。deep の単位や、著者の手元だけの材料は選びません。
  - 抜けても読者が手順を進められるものに限ります（無いと先へ進めない説明は選びません）。
  - label は 20 字以内の名詞句。units は対応する単位の番号（無ければ空）。why は 1 文。
(b) aside（{aside_max} 個まで、0 個でもよい）: 著者自身の材料や回答のうち、著者が驚いた・引っかかった・面白がった話や、本題から外れた話で、記事に脱線として入れるもの。
  - 選ぶのは、use が mention で、目印が「手元だけ」の単位か回答の単位です。deep の単位や、著者が書かないと決めた drop の単位は選びません。
  - where には、著者がその話に出くわした場面（記事の流れのどのあたりに置くか）を 20 字以内で書きます。why は 1 文。

# W

{baseline}

# 材料と回答

{units}"""


def noise_schema(skip_max: int, aside_max: int) -> dict:
    return obj(skip=arr(obj(label=STR, units=arr(STR), why=STR), skip_max),
               aside=arr(obj(id=STR, where=STR, why=STR), aside_max))


def noise_prompt(p: Project, d: Design, units: list[Unit], baseline: str, skip_max: int,
                 aside_max: int) -> str:
    lines = unit_lines(units, with_context=True, note=lambda u: f"（use: {d.use_of(u.id)}）")
    return NOISE_PROMPT_JA.format(topic=p.topic, audience=p.audience, kind=p.kind, skip_max=skip_max,
                                  aside_max=aside_max, baseline=baseline.strip() or "（なし）", units=lines)


LABEL_MAX = 30


def parse_noise(data: dict, d: Design, units: list[Unit], skip_max: int, aside_max: int) -> Design:
    by_id = {u.id: u for u in units}
    skips, taken = [], set()
    for row in rows(data, "skip"):
        label = str(row.get("label", "")).strip()
        ids = [i for i in row.get("units", []) if i in by_id and by_id[i].is_material
               and by_id[i].searchable in ("yes", "partial") and d.use_of(i) != "deep" and i not in taken]
        if label and len(label) <= LABEL_MAX and len(skips) < skip_max:
            taken.update(ids)
            skips.append(Skip(label=label, units=ids, why=str(row.get("why", ""))))
    asides = []
    for row in rows(data, "aside"):
        i = row.get("id")
        u = by_id.get(i)
        if u and u.firsthand and d.use_of(i) == "mention" and i not in taken and len(asides) < aside_max \
                and all(a.id != i for a in asides):
            asides.append(Aside(id=i, where=str(row.get("where", ""))[:LABEL_MAX], why=str(row.get("why", ""))))
    return apply_noise(d.model_copy(update={"skip": skips, "aside": asides}))


def apply_noise(d: Design) -> Design:
    skipped = {i for s in d.skip for i in s.units}
    aside = d.aside_ids()
    units = []
    for u in d.units:
        if u.id in skipped and u.use != "drop":
            u = u.model_copy(update={"use": "drop", "why": "skip（説明しない前提）"})
        elif u.id in aside and u.use == "drop":
            u = u.model_copy(update={"use": "mention", "why": "aside（脱線）"})
        units.append(u)
    return d.model_copy(update={"units": units})


def propose_noise(d: Design, p: Project, units: list[Unit], baseline: str, provider: Provider,
                  skip_max: int, aside_max: int) -> Design:
    d = sync_design(d, units)
    data = ask_json(provider, noise_prompt(p, d, units, baseline, skip_max, aside_max), noise_schema(skip_max, aside_max))
    return parse_noise(data, d, units, skip_max, aside_max)


def noise_workdir(wd: WorkDir, provider: Provider, judge: Provider, skip_max: int, aside_max: int) -> Design:
    units = wd.units()
    p = wd.project()
    old = wd.design()
    d = propose_noise(old, p, units, baseline_text(wd), provider, skip_max, aside_max)
    d = d.model_copy(update={"terms": find_terms(d, p, units, judge)})
    d = find_conflicts(d, p, units, provider).model_copy(update={"avoid": old.avoid, "avoid_proposed": old.avoid_proposed})
    wd.save_design(d)
    return d


def land_workdir(wd: WorkDir, judge: Provider) -> Design:
    units = wd.units()
    d = propose_land(sync_design(wd.design(), units), wd.project(), units, judge)
    wd.save_design(d)
    return d


REVIEW_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます。下の「使う材料」はすべて記事に載せ、「使わない材料」は載せないと決めました。

(a) conflicts: 使わない材料のそれぞれについて、使う材料を記事に載せるだけで、その情報が記事に出てしまうか（level）を判定してください。
  - yes: 使う材料（コード・設定・手順を含む）を書けば、使わない材料の情報がほぼそのまま出る
  - partial: 一部が出る
  - no: 出ない
  by には、出てしまう原因になる使う材料の番号（最大 4 つ）を入れます。note には、記事に出てしまう内容そのものを、番号を使わずに 40 字以内で具体的に書きます（例:「exiftool の日付書式を指定するコマンド」）。著者は note だけを読んで、何が出るかを判断します。no のときは by を空にします。
(b) avoid: この話題で記事を書く人が、頼まれなくても書き足しがちで、この設計では書かないことにしたい話題の名前を 0–6 個。使わない材料から読み取れるもの（例:「専用スキャナーの価格比較」）と、話題の型として足されがちなもの（例:「FAQ」「一般的な運用の助言」）。それぞれ 20 字以内の名詞句にし、材料の文を写さないでください。

# 使う材料

{kept}

# 使わない材料

{drop}"""


def review_schema() -> dict:
    return obj(conflicts=arr(obj(id=STR, level=enum("yes", "partial", "no"), by=arr(STR), note=STR)),
               avoid=arr(STR, AVOID_MAX))


def review_prompt(p: Project, d: Design, units: list[Unit]) -> str:
    kept = [u for u in units if d.use_of(u.id) in ("deep", "mention")]
    drop = [u for u in units if d.use_of(u.id) == "drop"]
    return REVIEW_PROMPT_JA.format(topic=p.topic, kept=unit_lines(kept, with_mark=False) or "（なし）",
                                   drop=unit_lines(drop, with_mark=False) or "（なし）")


AVOID_MAX_CHARS = 30
NOTE_CHARS = 60


def parse_review(data: dict, d: Design) -> Design:
    conflicts, seen = [], set()
    for row in rows(data, "conflicts"):
        cid, level = row.get("id"), row.get("level")
        by = [b for b in row.get("by", []) if d.use_of(b) in ("deep", "mention")][:4]
        if level in ("yes", "partial") and d.use_of(cid) == "drop" and by and cid not in seen:
            seen.add(cid)
            note = strip_unit_refs(str(row.get("note", "")), set(d.unit_ids()))[:NOTE_CHARS]
            conflicts.append(Conflict(id=cid, by=by, level=level, note=note))
    avoid = [a for a in strings(data, "avoid") if 0 < len(a) <= AVOID_MAX_CHARS][:AVOID_MAX]
    return d.model_copy(update={"conflicts": conflicts, "avoid": avoid, "avoid_proposed": avoid})


def find_conflicts(d: Design, p: Project, units: list[Unit], provider: Provider) -> Design:
    d = sync_design(d, units)
    if not any(d.use_of(u.id) == "drop" for u in units):
        return d.model_copy(update={"conflicts": []})
    return parse_review(ask_json(provider, review_prompt(p, d, units), review_schema()), d)


def review_conflicts(wd: WorkDir, provider: Provider, judge: Provider) -> Design:
    units = wd.units()
    old = sync_design(wd.design(), units)
    p = wd.project()
    d = find_conflicts(old.model_copy(update={"terms": find_terms(old, p, units, judge)}), p, units, provider)
    d = d.model_copy(update={"avoid": old.avoid, "avoid_proposed": old.avoid_proposed})
    wd.save_design(d)
    return d


def sync_design(d: Design, units: list[Unit]) -> Design:
    have = {u.id for u in d.units}
    extra = [UnitUse(id=u.id, use=default_use(u), why="既定（設計の後に増えた）") for u in units if u.id not in have]
    ids = {u.id for u in units}
    return d.model_copy(update={"units": [u for u in d.units if u.id in ids] + extra,
                                "skip": [s.model_copy(update={"units": [i for i in s.units if i in ids]}) for s in d.skip],
                                "aside": [a for a in d.aside if a.id in ids]})
