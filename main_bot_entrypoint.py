import os
import asyncio
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
import config
from database import init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("LooksMatch")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Logged in successfully as {bot.user} (ID: {bot.user.id})")

    await bot.change_presence(activity=discord.CustomActivity(name="💕 Find your match."))

    logger.info("Synchronizing application slash command tree...")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Successfully synced {len(synced)} slash commands globally.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

async def main():
    logger.info("Initializing SQLite database schema...")
    await init_db()

    cogs = [
        "cogs.setup",
        "cogs.admin",
        "cogs.dating",
        "cogs.ratings",
        "cogs.levels"
    ]

    for cog in cogs:
        try:
            await bot.load_extension(cog)
            logger.info(f"Extension loaded: {cog}")
        except Exception as e:
            logger.error(f"Failed to load extension {cog}: {e}")

    if not config.DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN is not configured in environment! Aborting startup.")
        return

    logger.info("Starting Discord bot connection...")
    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())