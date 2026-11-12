import os
import re
import sys
import asyncio
import logging
import secrets
import threading
import traceback
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord.ext import commands
from flask import Flask
from waitress import serve
from google import genai


# ==========================================
# 1. ログ設定・エラー診断
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("SupportBot")
discord_logger = logging.getLogger("discord")


def log_http_error(where, error):
    """Discord HTTPExceptionのHTML本文を丸ごと出さず、診断情報だけ記録する。"""
    logger.error("Discord HTTPエラー発生箇所: %s", where)
    logger.error(
        "HTTP status=%s / Discord error code=%s / message=%s",
        getattr(error, "status", "不明"),
        getattr(error, "code", "不明"),
        getattr(error, "text", "本文なし")[:300],
    )

    response = getattr(error, "response", None)
    response_url = getattr(response, "url", None)
    if response_url:
        # URLに認証情報が含まれる可能性があるため、ホストとパスだけ記録
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit(str(response_url))
            logger.error("接続先: %s%s", parsed.netloc, parsed.path)
        except Exception:
            logger.error("接続先URLの取得に失敗しました。")

    body = str(getattr(error, "text", "") or "")
    ray_match = re.search(
        r"Cloudflare Ray ID:\s*</?[^>]*>\s*([a-fA-F0-9]+)",
        body,
        re.IGNORECASE,
    )
    if not ray_match:
        ray_match = re.search(
            r"Cloudflare Ray ID:\s*([a-fA-F0-9]+)",
            body,
            re.IGNORECASE,
        )

    if ray_match:
        logger.error("Cloudflare Ray ID: %s", ray_match.group(1))

    if "<!doctype html" in body.lower() or "<html" in body.lower():
        logger.error(
            "返答本文はHTMLです。通常のDiscord API JSON応答ではありません。"
        )
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            logger.error("HTMLページのタイトル: %s", title[:200])

    # 例外の巨大なHTML本文でログが埋まらないよう、スタックの位置だけ表示
    tb = traceback.extract_tb(error.__traceback__)
    if tb:
        logger.error("エラー発生箇所（呼び出し履歴）:")
        for frame in tb[-8:]:
            logger.error(
                '  File "%s", line %s, in %s',
                frame.filename,
                frame.lineno,
                frame.name,
            )
            if frame.line:
                logger.error("    %s", frame.line.strip())


def log_unexpected_error(where, error):
    logger.error(
        "予期しないエラー: %s / %s",
        where,
        type(error).__name__,
    )
    logger.error("%s", str(error)[:1000])
    tb = traceback.extract_tb(error.__traceback__)
    for frame in tb[-10:]:
        logger.error(
            '  File "%s", line %s, in %s',
            frame.filename,
            frame.lineno,
            frame.name,
        )
        if frame.line:
            logger.error("    %s", frame.line.strip())


# ==========================================
# 2. Render用Webサーバー
# ==========================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/health")
def health():
    return "OK", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    logger.info("Webサーバー起動: port=%s", port)
    serve(app, host="0.0.0.0", port=port)


threading.Thread(target=run_web, daemon=True).start()


# ==========================================
# 3. 基本設定
# ==========================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

AI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKENが設定されていません。")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEYが設定されていません。")


STAFF_ROLES = [
    1569062644919566336,
    1569106427065401354,
]

TICKET_CATEGORY = "チケット"

# ---- 注文受付まわりの設定（すべて任意） ----
# BRAND_NAME        : Embedに表示するサービス名
# PAYPAY_LINK       : PayPay受け取りリンク（共通）
# PAYPAY_LINKS      : 金額ごとのリンク 例) 900=https://...,4200=https://...
# STAFF_CHANNEL_ID  : 注文通知を送るスタッフ用チャンネル（未設定ならチケット内に通知）
# ORDER_BANNER_URL  : 受付パネル下部に表示するバナー画像URL
BRAND_NAME = os.getenv("BRAND_NAME", "PREMIUM SUPPORT")
PAYPAY_LINK = os.getenv("PAYPAY_LINK", "").strip()
ORDER_BANNER_URL = os.getenv("ORDER_BANNER_URL", "").strip()


def _parse_int_env(name):
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else 0
    except ValueError:
        logger.warning("%s が数値ではないため無視します。", name)
        return 0


def _parse_paypay_links():
    links = {}
    raw = os.getenv("PAYPAY_LINKS", "").strip()
    if not raw:
        return links
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        amount, url = pair.split("=", 1)
        try:
            links[int(amount.strip())] = url.strip()
        except ValueError:
            logger.warning("PAYPAY_LINKS の書式が不正です: %s", pair)
    return links


STAFF_CHANNEL_ID = _parse_int_env("STAFF_CHANNEL_ID")
PAYPAY_LINKS = _parse_paypay_links()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


# ==========================================
# 4. Bot本体
# ==========================================

class SupportBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(TicketPanel())
        self.add_view(OpenTicketView())
        self.add_view(ClosedTicketView())
        # 注文受付のボタン・セレクト・モーダルは on_interaction で custom_id から
        # 直接処理するため、再起動後も追加登録なしで動作する。
        logger.info("永続Viewの登録が完了しました。")


bot = SupportBot(
    command_prefix="!",
    intents=intents,
)


# ==========================================
# 5. Gemini設定
# ==========================================

ai_client = genai.Client(api_key=GEMINI_API_KEY)

ai_enabled = True
paused_channels = set()
histories = {}
handoff_notified = set()

AI_SEMAPHORE = asyncio.Semaphore(2)
channel_locks = defaultdict(asyncio.Lock)
order_locks = defaultdict(asyncio.Lock)


# ==========================================
# 6. 料金表
# ==========================================

RANK_NAMES = {
    0: "エリート1",
    1: "レジェンド1",
    2: "レジェンド2",
    3: "レジェンド3",
    4: "マスター1",
    5: "マスター2",
    6: "マスター3",
    7: "プロ",
}

RANK_ALIASES = {
    0: ["エリート1", "エリート 1", "エリ1", "エリ 1", "elite1", "elite 1"],
    1: ["レジェンド1", "レジェンド 1", "レジェ1", "レジェ 1", "legend1", "legend 1"],
    2: ["レジェンド2", "レジェンド 2", "レジェ2", "レジェ 2", "legend2", "legend 2"],
    3: ["レジェンド3", "レジェンド 3", "レジェ3", "レジェ 3", "legend3", "legend 3"],
    4: ["マスター1", "マスター 1", "マス1", "マス 1", "master1", "master 1"],
    5: ["マスター2", "マスター 2", "マス2", "マス 2", "master2", "master 2"],
    6: ["マスター3", "マスター 3", "マス3", "マス 3", "master3", "master 3"],
    7: ["プロ", "pro"],
}

RANK_PRICES = {
    (0, 1): 900,
    (1, 4): 4200,
    (2, 4): 3200,
    (3, 4): 2000,
    (4, 5): 8000,
    (5, 6): 20000,
    (6, 7): 40000,
}

RANK_SEGMENTS = {
    (0, 1): 900,
    (1, 4): 4200,
}

TROPHY_PRICES = {
    (0, 1000): 300,
    (1000, 2000): 700,
    (2000, 3000): 15000,
    (2500, 3000): 8499,
}


# ==========================================
# 7. スタッフ判定
# ==========================================

def is_staff(member):
    if not isinstance(member, discord.Member):
        return False
    return any(role.id in STAFF_ROLES for role in member.roles)


def is_ticket(channel):
    return (
        isinstance(channel, discord.TextChannel)
        and channel.topic is not None
        and channel.topic.startswith("ticket_owner:")
    )


def get_ticket_owner(channel):
    if not is_ticket(channel):
        return None
    try:
        return int(channel.topic.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        return None


async def fetch_ticket_owner_member(guild, owner_id):
    """チケット所有者のMemberを取得する（権限設定にはMemberが必要）。"""
    if guild is None or not owner_id:
        return None

    member = guild.get_member(owner_id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(owner_id)
    except discord.NotFound:
        # 所有者がサーバーを退出済みの場合、権限変更の対象は存在しない。
        return None


def staff_mentions(guild):
    if guild is None:
        return ""

    mentions = []
    for role_id in STAFF_ROLES:
        role = guild.get_role(role_id)
        if role:
            mentions.append(role.mention)
    return " ".join(mentions)


# ==========================================
# 8. 料金計算
# ==========================================

def find_ranks(text):
    found = []
    for rank, aliases in RANK_ALIASES.items():
        for alias in aliases:
            if re.search(re.escape(alias), text, re.IGNORECASE):
                found.append(rank)
                break
    return sorted(set(found))


def calculate_rank_price(text):
    ranks = find_ranks(text)
    if len(ranks) < 2:
        return None

    start = ranks[0]
    goal = ranks[-1]

    if start >= goal:
        return None

    direct = RANK_PRICES.get((start, goal))
    if direct is not None:
        return direct

    if len(ranks) >= 3:
        total = 0
        for i in range(len(ranks) - 1):
            price = RANK_SEGMENTS.get((ranks[i], ranks[i + 1]))
            if price is None:
                return None
            total += price
        return total

    return None


def calculate_trophy_price(text):
    if not re.search(r"トロ|トロフィー|trophy", text, re.IGNORECASE):
        return None

    numbers = re.findall(r"\d+", text)
    if len(numbers) < 2:
        return None

    start = int(numbers[0])
    goal = int(numbers[1])

    if start >= goal:
        return None

    return TROPHY_PRICES.get((start, goal))


# ---- ボタン式受付用（区間を直接指定する版。料金表の外は計算しない） ----

def rank_quote(start, goal):
    """料金表の直接区間、またはRANK_SEGMENTSの連結だけで料金を出す。"""
    if start is None or goal is None or start >= goal:
        return None

    direct = RANK_PRICES.get((start, goal))
    if direct is not None:
        return direct

    def walk(current, total):
        if current == goal:
            return total
        for (a, b), price in RANK_SEGMENTS.items():
            if a == current and b <= goal:
                result = walk(b, total + price)
                if result is not None:
                    return result
        return None

    return walk(start, 0)


def trophy_quote(start, goal):
    if start is None or goal is None:
        return None
    return TROPHY_PRICES.get((start, goal))


def rank_goal_options(start):
    return [g for g in sorted(RANK_NAMES) if rank_quote(start, g) is not None]


def rank_start_options():
    return [r for r in sorted(RANK_NAMES) if rank_goal_options(r)]


def trophy_start_options():
    return sorted({s for s, _ in TROPHY_PRICES})


def trophy_goal_options(start):
    return sorted(g for s, g in TROPHY_PRICES if s == start)


# ==========================================
# 9. Gemini AI
# ==========================================

SYSTEM_PROMPT = """
あなたはブロスタの代行サービスを案内するDiscordサポートAIです。

必ず日本語で、丁寧かつ分かりやすく回答してください。

【重要なルール】

1. 分からないことを絶対に作らない。
2. 料金表にない金額を勝手に計算しない。
3. 支払いが完了したかどうかを勝手に判断しない。
4. アカウントのパスワードや認証コードを要求しない。
5. 返金、トラブル、クレーム、支払い確認はスタッフに引き継ぐ。
6. 判断できない内容はすぐにスタッフへ引き継ぐ。
7. チートの配布・導入方法などの案内はしない。問い合わせはスタッフへ引き継ぐ。
8. 注文はチケット作成後に表示されるボタン式の受付パネルから行うよう案内する。

【ガチバトル料金】

エリート1〜レジェンド1：900円
レジェンド1〜マスター1：4200円
レジェンド2〜マスター1：3200円
レジェンド3〜マスター1：2000円
マスター1〜マスター2：8000円
マスター2〜マスター3：20000円
マスター3〜プロ：40000円

【トロ上げ料金】

0〜1000：300円
1000〜2000：700円
2000〜3000：15000円
2500〜3000：8499円

2000〜2500は料金未設定です。
料金表にない区間はスタッフに確認してください。

【スタッフへの引き継ぎ】

対応できない場合は、回答の先頭に必ず
[STAFF_HANDOFF]
と付けてください。

特に料金不明、例外、支払い、トラブル、チート関連の相談は
すぐにスタッフへ引き継いでください。

スタッフに確認する必要がある場合は、
ユーザーに「スタッフに確認します」と伝えてください。
"""


async def ask_ai(channel_id, user_message):
    async with AI_SEMAPHORE:
        history = histories.get(channel_id, [])

        chat_history_text = SYSTEM_PROMPT + "\n\n【これまでの会話】\n"

        for h in history[-10:]:
            role_name = "ユーザー" if h["role"] == "user" else "AI"
            chat_history_text += f"{role_name}: {h['content']}\n"

        chat_history_text += f"ユーザー: {user_message}\nAI:"

        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model=AI_MODEL,
            contents=chat_history_text,
        )

        answer = response.text
        if not answer:
            raise ValueError("Geminiから空の回答が返されました。")

        answer = answer.strip()

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": answer})
        histories[channel_id] = history[-16:]

        return answer


# ==========================================
# 10. Discord送信処理
# ==========================================

async def safe_send(channel, content, **kwargs):
    try:
        return await channel.send(content, **kwargs)

    except discord.HTTPException as error:
        log_http_error("チャンネルへのメッセージ送信", error)

    except discord.DiscordException as error:
        log_unexpected_error("Discordメッセージ送信", error)

    return None


async def send_long_message(channel, content):
    if not content:
        return

    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)]
    for chunk in chunks:
        result = await safe_send(channel, chunk)
        if result is None:
            break


# ==========================================
# 11. ボタン式 注文受付 ─ デザイン定義
# ==========================================
#
# 状態の保存先は「受付パネルのEmbed自身」。
#   ・注文ID / 状態 … フッター「ORDER #XXXXXX ・ 状態」
#   ・回答内容     … 各フィールド（名前=項目ラベル、値=コードブロック）
#   ・注文者       … チケットチャンネルのトピック ticket_owner:<ID>
#   ・現在の画面   … 押されたボタンの custom_id
# そのため、Renderのようにディスクが消える環境でも、再起動後に
# パネルのボタンを押せば続きから受付を再開できる。

COLOR_MAIN = 0x7C3AED   # バイオレット
COLOR_DEEP = 0x4C1D95   # ディープパープル
COLOR_GLOW = 0xA78BFA   # ライトパープル
COLOR_WHITE = 0xF5F3FF  # 白（完了）
COLOR_VOID = 0x18181B   # 黒（キャンセル）

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
EMPTY = "―"

CATEGORY_LABELS = {"boost": "代行", "cheat": "チート"}
SERVICE_LABELS = {"trophy": "トロ上げ代行", "ranked": "ガチバ代行"}
DEVICE_LABELS = {"ios": "iOS", "android": "Android"}

STATUS_TEXT = {
    "draft": "入力中",
    "submitted": "受付済み",
    "reported": "入金報告済み",
    "paid": "対応中",
    "done": "完了",
    "cancelled": "キャンセル",
}
STATUS_FROM_TEXT = {v: k for k, v in STATUS_TEXT.items()}

FIELD_LABELS = {
    "category": "区分",
    "service": "サービス",
    "start": "現在",
    "goal": "目標",
    "tag": "プレイヤータグ",
    "brawler": "対象キャラ",
    "note": "備考",
    "device": "端末",
    "detail": "相談内容",
}
LABEL_TO_KEY = {v: k for k, v in FIELD_LABELS.items()}

FOOTER_RE = re.compile(r"ORDER #([0-9A-F]{4,16})\s*・\s*(\S+)")


def step_title(state, step):
    service = state["answers"].get("service")
    titles = {
        "category": "ご依頼の種類",
        "service": "サービス",
        "start": "現在のトロフィー" if service == "trophy" else "現在のランク",
        "goal": "目標のトロフィー" if service == "trophy" else "目標のランク",
        "info": "アカウント情報",
        "device": "ご利用端末",
        "detail": "ご相談内容",
        "confirm": "最終確認",
    }
    return titles.get(step, step)


# ==========================================
# 12. ボタン式 注文受付 ─ 状態とフロー
# ==========================================

def new_order_state(owner_id):
    return {
        "order_id": secrets.token_hex(3).upper(),
        "owner_id": owner_id,
        "status": "draft",
        "answers": {},
    }


def step_sequence(state):
    """選択内容に応じて質問の順番を分岐させる。"""
    answers = state["answers"]
    seq = ["category"]
    if answers.get("category") == "cheat":
        seq += ["device", "detail"]
    elif answers.get("category") == "boost":
        seq += ["service", "start", "goal", "info"]
    seq.append("confirm")
    return seq


def order_quote(state):
    answers = state["answers"]
    if answers.get("category") != "boost":
        return None
    start, goal = answers.get("start"), answers.get("goal")
    if answers.get("service") == "trophy":
        return trophy_quote(start, goal)
    if answers.get("service") == "ranked":
        return rank_quote(start, goal)
    return None


def step_done(state, step):
    answers = state["answers"]
    if step == "category":
        return answers.get("category") in CATEGORY_LABELS
    if step == "service":
        return answers.get("service") in SERVICE_LABELS
    if step == "start":
        return answers.get("start") is not None
    if step == "goal":
        return order_quote(state) is not None
    if step == "info":
        return bool(answers.get("tag"))
    if step == "device":
        return answers.get("device") in DEVICE_LABELS
    if step == "detail":
        return bool(answers.get("detail"))
    return True


def first_incomplete(state):
    for step in step_sequence(state)[:-1]:
        if not step_done(state, step):
            return step
    return "confirm"


def next_step(state, step):
    seq = step_sequence(state)
    if step not in seq:
        return first_incomplete(state)
    idx = seq.index(step)
    return seq[min(idx + 1, len(seq) - 1)]


def prev_step(state, step):
    seq = step_sequence(state)
    if step not in seq:
        return None
    idx = seq.index(step)
    return seq[idx - 1] if idx > 0 else None


def apply_answer(state, key, value):
    """回答を反映し、前提が変わって無効になった回答は消す。"""
    answers = state["answers"]

    if key == "category":
        if answers.get("category") != value:
            state["answers"] = {"category": value}
        return

    if key == "service":
        if answers.get("service") != value:
            kept = {k: answers[k] for k in ("category", "tag", "note") if k in answers}
            kept["service"] = value
            state["answers"] = kept
        return

    answers[key] = value
    if key == "start" and answers.get("goal") is not None:
        if order_quote(state) is None:
            answers.pop("goal", None)


def display_value(state, key):
    answers = state["answers"]
    value = answers.get(key)
    if value is None or value == "":
        return None
    if key == "category":
        return CATEGORY_LABELS.get(value)
    if key == "service":
        return SERVICE_LABELS.get(value)
    if key == "device":
        return DEVICE_LABELS.get(value)
    if key in ("start", "goal"):
        if answers.get("service") == "trophy":
            return f"{value:,} トロ"
        return RANK_NAMES.get(value)
    return str(value)


def decode_value(key, text, service):
    if text in ("", EMPTY):
        return None
    if key == "category":
        return {v: k for k, v in CATEGORY_LABELS.items()}.get(text)
    if key == "service":
        return {v: k for k, v in SERVICE_LABELS.items()}.get(text)
    if key == "device":
        return {v: k for k, v in DEVICE_LABELS.items()}.get(text)
    if key in ("start", "goal"):
        if service == "trophy":
            digits = re.sub(r"\D", "", text)
            return int(digits) if digits else None
        return {v: k for k, v in RANK_NAMES.items()}.get(text)
    return text


def summary_keys(state):
    answers = state["answers"]
    category = answers.get("category")
    if category == "cheat":
        return ["category", "device", "detail"]
    if category == "boost":
        keys = ["category", "service"]
        service = answers.get("service")
        if service:
            keys += ["start", "goal", "tag"]
            if service == "trophy":
                keys.append("brawler")
            keys.append("note")
        return keys
    return ["category"]


def box(text):
    safe = str(text).replace("```", "ˋˋˋ")
    return f"```\n{safe}\n```"


def unbox(value):
    text = (value or "").strip()
    if text.startswith("```") and text.endswith("```") and len(text) >= 6:
        text = text[3:-3]
    return text.strip("\n").strip()


def parse_order_message(message):
    """受付パネルのEmbedから注文状態を復元する（再起動対策の要）。"""
    if message is None or not message.embeds:
        return None

    embed = message.embeds[0]
    footer = (embed.footer.text if embed.footer else "") or ""
    match = FOOTER_RE.search(footer)
    if not match:
        return None

    raw = {}
    for field in embed.fields:
        label = (field.name or "").split(" ", 1)[-1].strip()
        key = LABEL_TO_KEY.get(label)
        if key:
            raw[key] = unbox(field.value)

    answers = {}
    for key in ("category", "service"):
        if key in raw:
            value = decode_value(key, raw[key], None)
            if value is not None:
                answers[key] = value

    service = answers.get("service")
    for key, text in raw.items():
        if key in ("category", "service"):
            continue
        value = decode_value(key, text, service)
        if value is not None:
            answers[key] = value

    return {
        "order_id": match.group(1),
        "owner_id": get_ticket_owner(message.channel),
        "status": STATUS_FROM_TEXT.get(match.group(2), "draft"),
        "answers": answers,
    }


# ==========================================
# 13. ボタン式 注文受付 ─ 画面描画
# ==========================================

def _button(label, custom_id=None, *, style=discord.ButtonStyle.secondary,
            emoji=None, row=None, disabled=False, url=None):
    if url:
        return discord.ui.Button(label=label, url=url, emoji=emoji, row=row)
    return discord.ui.Button(
        label=label,
        custom_id=custom_id,
        style=style,
        emoji=emoji,
        row=row,
        disabled=disabled,
    )


def _view(items):
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    # 押下時の処理は on_interaction で custom_id を見て行う。
    # Viewは見た目の生成だけに使い、内部ストアに溜め込まないよう停止しておく。
    view.stop()
    return view


def _progress_header(state, step):
    seq = step_sequence(state)
    known = state["answers"].get("category") is not None

    if not known:
        return (
            "`STEP 1 / ?`　▰▱▱▱▱▱\n"
            f"**▸ {step_title(state, 'category')}**　›　…　›　{step_title(state, 'confirm')}"
        )

    idx = seq.index(step) if step in seq else 0
    total = len(seq)
    bar = "▰" * (idx + 1) + "▱" * (total - idx - 1)
    crumbs = []
    for i, s in enumerate(seq):
        title = step_title(state, s)
        crumbs.append(f"**▸ {title}**" if i == idx else title)
    return f"`STEP {idx + 1} / {total}`　{bar}\n" + "　›　".join(crumbs)


def _status_header(state):
    is_cheat = state["answers"].get("category") == "cheat"
    stages = (
        ["受付", "スタッフ確認", "対応中", "完了"]
        if is_cheat
        else ["注文受付", "入金確認", "対応中", "完了"]
    )
    reached = {
        "submitted": 0,
        "reported": 1,
        "paid": 2,
        "done": 3,
    }.get(state["status"], -1)

    parts = []
    for i, name in enumerate(stages):
        if i < reached:
            parts.append(f"◆ {name}")
        elif i == reached:
            parts.append(f"**◈ {name}**")
        else:
            parts.append(f"◇ {name}")
    return "　━　".join(parts)


def build_order_embed(state, title, body, *, header=None, color=COLOR_MAIN):
    lines = []
    if header:
        lines += [header, DIVIDER]
    lines.append(body)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=f"◆ {BRAND_NAME}　｜　ORDER DESK")

    for key in summary_keys(state):
        text = display_value(state, key)
        icon = "◆" if text else "◇"
        embed.add_field(
            name=f"{icon} {FIELD_LABELS[key]}",
            value=box(text or EMPTY),
            inline=key not in ("note", "detail"),
        )

    category = state["answers"].get("category")
    if category == "boost":
        price = order_quote(state)
        embed.add_field(
            name="✦ お見積り",
            value=box(f"¥ {price:,}" if price is not None else EMPTY),
            inline=False,
        )
    elif category == "cheat":
        embed.add_field(
            name="✦ 料金",
            value=box("スタッフが個別にご案内します"),
            inline=False,
        )

    embed.set_footer(
        text=f"ORDER #{state['order_id']} ・ {STATUS_TEXT[state['status']]}"
    )
    if ORDER_BANNER_URL:
        embed.set_image(url=ORDER_BANNER_URL)
    return embed


def _nav_items(state, step, ret, *, show_next=False):
    items = []
    if ret == "c" and first_incomplete(state) == "confirm":
        items.append(_button("確認画面へ戻る", f"ord:back:{step}:c", emoji="↩️", row=4))
    else:
        items.append(_button(
            "戻る", f"ord:back:{step}:n", emoji="◀️", row=4,
            disabled=prev_step(state, step) is None,
        ))
    if show_next:
        items.append(_button(
            "次へ", f"ord:next:{step}:{ret}",
            style=discord.ButtonStyle.primary, emoji="▶️", row=4,
        ))
    items.append(_button("キャンセル", f"ord:cancel:{step}", emoji="✖️", row=4))
    return items


def _choice_style(selected, value, default_primary):
    if selected is None:
        return discord.ButtonStyle.primary if default_primary else discord.ButtonStyle.secondary
    return discord.ButtonStyle.primary if selected == value else discord.ButtonStyle.secondary


def render_step(state, step, ret="n"):
    answers = state["answers"]
    header = _progress_header(state, step)
    prefix = "変更 ─ " if ret == "c" else ""
    items = []
    show_next = False

    if step == "category":
        title = f"{prefix}ご依頼の種類を選択してください"
        body = (
            "ご希望のメニューを下のボタンから選んでください。\n\n"
            "⚡ **代行**\n"
            "　└ トロ上げ代行・ガチバトル代行（料金を自動計算）\n"
            "🧩 **チート**\n"
            "　└ ご相談内容を伺い、スタッフが個別にご案内します"
        )
        selected = answers.get("category")
        items += [
            _button("代行", f"ord:set:category:boost:{ret}", emoji="⚡", row=0,
                    style=_choice_style(selected, "boost", True)),
            _button("チート", f"ord:set:category:cheat:{ret}", emoji="🧩", row=0,
                    style=_choice_style(selected, "cheat", False)),
        ]

    elif step == "service":
        title = f"{prefix}代行メニューを選択してください"
        body = (
            "ご希望の代行サービスを選んでください。\n\n"
            f"🏆 **トロ上げ代行**　─　¥{min(TROPHY_PRICES.values()):,} 〜\n"
            "　└ トロフィー帯を指定して代行します\n"
            f"⚔️ **ガチバ代行**　─　¥{min(RANK_PRICES.values()):,} 〜\n"
            "　└ ガチバトルのランクを指定して代行します"
        )
        selected = answers.get("service")
        items += [
            _button("トロ上げ代行", f"ord:set:service:trophy:{ret}", emoji="🏆", row=0,
                    style=_choice_style(selected, "trophy", True)),
            _button("ガチバ代行", f"ord:set:service:ranked:{ret}", emoji="⚔️", row=0,
                    style=_choice_style(selected, "ranked", True)),
        ]

    elif step == "start":
        trophy = answers.get("service") == "trophy"
        title = f"{prefix}{step_title(state, step)}を選択してください"
        body = (
            "現在の状況に最も近いものを選んでください。\n"
            "料金表に設定のある区間のみ選択できます。"
        )
        current = answers.get("start")
        if trophy:
            options = [
                discord.SelectOption(
                    label=f"{s:,} トロ から",
                    value=str(s),
                    emoji="🏆",
                    default=current == s,
                )
                for s in trophy_start_options()
            ]
        else:
            options = [
                discord.SelectOption(
                    label=RANK_NAMES[r],
                    value=str(r),
                    emoji="⚔️",
                    default=current == r,
                )
                for r in rank_start_options()
            ]
        items.append(discord.ui.Select(
            custom_id=f"ord:sel:start:{ret}",
            placeholder="▸ " + step_title(state, step) + "を選択",
            options=options,
            row=0,
        ))
        show_next = step_done(state, step)

    elif step == "goal":
        trophy = answers.get("service") == "trophy"
        start = answers.get("start")
        title = f"{prefix}{step_title(state, step)}を選択してください"
        start_text = display_value(state, "start") or EMPTY
        body = (
            f"現在：**{start_text}**\n"
            "目標を選ぶと、料金表に基づいて料金が自動で計算されます。"
        )
        price = order_quote(state)
        if price is not None:
            body += f"\n\n### お見積り　¥{price:,}"

        current = answers.get("goal")
        if trophy:
            goals = trophy_goal_options(start)
            options = [
                discord.SelectOption(
                    label=f"{g:,} トロ まで",
                    value=str(g),
                    description=f"料金 ¥{trophy_quote(start, g):,}",
                    emoji="💎",
                    default=current == g,
                )
                for g in goals
            ]
        else:
            goals = rank_goal_options(start)
            options = [
                discord.SelectOption(
                    label=RANK_NAMES[g],
                    value=str(g),
                    description=f"料金 ¥{rank_quote(start, g):,}",
                    emoji="💎",
                    default=current == g,
                )
                for g in goals
            ]
        if options:
            items.append(discord.ui.Select(
                custom_id=f"ord:sel:goal:{ret}",
                placeholder="▸ " + step_title(state, step) + "を選択",
                options=options,
                row=0,
            ))
        else:
            body += "\n\n⚠️ この開始地点から選べる目標がありません。戻って選び直してください。"
        show_next = step_done(state, step)

    elif step == "info":
        trophy = answers.get("service") == "trophy"
        title = f"{prefix}アカウント情報を入力してください"
        lines = [
            "下のボタンから入力フォームを開いてください。\n",
            "◆ **プレイヤータグ**（必須）　例：`#ABC123`",
        ]
        if trophy:
            lines.append("◆ **対象キャラ**（任意）")
        lines += [
            "◆ **備考**（任意）　希望納期など",
            "",
            "🔐 パスワードや認証コードは**入力しないでください**。",
            "　└ 必要な手続きはスタッフから個別にご案内します。",
        ]
        body = "\n".join(lines)
        done = step_done(state, step)
        items.append(_button(
            "入力内容を修正する" if done else "情報を入力する",
            f"ord:open:info:{ret}",
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary,
            emoji="📝",
            row=0,
        ))
        show_next = done

    elif step == "device":
        title = f"{prefix}ご利用の端末を選択してください"
        body = "ご相談の対象となる端末を選んでください。"
        selected = answers.get("device")
        items += [
            _button("iOS", f"ord:set:device:ios:{ret}", emoji="📱", row=0,
                    style=_choice_style(selected, "ios", True)),
            _button("Android", f"ord:set:device:android:{ret}", emoji="🤖", row=0,
                    style=_choice_style(selected, "android", True)),
        ]

    elif step == "detail":
        title = f"{prefix}ご相談内容を入力してください"
        body = (
            "下のボタンから入力フォームを開き、ご相談内容をご記入ください。\n"
            "内容を確認したうえで、スタッフが個別にご案内します。\n\n"
            "🔐 パスワードや認証コードは**入力しないでください**。"
        )
        done = step_done(state, step)
        items.append(_button(
            "入力内容を修正する" if done else "相談内容を入力する",
            f"ord:open:detail:{ret}",
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary,
            emoji="📝",
            row=0,
        ))
        show_next = done

    else:
        return render_panel(state)

    items += _nav_items(state, step, ret, show_next=show_next)
    embed = build_order_embed(state, title, body, header=header)
    return embed, _view(items)


def render_confirm(state):
    header = _progress_header(state, "confirm")
    is_cheat = state["answers"].get("category") == "cheat"

    if is_cheat:
        title = "ご相談内容の最終確認"
        body = (
            "以下の内容でよろしければ **スタッフへ送信** を押してください。\n"
            "送信後、担当スタッフから個別にご連絡します。"
        )
        submit_label = "スタッフへ送信"
    else:
        price = order_quote(state)
        title = "ご注文内容の最終確認"
        body = (
            "以下の内容でお間違いなければ **注文を確定** を押してください。\n\n"
            f"### お支払い金額　¥{price:,}\n"
            "確定後、PayPayでのお支払い方法をご案内します。"
        )
        submit_label = "注文を確定"

    options = []
    for step in step_sequence(state)[:-1]:
        current = None
        if step == "info":
            current = display_value(state, "tag")
        elif step in FIELD_LABELS:
            current = display_value(state, step)
        options.append(discord.SelectOption(
            label=f"{step_title(state, step)} を変更",
            value=step,
            description=(current or "未入力")[:90],
            emoji="✏️",
        ))

    items = [
        discord.ui.Select(
            custom_id="ord:edit",
            placeholder="✏️ 内容を変更する項目を選択",
            options=options,
            row=0,
        ),
        _button(submit_label, "ord:submit", style=discord.ButtonStyle.primary,
                emoji="✅", row=1),
    ]
    items += _nav_items(state, "confirm", "n")
    embed = build_order_embed(state, title, body, header=header, color=COLOR_DEEP)
    return embed, _view(items)


def paypay_link_for(amount):
    if amount is not None and amount in PAYPAY_LINKS:
        return PAYPAY_LINKS[amount]
    return PAYPAY_LINK or None


def render_status(state):
    status = state["status"]
    is_cheat = state["answers"].get("category") == "cheat"
    header = _status_header(state)
    oid = state["order_id"]
    items = []
    color = COLOR_MAIN

    if status == "submitted" and not is_cheat:
        price = order_quote(state)
        link = paypay_link_for(price)
        title = "ご注文を受け付けました ─ お支払いのご案内"
        steps = []
        if link:
            steps.append(f"**1.** 下の **PayPayで支払う** から **¥{price:,}** を送金")
        else:
            steps.append(f"**1.** スタッフがお送りするPayPayリンクから **¥{price:,}** を送金")
        steps += [
            f"**2.** 送金メッセージに注文ID **`#{oid}`** を記入",
            "**3.** 送金後、**支払いを報告** を押してください",
        ]
        body = (
            f"### お支払い金額　¥{price:,}\n"
            + "\n".join(steps)
            + "\n\n※ 入金の確認はスタッフが行います。確認後に対応を開始します。"
        )
        if link:
            items.append(_button("PayPayで支払う", url=link, emoji="💳", row=0))
        items += [
            _button("支払いを報告", "ord:report", style=discord.ButtonStyle.primary,
                    emoji="💸", row=0),
            _button("注文を取り消す", "ord:cancel:pay", emoji="✖️", row=1),
        ]

    elif status == "submitted":
        title = "ご相談をスタッフへ送信しました"
        body = (
            "担当スタッフが内容を確認しています。\n"
            "このチャンネルでのご連絡をお待ちください。"
        )
        items.append(_button("相談を取り消す", "ord:cancel:pay", emoji="✖️", row=0))

    elif status == "reported":
        title = "入金を確認しています"
        body = (
            "お支払いのご報告ありがとうございます。\n"
            "スタッフが入金を確認するまで、しばらくお待ちください。"
        )
        color = COLOR_GLOW

    elif status == "paid":
        title = "対応を開始しました"
        body = (
            "スタッフが内容を確認し、対応を開始しました。\n"
            "進行状況はこのチャンネルでご連絡します。"
            if is_cheat else
            "入金を確認しました。担当スタッフが代行を開始します。\n"
            "進行状況はこのチャンネルでご連絡します。"
        )
        color = COLOR_GLOW

    elif status == "done":
        title = "ご依頼が完了しました"
        body = "ご利用ありがとうございました。またのご依頼をお待ちしております。"
        color = COLOR_WHITE
        items.append(_button("新しく注文する", "ord:restart",
                             style=discord.ButtonStyle.primary, emoji="✨", row=0))

    else:  # cancelled
        title = "ご注文はキャンセルされました"
        body = "もう一度ご依頼される場合は、下のボタンから最初からやり直せます。"
        header = None
        color = COLOR_VOID
        items.append(_button("最初からやり直す", "ord:restart",
                             style=discord.ButtonStyle.primary, emoji="🔄", row=0))

    embed = build_order_embed(state, title, body, header=header, color=color)
    return embed, _view(items)


def render_cancel_confirm(state, back_to):
    submitted = state["status"] != "draft"
    title = "ご注文をキャンセルしますか？"
    body = (
        "キャンセルすると、この注文は取り消されスタッフにも通知されます。"
        if submitted else
        "入力した内容は破棄されます。キャンセル後は最初からやり直せます。"
    )
    items = [
        _button("キャンセルする", "ord:cxy", style=discord.ButtonStyle.danger,
                emoji="🗑️", row=0),
        _button("続ける", f"ord:cxn:{back_to}", style=discord.ButtonStyle.primary,
                emoji="↩️", row=0),
    ]
    embed = build_order_embed(state, title, body, color=COLOR_VOID)
    return embed, _view(items)


def render_panel(state, step=None, ret="n"):
    if state["status"] != "draft":
        return render_status(state)
    step = step or first_incomplete(state)
    if step == "confirm":
        if first_incomplete(state) != "confirm":
            return render_step(state, first_incomplete(state), "n")
        return render_confirm(state)
    return render_step(state, step, ret)


# ---- モーダル（入力フォーム） ----

def build_info_modal(state, ret):
    answers = state["answers"]
    modal = discord.ui.Modal(
        title="アカウント情報の入力",
        custom_id=f"ord:modal:info:{ret}",
        timeout=600,
    )
    modal.add_item(discord.ui.TextInput(
        label="プレイヤータグ（必須）",
        custom_id="tag",
        placeholder="#ABC123",
        default=answers.get("tag") or None,
        required=True,
        max_length=16,
    ))
    if answers.get("service") == "trophy":
        brawler = answers.get("brawler")
        modal.add_item(discord.ui.TextInput(
            label="対象キャラ（任意）",
            custom_id="brawler",
            placeholder="例：シェリー",
            default=brawler if brawler and brawler != "なし" else None,
            required=False,
            max_length=40,
        ))
    note = answers.get("note")
    modal.add_item(discord.ui.TextInput(
        label="備考（任意）",
        custom_id="note",
        style=discord.TextStyle.paragraph,
        placeholder="希望納期・連絡事項など（パスワードは書かないでください）",
        default=note if note and note != "なし" else None,
        required=False,
        max_length=300,
    ))
    return modal


def build_detail_modal(state, ret):
    modal = discord.ui.Modal(
        title="ご相談内容の入力",
        custom_id=f"ord:modal:detail:{ret}",
        timeout=600,
    )
    modal.add_item(discord.ui.TextInput(
        label="ご相談内容",
        custom_id="detail",
        style=discord.TextStyle.paragraph,
        placeholder="ご希望の内容をご記入ください（パスワードは書かないでください）",
        default=state["answers"].get("detail") or None,
        required=True,
        max_length=800,
    ))
    return modal


def modal_values(data):
    """モーダル送信データから custom_id → 入力値 を取り出す（構造の違いに強い）。"""
    values = {}

    def walk(node):
        if isinstance(node, dict):
            if "custom_id" in node and "value" in node:
                values[node["custom_id"]] = node["value"]
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk((data or {}).get("components", []))
    return values


def clean_text(value, limit):
    text = unicodedata.normalize("NFKC", value or "").strip()
    return text[:limit]


def normalize_tag(value):
    tag = unicodedata.normalize("NFKC", value or "").strip().upper().replace(" ", "")
    if not tag.startswith("#"):
        tag = "#" + tag
    if not re.fullmatch(r"#[0-9A-Z]{3,15}", tag):
        return None
    return tag


# ---- スタッフ通知 ----

def build_staff_embed(state, ticket_channel, title, panel_url=None, handled_by=None):
    oid = state["order_id"]
    lines = [
        f"**注文ID**　`#{oid}`",
        f"**お客様**　<@{state['owner_id']}>" if state.get("owner_id") else "**お客様**　不明",
        f"**チケット**　{ticket_channel.mention}",
        f"**状態**　{STATUS_TEXT[state['status']]}",
    ]
    if handled_by is not None:
        lines.append(f"**最終操作**　{handled_by.mention}")
    if panel_url:
        lines.append(f"\n[▸ 受付パネルを開く]({panel_url})")

    color = {
        "done": COLOR_WHITE,
        "cancelled": COLOR_VOID,
        "paid": COLOR_GLOW,
    }.get(state["status"], COLOR_MAIN)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=f"◆ {BRAND_NAME}　｜　STAFF CONSOLE")
    for key in summary_keys(state):
        text = display_value(state, key)
        embed.add_field(
            name=FIELD_LABELS[key],
            value=box(text or EMPTY),
            inline=key not in ("note", "detail"),
        )
    if state["answers"].get("category") == "boost":
        price = order_quote(state)
        embed.add_field(
            name="金額",
            value=box(f"¥ {price:,}" if price is not None else EMPTY),
            inline=False,
        )
    embed.set_footer(text=f"ORDER #{oid}（スタッフ控え）")
    return embed


def staff_view(state, channel_id, message_id):
    status = state["status"]
    is_cheat = state["answers"].get("category") == "cheat"
    suffix = f"{channel_id}:{message_id}"
    items = []
    if status in ("submitted", "reported"):
        items.append(_button(
            "対応開始" if is_cheat else "入金確認・対応開始",
            f"ord:s:paid:{suffix}",
            style=discord.ButtonStyle.primary, emoji="✅", row=0,
        ))
        items.append(_button("注文取消", f"ord:s:cancel:{suffix}", emoji="✖️", row=0))
    elif status == "paid":
        items.append(_button(
            "対応完了", f"ord:s:done:{suffix}",
            style=discord.ButtonStyle.primary, emoji="🏁", row=0,
        ))
        items.append(_button("注文取消", f"ord:s:cancel:{suffix}", emoji="✖️", row=0))
    else:
        items.append(_button(
            f"処理済み（{STATUS_TEXT[status]}）", f"ord:s:noop:{suffix}",
            emoji="🔒", row=0, disabled=True,
        ))
    return _view(items)


async def notify_staff(guild, ticket_channel, panel_message, state, title):
    target = guild.get_channel(STAFF_CHANNEL_ID) if (guild and STAFF_CHANNEL_ID) else None
    if target is None:
        target = ticket_channel

    embed = build_staff_embed(state, ticket_channel, title, panel_message.jump_url)
    view = staff_view(state, ticket_channel.id, panel_message.id)
    mentions = staff_mentions(guild)
    await safe_send(
        target,
        f"⚠️ **スタッフの対応をお願いします。** {mentions}".strip(),
        embed=embed,
        view=view,
    )


async def send_order_panel(channel, owner_id):
    state = new_order_state(owner_id)
    embed, view = render_panel(state)
    return await safe_send(channel, None, embed=embed, view=view)


# ==========================================
# 14. ボタン式 注文受付 ─ 操作の処理
# ==========================================

async def _ephemeral(interaction, text):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException as error:
        log_http_error("エフェメラル返信", error)


async def _show(interaction, state, step=None, ret="n"):
    embed, view = render_panel(state, step, ret)
    await interaction.response.edit_message(embed=embed, view=view)


async def _show_after_answer(interaction, state, step, ret):
    # 通常時は次の質問へ。確認画面からの変更時は、未入力があればそこへ、
    # なければ確認画面へ戻る（以降は通常の流れに戻す）。
    target = first_incomplete(state) if ret == "c" else next_step(state, step)
    mode = "c" if (ret == "c" and target != "confirm") else "n"
    await _show(interaction, state, target, mode)


def _ticket_closed_for(channel, owner_id):
    try:
        overwrite = channel.overwrites_for(discord.Object(id=owner_id))
        return overwrite.send_messages is False
    except Exception:
        return False


async def handle_customer_action(interaction, parts):
    message = interaction.message
    channel = interaction.channel
    state = parse_order_message(message)

    if state is None or not is_ticket(channel):
        await _ephemeral(interaction, "受付データを読み込めませんでした。スタッフにお問い合わせください。")
        return

    owner_id = state["owner_id"]
    if interaction.user.id != owner_id:
        await _ephemeral(
            interaction,
            f"この受付パネルは <@{owner_id}> さん専用です。ご自身のチケットから操作してください。",
        )
        return

    if _ticket_closed_for(channel, owner_id):
        await _ephemeral(interaction, "このチケットはクローズされています。")
        return

    action = parts[1]

    # ---- 入力中のみ許可する操作 ----
    draft_actions = {"set", "sel", "open", "modal", "back", "next", "edit", "submit"}
    if action in draft_actions and state["status"] != "draft":
        await _ephemeral(interaction, "この注文はすでに確定しています。")
        return

    if action == "set":            # ord:set:<key>:<value>:<ret>
        key, value, ret = parts[2], parts[3], parts[4]
        allowed = {"category": CATEGORY_LABELS, "service": SERVICE_LABELS, "device": DEVICE_LABELS}
        if key not in allowed or value not in allowed[key]:
            return await _ephemeral(interaction, "不正な選択です。")
        apply_answer(state, key, value)
        return await _show_after_answer(interaction, state, key, ret)

    if action == "sel":            # ord:sel:<key>:<ret>
        key, ret = parts[2], parts[3]
        values = (interaction.data or {}).get("values") or []
        if key not in ("start", "goal") or not values:
            return await _ephemeral(interaction, "不正な選択です。")
        try:
            value = int(values[0])
        except ValueError:
            return await _ephemeral(interaction, "不正な選択です。")
        trophy = state["answers"].get("service") == "trophy"
        if key == "start":
            valid = trophy_start_options() if trophy else rank_start_options()
        else:
            start = state["answers"].get("start")
            valid = trophy_goal_options(start) if trophy else rank_goal_options(start)
        if value not in valid:
            return await _ephemeral(interaction, "料金表にない区間は選択できません。")
        apply_answer(state, key, value)
        return await _show_after_answer(interaction, state, key, ret)

    if action == "open":           # ord:open:<info|detail>:<ret>
        key, ret = parts[2], parts[3]
        modal = build_info_modal(state, ret) if key == "info" else build_detail_modal(state, ret)
        await interaction.response.send_modal(modal)
        return

    if action == "modal":          # ord:modal:<info|detail>:<ret>
        key, ret = parts[2], parts[3]
        values = modal_values(interaction.data)
        if key == "info":
            tag = normalize_tag(values.get("tag"))
            if tag is None:
                return await _ephemeral(
                    interaction,
                    "プレイヤータグの形式が正しくありません。`#` と英数字（例：`#ABC123`）で入力してください。",
                )
            state["answers"]["tag"] = tag
            if state["answers"].get("service") == "trophy":
                state["answers"]["brawler"] = clean_text(values.get("brawler"), 40) or "なし"
            else:
                state["answers"].pop("brawler", None)
            state["answers"]["note"] = clean_text(values.get("note"), 300) or "なし"
        else:
            detail = clean_text(values.get("detail"), 800)
            if not detail:
                return await _ephemeral(interaction, "ご相談内容を入力してください。")
            state["answers"]["detail"] = detail
        return await _show_after_answer(interaction, state, key, ret)

    if action == "back":           # ord:back:<step>:<ret>
        step, ret = parts[2], parts[3]
        if ret == "c" and first_incomplete(state) == "confirm":
            return await _show(interaction, state, "confirm")
        target = prev_step(state, step) or "category"
        return await _show(interaction, state, target)

    if action == "next":           # ord:next:<step>:<ret>
        step, ret = parts[2], parts[3]
        if not step_done(state, step):
            return await _ephemeral(interaction, "先にこの項目を選択・入力してください。")
        return await _show_after_answer(interaction, state, step, ret)

    if action == "edit":           # ord:edit（セレクト）
        values = (interaction.data or {}).get("values") or []
        step = values[0] if values else None
        if step not in step_sequence(state)[:-1]:
            return await _ephemeral(interaction, "変更できない項目です。")
        return await _show(interaction, state, step, "c")

    if action == "submit":
        if first_incomplete(state) != "confirm":
            return await _ephemeral(interaction, "未入力の項目があります。")
        state["status"] = "submitted"
        await _show(interaction, state)
        title = "🧩 新しいご相談" if state["answers"].get("category") == "cheat" else "🔔 新規注文"
        await notify_staff(interaction.guild, channel, message, state, title)
        logger.info("注文確定: #%s owner=%s", state["order_id"], owner_id)
        return

    if action == "report":
        if state["status"] != "submitted" or state["answers"].get("category") != "boost":
            return await _ephemeral(interaction, "現在この操作はできません。")
        state["status"] = "reported"
        await _show(interaction, state)
        await notify_staff(interaction.guild, channel, message, state, "💸 入金報告がありました")
        return

    if action == "cancel":         # ord:cancel:<戻り先>
        if state["status"] not in ("draft", "submitted"):
            return await _ephemeral(
                interaction,
                "入金報告後のキャンセルはスタッフが対応します。このチャンネルでお知らせください。",
            )
        embed, view = render_cancel_confirm(state, parts[2])
        await interaction.response.edit_message(embed=embed, view=view)
        return

    if action == "cxn":            # キャンセルをやめて元の画面へ
        back_to = parts[2]
        if back_to == "pay" or state["status"] != "draft":
            return await _show(interaction, state)
        return await _show(interaction, state, back_to)

    if action == "cxy":            # キャンセル確定
        previous = state["status"]
        if previous not in ("draft", "submitted"):
            return await _ephemeral(interaction, "現在の状態ではキャンセルできません。")
        state["status"] = "cancelled"
        await _show(interaction, state)
        if previous == "submitted":
            await notify_staff(interaction.guild, channel, message, state, "✖️ お客様が注文を取り消しました")
        return

    if action == "restart":
        if state["status"] not in ("cancelled", "done"):
            return await _ephemeral(interaction, "現在の注文が進行中です。")
        if state["status"] == "done":
            # 完了した注文の記録は残し、新しいパネルを別メッセージで出す
            await interaction.response.defer()
            await send_order_panel(channel, owner_id)
            return
        return await _show(interaction, new_order_state(owner_id))

    await _ephemeral(interaction, "不明な操作です。")


async def handle_staff_action(interaction, parts):
    # ord:s:<action>:<channel_id>:<message_id>
    if not is_staff(interaction.user):
        await _ephemeral(interaction, "スタッフのみ操作できます。")
        return

    action = parts[2]
    try:
        channel_id, message_id = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await _ephemeral(interaction, "注文情報が壊れています。")
        return

    transitions = {
        "paid": ({"submitted", "reported"}, "paid"),
        "done": ({"paid"}, "done"),
        "cancel": ({"draft", "submitted", "reported", "paid"}, "cancelled"),
    }
    if action not in transitions:
        await _ephemeral(interaction, "不明な操作です。")
        return

    await interaction.response.defer()

    async with order_locks[message_id]:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        try:
            panel = await channel.fetch_message(message_id)
        except discord.NotFound:
            await _ephemeral(interaction, "受付パネルが見つかりません（削除された可能性があります）。")
            return

        state = parse_order_message(panel)
        if state is None:
            await _ephemeral(interaction, "受付データを読み込めませんでした。")
            return

        allowed, new_status = transitions[action]
        if state["status"] not in allowed:
            await _ephemeral(
                interaction,
                f"現在の状態（{STATUS_TEXT[state['status']]}）ではこの操作はできません。",
            )
        else:
            state["status"] = new_status
            embed, view = render_panel(state)
            await panel.edit(embed=embed, view=view)

            owner = f"<@{state['owner_id']}>" if state.get("owner_id") else ""
            is_cheat = state["answers"].get("category") == "cheat"
            notices = {
                "paid": (
                    f"{owner} スタッフが対応を開始しました。"
                    if is_cheat else
                    f"{owner} 入金を確認しました。代行を開始します！"
                ),
                "done": f"{owner} ご依頼が完了しました。ご利用ありがとうございました！",
                "cancelled": f"{owner} ご注文 `#{state['order_id']}` はスタッフによりキャンセルされました。",
            }
            await safe_send(channel, notices[new_status])
            logger.info(
                "注文 #%s を %s に変更 (staff=%s)",
                state["order_id"], new_status, interaction.user.id,
            )

        title = interaction.message.embeds[0].title if interaction.message.embeds else "注文"
        await interaction.edit_original_response(
            embed=build_staff_embed(state, channel, title, panel.jump_url, interaction.user),
            view=staff_view(state, channel_id, message_id),
        )


@bot.listen("on_interaction")
async def on_order_interaction(interaction):
    if interaction.type not in (
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    ):
        return

    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("ord:"):
        return

    parts = custom_id.split(":")
    try:
        if parts[1] == "s":
            await handle_staff_action(interaction, parts)
            return

        lock_key = interaction.message.id if interaction.message else interaction.channel_id
        async with order_locks[lock_key]:
            await handle_customer_action(interaction, parts)

    except discord.HTTPException as error:
        log_http_error("注文受付の操作", error)
        await _ephemeral(interaction, "通信エラーが発生しました。もう一度お試しください。")
    except Exception as error:
        log_unexpected_error("注文受付の操作", error)
        await _ephemeral(interaction, "エラーが発生しました。スタッフにお問い合わせください。")


# ==========================================
# 15. チケットパネル
# ==========================================

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チケットを作成",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="ticket:create",
    )
    async def create_ticket(self, interaction, button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message(
                "サーバー内で使用してください。",
                ephemeral=True,
            )
            return

        for channel in guild.text_channels:
            if is_ticket(channel) and get_ticket_owner(channel) == user.id:
                await interaction.response.send_message(
                    f"すでにチケットがあります：{channel.mention}",
                    ephemeral=True,
                )
                return

        try:
            category = discord.utils.get(guild.categories, name=TICKET_CATEGORY)
            if category is None:
                category = await guild.create_category(TICKET_CATEGORY)

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                ),
            }

            for role_id in STAFF_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_channels=True,
                    )

            channel = await guild.create_text_channel(
                name=f"ticket-{user.name}",
                category=category,
                overwrites=overwrites,
                topic=f"ticket_owner:{user.id}",
            )

            await interaction.response.send_message(
                f"チケットを作成しました：{channel.mention}",
                ephemeral=True,
            )

            welcome = discord.Embed(
                title="お問い合わせありがとうございます",
                description=(
                    "下の **受付パネル** のボタンを順番に選ぶだけで、ご注文が完了します。\n"
                    "料金は料金表に基づいて自動で計算されます。\n\n"
                    "◆ 内容の変更・戻る・キャンセルはいつでも可能です\n"
                    "◆ パネルはご本人様のみ操作できます\n"
                    "◆ 料金表にないご相談はスタッフが個別に対応します"
                ),
                color=COLOR_DEEP,
            )
            welcome.set_author(name=f"◆ {BRAND_NAME}　｜　SUPPORT")
            await channel.send(
                f"{user.mention}",
                embed=welcome,
                view=OpenTicketView(),
            )
            await send_order_panel(channel, user.id)

        except discord.HTTPException as error:
            log_http_error("チケット作成", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "チケットの作成に失敗しました。スタッフに確認してください。",
                    ephemeral=True,
                )


# ==========================================
# 16. チケット操作
# ==========================================

class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チケットを閉じる",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket:close",
    )
    async def close_ticket(self, interaction, button):
        channel = interaction.channel
        owner_id = get_ticket_owner(channel)

        if not is_staff(interaction.user) and interaction.user.id != owner_id:
            await interaction.response.send_message(
                "この操作は許可されていません。",
                ephemeral=True,
            )
            return

        if owner_id:
            owner = await fetch_ticket_owner_member(channel.guild, owner_id)
            if owner is not None:
                await channel.set_permissions(
                    owner,
                    send_messages=False,
                )

        await interaction.response.send_message(
            "チケットを閉じました。",
            view=ClosedTicketView(),
        )


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="再開する",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="ticket:reopen",
    )
    async def reopen_ticket(self, interaction, button):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "スタッフのみ操作できます。",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        owner_id = get_ticket_owner(channel)

        if owner_id:
            owner = await fetch_ticket_owner_member(channel.guild, owner_id)
            if owner is not None:
                await channel.set_permissions(
                    owner,
                    send_messages=True,
                )

        await interaction.response.send_message(
            "チケットを再開しました。",
            view=OpenTicketView(),
        )

    @discord.ui.button(
        label="チケットを削除",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="ticket:delete",
    )
    async def delete_ticket(self, interaction, button):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "スタッフのみ操作できます。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("チケットを削除します。")
        await interaction.channel.delete()


# ==========================================
# 17. 起動・接続イベント
# ==========================================

@bot.event
async def on_connect():
    logger.info("Discord Gatewayへの接続を確認しました。")


@bot.event
async def on_ready():
    logger.info("================================")
    logger.info("Bot起動成功: %s", bot.user)
    logger.info("AI: %s", "有効" if ai_enabled else "無効")
    logger.info("Geminiモデル: %s", AI_MODEL)
    logger.info("スタッフ通知先: %s", STAFF_CHANNEL_ID or "各チケット内")
    logger.info("PayPayリンク: %s", "設定済み" if (PAYPAY_LINK or PAYPAY_LINKS) else "未設定")
    logger.info("================================")


# ==========================================
# 18. スタッフ用コマンド
# ==========================================

@bot.command()
async def setup_ticket(ctx):
    if not is_staff(ctx.author):
        return

    embed = discord.Embed(
        title="お問い合わせ・ご注文",
        description=(
            "下のボタンを押すと、あなた専用のチケットが作成されます。\n"
            f"{DIVIDER}\n"
            "⚡ **代行**　─　トロ上げ / ガチバトル（料金自動計算）\n"
            "🧩 **チート**　─　スタッフが個別にご案内\n"
            f"{DIVIDER}\n"
            "ボタンを選ぶだけで、ご注文からお支払いまで完結します。"
        ),
        color=COLOR_MAIN,
    )
    embed.set_author(name=f"◆ {BRAND_NAME}")
    if ORDER_BANNER_URL:
        embed.set_image(url=ORDER_BANNER_URL)

    await ctx.send(embed=embed, view=TicketPanel())


@bot.command()
async def order_panel(ctx):
    """チケット内に受付パネルを出し直す（スタッフ用）。"""
    if not is_staff(ctx.author):
        return
    owner_id = get_ticket_owner(ctx.channel)
    if owner_id is None:
        await ctx.send("このコマンドはチケット内で使用してください。")
        return
    await send_order_panel(ctx.channel, owner_id)


@bot.command()
async def ai_on(ctx):
    global ai_enabled
    if not is_staff(ctx.author):
        return
    ai_enabled = True
    await ctx.send("AIを有効にしました。")


@bot.command()
async def ai_off(ctx):
    global ai_enabled
    if not is_staff(ctx.author):
        return
    ai_enabled = False
    await ctx.send("AIを無効にしました。")


@bot.command()
async def ai_resume(ctx):
    if not is_staff(ctx.author):
        return

    paused_channels.discard(ctx.channel.id)
    handoff_notified.discard(ctx.channel.id)
    histories.pop(ctx.channel.id, None)

    await ctx.send("このチャンネルのAIを再開しました。")


# ==========================================
# 19. メッセージ処理
# ==========================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.content.startswith("!"):
        return

    channel = message.channel
    ticket = is_ticket(channel)

    if ticket and is_staff(message.author):
        paused_channels.add(channel.id)
        return

    if not ai_enabled:
        return

    if channel.id in paused_channels:
        return

    if message.guild is None:
        return

    # チケット受付はボタン式に移行したため、チケット内でもAIが勝手に
    # 受付を始めることはない。どのチャンネルでも、Botへのメンションか
    # Botへの返信があったときだけAIが回答する。
    mentioned = bot.user is not None and bot.user in message.mentions
    replied = False

    if message.reference:
        referenced = message.reference.resolved
        if (
            isinstance(referenced, discord.Message)
            and bot.user is not None
            and referenced.author.id == bot.user.id
        ):
            replied = True

    if not mentioned and not replied:
        return

    # 料金の自動判定
    try:
        rank_price = calculate_rank_price(message.content)
        if rank_price is not None:
            await safe_send(
                channel,
                f"料金表に基づく見積もりは **{rank_price:,}円** です。\n"
                "ご依頼内容に間違いがないか確認してください。",
            )
            return

        trophy_price = calculate_trophy_price(message.content)
        if trophy_price is not None:
            await safe_send(
                channel,
                f"料金表に基づく見積もりは **{trophy_price:,}円** です。\n"
                "ご依頼内容に間違いがないか確認してください。",
            )
            return

    except Exception as error:
        log_unexpected_error("料金判定", error)

    # Gemini AIに問い合わせ
    async with channel_locks[channel.id]:
        if channel.id in paused_channels:
            return

        try:
            async with channel.typing():
                answer = await ask_ai(channel.id, message.content)

        except Exception as error:
            log_unexpected_error("Gemini AI処理", error)

            await safe_send(
                channel,
                "申し訳ありません。AIの応答でエラーが発生しました。\n"
                "スタッフに確認を依頼してください。",
            )

            if channel.id not in handoff_notified:
                mentions = staff_mentions(message.guild)
                await safe_send(
                    channel,
                    f"⚠️ **スタッフの対応をお願いします。**\n{mentions}",
                )
                handoff_notified.add(channel.id)
            return

        # スタッフへの引き継ぎ
        if "[STAFF_HANDOFF]" in answer:
            answer = answer.replace("[STAFF_HANDOFF]", "").strip()
            await send_long_message(channel, answer)

            if channel.id not in handoff_notified:
                mentions = staff_mentions(message.guild)
                await safe_send(
                    channel,
                    f"⚠️ **スタッフへの確認が必要です。**\n{mentions}",
                )
                handoff_notified.add(channel.id)
            return

        await send_long_message(channel, answer)


# ==========================================
# 20. Discordイベントエラー
# ==========================================

@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception("Discordイベントエラー: %s", event_method)


# ==========================================
# 21. Bot起動
# ==========================================

logger.info("Botを起動します...")
logger.info("Python version: %s", sys.version.split()[0])
logger.info("discord.py version: %s", discord.__version__)
logger.info("Gemini model setting: %s", AI_MODEL)

try:
    bot.run(DISCORD_TOKEN, log_handler=None)

except discord.HTTPException as error:
    # HTMLの全文を表示するとRenderログが埋まるため、要点だけ記録する
    log_http_error("Bot起動・Discord API通信", error)
    logger.critical(
        "Discord API通信で起動に失敗しました。上のHTTP status・Ray IDを確認してください。"
    )
    sys.exit(1)

except discord.LoginFailure as error:
    logger.critical("Discordトークンが正しくありません: %s", str(error))
    sys.exit(1)

except Exception as error:
    log_unexpected_error("Bot起動中", error)
    logger.critical("Bot起動中に致命的なエラーが発生しました。")
    sys.exit(1)
