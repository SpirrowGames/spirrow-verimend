# Verimend 設計ドキュメント

ステータス: 実装着手可能 / 最終更新: 2026-08-28

## 1. 目的とスコープ

Spirrow各プロダクトの実態（コード・MCPツール定義・設定・稼働サービス）とドキュメントの乖離を夜間バッチで検出し、修正案をPRとして提出する。

**スコープ内 (v1)**
- 対象: GitHub 上の Spirrow リポジトリ群の Markdown ドキュメント（README, docs/, CLAUDE.md）
  - 初期対象: `spirrow-magickit`, `spirrow-voxelworld`（順次追加前提。追加時は implementer PAT の Selected Repositories 登録がセット）
- 実態ソース: リポジトリ内のコード/設定、MCPツールスキーマ、service_health
- 出口: 対象リポジトリへのPR、Conclair chatroom へのエスカレーション

**スコープ外 (v1)**
- Prismind/Drive 上ドキュメント（v2 で擬似PRフローを検討。方針としては正本をリポジトリ側へ寄せていく）
- コードコメント/docstring の乖離検出
- ドキュメントの新規生成（既存記述の修正のみを扱う）

## 2. アーキテクチャ

```
                    systemd timer (夜間)
                          │
                          ▼
  ┌────────────── Verimend (:8118) ─────────────┐
  │                                                  │
  │  collector → extractor → reconciler → mender    │
  │      │           │            │           │      │
  └─────┼───────────┼────────────┼───────────┼──────┘
        │           │            │           │
     Magickit     Lexora       Lexora     Magickit
     (github)  (Light tier) (Light/Heavy) (github PR /
                                           chatroom)
```

- **collector**: 対象リポジトリを shallow clone / API 取得し、実態ファクト（ツールスキーマ、ポート、env変数、CLIエントリポイント等）を構造化抽出。LLM不使用
- **extractor**: ドキュメントを原子的クレームに分解（LLM）。出典アンカー（ファイルパス＋行範囲）付き
- **reconciler**: クレームごとに検索で絞ったファクトと照合。まず決定的チェッカー、次にLLM判定
- **mender**: stale クレームのパッチ草案生成、ブランチ作成、PR組み立て、エスカレーション投稿

## 3. 技術スタック（決定）

- **Python 3.11+ / FastAPI / httpx / Pydantic v2** — プラットフォーム他サービスと完全に同一構成。アダプタ層（Lexora/Prismind/Magickit クライアント）を既存実装から流用できることを最優先した。.NET/C# 案は却下（アダプタ再実装のコストが利点を上回る）
- **SQLite** — クロール実行履歴・クレーム・判定結果の永続化。単一ノード・夜陙3バッチに RDB サーバーは過剰
- **ポート :8118** — プラットフォームの連番に続く
- **systemd timer** — スケジューリング。プロセス内 cron より運用が既存ユニット群と揃う。二重起動防止の flock 付き

## 4. データモデル

```
crawl_run(id, started_at, finished_at, status, pr_url, thread_id, stats_json)
fact(id, run_id, source_kind, source_ref, content, content_hash)
  source_kind: file | tool_schema | service_health | config
claim(id, run_id, doc_repo, doc_path, anchor_start, anchor_end,
      text, claim_type, extraction_model, subject_json)
  claim_type: link | version | command | parameter | port | path |
              behavior | architecture
verdict(id, claim_id, result, method, evidence_json, patch_draft)
  result: verified | stale | unverifiable
  method: deterministic | llm
metric(run_id, prs_opened, prs_merged, prs_edited_before_merge, ...)
```

同一ドキュメントのクレームは content_hash で前回実行と突合し、未変更なら抽出をスキップ（増分クロール）。

- `subject_json`（M2a で追加）: extractor が文脈を読んだうえで出力する、クレームの構造化 subject。NULL 可。スキーマの正本は `verimend/claims.py`（Pydantic、strict・未知キー禁止）。extractor の出力スキーマと T05 の正解ラベルはこれに揃える
  - 共通: `polarity` = `current`（現在の事実として主張）| `negated`（「8080 は使わない」）| `historical`（「8080 から 9000 に変更」の 8080 側）
  - `port`: `{port: int(1-65535), service: str|null}` / `parameter`: `{tool, param}` または `{env}` / `command`: `{command}`
  - 他の claim_type の subject モデルは、それを照合するチェッカーと同じ PR で追加する
- `verdict.claim_id` は UNIQUE（1 クレーム 1 判定）。claim / verdict は `crawl_run` の削除に CASCADE で追従する
- ドキュメント単位の `content_hash` は、それを書く extractor の PR で追加する（M2a には無い）

## 5. パイプライン詳細

### 5.1 collector
- 対象は `config/targets.yaml` で宣言（リポジトリ、ドキュメント glob、ファクト抽出器の有効/無効）
- ファクト抽出器（すべて決定的）:
  - `mcp_schema`: FastMCP ツール定義を AST 解析しツール名/パラメータ/型を抽出
  - `config_keys`: Pydantic Settings / env 参照を抽出
  - `entrypoints`: pyproject / systemd unit 想定の起動コマンド
  - `ports`: リテラルポート番号の出現箇所
  - `service_health`: Magickit 経由で取得した稼働状態
- 実装メモ（T03）:
  - 読むのは git 管理下のファイルのみ。`tests/` `test/` `testing/` `fixtures/` 配下は除外（テストの偽ツール・偽ポートは実態ではない）。Markdown は ports の走査対象外
  - `fact.source_kind` への対応: `mcp_schema`→`tool_schema`、`config_keys` / `entrypoints`→`config`、`ports`→`file`。抽出器名は content JSON の `extractor` に入る
  - `content` は「何が真か」だけを持ち（正規化 JSON、`content_hash` = その SHA-256）、位置は `source_ref`（`owner/name@<commit>:<path>:<line>`）にのみ持つ。行がずれても同じファクトは同じハッシュ
  - 障害の封じ込め単位: 抽出器が例外 → その抽出器のファクトは1件も保存しない / clone 失敗 → そのリポジトリのみ欠落。どちらも run は `partial`
- 実装メモ（T04 `service_health`）:
  - 取得元は Magickit の MCP ツール `service_health`（Streamable HTTP、接続先は `VERIMEND_MAGICKIT_URL`、timeout は `VERIMEND_MAGICKIT_TIMEOUT_S`）。REST `GET /health` は使わない（3 サービスしか返さず version がハードコードのため）
  - 1 サービス 1 ファクト、`source_kind`=`service_health`。`content` は `extractor` / `service` / `status` / `url` / `available_tools`（sorted、無ければキーごと省く）のみ。`timestamp` / `response_time_ms` / `error` の文言 / 全体 `status` は毎回変わるので入れない（観測時刻は `crawl_run.started_at`）
  - `source_ref` は `magickit:service_health/<service>`。commit も target repo も含まない（稼働状態はどちらの性質でもない）
  - 実行は他の抽出器と同じ target ループの中（結果は `stats.targets[<repo>].extractors.service_health`）。clone に失敗した target では試行されない
  - `service_health` を有効にできる target は最大 1 つ（`targets.yaml` のロード時検証）。`fact` に target 列が無いため、2 つ目は同じファクトの重複挿入になる
  - 届かない / timeout / 応答不正 / `services` が空 → その target の error、ファクト 0 件、run は `partial`。URL 未設定も同じ扱い

### 5.2 extractor（LLM）
- 入力は見出し単位のチャンク（数百〜2000トークン程度）。長コンテキストに依存しない
- 出力は JSON（クレーム配列）。スキーマ違反は1回リトライ→失敗時はそのチャンクを unverifiable 扱いで記録（パイプラインは止めない）
- 意見・方針・将来計画の記述はクレーム化しない（検証不能な規範的記述の除外ルールをプロンプトに明記）

### 5.3 reconciler
- 照合順序: 決定的チェッカー（link/version/command/parameter/port/path）→ 該当しないタイプのみ LLM 判定
- 決定的チェッカーが照合するのは **構造化 subject（`subject_json`）だけ**。クレーム本文（自然言語）には正規表現を当てない。否定・対比・履歴の解釈は extractor の責務で、チェッカーは `polarity == current` の subject しか照合しない
- 実装メモ（M2a: `verimend/reconciler/`、チェッカーは `port` / `parameter` / `command`）:
  - 対象は run 内で verdict の無いクレーム。照合に使うファクトは同じ run・同じリポジトリ（`source_ref` が `<doc_repo>@` で始まるもの）だけ
  - subject の状態ごとの扱い: NULL / `polarity != current` / チェッカーが決められない → verdict を書かず `deferred`（後続の LLM 判定へ）。JSON として読めない・スキーマ違反 → その場で `unverifiable` / `deterministic` を確定（evidence `{"checker","error":"subject_schema_violation","detail"}`、`log.error`）し `invalid_subject` に数える。抽出器の故障であって「判定不能」ではないので LLM 判定には流さず、1 件の故障で run は落とさない（5.2 と同じ封じ込め）
  - stale は **矛盾するファクトが実在するときだけ**。「ファクトに見当たらない」は stale にしない（抽出器が失敗すればファクトは 0 件なので、不在は何も証明しない）
    - `port`: `ports` ファクトに同じ番号があれば verified（`match: "port_literal"` — サービスとの対応までは見ていない弱い一致）。無ければ保留
    - `parameter`（tool）: そのツールの定義のどれかに param があれば verified。stale は、そのツールの `tool_schema` ファクトがちょうど 1 件・`parameters` が NULL でない・`open_parameters == false`・`tool_expr` が無い、をすべて満たし、なお param が無いときだけ。それ以外は保留
    - `parameter`（env）: `config_keys` に一致があれば verified、無ければ保留（stale なし）
    - `command`: `entrypoints` の宣言（console script 名 / `python -m X` / `ExecStart`）と一致すれば verified（`match: "exact"`、引数付きの前方一致は `"prefix"`）。無ければ保留（stale なし）
  - evidence の形: `{"checker","fact_ids","reason"[,"match"]}`
  - run の `stats_json.reconcile` に run 全体の集計（`verified` / `stale` / `unverifiable` / `deferred` / `invalid_subject`）を表から再計算して書く。再実行は verdict の無いクレームだけを見るので重複しない
  - `mcp_schema` の content に `open_parameters`（`*args` / `**kwargs` があれば true、関数を解決できなければ null）を追加。これが無いと閉じたシグネチャと任意の名前を受け取るシグネチャが区別できない
- LLM 判定の入力: クレーム＋検索（BM25。v1 は rank-bm25 で十分、必要になったら Prismind の BGE-M3 を併用）で絞った上位ファクト最大5件
- 出力: result + 根拠ファクトID + 短い理由。根拠ファクトの提示がない stale 判定は unverifiable に降格（幻覚ガード）

### 5.4 mender
- stale のみパッチ草案を生成。アンカー範囲外の変更は生成しても棄却（diff の暴走防止）
- ブランチ: `verimend/YYYY-MM-DD`。1クロール1PR、対象リポジトリごと
- PR本文: 「決定的検証済み」「LLM判定」の2セクション。各項目にクレーム原文・根拠・判定方法を記載
- 自動適用（PRを経ない直接 commit）は v1 では実装しない。全修正が PR 経由。マージ率の実績が出た後、決定的検証済みカテゴリのみ v2 で検討
- unverifiable と大規模乖離（同一ドキュメントで stale が閾値超え）は chatroom に1スレッドでまとめて投稿

## 6. LLM 運用

- 推論はすべて **Lexora 経由（OpenAI互換 API 直接呼び出し）**。Magickit は経由しない（MCP はエージェント向けの層であり、バッチ推論のパスに挿む利点がない）
- モデル名ではなく **ティア名（Light / Heavy）で指定**。Qwen3.8-27B は両ティアとして稼働済み。モデル差し替えは Lexora 側の関心事で、Verimend は追従するだけ
- ステージ別ティア割当（config で変更可）:
  - extractor: **Light**（高頻度・定型抽出）
  - reconciler: **Light**、根拠が拮抗する場合のみ **Heavy** で再判定
  - mender（パッチ草案）: **Heavy**（件数少・品質優先）
- vLLM の直接運用・スロット管理は Verimend の関心事から除外（Lexora の責務）

## 7. 運用

- systemd: `spirrow-verimend.service`（oneshot）+ `spirrow-verimend.timer`（夜間）
- service_health の監視対象に追加（:8118 の /health）
- Magickit に薄いツール追加: `run_reconciliation`（手動トリガー）/ `get_reconciliation_status`（直近 run の結果照会）。実体は :8118 への委譲
- ログは journald。run 単位の統計は SQLite + status API

## 8. テスト戦略

- 決定的チェッカー: 通常の unit test
- LLM 工程: ゴールデンセットによる回帰評価。既知の乖離（廃止済み記述）を仕込んだドキュメント 20〜30件を正解付きで用意し、stale 検出の precision/recall を計測
- ゴールデンセットの実装メモ（T05: `eval/golden/`、採点器 `verimend/eval/golden.py`、詳細は `eval/golden/README.md`）:
  - 置き場所は `eval/golden/<case-id>/`。テスト用の偽データではなく評価資産なので `tests/` の外に置く。1 ケース = `doc.md`（評価対象。1 チャンク 2000 トークン以下）+ `facts.json`（凍結したファクト `[{key, source_kind, source_ref, content}]`）+ `gold.json`（`{claims, non_claims, injected_invalid}`）
  - ファクトは spirrow-magickit の固定 commit から M1 の抽出器で取ったもの（commit は README に記録）。実データに例の無い罠（`open_parameters` を持つツール、同名ツール 2 定義）のファクトだけ作り足し、`source_ref` を `<repo>@golden-injected:...` とする
  - 正解ラベルは 2 層:
    - `final`（`verified` / `stale` / `unverifiable`）: 凍結ファクトに照らした真の答え。extractor 込みの end-to-end 採点に使う
    - `deterministic`（`verified` / `stale` / `deferred`）: 正しい subject を受け取った M2a reconciler の期待値。oracle モードの採点に使う。`deferred` は「verdict 行がまだ無い」状態で、`verdict.result` の値ではない。チェッカーの無い種別、`polarity != current` のクレームは常に `deferred`
    - 両者は一致しなくてよい（否定文は deterministic=`deferred` / final=`verified`、`port_literal` の弱い一致は deterministic=`verified` / final=`stale`）
  - subject は `verimend.claims` で検証できる形。`port` / `parameter` / `command` は必須、モデルの無い種別は null（モデルはチェッカーと同じ PR で入る）。stale のラベルには `evidence_keys`（`facts.json` の key）が必須
  - 決定的段で `unverifiable` を期待するのは `injected_invalid`（壊れた JSON・型違い・未知キーの `raw_subject_json`）だけ。oracle にだけ流し、`unverifiable` / `subject_schema_violation` で確定し `invalid_subject` に数えられることを確かめる
  - 正解データの不備（anchor の範囲外、実在しない evidence key、モデルに通らない subject、同じ種別で anchor が重なる 2 クレーム）は `GoldenCaseError`。抽出器の故障と違って封じ込めない（壊れたラベルでの評価は無意味）
  - 採点 `score(case, predicted, layer)`: 同じ `claim_type` で anchor の行範囲が重なるものを、重なりの大きい順に貪欲に 1 対 1 で対応付ける。対応しない予測は余分な抽出（`non_claims` に重なれば誤抽出にも数える）。主指標は stale の precision / recall で、根拠の key が正解の `evidence_keys` と 1 つも重ならない stale は誤り。副指標は抽出 recall、余分な抽出、種別ごとの内訳。verdict の無い予測は `deferred`
  - oracle モード `run_oracle(case)`: 一時 DB に crawl_run・ファクト・正解 subject を持つ claim（＋ `injected_invalid`）を入れて `reconcile()` を実行し、deterministic 層で採点する。テストは全ケースで `deterministic` 欄との完全一致を要求する
  - CI で precision 閾値を gate にするかは、extractor の PR 以降に判断する
- 稼働基準: stale 判定の precision 90% 以上で夜間定期実行を有効化（それまでは手動トリガーのみ）
- メトリクス: PR マージ率・マージ前修正率を run ごとに記録し、自動化範囲拡大の判断材料とする

## 9. マイルストーン

- **M1**: collector + データモデル。対象1リポジトリ（spirrow-magickit）でファクト抽出が動く
- **M2**: extractor + reconciler。ゴールデンセットで precision/recall 計測できる。対象に spirrow-voxelworld を追加
- **M3**: mender。手動トリガーで end-to-end に PR が立つ
- **M4**: systemd 定期実行 + エスカレーション + service_health 組込み + Magickit 薄ツール

## 10. リスクと対策

| リスク | 対策 |
|---|---|
| Lexora ティアのモデル差し替え・障害 | ティア抽象に依存し特定モデルに依存しない。ステージ別割当は config のみで変更可 |
| stale 判定の幽霊（実態の読み違い） | 根拠ファクト必須 + 提示なしは unverifiable 降格 |
| PR 過多によるレビュー破産 | 1クロール1PR + 増分クロール + 閾値でまとめエスカレーション |
| 規範的記述（方針・計画）への誤検出 | extractor の除外ルール + claim_type 分類でフィルタ |
