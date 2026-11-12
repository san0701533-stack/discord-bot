---
name: ai-review
description: Claude Code と Codex CLI でコードレビュー・議論・修正を最大3往復行う。ユーザーが「Codexとレビュー」「AIレビュー」「2つのAIで確認」などと依頼したときに使う。
---

# Claude Code × Codex レビューループ

回数上限・タイムアウト・状態管理は `tools/ai-review/ai-review.sh` が強制する。このスクリプトを迂回して Codex を直接呼んでループしないこと。

## 前提チェック
1. `codex login status` が「Logged in using ChatGPT」であること。未ログインなら `codex login --device-auth` を実行し、表示された URL とコードをユーザーに伝えて**作業を止める**。API キーは使わない。
2. `git branch --show-current` が作業ブランチであること（main/master では fix モード不可）。

## 手順
1. **開始**: 対象を決めて実行する。
   - 自分の変更をレビュー: `tools/ai-review/ai-review.sh start --mode fix --base main --goal "変更の目的"`
   - 既存コードを読むだけ: `tools/ai-review/ai-review.sh start --mode review-only --files index.js`
2. **レビュー**: `tools/ai-review/ai-review.sh round`
3. **検討**: Codex の各指摘を自分でコードを読んで検証し、1件ずつ判断する。
   - 妥当 → 修正し `fixed`
   - 誤り・不要 → 根拠を具体的に書いて `rejected`
   - 既存 Bot の機能・料金・設定に影響する、仕様判断が必要、ユーザーが修正を止めている → 修正せず `deferred`（理由に「ユーザー確認が必要」等）。**これらは勝手に修正しない。**
4. **回答登録**: 回答を JSON でスクラッチパッドなどリポジトリ外に書き、`tools/ai-review/ai-review.sh respond <file>`
   ```json
   [{"finding_id": "R1-1", "decision": "fixed", "reason": "…"}]
   ```
5. 2〜4 を繰り返す。スクリプトが「セッション終了」を出したら止める（指摘ゼロ / 3往復到達 / タイムアウト / エラー）。
6. **報告**: `tools/ai-review/ai-review.sh report` の内容をもとに、ユーザーへ以下を報告する。
   - 両 AI の意見（Codex の指摘と Claude の判断・反論）
   - 採用した改善案
   - 未解決の問題と、ユーザーの判断が必要な点

## 禁止事項
- main へのマージ・プッシュ、PR 作成はユーザーの明示的な許可なしに行わない。
- 認証情報（`~/.codex/auth.json` 等）をリポジトリに入れない。
- エラーやタイムアウトを無理に回避しない。`report` とログ（`~/.cache/ai-review/<id>/round-N/codex.log`）を確認し、原因をユーザーに報告する。
- `AI_REVIEW_MAX_ROUNDS` を 3 より大きくしない（スクリプト側でも 3 に制限される）。
