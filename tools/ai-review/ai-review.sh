#!/usr/bin/env bash
# Claude Code <-> Codex レビュー往復の管理スクリプト。
# 回数上限・タイムアウト・状態管理はこのスクリプトが強制する（AI の判断に任せない）。
# Codex は常に読み取り専用サンドボックスで実行され、コードを変更するのは Claude Code のみ。
# このスクリプトはコミット・プッシュ・PR 作成を一切行わない。
#
# 使い方:
#   ai-review.sh start [--mode review-only|fix] [--base <ref>] [--files f1 f2 ...] [--goal "説明"]
#   ai-review.sh round                 # Codex にレビューさせる（次の往復）
#   ai-review.sh respond <file.json>   # 直近の指摘への Claude の回答を登録
#   ai-review.sh status
#   ai-review.sh report                # 最終レポート（Markdown）を出力
#   ai-review.sh abort
#
# 環境変数（任意）:
#   AI_REVIEW_MAX_ROUNDS   最大往復数（既定 3、上限 3）
#   AI_REVIEW_TIMEOUT      Codex 1 回あたりの秒数（既定 600）
#   AI_REVIEW_DEADLINE     セッション全体の秒数（既定 3600）
#   AI_REVIEW_STATE        状態保存先（既定 ~/.cache/ai-review。リポジトリ外）

set -euo pipefail

HARD_MAX_ROUNDS=3
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="$SCRIPT_DIR/schema.json"
INSTRUCTIONS="$SCRIPT_DIR/codex-instructions.md"
STATE_ROOT="${AI_REVIEW_STATE:-$HOME/.cache/ai-review}"
MAX_DIFF_BYTES=200000

die() { echo "ai-review: エラー: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 が見つかりません"; }
now() { date +%s; }

need jq; need git; need timeout; need codex

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || die "git リポジトリ内で実行してください"
cd "$REPO"

session_dir() {
  [ -L "$STATE_ROOT/current" ] || die "実行中のセッションがありません（start を先に実行）"
  readlink -f "$STATE_ROOT/current"
}
meta_get() { jq -r "$1" "$SESSION/meta.json"; }
meta_set() { local tmp; tmp="$(mktemp)"; jq "$@" "$SESSION/meta.json" >"$tmp" && mv "$tmp" "$SESSION/meta.json"; }
fingerprint() { { git rev-parse HEAD; git status --porcelain=v1 --untracked-files=all; git diff HEAD; } | sha256sum | cut -d' ' -f1; }

cmd_start() {
  local mode="review-only" base="" goal="" files=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --mode) mode="$2"; shift 2 ;;
      --base) base="$2"; shift 2 ;;
      --goal) goal="$2"; shift 2 ;;
      --files) shift; while [ $# -gt 0 ] && [[ "$1" != --* ]]; do files+=("$1"); shift; done ;;
      *) die "不明な引数: $1" ;;
    esac
  done
  [[ "$mode" == "review-only" || "$mode" == "fix" ]] || die "--mode は review-only か fix"
  [ -n "$base" ] || [ ${#files[@]} -gt 0 ] || die "--base か --files のどちらかを指定してください"
  [ -z "$base" ] || git rev-parse --verify -q "$base^{commit}" >/dev/null || die "ベース $base が見つかりません"
  for f in "${files[@]}"; do [ -e "$f" ] || die "ファイルがありません: $f"; done

  local branch; branch="$(git branch --show-current)"
  if [ "$mode" = "fix" ] && [[ "$branch" =~ ^(main|master)$ || -z "$branch" ]]; then
    die "fix モードは main/master や detached HEAD では実行できません（作業ブランチで実行）"
  fi
  if [ -L "$STATE_ROOT/current" ]; then
    local cur; cur="$(readlink -f "$STATE_ROOT/current")"
    if [ "$(jq -r .status "$cur/meta.json")" = "active" ]; then
      die "実行中のセッションがあります（report で確認後、abort してください）: $cur"
    fi
  fi

  local max="${AI_REVIEW_MAX_ROUNDS:-$HARD_MAX_ROUNDS}"
  [[ "$max" =~ ^[1-9][0-9]*$ ]] || die "AI_REVIEW_MAX_ROUNDS が不正です"
  [ "$max" -le "$HARD_MAX_ROUNDS" ] || max=$HARD_MAX_ROUNDS
  local deadline=$(( $(now) + ${AI_REVIEW_DEADLINE:-3600} ))

  local id; id="$(date +%Y%m%d-%H%M%S)-$$"
  SESSION="$STATE_ROOT/$id"
  [ ! -e "$SESSION" ] || die "セッション ID が重複しました。再実行してください"
  mkdir -p "$SESSION"; chmod 700 "$STATE_ROOT" "$SESSION"
  jq -n --arg id "$id" --arg mode "$mode" --arg base "$base" --arg goal "$goal" \
        --arg branch "$branch" --arg fp "$(fingerprint)" \
        --argjson max "$max" --argjson deadline "$deadline" --argjson started "$(now)" \
        --args '{id:$id, mode:$mode, base:$base, goal:$goal, branch:$branch, files:$ARGS.positional,
                 max_rounds:$max, round:0, status:"active", started:$started, deadline:$deadline,
                 fingerprint:$fp, end_reason:""}' "${files[@]}" >"$SESSION/meta.json"
  ln -sfn "$SESSION" "$STATE_ROOT/current"
  echo "セッション開始: $id（モード: $mode / 最大 $max 往復 / 全体期限 $(date -d @"$deadline" '+%H:%M:%S')）"
}

finish() { meta_set --arg s "$1" --arg r "$2" '.status=$s | .end_reason=$r'; echo "セッション終了: $2"; }

build_prompt() {
  local n="$1"
  cat "$INSTRUCTIONS"
  echo
  echo "## セッション情報"
  echo "- 往復: $n / $(meta_get .max_rounds)（この回が最後なら、本当に重要な指摘だけに絞ること）"
  echo "- モード: $(meta_get .mode)（review-only の場合、Claude はコードを修正せず保留・却下のみ行う）"
  [ -z "$(meta_get .goal)" ] || echo "- 変更の目的: $(meta_get .goal)"
  echo
  local base; base="$(meta_get .base)"
  local -a files
  mapfile -t files < <(meta_get '.files[]')
  if [ ${#files[@]} -gt 0 ]; then
    echo "## レビュー対象ファイル（リポジトリ内で直接読んでください）"
    printf -- '- %s\n' "${files[@]}"
    echo
  fi
  if [ -n "$base" ]; then
    echo "## 差分（$base からの変更。作業ツリーの未コミット分を含む）"
    echo '```diff'
    git diff "$base" -- "${files[@]}" | head -c "$MAX_DIFF_BYTES"
    echo '```'
    echo
  fi
  if [ "$n" -gt 1 ]; then
    echo "## これまでのやり取り"
    for ((i = 1; i < n; i++)); do
      echo "### 往復 $i: あなた（Codex）の指摘"
      echo '```json'; jq '.findings' "$SESSION/round-$i/codex.json"; echo '```'
      echo "### 往復 $i: Claude Code の回答（fixed=修正済み / rejected=却下 / deferred=保留）"
      echo '```json'; cat "$SESSION/round-$i/claude.json"; echo '```'
    done
  fi
}

cmd_round() {
  SESSION="$(session_dir)"
  [ "$(meta_get .status)" = "active" ] || die "セッションは終了しています（$(meta_get .end_reason)）"
  local round max; round="$(meta_get .round)"; max="$(meta_get .max_rounds)"

  if [ "$(now)" -ge "$(meta_get .deadline)" ]; then finish timeout "全体の制限時間を超過"; exit 2; fi
  if [ "$round" -ge "$max" ]; then finish max_rounds "最大 $max 往復に到達"; exit 2; fi
  if [ "$round" -gt 0 ] && [ ! -f "$SESSION/round-$round/claude.json" ]; then
    die "往復 $round の指摘への回答が未登録です（respond を先に実行）"
  fi
  if [ "$(meta_get .mode)" = "review-only" ] && [ "$(fingerprint)" != "$(meta_get .fingerprint)" ]; then
    finish error "review-only モードなのに作業ツリーが変更された"; exit 3
  fi

  local n=$((round + 1)) dir="$SESSION/round-$((round + 1))"
  mkdir -p "$dir"
  build_prompt "$n" >"$dir/prompt.md"

  local remaining=$(( $(meta_get .deadline) - $(now) )) t="${AI_REVIEW_TIMEOUT:-600}"
  [ "$remaining" -lt "$t" ] && t="$remaining"
  echo "往復 $n/$max: Codex にレビューを依頼中（タイムアウト ${t} 秒）..."
  local rc=0
  timeout --kill-after=10 "$t" codex exec --sandbox read-only --ephemeral --skip-git-repo-check \
    -C "$REPO" --output-schema "$SCHEMA" -o "$dir/codex.json" - <"$dir/prompt.md" >"$dir/codex.log" 2>&1 || rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then finish timeout "Codex の応答が ${t} 秒以内に返らなかった（往復 $n）"; exit 2; fi
  if [ "$rc" -ne 0 ]; then
    tail -20 "$dir/codex.log" >&2
    finish error "Codex がエラー終了（終了コード $rc、往復 $n）。ログ: $dir/codex.log"; exit 3
  fi
  jq -e '.verdict and (.findings|type=="array")' "$dir/codex.json" >/dev/null 2>&1 \
    || { finish error "Codex の出力が想定形式ではない（往復 $n）"; exit 3; }

  meta_set --argjson n "$n" '.round=$n'
  if [ "$(meta_get .mode)" = "review-only" ] && [ "$(fingerprint)" != "$(meta_get .fingerprint)" ]; then
    finish error "Codex 実行中に作業ツリーが変更された"; exit 3
  fi

  jq -r '"判定: \(.verdict)\n要約: \(.summary)",
         (.responses_to_claude[] | "  回答 [\(.finding_id)] \(.stance): \(.comment)"),
         (.findings[] | "  指摘 [\(.id)] (\(.severity)) \(.file):\(.line) \(.title)\n      \(.detail)\n      提案: \(.suggestion)")' \
    "$dir/codex.json"

  if [ "$(jq '.findings|length' "$dir/codex.json")" -eq 0 ]; then
    finish lgtm "往復 $n で指摘がなくなった"
  elif [ "$n" -ge "$max" ]; then
    echo "（最終往復です。respond で回答を登録すると、セッションは上限到達で終了します）"
  fi
}

cmd_respond() {
  local file="${1:-}"; [ -f "$file" ] || die "回答ファイルを指定してください"
  SESSION="$(session_dir)"
  [ "$(meta_get .status)" = "active" ] || die "セッションは終了しています（$(meta_get .end_reason)）"
  local round; round="$(meta_get .round)"
  [ "$round" -gt 0 ] || die "まだ Codex のレビューがありません"
  local dir="$SESSION/round-$round"
  [ ! -f "$dir/claude.json" ] || die "往復 $round の回答は登録済みです"

  jq -e 'type=="array" and all(.[]; (.finding_id|type=="string") and
         (.decision|IN("fixed","rejected","deferred")) and (.reason|type=="string" and length>0))' \
    "$file" >/dev/null 2>&1 || die '回答は [{"finding_id","decision":"fixed|rejected|deferred","reason"}] の配列で書いてください'
  local missing
  missing="$(jq -rn --slurpfile c "$dir/codex.json" --slurpfile r "$file" \
    '[$c[0].findings[].id] - [$r[0][].finding_id] | join(", ")')"
  [ -z "$missing" ] || die "回答がない指摘があります: $missing"

  if [ "$(meta_get .mode)" = "review-only" ]; then
    jq -e 'all(.[]; .decision != "fixed")' "$file" >/dev/null || die "review-only モードでは fixed は使えません"
    [ "$(fingerprint)" = "$(meta_get .fingerprint)" ] || { finish error "review-only モードなのに作業ツリーが変更された"; exit 3; }
  else
    meta_set --arg fp "$(fingerprint)" '.fingerprint=$fp'
  fi
  cp "$file" "$dir/claude.json"
  echo "往復 $round の回答を登録しました"
  if [ "$round" -ge "$(meta_get .max_rounds)" ]; then finish max_rounds "最大 $(meta_get .max_rounds) 往復に到達"; fi
}

cmd_status() {
  SESSION="$(session_dir)"
  jq -r '"セッション \(.id) / モード \(.mode) / 往復 \(.round)/\(.max_rounds) / 状態 \(.status) \(.end_reason)"' "$SESSION/meta.json"
}

cmd_abort() {
  SESSION="$(session_dir)"
  [ "$(meta_get .status)" != "active" ] || finish aborted "手動で中止"
}

cmd_report() {
  SESSION="$(session_dir)"
  local n; n="$(meta_get .round)"
  jq -r '"# AI レビュー報告（\(.id)）\n\n- モード: \(.mode)\n- ブランチ: \(.branch)\n- 対象: \(if .base != "" then "\(.base) からの差分 " else "" end)\(.files|join(", "))\n- 往復: \(.round) / \(.max_rounds)\n- 終了理由: \(if .status=="active" then "（実行中）" else .end_reason end)\n"' "$SESSION/meta.json"
  for ((i = 1; i <= n; i++)); do
    local d="$SESSION/round-$i"
    echo "## 往復 $i"
    jq -r '"**Codex の判定:** \(.verdict) — \(.summary)\n",
           (if (.responses_to_claude|length)>0 then "**Codex から Claude への返答:**\n" + ([.responses_to_claude[] | "- [\(.finding_id)] \(.stance): \(.comment)"]|join("\n")) + "\n" else empty end),
           (if (.findings|length)>0 then "**Codex の指摘:**\n" + ([.findings[] | "- [\(.id)] (\(.severity)) `\(.file):\(.line)` \(.title) — \(.detail) 提案: \(.suggestion)"]|join("\n")) + "\n" else "**指摘なし**\n" end)' "$d/codex.json"
    if [ -f "$d/claude.json" ]; then
      echo "**Claude Code の回答:**"
      jq -r '.[] | "- [\(.finding_id)] \(.decision): \(.reason)"' "$d/claude.json"
      echo
    fi
  done
  [ "$n" -gt 0 ] || { echo "## まとめ"; echo "- レビューは実行されていません"; return; }
  # 往復ごとの Codex 出力と Claude 回答を時系列に並べ、指摘 id ごとの最終状態を集計する
  local rounds_json
  rounds_json="$(for ((i = 1; i <= n; i++)); do
      jq -n --argjson i "$i" --slurpfile c "$SESSION/round-$i/codex.json" \
        --slurpfile r <(cat "$SESSION/round-$i/claude.json" 2>/dev/null || echo '[]') \
        '{round:$i, codex:$c[0], claude:$r[0]}'
    done | jq -s .)"
  jq -r '
    . as $rounds
    | ([$rounds[].codex.findings[]] | map({key:.id, value:.}) | from_entries) as $f
    | ([$rounds[].claude[]] | map({key:.finding_id, value:.}) | from_entries) as $last_claude
    | ([$rounds[].codex.responses_to_claude[]] | map({key:.finding_id, value:.}) | from_entries) as $last_codex
    | ($rounds[-1].codex.findings | map(.id)) as $open
    | def title($id): if $f[$id] then "(\($f[$id].severity)) \($f[$id].title)" else "" end;
      def codex($id): if $last_codex[$id] then "Codex: \($last_codex[$id].stance) — \($last_codex[$id].comment)" else "Codex: 再確認なし" end;
      def list(sel): [$last_claude | to_entries[] | select(.value.decision == sel) | .key];
      def show(ids; extra): if (ids|length)==0 then "- なし" else (ids[] | "- [\(.)] \(title(.))\n  - Claude: \($last_claude[.].reason)" + extra) end;
    "## まとめ\n",
    "**採用した改善案（Claude が修正）:**",
    show(list("fixed"); . as $id | if ($open|index($id)) then "\n  - ⚠ Codex は最終レビューでも未解決と判断" else "" end), "",
    "**保留（ユーザーの判断待ち。問題は未解決）:**",
    show(list("deferred") - $open; "\n  - " + codex(.)), "",
    "**却下（Claude が不要と判断）:**",
    show(list("rejected") - $open; "\n  - " + codex(.)), "",
    "**最終レビューで残った指摘（未解決）:**",
    (if ($open|length)==0 then "- なし" else
      ($rounds[-1].codex.findings[] | "- [\(.id)] (\(.severity)) `\(.file):\(.line)` \(.title)"
        + (. as $x | if ([$rounds[-1].claude[].finding_id] | index($x.id)) then "\n  - Claude: \($last_claude[.id].decision) — \($last_claude[.id].reason)" else "\n  - Claude: 回答なし" end)) end)
  ' <<<"$rounds_json"
}

case "${1:-}" in
  start) shift; cmd_start "$@" ;;
  round) cmd_round ;;
  respond) shift; cmd_respond "$@" ;;
  status) cmd_status ;;
  report) cmd_report ;;
  abort) cmd_abort ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
