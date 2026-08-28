# Verimend（ベリメンド）

> Verify reality, mend the docs.

Spirrow各プロダクトの実態（コード・設定・稼働サービス）とドキュメントの乖離を夜間クロールで検出し、PRとして修正案を提出するツール。

## 名前の由来

verify + mend。検証と修正を毎晩回すことで、ドキュメントが実態に漸近していく螺旋を描く。

## 動作概要

1. **実態収集** — リポジトリ、MCPツール定義、設定、service_health を決定的スクリプトで収集
2. **クレーム化** — ドキュメントを原子的な主張に分解（LLM: Qwen3.8-27B via Lexora）
3. **照合** — 各クレームを実態と突き合わせ `verified / stale / unverifiable` に分類
4. **修正** — stale はパッチ草案を生成し、1クロール1PRにまとめて提出。unverifiable と大きな乖離は chatroom へエスカレーション

## 設計原則

- **出口は検証可能性でルーティング**: 決定的チェックで裏が取れる修正のみ自動適用可、LLM判定によるものはすべてPR止まり（人間がマージ）
- **長コンテキストに頼らない**: 照合は検索で絞った小さな単位で実行する
- **周辺は既存プラットフォームに委譲**: LLM推論=Lexora / ドキュメント取得=Prismind / GitHub操作=Magickit / エスカレーション=Conclair chatroom

## ステータス

設計フェーズ。詳細は [docs/design.md](docs/design.md)。
