"""Nuke対策（/nuke対策）

- `/nuke対策` で有効化すると、サーバーのロール・チャンネルの状態を保存する。
- 有効化後は、ロールやチャンネルが作成・変更・削除されるたびに（少し待ってから）自動で保存し直す。
- バックアップは指定ユーザーのDMにJSONファイルとして送る（Bot再起動後もDMから読み戻す）。
- Bot（アプリ）が短時間に大量の変更（=大規模な変更）をしたら、
  そのBotの権限を剥奪してBANし、保存しておいた状態にサーバーを戻す。
"""

import asyncio
import io
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands


logger = logging.getLogger("SupportBot.AntiNuke")


# ==========================================
# 設定
# ==========================================

# 「大規模な変更」の判定: 同じBotが DETECT_WINDOW_SECONDS 秒以内に
# 監視対象の操作を DETECT_THRESHOLD 回以上行ったら検知する。
DETECT_WINDOW_SECONDS = 10
DETECT_THRESHOLD = 5

# 変更を検知してから自動保存するまでの待ち時間（秒）。
# 連続した変更をまとめて1回の保存にする。
SAVE_DEBOUNCE_SECONDS = 60

# Botによる削除・BANなどがあった直後は、壊された状態を保存しないよう保存を保留する（秒）。
SAVE_QUIET_SECONDS = 60

# 攻撃Botの操作履歴（復元時に「攻撃で作られたもの」を消すのに使う）を保持する時間（秒）。
ACTOR_HISTORY_SECONDS = 600

BACKUP_FILE_PREFIX = "nuke_backup_"
BACKUP_HISTORY_LIMIT = 300
REASON = "Nuke対策: 自動復元"

A = discord.AuditLogAction

# 「大規模な変更」として数える操作
WATCHED_ACTIONS = {
    A.channel_create,
    A.channel_delete,
    A.channel_update,
    A.overwrite_create,
    A.overwrite_update,
    A.overwrite_delete,
    A.role_create,
    A.role_delete,
    A.role_update,
    A.ban,
    A.kick,
    A.member_prune,
    A.webhook_create,
    A.guild_update,
}

# 直後の自動保存を保留する操作（壊された状態をバックアップに上書きしないため）
DESTRUCTIVE_ACTIONS = {
    A.channel_delete,
    A.role_delete,
    A.ban,
    A.kick,
    A.member_prune,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _short_error(error):
    text = getattr(error, "text", None) or str(error)
    return f"{type(error).__name__}: {text}"[:150]


class AntiNuke:
    def __init__(self, bot, backup_user_id, exclude_channel=None, whitelist_bot_ids=()):
        self.bot = bot
        self.backup_user_id = backup_user_id
        self.exclude_channel = exclude_channel or (lambda channel: False)
        self.whitelist = set(whitelist_bot_ids)

        self.snapshots = {}            # guild_id -> 保存データ(dict)
        self.backup_messages = {}      # guild_id -> DMのバックアップメッセージ
        self.autosave_paused = set()   # 復元に失敗があり、自動保存を止めているサーバー
        self.save_tasks = {}           # guild_id -> 自動保存待ちのTask
        self.save_locks = defaultdict(asyncio.Lock)

        # guild_id -> actor_id -> deque[(時刻, action, target_id)]
        self.actor_log = defaultdict(lambda: defaultdict(deque))
        self.last_bot_destructive = {}   # guild_id -> 時刻
        self.handled = defaultdict(set)  # guild_id -> 処理済み(処罰済み)のBot ID
        self.pending_attackers = defaultdict(set)
        self.incident = set()            # 復元中のサーバー
        self.loaded = False

    # ------------------------------------------
    # 状態
    # ------------------------------------------

    def is_enabled(self, guild_id):
        snap = self.snapshots.get(guild_id)
        return bool(snap and snap.get("enabled"))

    # ------------------------------------------
    # スナップショット
    # ------------------------------------------

    def take_snapshot(self, guild):
        roles = []
        for role in guild.roles:
            if role.managed:
                # Bot・連携サービス用のロールは作り直せないため保存しない
                continue
            data = {
                "id": role.id,
                "name": role.name,
                "permissions": role.permissions.value,
                "color": role.colour.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "position": role.position,
                "default": role.is_default(),
            }
            if not role.is_default():
                data["members"] = [member.id for member in role.members]
            roles.append(data)

        channels = []
        for channel in guild.channels:
            if self.exclude_channel(channel):
                continue
            data = {
                "id": channel.id,
                "name": channel.name,
                "type": channel.type.name,
                "position": channel.position,
                "category_id": channel.category_id,
                "overwrites": [
                    {
                        "id": ow.id,
                        "type": "role" if ow.is_role() else "member",
                        "allow": ow.allow,
                        "deny": ow.deny,
                    }
                    for ow in getattr(channel, "_overwrites", [])
                ],
            }
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                data["topic"] = channel.topic
                data["nsfw"] = channel.nsfw
                data["slowmode_delay"] = channel.slowmode_delay
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                data["bitrate"] = channel.bitrate
                data["user_limit"] = channel.user_limit
            channels.append(data)

        return {
            "version": 1,
            "enabled": True,
            "guild_id": guild.id,
            "guild_name": guild.name,
            "saved_at": _now_iso(),
            "roles": roles,
            "channels": channels,
        }

    @staticmethod
    def _comparable(snapshot):
        if not snapshot:
            return None
        return {k: v for k, v in snapshot.items() if k != "saved_at"}

    async def save_now(self, guild, *, force=False, enabled=True):
        """現在のサーバーの状態を保存し、DMのバックアップを更新する。"""
        async with self.save_locks[guild.id]:
            snapshot = self.take_snapshot(guild)
            snapshot["enabled"] = enabled
            if not force and self._comparable(snapshot) == self._comparable(self.snapshots.get(guild.id)):
                return True
            self.snapshots[guild.id] = snapshot
            self.autosave_paused.discard(guild.id)
            return await self.upload_backup(guild, snapshot)

    # ------------------------------------------
    # DMへのバックアップ
    # ------------------------------------------

    async def _dm_channel(self):
        user = self.bot.get_user(self.backup_user_id) or await self.bot.fetch_user(self.backup_user_id)
        return user.dm_channel or await user.create_dm()

    async def upload_backup(self, guild, snapshot):
        payload = json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8")
        saved_at = int(datetime.fromisoformat(snapshot["saved_at"]).timestamp())
        content = (
            "🛡️ **Nuke対策バックアップ**\n"
            f"サーバー: {guild.name}（{guild.id}）\n"
            f"最終保存: <t:{saved_at}:f>\n"
            f"ロール {len(snapshot['roles'])} 件 / チャンネル {len(snapshot['channels'])} 件\n"
            f"状態: {'有効' if snapshot.get('enabled') else '無効'}\n"
            "※ このメッセージは消さないでください（Bot再起動後の復元に使います）"
        )

        def make_file():
            return discord.File(io.BytesIO(payload), filename=f"{BACKUP_FILE_PREFIX}{guild.id}.json")

        try:
            message = self.backup_messages.get(guild.id)
            if message is not None:
                try:
                    await message.edit(content=content, attachments=[make_file()])
                    return True
                except discord.NotFound:
                    self.backup_messages.pop(guild.id, None)
            dm = await self._dm_channel()
            self.backup_messages[guild.id] = await dm.send(content, file=make_file())
            return True
        except discord.HTTPException as error:
            logger.warning("Nuke対策バックアップをDMに送れませんでした: %s", _short_error(error))
            return False

    async def load_backups(self):
        """Bot起動時に、DMに保存したバックアップを読み戻す。"""
        if self.loaded:
            return
        self.loaded = True
        try:
            dm = await self._dm_channel()
            async for message in dm.history(limit=BACKUP_HISTORY_LIMIT):
                if message.author.id != self.bot.user.id:
                    continue
                for attachment in message.attachments:
                    name = attachment.filename
                    if not (name.startswith(BACKUP_FILE_PREFIX) and name.endswith(".json")):
                        continue
                    try:
                        guild_id = int(name[len(BACKUP_FILE_PREFIX):-len(".json")])
                    except ValueError:
                        continue
                    # 新しいメッセージから順に見るので、最初に見つけたものが最新
                    if guild_id in self.snapshots or self.bot.get_guild(guild_id) is None:
                        continue
                    data = json.loads(await attachment.read())
                    if data.get("guild_id") != guild_id:
                        continue
                    self.snapshots[guild_id] = data
                    self.backup_messages[guild_id] = message
            logger.info("Nuke対策: %s サーバー分のバックアップを読み込みました。", len(self.snapshots))
        except Exception as error:  # 読み込み失敗でBot全体を止めない
            logger.warning("Nuke対策バックアップの読み込みに失敗しました: %s", _short_error(error))

    # ------------------------------------------
    # 自動保存
    # ------------------------------------------

    def schedule_save(self, guild):
        if guild is None or not self.is_enabled(guild.id):
            return
        if guild.id in self.incident or guild.id in self.autosave_paused:
            return
        task = self.save_tasks.get(guild.id)
        if task is not None and not task.done():
            return  # すでに保存待ち（保存時点の最新状態が保存される）
        self.save_tasks[guild.id] = asyncio.create_task(self._delayed_save(guild.id))

    async def _delayed_save(self, guild_id):
        try:
            await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
            while True:
                last = self.last_bot_destructive.get(guild_id, 0)
                if guild_id in self.incident or time.monotonic() - last < SAVE_QUIET_SECONDS:
                    await asyncio.sleep(SAVE_QUIET_SECONDS)
                    continue
                break
            guild = self.bot.get_guild(guild_id)
            if guild is None or not self.is_enabled(guild_id) or guild_id in self.autosave_paused:
                return
            await self.save_now(guild)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.warning("Nuke対策の自動保存に失敗しました: %s", _short_error(error))

    def _cancel_save(self, guild_id):
        task = self.save_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ------------------------------------------
    # 検知
    # ------------------------------------------

    async def on_audit_log_entry_create(self, entry):
        guild = entry.guild
        if entry.action not in WATCHED_ACTIONS or not self.is_enabled(guild.id):
            return
        actor_id = entry.user_id
        if actor_id is None or actor_id == self.bot.user.id or actor_id in self.whitelist:
            return

        actor = entry.user or guild.get_member(actor_id)
        if actor is None:
            try:
                actor = await self.bot.fetch_user(actor_id)
            except discord.HTTPException:
                return
        if not actor.bot:
            return  # 対象はBot（アプリ）のみ

        target_id = getattr(entry, "_target_id", None)
        if target_id is None:
            target_id = getattr(entry.target, "id", None)

        now = time.monotonic()
        log = self.actor_log[guild.id][actor_id]
        log.append((now, entry.action, target_id))
        while log and now - log[0][0] > ACTOR_HISTORY_SECONDS:
            log.popleft()

        if entry.action in DESTRUCTIVE_ACTIONS:
            self.last_bot_destructive[guild.id] = now

        if actor_id in self.handled[guild.id]:
            return  # 処理中。復元時に消せるよう履歴だけ記録しておく

        recent = sum(1 for ts, _, _ in log if now - ts <= DETECT_WINDOW_SECONDS)
        if recent >= DETECT_THRESHOLD:
            self.handled[guild.id].add(actor_id)
            asyncio.create_task(self.handle_attack(guild, actor, recent))

    # ------------------------------------------
    # 対処
    # ------------------------------------------

    async def handle_attack(self, guild, actor, count):
        logger.warning("Nuke検知: guild=%s bot=%s(%s) %s回/%s秒", guild.id, actor, actor.id, count, DETECT_WINDOW_SECONDS)
        self._cancel_save(guild.id)
        self.pending_attackers[guild.id].add(actor.id)
        already_restoring = guild.id in self.incident
        self.incident.add(guild.id)

        # 復元中にも壊され続けないよう、先に権限を剥奪してBANする
        punish_lines = await self.punish(guild, actor.id)
        await self.notify(
            f"🚨 **Nukeを検知しました**（{guild.name}）\n"
            f"Bot: {actor}（{actor.id}）\n"
            f"{DETECT_WINDOW_SECONDS}秒間に {count} 回の大規模な変更\n"
            + "\n".join(punish_lines)
            + ("\n保存した状態への復元を開始します…" if not already_restoring else "")
        )
        if already_restoring:
            return  # 実行中の復元処理がこのBotの分もまとめて処理する

        attackers = set()
        lines = []
        had_error = False
        try:
            await asyncio.sleep(3)  # 攻撃中の操作のイベントが届き切るのを待つ
            while self.pending_attackers[guild.id]:
                batch = set(self.pending_attackers[guild.id])
                self.pending_attackers[guild.id].clear()
                attackers |= batch
                result_lines, errors = await self.restore(guild, batch)
                lines.extend(result_lines)
                if errors:
                    had_error = True
                    lines.append("⚠️ 失敗した処理:")
                    lines.extend(f"・{e}" for e in errors[:15])
                    if len(errors) > 15:
                        lines.append(f"・ほか {len(errors) - 15} 件")
        except Exception as error:
            had_error = True
            logger.exception("Nuke対策の復元中にエラー")
            lines.append(f"⚠️ 復元中にエラー: {_short_error(error)}")
        finally:
            self.incident.discard(guild.id)
            for actor_id in attackers:
                self.handled[guild.id].discard(actor_id)
                self.actor_log[guild.id].pop(actor_id, None)

        if had_error:
            # 復元しきれなかった状態でバックアップを上書きしないよう、自動保存を止める
            self.autosave_paused.add(guild.id)
            lines.append(
                "\n一部を復元できなかったため、バックアップは上書きせず自動保存を停止しました。"
                "サーバーを確認・修正してから `/nuke対策` の「今すぐ保存」を実行してください。"
            )
        else:
            await self.save_now(guild, force=True)
            lines.append("\nバックアップを復元後の状態で更新しました。")

        await self.notify(f"✅ **復元処理が完了しました**（{guild.name}）\n" + "\n".join(lines))

    async def punish(self, guild, actor_id):
        lines = []
        me = guild.me
        member = guild.get_member(actor_id)
        if member is not None:
            removable = [
                r for r in member.roles
                if not r.is_default() and not r.managed and r < me.top_role
            ]
            if removable:
                try:
                    await member.remove_roles(*removable, reason="Nuke対策: 権限剥奪")
                    lines.append(f"・ロール {len(removable)} 件を剥奪しました")
                except discord.HTTPException as error:
                    lines.append(f"・ロールの剥奪に失敗: {_short_error(error)}")
            for role in member.roles:
                if role.managed and role.permissions.value and role < me.top_role:
                    try:
                        await role.edit(permissions=discord.Permissions.none(), reason="Nuke対策: 権限剥奪")
                        lines.append(f"・Bot専用ロール「{role.name}」の権限をすべて外しました")
                    except discord.HTTPException as error:
                        lines.append(f"・Bot専用ロールの権限剥奪に失敗: {_short_error(error)}")
        try:
            await guild.ban(discord.Object(id=actor_id), reason="Nuke対策: 大規模な変更を行ったBot", delete_message_seconds=0)
            lines.append("・BANしました")
        except discord.HTTPException as error:
            lines.append(
                f"・BANに失敗: {_short_error(error)}"
                "（このBotのロールを相手より上に置いてください）"
            )
        return lines

    async def notify(self, text):
        try:
            dm = await self._dm_channel()
            for start in range(0, len(text), 1900):
                await dm.send(text[start:start + 1900])
        except discord.HTTPException as error:
            logger.warning("Nuke対策の通知をDMに送れませんでした: %s", _short_error(error))

    # ------------------------------------------
    # 復元
    # ------------------------------------------

    async def restore(self, guild, attackers):
        snapshot = self.snapshots.get(guild.id)
        if not snapshot:
            return ["保存データがないため復元できませんでした。"], []

        errors = []
        counts = defaultdict(int)
        me = guild.me

        failed = object()

        async def attempt(label, coro):
            """失敗しても復元を続けるためのラッパー。失敗時は failed を返す。"""
            try:
                return await coro
            except discord.HTTPException as error:
                errors.append(f"{label}: {_short_error(error)}")
                return failed

        def attacker_actions():
            return {
                act
                for actor_id in attackers
                for _, act, _ in self.actor_log[guild.id].get(actor_id, ())
            }

        def targets(action):
            ids = set()
            for actor_id in attackers:
                for _, act, target_id in self.actor_log[guild.id].get(actor_id, ()):
                    if act == action and target_id:
                        ids.add(target_id)
            return ids

        snap_role_ids = {r["id"] for r in snapshot["roles"]}
        snap_channel_ids = {c["id"] for c in snapshot["channels"]}

        # 1. 攻撃で作られたチャンネル・ロール・Webhookを削除（上限に引っかからないよう先に消す）
        for channel_id in targets(A.channel_create) - snap_channel_ids:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                if await attempt(f"チャンネル削除 #{channel.name}", channel.delete(reason=REASON)) is not failed:
                    counts["deleted_channels"] += 1
        for role_id in targets(A.role_create) - snap_role_ids:
            role = guild.get_role(role_id)
            if role is not None and not role.managed and role < me.top_role:
                if await attempt(f"ロール削除 {role.name}", role.delete(reason=REASON)) is not failed:
                    counts["deleted_roles"] += 1
        webhook_ids = targets(A.webhook_create)
        if webhook_ids:
            hooks = await attempt("Webhook一覧の取得", guild.webhooks())
            for hook in ([] if hooks is failed else hooks):
                if hook.id in webhook_ids:
                    if await attempt("Webhook削除", hook.delete(reason=REASON)) is not failed:
                        counts["deleted_webhooks"] += 1

        # 2. ロール
        role_map = {}
        recreated_roles = []
        for data in sorted(snapshot["roles"], key=lambda r: r["position"]):
            if data.get("default"):
                if guild.default_role.permissions.value != data["permissions"]:
                    if await attempt(
                        "@everyone の権限",
                        guild.default_role.edit(permissions=discord.Permissions(data["permissions"]), reason=REASON),
                    ) is not failed:
                        counts["fixed_roles"] += 1
                continue

            role = guild.get_role(data["id"])
            if role is None:
                new_role = await attempt(
                    f"ロール作成 {data['name']}",
                    guild.create_role(
                        name=data["name"],
                        permissions=discord.Permissions(data["permissions"]),
                        colour=discord.Colour(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                        reason=REASON,
                    ),
                )
                if new_role is not failed:
                    role_map[data["id"]] = new_role
                    recreated_roles.append((data, new_role))
                    counts["recreated_roles"] += 1
                continue

            role_map[data["id"]] = role
            if role.managed or role >= me.top_role:
                continue
            changes = {}
            if role.name != data["name"]:
                changes["name"] = data["name"]
            if role.permissions.value != data["permissions"]:
                changes["permissions"] = discord.Permissions(data["permissions"])
            if role.colour.value != data["color"]:
                changes["colour"] = discord.Colour(data["color"])
            if role.hoist != data["hoist"]:
                changes["hoist"] = data["hoist"]
            if role.mentionable != data["mentionable"]:
                changes["mentionable"] = data["mentionable"]
            if changes:
                if await attempt(f"ロール修正 {data['name']}", role.edit(reason=REASON, **changes)) is not failed:
                    counts["fixed_roles"] += 1

        if recreated_roles:
            # 作り直したロールの並び順を、保存時の順番に近づける（失敗しても続行）
            snap_pos = {r["id"]: r["position"] for r in snapshot["roles"]}
            new_to_old = {new.id: data["id"] for data, new in recreated_roles}
            movable = [
                r for r in guild.roles
                if not r.is_default() and r < me.top_role
            ]

            def sort_key(r):
                old_id = new_to_old.get(r.id, r.id)
                return (snap_pos.get(old_id, r.position), r.position)

            positions = {r: i + 1 for i, r in enumerate(sorted(movable, key=sort_key))}
            await attempt("ロールの並び順", guild.edit_role_positions(positions=positions, reason=REASON))

        # 3. チャンネル
        def build_overwrites(data):
            result = {}
            for ow in data.get("overwrites", []):
                if ow["type"] == "role":
                    target = role_map.get(ow["id"]) or guild.get_role(ow["id"])
                    if target is None:
                        continue
                else:
                    target = guild.get_member(ow["id"]) or discord.Object(id=ow["id"])
                result[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
                )
            return result

        def overwrite_key(overwrites):
            items = set()
            for target, ow in overwrites.items():
                allow, deny = ow.pair()
                items.add((target.id, allow.value, deny.value))
            return items

        def current_overwrite_key(channel):
            return {(ow.id, ow.allow, ow.deny) for ow in getattr(channel, "_overwrites", [])}

        channel_map = {}
        categories = [c for c in snapshot["channels"] if c["type"] == "category"]
        others = [c for c in snapshot["channels"] if c["type"] != "category"]

        for data in sorted(categories, key=lambda c: c["position"]):
            overwrites = build_overwrites(data)
            channel = guild.get_channel(data["id"])
            if channel is None:
                created = await attempt(
                    f"カテゴリ作成 {data['name']}",
                    guild.create_category(data["name"], overwrites=overwrites, position=data["position"], reason=REASON),
                )
                if created is not failed:
                    channel_map[data["id"]] = created
                    counts["recreated_channels"] += 1
                continue
            channel_map[data["id"]] = channel
            changes = {}
            if channel.name != data["name"]:
                changes["name"] = data["name"]
            if current_overwrite_key(channel) != overwrite_key(overwrites):
                changes["overwrites"] = overwrites
            if changes:
                if await attempt(f"カテゴリ修正 {data['name']}", channel.edit(reason=REASON, **changes)) is not failed:
                    counts["fixed_channels"] += 1

        for data in sorted(others, key=lambda c: c["position"]):
            overwrites = build_overwrites(data)
            category = None
            if data.get("category_id"):
                category = channel_map.get(data["category_id"]) or guild.get_channel(data["category_id"])
            channel = guild.get_channel(data["id"])

            if channel is None:
                created = await attempt(
                    f"チャンネル作成 #{data['name']}",
                    self._create_channel(guild, data, category, overwrites),
                )
                if created is not failed:
                    channel_map[data["id"]] = created
                    counts["recreated_channels"] += 1
                continue

            changes = {}
            if channel.name != data["name"]:
                changes["name"] = data["name"]
            if channel.category_id != (category.id if category else None):
                changes["category"] = category
            if current_overwrite_key(channel) != overwrite_key(overwrites):
                changes["overwrites"] = overwrites
            if "topic" in data and getattr(channel, "topic", None) != data["topic"]:
                changes["topic"] = data["topic"]
            if "nsfw" in data and getattr(channel, "nsfw", None) != data["nsfw"]:
                changes["nsfw"] = data["nsfw"]
            if "slowmode_delay" in data and getattr(channel, "slowmode_delay", None) != data["slowmode_delay"]:
                changes["slowmode_delay"] = data["slowmode_delay"]
            if changes:
                if await attempt(f"チャンネル修正 #{data['name']}", channel.edit(reason=REASON, **changes)) is not failed:
                    counts["fixed_channels"] += 1

        # 4. 攻撃でBANされた人のBAN解除
        for user_id in targets(A.ban):
            if user_id in attackers:
                continue
            if await attempt(f"BAN解除 {user_id}", guild.unban(discord.Object(id=user_id), reason=REASON)) is not failed:
                counts["unbanned"] += 1

        # 5. サーバー名
        if A.guild_update in attacker_actions() and guild.name != snapshot.get("guild_name"):
            if await attempt("サーバー名の復元", guild.edit(name=snapshot["guild_name"], reason=REASON)) is not failed:
                counts["guild_name"] += 1

        # 6. 作り直したロールをメンバーに付け直す
        for data, new_role in recreated_roles:
            for member_id in data.get("members", []):
                member = guild.get_member(member_id)
                if member is not None and new_role not in member.roles:
                    if await attempt(f"ロール付与 {new_role.name}", member.add_roles(new_role, reason=REASON)) is not failed:
                        counts["role_members"] += 1

        labels = [
            ("deleted_channels", "攻撃で作られたチャンネルを削除"),
            ("deleted_roles", "攻撃で作られたロールを削除"),
            ("deleted_webhooks", "攻撃で作られたWebhookを削除"),
            ("recreated_roles", "ロールを作り直し"),
            ("fixed_roles", "ロールの設定を元に戻す"),
            ("recreated_channels", "チャンネル・カテゴリを作り直し"),
            ("fixed_channels", "チャンネルの設定を元に戻す"),
            ("unbanned", "BAN解除"),
            ("guild_name", "サーバー名を元に戻す"),
            ("role_members", "メンバーへのロール付け直し"),
        ]
        lines = [f"・{label}: {counts[key]} 件" for key, label in labels if counts[key]]
        if not lines:
            lines.append("・元に戻す必要のある変更はありませんでした")
        lines.append("※ 削除されたチャンネル内のメッセージは元に戻せません。")
        return lines, errors

    async def _create_channel(self, guild, data, category, overwrites):
        kind = data["type"]
        common = {
            "category": category,
            "overwrites": overwrites,
            "position": data["position"],
            "reason": REASON,
        }
        text_opts = {}
        if data.get("topic"):
            text_opts["topic"] = data["topic"]
        if "nsfw" in data:
            text_opts["nsfw"] = data["nsfw"]
        if data.get("slowmode_delay"):
            text_opts["slowmode_delay"] = data["slowmode_delay"]

        if kind in ("voice", "stage_voice"):
            voice_opts = {}
            if data.get("bitrate"):
                voice_opts["bitrate"] = int(min(data["bitrate"], guild.bitrate_limit))
            if kind == "stage_voice":
                try:
                    return await guild.create_stage_channel(data["name"], **common)
                except discord.HTTPException:
                    pass  # コミュニティ未設定などで作れない場合はボイスで作る
            if data.get("user_limit"):
                voice_opts["user_limit"] = data["user_limit"]
            return await guild.create_voice_channel(data["name"], **common, **voice_opts)

        if kind in ("forum", "media"):
            try:
                return await guild.create_forum(data["name"], media=(kind == "media"), **common, **text_opts)
            except discord.HTTPException:
                pass  # 作れない場合はテキストで作る

        if kind == "news":
            try:
                return await guild.create_text_channel(data["name"], news=True, **common, **text_opts)
            except discord.HTTPException:
                pass

        return await guild.create_text_channel(data["name"], **common, **text_opts)


# ==========================================
# Botへの登録
# ==========================================

REQUIRED_PERMISSIONS = {
    "view_audit_log": "監査ログを表示",
    "manage_channels": "チャンネルの管理",
    "manage_roles": "ロールの管理",
    "ban_members": "メンバーをBAN",
    "manage_webhooks": "ウェブフックの管理",
    "manage_guild": "サーバー管理",
}


def _permission_warnings(guild):
    me = guild.me
    perms = me.guild_permissions
    lines = []
    missing = [label for name, label in REQUIRED_PERMISSIONS.items() if not getattr(perms, name)]
    if missing:
        lines.append("⚠️ このBotに次の権限がありません: " + "、".join(missing) + "（管理者権限を推奨）")
    higher = [r for r in guild.roles if r > me.top_role]
    if higher:
        lines.append(
            "⚠️ このBotのロールより上にロールがあります。"
            "サーバー設定 → ロールで、このBotのロールを一番上に移動してください"
            "（下にあるBotしかBAN・権限剥奪できません）。"
        )
    return lines


def setup_anti_nuke(bot, *, backup_user_id, exclude_channel=None, whitelist_bot_ids=()):
    guard = AntiNuke(bot, backup_user_id, exclude_channel, whitelist_bot_ids)

    async def on_ready():
        await guard.load_backups()

    async def on_audit_log_entry_create(entry):
        try:
            await guard.on_audit_log_entry_create(entry)
        except Exception:
            logger.exception("Nuke対策の検知処理でエラー")

    async def on_channel_change(channel, *_):
        guard.schedule_save(channel.guild)

    async def on_channel_update(before, after):
        guard.schedule_save(after.guild)

    async def on_role_change(role):
        guard.schedule_save(role.guild)

    async def on_role_update(before, after):
        guard.schedule_save(after.guild)

    async def on_member_update(before, after):
        if before.roles != after.roles:
            guard.schedule_save(after.guild)

    async def on_guild_update(before, after):
        if before.name != after.name:
            guard.schedule_save(after)

    bot.add_listener(on_ready, "on_ready")
    bot.add_listener(on_audit_log_entry_create, "on_audit_log_entry_create")
    bot.add_listener(on_channel_change, "on_guild_channel_create")
    bot.add_listener(on_channel_change, "on_guild_channel_delete")
    bot.add_listener(on_channel_update, "on_guild_channel_update")
    bot.add_listener(on_role_change, "on_guild_role_create")
    bot.add_listener(on_role_change, "on_guild_role_delete")
    bot.add_listener(on_role_update, "on_guild_role_update")
    bot.add_listener(on_member_update, "on_member_update")
    bot.add_listener(on_guild_update, "on_guild_update")

    @app_commands.command(name="nuke対策", description="サーバーを自動バックアップし、荒らしBotの大規模な変更から自動で復元します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(action="操作")
    @app_commands.describe(action="実行する操作（省略すると有効化）")
    @app_commands.choices(action=[
        app_commands.Choice(name="有効化（今の状態を保存して監視開始）", value="enable"),
        app_commands.Choice(name="今すぐ保存", value="save"),
        app_commands.Choice(name="状態を確認", value="status"),
        app_commands.Choice(name="無効化", value="disable"),
    ])
    async def nuke_command(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None):
        guild = interaction.guild
        user = interaction.user
        if guild is None:
            return
        if not (user.id == guild.owner_id or user.guild_permissions.administrator):
            await interaction.response.send_message("このコマンドは管理者のみ使用できます。", ephemeral=True)
            return

        value = action.value if action else "enable"
        await interaction.response.defer(ephemeral=True, thinking=True)
        warnings = _permission_warnings(guild)

        if value in ("enable", "save"):
            ok = await guard.save_now(guild, force=True, enabled=True)
            snap = guard.snapshots[guild.id]
            lines = [
                "🛡️ **Nuke対策を有効にしました**" if value == "enable" else "💾 **現在の状態を保存しました**",
                f"ロール {len(snap['roles'])} 件 / チャンネル {len(snap['channels'])} 件を保存",
                (f"バックアップを <@{guard.backup_user_id}> のDMに送りました。"
                 if ok else "⚠️ バックアップをDMに送れませんでした（DMが閉じている可能性があります）。"
                            "Botを再起動するとバックアップが消えます。"),
                "",
                "・ロールやチャンネルが作成・変更・削除されると、自動で保存し直します。",
                f"・Botが {DETECT_WINDOW_SECONDS}秒間に{DETECT_THRESHOLD}回以上の大規模な変更をすると、"
                "そのBotの権限を剥奪してBANし、保存した状態に戻します。",
            ]
        elif value == "disable":
            if guild.id not in guard.snapshots:
                lines = ["Nuke対策はまだ有効になっていません。"]
            else:
                guard._cancel_save(guild.id)
                guard.snapshots[guild.id]["enabled"] = False
                await guard.upload_backup(guild, guard.snapshots[guild.id])
                lines = ["Nuke対策を無効にしました。（保存データは残っています）"]
        else:
            snap = guard.snapshots.get(guild.id)
            if not snap:
                lines = ["Nuke対策: **無効**（まだ保存されていません）"]
            else:
                saved_at = int(datetime.fromisoformat(snap["saved_at"]).timestamp())
                lines = [
                    f"Nuke対策: **{'有効' if snap.get('enabled') else '無効'}**",
                    f"最終保存: <t:{saved_at}:f>",
                    f"ロール {len(snap['roles'])} 件 / チャンネル {len(snap['channels'])} 件",
                ]
                if guild.id in guard.autosave_paused:
                    lines.append("⚠️ 前回の復元で失敗があったため、自動保存を停止中です（「今すぐ保存」で再開）。")

        if warnings:
            lines.append("")
            lines.extend(warnings)
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    bot.tree.add_command(nuke_command)
    return guard
