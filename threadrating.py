import datetime
import logging

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


class RatingButton(discord.ui.Button):
    def __init__(self, score: int):
        super().__init__(label=str(score), style=discord.ButtonStyle.primary)
        self.score = score

    async def callback(self, interaction: discord.Interaction):
        await self.view.submit(interaction, self.score)


class RatingView(discord.ui.View):
    def __init__(self, cog, thread, closer):
        super().__init__(timeout=60 * 60 * 24)
        self.cog = cog
        self.thread = thread
        self.closer = closer
        self.message = None
        for score in range(1, 6):
            self.add_item(RatingButton(score))

    async def submit(self, interaction: discord.Interaction, score: int):
        await interaction.response.edit_message(
            content=f"Thanks for your feedback! You rated this conversation {score}/5.",
            view=None,
        )
        await self.cog.record_rating(self.thread, self.closer, score)
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="This survey has expired.", view=None)
            except discord.HTTPException:
                pass


logger = logging.getLogger("Modmail")

RETENTION_DAYS = 90


class ThreadRating(commands.Cog):
    """Asks users to rate their support experience when a thread closes."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)

    async def cog_load(self):
        # TTL index: MongoDB automatically deletes ratings older than RETENTION_DAYS.
        # The config document has no "timestamp" field, so it is never deleted.
        try:
            await self.coll.create_index(
                "timestamp", expireAfterSeconds=RETENTION_DAYS * 24 * 60 * 60
            )
        except Exception:
            logger.exception("ThreadRating: could not create retention index")

    async def is_enabled(self) -> bool:
        config = await self.coll.find_one({"_id": "config"})
        return config.get("enabled", True) if config else True

    async def record_rating(self, thread, closer, score: int):
        await self.coll.insert_one(
            {
                "type": "rating",
                "user_id": thread.recipient.id,
                "closer_id": getattr(closer, "id", None),
                "channel_id": getattr(thread, "channel", None) and thread.channel.id,
                "rating": score,
                "timestamp": datetime.datetime.utcnow(),
            }
        )

        log_channel = self.bot.log_channel
        if log_channel is None:
            return
        embed = discord.Embed(
            title="Thread rated",
            description=f"{thread.recipient.mention} rated their conversation **{score}/5**.",
            color=self.bot.main_color,
            timestamp=discord.utils.utcnow(),
        )
        if closer is not None:
            embed.add_field(name="Closed by", value=closer.mention)
        await log_channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        # Silent closes mean the user shouldn't be contacted.
        if silent or not await self.is_enabled():
            return

        view = RatingView(self, thread, closer)
        embed = discord.Embed(
            title="How did we do?",
            description="Your conversation has been closed. Please rate your experience from 1 (poor) to 5 (excellent).",
            color=self.bot.main_color,
        )
        try:
            view.message = await thread.recipient.send(embed=embed, view=view)
        except discord.HTTPException:
            pass  # User has DMs closed or blocked the bot.

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ratings(self, ctx):
        """Show rating statistics."""
        overall = await self.coll.aggregate(
            [
                {"$match": {"type": "rating"}},
                {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
            ]
        ).to_list(length=1)

        if not overall:
            return await ctx.send("No ratings yet.")

        dist = await self.coll.aggregate(
            [{"$match": {"type": "rating"}}, {"$group": {"_id": "$rating", "n": {"$sum": 1}}}]
        ).to_list(length=5)
        counts = {d["_id"]: d["n"] for d in dist}

        embed = discord.Embed(title="Thread ratings", color=self.bot.main_color)
        embed.add_field(name="Average", value=f"{overall[0]['avg']:.2f} / 5")
        embed.add_field(name="Responses", value=str(overall[0]["n"]))
        embed.add_field(
            name="Breakdown",
            value="\n".join(f"{s}: {counts.get(s, 0)}" for s in range(5, 0, -1)),
            inline=False,
        )
        await ctx.send(embed=embed)

    @ratings.command(name="toggle")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ratings_toggle(self, ctx):
        """Turn the post-close survey on or off."""
        enabled = not await self.is_enabled()
        await self.coll.find_one_and_update(
            {"_id": "config"}, {"$set": {"enabled": enabled}}, upsert=True
        )
        await ctx.send(f"Rating surveys are now **{'enabled' if enabled else 'disabled'}**.")


async def setup(bot):
    await bot.add_cog(ThreadRating(bot))
