import os
import asyncio
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web
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

async def handle_health_check(request):
    """Health check endpoint for Fly.io proxy."""
    return web.Response(text="LooksMatch Bot is healthy and running!", status=200)

async def start_health_check_server():
    """Starts an asynchronous HTTP server on 0.0.0.0:8080."""
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health check HTTP server listening on 0.0.0.0:{port}")

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

    logger.info("Starting lightweight HTTP health check server for Fly.io...")
    await start_health_check_server()

    cogs = [
        "cogs.setup",
        "cogs.admin",
        "cogs.dating",
        "cogs.rating",
        "cogs.levels",
        "cogs.confessions"
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
