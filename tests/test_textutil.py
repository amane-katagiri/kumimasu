from __future__ import annotations

from kumimasu.check import dash_hits
from kumimasu.factcheck import extract_urls
from kumimasu.metadiscourse import split_sentences
from kumimasu.textutil import (
    apply_edits,
    blocks,
    code_free_lines,
    excerpt,
    locate,
    sentences,
)

FENCED = """前の文です——ここは本文。

````md
```
中の文です——コードの中。https://inner.example/
```
````

~~~
チルダの中——コード。
~~~

後の文です。https://outer.example/
"""


def test_every_fence_style_is_code_everywhere():
    lines = code_free_lines(FENCED)
    assert not any("コード" in ln for ln in lines) and "後の文です。https://outer.example/" in lines
    assert dash_hits(FENCED) == ["前の文です——ここは本文。"]
    assert extract_urls(FENCED) == ["https://outer.example/"]
    assert all("コード" not in u.text for u in split_sentences(FENCED))
    code_block = next(FENCED[a:b] for a, b in blocks(FENCED) if FENCED[a:b].startswith("````"))
    assert code_block.count("````") == 2
    at = FENCED.index("中の文です")
    assert apply_edits(FENCED, [(at, at + 5, "")]) == FENCED


def test_sentences_locate_and_excerpt():
    assert sentences("「そうだ。」次の文。\n続き！") == ["「そうだ。」", "次の文。", "続き！"]
    assert locate("a **b**\n c", "a b c") == (0, 10)
    assert excerpt("a\n\n b c", 3) == "a b" and excerpt("abcdef", 3, ellipsis=True) == "abc…"
