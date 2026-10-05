# kumimasu

著者の手元の材料（メモ・ログ・コード・リンク集。まだ選んでいないもの）から、著者に短いインタビューをして、何を書いて何を書かないかを一緒に決め、LLM に一回で書かせ、設計に照らして検査し、最後に著者が一文ずつ確かめてから記事を渡すツールです。CLI の `kumimasu` と、それを回す Claude Code のスキル（プラグイン）からなります。

> **English summary.** kumimasu turns an author's raw material (notes, logs, code, links) into a blog article through a short interview, a design the author approves (what to go deep on, what to mention, what to leave out), a single one-shot draft by an LLM, checks of the draft against that design, and a final sentence-level review by the author. It ships as a Python CLI (`kumimasu`) and a Claude Code plugin whose skill drives the loop and hands control back to the author at the three human checkpoints (interview, design, final check). Prompts and UI are in Japanese. The background research lives in [kumimasu-ideas](https://github.com/vividoyomogimochi/kumimasu-ideas).

## 流れ

```
材料 ─ init / mark ─▶ インタビュー（人） ─▶ 設計（エージェントが提案 → 人が直す）
     ─▶ 一発書き（draft） ─▶ 検査（check / revise） ─▶ 最終チェック（人） ─▶ 確定した記事を渡す
```

1. **材料（`init`・`mark`）。** 材料を作業ディレクトリにコピーして単位に分け、ウェブ調査ありの一般的な記事と比べて、単位ごとに「検索で届く」「手元だけ」の目印を付けます。目印は判断の手がかりで、目標ではありません。
2. **インタビュー（人）。** 材料を名指しする短い質問を 4–6 個作ります。答えは手元の材料として加わります。
3. **設計（エージェント → 人）。** ねらい、持ち帰り、単位ごとの deep / mention / drop、書かない話題、説明しない前提、脱線、書き方のルールを提案し、著者が直して確定します。
4. **一発書き（`draft`）。** 設計と使う材料を 1 回の依頼で渡して書かせます（既定は `claude-cli:opus`、ウェブ調査あり）。節ごとに分けては書かせません。
5. **検査（`check`・`revise`）。** 設計から決まる関係（drop の単位が本文に無い、deep の単位が厚い、手元だけの材料が残る、作り話の体験や出典の無い数値が無い、メタ言説やつなぎの効用文が無い、など）を確かめます。構造の検査が落ちたときだけ、失敗から作った指示で 1 回書き直します。
6. **最終チェック（人）。** 検査で当たった文に色を付けて見せ、著者が 1 件ずつ「残す」「削る」「書き直す」を決めます。削るのは決定的に、書き直しは決めた文だけを LLM に頼みます。
7. **渡す。** 確定した記事のパスと、題・ねらい・持ち帰り・使った設定を返します。公開先への配置や投稿はツールの外の仕事で、スキルは外へ出す前に必ず本人に確認します。

人の判断が要る 3 か所（インタビュー・設計・最終チェック）は、ブラウザの画面（`kumimasu serve`）でもチャット（`answer`・`set`・`decide` など）でも受けられ、どちらも同じファイルを書きます。

## インストール

### CLI

[uv](https://docs.astral.sh/uv/) で入れます（Python 3.12 以上）。

```sh
uv tool install git+https://github.com/amane-katagiri/kumimasu
kumimasu --help
```

既定の LLM は [Claude Code](https://claude.com/claude-code) の `claude -p` を呼びます（`claude-cli:opus` / `claude-cli:sonnet`）。API を直接使うときは追加の依存を入れて、設定で `anthropic:<model>` や `openai:<model>` を選びます。

```sh
uv tool install "kumimasu[anthropic] @ git+https://github.com/amane-katagiri/kumimasu"
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
kumimasu serve work/exif-rename          # http://127.0.0.1:8792/ で質問に答えて確定する
kumimasu design work/exif-rename
# 画面で設計を直して確定する
kumimasu draft work/exif-rename
kumimasu check work/exif-rename
kumimasu confirm work/exif-rename --agent
# 画面の「最終チェック」で決めて確定する
kumimasu export work/exif-rename --to .
```

作業ディレクトリ（`project.yaml`・`units.yaml`・`interview.yaml`・`design.yaml`・`draft.md`・`check.json`・`review.draft.yaml`・`draft.final.md`・`handoff.json`・`history.jsonl` など）にすべての状態があり、どの段からでも読み直し・やり直しができます。`kumimasu show DIR` で今の段階と決められることが出ます。

## 設定

上ほど強い層で決まります。

1. コマンドの引数（`--writer`・`--judge`・`--provider`・`--port` など）
2. 作業ディレクトリの `project.yaml` の `config:`
3. 今いるディレクトリの `./kumimasu.yaml`
4. ユーザーの設定ファイル（`$KUMIMASU_CONFIG`、なければ `~/.config/kumimasu/config.yaml`）
5. 同梱の既定（[`src/kumimasu/default_config.yaml`](src/kumimasu/default_config.yaml)）

| キー | 既定 | 使う所 |
|---|---|---|
| `providers.writer` / `baseline` | `claude-cli:opus` | 下書き・書き直し / mark の一般的な記事 |
| `providers.judge` / `interviewer` / `designer` / `detector` / `rewriter` / `auto` | `claude-cli:sonnet` | 判定・質問・設計・表面の検出（`rules` なら規則だけ）・最終チェックの書き直し・auto |
| `surface.runs` / `min_votes` / `max_rounds` | 3 / 2 / 3 | 表面の検出の多数決と polish のラウンド |
| `cache_dir` | `~/.cache/kumimasu` | LLM の応答のキャッシュ |
| `rules_file` | `~/.config/kumimasu/rules.yaml` | 書き方のルールの既定を丸ごと置き換えるファイル（無ければ同梱の [`default_rules.yaml`](src/kumimasu/default_rules.yaml)） |
| `serve.port` / `serve.poll_seconds` | 8792 / 3 | 画面のポートと、画面が変更を見に行く間隔 |
| `workdir_root` | システムの一時ディレクトリの下の `kumimasu` | 「一時的な場所に置く」を選んだときの作業ディレクトリの置き場 |
| `defaults.register` / `drop_list` / `avoid` / `noise.*` / `forms` | keitai / topics / [] / 3, 2 / "" | 新しい設計の既定 |
| `interview.always_ask` | 「読者に一つだけ持ち帰ってほしいことは何ですか」 | どのインタビューにも足す質問 |

`kumimasu config [DIR]` で効いている値とその層が、`kumimasu config init --user|--project` で全キーをコメントにした雛形が出ます。ツールが設定ファイルを勝手に作ることはありません。記事で変えたルールや既定は、`kumimasu rules diff DIR` / `prefs diff DIR` で確かめ、`rules save` / `prefs save` で選んで書き戻せます。

## コマンド

| 段階 | コマンド |
|---|---|
| 材料 | `init` `mark` `interview` |
| インタビュー（人） | `answer` `confirm` |
| 設計 | `design` `noise` `review`、人の直し: `set` `rule` `confirm` |
| 下書き | `draft` `check` `revise` `polish` `confirm --agent` |
| 最終チェック（人） | `decide` `add-item` `remove-item` `apply` `confirm` |
| いつでも | `serve` `show` `wait` `export` `restart` `auto` `config` `rules` `prefs` |

詳しくは `kumimasu <command> --help` と、スキルの手順（[`skills/kumimasu/SKILL.md`](skills/kumimasu/SKILL.md)）を見てください。

## 制限

- 1 人の著者・日本語の技術ブログで作り、試しました。依頼文・画面・ルールは日本語です。
- 判定者（sonnet）と書き手（opus）は同じ系統です。網羅や割り戻しの判定は揺れます。
- 「検索で届く」の目印は、ウェブ調査ありの記事 1 本との比較で、検索で届くものが「手元だけ」に混じります。著者が `units.yaml` で直せます。
- 数値の検査は文字列の一致なので、言い換えた数値を誤って指摘します。作り話の体験の検出は、一般化した書き方を拾えません。
- 表面の検出（メタ言説・つなぎの効用文）は誤ることがあるので、自動では消さず、最終チェックで人が決めます。
- 1 本を書き上げるまでに、opus 2–3 回、sonnet 6–9 回くらいの呼び出しをします。
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
