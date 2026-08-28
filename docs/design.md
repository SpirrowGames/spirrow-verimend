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
      text, claim_type, extraction_model)
  claim_type: link | version | command | parameter | port | path |
              behavior | architecture
verdict(id, claim_id, result, method, evidence_json, patch_draft)
  result: verified | stale | unverifiable
  method: deterministic | llm
metric(run_id, prs_opened, prs_merged, prs_edited_before_merge, ...)
```

同一ドキュメントのクレームは content_hash で前回実行と突合し、未変更なら抽出をスキップ（増分クロール）。

## 5. パイプライン詳細

### 5.1 collector
- 対象は `config/targets.yaml` で宣言（リポジトリ、ドキュメント glob、ファクト抽出器の有効/無効）
- ファクト抽出器（すべて決定的）:
  - `mcp_schema`: FastMCP ツール定義を AST 解析しツール名/パラメータ/型を抽出
  - `config_keys`: Pydantic Settings / env 参照を抽出
  - `entrypoints`: pyproject / systemd unit 想定の起動コマンド
  - `ports`: リテラルポート番号の出現箇所
  - `service_health`: Magickit 経由で取得した稼働状態

### 5.2 extractor（LLM）
- 入力は見出し単位のチャンク（数百〜2000トークン程度）。長コンテキストに依存しない
- 出力は JSON（クレーム配列）。スキーマ違反は1回リトライ→失敗時はそのチャンクを unverifiable 扱いで記録（パイプラインは止めない）
- 意見・方針・将来計画の記述はクレーム化しない（検証不能な規範的記述の除外ルールをプロンプトに明記）

### 5.3 reconciler
- 照合順序: 決定的チェッカー（link/version/command/parameter/port/path）→ 該当しないタイプのみ LLM 判定
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
