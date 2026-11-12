import discord
from discord.ext import commands
import os
import asyncio
import db
import pokedata
import font_downloader   # ← NEW

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True   # ← REQUIRED for on_member_join / on_member_remove

# enable_debug_events=True is REQUIRED for on_socket_raw_receive to fire (converter cog)
bot = commands.Bot(
    command_prefix=["s!", "S!", "!"],
    help_command=None,
    intents=intents,
    enable_debug_events=True,
    case_insensitive=True,
)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Servers: {len(bot.guilds)}")


async def main():
    pokedata.load()
    await font_downloader.ensure_fonts()   # ← download Poppins fonts on startup
    await db.ensure_indexes()              # create query indexes (idempotent)
    async with bot:
        await bot.load_extension("cogs.help_cog")
        await bot.load_extension("cogs.event_helper_cog")
        await bot.load_extension("cogs.emoji_extractor")
        await bot.load_extension("cogs.repeat_cog")
        await bot.load_extension("cogs.admin_cog")
        await bot.load_extension("cogs.gym_cog")
        await bot.load_extension("cogs.loans_cog")
        await bot.load_extension("cogs.converter_cog")
        await bot.load_extension("cogs.tracker_cog")
        await bot.load_extension("cogs.leaderboard_cog")
        await bot.load_extension("cogs.calculator_cog")
        await bot.load_extension("cogs.category_cog")
        await bot.load_extension("cogs.moderation_cog")
        await bot.load_extension("cogs.battle_cog")
        await bot.load_extension("cogs.welcome_cog")   # ← NEW
        await bot.start(TOKEN)


asyncio.run(main())
