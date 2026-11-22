"""StudyRoomBOT: text-based study sessions plus registered voice-room roles."""

import asyncio
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEXT_CHANNEL_NAME = os.getenv("STUDY_TEXT_CHANNEL_NAME", "勉強時間記録")
STUDY_ROLE_NAME = os.getenv("STUDY_ROLE_NAME", "勉強中")
JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc
DB_PATH = Path("studyroom.db")


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_db(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def from_db(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def format_seconds(seconds: float) -> str:
    total_minutes = max(0, round(seconds / 60))
    return f"{total_minutes // 60}時間{total_minutes % 60:02d}分"


RANKS = [
    (30, "プレデター"),
    (25, "マスター"),
    (20, "ダイヤ"),
    (15, "プラチナ"),
    (10, "ゴールド"),
    (7, "シルバー"),
    (3, "ブロンズ"),
    (0, "ルーキー"),
]


def rank_for(seconds: float) -> str:
    hours = seconds / 3600
    return next(name for threshold, name in RANKS if hours >= threshold)


class StudyDatabase:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                text_channel_id INTEGER,
                last_processed_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS study_voice_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(guild_id, user_id, started_at);
        """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(guild_settings)")}
        if "last_processed_message_id" not in columns:
            self.conn.execute("ALTER TABLE guild_settings ADD COLUMN last_processed_message_id INTEGER")
        self.conn.commit()

    def set_text_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            "INSERT INTO guild_settings(guild_id, text_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET text_channel_id=excluded.text_channel_id",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def text_channel_id(self, guild_id: int) -> int | None:
        row = self.conn.execute("SELECT text_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
        return row["text_channel_id"] if row else None

    def last_processed_message_id(self, guild_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT last_processed_message_id FROM guild_settings WHERE guild_id=?", (guild_id,)
        ).fetchone()
        return row["last_processed_message_id"] if row else None

    def set_last_processed_message(self, guild_id: int, message_id: int) -> None:
        self.conn.execute(
            "UPDATE guild_settings SET last_processed_message_id=? WHERE guild_id=?",
            (message_id, guild_id),
        )
        self.conn.commit()

    def add_voice_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO study_voice_channels VALUES (?, ?)", (guild_id, channel_id))
        self.conn.commit()

    def remove_voice_channel(self, guild_id: int, channel_id: int) -> bool:
        result = self.conn.execute("DELETE FROM study_voice_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        self.conn.commit()
        return result.rowcount > 0

    def voice_channel_ids(self, guild_id: int) -> set[int]:
        return {row["channel_id"] for row in self.conn.execute("SELECT channel_id FROM study_voice_channels WHERE guild_id=?", (guild_id,))}

    def active_session(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()

    def start_session(self, guild_id: int, user_id: int, started_at: datetime) -> bool:
        if self.active_session(guild_id, user_id):
            return False
        self.conn.execute("INSERT INTO sessions(guild_id, user_id, started_at) VALUES (?, ?, ?)", (guild_id, user_id, to_db(started_at)))
        self.conn.commit()
        return True

    def stop_session(self, guild_id: int, user_id: int, ended_at: datetime) -> float | None:
        session = self.active_session(guild_id, user_id)
        if not session:
            return None
        started = from_db(session["started_at"])
        self.conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (to_db(ended_at), session["id"]))
        self.conn.commit()
        return (ended_at - started).total_seconds()

    def total_between(self, guild_id: int, user_id: int, start: datetime, end: datetime) -> float:
        rows = self.conn.execute(
            "SELECT started_at, ended_at FROM sessions WHERE guild_id=? AND user_id=? AND started_at < ?",
            (guild_id, user_id, to_db(end)),
        )
        total = 0.0
        for row in rows:
            session_start = from_db(row["started_at"])
            session_end = from_db(row["ended_at"]) if row["ended_at"] else now_utc()
            overlap_start, overlap_end = max(start, session_start), min(end, session_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        return total

    def lifetime_total(self, guild_id: int, user_id: int) -> float:
        return self.total_between(guild_id, user_id, datetime(1970, 1, 1, tzinfo=UTC), now_utc())

    def active_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
db = StudyDatabase()


async def require_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("このコマンドはギルド内で使ってください。", ephemeral=True)
        return False
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("「サーバー管理」権限を持つ管理者だけが使えます。", ephemeral=True)
        return False
    return True


async def ensure_study_role(guild: discord.Guild) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=STUDY_ROLE_NAME)
    if role:
        return role
    try:
        return await guild.create_role(name=STUDY_ROLE_NAME, reason="StudyRoomBOT: 自習中ロール")
    except discord.Forbidden:
        return None


async def update_member_study_role(member: discord.Member) -> None:
    channel_ids = db.voice_channel_ids(member.guild.id)
    in_study_room = member.voice is not None and member.voice.channel is not None and member.voice.channel.id in channel_ids
    role = discord.utils.get(member.guild.roles, name=STUDY_ROLE_NAME)
    if in_study_room and not role:
        role = await ensure_study_role(member.guild)
    if not role:
        return
    try:
        if in_study_room and role not in member.roles:
            await member.add_roles(role, reason="登録済み自習室VCに参加")
        elif not in_study_room and role in member.roles:
            await member.remove_roles(role, reason="登録済み自習室VCから退出")
    except discord.Forbidden:
        pass


@bot.event
async def on_ready() -> None:
    db.initialize()
    await import_missed_study_messages()
    if not status_updater.is_running():
        status_updater.start()
    print(f"{bot.user} としてログインしました")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild or not isinstance(message.channel, discord.TextChannel):
        return
    configured_id = db.text_channel_id(message.guild.id)
    # 最初の利用時は、指定名のチャンネルを自動的に記録対象にする。
    if configured_id is None and message.channel.name == TEXT_CHANNEL_NAME:
        db.set_text_channel(message.guild.id, message.channel.id)
        configured_id = message.channel.id
    if message.channel.id != configured_id:
        return

    await record_study_message(message, announce=True)
    db.set_last_processed_message(message.guild.id, message.id)


async def record_study_message(message: discord.Message, *, announce: bool) -> None:
    """Record one start/end message using its original Discord timestamp."""
    text = message.content.strip()
    recorded_at = message.created_at.astimezone(UTC)
    if text == "開始":
        if db.start_session(message.guild.id, message.author.id, recorded_at):
            if announce:
                await message.channel.send(f"{message.author.mention}さんが勉強を開始しました。")
        elif announce:
            await message.channel.send(f"{message.author.mention}さんは、すでに勉強を開始しています。")
    elif text == "終了":
        elapsed = db.stop_session(message.guild.id, message.author.id, recorded_at)
        if elapsed is None:
            if announce:
                await message.channel.send(f"{message.author.mention}さんは、開始中の勉強がありません。")
        elif announce:
            await message.channel.send(f"{message.author.mention}さんが勉強を終了しました。今回: {format_seconds(elapsed)}")


async def import_missed_study_messages() -> None:
    """Backfill start/end messages that arrived while the BOT was offline.

    Discord retains text messages, so their timestamps can safely be used after
    a restart. The latest handled message ID is stored to prevent duplicates.
    """
    for guild in bot.guilds:
        channel_id = db.text_channel_id(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name=TEXT_CHANNEL_NAME)
            if not channel:
                continue
            db.set_text_channel(guild.id, channel.id)

        last_id = db.last_processed_message_id(guild.id)
        after = discord.Object(id=last_id) if last_id else None
        try:
            async for message in channel.history(limit=None, oldest_first=True, after=after):
                if not message.author.bot:
                    await record_study_message(message, announce=False)
                db.set_last_processed_message(guild.id, message.id)
        except discord.Forbidden:
            print(f"{guild.name}: メッセージ履歴を読む権限がありません")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if before.channel != after.channel:
        await update_member_study_role(member)


@tasks.loop(seconds=60)
async def status_updater() -> None:
    activity = discord.CustomActivity(name=f"{len(bot.guilds)}ギルド / {db.active_count()}人が勉強中")
    await bot.change_presence(status=discord.Status.online, activity=activity)


def jst_midnight(day: datetime) -> datetime:
    return datetime.combine(day.date(), time.min, tzinfo=JST).astimezone(UTC)


@bot.tree.command(name="about", description="BOTの概要を表示します")
async def about(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("StudyRoomBOTは「勉強時間記録」チャンネルの「開始」「終了」で勉強時間を記録します。")


@bot.tree.command(name="help", description="BOTの使い方を表示します")
async def help_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "**使い方**\n"
        f"#{TEXT_CHANNEL_NAME} で「開始」→ 勉強開始、「終了」→ 勉強終了\n"
        "`/studydate` 日別記録、`/studyweek` 週別記録\n"
        "管理者: `/admin`、VC参加中ロール: `/studyroomadd` / `/studyroomdel`"
    )


@bot.tree.command(name="studydate", description="過去7日間の日別データ・累計・ランクを表示します")
async def studydate(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("このコマンドはギルド内で使ってください。", ephemeral=True)
        return
    today_jst = datetime.now(JST)
    first_day = today_jst - timedelta(days=6)
    lines = []
    for offset in range(7):
        day = first_day + timedelta(days=offset)
        start = jst_midnight(day)
        lines.append(f"{day:%m/%d}: {format_seconds(db.total_between(interaction.guild.id, interaction.user.id, start, start + timedelta(days=1)))}")
    seven_days = db.total_between(interaction.guild.id, interaction.user.id, jst_midnight(first_day), jst_midnight(today_jst) + timedelta(days=1))
    lifetime = db.lifetime_total(interaction.guild.id, interaction.user.id)
    embed = discord.Embed(title=f"{interaction.user.display_name}さんの過去7日間", color=discord.Color.blue())
    embed.add_field(name="日別", value="\n".join(lines), inline=False)
    embed.add_field(name="過去7日", value=format_seconds(seven_days))
    embed.add_field(name="累計", value=format_seconds(lifetime))
    embed.add_field(name="ランク", value=rank_for(seven_days))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="studyweek", description="過去5週間の週別データ・累計・ランクを表示します")
async def studyweek(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("このコマンドはギルド内で使ってください。", ephemeral=True)
        return
    today = datetime.now(JST)
    current_monday = today - timedelta(days=today.weekday())
    lines = []
    for index in range(4, -1, -1):
        week_start_jst = current_monday - timedelta(weeks=index)
        start = jst_midnight(week_start_jst)
        end = start + timedelta(days=7)
        seconds = db.total_between(interaction.guild.id, interaction.user.id, start, end)
        lines.append(f"{week_start_jst:%m/%d}週: {format_seconds(seconds)}")
    first = current_monday - timedelta(weeks=4)
    recent_start = jst_midnight(today - timedelta(days=6))
    recent_end = jst_midnight(today) + timedelta(days=1)
    seven_days = db.total_between(interaction.guild.id, interaction.user.id, recent_start, recent_end)
    embed = discord.Embed(title=f"{interaction.user.display_name}さんの過去5週間", color=discord.Color.purple())
    embed.add_field(name="週別", value="\n".join(lines), inline=False)
    embed.add_field(name="累計", value=format_seconds(db.lifetime_total(interaction.guild.id, interaction.user.id)))
    embed.add_field(name="現在のランク（過去7日）", value=rank_for(seven_days))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="admin", description="管理者向けガイドを表示します")
async def admin(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.send_message(
        f"**管理者ガイド**\n1. `#{TEXT_CHANNEL_NAME}` を作成します（そこで「開始」「終了」を受け付けます）。\n"
        "2. 自習室にしたいVCへ入室して `/studyroomadd` を実行します。\n"
        "3. 解除したいVCへ入室して `/studyroomdel` を実行します。\n"
        f"BOTが `{STUDY_ROLE_NAME}` ロールを作成・付与できるよう、BOTに「ロールの管理」権限を与えてください。",
        ephemeral=True,
    )


@bot.tree.command(name="studyroomadd", description="参加中のVCを自習室として登録します")
async def studyroomadd(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    member = interaction.user
    if not member.voice or not member.voice.channel:
        await interaction.response.send_message("登録したいVCへ参加してから実行してください。", ephemeral=True)
        return
    db.add_voice_channel(interaction.guild.id, member.voice.channel.id)
    text_channel = discord.utils.get(interaction.guild.text_channels, name=TEXT_CHANNEL_NAME)
    if text_channel:
        db.set_text_channel(interaction.guild.id, text_channel.id)
    await update_member_study_role(member)
    await interaction.response.send_message(f"「{member.voice.channel.name}」を自習室VCとして登録しました。", ephemeral=True)


@bot.tree.command(name="studyroomdel", description="参加中のVCの自習室登録を解除します")
async def studyroomdel(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    member = interaction.user
    if not member.voice or not member.voice.channel:
        await interaction.response.send_message("解除したいVCへ参加してから実行してください。", ephemeral=True)
        return
    if db.remove_voice_channel(interaction.guild.id, member.voice.channel.id):
        await update_member_study_role(member)
        await interaction.response.send_message(f"「{member.voice.channel.name}」の自習室登録を解除しました。", ephemeral=True)
    else:
        await interaction.response.send_message("このVCは自習室として登録されていません。", ephemeral=True)


@bot.event
async def setup_hook() -> None:
    await bot.tree.sync()


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です。.env を作成して設定してください。")

bot.run(TOKEN)
