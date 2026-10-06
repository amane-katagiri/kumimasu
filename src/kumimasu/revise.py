from __future__ import annotations

from typing import TYPE_CHECKING

from .check import READER_KIND_LABEL, CheckReport, Fetch, Votes, check, fresh_report
from .design import sync_design
from .draft import design_block, write_used
from .generate import DATA_NOTE_JA, OUTPUT_FORMAT_JA, SOURCES_RULES_JA, article_from
from .research import Research, load_research, research_block
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider


def _list(items: list[str]) -> str:
    return "\n".join(f"  - {x}" for x in items)


def instructions(rep: CheckReport, target_length: int) -> list[str]:
    out = []
    for c in rep.structural_failed():
        match c.id:
            case "drop_absent":
                out.append("次の事柄は設計で「書かない」にしたのに本文に出ています。本文から消してください。\n"
                           + _list([i["text"] for i in c.items if i.get("status") != "implied"]))
            case "deep_present":
                out.append("次の「掘り下げる材料」が本文にありません。記事の中心として書き加えてください。\n"
                           + _list([i["text"] for i in c.items]))
            case "deep_space":
                deep = [i["id"] for i in c.items if i["use"] == "deep"]
                out.append(f"掘り下げる材料（{', '.join(deep)}）に使う紙幅が、触れるだけの材料より少なくなっています。"
                           "掘り下げる材料を厚くし、触れるだけの材料は一言か短い段落に縮めてください。")
            case "firsthand_retained":
                out.append("次の著者の手元の材料が本文から落ちています。設計どおりに入れてください。\n"
                           + _list([i["text"] for i in c.items]))
            case "takeaways":
                out.append("次の持ち帰りが本文から読み取れません。読者が持ち帰れるように書いてください"
                           "（結論をまとめ直す節を足すのではなく、本文の中で）。\n"
                           + _list([i["takeaway"] for i in c.items if not i["present"]]))
            case "fabrication":
                out.append("次の箇所は、材料に無い体験を著者の体験として書いています。消すか、材料にある事実だけに直してください。\n"
                           + _list([i["text"] for i in c.items]))
            case "numbers":
                out.append("次の数値は材料に無く、出典もありません。ウェブ調査の結果に出典があればリンクを付け、無ければ消してください。\n"
                           + _list([f"{i['number']}（{i['context']}）" for i in c.items]))
            case "links":
                out.append("次のリンクは開けませんでした。ウェブ調査の結果にある出典に差し替えるか、消してください。\n"
                           + _list([i["url"] for i in c.items if i.get("verdict") in ("dead", "unreachable")]))
            case "skip_unexplained":
                out.append("次の事柄は、読者が知っている前提として説明しないと決めたのに、本文で説明しています。"
                           "説明の文を消してください（名前を出す・使うのは構いません。言い換えや前置きで補わないでください）。\n"
                           + _list([f"{i['skip']}（{' / '.join(i['evidence'])}）" for i in c.items]))
            case "aside_present":
                out.append("次の脱線が本文から落ちています。設計の場所の手がかりに入れてください。"
                           "本題との関係の説明や、役に立つ話への結びつけは付けません。\n"
                           + _list([i["text"] for i in c.items]))
            case "reader":
                out.append("次の箇所は、読者には分かりません。それぞれ、初めて出る所で一言か一文の説明を足してください"
                           "（用語なら何であるか、数字なら何を測ったか・何と比べたか、飛躍なら抜けている前提）。"
                           "説明には材料（下の「材料」があればそれ）にあることだけを使い、材料に無い事実・数値・定義は作りません。"
                           "材料から説明できないものは、その言葉や数字を使わない書き方に直してください。"
                           "説明を足した分、ほかの所を縮めて、全体の長さを保ってください。\n" + _list([reader_line(i) for i in c.items]))
            case "form":
                out.append("次の箇所は、形を変えると読みやすくなります。密度の指摘は、その段落を直し方にある形（表・番号付きリストなど）に"
                           "組み替えてください。図の指摘は、その場所に `<!-- 図: 何を示す図か -->` の目印を 1 行で置いてください"
                           "（図そのものは描きません）。どちらも事実・数値は変えず、新しい事実を足しません。\n"
                           + _list([form_line(i) for i in c.items]))
            case "length":
                out.append(f"長さが目標から外れています（{c.detail}）。全体を {target_length} 字くらいにしてください。")
    return out


def reader_line(i: dict) -> str:
    line = f"「{i['quote']}」（{READER_KIND_LABEL[i['kind']]}）: {i['why']} → {i['fix']}"
    return line + (f"\n    材料 {i['unit']}: {i['unit_text']}" if i.get("unit") else "")


def form_line(i: dict) -> str:
    return f"「{i['quote']}」（{READER_KIND_LABEL[i['kind']]}）: {i['why']} → {i['fix']}"


def revise_prompt(block: str, draft: str, todo: list[str], research: Research | None = None) -> str:
    return "\n\n".join(x for x in [
        DATA_NOTE_JA,
        ("次の下書きを、設計に照らした検査で見つかった点だけ直して、全文を書き直してください。"
         "指摘の無い所は、できるだけそのまま残してください。"),
        "# 直す点\n\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(todo, 1)),
        "# 下書き\n\n" + draft.strip(),
        "# 設計と材料（下書きを書いたときの依頼）\n\n" + block,
        research_block(research),
        SOURCES_RULES_JA,
        OUTPUT_FORMAT_JA,
    ] if x)


def revise(wd: WorkDir, writer: Provider, judge: Provider, meta: Provider | None, roles: dict[str, str], votes: Votes,
           fetch: Fetch | None, src: str, dst: str) -> tuple[CheckReport | None, list[str]]:
    rep = fresh_report(wd, src, False) or check(wd, judge, meta, src, votes, fetch)
    p, units = wd.project(), wd.units()
    d = sync_design(wd.design(), units)
    todo = instructions(rep, d.target_length)
    if not todo:
        return None, []
    prompt = revise_prompt(design_block(p, d, units), wd.read(src), todo, load_research(wd))
    wd.write(dst.replace(".md", ".prompt.md"), prompt)
    write_used(wd, dst, d, units, d.drop_list, roles | {"writer": f"{writer.name}:{writer.model}"})
    wd.write(dst, article_from(writer.complete(prompt)))
    return check(wd, judge, meta, dst, votes, fetch), todo
