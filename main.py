import os
import re
import sys
import asyncio
import logging
import threading
import traceback
from collections import defaultdict

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
# 11. チケットパネル
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

            await channel.send(
                f"{user.mention} さん、問い合わせありがとうございます！\n\n"
                "まずはお問い合わせ内容を教えてください。\n"
                "ガチバトル代行・トロ上げ代行・その他の相談に対応しています。\n\n"
                "分からないことや料金表にない内容は、すぐにスタッフへ確認します。",
                view=OpenTicketView(),
            )

        except discord.HTTPException as error:
            log_http_error("チケット作成", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "チケットの作成に失敗しました。スタッフに確認してください。",
                    ephemeral=True,
                )


# ==========================================
# 12. チケット操作
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
            await channel.set_permissions(
                discord.Object(id=owner_id),
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
            await channel.set_permissions(
                discord.Object(id=owner_id),
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
# 13. 起動・接続イベント
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
    logger.info("================================")


# ==========================================
# 14. スタッフ用コマンド
# ==========================================

@bot.command()
async def setup_ticket(ctx):
    if not is_staff(ctx.author):
        return

    embed = discord.Embed(
        title="お問い合わせ",
        description=(
            "お問い合わせはこちらからお願いします。\n\n"
            "🎫 ボタンを押すと専用チケットが作成されます。\n"
            "料金の確認や代行に関する相談もこちらで受け付けています。"
        ),
        color=discord.Color.blurple(),
    )

    await ctx.send(embed=embed, view=TicketPanel())


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
# 15. メッセージ処理
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

    if not ticket:
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
# 16. Discordイベントエラー
# ==========================================

@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception("Discordイベントエラー: %s", event_method)


# ==========================================
# 17. Bot起動
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
