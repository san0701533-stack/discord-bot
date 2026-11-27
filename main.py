import os
import re
import sys
import asyncio
import copy
import json
import logging
import secrets
import threading
import time
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

# スタッフと同じ扱いにする追加のユーザー・ロール（どちらのIDでもよい）
# チケットの閲覧・書き込み・閉じる/再開/削除・注文の状態変更ができる（スタッフ通知のメンションには入れない）
TICKET_EXTRA_ACCESS_IDS = [
    1571087140886937672,
]

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

# ---- 実績 ----
JISSEKI_CHANNEL_ID = 1569067475725713609     # 完了した注文の実績を載せるチャンネル
REVIEW_DEADLINE_HOURS = 24                   # この時間コメントがなければ、コメントなし・名前ありで載せる
REVIEW_REMIND_SECONDS = 3600                 # コメントを催促する間隔
REVIEW_CHECK_INTERVAL = 300                  # 催促・自動掲載を確認する間隔

# ---- コマンドを使える人 ----
# !setup_ticket・!order_panel・/nuke対策 はこのユーザーだけが使える
COMMAND_OWNER_ID = 1462791798891090084

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
        # 実績コメントの催促と、24時間後の自動掲載
        self.loop.create_task(review_reminder_loop())
        self.loop.create_task(no_order_notice_loop())

        # Nuke対策（/nuke対策）。バックアップと通知は NUKE_BACKUP_USER_ID のDMに送る。
        # チケットは開閉で頻繁に増減するため、保存・復元の対象から外す。
        setup_anti_nuke(
            self,
            backup_user_id=NUKE_BACKUP_USER_ID,
            exclude_channel=is_ticket,
            whitelist_bot_ids=NUKE_WHITELIST_BOT_IDS,
            command_user_ids={COMMAND_OWNER_ID},
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
    if member.id in TICKET_EXTRA_ACCESS_IDS:
        return True
    return any(role.id in STAFF_ROLES or role.id in TICKET_EXTRA_ACCESS_IDS for role in member.roles)


def is_command_owner(user):
    return user is not None and user.id == COMMAND_OWNER_ID


async def ticket_extra_access_targets(guild):
    """追加で閲覧できるユーザー・ロールを、サーバー内のロールかメンバーとして取り出す。"""
    targets = []
    for target_id in TICKET_EXTRA_ACCESS_IDS:
        target = guild.get_role(target_id) or guild.get_member(target_id)
        if target is None:
            try:
                target = await guild.fetch_member(target_id)
            except discord.HTTPException:
                logger.warning("チケット閲覧の追加先(%s)がサーバーに見つかりません。", target_id)
                continue
        targets.append(target)
    return targets


# スタッフロールと同じ権限
TICKET_EXTRA_ACCESS_PERMS = {
    "view_channel": True,
    "send_messages": True,
    "read_message_history": True,
    "manage_channels": True,
}


async def grant_extra_access_to_existing_tickets():
    """起動時に、すでにあるチケットにも追加のユーザー・ロールへスタッフと同じ権限を付ける。"""
    for guild in bot.guilds:
        targets = await ticket_extra_access_targets(guild)
        if not targets:
            continue
        for channel in list(guild.text_channels):
            if not is_ticket(channel):
                continue
            for target in targets:
                current = channel.overwrites_for(target)
                if all(getattr(current, name) is value for name, value in TICKET_EXTRA_ACCESS_PERMS.items()):
                    continue
                try:
                    await channel.set_permissions(target, **TICKET_EXTRA_ACCESS_PERMS)
                except discord.HTTPException as error:
                    log_http_error(f"既存チケットへの閲覧権限の追加 #{channel.name}", error)


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
        "meta": {},
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
        # トロ上げ・ガチバは入口のメニューで選ぶ。チケットでは現在と目標を1画面で選ぶ。
        seq += ["start"]
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
        category = answers.get("category")
        if category == "boost":
            return answers.get("service") in SERVICE_LABELS
        return category in CATEGORY_LABELS
    if step == "service":
        return answers.get("service") in SERVICE_LABELS
    if step in ("start", "goal"):
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


# 入口・チケットのメニューの値 → （区分, サービス）
MENU_CHOICES = {
    "trophy": ("boost", "trophy"),
    "ranked": ("boost", "ranked"),
    "cheat": ("cheat", None),
    "middle": ("middle", None),
    "boost": ("boost", None),  # 以前の入口パネル（代行をまとめて選ぶ形）
}


def apply_menu_choice(state, value):
    category, service = MENU_CHOICES[value]
    apply_answer(state, "category", category)
    if service:
        apply_answer(state, "service", service)


def order_menu_value(state):
    answers = state["answers"]
    if answers.get("category") == "boost":
        return answers.get("service")
    return answers.get("category")


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
    """受付パネルから注文状態を取り出す。

    新しい画面は「ORDER #番号」から保存データを読み、以前の画面（Embed）は表示内容から復元する。
    """
    if message is None:
        return None
    if not message.embeds:
        order_id = order_id_in_message(message)
        entry = load_order(order_id) if order_id else None
        if entry is None or entry.get("channel_id") != message.channel.id:
            return None
        state = copy.deepcopy(entry["state"])
        state.setdefault("meta", {})
        return state

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
        "meta": {},
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


# ---- 注文データの保存（新しい画面は文字だけなので、注文の中身はファイルに保存する） ----

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDER_STORE_PATH = os.path.join(BASE_DIR, "data", "orders.json")
ORDER_FOOTER_RE = re.compile(r"ORDER #([0-9A-F]{4,16})")
# 画面右上のRSGYロゴ。環境変数で差し替えられる
RSGY_LOGO_URL = os.getenv(
    "RSGY_LOGO_URL",
    "https://raw.githubusercontent.com/san0701533-stack/discord-bot/main/assets/rsgy_logo.jpg?v=2",
).strip()
COLOR_CHAMPAGNE = 0xB89A6A
ORDER_KEEP_DAYS = 90

_order_store = None


def _load_order_store():
    global _order_store
    if _order_store is None:
        try:
            with open(ORDER_STORE_PATH, encoding="utf-8") as f:
                _order_store = json.load(f)
        except FileNotFoundError:
            _order_store = {}
        except (OSError, ValueError) as error:
            logger.error("注文データの読み込みに失敗しました: %s", error)
            _order_store = {}
    return _order_store


def _write_order_store():
    store = _load_order_store()
    cutoff = time.time() - ORDER_KEEP_DAYS * 86400
    for order_id in [k for k, v in store.items() if v.get("updated", 0) < cutoff]:
        store.pop(order_id, None)
    try:
        os.makedirs(os.path.dirname(ORDER_STORE_PATH), exist_ok=True)
        tmp = ORDER_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, ORDER_STORE_PATH)
    except OSError as error:
        logger.error("注文データの保存に失敗しました: %s", error)


def save_order(state, channel_id, message_id):
    """注文の状態と、それを表示している画面（メッセージ）を記録する。"""
    store = _load_order_store()
    entry = store.setdefault(state["order_id"], {})
    old = entry.get("message_id")
    aliases = entry.setdefault("aliases", [])
    if old and old != message_id and old not in aliases:
        aliases.append(old)
    entry.update({
        "state": copy.deepcopy(state),
        "channel_id": channel_id,
        "message_id": message_id,
        "updated": time.time(),
    })
    _write_order_store()


def load_order(order_id):
    return _load_order_store().get(order_id)


def find_order_by_message(channel_id, message_id):
    for entry in _load_order_store().values():
        if entry.get("channel_id") != channel_id:
            continue
        if entry.get("message_id") == message_id or message_id in entry.get("aliases", []):
            return entry
    return None


def _walk_components(components):
    for component in components or []:
        yield component
        for attr in ("children", "components"):
            yield from _walk_components(getattr(component, attr, None))
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            yield accessory


def order_id_in_message(message):
    for component in _walk_components(getattr(message, "components", None)):
        match = ORDER_FOOTER_RE.search(getattr(component, "content", None) or "")
        if match:
            return match.group(1)
    return None


# ---- 新しい画面の部品 ----

STEP_LABELS = {
    "category": "ご依頼の種類",
    "start": "現在と目標",
    "info": "アカウント情報",
    "device": "ご利用端末",
    "detail": "ご相談内容",
    "partner": "取引相手",
    "confirm": "最終確認",
    "payment": "お支払い",
}


def _layout(*items, accent=COLOR_CHAMPAGNE, outside=None):
    """枠（コンテナ）1つの画面を作る。outside は枠の上に出す文（メンションなど）。"""
    view = discord.ui.LayoutView(timeout=None)
    if outside:
        view.add_item(discord.ui.TextDisplay(outside))
    container = discord.ui.Container(accent_colour=accent)
    for item in items:
        if item is not None:
            container.add_item(item)
    view.add_item(container)
    view.stop()
    return view


def _text(content):
    return discord.ui.TextDisplay(content)


def _sep(visible=True):
    return discord.ui.Separator(visible=visible)


def _row(*items):
    return discord.ui.ActionRow(*[i for i in items if i is not None])


def _head(*lines, thumb=True):
    """右上にロゴを置いた見出し部分。"""
    content = "\n".join(line for line in lines if line)
    if thumb and RSGY_LOGO_URL:
        return discord.ui.Section(content, accessory=discord.ui.Thumbnail(RSGY_LOGO_URL))
    return _text(content)


def _kicker(state):
    return f"-# RSGY　/　ORDER #{state['order_id']}"


def _footer(state):
    return _text(f"-# ORDER #{state['order_id']}　·　{STATUS_TEXT[state['status']]}")


def _visible_steps(state):
    return [s for s in step_sequence(state) if s != "category"] + ["payment"]


def _step_line(state, step):
    steps = _visible_steps(state)
    if step not in steps:
        return None
    idx = steps.index(step) + 1
    dots = "◆" * idx + "◇" * (len(steps) - idx)
    return f"**STEP {idx} / {len(steps)}**　{STEP_LABELS[step]}　{dots}"


def _price_block(label, price):
    return f"-# {label}\n## ¥{price:,}"


def _range_text(state):
    """お支払い・完了・実績で使う「どこからどこまで」の説明。"""
    answers = state["answers"]
    category = answers.get("category")
    if category == "boost":
        start, goal = display_value(state, "start"), display_value(state, "goal")
        if start and goal:
            return f"{start} → {goal}"
    if category == "middle" and answers.get("partner"):
        return f"取引相手：{PARTNER_ID_RE.sub('', answers['partner'])}"
    if category == "cheat" and answers.get("device"):
        return f"端末：{display_value(state, 'device')}"
    return None


def order_title(state):
    answers = state["answers"]
    if answers.get("category") == "boost":
        return SERVICE_LABELS.get(answers.get("service"), "代行")
    return CATEGORY_LABELS.get(answers.get("category"), "ご注文")


def order_amount(state):
    if state["answers"].get("category") == "cheat":
        return state["answers"].get("amount")
    return order_quote(state)


def _summary_lines(state):
    lines = []
    for key in summary_keys(state):
        if key in ("payment", "category", "service"):
            continue
        text = display_value(state, key)
        if key == "partner" and text:
            text = PARTNER_ID_RE.sub("", text)
        if text:
            lines.append(f"{FIELD_LABELS[key]}　**{text}**")
    return lines


def _btn(label, custom_id, *, style=discord.ButtonStyle.secondary, emoji=None, disabled=False):
    return discord.ui.Button(label=label, custom_id=custom_id, style=style, emoji=emoji, disabled=disabled)


# ---- 入力中の画面 ----

def render_step(state, step, ret="n"):
    answers = state["answers"]
    prefix = "変更 ─ " if ret == "c" else ""
    items = []
    nav_next = False

    if step == "category":
        items.append(_head(_kicker(state), f"## {prefix}ご依頼の種類を選んでください"))
        items.append(_sep())
        current = order_menu_value(state)
        items.append(_row(discord.ui.Select(
            custom_id=f"ord:sel:menu:{ret}",
            placeholder="ご依頼の種類を選択",
            options=[
                discord.SelectOption(label=label, value=value, emoji=emoji, description=desc, default=current == value)
                for value, label, emoji, desc in TICKET_MENU_OPTIONS
            ],
        )))

    elif step == "start":
        trophy = answers.get("service") == "trophy"
        unit = "トロフィー" if trophy else "ランク"
        items.append(_head(_kicker(state), _step_line(state, step), f"## {prefix}現在と目標の{unit}を選んでください"))
        items.append(_sep())
        start = answers.get("start")
        goal = answers.get("goal")
        if trophy:
            start_opts = [discord.SelectOption(label=f"現在：{s:,} トロ", value=str(s), default=start == s)
                          for s in trophy_start_options()]
        else:
            start_opts = [discord.SelectOption(label=f"現在：{RANK_NAMES[r]}", value=str(r), default=start == r)
                          for r in rank_start_options()]
        items.append(_row(discord.ui.Select(
            custom_id=f"ord:sel:start:{ret}", placeholder=f"現在の{unit}を選択", options=start_opts,
        )))
        if start is None:
            goal_select = discord.ui.Select(
                custom_id=f"ord:sel:goal:{ret}", placeholder=f"先に現在の{unit}を選んでください",
                options=[discord.SelectOption(label="―", value="none")], disabled=True,
            )
        else:
            if trophy:
                goal_opts = [discord.SelectOption(label=f"目標：{g:,} トロ", value=str(g),
                                                  description=f"料金 ¥{trophy_quote(start, g):,}", default=goal == g)
                             for g in trophy_goal_options(start)]
            else:
                goal_opts = [discord.SelectOption(label=f"目標：{RANK_NAMES[g]}", value=str(g),
                                                  description=f"料金 ¥{rank_quote(start, g):,}", default=goal == g)
                             for g in rank_goal_options(start)]
            goal_select = discord.ui.Select(
                custom_id=f"ord:sel:goal:{ret}", placeholder=f"目標の{unit}を選択",
                options=goal_opts or [discord.SelectOption(label="選べる目標がありません", value="none")],
                disabled=not goal_opts,
            )
        items.append(_row(goal_select))
        price = order_quote(state)
        if price is not None:
            items.append(_sep())
            items.append(_text(_price_block("お見積り", price) + "\n-# 料金表に基づいて自動で計算しています"))
        nav_next = step_done(state, step)

    elif step == "info":
        items.append(_head(_kicker(state), _step_line(state, step), f"## {prefix}アカウント情報を入力してください"))
        items.append(_sep())
        lines = [
            "◆ **プレイヤータグ**（必須）　例：`#ABC123`",
            "◆ **対象キャラ**（任意）",
            "◆ **備考**（任意）　希望納期など",
            "-# 🔐 パスワードは入力しないでください（ログインに必要な情報は、お支払い後にご案内します）",
        ]
        if answers.get("tag"):
            lines.append(f"\n入力済み：**{answers['tag']}**　/　{answers.get('brawler') or 'なし'}　/　{answers.get('note') or 'なし'}")
        items.append(_text("\n".join(lines)))
        done = step_done(state, step)
        items.append(_row(_btn("入力内容を修正する" if done else "情報を入力する", f"ord:open:info:{ret}",
                               style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary, emoji="📝")))
        nav_next = done

    elif step == "device":
        items.append(_head(_kicker(state), _step_line(state, step), f"## {prefix}ご利用の端末を選んでください"))
        items.append(_sep())
        selected = answers.get("device")
        items.append(_row(
            _btn("iOS", f"ord:set:device:ios:{ret}", emoji="📱",
                 style=discord.ButtonStyle.primary if selected == "ios" else discord.ButtonStyle.secondary),
            _btn("Android", f"ord:set:device:android:{ret}", emoji="🤖",
                 style=discord.ButtonStyle.primary if selected == "android" else discord.ButtonStyle.secondary),
        ))

    elif step == "detail":
        items.append(_head(_kicker(state), _step_line(state, step), f"## {prefix}ご相談内容を入力してください"))
        items.append(_sep())
        body = "内容を確認したうえで、スタッフが個別にご案内します。\n-# 🔐 パスワードや認証コードは入力しないでください"
        if answers.get("detail"):
            body += f"\n\n入力済み：\n> {answers['detail'][:300]}"
        items.append(_text(body))
        done = step_done(state, step)
        items.append(_row(_btn("入力内容を修正する" if done else "相談内容を入力する", f"ord:open:detail:{ret}",
                               style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary, emoji="📝")))
        nav_next = done

    elif step == "partner":
        items.append(_head(_kicker(state), _step_line(state, step), f"## {prefix}取引相手を入力してください"))
        items.append(_sep())
        body = (
            "取引したい相手の **ユーザーネーム** を入力してください。相手はこのサーバーに参加している必要があります。\n"
            f"-# 仲介手数料 ¥{MIDDLE_FEE:,}（PayPay）　·　お支払い後、取引相手がこのチケットに入ります"
        )
        if answers.get("partner"):
            body += f"\n\n現在の取引相手：**{PARTNER_ID_RE.sub('', answers['partner'])}**"
        items.append(_text(body))
        done = step_done(state, step)
        items.append(_row(_btn("取引相手を変更する" if done else "取引相手を入力する", f"ord:open:partner:{ret}",
                               style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.primary, emoji="🤝")))
        nav_next = done

    else:
        return render_panel(state)

    items.append(_sep())
    if ret == "c" and first_incomplete(state) == "confirm":
        nav = [_btn("確認画面へ戻る", f"ord:back:{step}:c", emoji="↩️")]
    else:
        nav = [_btn("戻る", f"ord:back:{step}:n", emoji="◀️", disabled=prev_step(state, step) is None)]
    if nav_next:
        nav.append(_btn("次へ", f"ord:next:{step}:{ret}", style=discord.ButtonStyle.primary, emoji="▶️"))
    items.append(_row(*nav))
    items.append(_footer(state))
    return _layout(*items)


def render_confirm(state):
    is_cheat = state["answers"].get("category") == "cheat"
    items = [
        _head(_kicker(state), _step_line(state, "confirm"), "## この内容でよろしいですか？"),
        _sep(),
        _text("\n".join([f"区分　**{order_title(state)}**"] + _summary_lines(state))),
        _sep(),
    ]
    if is_cheat:
        items.append(_text("-# 料金\n### スタッフが個別にご案内します\n-# 送信後、このチケットで金額をご案内します"))
    else:
        items.append(_text(_price_block("合計", order_quote(state))))
    options = []
    for step in step_sequence(state)[:-1]:
        if step == "start":
            current = _range_text(state)
        elif step == "info":
            current = display_value(state, "tag")
        elif step == "category":
            current = order_title(state)
        else:
            current = display_value(state, step)
            if step == "partner" and current:
                current = PARTNER_ID_RE.sub("", current)
        options.append(discord.SelectOption(
            label=f"{STEP_LABELS.get(step, step)} を変更", value=step,
            description=(current or "未入力")[:90], emoji="✏️",
        ))
    items.append(_row(discord.ui.Select(custom_id="ord:edit", placeholder="✏️ 内容を変更する項目を選択", options=options)))
    items.append(_row(
        _btn("スタッフへ送信" if is_cheat else "注文を確定", "ord:submit", style=discord.ButtonStyle.success, emoji="✅"),
        _btn("キャンセル", "ord:cancel:confirm", emoji="✖️"),
    ))
    items.append(_footer(state))
    return _layout(*items)


def paypay_link_for(amount):
    if amount is not None and amount in PAYPAY_LINKS:
        return PAYPAY_LINKS[amount]
    return PAYPAY_LINK or None


# ---- お支払い・進み具合・完了 ----

def _timeline(state):
    status = state["status"]
    payment = state["answers"].get("payment")
    pay_label = "LTC送金報告" if payment == "ltc" else "PayPayリンク送信"
    amount = order_amount(state)
    first = f"注文受付　{order_title(state)}" + (f"　¥{amount:,}" if amount else "")
    steps = [first, pay_label, "入金確認", "対応中", "完了"]
    reached = {"link_sent": 2, "reported": 2, "received": 3, "paid": 3, "done": 5}.get(status, 1)
    lines = []
    for i, label in enumerate(steps):
        if i < reached:
            lines.append(f"✅　{label}")
        elif i == reached:
            lines.append(f"⏳　**{label}**")
        else:
            lines.append(f"▫️　{label}")
    return "\n".join(lines)


def render_status(state):
    status = state["status"]
    answers = state["answers"]
    is_cheat = answers.get("category") == "cheat"
    is_middle = answers.get("category") == "middle"
    owner = f"<@{state['owner_id']}>" if state.get("owner_id") else ""
    has_payment_flow = not is_cheat or bool(answers.get("payment"))
    range_text = _range_text(state)

    def price_text():
        if is_cheat:
            return "-# お支払い金額\n### スタッフがご案内した金額\n-# 金額の案内を受けてからお支払いください"
        return _price_block("お支払い金額", order_quote(state))

    if status == "payment_select":
        items = [
            _head(_kicker(state), _step_line(state, "payment")),
            _text(price_text() + (f"\n-# {range_text}" if range_text else "")),
            _sep(),
            discord.ui.Section(
                "**💳 PayPay**\n-# 受け取りリンクを専用フォームから送信。チケットにリンクは出ません",
                accessory=_btn("PayPayで支払う", "ord:pay:paypay", style=discord.ButtonStyle.primary),
            ),
        ]
        if not is_middle:
            items.append(discord.ui.Section(
                "**🪙 LTC（仮想通貨）**\n-# 送金先アドレスを表示します",
                accessory=_btn("LTCで支払う", "ord:pay:ltc"),
            ))
        items += [_sep(), _text("-# リンクはチャットに貼らず、必ずフォームから送ってください"),
                  *_staff_controls(state), _footer(state)]
        return _layout(*items, outside=f"{owner} お支払いのご案内です。")

    if status == "submitted" and has_payment_flow:
        items = [_head(_kicker(state), _step_line(state, "payment")),
                 _text(price_text() + (f"\n-# {range_text}" if range_text else "")), _sep()]
        if answers.get("payment") == "ltc":
            items.append(_text(
                "**お支払い方法：LTC（仮想通貨）**\n下記のアドレスにLTCで送金してください。\n"
                f"```\n{LTC_ADDRESS}\n```\n"
                "アドレスを一文字も間違えずに送金したら、「はい、送金しました」を押してください。"
            ))
            items.append(_row(
                _btn("LTCアドレスを表示", "ord:copy:ltc", emoji="📋"),
                _btn("はい、送金しました", "ord:report", style=discord.ButtonStyle.primary, emoji="✅"),
            ))
        else:
            items.append(_text(
                "**お支払い方法：PayPay**\n下のボタンから、PayPay受け取りリンクを送信してください。\n"
                "-# リンクは担当アカウントのDMにだけ届き、チケットには表示されません"
            ))
            items.append(_row(_btn("PayPayリンクを入力・送信", "ord:p:paypay_open",
                                   style=discord.ButtonStyle.primary, emoji="💳")))
        items += [*_staff_controls(state), _footer(state)]
        return _layout(*items)

    if status == "submitted":  # 支払い方法がない旧形式のチート相談
        return _layout(
            _head(_kicker(state), "## ご相談をスタッフへ送信しました", "担当スタッフが内容を確認しています。このチャンネルでのご連絡をお待ちください。"),
            _footer(state),
        )

    if status in ("link_sent", "received", "reported", "paid"):
        headings = {
            "link_sent": ("入金を確認しています", "担当者がPayPayの受け取りを確認中です。確認が取れ次第、スタッフが対応します。"),
            "reported": ("入金を確認しています", "送金のご報告ありがとうございます。スタッフが入金を確認しています。"),
            "received": ("お支払いを確認しました", "スタッフが対応を開始するまで、しばらくお待ちください。"),
            "paid": ("対応中です", "担当スタッフが対応しています。進行状況はこのチャンネルでご連絡します。"),
        }
        title, body = headings[status]
        return _layout(
            _head(_kicker(state), f"## {title}", body),
            _sep(),
            _text(_timeline(state)),
            _text("-# ご不明な点は、このチャンネルにそのまま書き込んでください"),
            *_staff_controls(state),
            _footer(state),
        )

    if status == "done":
        review = state.get("meta", {}).get("review", {})
        amount = order_amount(state)
        summary = [f"内容　**{order_title(state)}**" + (f"　{range_text}" if range_text else "")]
        if amount:
            summary.append(f"金額　**¥{amount:,}**" + (f"（{PAYMENT_LABELS.get(answers.get('payment'), '')}）" if answers.get("payment") else ""))
        items = [
            _head(
                _kicker(state),
                "# ご依頼が完了しました",
                "ご利用ありがとうございました。またのご依頼をお待ちしております。\n"
                "確認が終わったら、このチケットを消してください。",
            ),
            _sep(),
            _text("\n".join(summary)),
            _sep(),
        ]
        if review.get("status") == "posted":
            items.append(_text("-# ✅ 実績に掲載しました。ありがとうございました！"))
            buttons = []
        else:
            items.append(_text(
                "**実績へのコメントをお願いします**\n"
                "-# いただいたコメントは実績チャンネルに載せます。名前を伏せることもできます。\n"
                f"-# {REVIEW_DEADLINE_HOURS}時間以内にコメントがない場合は、コメントなし・お名前ありで載せます。"
            ))
            buttons = [_btn("実績にコメントする", "ord:open:review:n", style=discord.ButtonStyle.primary, emoji="✍️")]
        buttons += [
            _btn("新しく注文する", "ord:restart", emoji="✨"),
            _btn("チケットを削除", "ord:delete", style=discord.ButtonStyle.danger, emoji="🗑️"),
        ]
        items += [_row(*buttons), _footer(state)]
        return _layout(*items, outside=owner)

    # cancelled
    return _layout(
        _head(_kicker(state), "## ご注文はキャンセルされました", "もう一度ご依頼される場合は、下のボタンから最初からやり直せます。"),
        _row(_btn("最初からやり直す", "ord:restart", style=discord.ButtonStyle.primary, emoji="🔄")),
        _footer(state),
    )


def _staff_controls(state):
    """スタッフだけが押せるボタン（お客様が押しても動かない）。"""
    status = state["status"]
    buttons = []
    if status == "reported":
        buttons.append(_btn("入金を確認", "ord:sp:confirm", style=discord.ButtonStyle.success, emoji="✅"))
    elif status == "received":
        buttons.append(_btn("対応を開始", "ord:sp:confirm", style=discord.ButtonStyle.success, emoji="✅"))
    elif status == "paid":
        buttons.append(_btn("完了", "ord:sp:done", style=discord.ButtonStyle.success, emoji="🏁"))
    buttons.append(_btn("注文取消", "ord:sp:cancel", emoji="✖️"))
    return [_sep(), _text("-# スタッフ用"), _row(*buttons)]


def render_account_request(state):
    """代行の対応開始時に、ログインに必要なメールアドレスと認証コードをお願いする画面。"""
    email = state.get("meta", {}).get("email")
    lines = [
        "## ログイン情報のご案内",
        "メールアドレスを入力してください。\n"
        "スタッフがメールアドレスを入力したら認証コードが届くので、それを教えてください。",
    ]
    items = [_head(_kicker(state), *lines), _sep()]
    if email:
        items.append(_text(f"📧 受け付けたメールアドレス：`{email}`\n-# 認証コードが届いたら、下のボタンから入力してください"))
    items.append(_row(
        _btn("メールアドレスを変更" if email else "メールアドレスを入力", "ord:acct:email",
             style=discord.ButtonStyle.secondary if email else discord.ButtonStyle.primary, emoji="📧"),
        _btn("認証コードを入力", "ord:acct:code",
             style=discord.ButtonStyle.primary if email else discord.ButtonStyle.secondary, emoji="🔑",
             disabled=not email),
    ))
    items.append(_text("-# 🔐 パスワードは聞きません　·　認証コードは届いたらすぐに入力してください"))
    items.append(_footer(state))
    return _layout(*items, outside=f"<@{state['owner_id']}>")


def build_account_modal(kind):
    if kind == "email":
        modal = discord.ui.Modal(title="メールアドレスの入力", custom_id="ord:acctm:email", timeout=600)
        modal.add_item(discord.ui.TextInput(
            label="メールアドレス", custom_id="email", placeholder="例：example@gmail.com",
            required=True, max_length=254,
        ))
    else:
        modal = discord.ui.Modal(title="認証コードの入力", custom_id="ord:acctm:code", timeout=600)
        modal.add_item(discord.ui.TextInput(
            label="認証コード", custom_id="code", placeholder="届いたコードをそのまま入力",
            required=True, max_length=12,
        ))
    return modal


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def handle_account_action(interaction, state, kind, submitted):
    """ログイン情報（メールアドレス・認証コード）の受け付け。"""
    if state["answers"].get("category") != "boost" or state["status"] != "paid":
        return await _ephemeral(interaction, "現在、ログイン情報の入力は受け付けていません。")
    if kind == "code" and not state.get("meta", {}).get("email"):
        return await _ephemeral(interaction, "先にメールアドレスを入力してください。")
    if not submitted:
        await interaction.response.send_modal(build_account_modal(kind))
        return
    values = modal_values(interaction.data)
    channel = interaction.channel
    mentions = staff_mentions(channel.guild)
    if kind == "email":
        email = unicodedata.normalize("NFKC", values.get("email") or "").strip()
        if not EMAIL_RE.match(email):
            return await _ephemeral(interaction, "メールアドレスの形式が正しくありません。もう一度入力してください。")
        state.setdefault("meta", {})["email"] = email
        entry = load_order(state["order_id"])
        if entry:
            save_order(state, entry["channel_id"], entry["message_id"])
        await interaction.response.edit_message(view=render_account_request(state), allowed_mentions=PANEL_MENTIONS)
        await safe_send(
            channel,
            f"📧 **メールアドレスを受け付けました**　`{email}`\n"
            f"スタッフはログインを進めてください。認証コードが届いたら、お客様に入力していただきます。{mentions}",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        logger.info("メールアドレスを受付: #%s", state["order_id"])
        return
    code = re.sub(r"\s", "", unicodedata.normalize("NFKC", values.get("code") or ""))
    if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", code):
        return await _ephemeral(interaction, "認証コードの形式が正しくありません。届いたコードをそのまま入力してください。")
    await interaction.response.send_message("認証コードをスタッフに伝えました。ありがとうございます。", ephemeral=True)
    await safe_send(
        channel,
        f"🔑 **認証コードが届きました**　`{code}`　{mentions}",
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )
    logger.info("認証コードを受付: #%s", state["order_id"])


def render_cancel_confirm(state, back_to):
    submitted = state["status"] != "draft"
    body = (
        "キャンセルすると、この注文は取り消されスタッフにも通知されます。"
        if submitted else
        "入力した内容は破棄されます。キャンセル後は最初からやり直せます。"
    )
    return _layout(
        _head(_kicker(state), "## ご注文をキャンセルしますか？", body),
        _row(
            _btn("キャンセルする", "ord:cxy", style=discord.ButtonStyle.danger, emoji="🗑️"),
            _btn("続ける", f"ord:cxn:{back_to}", style=discord.ButtonStyle.primary, emoji="↩️"),
        ),
        _footer(state),
    )


def render_panel(state, step=None, ret="n"):
    if state["status"] != "draft":
        return render_status(state)
    step = step or first_incomplete(state)
    if step == "confirm":
        if first_incomplete(state) != "confirm":
            return render_step(state, first_incomplete(state), "n")
        return render_confirm(state)
    return render_step(state, step, ret)


def render_welcome(member):
    """チケットを開いたときの「ようこそ」（消さずに残す）。"""
    return _layout(
        _head(
            "-# RSGY　/　ORDER DESK",
            f"# ようこそ、{member.display_name} さん",
            "このチケットはあなた専用です。メニューを順番に選ぶだけで、ご注文からお支払いまで完了します。",
        ),
        _sep(),
        _row(_btn("チケットを閉じる", "ticket:close", style=discord.ButtonStyle.danger, emoji="🔒")),
        _text("-# 内容の変更はいつでもできます　·　パネルはご本人だけが操作できます"),
        outside=member.mention,
    )


# ---- 画面の送信・更新 ----

PANEL_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)


async def repost_panel(channel, old_message, state, view=None):
    """新しい画面を出してから、前の画面を消す。送信に失敗したら前の画面は残す。"""
    view = view or render_panel(state)
    try:
        new_message = await channel.send(view=view, allowed_mentions=PANEL_MENTIONS)
    except discord.HTTPException as error:
        log_http_error("注文画面の送信", error)
        return None
    save_order(state, channel.id, new_message.id)
    if old_message is not None:
        try:
            await old_message.delete()
        except discord.HTTPException as error:
            log_http_error("前の注文画面の削除", error)
    return new_message


async def edit_panel(message, state, view=None):
    """画面をその場で書き換える（旧形式の画面は新しい形式で出し直す）。"""
    view = view or render_panel(state)
    if message.embeds:
        new_message = await repost_panel(message.channel, message, state, view)
        if new_message is None:
            raise discord.HTTPException(_FakeResponse(), "注文画面を出し直せませんでした")
        return new_message
    await message.edit(view=view, allowed_mentions=PANEL_MENTIONS)
    save_order(state, message.channel.id, message.id)
    return message


class _FakeResponse:
    status = 0
    reason = "repost failed"


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
            elif "custom_id" in node and "values" in node:
                picked = node["values"] or []
                values[node["custom_id"]] = picked[0] if picked else ""
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
    """スタッフへ一行だけ知らせる（操作は進み具合の画面のスタッフ用ボタンで行う）。"""
    target = guild.get_channel(STAFF_CHANNEL_ID) if (guild and STAFF_CHANNEL_ID) else None
    if target is None:
        target = ticket_channel
    link = f"　[▸ 画面を開く]({panel_message.jump_url})" if target is not ticket_channel else ""
    await safe_send(
        target,
        f"{title}　{staff_mentions(guild)}".strip() + link,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )


async def send_order_panel(channel, owner_id, choice=None):
    state = new_order_state(owner_id)
    if choice in MENU_CHOICES:
        apply_menu_choice(state, choice)
    return await repost_panel(channel, None, state)


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


async def _show_view(interaction, state, view, *, new_page=False):
    """新しいページにするときは前の画面を消して出し直し、それ以外はその場で書き換える。"""
    message = interaction.message
    if new_page or message is None or message.embeds:
        if not interaction.response.is_done():
            await interaction.response.defer()
        new_message = await repost_panel(interaction.channel, message, state, view)
        if new_message is None:
            await _ephemeral(interaction, "画面を更新できませんでした。もう一度お試しください。")
        return new_message
    await interaction.response.edit_message(view=view, allowed_mentions=PANEL_MENTIONS)
    save_order(state, interaction.channel.id, message.id)
    return message


async def _show(interaction, state, step=None, ret="n", *, new_page=False):
    return await _show_view(interaction, state, render_panel(state, step, ret), new_page=new_page)


async def _show_after_answer(interaction, state, step, ret):
    # 通常時は次の質問へ。確認画面からの変更時は、未入力があればそこへ、
    # なければ確認画面へ戻る（以降は通常の流れに戻す）。次のステップは新しいページで出す。
    target = first_incomplete(state) if ret == "c" else next_step(state, step)
    mode = "c" if (ret == "c" and target != "confirm") else "n"
    await _show(interaction, state, target, mode, new_page=target != step)


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
        interaction.message = message
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

    action = parts[1]
    if action in ("acct", "acctm"):     # ログイン情報の入力（代行の対応中）
        return await handle_account_action(interaction, state, parts[2] if len(parts) > 2 else "", action == "acctm")
    is_review = action in ("open", "modal") and len(parts) > 2 and parts[2] == "review"

    if not is_review and _ticket_closed_for(channel, owner_id):
        await _ephemeral(interaction, "このチケットはクローズされています。")
        return

    # ---- 入力中のみ許可する操作 ----
    draft_actions = {"set", "sel", "open", "modal", "back", "next", "edit", "submit"}
    if action in draft_actions and not is_review and state["status"] != "draft":
        await _ephemeral(interaction, "この注文はすでに確定しています。")
        return

    if action == "set":            # ord:set:<key>:<value>:<ret>（以前の画面のボタンも含む）
        key, value, ret = parts[2], parts[3], parts[4]
        allowed = {"category": CATEGORY_LABELS, "service": SERVICE_LABELS, "device": DEVICE_LABELS, "payment": PAYMENT_LABELS}
        if key not in allowed or value not in allowed[key]:
            return await _ephemeral(interaction, "不正な選択です。")
        apply_answer(state, key, value)
        if key in ("category", "service"):
            return await _show(interaction, state, first_incomplete(state), "n", new_page=True)
        return await _show_after_answer(interaction, state, key, ret)

    if action == "sel":            # ord:sel:<key>:<ret>
        key, ret = parts[2], parts[3]
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await _ephemeral(interaction, "不正な選択です。")
        if key == "menu":
            if values[0] not in MENU_CHOICES:
                return await _ephemeral(interaction, "不正な選択です。")
            apply_menu_choice(state, values[0])
            return await _show(interaction, state, first_incomplete(state), "n", new_page=True)
        if key not in ("start", "goal"):
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
        # 現在と目標は同じ画面で選ぶので、その場で書き換える
        return await _show(interaction, state, "start", ret)

    if action == "open":           # ord:open:<info|detail|partner|review>:<ret>
        key, ret = parts[2], parts[3]
        if key == "review":
            if state["status"] != "done" or state.get("meta", {}).get("review", {}).get("status") == "posted":
                return await _ephemeral(interaction, "この注文の実績はすでに掲載済みです。")
            await interaction.response.send_modal(build_review_modal())
            return
        if key == "info":
            modal = build_info_modal(state, ret)
        elif key == "partner":
            modal = build_partner_modal(state, ret)
        else:
            modal = build_detail_modal(state, ret)
        await interaction.response.send_modal(modal)
        return

    if action == "modal":          # ord:modal:<info|detail|partner|review>:<ret>
        key, ret = parts[2], parts[3]
        values = modal_values(interaction.data)
        if key == "review":
            return await submit_review(interaction, state, values)
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
            return await _show(interaction, state, "confirm", new_page=True)
        target = prev_step(state, step) or "category"
        return await _show(interaction, state, target, new_page=True)

    if action == "next":           # ord:next:<step>:<ret>
        step, ret = parts[2], parts[3]
        if not step_done(state, step):
            return await _ephemeral(interaction, "先にこの項目を選択・入力してください。")
        return await _show_after_answer(interaction, state, step, ret)

    if action == "edit":           # ord:edit（確認画面のセレクト）
        values = (interaction.data or {}).get("values") or []
        step = values[0] if values else None
        if step not in step_sequence(state)[:-1]:
            return await _ephemeral(interaction, "変更できない項目です。")
        return await _show(interaction, state, step, "c", new_page=True)

    if action == "submit":
        if first_incomplete(state) != "confirm":
            return await _ephemeral(interaction, "未入力の項目があります。")
        is_cheat = state["answers"].get("category") == "cheat"
        # 注文内容を確定し、確認画面を消してお支払いの画面を出す
        state["status"] = "payment_select"
        state.setdefault("meta", {})["submitted_at"] = time.time()
        payment_message = await _show(interaction, state, new_page=True)
        if payment_message is None:
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
        shown = await _show(interaction, state)
        await notify_staff(interaction.guild, channel, shown or message, state, "💸 入金報告がありました")
        return

    if action == "cancel":         # ord:cancel:<戻り先>（入力中の確認画面のみ）
        if state["status"] != "draft":
            return await _ephemeral(
                interaction,
                "注文確定後のキャンセルはスタッフが対応します。このチャンネルでお知らせください。",
            )
        await _show_view(interaction, state, render_cancel_confirm(state, parts[2]))
        return

    if action == "cxn":            # キャンセルをやめて元の画面へ
        back_to = parts[2]
        if back_to in ("pay", "payment") or state["status"] != "draft":
            return await _show(interaction, state)
        return await _show(interaction, state, back_to)

    if action == "cxy":            # キャンセル確定
        previous = state["status"]
        if previous not in ("draft", "payment_select", "submitted"):
            return await _ephemeral(interaction, "現在の状態ではキャンセルできません。")
        state["status"] = "cancelled"
        await _show(interaction, state)
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
        return await _show(interaction, new_order_state(owner_id), new_page=True)

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


def _paypay_reveal_view(channel_id, message_id, order_id):
    """通知先DMの「リンクを表示」ボタン。"""
    return _view([_button(
        "リンクを表示",
        f"ord:p:pv:{channel_id}:{message_id}:{order_id}",
        style=discord.ButtonStyle.primary,
        emoji="🔗", row=0,
    )])


def _paypay_owner_view(channel_id, message_id, order_id, *, held=False):
    """リンク表示後の「受け取りました」「保留」ボタン。"""
    return _view([
        _button(
            "受け取りました",
            f"ord:p:paypay_recv:{channel_id}:{message_id}:{order_id}",
            style=discord.ButtonStyle.success,
            emoji="✅", row=0,
        ),
        _button(
            "保留を通知済み" if held else "保留",
            f"ord:p:paypay_hold:{channel_id}:{message_id}:{order_id}",
            style=discord.ButtonStyle.secondary,
            emoji="⏸️", row=0,
            disabled=held,
        ),
    ])


async def find_sent_paypay_link(channel_id, order_id):
    """代行の受け取り先へ送ったDMから、その注文のPayPayリンクを探す。"""
    recipient = bot.get_user(PAYPAY_BOOST_USER_ID) or await bot.fetch_user(PAYPAY_BOOST_USER_ID)
    dm = recipient.dm_channel or await recipient.create_dm()
    async for message in dm.history(limit=300):
        if message.author.id != bot.user.id:
            continue
        content = message.content or ""
        if f"注文ID：#{order_id}" not in content or f"<#{channel_id}>" not in content:
            continue
        match = PAYPAY_URL_RE.search(content)
        if match:
            return match.group(0)
    return None


async def _fetch_ticket_panel(channel_id, message_id):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if not is_ticket(channel):
        return channel, None
    try:
        return channel, await channel.fetch_message(message_id)
    except discord.NotFound:
        pass
    # 画面を出し直した注文は、今の画面を保存データから探す
    entry = find_order_by_message(channel_id, message_id)
    if entry and entry.get("message_id") != message_id:
        try:
            return channel, await channel.fetch_message(entry["message_id"])
        except discord.NotFound:
            pass
    return channel, None


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
    await safe_send(
        channel,
        f"🤝 取引相手 {partner.mention} をこのチケットに追加しました。\n"
        f"スタッフの対応をお待ちください。{staff_mentions(channel.guild)}",
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
                state["answers"]["amount"] = price  # 実績に載せる金額として記録
            else:
                price = order_quote(state)
            recipient_ids = paypay_recipient_ids(state)
            is_middle = state["answers"].get("category") == "middle"
            category_label = CATEGORY_LABELS.get(state["answers"].get("category"), EMPTY)

            # 段階1：PayPay選択を確定し「リンク入力待ち」にする（DM失敗時はこの画面に留まる）
            if state["status"] == "payment_select":
                state["answers"]["payment"] = "paypay"
                state["status"] = "submitted"
                try:
                    panel = await edit_panel(panel, state)
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
            # （リンクは「リンクを表示」ボタンを押したときだけ出す。失敗しても注文の進行は止めない）
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
                        view=_paypay_reveal_view(channel.id, panel.id, state["order_id"]),
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException as error:
                    log_http_error("PayPayリンク送信の通知DM", error)

            # DM送信成功 → 受け取り確認待ち（スタッフへのメンションはしない）
            state["status"] = "link_sent"
            try:
                panel = await edit_panel(panel, state)
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

            # 段階3：受け取りを確認したら、そのまま「対応中」へ進める
            try:
                panel = await apply_status_change(channel, panel, state, "paid")
            except discord.HTTPException as error:
                log_http_error("PayPay受け取り確認の反映", error)
                return await _ephemeral(interaction, "チケットの更新に失敗しました。もう一度ボタンを押してください。")

            paypay_confirmed_panels.add((message_id, state["order_id"]))
            logger.info("PayPay受け取り確認: #%s by %s", state["order_id"], interaction.user.id)

            if state["answers"].get("category") == "boost" and interaction.user.id == PAYPAY_BOOST_USER_ID:
                await notify_boost_share(state)

            await interaction.edit_original_response(view=_paypay_dm_view(0, 0, confirmed=True))

        return await _ephemeral(interaction, "受け取りを確認し、チケットを「対応中」に進めました。")

    # ---- 通知先DM：「リンクを表示」ボタン ----
    if action == "pv":
        if interaction.user.id != PAYPAY_NOTIFY_USER_ID:
            return await _ephemeral(interaction, "このボタンは操作できません。")
        try:
            channel_id, message_id, order_id = int(parts[3]), int(parts[4]), parts[5]
        except (IndexError, ValueError):
            return await _ephemeral(interaction, "注文情報を読み込めませんでした。")
        await interaction.response.defer()
        try:
            link = await find_sent_paypay_link(channel_id, order_id)
        except discord.HTTPException as error:
            log_http_error("送信済みPayPayリンクの取得", error)
            link = None
        if link is None:
            return await _ephemeral(interaction, "送信したPayPayリンクが見つかりませんでした。")
        content = (interaction.message.content or "") + f"\n\n🔗 **リンク**：{link}"
        await interaction.edit_original_response(
            content=content,
            view=_paypay_owner_view(channel_id, message_id, order_id),
        )
        return

    # ---- 通知先DM：「保留」ボタン ----
    if action == "paypay_hold":
        if interaction.user.id != PAYPAY_NOTIFY_USER_ID:
            return await _ephemeral(interaction, "このボタンは操作できません。")
        try:
            channel_id, message_id, order_id = int(parts[3]), int(parts[4]), parts[5]
        except (IndexError, ValueError):
            return await _ephemeral(interaction, "注文情報を読み込めませんでした。")
        await interaction.response.defer()
        try:
            channel, panel = await _fetch_ticket_panel(channel_id, message_id)
        except discord.HTTPException as error:
            log_http_error("PayPay保留：チケット取得", error)
            channel, panel = None, None
        state = parse_order_message(panel) if panel is not None else None
        if state is None:
            return await _ephemeral(interaction, "対象のチケットまたはお支払い画面が見つかりません。")
        if state["order_id"] != order_id:
            return await _ephemeral(interaction, "このDMの注文は、現在のお支払い画面の注文と一致しません。")
        if state["status"] not in ("link_sent", "submitted"):
            return await _ephemeral(
                interaction,
                f"現在の状態（{STATUS_TEXT[state['status']]}）では保留の通知はできません。",
            )
        sent = await safe_send(
            channel,
            f"<@{state['owner_id']}>\n"
            "⏸️ 送っていただいたPayPayリンクが保留になったので、承認をしてください。",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        if sent is None:
            return await _ephemeral(interaction, "チケットへの送信に失敗しました。もう一度お試しください。")
        logger.info("PayPay保留を通知: #%s", order_id)
        await interaction.edit_original_response(
            view=_paypay_owner_view(channel_id, message_id, order_id, held=True),
        )
        return await _ephemeral(interaction, "チケットでお客様に保留を通知しました。")

    return await _ephemeral(interaction, "不明な操作です。")


async def apply_status_change(channel, panel, state, new_status):
    """注文の状態を進め、画面とお知らせを更新する。更新後の画面（メッセージ）を返す。"""
    state["status"] = new_status
    owner = f"<@{state['owner_id']}>" if state.get("owner_id") else ""
    is_boost = state["answers"].get("category") == "boost"
    if new_status == "done":
        # 進み具合の画面を消して、完了の画面を出す（実績へのコメント待ちを始める）
        now = time.time()
        state.setdefault("meta", {})["done_at"] = now
        state["meta"]["review"] = {"status": "pending", "last_remind": now}
        return await repost_panel(channel, panel, state)

    panel = await edit_panel(panel, state)
    if new_status == "paid":
        if is_boost:
            # 代行はログインに必要なメールアドレスと認証コードをお願いする
            await channel.send(view=render_account_request(state), allowed_mentions=PANEL_MENTIONS)
        else:
            await safe_send(channel, f"{owner} 入金を確認しました。対応を開始します！")
            await notify_staff(channel.guild, channel, panel, state, "✅ 入金を確認しました。対応をお願いします。")
    elif new_status == "cancelled":
        await safe_send(channel, f"{owner} ご注文 `#{state['order_id']}` はスタッフによりキャンセルされました。")
    return panel


async def handle_panel_staff_action(interaction, action):
    """進み具合の画面にある、スタッフ用ボタン（ord:sp:<action>）。"""
    if not is_staff(interaction.user):
        return await _ephemeral(interaction, "スタッフのみ操作できます。")
    channel = interaction.channel
    panel = await channel.fetch_message(interaction.message.id)
    state = parse_order_message(panel)
    if state is None:
        return await _ephemeral(interaction, "受付データを読み込めませんでした。")
    transitions = {
        "confirm": ({"reported", "received"}, "paid"),
        "done": ({"paid"}, "done"),
        "cancel": ({"payment_select", "submitted", "link_sent", "received", "reported", "paid"}, "cancelled"),
    }
    if action not in transitions:
        return await _ephemeral(interaction, "不明な操作です。")
    allowed, new_status = transitions[action]
    if state["status"] not in allowed:
        return await _ephemeral(interaction, f"現在の状態（{STATUS_TEXT[state['status']]}）ではこの操作はできません。")
    await interaction.response.defer()
    if await apply_status_change(channel, panel, state, new_status) is None:
        return await _ephemeral(interaction, "画面を更新できませんでした。もう一度お試しください。")
    logger.info("注文 #%s を %s に変更 (staff=%s)", state["order_id"], new_status, interaction.user.id)


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
        channel, panel = await _fetch_ticket_panel(channel_id, message_id)
        if panel is None:
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
            new_panel = await apply_status_change(channel, panel, state, new_status)
            if new_panel is None:
                await _ephemeral(interaction, "画面を更新できませんでした。もう一度お試しください。")
                return
            panel = new_panel
            logger.info(
                "注文 #%s を %s に変更 (staff=%s)",
                state["order_id"], new_status, interaction.user.id,
            )

        title = interaction.message.embeds[0].title if interaction.message.embeds else "注文"
        await interaction.edit_original_response(
            embed=build_staff_embed(state, channel, title, panel.jump_url, interaction.user),
            view=staff_view(state, channel_id, panel.id),
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
        if parts[1] == "delete":
            await handle_order_delete(interaction)
            return
        if parts[1] == "sp":
            async with order_locks[interaction.message.id]:
                await handle_panel_staff_action(interaction, parts[2] if len(parts) > 2 else "")
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
        await open_ticket(interaction)


async def open_ticket(interaction, order_category=None):
    """チケットを作成する（旧パネルのボタン・新パネルのメニュー共通）。

    order_category を指定すると、受付パネルは「ご依頼の種類」を選んだ状態から始まる。
    """
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

            for target in await ticket_extra_access_targets(guild):
                if target not in overwrites:
                    overwrites[target] = discord.PermissionOverwrite(**TICKET_EXTRA_ACCESS_PERMS)

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

            # 「ようこそ」はステップが進んでも消さずに残す
            await channel.send(view=render_welcome(user), allowed_mentions=PANEL_MENTIONS)
            await send_order_panel(channel, user.id, order_category)

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


# ---- 新しいチケット作成パネル（上部に動画・メニューで種類を選ぶ） ----

# 上部の動画（RSGYの紹介動画をGIFにしたもの。GIFはDiscordで自動再生される）。黒・アイボリー・シャンパンゴールドの雰囲気に合わせる
TICKET_BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "ticket_banner.gif")
TICKET_BANNER_FILENAME = "ticket_banner.gif"

TICKET_MENU_OPTIONS = [
    ("trophy", "トロ上げ代行", "🏆", f"¥{min(TROPHY_PRICES.values()):,}〜／料金は自動計算"),
    ("ranked", "ガチバ代行", "⚔️", f"¥{min(RANK_PRICES.values()):,}〜／料金は自動計算"),
    ("cheat", "チート", "🗝️", "スタッフが個別にご案内します"),
    ("middle", "仲介", "🤝", f"お取引の仲介／手数料 ¥{MIDDLE_FEE:,}"),
]


def ticket_banner_media():
    """上部の動画・画像。ORDER_BANNER_URL があればそれを、なければ同梱の動画を使う。"""
    if ORDER_BANNER_URL:
        return ORDER_BANNER_URL
    if os.path.exists(TICKET_BANNER_PATH):
        return f"attachment://{TICKET_BANNER_FILENAME}"
    return None


def ticket_banner_files():
    """パネルを新しく送るときに添付する動画。"""
    if not ORDER_BANNER_URL and os.path.exists(TICKET_BANNER_PATH):
        return [discord.File(TICKET_BANNER_PATH, filename=TICKET_BANNER_FILENAME)]
    return []


def build_ticket_panel_layout():
    container = discord.ui.Container(accent_colour=COLOR_CHAMPAGNE)
    banner = ticket_banner_media()
    if banner:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(banner)))
    container.add_item(discord.ui.TextDisplay(
        "-# R S G Y　　/　　O R D E R　D E S K\n"
        "# ご注文受付\n"
        "下のメニューから種類を選ぶと、あなた専用のチケットが作成されます。"
    ))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.ActionRow(discord.ui.Select(
        custom_id="tp:menu",
        placeholder="ご依頼の種類を選択",
        options=[
            discord.SelectOption(label=label, value=value, emoji=emoji, description=desc)
            for value, label, emoji, desc in TICKET_MENU_OPTIONS
        ],
    )))
    container.add_item(discord.ui.ActionRow(discord.ui.Button(
        label="料金表",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        custom_id="tp:prices",
    )))
    container.add_item(discord.ui.Separator(visible=False))
    container.add_item(discord.ui.TextDisplay(
        "-# ✦　メニューは使うたびに元に戻ります　·　お支払いは PayPay / LTC"
    ))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def price_table_text():
    lines = ["## 📋 料金表", "### 🏆 トロ上げ代行"]
    for (start, goal), price in sorted(TROPHY_PRICES.items()):
        lines.append(f"・{start:,} → {goal:,} トロ　**¥{price:,}**")
    lines.append("### ⚔️ ガチバ代行")
    for (start, goal), price in sorted(RANK_PRICES.items()):
        lines.append(f"・{RANK_NAMES[start]} → {RANK_NAMES[goal]}　**¥{price:,}**")
    lines += [
        "-# 表にない区間は、つながる区間を合計して自動で計算します（マスター帯以降は除く）。",
        "### 🧩 チート",
        "・スタッフが個別にご案内します",
        "### 🤝 仲介",
        f"・手数料　**¥{MIDDLE_FEE:,}**",
    ]
    return "\n".join(lines)


@bot.listen("on_interaction")
async def on_ticket_panel_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("tp:"):
        return
    try:
        if custom_id == "tp:prices":
            await interaction.response.send_message(price_table_text(), ephemeral=True)
            return
        if custom_id == "tp:menu":
            values = (interaction.data or {}).get("values") or []
            order_category = values[0] if values else None
            if order_category not in MENU_CHOICES:
                await _ephemeral(interaction, "不正な選択です。")
                return
            await open_ticket(interaction, order_category)
            # 選んだ項目が残らないよう、メニューを元に戻す（失敗してもチケット作成には影響しない）
            try:
                await interaction.message.edit(view=build_ticket_panel_layout())
            except discord.HTTPException as error:
                log_http_error("チケット作成メニューのリセット", error)
    except discord.HTTPException as error:
        log_http_error("チケット作成パネルの操作", error)
        await _ephemeral(interaction, "通信エラーが発生しました。もう一度お試しください。")
    except Exception as error:
        log_unexpected_error("チケット作成パネルの操作", error)
        await _ephemeral(interaction, "エラーが発生しました。スタッフにお問い合わせください。")


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
        await finalize_reviews_for_channel(interaction.channel.id)
        await interaction.channel.delete()


# ==========================================
# 15-2. 無言チケット・注文なしクローズの罰
# ==========================================

# 「受付済み」以降まで進んだ注文は「注文した」とみなす（キャンセルは注文なし扱い）
ORDERED_STATUSES = {"submitted", "link_sent", "received", "reported", "paid", "done"}

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
    """設定されたチャンネルに警告メッセージを送る。"""
    channel = bot.get_channel(NO_ORDER_NOTICE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(NO_ORDER_NOTICE_CHANNEL_ID)
        except discord.HTTPException as error:
            log_http_error("無言チケット警告チャンネルの取得", error)
            return
    return await safe_send(
        channel,
        f"<@&{NO_ORDER_ROLE_ID}>\n"
        "無言チケットやめよ😅\n"
        "-# 訳がある場合はチケットを開いてオーナーをメンションしてください",
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )


async def no_order_notice_loop():
    await bot.wait_until_ready()
    logger.info(
        "無言チケット警告の定期送信を開始: チャンネル=%s / 間隔=%s秒",
        NO_ORDER_NOTICE_CHANNEL_ID,
        NO_ORDER_NOTICE_INTERVAL,
    )
    loop = asyncio.get_running_loop()
    first_send_logged = False
    while not bot.is_closed():
        started_at = loop.time()
        try:
            sent = await send_no_order_notice()
            if sent is not None and not first_send_logged:
                logger.info("無言チケット警告の初回送信成功: メッセージID=%s", sent.id)
                first_send_logged = True
        except Exception as error:
            log_unexpected_error("無言チケット警告の定期送信", error)
        elapsed = loop.time() - started_at
        await asyncio.sleep(max(0, NO_ORDER_NOTICE_INTERVAL - elapsed))


async def punish_no_order(guild, user_id, reason):
    """罰ロールを付ける。すでにロールがある人には何もしない。"""
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
# 15-3. 実績（完了した注文の掲載・コメントの催促）
# ==========================================

def build_review_modal():
    modal = discord.ui.Modal(title="実績へのコメント", custom_id="ord:modal:review:n", timeout=900)
    modal.add_item(discord.ui.Label(
        text="コメント",
        description="ご利用の感想をお聞かせください。実績チャンネルに載ります。",
        component=discord.ui.TextInput(
            custom_id="comment",
            style=discord.TextStyle.paragraph,
            placeholder="例：早く丁寧に対応してもらえました！",
            required=True,
            max_length=300,
        ),
    ))
    modal.add_item(discord.ui.Label(
        text="名前を伏せますか？",
        component=discord.ui.Select(
            custom_id="anon",
            placeholder="選んでください",
            options=[
                discord.SelectOption(label="はい（名前を伏せる）", value="yes", emoji="🙈"),
                discord.SelectOption(label="いいえ（名前を出して大丈夫）", value="no", emoji="🙆"),
            ],
        ),
    ))
    return modal


def _format_duration(seconds):
    if seconds is None or seconds < 0:
        return "―"
    hours = max(1, round(seconds / 3600))
    if hours < 48:
        return f"約{hours}時間"
    return f"約{round(seconds / 86400)}日"


def build_jisseki_view(state, comment=None, anonymous=False):
    meta = state.get("meta", {})
    amount = order_amount(state)
    submitted_at, done_at = meta.get("submitted_at"), meta.get("done_at")
    duration = done_at - submitted_at if (submitted_at and done_at) else None
    answers = state["answers"]
    if answers.get("category") == "middle":
        detail = "お取引の仲介"
    else:
        detail = _range_text(state)
    name = "匿名のお客様" if anonymous else f"<@{state['owner_id']}>"
    if comment:
        comment_text = "\n".join(f"> {line}" for line in comment.splitlines() if line.strip())
    else:
        comment_text = "-# コメントなし"
    done_date = datetime.fromtimestamp(done_at or time.time(), timezone.utc).astimezone()
    return _layout(
        _head("-# RSGY　/　実績", f"## {order_title(state)}", detail or None),
        _sep(),
        _text(
            f"金額　**{f'¥{amount:,}' if amount else '―'}**\n"
            f"期間　**{_format_duration(duration)}**\n"
            f"お客様　**{name}**"
        ),
        _sep(),
        _text(comment_text),
        _text(f"-# {done_date:%Y/%m/%d} 完了"),
    )


async def post_jisseki(state, comment=None, anonymous=False):
    channel = bot.get_channel(JISSEKI_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(JISSEKI_CHANNEL_ID)
        except discord.HTTPException as error:
            log_http_error("実績チャンネルの取得", error)
            return False
    try:
        await channel.send(
            view=build_jisseki_view(state, comment, anonymous),
            allowed_mentions=discord.AllowedMentions(users=not anonymous, roles=False, everyone=False),
        )
    except discord.HTTPException as error:
        log_http_error("実績の投稿", error)
        return False
    logger.info("実績を掲載: #%s（匿名=%s・コメント=%s）", state["order_id"], anonymous, bool(comment))
    return True


async def finalize_review(order_id, comment=None, anonymous=False):
    """実績を掲載して、注文を「掲載済み」にする。完了画面が残っていれば書き換える。"""
    entry = load_order(order_id)
    if entry is None:
        return False
    state = copy.deepcopy(entry["state"])
    review = state.setdefault("meta", {}).setdefault("review", {})
    if state["status"] != "done" or review.get("status") == "posted":
        return False
    if not await post_jisseki(state, comment, anonymous):
        return False
    review.update({"status": "posted", "posted_at": time.time(), "anonymous": anonymous, "comment": bool(comment)})
    save_order(state, entry["channel_id"], entry["message_id"])
    channel = bot.get_channel(entry["channel_id"])
    if channel is not None:
        try:
            message = await channel.fetch_message(entry["message_id"])
            await edit_panel(message, state)
        except discord.HTTPException:
            pass  # 完了画面がなくても掲載はできている
    return True


async def submit_review(interaction, state, values):
    comment = clean_text(values.get("comment"), 300)
    anon = values.get("anon")
    if not comment:
        return await _ephemeral(interaction, "コメントを入力してください。")
    if anon not in ("yes", "no"):
        return await _ephemeral(interaction, "名前を伏せるかどうかを選んでください。")
    await interaction.response.defer(ephemeral=True, thinking=True)
    if await finalize_review(state["order_id"], comment, anonymous=anon == "yes"):
        await interaction.followup.send("実績に掲載しました。ありがとうございました！", ephemeral=True)
    else:
        await interaction.followup.send("実績に掲載できませんでした。時間をおいてもう一度お試しください。", ephemeral=True)


async def finalize_reviews_for_channel(channel_id):
    """チケットを消す前に、コメント待ちの実績を（コメントなし・名前ありで）掲載する。"""
    for order_id, entry in list(_load_order_store().items()):
        state = entry.get("state", {})
        if (entry.get("channel_id") == channel_id and state.get("status") == "done"
                and state.get("meta", {}).get("review", {}).get("status") == "pending"):
            await finalize_review(order_id)


async def check_pending_reviews():
    now = time.time()
    for order_id, entry in list(_load_order_store().items()):
        state = copy.deepcopy(entry.get("state", {}))
        review = state.get("meta", {}).get("review", {})
        if state.get("status") != "done" or review.get("status") != "pending":
            continue
        done_at = state["meta"].get("done_at", now)
        if now - done_at >= REVIEW_DEADLINE_HOURS * 3600:
            await finalize_review(order_id)
            continue
        if now - review.get("last_remind", done_at) < REVIEW_REMIND_SECONDS:
            continue
        channel = bot.get_channel(entry["channel_id"])
        if channel is None:
            # チケットが消えている場合は、その場で掲載する
            await finalize_review(order_id)
            continue
        remaining = max(1, round((done_at + REVIEW_DEADLINE_HOURS * 3600 - now) / 3600))
        await safe_send(
            channel,
            f"<@{state['owner_id']}> 実績へのコメントをお願いします！\n"
            "完了画面の「✍️ 実績にコメントする」から書けます（名前を伏せることもできます）。\n"
            f"-# あと約{remaining}時間で、コメントなし・お名前ありで実績に載ります。",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        review["last_remind"] = now
        save_order(state, entry["channel_id"], entry["message_id"])


async def review_reminder_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await check_pending_reviews()
        except Exception as error:
            log_unexpected_error("実績コメントの催促", error)
        await asyncio.sleep(REVIEW_CHECK_INTERVAL)


async def handle_order_delete(interaction):
    """完了画面の「チケットを削除」。注文したお客様本人とスタッフが押せる。"""
    channel = interaction.channel
    message = interaction.message
    state = parse_order_message(message)
    if state is None or not is_ticket(channel):
        return await _ephemeral(interaction, "受付データを読み込めませんでした。")
    is_owner = interaction.user.id == state["owner_id"]
    if not (is_staff(interaction.user) or (is_owner and state["status"] == "done")):
        return await _ephemeral(interaction, "このチケットを削除する権限がありません。")
    await interaction.response.send_message("チケットを削除します。")
    await finalize_reviews_for_channel(channel.id)
    await asyncio.sleep(2)
    await channel.delete(reason=f"完了したチケットを削除（{interaction.user}）")


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
    if not getattr(bot, "_extra_access_granted", False):
        bot._extra_access_granted = True
        try:
            await grant_extra_access_to_existing_tickets()
        except Exception as error:
            log_unexpected_error("既存チケットへの閲覧権限の追加", error)


# ==========================================
# 17. スタッフ用コマンド
# ==========================================

@bot.command()
async def setup_ticket(ctx):
    if not is_command_owner(ctx.author):
        return

    # 上部に動画、メニューで種類を選ぶパネル（以前に出したボタンのパネルも引き続き動く）
    await ctx.send(view=build_ticket_panel_layout(), files=ticket_banner_files())


@bot.command()
async def order_panel(ctx):
    """チケット内に受付パネルを出し直す（スタッフ用）。"""
    if not is_command_owner(ctx.author):
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
