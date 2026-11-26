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

from anti_nuke import setup_anti_nuke


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
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKENが設定されていません。")


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

# ---- 無言チケット・注文なしクローズの罰 ----
NO_ORDER_ROLE_ID = 1575153786266325062        # 付けるロール
NO_ORDER_NOTICE_CHANNEL_ID = 1575153532619726908  # 警告を送るチャンネル
NO_ORDER_NOTICE_INTERVAL = 10                 # 警告メッセージの送信間隔（秒）
SILENT_TICKET_HOURS = 24                      # この時間注文がなければ無言チケット
SILENT_CHECK_INTERVAL = 600                   # 無言チケットを確認する間隔（秒）
SILENT_TOPIC_FLAG = ":silent"                 # 無言で自動クローズ済みの印（チャンネルトピック）

# ---- Nuke対策 ----
# NUKE_BACKUP_USER_ID     : バックアップと検知通知を送る相手（DM）
# NUKE_WHITELIST_BOT_IDS  : 検知の対象外にするBotのID（カンマ区切り・任意）
NUKE_BACKUP_USER_ID = _parse_int_env("NUKE_BACKUP_USER_ID") or 1462791798891090084
NUKE_WHITELIST_BOT_IDS = {
    int(x) for x in os.getenv("NUKE_WHITELIST_BOT_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

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

        # 無言チケットの巡回（24時間注文がないチケットを自動で閉じて罰ロールを付ける）
        self.loop.create_task(silent_ticket_loop())

        # Nuke対策（/nuke対策）。バックアップと通知は NUKE_BACKUP_USER_ID のDMに送る。
        # チケットは開閉で頻繁に増減するため、保存・復元の対象から外す。
        setup_anti_nuke(
            self,
            backup_user_id=NUKE_BACKUP_USER_ID,
            exclude_channel=is_ticket,
            whitelist_bot_ids=NUKE_WHITELIST_BOT_IDS,
        )
        try:
            synced = await self.tree.sync()
            logger.info("スラッシュコマンドを同期しました: %s件", len(synced))
        except discord.HTTPException as error:
            log_http_error("スラッシュコマンドの同期", error)


bot = SupportBot(
    command_prefix="!",
    intents=intents,
)


# ==========================================
# 5. 注文処理ロック
# ==========================================

order_locks = defaultdict(asyncio.Lock)
# チケット作成の連打・同時押しで同じ利用者のチケットが複数作られないよう、
# (サーバーID, ユーザーID) ごとに作成処理を直列化する。
ticket_create_locks = defaultdict(asyncio.Lock)


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

# 料金表に直接記載された区間（rank_quote ではこちらを最優先で使う）
RANK_PRICES = {
    (0, 1): 900,      # エリート1〜レジェンド1
    (1, 2): 1000,     # レジェンド1〜レジェンド2
    (2, 3): 1200,     # レジェンド2〜レジェンド3
    (1, 4): 4200,     # レジェンド1〜マスター1
    (2, 4): 3200,     # レジェンド2〜マスター1
    (3, 4): 2000,     # レジェンド3〜マスター1
    (4, 5): 8000,     # マスター1〜マスター2
    (5, 6): 20000,    # マスター2〜マスター3
    (6, 7): 40000,    # マスター3〜プロ
}

# 連結して自動計算してよい区間（エリート〜レジェンド帯〜マスター1）。
# 例：レジェンド1〜レジェンド3 = 1,000 + 1,200 = 2,200円。
# マスター帯（マスター1以降）は連結しない。
RANK_SEGMENTS = {
    (0, 1): 900,
    (1, 4): 4200,
    (1, 2): 1000,
    (2, 3): 1200,
    (3, 4): 2000,
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

    # ボタン式受付と同じ計算（直接区間を優先し、レジェンド帯は連結して合計）
    return rank_quote(start, goal)


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

CATEGORY_LABELS = {"boost": "代行", "cheat": "チート", "middle": "仲介"}
SERVICE_LABELS = {"trophy": "トロ上げ代行", "ranked": "ガチバ代行"}
PAYMENT_LABELS = {"paypay": "PayPay", "ltc": "LTC（仮想通貨）"}
LTC_ADDRESS = "LbuHqe7Uy5NgqGYi2AT544aY41CcYiSMcs"
# PayPay受け取りリンクのDM送信先（区分ごと）
PAYPAY_BOOST_USER_ID = 1318679308688036023   # 「代行」のリンク受け取り先
PAYPAY_CHEAT_USER_ID = 1462791798891090084   # 「チート」のリンク受け取り先
# 「代行」のリンクを PAYPAY_BOOST_USER_ID へ送ったことを知らせる相手（金額などを通知）
PAYPAY_NOTIFY_USER_ID = 1462791798891090084
PAYPAY_RECIPIENT_IDS = {PAYPAY_BOOST_USER_ID, PAYPAY_CHEAT_USER_ID}
# 「仲介」: 手数料は固定。PayPayリンクは代行・チート両方の受け取り先へDMする
MIDDLE_FEE = 200
PAYPAY_MIDDLE_USER_IDS = (PAYPAY_BOOST_USER_ID, PAYPAY_CHEAT_USER_ID)
# 「代行」の受け取り確認時に通知する取り分（2割）と、受け取り先の呼び名
BOOST_SHARE_PERCENT = 20
BOOST_RECIPIENT_NAME = "たまごっち"
PARTNER_ID_RE = re.compile(r"（(\d{15,20})）$")
PAYPAY_AMOUNT_MAX = 10_000_000
PAYPAY_URL_RE = re.compile(r"https?://pay\.paypay\.ne\.jp/[A-Za-z0-9_-]+", re.IGNORECASE)
DEVICE_LABELS = {"ios": "iOS", "android": "Android"}

STATUS_TEXT = {
    "draft": "入力中",
    "payment_select": "支払い方法選択中",
    "submitted": "受付済み",
    "link_sent": "PayPay受け取り確認待ち",
    "received": "PayPay受け取り確認済み",
    "reported": "送金報告済み",
    "paid": "対応中",
    "done": "完了",
    "cancelled": "キャンセル",
}
STATUS_FROM_TEXT = {v: k for k, v in STATUS_TEXT.items()}
# 旧バージョンのパネル表記も読めるようにしておく
STATUS_FROM_TEXT.setdefault("PayPayリンク転送済み", "link_sent")

# PayPay受け取りフローの二重送信・二重通知防止（プロセス内ガード）
# ※ 正式な状態はパネルEmbedのフッターで管理し、こちらは補助的なガード。
# 同じパネルで再注文（キャンセル後の「新しく注文」）された場合に新しい注文を妨げないよう、
# (パネルのメッセージID, 注文ID) の組で記録する。
paypay_dm_sent_panels = set()
paypay_confirmed_panels = set()


def paypay_recipient_ids(state):
    """区分に応じたPayPayリンクのDM送信先（複数可）。"""
    category = state["answers"].get("category")
    if category == "middle":
        return list(PAYPAY_MIDDLE_USER_IDS)
    if category == "cheat":
        return [PAYPAY_CHEAT_USER_ID]
    return [PAYPAY_BOOST_USER_ID]


def boost_share(price):
    """代行の金額のうち、通知先が受け取れる分（2割）。"""
    return price * BOOST_SHARE_PERCENT // 100


def partner_id_from(text):
    match = PARTNER_ID_RE.search(text or "")
    return int(match.group(1)) if match else None


def resolve_partner(guild, text):
    """入力されたユーザーネーム・メンション・IDから、サーバーのメンバーを探す。"""
    text = unicodedata.normalize("NFKC", text or "").strip().lstrip("@")
    if not text or guild is None:
        return None
    match = re.fullmatch(r"<@!?(\d+)>|(\d{15,20})", text)
    if match:
        return guild.get_member(int(match.group(1) or match.group(2)))
    member = guild.get_member_named(text)
    if member is not None:
        return member
    lowered = text.lower()
    for member in guild.members:
        names = (member.name, member.global_name, member.nick)
        if any(n and n.lower() == lowered for n in names):
            return member
    return None


def parse_yen_amount(text):
    """「3,000円」「¥3000」などの入力から金額（整数）を取り出す。不正なら None。"""
    cleaned = re.sub(r"[\s,，、¥￥円]", "", unicodedata.normalize("NFKC", text or ""))
    if not cleaned.isdigit():
        return None
    amount = int(cleaned)
    return amount if 1 <= amount <= PAYPAY_AMOUNT_MAX else None


def mask_paypay_link(_link=None):
    """チケット上に表示するための伏せ字。リンクの一部も出さない。"""
    return "`●●●●●●●●●●（伏せ字）`"

FIELD_LABELS = {
    "category": "区分",
    "service": "サービス",
    "start": "現在",
    "goal": "目標",
    "tag": "プレイヤータグ",
    "brawler": "対象キャラ",
    "payment": "お支払い方法",
    "note": "備考",
    "device": "端末",
    "detail": "相談内容",
    "partner": "取引相手",
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
        "info": "追加情報",
        "payment": "お支払い方法",
        "device": "ご利用端末",
        "detail": "ご相談内容",
        "partner": "取引相手",
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
    elif answers.get("category") == "middle":
        seq += ["partner"]
    elif answers.get("category") == "boost":
        seq += ["service", "start", "goal"]
        # ガチバ代行は現在ランク・目標ランクの入力後に注文内容を確定し、
        # 支払い方法は別メッセージの専用UIで選択する。
        if answers.get("service") == "trophy":
            seq += ["info"]
    seq.append("confirm")
    return seq


def order_quote(state):
    answers = state["answers"]
    if answers.get("category") == "middle":
        return MIDDLE_FEE
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
        if answers.get("service") == "ranked":
            return True
        return bool(answers.get("tag"))
    if step == "device":
        return answers.get("device") in DEVICE_LABELS
    if step == "detail":
        return bool(answers.get("detail"))
    if step == "partner":
        return partner_id_from(answers.get("partner")) is not None
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
    if key == "payment":
        return PAYMENT_LABELS.get(value)
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
    if key == "payment":
        return {v: k for k, v in PAYMENT_LABELS.items()}.get(text)
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
        return ["category", "device", "detail", "payment"]
    if category == "middle":
        return ["category", "partner", "payment"]
    if category == "boost":
        keys = ["category", "service"]
        service = answers.get("service")
        if service:
            keys += ["start", "goal"]
            if service == "trophy":
                keys += ["tag", "brawler"]
            keys += ["note", "payment"]
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
        ["受付", "入金確認", "対応中", "完了"]
        if is_cheat
        else ["注文受付", "入金確認", "対応中", "完了"]
    )
    reached = {
        "payment_select": 0,
        "submitted": 0,
        "link_sent": 1,
        "received": 1,
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
    elif category == "middle":
        embed.add_field(
            name="✦ 仲介手数料",
            value=box(f"¥ {MIDDLE_FEE:,}"),
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
            "　└ ご相談内容を伺い、スタッフが個別にご案内します\n"
            "🤝 **仲介**\n"
            f"　└ お取引の仲介をします（手数料 ¥{MIDDLE_FEE:,}）"
        )
        selected = answers.get("category")
        items += [
            _button("代行", f"ord:set:category:boost:{ret}", emoji="⚡", row=0,
                    style=_choice_style(selected, "boost", True)),
            _button("チート", f"ord:set:category:cheat:{ret}", emoji="🧩", row=0,
                    style=_choice_style(selected, "cheat", False)),
            _button("仲介", f"ord:set:category:middle:{ret}", emoji="🤝", row=0,
                    style=_choice_style(selected, "middle", False)),
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
        lines = ["下のボタンから追加情報を入力してください。\n"]
        if trophy:
            lines.append("◆ **プレイヤータグ**（必須）　例：`#ABC123`")
            lines.append("◆ **対象キャラ**（任意）")
        else:
            lines.append("◆ **プレイヤータグの入力は不要です**")
        lines += [
            "◆ **備考**（任意）　希望納期など",
            "",
            "🔐 パスワードや認証コードは**入力しないでください**。",
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

    elif step == "payment":
        title = f"{prefix}お支払い方法を選択してください"
        body = (
            "ご希望のお支払い方法を選んでください。\n\n"
            "💳 **PayPay**\n　└ お客様が専用フォームからリンクを送信します\n"
            "🪙 **LTC（仮想通貨）**\n　└ 指定のLTCアドレスをご案内します"
        )
        selected = answers.get("payment")
        items += [
            _button("PayPay", f"ord:set:payment:paypay:{ret}", emoji="💳", row=0,
                    style=_choice_style(selected, "paypay", True)),
            _button("LTC（仮想通貨）", f"ord:set:payment:ltc:{ret}", emoji="🪙", row=0,
                    style=_choice_style(selected, "ltc", True)),
        ]
        show_next = step_done(state, step)

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

    elif step == "partner":
        title = f"{prefix}取引相手を入力してください"
        current = answers.get("partner")
        body = (
            "下のボタンから、取引したい相手の **ユーザーネーム** を入力してください。\n"
            "相手はこのサーバーに参加している必要があります。\n\n"
            f"◆ 仲介手数料：**¥{MIDDLE_FEE:,}**（PayPay）\n"
            "◆ PayPayリンクの送信後、取引相手がこのチケットを見られるようになり、スタッフが対応します。"
        )
        if current:
            body += f"\n\n現在の取引相手：**{current}**"
        done = step_done(state, step)
        items.append(_button(
            "取引相手を変更する" if done else "取引相手を入力する",
            f"ord:open:partner:{ret}",
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary,
            emoji="🤝",
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
            "送信後、担当スタッフが料金を個別にご案内します。\n"
            "あわせて、お支払い専用の画面を別に表示します。"
        )
        submit_label = "スタッフへ送信"
    else:
        price = order_quote(state)
        title = "ご注文内容の最終確認"
        body = (
            "以下の内容でお間違いなければ **注文を確定** を押してください。\n\n"
            f"### お見積り金額　¥{price:,}\n"
            "注文確定後、お支払い専用の画面を別に表示します。"
        )
        submit_label = "注文を確定"

    options = []
    for step in step_sequence(state)[:-1]:
        current = None
        if step == "info":
            current = display_value(state, "tag") or display_value(state, "note")
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

    # チートは料金を個別案内するため、金額はスタッフの案内に従ってもらう
    if is_cheat:
        price_heading = (
            "### お支払い金額　スタッフがご案内した金額\n"
            "※ 料金はスタッフがこのチケットで個別にご案内します。金額の案内を受けてからお支払いください。\n"
        )
    elif status in ("payment_select", "submitted"):
        price_heading = f"### お支払い金額　¥{order_quote(state):,}\n"
    else:
        price_heading = ""
    # 旧フロー（支払い方法なし）で送信済みのチート相談は、従来の画面のまま扱う
    has_payment_flow = not is_cheat or bool(state["answers"].get("payment"))

    if status == "payment_select" and state["answers"].get("category") == "middle":
        title = "お支払い専用メニュー"
        body = (
            f"{price_heading}"
            "仲介手数料のお支払いはPayPayのみです。\n\n"
            "💳 **PayPay**　選択すると受け取りリンクの入力画面が開きます。リンクはチケットに表示されません。\n"
            "リンクを送信すると、取引相手がこのチケットを見られるようになり、スタッフが対応します。"
        )
        items.extend([
            _button("PayPay", "ord:pay:paypay", style=discord.ButtonStyle.primary, emoji="💳", row=0),
            _button("注文を取り消す", "ord:cancel:payment", emoji="✖️", row=1),
        ])

    elif status == "payment_select":
        title = "お支払い専用メニュー"
        body = (
            f"{price_heading}"
            "この画面はお支払い専用です。ご希望の方法を選択してください。\n\n"
            "💳 **PayPay**　選択すると受け取りリンクの入力画面が開きます。リンクはチケットに表示されません。\n"
            "🪙 **LTC（仮想通貨）**　送金先アドレスを表示します。送金後はお客様ご自身で報告してください。"
        )
        items.extend([
            _button("PayPay", "ord:pay:paypay", style=discord.ButtonStyle.primary, emoji="💳", row=0),
            _button("LTC（仮想通貨）", "ord:pay:ltc", style=discord.ButtonStyle.secondary, emoji="🪙", row=0),
            _button("注文を取り消す", "ord:cancel:payment", emoji="✖️", row=1),
        ])

    elif status == "submitted" and has_payment_flow:
        payment = state["answers"].get("payment")
        title = "お見積り・お支払いのご案内"
        if payment == "ltc":
            body = (
                f"{price_heading}"
                "**お支払い方法：LTC（仮想通貨）**\n\n"
                "下記のアドレスにLTCで送金してください。\n"
                f"```\n{LTC_ADDRESS}\n```\n"
                "送金先アドレス `LbuHqe7Uy5NgqGYi2AT544aY41CcYiSMcs` を一文字も間違えずに送金しましたか？\n"
                "送金が完了したら、下の「はい、送金しました」を押してください。\n\n"
                "※ スタッフが送金状況を確認します。"
            )
            items.append(_button("LTCアドレスを表示", "ord:copy:ltc", emoji="📋", row=0))
            items.append(_button(
                "はい、送金しました", "ord:report",
                style=discord.ButtonStyle.primary, emoji="✅", row=0,
            ))
        else:
            body = (
                f"{price_heading}"
                "**お支払い方法：PayPay**\n\n"
                "下の「PayPayリンクを入力・送信」ボタンから、PayPay受け取りリンクを入力してください。\n"
                "送信されたリンクは指定アカウントのDMにのみ転送され、チケットにはリンク自体を表示しません。\n\n"
                "※ リンクはチャット欄に直接貼らず、必ず専用フォームから送信してください。"
            )
            items.append(_button(
                "PayPayリンクを入力・送信", "ord:p:paypay_open",
                style=discord.ButtonStyle.primary, emoji="💳", row=0,
            ))
        items.append(_button("注文を取り消す", "ord:cancel:pay", emoji="✖️", row=1))

    elif status == "link_sent":
        title = "PayPayの受け取り確認中です"
        body = (
            "PayPay受け取りリンクを受け付け、担当アカウントへ送信しました。\n"
            f"リンク：{mask_paypay_link()}\n\n"
            "現在、担当者が受け取りを確認しています。\n"
            "受け取りが確認されると、スタッフが次の対応へ進みます。しばらくお待ちください。"
        )
        color = COLOR_GLOW

    elif status == "received":
        title = "PayPayの受け取りを確認しました"
        body = (
            "担当者がPayPayの受け取りを確認しました。\n"
            "スタッフが対応を開始するまで、しばらくお待ちください。"
        )
        color = COLOR_GLOW

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
            if is_cheat and not has_payment_flow else
            "入金を確認しました。担当スタッフが対応を開始します。\n"
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
    if answers.get("service") == "trophy":
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


def build_partner_modal(state, ret):
    modal = discord.ui.Modal(
        title="取引相手の入力",
        custom_id=f"ord:modal:partner:{ret}",
        timeout=600,
    )
    current = state["answers"].get("partner") or ""
    modal.add_item(discord.ui.TextInput(
        label="取引相手のユーザーネーム",
        custom_id="partner",
        placeholder="例：tamagotchi_123（@は不要です）",
        default=PARTNER_ID_RE.sub("", current) or None,
        required=True,
        max_length=40,
    ))
    return modal


class PayPayLinkModal(discord.ui.Modal):
    def __init__(self, channel_id, message_id, *, ask_amount=False):
        super().__init__(
            title="PayPay受け取りリンクの入力",
            custom_id=f"ord:p:paypay_submit:{channel_id}:{message_id}",
            timeout=300,
        )
        self.add_item(discord.ui.TextInput(
            label="PayPay受け取りリンク",
            custom_id="paypay_url",
            placeholder="https://pay.paypay.ne.jp/...",
            required=True,
            max_length=300,
        ))
        if ask_amount:
            # チートは料金が個別案内のため、送金額をお客様に入力してもらう
            self.add_item(discord.ui.TextInput(
                label="送金額（円）",
                custom_id="paypay_amount",
                placeholder="例：3000（スタッフからご案内した金額）",
                required=True,
                max_length=12,
            ))


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
    if state["answers"].get("category") in ("boost", "middle"):
        price = order_quote(state)
        embed.add_field(
            name="金額",
            value=box(f"¥ {price:,}" if price is not None else EMPTY),
            inline=False,
        )
        embed.add_field(
            name="お支払い方法",
            value=box(PAYMENT_LABELS.get(state["answers"].get("payment"), EMPTY)),
            inline=False,
        )
    embed.set_footer(text=f"ORDER #{oid}（スタッフ控え）")
    return embed


def staff_view(state, channel_id, message_id):
    status = state["status"]
    is_cheat = state["answers"].get("category") == "cheat"
    suffix = f"{channel_id}:{message_id}"
    has_payment_flow = not is_cheat or bool(state["answers"].get("payment"))
    items = []
    if status == "payment_select":
        items.append(_button(
            "お客様のお支払い方法選択待ち",
            f"ord:s:noop:{suffix}",
            emoji="⏳", row=0, disabled=True,
        ))
        items.append(_button("注文取消", f"ord:s:cancel:{suffix}", emoji="✖️", row=1))
    elif status == "submitted" and has_payment_flow:
        if state["answers"].get("payment") == "paypay":
            items.append(_button(
                "お客様のPayPayリンク入力待ち",
                f"ord:s:noop:{suffix}",
                emoji="⏳", row=0, disabled=True,
            ))
        else:
            items.append(_button(
                "お客様のLTC送金報告待ち",
                f"ord:s:noop:{suffix}",
                emoji="⏳", row=0, disabled=True,
            ))
        items.append(_button("注文取消", f"ord:s:cancel:{suffix}", emoji="✖️", row=1))
    elif status == "link_sent":
        # DM送信済みだが、受け取り先ユーザーの確認はまだ。対応開始はさせない。
        items.append(_button(
            "PayPay受け取り確認待ち",
            f"ord:s:noop:{suffix}",
            emoji="⏳", row=0, disabled=True,
        ))
        items.append(_button("注文取消", f"ord:s:cancel:{suffix}", emoji="✖️", row=1))
    elif status in ("received", "reported") or status == "submitted":
        items.append(_button(
            "対応開始" if not has_payment_flow else "入金確認・対応開始",
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
    # interaction.message は押した時点のスナップショット。連打やスタッフ操作の直後に
    # 古い状態で処理しないよう、ロック取得後（呼び出し元）に最新のパネルを取り直す。
    # 取得に失敗した場合は古い状態で続行せず、呼び出し元の例外処理で
    # 「通信エラーが発生しました。もう一度お試しください。」を案内する。
    if message is not None and channel is not None:
        message = await channel.fetch_message(message.id)
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
        allowed = {"category": CATEGORY_LABELS, "service": SERVICE_LABELS, "device": DEVICE_LABELS, "payment": PAYMENT_LABELS}
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
        if key == "info":
            modal = build_info_modal(state, ret)
        elif key == "partner":
            modal = build_partner_modal(state, ret)
        else:
            modal = build_detail_modal(state, ret)
        await interaction.response.send_modal(modal)
        return

    if action == "modal":          # ord:modal:<info|detail>:<ret>
        key, ret = parts[2], parts[3]
        values = modal_values(interaction.data)
        if key == "info":
            if state["answers"].get("service") == "trophy":
                tag = normalize_tag(values.get("tag"))
                if tag is None:
                    return await _ephemeral(
                        interaction,
                        "プレイヤータグの形式が正しくありません。`#` と英数字（例：`#ABC123`）で入力してください。",
                    )
                state["answers"]["tag"] = tag
                state["answers"]["brawler"] = clean_text(values.get("brawler"), 40) or "なし"
            else:
                state["answers"].pop("tag", None)
                state["answers"].pop("brawler", None)
            state["answers"]["note"] = clean_text(values.get("note"), 300) or "なし"
        elif key == "partner":
            partner = resolve_partner(interaction.guild, clean_text(values.get("partner"), 40))
            if partner is None:
                return await _ephemeral(
                    interaction,
                    "そのユーザーネームの人がこのサーバーに見つかりませんでした。\n"
                    "相手のユーザーネーム（表示名ではなく `@` の後ろの名前）を確認して、もう一度入力してください。",
                )
            if partner.id == owner_id or partner.bot:
                return await _ephemeral(interaction, "取引相手には、ご自身とBot以外のユーザーを入力してください。")
            state["answers"]["partner"] = f"{partner.name}（{partner.id}）"
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
        is_cheat = state["answers"].get("category") == "cheat"
        # 代行・チート共通：注文内容を確定し、支払い方法は別メッセージの専用UIで選択する
        state["status"] = "payment_select"
        intake_embed = build_order_embed(
            state,
            "ご相談内容を受け付けました" if is_cheat else "注文内容を受け付けました",
            (
                "ご相談内容をスタッフへ送信しました。料金はスタッフがこのチャンネルで個別にご案内します。\n"
                "金額の案内後、下に表示された別の「お支払い専用メニュー」から、お支払い方法を選択してください。"
                if is_cheat else
                "注文内容の入力が完了しました。\n下に表示された別の「お支払い専用メニュー」から、お支払い方法を選択してください。"
            ),
            color=COLOR_GLOW,
        )
        await interaction.response.edit_message(embed=intake_embed, view=None)
        payment_embed, payment_view = render_panel(state)
        payment_message = await safe_send(
            channel,
            f"<@{owner_id}> お支払い専用メニューです。",
            embed=payment_embed,
            view=payment_view,
        )
        if payment_message is None:
            await safe_send(channel, "⚠️ お支払い画面を作成できませんでした。スタッフにお知らせください。")
            return
        if is_cheat:
            # チートは料金が個別案内のため、相談内容はこの時点でスタッフへ通知する
            await notify_staff(interaction.guild, channel, payment_message, state, "🧩 新しいご相談")
        logger.info("注文確定: #%s owner=%s", state["order_id"], owner_id)
        return

    if action == "pay":
        payment = parts[2] if len(parts) > 2 else None
        if state["status"] != "payment_select" or payment not in PAYMENT_LABELS:
            return await _ephemeral(interaction, "この画面では支払い方法を選択できません。")
        if state["answers"].get("category") == "middle" and payment != "paypay":
            return await _ephemeral(interaction, "仲介のお支払いはPayPayのみです。")
        if payment == "paypay":
            # PayPay選択だけでは処理を完了させず、すぐに受け取りリンクの入力画面を開く。
            # 状態の更新・スタッフ通知はリンク送信後の段階で行う（ここでは通知しない）。
            await interaction.response.send_modal(PayPayLinkModal(
                channel.id, message.id,
                ask_amount=state["answers"].get("category") == "cheat",
            ))
            return
        state["answers"]["payment"] = payment
        state["status"] = "submitted"
        await _show(interaction, state)
        if payment == "ltc":
            await safe_send(
                channel,
                f"<@{owner_id}> LTC送金先アドレスを表示しました。送金後、専用画面の「はい、送金しました」を押してください。",
            )
        return

    if action == "copy":
        if parts[2] == "ltc":
            return await _ephemeral(interaction, f"LTCアドレス：\n`{LTC_ADDRESS}`")
        return await _ephemeral(interaction, "コピー対象が不正です。")

    if action == "report":
        if (
            state["status"] != "submitted"
            or state["answers"].get("payment") != "ltc"
        ):
            return await _ephemeral(interaction, "LTCを選択した注文でのみ、この操作を利用できます。")
        state["status"] = "reported"
        await _show(interaction, state)
        await notify_staff(interaction.guild, channel, message, state, "💸 入金報告がありました")
        return

    if action == "cancel":         # ord:cancel:<戻り先>
        if state["status"] not in ("draft", "payment_select", "submitted"):
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
        if previous not in ("draft", "payment_select", "submitted"):
            return await _ephemeral(interaction, "現在の状態ではキャンセルできません。")
        state["status"] = "cancelled"
        await _show(interaction, state)
        # 代行のPayPayリンク入力待ち段階はスタッフへ未通知のため、取消通知も送らない。
        # チートは相談送信時点でスタッフへ通知済みのため、支払い前の取消も知らせる。
        if state["answers"].get("category") == "cheat":
            staff_knows = previous in ("payment_select", "submitted")
        else:
            staff_knows = previous == "submitted" and state["answers"].get("payment") != "paypay"
        if staff_knows:
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


def _paypay_dm_view(channel_id, message_id, *, confirmed=False, label=None, order_id=None):
    """受け取り先DMに付ける「受け取りました」ボタン。"""
    if confirmed:
        return _view([_button(
            label or "受け取り確認済み",
            "ord:p:noop",
            style=discord.ButtonStyle.success,
            emoji="✅", row=0, disabled=True,
        )])
    return _view([_button(
        "受け取りました",
        # 注文IDも含め、同じパネルで作り直された別の注文を誤って確認しないようにする
        f"ord:p:paypay_recv:{channel_id}:{message_id}" + (f":{order_id}" if order_id else ""),
        style=discord.ButtonStyle.success,
        emoji="✅", row=0,
    )])


async def _fetch_ticket_panel(channel_id, message_id):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if not is_ticket(channel):
        return channel, None
    try:
        panel = await channel.fetch_message(message_id)
    except discord.NotFound:
        return channel, None
    return channel, panel


def _paypay_link_input_allowed(state):
    """PayPayリンクを受け付けてよい状態か（支払い方法選択中 or PayPayリンク入力待ち）。"""
    if state is None or state["answers"].get("category") not in CATEGORY_LABELS:
        return False
    if state["status"] == "payment_select":
        return True
    return state["status"] == "submitted" and state["answers"].get("payment") == "paypay"


async def add_middle_partner(channel, panel, state, partner_id):
    """仲介：取引相手にチケットを見られるようにして、スタッフをメンションする。"""
    partner = await fetch_ticket_owner_member(channel.guild, partner_id) if partner_id else None
    if partner is None:
        await safe_send(
            channel,
            f"⚠️ 取引相手（{state['answers'].get('partner') or '不明'}）がサーバーに見つからないため、"
            f"チケットに追加できませんでした。{staff_mentions(channel.guild)}",
        )
        return
    try:
        await channel.set_permissions(
            partner,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
    except discord.HTTPException as error:
        log_http_error("仲介の取引相手をチケットに追加", error)
        await safe_send(
            channel,
            f"⚠️ 取引相手 {partner.mention} をチケットに追加できませんでした。{staff_mentions(channel.guild)}",
        )
        return
    staff_embed = build_staff_embed(state, channel, "🤝 仲介の取引が始まりました", panel.jump_url)
    await safe_send(
        channel,
        f"🤝 取引相手 {partner.mention} をこのチケットに追加しました。\n"
        f"スタッフの対応をお待ちください。{staff_mentions(channel.guild)}",
        embed=staff_embed,
        view=staff_view(state, channel.id, panel.id),
    )
    logger.info("仲介の取引相手を追加: #%s partner=%s", state["order_id"], partner.id)


async def notify_boost_share(state):
    """代行の受け取り確認時に、通知先へ金額と2割を知らせる。"""
    price = order_quote(state)
    if price is None:
        return
    share = boost_share(price)
    text = (
        f"<@{PAYPAY_NOTIFY_USER_ID}>\n"
        f"{BOOST_RECIPIENT_NAME}さんが¥{price:,}受け取りました。\n"
        f"あなたは¥{share:,}受け取れます。\n"
        f"¥{price:,}受け取れますが、その2割を俺に送って\n"
        f"注文ID：#{state['order_id']}"
    )
    try:
        user = bot.get_user(PAYPAY_NOTIFY_USER_ID) or await bot.fetch_user(PAYPAY_NOTIFY_USER_ID)
        await user.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException as error:
        log_http_error("代行の2割通知DM", error)


async def handle_customer_paypay_action(interaction, parts):
    """PayPay受け取りフロー。

    段階：
      1. 支払い方法でPayPay選択 → 入力フォーム（モーダル）表示
      2. リンク入力 → 検知 → 指定ユーザーへDM（メンション付き・確認ボタン付き）
         ※ この段階ではスタッフにメンションしない（状態：link_sent）
      3. DM受信者が「受け取りました」を押す → チケット内でスタッフにメンション（状態：received）
    """
    action = parts[2] if len(parts) > 2 else ""

    # ---- 入力フォームを開く（リンク入力待ち画面のボタン） ----
    if action == "paypay_open":
        message = interaction.message
        state = parse_order_message(message)
        if (not is_ticket(interaction.channel)
                or state is None
                or interaction.user.id != state.get("owner_id")
                or not _paypay_link_input_allowed(state)):
            return await _ephemeral(interaction, "この注文ではPayPayリンクを入力できません。")
        if _ticket_closed_for(interaction.channel, state["owner_id"]):
            return await _ephemeral(interaction, "このチケットはクローズされています。")
        await interaction.response.send_modal(PayPayLinkModal(
            interaction.channel.id, message.id,
            ask_amount=state["answers"].get("category") == "cheat",
        ))
        return

    # ---- リンク送信（モーダル送信） ----
    if action == "paypay_submit":
        try:
            channel_id, message_id = int(parts[3]), int(parts[4])
        except (IndexError, ValueError):
            return await _ephemeral(interaction, "注文情報を読み込めませんでした。")

        submitted_values = modal_values(interaction.data)
        raw_link = clean_text(submitted_values.get("paypay_url"), 300)
        match = PAYPAY_URL_RE.search(raw_link)
        if not match:
            return await _ephemeral(
                interaction,
                "PayPay受け取りリンクの形式が正しくありません。`https://pay.paypay.ne.jp/...` のリンクを入力してください。",
            )
        link = match.group(0)

        await interaction.response.defer(ephemeral=True)

        async with order_locks[message_id]:
            channel, panel = await _fetch_ticket_panel(channel_id, message_id)
            if panel is None:
                return await _ephemeral(interaction, "お支払い画面が見つかりません。スタッフにお知らせください。")

            state = parse_order_message(panel)
            if state is None or interaction.user.id != state.get("owner_id"):
                return await _ephemeral(interaction, "この注文ではPayPayリンクを受け付けられません。")
            if _ticket_closed_for(channel, state["owner_id"]):
                return await _ephemeral(interaction, "このチケットはクローズされています。")

            # 二重送信防止（状態とプロセス内ガードの両方で確認）
            if (message_id, state["order_id"]) in paypay_dm_sent_panels or state["status"] in ("link_sent", "received", "paid", "done"):
                return await _ephemeral(interaction, "PayPayリンクはすでに送信済みです。受け取り確認をお待ちください。")
            if not _paypay_link_input_allowed(state):
                return await _ephemeral(interaction, "この注文ではPayPayリンクを受け付けられません。")

            # 金額：代行は見積りから自動計算、チートはお客様がフォームに入力した送金額
            is_cheat = state["answers"].get("category") == "cheat"
            if is_cheat:
                price = parse_yen_amount(submitted_values.get("paypay_amount"))
                if price is None:
                    return await _ephemeral(
                        interaction,
                        "送金額の形式が正しくありません。スタッフからご案内した金額を数字で入力してください（例：`3000`）。",
                    )
            else:
                price = order_quote(state)
            recipient_ids = paypay_recipient_ids(state)
            is_middle = state["answers"].get("category") == "middle"
            category_label = CATEGORY_LABELS.get(state["answers"].get("category"), EMPTY)

            # 段階1：PayPay選択を確定し「リンク入力待ち」にする（DM失敗時はこの画面に留まる）
            if state["status"] == "payment_select":
                state["answers"]["payment"] = "paypay"
                state["status"] = "submitted"
                embed, view = render_panel(state)
                try:
                    await panel.edit(embed=embed, view=view)
                except discord.HTTPException as error:
                    log_http_error("PayPay選択の反映", error)
                    return await _ephemeral(interaction, "お支払い画面を更新できませんでした。もう一度お試しください。")

            # 段階2：指定ユーザーへDM送信（メンション付き・確認ボタン付き）
            partner_id = partner_id_from(state["answers"].get("partner")) if is_middle else None
            sent_to = []
            for recipient_id in recipient_ids:
                dm_text = (
                    f"<@{recipient_id}>\n"
                    "💳 **PayPay受け取りリンクが届きました**\n"
                    f"注文ID：#{state['order_id']}\n"
                    f"区分：{category_label}\n"
                )
                if price is not None:
                    dm_text += f"金額：¥{price:,}" + ("（お客様の申告額）\n" if is_cheat else "\n")
                    if state["answers"].get("category") == "boost":
                        dm_text += f"2割：¥{boost_share(price):,}\n"
                if partner_id:
                    dm_text += f"取引相手：<@{partner_id}>（ID: {partner_id}）\n"
                dm_text += (
                    f"チケット：{channel.mention}（{channel.name}）\n"
                    f"お客様：<@{state['owner_id']}>（ID: {state['owner_id']}）\n"
                    f"リンク：{link}\n\n"
                    "**受け取りましたか？**\n"
                    "受け取りが完了したら、下の「受け取りました」ボタンを押してください。"
                )
                try:
                    recipient = bot.get_user(recipient_id)
                    if recipient is None:
                        recipient = await bot.fetch_user(recipient_id)
                    await recipient.send(
                        dm_text,
                        view=_paypay_dm_view(channel.id, panel.id, order_id=state["order_id"]),
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                    sent_to.append(recipient_id)
                except discord.HTTPException as error:
                    log_http_error(f"PayPayリンクのDM送信（{recipient_id}）", error)

            if not sent_to:
                # 全員へのDMに失敗した場合は受け取り確認待ちに進めない（状態は「リンク入力待ち」のまま）
                return await _ephemeral(
                    interaction,
                    "⚠️ リンクの送信に失敗しました。リンクは転送されていません。\n"
                    "時間をおいて「PayPayリンクを入力・送信」から再度お試しいただくか、スタッフにお知らせください。",
                )

            paypay_dm_sent_panels.add((message_id, state["order_id"]))
            logger.info("PayPayリンクDM送信: #%s → %s", state["order_id"], sent_to)
            recipient_id = sent_to[0]

            # 代行のリンクを受け取り先へ送ったことを、通知先ユーザーへDMで知らせる
            # （リンク自体は含めない。失敗しても注文の進行は止めない）
            if (state["answers"].get("category") == "boost"
                    and recipient_id == PAYPAY_BOOST_USER_ID
                    and PAYPAY_NOTIFY_USER_ID != recipient_id):
                service_label = SERVICE_LABELS.get(state["answers"].get("service"), EMPTY)
                notify_text = (
                    f"<@{PAYPAY_NOTIFY_USER_ID}>\n"
                    f"📨 **<@{recipient_id}> にPayPayリンクを送信しました**\n"
                    f"金額：{f'¥{price:,}' if price is not None else '不明'}\n"
                    f"注文ID：#{state['order_id']}\n"
                    f"区分：{category_label}（{service_label}）\n"
                    f"チケット：{channel.mention}（{channel.name}）\n"
                    f"お客様：<@{state['owner_id']}>（ID: {state['owner_id']}）"
                )
                try:
                    notify_user = bot.get_user(PAYPAY_NOTIFY_USER_ID)
                    if notify_user is None:
                        notify_user = await bot.fetch_user(PAYPAY_NOTIFY_USER_ID)
                    await notify_user.send(
                        notify_text,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException as error:
                    log_http_error("PayPayリンク送信の通知DM", error)

            # DM送信成功 → 受け取り確認待ち（スタッフへのメンションはしない）
            state["status"] = "link_sent"
            embed, view = render_panel(state)
            try:
                await panel.edit(embed=embed, view=view)
            except discord.HTTPException as error:
                log_http_error("PayPay送信後の画面更新", error)
            await safe_send(
                channel,
                f"💳 <@{state['owner_id']}> PayPay受け取りリンクを受け付けました。\n"
                f"リンク：{mask_paypay_link()}\n"
                "現在、担当者が受け取りを確認しています。確認が取れ次第スタッフが対応しますので、しばらくお待ちください。",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            if is_middle:
                await add_middle_partner(channel, panel, state, partner_id)

        return await _ephemeral(
            interaction,
            "PayPay受け取りリンクを送信しました。チケットにはリンクを表示していません。受け取り確認をお待ちください。",
        )

    # ---- DM：「受け取りました」ボタン ----
    if action == "paypay_recv":
        # DMを受け取った本人だけが押せる
        # （送信先変更前に送ったDMのボタンも押せるよう、受け取り先のいずれかであればよい）
        if interaction.user.id not in PAYPAY_RECIPIENT_IDS:
            return await _ephemeral(interaction, "このボタンはPayPayリンクの受け取り先ご本人のみ操作できます。")
        try:
            channel_id, message_id = int(parts[3]), int(parts[4])
        except (IndexError, ValueError):
            return await _ephemeral(interaction, "注文情報を読み込めませんでした。")
        # 注文IDはこの修正以降に送ったDMにだけ含まれる（以前のDMは None）
        dm_order_id = parts[5] if len(parts) > 5 else None

        await interaction.response.defer()

        async with order_locks[message_id]:
            try:
                channel, panel = await _fetch_ticket_panel(channel_id, message_id)
            except discord.HTTPException as error:
                log_http_error("PayPay受け取り確認：チケット取得", error)
                channel, panel = None, None
            if panel is None:
                return await _ephemeral(interaction, "対象のチケットまたはお支払い画面が見つかりません。")

            state = parse_order_message(panel)
            if state is None:
                return await _ephemeral(interaction, "注文情報を読み込めませんでした。")

            # 二重通知防止
            if (message_id, state["order_id"]) in paypay_confirmed_panels or state["status"] in ("received", "paid", "done"):
                await interaction.edit_original_response(view=_paypay_dm_view(0, 0, confirmed=True))
                return await _ephemeral(interaction, "この注文はすでに受け取り確認済みです。")
            if state["status"] == "cancelled":
                await interaction.edit_original_response(
                    view=_paypay_dm_view(0, 0, confirmed=True, label="注文キャンセル済み"),
                )
                return await _ephemeral(interaction, "この注文はキャンセルされています。")
            if dm_order_id is not None and dm_order_id != state["order_id"]:
                return await _ephemeral(interaction, "このDMの注文は、現在のお支払い画面の注文と一致しません。")
            # このボタンはDM送信に成功した場合にだけ存在する。DM送信後のパネル更新
            # （link_sent の保存）に失敗して submitted のまま残った注文も、注文IDが
            # 一致するDMからに限り、ここで復旧できるようにする。
            allowed_status = ("link_sent", "submitted") if dm_order_id is not None else ("link_sent",)
            if (state["status"] not in allowed_status
                    or state["answers"].get("payment") != "paypay"):
                return await _ephemeral(interaction, "現在の状態では受け取り確認できません。")

            # 段階3：受け取り確認済みへ
            state["status"] = "received"
            embed, view = render_panel(state)
            try:
                await panel.edit(embed=embed, view=view)
            except discord.HTTPException as error:
                log_http_error("PayPay受け取り確認の反映", error)
                return await _ephemeral(interaction, "チケットの更新に失敗しました。もう一度ボタンを押してください。")

            paypay_confirmed_panels.add((message_id, state["order_id"]))
            logger.info("PayPay受け取り確認: #%s by %s", state["order_id"], interaction.user.id)

            # ボタン押下後にのみ、チケット内でスタッフへメンション
            # （仲介はリンク送信時にメンション済みのため、ここではメンションしない）
            is_middle = state["answers"].get("category") == "middle"
            mentions = "" if is_middle else staff_mentions(channel.guild)
            staff_embed = build_staff_embed(
                state, channel, "✅ PayPay受け取り確認済み", panel.jump_url, interaction.user,
            )
            await safe_send(
                channel,
                f"✅ **PayPayの受け取りが確認されました。対応をお願いします。** {mentions}".strip(),
                embed=staff_embed,
                view=staff_view(state, channel.id, panel.id),
            )

            if state["answers"].get("category") == "boost" and interaction.user.id == PAYPAY_BOOST_USER_ID:
                await notify_boost_share(state)

            await interaction.edit_original_response(view=_paypay_dm_view(0, 0, confirmed=True))

        return await _ephemeral(interaction, "受け取り確認を記録し、スタッフへ通知しました。")

    return await _ephemeral(interaction, "不明な操作です。")


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
        # PayPayは受け取り先ユーザーが「受け取りました」を押した後（received）のみ対応開始可
        "paid": ({"submitted", "received", "reported"}, "paid"),
        "done": ({"paid"}, "done"),
        "cancel": ({"draft", "payment_select", "submitted", "link_sent", "received", "reported", "paid"}, "cancelled"),
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
        if (
            action == "paid"
            and state["status"] == "submitted"
            and (
                state["answers"].get("category") == "boost"
                or state["answers"].get("payment")
            )
        ):
            await _ephemeral(interaction, "お客様のお支払い手続きが完了するまで、対応開始はできません。")
            return
        if action == "paid" and state["status"] == "link_sent":
            await _ephemeral(interaction, "PayPayの受け取り確認がまだ完了していません。受け取り先の確認をお待ちください。")
            return
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
                    if is_cheat and not state["answers"].get("payment") else
                    f"{owner} 入金を確認しました。対応を開始します！"
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
        if len(parts) > 1 and parts[1] == "p":
            await handle_customer_paypay_action(interaction, parts)
            return
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
# 14. チケットパネル
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

        # チャンネル作成は3秒の応答期限を超えることがあるため、先に本人だけに「考え中…」を出す。
        # 期限切れで応答に失敗すると、ウェルカムメッセージ・受付パネルが送られないチケットが残る。
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with ticket_create_locks[(guild.id, user.id)]:
            for channel in guild.text_channels:
                if is_ticket(channel) and get_ticket_owner(channel) == user.id:
                    await interaction.followup.send(
                        f"すでにチケットがあります：{channel.mention}",
                        ephemeral=True,
                    )
                    return

            notified = False
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

                notified = True
                try:
                    await interaction.followup.send(
                        f"チケットを作成しました：{channel.mention}",
                        ephemeral=True,
                    )
                except discord.HTTPException as error:
                    # 通知に失敗してもチケットの初期化（ウェルカム・受付パネル）は続ける
                    log_http_error("チケット作成の完了通知", error)

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
                if notified:
                    return  # 作成完了は通知済み（従来どおり失敗通知は重ねて出さない）
                try:
                    await interaction.followup.send(
                        "チケットの作成に失敗しました。スタッフに確認してください。",
                        ephemeral=True,
                    )
                except discord.HTTPException as notify_error:
                    log_http_error("チケット作成失敗の通知", notify_error)


# ==========================================
# 15. チケット操作
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

        # 本人が注文せずに閉じた場合は罰ロールを付ける（スタッフが閉じた場合は対象外）
        if owner_id and interaction.user.id == owner_id and not is_staff(interaction.user):
            asyncio.create_task(
                punish_if_no_order(channel, owner_id, "注文せずにチケットを閉じた")
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
                # クローズ時の set_permissions で閲覧権限なども消えているため、
                # 作成時と同じ権限をまとめて戻す（送信だけ戻すと顧客が閲覧できない）。
                await channel.set_permissions(
                    owner,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )

        await interaction.response.send_message(
            "チケットを再開しました。",
            view=OpenTicketView(),
        )

        # スタッフが再開したチケットは、無言チケットとして自動で閉じない
        if channel.topic and not channel.topic.endswith(SILENT_TOPIC_FLAG):
            try:
                await channel.edit(topic=f"{channel.topic}{SILENT_TOPIC_FLAG}")
            except discord.HTTPException as error:
                log_http_error("再開したチケットの印付け", error)

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
# 15-2. 無言チケット・注文なしクローズの罰
# ==========================================

# 「受付済み」以降まで進んだ注文は「注文した」とみなす（キャンセルは注文なし扱い）
ORDERED_STATUSES = {"submitted", "link_sent", "received", "reported", "paid", "done"}

no_order_notice_lock = asyncio.Lock()
_last_no_order_notice = 0.0


async def ticket_has_order(channel):
    """チケット内の受付パネルに、注文まで進んだものが1つでもあるか。"""
    async for message in channel.history(limit=500):
        if message.author.id != bot.user.id:
            continue
        state = parse_order_message(message)
        if state and state["status"] in ORDERED_STATUSES:
            return True
    return False


async def send_no_order_notice():
    """警告メッセージを送る。何人分あっても NO_ORDER_NOTICE_INTERVAL 秒に1回まで。"""
    global _last_no_order_notice
    async with no_order_notice_lock:
        wait = NO_ORDER_NOTICE_INTERVAL - (asyncio.get_running_loop().time() - _last_no_order_notice)
        if wait > 0:
            await asyncio.sleep(wait)
        channel = bot.get_channel(NO_ORDER_NOTICE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(NO_ORDER_NOTICE_CHANNEL_ID)
            except discord.HTTPException as error:
                log_http_error("無言チケット警告チャンネルの取得", error)
                return
        await safe_send(
            channel,
            f"<@&{NO_ORDER_ROLE_ID}>\n"
            "無言チケットやめよ😅\n"
            "-# 訳がある場合はチケットを開いてオーナーをメンションしてください",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        _last_no_order_notice = asyncio.get_running_loop().time()


async def punish_no_order(guild, user_id, reason):
    """罰ロールを付けて警告を送る。すでにロールがある人には何もしない。"""
    member = await fetch_ticket_owner_member(guild, user_id)
    if member is None or is_staff(member):
        return
    role = guild.get_role(NO_ORDER_ROLE_ID)
    if role is None:
        logger.warning("罰ロール(%s)が見つかりません。", NO_ORDER_ROLE_ID)
        return
    if role in member.roles:
        return
    try:
        await member.add_roles(role, reason=f"無言チケット対策: {reason}")
    except discord.HTTPException as error:
        log_http_error("無言チケットの罰ロール付与", error)
        return
    logger.info("罰ロールを付与: %s (%s) / %s", member, member.id, reason)
    await send_no_order_notice()


async def punish_if_no_order(channel, owner_id, reason):
    try:
        if not await ticket_has_order(channel):
            await punish_no_order(channel.guild, owner_id, reason)
    except discord.HTTPException as error:
        log_http_error("注文なしクローズの確認", error)
    except Exception as error:
        log_unexpected_error("注文なしクローズの確認", error)


def ticket_is_open(channel, owner_id):
    """閉じたチケットは本人の送信権限が外されている。"""
    return channel.overwrites_for(discord.Object(id=owner_id)).send_messages is not False


async def close_silent_ticket(channel, owner_id):
    # 先に印を付けて、スタッフが再開した後に何度も閉じられないようにする
    await channel.edit(topic=f"{channel.topic}{SILENT_TOPIC_FLAG}")
    owner = await fetch_ticket_owner_member(channel.guild, owner_id)
    if owner is not None:
        await channel.set_permissions(owner, send_messages=False)
    await safe_send(
        channel,
        f"{SILENT_TICKET_HOURS}時間ご注文がなかったため、チケットを閉じました。",
        view=ClosedTicketView(),
    )
    logger.info("無言チケットを自動で閉じました: #%s", channel.name)
    await punish_no_order(channel.guild, owner_id, f"{SILENT_TICKET_HOURS}時間注文なし（無言チケット）")


async def check_silent_tickets():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        for channel in list(guild.text_channels):
            if not is_ticket(channel) or channel.topic.endswith(SILENT_TOPIC_FLAG):
                continue
            if (now - channel.created_at).total_seconds() < SILENT_TICKET_HOURS * 3600:
                continue
            owner_id = get_ticket_owner(channel)
            if not owner_id or not ticket_is_open(channel, owner_id):
                continue
            try:
                if await ticket_has_order(channel):
                    continue
                await close_silent_ticket(channel, owner_id)
            except discord.HTTPException as error:
                log_http_error(f"無言チケットの処理 #{channel.name}", error)


async def silent_ticket_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await check_silent_tickets()
        except Exception as error:
            log_unexpected_error("無言チケットの巡回", error)
        await asyncio.sleep(SILENT_CHECK_INTERVAL)


# ==========================================
# 16. 起動・接続イベント
# ==========================================

@bot.event
async def on_connect():
    logger.info("Discord Gatewayへの接続を確認しました。")


@bot.event
async def on_ready():
    logger.info("================================")
    logger.info("Bot起動成功: %s", bot.user)
    logger.info("スタッフ通知先: %s", STAFF_CHANNEL_ID or "各チケット内")
    logger.info("PayPayリンク: %s", "設定済み" if (PAYPAY_LINK or PAYPAY_LINKS) else "未設定")
    logger.info("================================")


# ==========================================
# 17. スタッフ用コマンド
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
            f"🤝 **仲介**　─　お取引の仲介（手数料 ¥{MIDDLE_FEE:,}）\n"
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


# ==========================================
# 19. メッセージ処理
# ==========================================



@bot.event
async def on_message(message):
    # PayPayリンクは専用モーダルで受け付ける（通常メッセージでは受け付けない）。
    # チケットにリンクが直接貼られた場合は、表示させないよう削除して伏せ字で案内する。
    if (not message.author.bot
            and is_ticket(message.channel)
            and PAYPAY_URL_RE.search(message.content or "")):
        try:
            await message.delete()
            await safe_send(
                message.channel,
                f"{message.author.mention} PayPayリンク {mask_paypay_link()} を検知したため、"
                "安全のためメッセージを削除しました。\n"
                "リンクはチャットに貼らず、お支払い画面の「PayPay」→ 専用入力フォームから送信してください。",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.Forbidden:
            logger.warning("PayPayリンクを含むメッセージを削除できませんでした（メッセージ管理権限が必要です）。")
        except discord.HTTPException as error:
            log_http_error("PayPayリンクを含むメッセージの削除", error)
        return
    await bot.process_commands(message)


# ==========================================
# 18. Discordイベントエラー
# ==========================================

@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception("Discordイベントエラー: %s", event_method)


# ==========================================
# 19. Bot起動
# ==========================================

logger.info("Botを起動します...")
logger.info("Python version: %s", sys.version.split()[0])
logger.info("discord.py version: %s", discord.__version__)

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
