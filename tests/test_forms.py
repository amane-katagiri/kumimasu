from __future__ import annotations

from conftest import (
    GOOD_DRAFT,
    PROJECT,
    SAMPLES,
    VOTES,
    always_ask,
    defaults,
    roles,
    scripted,
    serving,
)

from kumimasu import ops, rules
from kumimasu.check import check, report_text
from kumimasu.design import design, design_prompt
from kumimasu.draft import draft, draft_prompt
from kumimasu.figures import figure_markers
from kumimasu.interview import interview
from kumimasu.mark import mark
from kumimasu.metadiscourse import split_sentences
from kumimasu.model import Design, Rule
from kumimasu.polish import find_flags
from kumimasu.render import render
from kumimasu.review import load_review
from kumimasu.revise import instructions, revise
from kumimasu.show import snapshot
from kumimasu.show import text as show_text
from kumimasu.surface import rule_hints, surface_prompt
from kumimasu.workdir import WorkDir, init_workdir

FIGURE = "<!-- 図: 撮影日時の無い 9 枚を更新日時で並べる流れ -->"
CAVEAT = "標本が少ないので、これは探索的な所見です。"
FIG_DRAFT = GOOD_DRAFT.replace("それでも並び順は", f"{CAVEAT}それでも並び順は") + f"\n{FIGURE}\n"
FORM_FINDINGS = [
    {"kind": "density", "quote": "スマホの写真 312 枚のうち 9 枚", "why": "数値が詰まっている", "fix": "表にする", "unit": ""},
    {"kind": "figure", "quote": "ファイルの更新日時で代用しました", "why": "流れがつかみにくい", "fix": "代用の流れの図", "unit": ""},
]


def designed(root, p) -> WorkDir:
    w, _ = init_workdir(root, PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    mark(w, p, p)
    interview(w, p, always_ask())
    ops.confirm(w, "agent-chat")
    design(w, p, p, defaults())
    return w


def test_default_rules_encourage_structure_and_fewer_caveats():
    texts = [r.text for r in rules.default_rules()]
    assert "数値が 3 つ以上並ぶ比較は表にする" in texts and "順序のある手順は番号付きリストにする" in texts
    assert any("<!-- 図: 何を示す図か -->" in t for t in texts)
    assert any(t.startswith("限界・未確認・注意の但し書きは、結論の読み方を変えるものだけを書く") for t in texts)


def test_draft_and_design_prompts_wording():
    d = Design(forms=["m3 はコード"], rules=rules.default_rules())
    prompt = draft_prompt(PROJECT, d, [])
    forms = prompt.split("## 形\n\n", 1)[1].split("\n## ", 1)[0]
    assert "設計で決めた形（必ず使う）:\n- m3 はコード" in forms and "表や番号付きリストにして構いません" in forms
    assert "太字で始まる箇条書き" in forms and "`<!-- 図: 何を示す図か -->` の目印を 1 行で置きます" in forms
    assert "## 但し書き" in prompt and "まとめて 1 か所で" in prompt
    off = d.model_copy(update={"rules": [Rule(text=r.text, on=False) for r in d.rules]})
    bare = draft_prompt(PROJECT, off, [])
    assert "## 形" in bare and "<!-- 図:" not in bare and "## 但し書き" not in bare
    dp = design_prompt(PROJECT, [])
    assert "ここに挙げた形は書き手が必ず使います" in dp and "読み方を変えない保守的な断り" in dp and "drop にします" in dp


def test_reader_density_and_figure_go_to_revise_and_final_check(tmp_path):
    p = scripted(reader=FORM_FINDINGS)
    w = designed(tmp_path / "w", p)
    draft(w, p, roles())
    rep = check(w, p, None, "draft.md", VOTES)
    reader = next(c for c in rep.checks if c.id == "reader")
    form = next(c for c in rep.checks if c.id == "form")
    assert reader.passed is True and form.passed is False and form in rep.structural_failed()
    assert [i["kind"] for i in form.items] == ["density", "figure"]
    prompt = next(c["prompt"] for c in p.calls if "この読者になりきって" in c["prompt"])
    assert "density:" in prompt and "figure:" in prompt and "すでに `<!-- 図: … -->` の目印がある所は挙げない" in prompt
    todo = next(t for t in instructions(rep, 600) if "形を変えると読みやすく" in t)
    assert "組み替えて" in todo and "`<!-- 図: 何を示す図か -->` の目印を 1 行で置いて" in todo and "新しい事実を足しません" in todo
    assert "「スマホの写真 312 枚のうち 9 枚」（密度）: 数値が詰まっている → 表にする" in todo
    items = [i for i in load_review(w, "draft.md").items if i.kind == "form"]
    assert {i.category for i in items} == {"密度", "図"} and all(i.start is not None for i in items)
    assert "直し方: 代用の流れの図" in next(i for i in items if i.category == "図").reason
    _, done = revise(w, p, p, None, roles(), VOTES, None, "draft.md", "draft.v2.md")
    assert any("形を変えると読みやすく" in t for t in done)


def test_figure_markers_extracted_rendered_and_handed_off(tmp_path):
    figs = figure_markers(FIG_DRAFT + "\n```\n<!-- 図: コードの中 -->\n```\n")
    assert [f["text"] for f in figs] == ["撮影日時の無い 9 枚を更新日時で並べる流れ"]
    assert figs[0]["near"].startswith("`exiftool") and figs[0]["heading"] == "写真の名前を撮影日時にそろえる"
    html = render(FIG_DRAFT + "\n段落の中 <!-- 図: 小さな図 --> です。\n\n<!-- ただのコメント -->\n<script>x</script>\n")[0]
    assert 'class="fig-ph"' in html and "<b>図</b>撮影日時の無い 9 枚" in html and 'class="fig-ph inline"' in html
    assert "<script>" not in html and "&lt;!-- ただのコメント --&gt;" in html
    html = render("段落 <img src=x onerror=alert(1)> と <span style=\"display:none\">隠し</span> です。\n")[0]
    assert "<img" not in html and "<span style" not in html and '<code class="html"><span data-s="3">&lt;img src=x' in html
    p = scripted(draft_text=FIG_DRAFT)
    w = designed(tmp_path / "w", p)
    ops.confirm(w, "agent-chat")
    draft(w, p, roles())
    rep = check(w, p, None, "draft.md", VOTES)
    assert rep.figures[0]["text"] == figs[0]["text"] and not any(c.id == "figures" for c in rep.failed())
    assert "情報  図の目印 1 個（失敗ではない" in report_text(rep)
    ops.confirm(w, "agent")
    assert "図の目印（情報。後で図にする）: 撮影日時の無い 9 枚" in show_text(snapshot(w))
    with serving(w) as c:
        code, body = c.get("/api/review/draft.md")
        assert code == 200 and body["figures"][0]["text"] == figs[0]["text"] and "fig-ph" in body["html"]
    h = ops.confirm(w, "agent-chat")
    assert h["figures"] == [{"text": figs[0]["text"], "near": figs[0]["near"], "heading": figs[0]["heading"]}]


def test_conservative_caveats_are_detected_reviewed_and_polished(tmp_path):
    su = split_sentences(FIG_DRAFT)
    hints = rule_hints(su)
    assert "caveat" in hints.values()
    sp = surface_prompt(su, hints, 1)
    assert "caveat: 保守的な但し書き" in sp and "結果を読み違える" in sp and "選ばない" in sp
    p = scripted(draft_text=FIG_DRAFT)
    w = designed(tmp_path / "w", p)
    ops.confirm(w, "agent-chat")
    draft(w, p, roles())
    rep = check(w, p, p, "draft.md", VOTES)
    cav = next(c for c in rep.checks if c.id == "caveat")
    assert cav.surface and cav.passed is False and cav.items[0]["text"] == CAVEAT
    assert all(i.get("category") != "caveat" for i in next(c for c in rep.checks if c.id == "meta").items)
    item = next(i for i in load_review(w, "draft.md").items if i.kind == "caveat")
    assert item.start is not None and "読み方" in item.reason
    flags, _ = find_flags(FIG_DRAFT, FIG_DRAFT, p, [], ("caveat",), VOTES, set())
    assert [(f.rule, f.text) for f in flags] == [("caveat", CAVEAT)]
    assert find_flags(FIG_DRAFT, FIG_DRAFT, p, [], ("meta",), VOTES, set())[0] == []


BRIDGE = "そこで、コマンド一つにまとめました。"


def test_flow_bridge_is_checked_and_reviewed(tmp_path):
    text = GOOD_DRAFT.replace("`exiftool -d", f"{BRIDGE}`exiftool -d")
    p = scripted(draft_text=text)
    w = designed(tmp_path / "w", p)
    ops.confirm(w, "agent-chat")
    draft(w, p, roles())
    rep = check(w, p, p, "draft.md", VOTES)
    flow = next(c for c in rep.checks if c.id == "flow")
    assert flow.surface and flow.passed is False and flow.items[0]["category"] == "bridge" and flow.items[0]["text"] == BRIDGE
    assert all(i.get("category") != "bridge" for i in next(c for c in rep.checks if c.id == "meta").items)
    item = next(i for i in load_review(w, "draft.md").items if i.kind == "flow")
    assert item.start is not None and item.category == "bridge" and "理由づけ" in item.reason
    flags, _ = find_flags(text, text, p, [], ("flow",), VOTES, set())
    assert [(f.rule, f.text) for f in flags] == [("bridge", BRIDGE)]
