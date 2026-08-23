import discord
from discord.ext import commands
import aiosqlite
import config
from database import DB_PATH

class ConfessionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not config.CHANNEL_CONFESSIONS or message.channel.id != config.CHANNEL_CONFESSIONS:
            return

        # 1. Save confession to DB to generate incremental ID
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO confessions (guild_id, user_id, content) VALUES (?, ?, ?)",
                (message.guild.id, message.author.id, message.content or "")
            )
            confession_id = cursor.lastrowid
            await db.commit()

        # 2. Extract attachments
        files = []
        for attachment in message.attachments:
            try:
                files.append(await attachment.to_file())
            except Exception:
                pass

        # 3. Create anonymous confession embed
        confession_embed = discord.Embed(
            title=f"🤫 Anonymous Confession #{confession_id}",
            description=message.content if message.content else "*Attachment(s) only*",
            color=config.PRIMARY_COLOR
        )
        confession_embed.set_footer(text="To post a confession, simply send a message in this channel!")

        # 4. Delete the original message from poster
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        # 5. Post anonymous confession to channel
        posted_msg = await message.channel.send(embed=confession_embed, files=files)

        # 6. Audit Logging for Staff Moderation
        if config.CHANNEL_CONFESSION_LOGS:
            log_ch = message.guild.get_channel(config.CHANNEL_CONFESSION_LOGS)
            if log_ch:
                log_embed = discord.Embed(
                    title=f"🛡️ Confession #{confession_id} Logged",
                    description=f"**Author:** {message.author.mention} (`{message.author.id}`)\n**Channel:** {message.channel.mention}\n**Jump:** [View Confession]({posted_msg.jump_url})",
                    color=discord.Color.dark_gray()
                )
                if message.content:
                    log_embed.add_field(name="Content", value=message.content, inline=False)
                if message.attachments:
                    att_urls = "\n".join([a.url for a in message.attachments])
                    log_embed.add_field(name="Attachments", value=att_urls, inline=False)

                await log_ch.send(embed=log_embed)

async def setup(bot):
    await bot.add_cog(ConfessionsCog(bot))
