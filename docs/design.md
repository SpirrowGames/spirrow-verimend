# Verimend 設計ドキュメント

ステータス: 実装着手可能 / 最終更新: 2026-10-23

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
- `verdict.method` と `evidence_json` の形:
  - `deterministic`（M2a）: `{"checker","fact_ids","reason"[,"match"]}`。subject のスキーマ違反は `{"checker","error":"subject_schema_violation","detail"}`
  - `llm`（M2c）: `{"judge":"llm","tier","fact_ids","reason"[,"downgraded":true]}`。候補ファクトが 0 件で LLM を呼ばなかったものは `{"judge":"llm","reason":"no_candidate_facts"}`（result は `unverifiable`）
- ドキュメント単位の増分抽出は実装済み（M2b、migration 0003）:
  - `document(run_id, doc_repo, doc_path, content_hash, extractor_key, chunk_hash, status)`。`status` = `extracted` | `cached` | `failed`
  - `content_hash` = 文書本文の SHA-256。`extractor_key` = プロンプトファイル・ティア・出力スキーマ版の SHA-256。`chunk_hash` = 計算したチャンク境界の SHA-256（境界の規則を変えて境界が動けば、版番号を上げなくてもキャッシュが外れる）
  - 前回までの run で、この 5 つ（`doc_repo` / `doc_path` を含む）がすべて一致し、`status` が `extracted` か `cached` の文書があれば、LLM を呼ばずにその run の claim を verdict 無しで複製する
  - キャッシュの参照元にしないもの: 失敗または打ち切りのチャンクを 1 件でも含む文書（`status = failed`）、`failed` の run、実行中の run

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
- 入力は見出し単位のチャンク（2000 トークン以下、文字数から近似: ASCII 4 文字 = 1、それ以外 1 文字 = 1）。長コンテキストに依存しない
  - 見出し（fenced code の中の `#` は見出しではない）で切り、2000 を超える節は段落の境界で、それでも超えれば行の境界で分ける。1 行だけで上限を超えるチャンクは LLM を呼ばずに `oversized_line` のチャンク失敗
  - LLM には文書の絶対行番号付きで渡し、anchor も絶対行番号で返させる
- 出力は JSON（クレーム配列 `[{anchor_start, anchor_end, text, claim_type, subject}]`）
  - 外枠の違反: JSON でない / 配列でない / キーの過不足 / 未知の `claim_type` / チャンク外の anchor / start > end
  - subject の違反: `verimend.claims.parse_subject` で通らない、モデルの無い種別で null でない
  - どちらかの違反があれば、違反の要約を添えて 1 回だけ再試行。再試行後も外枠が壊れていればチャンク失敗（`schema`、claim は 0 件）。外枠が正しく subject だけが違反している claim は生の `subject_json` のまま保存し、reconciler で `invalid_subject` になる
- 意見・方針・将来計画の記述はクレーム化しない（検証不能な規範的記述の除外ルールをプロンプト `verimend/extractor/prompt.md` に明記）
- 実装メモ（M2b: `verimend/extractor/`、パイプラインは `verimend/pipeline.py`、CLI は `verimend run`）:
  - Lexora の `/v1/chat/completions` をティア名（既定 `light`）で呼ぶ。設定は `VERIMEND_LEXORA_URL`（既定なし）、`VERIMEND_LEXORA_TIMEOUT_S`（httpx の timeout にだけ使う）、`VERIMEND_LEXORA_RETRY_AFTER_MAX_S`（既定 120、timeout とは独立）、`VERIMEND_EXTRACTOR_TIER`
  - 文書は collector と同じ checkout から読む（`targets.yaml` の `doc_globs`。`**/` は 0 個以上のディレクトリ）。claim と照合するファクトは常に同じ commit
- 失敗の区分:

| 区分 | 該当 | 扱い | 本番の run | 評価（`run_e2e`） |
|---|---|---|---|---|
| 設定の誤り | `lexora_url` 未設定（チャンクを作る前に検査）、401、403、404（OpenAI 互換 API で 404 はエンドポイントかモデル＝ティアが無いとき。要求の中身では直らない） | `ExtractorConfigError`、再試行しない | run を `failed`、`stats_json.extract.config_error`（`lexora_url_unset` / `http_401` / `http_403` / `http_404`）、reconcile しない、0 以外で終了、保存済みのファクトは残す | 例外を伝え、点数は出さない |
| 一時的 | timeout、接続エラー、5xx、429、408 | 1 回だけ再試行。429 / 503 の `Retry-After`（0 以上の整数の秒数のみ）が上限以下ならその秒数、それ以外（ヘッダ無し、HTTP-date、読めない値、他の status）は 2 秒待つ | 再試行も失敗したらチャンク失敗（`http_<status>` / `transport`）。チャンクをまたいで連続 3 回で打ち切り（`consecutive_transient_failures`）。成功したチャンクで数は 0 に戻る | 1 件でも失敗したら `ExtractorAborted` |
| 一時的・待てない | 429 / 503 で `Retry-After` > `lexora_retry_after_max_s` | 再試行しない | その場で打ち切り（`retry_after_exceeds_max`）。次のチャンクは送らない | `ExtractorAborted` |
| 恒久的 | 上記以外の 4xx（400、413、422 など）、`oversized_line`、`schema` | 再試行しない（`schema` は上の出力の再試行のみ）。一時的な失敗の連続数に入れない | チャンク失敗、run は `partial` | 点数に反映する（抽出器の品質） |
| 恒久的・HTTP の連続 | 恒久的な 4xx がチャンクをまたいで連続 3 回（知らないティアに 400 が返る場合など）。数は Lexora の呼び出しが 1 回成功したときだけ 0 に戻る（一時的な失敗が挟まっても戻らない）。`schema` / `oversized_line` は数えない。一時的な失敗の連続数とは別に数える | — | 3 回目でその場で打ち切り（`permanent_http_streak`、`status` 付き）。正しい設定で 4xx が 3 回続くのは要求の組み立てか設定が壊れている合図 | `ExtractorAborted`（設定の誤りを点数に混ぜない）。1 回だけの恒久的な失敗は従来どおり点数に反映 |

- HTTP の分類と再試行は `verimend/llm_policy.py` にあり、reconciler の LLM 判定（5.3）と共有する
- 打ち切り: 残りのチャンク（以降の文書を含む）は Lexora を呼ばずに `extractor_aborted` のチャンク失敗。`stats_json.extract.aborted` = `{reason, at_chunk: {doc_repo, doc_path, lines}[, retry_after_s][, status]}`（`reason` = `consecutive_transient_failures` / `retry_after_exceeds_max` / `permanent_http_streak`）。run は `partial` で、抽出できた分は reconcile する。打ち切られた文書は `failed` なので次の run で抽出し直される
- チャンク失敗は `stats_json.extract.failed_chunks` に `{doc_repo, doc_path, lines, error}` で記録する。`error` は `schema` / `http_<status>` / `transport` / `oversized_line` / `extractor_aborted`。偽の claim 行は入れない
- `partial` と `failed` の境界: `failed` は **抽出の段の** 設定の誤りか想定外の例外だけ。`partial` は一時的な停止・打ち切り・チャンク単位の失敗（collector の失敗と同じ封じ込め）と、reconciler の LLM 判定の段の失敗すべて（設定の誤りを含む。5.3）。どちらも 0 以外で終了する
- 評価: `python -m verimend.eval`（要 `VERIMEND_LEXORA_URL`）が golden set の各ケースを `run_e2e` に通し、deterministic 層と final 層（LLM 判定込み）で採点する。golden set が 0 件なら `no golden cases under <root>` を出して 0 以外で終了する。CI では閾値ゲートを入れない（テストは録画済みの応答でケースを通す）

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
- 実装メモ（M2c: `verimend/reconciler/llm.py`、トークン化は `verimend/reconciler/tokenize.py`、プロンプトは `verimend/reconciler/prompt.md`）:
  - 対象は決定的段の後も verdict の無いクレーム（`deferred`）だけ。`invalid_subject` と決定的段で判定済みのクレームは回らない
  - 検索: 同じ run・同じリポジトリのファクト（決定的段と同じ絞り込み）を rank-bm25 で順位付けし、スコアが 0 より大きい上位 5 件を候補にする。クエリはクレーム本文と subject の文字列値。ファクト側は content の値（文字列と数値）だけを索引し、キーは入れない。idf は負にならない式 `log(1 + (N - n + 0.5) / (n + 0.5))` に差し替える（rank-bm25 既定の idf はファクトが 1〜2 件だと一致したファクトでもスコアが負になる）。候補が 0 件なら LLM を呼ばず `unverifiable` / `llm`（evidence `{"judge":"llm","reason":"no_candidate_facts"}`）
  - トークン化（決定的、形態素解析器なし）: NFKC で正規化 → 英数字と `_` `-` `.` `/` の連なりを小文字 1 トークン（前後の `.` `-` `/` は落とす。文末の `.` で `8113.` にならないように）→ ひらがな・カタカナ（長音符・半角カナを含む）・漢字（統合漢字と拡張 A）の連なりは文字 2-gram（1 文字だけならその 1 文字）→ それ以外は区切り。クエリとファクトに同じ関数を使う
  - LLM の入力: クレーム本文、`doc_path` と anchor の行、候補（`fact_id`、kind、content の JSON を 1 件 1500 文字で切る）。出力は `{result, fact_ids, reason}`（`reason` は 300 文字で切る）。外枠の違反（JSON でない、キーの過不足、未知の `result`、型違い）は違反の要約を添えて 1 回だけ再試行し、それでも失敗したら verdict を書かずに `deferred` のまま残す（`llm_failed` に `{claim_id, error:"schema"}`）
  - 幻覚ガード: 提示していない `fact_ids` は捨てる。捨てた後で `fact_ids` が空の `stale` **と `verified`** は `unverifiable` に降格し、`evidence.downgraded = true`。根拠の無い判定はどちらの向きでも残さない
  - Lexora の失敗は 5.2 と同じ区分（`verimend/llm_policy.py`）で、単位はクレーム。**この段の失敗はどの区分でも run を `partial` にする（`failed` にはしない）**。抽出は成功しているので、`failed` にすると次の run でキャッシュが使えず全文書を抽出し直すことになる
    - 設定の誤り（401 / 403 / 404）: その場で段を止め、残りのクレームは呼ばずに `deferred`。`stats_json.reconcile.llm_config_error = {status, at_claim}`、`log.error`。決定的段の verdict と、それまでの LLM の verdict は残す
    - 一時的: 1 回だけ再試行し、失敗したらクレーム失敗（`deferred` のまま、`llm_failed`）。クレームをまたいで連続 3 回で打ち切り（`consecutive_transient_failures`）
    - 一時的・待てない: その場で打ち切り（`retry_after_exceeds_max`）
    - 恒久的な 4xx: クレーム失敗。連続 3 回で打ち切り（`permanent_http_streak`、数え方は 5.2 と同じ）
    - 打ち切り後は残りのクレームを呼ばずに `deferred` のまま残す。`stats_json.reconcile.llm_aborted = {reason, at_claim[, retry_after_s][, status]}`（`at_claim` はクレーム id）
  - verdict は 1 件ごとにコミットする。`stats_json.reconcile` に `llm_verified` / `llm_stale` / `llm_unverifiable` / `llm_downgraded`（`method = llm` の verdict から再計算）と、その回の `llm_failed`（`[{claim_id, error}]`）を足す
  - 設定: `VERIMEND_RECONCILER_TIER`（既定 `light`、空文字は Settings の検証で弾く）。Lexora が知らないティア名は 404（設定の誤り）か 4xx の連続（打ち切り）で止まる
  - 評価: `run_e2e` に strict な `JudgeSession` を渡すと final 層も採点する。設定の誤りは `JudgeConfigError`、一時的な失敗 1 件と打ち切りは `JudgeAborted` で、どちらも点数を出さない。1 回だけの恒久的な失敗（4xx、`schema`）はクレームが verdict 無しのまま final 層で誤りとして数えられる

### 5.4 mender
- stale のみパッチ草案を生成。アンカー範囲外の変更は生成しても棄却（diff の暴走防止）
  - M3a（`verimend/mender/draft.py`）: その run の `result = stale` かつ `patch_draft IS NULL` の verdict に草案を書く（再実行で作り直さない）。LLM（`VERIMEND_MENDER_TIER`、既定 `heavy`）が返すのはアンカー範囲を置き換える行 `{"lines": [...]}` だけで、unified diff は verimend が計算する。範囲外の変更は作り方の上で表現できない
  - 呼ぶ前の確認（満たさなければ LLM を呼ばず `stats_json.mend.skipped` に理由ごとに数える）: `no_evidence`（`evidence_json.fact_ids` が空でない整数のリストで、全部その run のファクト）、`doc_changed`（extractor が読んだ本文の SHA-256 がその run の `document.content_hash` と一致）、`no_anchor`（アンカーが文書の中）
  - 棄却（`stats_json.mend.rejected`）: `empty`（空・空白だけ）、`no_change`（元の行と同じ）、`runaway`（元の行数の 3 倍と 10 行の大きい方を超える）
  - Lexora の失敗は 5.3 の LLM 段と同じ区分で、単位は verdict。どの失敗も run を `partial` にする。`stats_json.mend.aborted = {reason, at_verdict[, retry_after_s][, status]}`、`config_error = {status, at_verdict}`
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
    - M2c では Heavy での再判定は未実装（Light のみ、`VERIMEND_RECONCILER_TIER`）。「拮抗」を決定的に定める材料がまだ無いので、Light での final 層の誤判定を見てから条件を決める
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
