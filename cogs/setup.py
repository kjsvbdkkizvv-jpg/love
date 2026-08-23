# cogs/setup.py
"""
Setup cog removed.
This file intentionally contains a no-op setup() so that the extension can be loaded safely
but does not register any commands or listeners. The original setup cog was removed by
an automated fix upon request.
"""
from discord.ext import commands

class DisabledSetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    # Intentionally do not add the original SetupCog — this disables the setup functionality.
    # Keeping a minimal setup function prevents load_extension failures while effectively
    # removing the setup cog behavior.
    await bot.add_cog(DisabledSetupCog(bot))
