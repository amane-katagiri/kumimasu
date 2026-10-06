# kumimasu

著者の手元の材料（メモ・ログ・コード・リンク集。まだ選んでいないもの）から記事を書くツールです。著者に短いインタビューをして、何を書いて何を書かないかを一緒に決め、LLM に一回で書かせ、設計に照らして検査し、最後に著者が一文ずつ確かめてから渡します。CLI の `kumimasu` と、それを回す Claude Code のスキル（プラグイン）からなります。

> English summary: kumimasu turns an author's raw material (notes, logs, code, links) into a blog article through a short interview, a design the author approves (what to go deep on, what to mention, what to leave out), a single one-shot draft by an LLM, checks of the draft against that design, and a final sentence-level review by the author. It ships as a Python CLI (`kumimasu`) and a Claude Code plugin whose skill drives the loop and hands control back to the author at the three human checkpoints (interview, design, final check). Prompts and UI are in Japanese. The background research lives in [kumimasu-ideas](https://github.com/vividoyomogimochi/kumimasu-ideas).

## 流れ

```
材料 ─ init（─ digest）/ mark ─▶ インタビュー（人） ─▶ 設計（エージェントが提案 → 人が直す）
     ─▶ 一発書き（draft） ─▶ 検査（check / revise） ─▶ 最終チェック（人） ─▶ 確定した記事を渡す
```

人の判断が要る 3 か所（インタビュー・設計・最終チェック）は、ブラウザの画面（`kumimasu serve`）でもチャット（`answer`・`set`・`decide` など）でも受けられ、どちらも同じファイルを書きます。エージェントが質問・設計・下書きを作っている間、画面は「作っています」の場面になり、次の手が要るところで自動で切り替わります。

### 1. 材料（`init`・`mark`）

材料を作業ディレクトリにコピーし、段落・箇条書きの項目・表の行・コードの塊ごとの「単位」（番号 `m1`・`m2` …）に分けます。長い段落は数文ずつに分けます。単位には、ファイルと見出しの階層（`path`、例 `notes.md › 実験 › 結果`）が付き、LLM に渡す単位の行と画面の単位の見出しに出ます。

`mark` は、ウェブ調査ありで書いた一般的な記事と比べて、単位ごとに「検索で届く」「手元だけ」の目印を付けます。目印は、何を書くかを決めるときの手がかりに使います。

材料がメモより仕上がった文書に近いと、`init`・`show`・画面が `digest`（任意）を提案します。判断の手がかりは、長い・見出しが多い・何の数値か分からない短い断片が多い・同じ見出しの下に単位が多い、の 4 つで、2 つ以上当たると提案が出ます。`kumimasu digest DIR` は見出しの節ごとに書き直し役（`providers.digester`、既定 `claude-cli:sonnet`、ツールなし）を 1 回ずつ呼び、小さい節は同じファイルの中でまとめます。書き直した単位は「何の話か・何を測ったか・何と比べたか・結果」が一つで読める自己完結したメモになります。

- 数値は意味と一緒に残します。材料に無い数値を含むメモは捨て、元の単位をそのまま使います。
- コードと表の行は書き直しません。
- 新しい単位には `origin: digest`・`from`（元の単位の番号）・`path` が付きます。元の単位は番号を `r1`・`r2` … に変えて `units.raw.yaml` に残り、画面の「元」から見られます。

`digest` のあとは、`mark`・`interview` を新しい単位で回します。12 万字・8 ファイルの研究文書で、呼び出しは 50 回ほどでした。

### 2. インタビュー（人）

材料からは分からない著者の視点を聞く短い質問を 4–6 個作ります。質問は材料を開かずに答えられる独立した文にし、単位の番号の代わりに何の話かを言い換えて含めます。拠った材料は参考として付きます。答えは手元の材料として加わります。

### 3. 設計（エージェント → 人）

エージェントが、ねらい・持ち帰り・単位ごとの扱い（deep＝掘り下げる / mention＝触れる / drop＝書かない）・書かない話題・説明しない前提・脱線・書き方のルールを提案し、著者が直して確定します。

提案のあと、判定役が使う単位から「読者が知らない用語」（材料の中で作った言葉・指標・説明の要る数値など）と、それを説明している単位を探します。説明が drop の単位にしか無ければ、いちばんよく説明している単位を mention に上げ、その用語を `promoted_for` に残して画面にも出します。その後に著者が扱いを変えたときは、自動では直さず警告だけを出します。説明が材料のどこにも無い用語は、書き手に「初出で説明する用語」として渡ります。

deep の単位のうち結果・観察・数字を運ぶものを判定役が選び（`land`）、著者に「この中で何か思ったものだけ一言」を聞きます。既定で 5 個までで、結果でない単位に付けてもかまいません。評価だけで中身の無い一言には、`kumimasu followup`（画面ではボタン）が「どのへんが？」のような聞き返しを 2 個まで付けます。答えは一言の後ろに足され、答えなくても先へ進めます。

書き手への頼み方は、一言の有無で変わります。

| 単位 | 書き手への頼み |
|---|---|
| 一言の無い結果の単位と mention の単位（bare） | 結果を述べたら、意味づけ・教訓・読者への効用を足さずに次へ進む |
| 一言のある単位 | 一言を地の文の文体で包まずに言い切る。理由や具体があれば必要な文数で書き、1 文の一言は 1 文のまま。その材料の段落の中に置き、一言の後ろを材料に無い意味づけで埋めない |

一言は手元の材料として検査に渡ります。一言から書いた文は、表面の検出（メタ言説・つなぎ・段落の運び）と `polish` の対象から外れます。

使う材料の字数が目標の字数の 2 倍を超えるか、mention の単位が目標の字数 / 150 個を超えると（どちらも既定値）、減らす候補と目標の字数の上げ幅を示します。減らす候補は、検索で届く単位と持ち帰りから離れた単位から選びます。書き手にも「全部に触れるより、説明できる数に絞る」と伝えます。

### 4. ウェブ調査と一発書き（`research`・`draft`）

設計の「ウェブで調べること」は、材料を見せない調査役（既定 `claude-cli:sonnet`、ウェブ検索あり）が調べ、出典付きの事実だけを返します。そのあと、設計・使う材料・調査結果を 1 回の依頼にまとめ、ツールを使えない書き手（既定 `claude-cli:opus`）に記事全体を書かせます。

設計の「形」は必ず使わせます。ほかの所でも、数値が並ぶ比較は表に、順序のある手順は番号付きリストにしてかまいません。書き手は図を描かず、図があると分かりやすい所に `<!-- 図: 何を示す図か -->` の目印だけを置きます。限界・未確認の但し書きは、結論の読み方を変えるものだけを 1 か所にまとめて書かせます。設計役も、読み方を変えない但し書きの単位は drop にし、残すときは why にどの主張の読み方を変えるかを書きます。ルールの全文は [`default_rules.yaml`](src/kumimasu/default_rules.yaml) にあります。

### 5. 検査（`check`・`revise`）

構造の検査は、設計から決まる関係を確かめます。drop の単位が本文に無いこと、deep の単位が厚いこと、手元だけの材料が残っていること、作り話の体験や出典の無い数値が無いこと、メタ言説やつなぎの効用文が無いこと、などです。構造の検査が落ちたときだけ、失敗から作った指示で 1 回書き直します。

ほかに、判定役が想定読者になりきって下書きを読む検査と、表面の検出があります。

| 検査 | 拾うもの | 直し方 | 最終チェックでの表示 |
|---|---|---|---|
| 読者の目（構造の検査に数える） | 説明の無い用語・何を測ったか分からない数字・前提の抜けた飛躍 | 材料にある説明だけを初出に足す | 読者に不明 |
| `form`（同じ読者が読む） | 表やリストにした方が読みやすい詰まった段落（密度）、図があると分かりやすい所（図） | 提案の形に組み替えるか、図の目印を足す | 形の提案 |
| `caveat`（表面の検出） | 標本が少ない・探索的・未検証の可能性など、結論の読み方を変えない但し書き | `polish` で消せる | 保守的な但し書き |
| `flow`（表面の検出） | 段落の頭で前の段落を受けて理由づけするだけの部分（`bridge`）、段落を解釈で結ぶだけの文（`wrapup`） | `polish` の `--rules` に `flow` を足したときだけ削る | 段落の運び |

### 6. 最終チェック（人）

検査で当たった文に色を付けて見せ、著者が 1 件ずつ「残す」「削る」「書き直す」を決めます。削るのは LLM を使わずに行い、書き直しは決めた文だけを LLM に頼みます。

### 7. 渡す

確定した記事のパスと、題・ねらい・持ち帰り・使った設定・図の目印の一覧（`figures: [{text, near, heading}]`）を返します。図の目印は `check.txt`・`show`・最終チェックの画面（枠の付いた「図」）にも出ますが、検査の失敗にはなりません。後の工程や人が、この一覧から図を描きます。公開先への配置や投稿はツールの外の仕事で、スキルは外へ出す前に必ず本人に確認します。

## インストール

### CLI

[uv](https://docs.astral.sh/uv/) で入れます（Python 3.12 以上）。

```sh
uv tool install git+https://github.com/amane-katagiri/kumimasu@v0.1.0
kumimasu --help
```

既定の LLM は [Claude Code](https://claude.com/claude-code) の `claude -p` を呼びます（`claude-cli:opus` / `claude-cli:sonnet`）。API を直接使うときは追加の依存を入れて、設定で `anthropic:<model>` や `openai:<model>` を選びます。

```sh
uv tool install "kumimasu[anthropic] @ git+https://github.com/amane-katagiri/kumimasu@v0.1.0"
```

### Claude Code のプラグイン

```
/plugin marketplace add amane-katagiri/kumimasu
/plugin install kumimasu@kumimasu
```

スキル `kumimasu` が入ります。「この材料から記事を書いて」と頼むと、エージェントが CLI を回し、人の段階で止まって確認を求めます。CLI が無ければ、スキルが上の `uv tool install` を案内します。

## はじめかた

```sh
kumimasu init work/exif-rename --topic "写真の名前を撮影日時にそろえる" --audience "スマホの写真を整理したい人" \
  -m notes.md -m rename.sh
kumimasu mark work/exif-rename
kumimasu interview work/exif-rename
kumimasu serve work/exif-rename          # 表示された http://127.0.0.1:8792/#token=… で質問に答えて確定する
kumimasu design work/exif-rename
# 画面で設計を直して確定する
kumimasu draft work/exif-rename          # ウェブ調査（調べることがあれば）→ 書き手
kumimasu check work/exif-rename
kumimasu confirm work/exif-rename --agent
# 画面の「最終チェック」で決めて確定する
kumimasu export work/exif-rename --to .
```

作業ディレクトリ（`project.yaml`・`units.yaml`・`interview.yaml`・`design.yaml`・`draft.md`・`check.json`・`review.draft.yaml`・`draft.final.md`・`handoff.json`・`history.jsonl` など）にすべての状態があり、どの段からでも読み直し・やり直しができます。今の段階と決められることは `kumimasu show DIR` で出ます。

前の段階に戻るときは、新しいラウンドを始めます（`kumimasu restart`、画面では前の段階を開いて「この段階からやり直す」）。前のラウンドのファイルはそのまま残ります。

| やり直し | 新しいラウンドで |
|---|---|
| `--from interview` | 質問と答えを引き継ぎ、答えを直す |
| `--from interview --regenerate` | 質問から作り直す（前の質問と答えは `interview.rN.yaml` に残る） |
| `--from design --keep` | 今の設計を写して直す |
| `--from design` | 設計を作り直す |
| `--from drafting` | 設計はそのまま、下書きと検査をやり直す |

やり直すと `handoffs.jsonl` に `event: "restart"` の行が入り、確定を待っている `kumimasu wait` はそれを返して終わります（確定なら `event: "handoff"`）。エージェントは `from` と `mode` を見て、作り直しなら質問・設計・下書きを作り、引き継ぎなら本人の確定を待ちます。

## 設定

上ほど強い層で決まります。

1. コマンドの引数（`--writer`・`--judge`・`--provider`・`--port` など）
2. 作業ディレクトリの `project.yaml` の `config:`（※）
3. 今いるディレクトリの `./kumimasu.yaml`（※）
4. ユーザーの設定ファイル（`$KUMIMASU_CONFIG`、なければ `~/.config/kumimasu/config.yaml`）
5. 同梱の既定（[`src/kumimasu/default_config.yaml`](src/kumimasu/default_config.yaml)）

※ この 2 つはリポジトリと一緒に届くことがあるので信頼しません。`providers.*`・`cache_dir`・`workdir_root` と、絶対パスや `..` を含む `rules_file` は、`kumimasu --trust-project …`（または `KUMIMASU_TRUST_PROJECT=1`）のときだけ使い、そうでなければ警告を出して無視します。`defaults.forms` などのほかの値は効き、設計の画面と `show` に出ます。

| キー | 既定 | 使う所 |
|---|---|---|
| `providers.writer` / `baseline` | `claude-cli:opus` | 下書き・書き直し（ツールなし） / mark の一般的な記事（ウェブ調査あり、送るのは題と読者だけ） |
| `providers.researcher` | `claude-cli:sonnet` | 下書きの前のウェブ調査（送るのは題・読者・ねらい・持ち帰り・調べることだけ） |
| `providers.judge` / `interviewer` / `designer` / `detector` / `rewriter` / `auto` | `claude-cli:sonnet` | 判定・質問・設計・表面の検出（`rules` なら規則だけ）・最終チェックの書き直し・auto |
| `providers.digester` | `claude-cli:sonnet` | `digest` で仕上がった文書を自己完結したメモに書き直す（ツールなし） |
| `surface.runs` / `min_votes` / `max_rounds` | 3 / 2 / 3 | 表面の検出の多数決と polish のラウンド |
| `cache_dir` | `~/.cache/kumimasu` | LLM の応答のキャッシュ |
| `rules_file` | `~/.config/kumimasu/rules.yaml` | 書き方のルールの既定を丸ごと置き換えるファイル（無ければ同梱の [`default_rules.yaml`](src/kumimasu/default_rules.yaml)） |
| `serve.port` / `serve.poll_seconds` | 8792 / 3 | 画面のポートと、画面が変更を見に行く間隔 |
| `workdir_root` | `~/.cache/kumimasu/work` | 既定の場所を選んだときの作業ディレクトリの置き場（700 で作り、ほかのユーザーが書ける場所は使わない） |
| `defaults.register` / `drop_list` / `avoid` / `noise.*` / `forms` | keitai / topics / [] / 3, 2 / "" | 新しい設計の既定 |
| `defaults.max_material_ratio` / `chars_per_mention` | 2.0 / 150 | 材料が多すぎる警告: 使う材料の字数が目標の何倍を超えたら、触れる単位 1 個あたり何字を目安にするか（新しい設計に写る） |
| `interview.always_ask` | 「読者に一つだけ持ち帰ってほしいことは何ですか」 | どのインタビューにも足す質問 |

`kumimasu config [DIR]` で効いている値とその層が、`kumimasu config init --user|--project` で全キーをコメントにした雛形が出ます。ツールが設定ファイルを勝手に作ることはありません。記事で変えたルールや既定は、`kumimasu rules diff DIR` / `prefs diff DIR` で確かめ、`rules save` / `prefs save` で選んで書き戻せます。

## コマンド

| 段階 | コマンド |
|---|---|
| 材料 | `init` `digest`（任意） `mark` `interview` |
| インタビュー（人） | `answer` `confirm` |
| 設計 | `design` `noise` `land` `followup` `review`、人の直し: `set`（一言は `set DIR note ID "…"`、聞き返しへの答えは `set DIR followup ID "…"`） `rule` `confirm` |
| 下書き | `research` `draft` `check` `revise` `polish` `confirm --agent` |
| 最終チェック（人） | `decide` `add-item` `remove-item` `apply` `confirm` |
| いつでも | `serve` `show` `wait` `export` `restart` `auto` `config` `rules` `prefs` |

詳しくは `kumimasu <command> --help` と、スキルの手順（[`skills/kumimasu/SKILL.md`](skills/kumimasu/SKILL.md)）を見てください。

## 安全のための仕組み

画面（`serve`）は 127.0.0.1 だけで待ち受け、起動時に表示する `#token=…` 付きの URL からしか使えません。材料や回答を見る LLM にはツールを渡さず、ウェブ調査は材料を見ない役だけが行います。作業ディレクトリと LLM のキャッシュは本人だけが読めるように作ります。

## 制限

- 1 人の著者・日本語の技術ブログで作り、試しました。依頼文・画面・ルールは日本語です。
- 判定者（sonnet）と書き手（opus）は同じ系統です。網羅や割り戻しの判定は揺れます。
- 「検索で届く」の目印は、ウェブ調査ありの記事 1 本との比較で、検索で届くものが「手元だけ」に混じります。著者が `units.yaml` で直せます。
- 数値の検査は文字列の一致なので、言い換えた数値を誤って指摘します。作り話の体験の検出は、一般化した書き方を拾えません。
- 表面の検出（メタ言説・つなぎの効用文・段落の運び）は誤ることがあるので、自動では消さず、最終チェックで人が決めます。
- 1 本を書き上げるまでに、opus 2–3 回、sonnet 9–13 回くらいの呼び出しをします（`digest` を使うと、節の数に応じて sonnet が数十回増えます）。設計の用語探しは、使わない材料が多いと増えます（使う材料と合わせて 6 万字ごとに 1 回）。
- 読者が知らない用語の判定と、読者の目の検査は LLM の推定です。読者が知っている言葉を挙げることも、知らない言葉を見落とすこともあります。
- `auto`（人の段階をエージェントが代わりに決める）は推奨しません。体験・感想・動機の質問には答えません。

## 背景

このループは、AI の書いた長文の何が読みにくいか、人の記事らしさはどこから来るかを調べた実験から作りました。調べたことと、ループを作った経緯は [kumimasu-ideas](https://github.com/vividoyomogimochi/kumimasu-ideas) にあります。

## 開発

```sh
uv sync
uv run pytest        # 偽の LLM（fake）だけを使い、実際の呼び出しはしません
uv run kumimasu --help
```

## ライセンス

MIT（[LICENSE](LICENSE)）
