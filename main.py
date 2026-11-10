%pip install -q -U discord.py google-genai

import discord
from discord.ext import commands
from google import genai
import os
import re
import asyncio

# ==========================================
# 1. 基本設定
# ==========================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


if not DISCORD_TOKEN or not GEMINI_API_KEY:
    raise ValueError("DISCORD_TOKENとGEMINI_API_KEYを設定してください。")

# 無料で使える高速・高性能なGeminiのモデル
AI_MODEL = "gemini-2.5-flash"

STAFF_ROLES = [
    1569062644919566336,
    1569106427065401354
]

TICKET_CATEGORY = "チケット"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# OpenAIからGemini（Google GenAI）に変更
ai_client = genai.Client(api_key=GEMINI_API_KEY)

ai_enabled = True
paused_channels = set()
histories = {}
handoff_notified = set()

# ==========================================
# 2. 料金表
# ==========================================

RANK_NAMES = {
    0: "エリート1",
    1: "レジェンド1",
    2: "レジェンド2",
    3: "レジェンド3",
    4: "マスター1",
    5: "マスター2",
    6: "マスター3",
    7: "プロ"
}

RANK_ALIASES = {
    0: ["エリート1", "エリート 1", "エリ1", "エリ 1", "elite1", "elite 1"],
    1: ["レジェンド1", "レジェンド 1", "レジェ1", "レジェ 1", "legend1", "legend 1"],
    2: ["レジェンド2", "レジェンド 2", "レジェ2", "レジェ 2", "legend2", "legend 2"],
    3: ["レジェンド3", "レジェンド 3", "レジェ3", "レジェ 3", "legend3", "legend 3"],
    4: ["マスター1", "マスター 1", "マス1", "マス 1", "master1", "master 1"],
    5: ["マスター2", "マスター 2", "マス2", "マス 2", "master2", "master 2"],
    6: ["マスター3", "マスター 3", "マス3", "マス 3", "master3", "master 3"],
    7: ["プロ", "pro"]
}

RANK_PRICES = {
    (0, 1): 900,
    (1, 4): 4200,
    (2, 4): 3200,
    (3, 4): 2000,
    (4, 5): 8000,
    (5, 6): 20000,
    (6, 7): 40000
}

RANK_SEGMENTS = {
    (0, 1): 900,
    (1, 4): 4200
}

TROPHY_PRICES = {
    (0, 1000): 300,
    (1000, 2000): 700,
    (2000, 3000): 15000,
    (2500, 3000): 8499
}

# ==========================================
# 3. スタッフ判定
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
    except:
        return None

def staff_mentions(guild):
    mentions = []
    for role_id in STAFF_ROLES:
        role = guild.get_role(role_id)
        if role:
            mentions.append(role.mention)
    return " ".join(mentions)

# ==========================================
# 4. ランク認識・料金計算
# ==========================================

def find_ranks(text):
    found = []
    for rank, aliases in RANK_ALIASES.items():
        for alias in aliases:
            pattern = re.escape(alias)
            if re.search(pattern, text, re.IGNORECASE):
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

# ==========================================
# 5. トロフィー料金計算
# ==========================================

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

    price = TROPHY_PRICES.get((start, goal))
    if price is not None:
        return price

    return None

# ==========================================
# 6. AIの設定（Gemini版）
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
    history = histories.get(channel_id, [])

    # Geminiの履歴形式に合わせる、またはプロンプトとして構築する
    # 今回はシンプルにこれまでの会話をテキスト形式で組み立てて渡します
    chat_history_text = SYSTEM_PROMPT + "\n\n【これまでの会話】\n"
    for h in history[-10:]:
        role_name = "ユーザー" if h["role"] == "user" else "AI"
        chat_history_text += f"{role_name}: {h['content']}\n"
    
    chat_history_text += f"ユーザー: {user_message}\nAI:"

    # 非同期でGeminiに問い合わせ
    response = await asyncio.to_thread(
        ai_client.models.generate_content,
        model=AI_MODEL,
        contents=chat_history_text
    )

    answer = response.text.strip()

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": answer})
    histories[channel_id] = history[-16:]

    return answer

# ==========================================
# 7. チケットパネル
# ==========================================

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チケットを作成",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="ticket:create"
    )
    async def create_ticket(self, interaction, button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            return

        for channel in guild.text_channels:
            if is_ticket(channel) and get_ticket_owner(channel) == user.id:
                await interaction.response.send_message(
                    f"すでにチケットがあります：{channel.mention}",
                    ephemeral=True
                )
                return

        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY)
        if category is None:
            category = await guild.create_category(TICKET_CATEGORY)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )
        }

        for role_id in STAFF_ROLES:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True
                )

        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"ticket_owner:{user.id}"
        )

        await interaction.response.send_message(
            f"チケットを作成しました：{channel.mention}",
            ephemeral=True
        )

        await channel.send(
            f"{user.mention} さん、問い合わせありがとうございます！\n\n"
            "まずはお問い合わせ内容を教えてください。\n"
            "ガチバトル代行・トロ上げ代行・その他の相談に対応しています。\n\n"
            "分からないことや料金表にない内容は、すぐにスタッフへ確認します。",
            view=OpenTicketView()
        )

# ==========================================
# 8. チケット操作ボタン
# ==========================================

class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チケットを閉じる",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket:close"
    )
    async def close_ticket(self, interaction, button):
        channel = interaction.channel
        owner_id = get_ticket_owner(channel)

        if not is_staff(interaction.user) and interaction.user.id != owner_id:
            await interaction.response.send_message("この操作は許可されていません。", ephemeral=True)
            return

        owner = interaction.guild.get_member(owner_id)
        if owner:
            await channel.set_permissions(owner, send_messages=False)

        await interaction.response.send_message("チケットを閉じました。", view=ClosedTicketView())


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="再開する",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="ticket:reopen"
    )
    async def reopen_ticket(self, interaction, button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("スタッフのみ操作できます。", ephemeral=True)
            return

        channel = interaction.channel
        owner_id = get_ticket_owner(channel)
        owner = interaction.guild.get_member(owner_id)

        if owner:
            await channel.set_permissions(owner, send_messages=True)

        await interaction.response.send_message("チケットを再開しました。", view=OpenTicketView())

    @discord.ui.button(
        label="チケットを削除",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="ticket:delete"
    )
    async def delete_ticket(self, interaction, button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("スタッフのみ操作できます。", ephemeral=True)
            return

        await interaction.response.send_message("チケットを削除します。")
        await interaction.channel.delete()

# ==========================================
# 9. 起動時の設定
# ==========================================

@bot.event
async def setup_hook():
    bot.add_view(TicketPanel())
    bot.add_view(OpenTicketView())
    bot.add_view(ClosedTicketView())

@bot.event
async def on_ready():
    print(f"Bot起動成功: {bot.user}")
    print(f"AI: {'有効' if ai_enabled else '無効'}")

# ==========================================
# 10. スタッフ用コマンド
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
        color=discord.Color.blurple()
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
# 11. メッセージ処理
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

    if not ai_enabled or channel.id in paused_channels:
        return

    if not ticket:
        mentioned = bot.user in message.mentions
        replied = False
        if message.reference:
            try:
                replied_message = await channel.fetch_message(message.reference.message_id)
                if replied_message.author.id == bot.user.id:
                    replied = True
            except:
                pass
        if not mentioned and not replied:
            return

    # 料金の自動判定
    try:
        rank_price = calculate_rank_price(message.content)
        if rank_price is not None:
            await channel.send(
                f"料金表に基づく見積もりは **{rank_price:,}円** です。\n"
                "ご依頼内容に間違いがないか確認してください。"
            )
            return

        trophy_price = calculate_trophy_price(message.content)
        if trophy_price is not None:
            await channel.send(
                f"料金表に基づく見積もりは **{trophy_price:,}円** です。\n"
                "ご依頼内容に間違いがないか確認してください。"
            )
            return
    except Exception as e:
        print("料金判定エラー:", repr(e))

    # AIに問い合わせ
    async with channel.typing():
        try:
            answer = await ask_ai(channel.id, message.content)

            if "[STAFF_HANDOFF]" in answer:
                answer = answer.replace("[STAFF_HANDOFF]", "").strip()
                await channel.send(answer)

                if channel.id not in handoff_notified:
                    mentions = staff_mentions(message.guild)
                    await channel.send(f"⚠️ **スタッフへの確認が必要です。**\n{mentions}")
                    handoff_notified.add(channel.id)
                return

            await channel.send(answer)

        except Exception as e:
            print("AIエラー:", repr(e))
            await channel.send(
                "申し訳ありません。AIの応答でエラーが発生しました。\n"
                "スタッフに確認を依頼してください。"
            )
            if channel.id not in handoff_notified:
                mentions = staff_mentions(message.guild)
                await channel.send(f"⚠️ **スタッフの対応をお願いします。**\n{mentions}")
                handoff_notified.add(channel.id)

# ==========================================
# 12. Bot起動
# ==========================================

print("Botを起動します...")
await bot.start(DISCORD_TOKEN)
