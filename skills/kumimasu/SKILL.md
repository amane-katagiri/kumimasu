---
name: kumimasu
description: 著者の選んでいない材料（メモ・ログ・コード・リンク集）から、インタビュー → 設計 → 一発書き → 検査 → 最終チェックの順に記事を書く。人の判断が要る 3 か所（インタビュー・設計・最終チェック）は、ブラウザの画面かチャットで受け、確定されたら次へ進む。「この材料から記事を書いて」「kumimasu で書いて」などで使う。
---

# kumimasu をエージェントとして回す

作業ディレクトリ（以下 DIR）にすべての状態がある。段階は `project.yaml` の `stage`（interview → design → drafting → review → done）。人の段階では人が決め、「確定してエージェントに渡す」（画面のボタンか `confirm`）で `handoff.json` が書かれて次の段階に進む。段階に合わない書き込みは断られる。

コマンドは `kumimasu ...`。どの LLM を使うかは設定（下の 0）で決まり、各コマンドの `--writer`・`--judge`・`--provider` などで一回だけ上書きできる。

## 0. 始める前に（エージェント）

1. `kumimasu --help` が通るか確かめる。コマンドが無ければ、本人に断ってから入れる。

   ```
   uv tool install git+https://github.com/vividoyomogimochi/kumimasu
   ```

   `uv` が無いときは、その旨を伝えて入れ方（https://docs.astral.sh/uv/ ）を案内する。勝手に別の方法で入れない。
2. `kumimasu config` で、使われる LLM（`providers.*`）・キャッシュの場所・ポートを確かめる。既定の LLM は `claude-cli:opus` と `claude-cli:sonnet`（`claude` コマンドを呼ぶ）。設定は「コマンドの引数 > 作業ディレクトリの `project.yaml` の `config:` > `./kumimasu.yaml` > ユーザーの設定ファイル（`$KUMIMASU_CONFIG`、なければ `~/.config/kumimasu/config.yaml`）> 同梱の既定」の順に効く。変えたいと言われたら、`kumimasu config init --user|--project` で雛形を作るか、該当のファイルを直す（勝手に作らない）。
3. **`init` の前に、作業ファイルをどこに置くかを本人に聞く。** 答えをもらうまで進めない。
   - (a) 今のフォルダの中（例 `./<slug>/`、または本人が言う場所）
   - (b) 一時的な既定の場所（`kumimasu config` の `workdir_root` の下、例 `<workdir_root>/<slug>`）。後で消えることがあるが、確定した記事は最後に必ず渡す、と伝える。

## 1. 材料を受け取って質問まで（エージェント）

```
kumimasu init DIR --topic "…" --audience "…" -m notes.md [-m log.txt ...] [--kind 実用|読み物|調査] [--length 5000]
kumimasu mark DIR          # providers.baseline 1 回（ウェブ調査あり）＋ providers.judge 2 回
kumimasu interview DIR     # providers.interviewer 1 回。4–6 個の質問
```

## 2. インタビュー（人）

どちらかを選んでもらう。

- 画面: `kumimasu serve DIR` をバックグラウンドで起動し、表示された URL（既定のポートは設定の `serve.port`、8792）を伝える。
- チャット: `kumimasu show DIR` の質問をそのまま聞き、答えを `kumimasu answer DIR q1 "…"` で書く。答えを作ったり要約で言い換えたりしない。最後に、本人の確認を取ってから `kumimasu confirm DIR [--note "…"]`。

確定を待つ間は `kumimasu wait DIR --for interview` をバックグラウンドで走らせる（終わると handoff の JSON が出る。`note` は本人からの指示）。

## 3. 設計（エージェント → 人）

```
kumimasu design DIR        # providers.designer 3 回（提案・前提と脱線・見直し）
```

本人に設計を見てもらう（画面か、チャットで `show` の内容を説明する）。チャットでの直し方:

```
kumimasu set DIR unit m12 --use deep|mention|drop
kumimasu set DIR takeaway 2 "…" | takeaway add "…" | takeaway rm 2
kumimasu set DIR purpose "…" | length 5000 | order "手がかり1" "手がかり2"
kumimasu set DIR skip m5 on|off [--label "…"] | aside m42 on|off [--where "…"] | avoid add "…"
kumimasu rule DIR list | on N | off N | edit N "…" | add "…" | rm N
kumimasu review DIR        # 使う単位を変えたら、出てしまう「書かない」単位の警告を作り直す
```

本人の確定は `confirm DIR`（画面ならボタン）。`wait DIR --for design` で待つ。

ルールや既定（`rules`・`prefs`・設定の `defaults:`）を設計の段階より後に変えたいと言われたら、今の設計には効かないので、`kumimasu restart DIR --from design` で新しいラウンドを始める。

## 4. 下書きと検査（エージェント）

```
kumimasu draft DIR         # providers.writer 1 回（ウェブ調査あり）
kumimasu check DIR         # providers.judge 2 回＋ providers.detector 2–3 回（surface.runs）
kumimasu revise DIR        # 構造の検査が落ちたときだけ（writer 1 回＋検査）。結果は draft.v2.md
kumimasu confirm DIR --agent [--draft draft.v2.md]
```

`--agent` の確定で最終チェックの段階に進む（画面は自動で切り替わる）。

## 5. 最終チェック（人）

画面で、または `show DIR` の項目をチャットで一つずつ見せて決めてもらう。

```
kumimasu decide DIR ITEM keep|delete|rewrite|none [--note "…"] [--result "自分の書き直し"] [--regenerate]
kumimasu add-item DIR --start N --end M [--decision delete] [--note "…"]
kumimasu confirm DIR       # 決定が反映より新しければ反映（書き直しの結果は固定のまま使う）してから完了
```

`wait DIR --for review` で待つ。返ってくる handoff の `final` が確定した記事、`article` に題・ねらい・持ち帰り・文体がある。

## 6. 渡したあと

確定した記事のパスと `article` の情報を本人に返す（`used` に、この下書きに使ったルール・書かない話題・文体・LLM がある）。ファイルとして持ち出すときは `kumimasu export DIR --to OUT`（上書きしない）。ブログの front-matter、公開先への配置や投稿は、このツールの外の仕事。**外へ出す前に、必ず本人に確認を取る。**

最後に、この記事で変えたことを次の記事の既定に戻すかを確かめる。

```
kumimasu rules diff DIR     # この記事の書き方のルールと、既定との違い（追加・削除・書き換え・オフ・オン）
kumimasu prefs diff DIR     # 文体・書かない事柄の載せ方・書かない話題・前提と脱線の数・形の好み
```

違いがあれば一覧を見せて、書き戻すか、どの範囲（user: 自分の既定、project: このフォルダの既定）か、どの項目かを本人に聞く。**答えをもらってから実行する。**

```
kumimasu rules save DIR --scope user|project [--only 1,3]
kumimasu prefs save DIR --scope user|project [--only 2,defaults.register]
```

既定そのものを直すときは `kumimasu rules list|on N|off N|add "…"|edit N "…"|rm N --scope user|project`（ファイルが無ければ同梱の既定から作り、そう伝える）。

## auto（推奨しない）

本人がいない段階をエージェントが代わりに決める。使うのは本人が頼んだときだけ。

```
kumimasu auto DIR --stage interview   # 範囲の質問だけ材料から答える。体験・感想・動機の質問は空のまま
kumimasu auto DIR --stage design      # 提案をそのまま受け入れる
kumimasu auto DIR --stage review      # 道しるべ・つなぎの効用文だけ削る。書き直しはしない
```

決めたことは `source: auto` として `history.jsonl` と `handoff.json` の `auto` に残る。本人に、どこを auto で決めたかを必ず伝える。

## やり直し

段階を戻さず、新しいラウンドを始める（材料と回答は残る。設計と下書きは `design.r2.yaml`・`draft.r2.md` のように別ファイル）。

```
kumimasu restart DIR --from interview|design|drafting
```
