import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger("Modmail")

CHECK_AFTER_HOURS = 36    # how long staff have to react
STRIKE_DECAY_DAYS = 7     # one strike is removed per this many days without a new strike
MAX_STRIKES = 3           # a strike while already holding this many = disciplinary action
TRACKED_KEEP_DAYS = 30    # finished message records are cleaned up after this long
UTC = datetime.timezone.utc


def utcnow():
    """Naive UTC datetime (matches what MongoDB hands back)."""
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def ts(dt, style="f"):
    return f"<t:{int(dt.replace(tzinfo=UTC).timestamp())}:{style}>"


class ReactCheck(commands.Cog):
    """Strikes staff who don't react to tracked messages in time (unless they're on LOA)."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self.watch_ids = set()
        self.warned_loa = False
        self.menus = [
            app_commands.ContextMenu(name="Require reactions", callback=self.require_reactions),
            app_commands.ContextMenu(name="Cancel reaction check", callback=self.cancel_reaction_check),
        ]

    async def cog_load(self):
        try:
            # Old message records are cleaned up automatically. Strikes and disciplinary records are never
            # touched by this index; strikes are removed by decay_loop, discipline records are kept forever.
            await self.coll.create_index("check_at", expireAfterSeconds=TRACKED_KEEP_DAYS * 24 * 60 * 60)
        except Exception:
            logger.exception("ReactCheck: could not create cleanup index")
        self.watch_ids = set((await self.get_config()).get("watch_channel_ids", []))
        for menu in self.menus:
            self.bot.tree.add_command(menu)
        self.check_loop.start()
        self.decay_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()
        self.decay_loop.cancel()
        for menu in self.menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    async def cog_app_command_error(self, interaction, error):
        logger.error("ReactCheck command failed", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ----- config + LOA plugin access -----

    async def get_config(self):
        return await self.coll.find_one({"_id": "config"}) or {}

    async def set_config(self, key, value):
        await self.coll.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)

    async def loa_config(self):
        """The LOA plugin's settings (staff role, manager role)."""
        loa = self.bot.get_cog("LOA")
        return (await loa.get_config()) if loa else {}

    @staticmethod
    def has_role(member, role_id):
        return bool(role_id) and any(r.id == role_id for r in member.roles)

    def is_manager(self, member, lcfg):
        return member.guild_permissions.administrator or self.has_role(member, lcfg.get("manager_role_id"))

    def is_staff(self, member, lcfg):
        return self.is_manager(member, lcfg) or self.has_role(member, lcfg.get("staff_role_id"))

    async def dm(self, member, embed):
        try:
            await member.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    async def log(self, embed, ping_managers=False):
        cfg = await self.get_config()
        channel = self.bot.get_channel(cfg.get("log_channel_id", 0))
        if channel is None:
            return
        content = None
        if ping_managers:
            role_id = (await self.loa_config()).get("manager_role_id")
            content = f"<@&{role_id}>" if role_id else None
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            pass

    # ----- choosing which messages need a reaction -----
    #
    # A message is tracked when EITHER
    #   1. it's posted in a tracked channel and pings the staff role, OR
    #   2. a manager right-clicks it -> Apps -> "Require reactions".
    # Everything else is ignored, so ordinary chat never earns anyone a strike.

    async def track(self, message, start):
        await self.coll.insert_one(
            {
                "type": "tracked",
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "author_id": message.author.id,
                "tracked_at": start,
                "check_at": start + datetime.timedelta(hours=CHECK_AFTER_HOURS),
                "checked": False,
            }
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.channel.id not in self.watch_ids:
            return
        if message.author.id == self.bot.user.id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        staff_role_id = (await self.loa_config()).get("staff_role_id")
        if not staff_role_id or staff_role_id not in message.raw_role_mentions:
            return
        await self.track(message, message.created_at.replace(tzinfo=None))

    async def require_reactions(self, interaction: discord.Interaction, message: discord.Message):
        if interaction.guild is None or not self.is_manager(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("This is for management only.", ephemeral=True)
        existing = await self.coll.find_one({"type": "tracked", "message_id": message.id})
        if existing:
            text = (
                "That message was already checked."
                if existing["checked"]
                else f"That message is already tracked. The check runs {ts(existing['check_at'], 'R')}."
            )
            return await interaction.response.send_message(text, ephemeral=True)
        start = utcnow()
        await self.track(message, start)
        due = start + datetime.timedelta(hours=CHECK_AFTER_HOURS)
        await interaction.response.send_message(
            f"✅ Staff have {CHECK_AFTER_HOURS} hours to react to that message. The check runs {ts(due, 'R')}.",
            ephemeral=True,
        )

    async def cancel_reaction_check(self, interaction: discord.Interaction, message: discord.Message):
        if interaction.guild is None or not self.is_manager(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("This is for management only.", ephemeral=True)
        result = await self.coll.delete_one({"type": "tracked", "message_id": message.id, "checked": False})
        text = (
            "Cancelled. Nobody will be struck for that message."
            if result.deleted_count
            else "That message isn't waiting for a reaction check."
        )
        await interaction.response.send_message(text, ephemeral=True)

    # ----- the check -----

    @tasks.loop(seconds=60)
    async def check_loop(self):
        try:
            loa = self.bot.get_cog("LOA")
            if loa is None:
                # Without the LOA plugin we can't tell who's on leave, so we wait instead of striking people.
                if not self.warned_loa:
                    logger.warning("ReactCheck: LOA plugin isn't loaded, so reaction checks are on hold.")
                    self.warned_loa = True
                return
            self.warned_loa = False

            due = (
                await self.coll.find({"type": "tracked", "checked": False, "check_at": {"$lte": utcnow()}})
                .sort("check_at", 1)
                .to_list(length=20)
            )
            for doc in due:
                try:
                    await self.process(doc, loa)
                except Exception:
                    logger.exception("ReactCheck: failed to process message %s (will retry)", doc.get("message_id"))
        except Exception:
            logger.exception("ReactCheck: check loop error")

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    async def skip(self, doc, reason):
        logger.warning("ReactCheck: skipped message %s (%s)", doc.get("message_id"), reason)
        await self.coll.update_one(
            {"_id": doc["_id"]}, {"$set": {"checked": True, "skipped": reason, "checked_at": utcnow()}}
        )

    async def process(self, doc, loa):
        channel = self.bot.get_channel(doc["channel_id"])
        if channel is None:
            return await self.skip(doc, "channel not found")
        try:
            message = await channel.fetch_message(doc["message_id"])
        except discord.NotFound:
            return await self.skip(doc, "message was deleted")
        except discord.Forbidden:
            return await self.skip(doc, "no permission to read the message")

        role = channel.guild.get_role((await loa.get_config()).get("staff_role_id") or 0)
        if role is None:
            return await self.skip(doc, "staff role isn't set in the LOA plugin")

        reacted = set()
        for reaction in message.reactions:
            async for user in reaction.users():
                reacted.add(user.id)

        now = utcnow()
        loa_docs = await loa.coll.find(
            {"type": "loa", "status": "approved", "start": {"$lte": now}, "end": {"$gt": now}}
        ).to_list(length=None)
        on_loa = {d["user_id"] for d in loa_docs}

        targets = [
            m for m in role.members
            if not m.bot and m.id != doc["author_id"] and m.id not in reacted and m.id not in on_loa
        ]

        claim = await self.coll.update_one(
            {"_id": doc["_id"], "checked": False}, {"$set": {"checked": True, "checked_at": now}}
        )
        if not claim.modified_count:
            return
        for member in targets:
            await self.give_strike(member, message)

    # ----- strikes + discipline -----

    async def give_strike(self, member, message):
        now = utcnow()
        active = await self.coll.count_documents({"type": "strike", "user_id": member.id})
        if active >= MAX_STRIKES:
            return await self.discipline(member, message, now)

        number = active + 1
        await self.coll.insert_one(
            {
                "type": "strike",
                "user_id": member.id,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "url": message.jump_url,
                "created_at": now,
            }
        )
        # A new strike restarts the clean-streak timer that removes strikes one at a time.
        await self.coll.update_one(
            {"type": "decay", "user_id": member.id}, {"$set": {"last_event": now}}, upsert=True
        )

        next_removal = now + datetime.timedelta(days=STRIKE_DECAY_DAYS)
        dm = discord.Embed(
            title=f"Strike {number}/{MAX_STRIKES}",
            description=f"You didn't react to [this message]({message.jump_url}) within "
            f"{CHECK_AFTER_HOURS} hours and weren't on LOA.",
            color=discord.Color.red() if number == MAX_STRIKES else discord.Color.orange(),
        )
        dm.add_field(
            name="Strike removal",
            value=f"If you have no new strikes, one is removed {ts(next_removal, 'R')}, "
            "then one more each week after that.",
            inline=False,
        )
        if number == MAX_STRIKES:
            dm.add_field(
                name="⚠️ Final strike",
                value="You have no more chances. Another strike means disciplinary action.",
                inline=False,
            )
        delivered = await self.dm(member, dm)

        log = discord.Embed(
            title=f"Strike {number}/{MAX_STRIKES}",
            description=f"{member.mention} missed [this message]({message.jump_url}).",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if not delivered:
            log.set_footer(text="Couldn't DM them (DMs closed).")
        await self.log(log)

    async def discipline(self, member, message, now):
        strikes = (
            await self.coll.find({"type": "strike", "user_id": member.id})
            .sort("created_at", 1)
            .to_list(length=50)
        )
        # The record is kept forever; the strikes it replaces are cleared.
        await self.coll.insert_one(
            {
                "type": "discipline",
                "user_id": member.id,
                "created_at": now,
                "trigger_message_id": message.id,
                "trigger_url": message.jump_url,
                "strikes": [{"url": s["url"], "created_at": s["created_at"]} for s in strikes],
            }
        )
        await self.coll.delete_many({"type": "strike", "user_id": member.id})
        await self.coll.delete_many({"type": "decay", "user_id": member.id})

        dm = discord.Embed(
            title="Disciplinary action",
            description=f"You missed [this message]({message.jump_url}) after already reaching "
            f"{MAX_STRIKES} strikes. Disciplinary action has been logged and management has been notified. "
            "Your strikes have been cleared.",
            color=discord.Color.dark_red(),
        )
        delivered = await self.dm(member, dm)

        log = discord.Embed(
            title="Disciplinary action required",
            description=f"{member.mention} missed [this message]({message.jump_url}) while holding "
            f"{MAX_STRIKES} strikes. Their strikes were cleared.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        if strikes:
            log.add_field(
                name="Strikes that led here",
                value="\n".join(f"{i}. [message]({s['url']}) · {ts(s['created_at'], 'd')}" for i, s in enumerate(strikes, 1)),
                inline=False,
            )
        if not delivered:
            log.set_footer(text="Couldn't DM them (DMs closed).")
        await self.log(log, ping_managers=True)

    # ----- weekly strike removal -----

    @tasks.loop(minutes=10)
    async def decay_loop(self):
        try:
            await self.decay_strikes()
        except Exception:
            logger.exception("ReactCheck: decay loop error")

    @decay_loop.before_loop
    async def before_decay_loop(self):
        await self.bot.wait_until_ready()

    async def decay_strikes(self):
        """For every full week without a new strike, remove that member's oldest strike."""
        now = utcnow()
        week = datetime.timedelta(days=STRIKE_DECAY_DAYS)
        due = await self.coll.find({"type": "decay", "last_event": {"$lte": now - week}}).to_list(length=None)
        for state in due:
            last = state["last_event"]
            while last <= now - week:
                oldest = (
                    await self.coll.find({"type": "strike", "user_id": state["user_id"]})
                    .sort("created_at", 1)
                    .to_list(length=1)
                )
                if not oldest:
                    break
                await self.coll.delete_one({"_id": oldest[0]["_id"]})
                last += week
            remaining = await self.coll.count_documents({"type": "strike", "user_id": state["user_id"]})
            if remaining:
                await self.coll.update_one({"_id": state["_id"]}, {"$set": {"last_event": last}})
            else:
                await self.coll.delete_one({"_id": state["_id"]})

    # ----- viewing -----

    async def strike_embed(self, member, history=False):
        strikes = (
            await self.coll.find({"type": "strike", "user_id": member.id}).sort("created_at", 1).to_list(length=10)
        )
        embed = discord.Embed(
            title=f"Strikes: {member.display_name}",
            description=f"**{len(strikes)}/{MAX_STRIKES}** active strikes.",
            color=self.bot.main_color,
        )
        lines = [f"{i}. [message]({s['url']}) · {ts(s['created_at'], 'd')}" for i, s in enumerate(strikes, 1)]
        embed.add_field(name="Active strikes", value="\n".join(lines) or "None", inline=False)
        if strikes:
            state = await self.coll.find_one({"type": "decay", "user_id": member.id})
            if state:
                due = state["last_event"] + datetime.timedelta(days=STRIKE_DECAY_DAYS)
                embed.add_field(name="Next strike removed", value=f"{ts(due, 'R')} if no new strikes", inline=False)
        if history:
            total = await self.coll.count_documents({"type": "discipline", "user_id": member.id})
            actions = (
                await self.coll.find({"type": "discipline", "user_id": member.id})
                .sort("created_at", -1)
                .to_list(length=10)
            )
            lines = [
                f"{ts(a['created_at'], 'd')} · [trigger]({a['trigger_url']}) · {len(a['strikes'])} strike(s) cleared"
                for a in actions
            ]
            embed.add_field(name=f"Disciplinary actions ({total})", value="\n".join(lines) or "None", inline=False)
        else:
            embed.set_footer(
                text=f"One strike is removed after each week without a new strike. "
                f"A strike while at {MAX_STRIKES} means disciplinary action."
            )
        return embed

    # ----- slash commands -----

    @app_commands.command(name="mystrikes", description="See your active strikes.")
    @app_commands.guild_only()
    async def mystrikes(self, interaction: discord.Interaction):
        if not self.is_staff(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("You need the staff role to use this.", ephemeral=True)
        await interaction.response.send_message(embed=await self.strike_embed(interaction.user), ephemeral=True)

    @app_commands.command(name="strikeview", description="View a member's strikes and disciplinary history (management).")
    @app_commands.describe(member="Whose record to view")
    @app_commands.guild_only()
    async def strikeview(self, interaction: discord.Interaction, member: discord.Member):
        if not self.is_manager(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        await interaction.response.send_message(embed=await self.strike_embed(member, history=True), ephemeral=True)

    # ----- prefix commands for setup (admins) -----

    @commands.command(name="reactsettings")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactsettings(self, ctx):
        """Show reaction-check settings."""
        cfg = await self.get_config()
        lcfg = await self.loa_config()
        embed = discord.Embed(title="Reaction check settings", color=self.bot.main_color)
        embed.add_field(
            name="Auto-tracked channels (staff-role pings)",
            value=" ".join(f"<#{i}>" for i in sorted(self.watch_ids)) or "none",
            inline=False,
        )
        embed.add_field(name="Log channel", value=f"<#{cfg['log_channel_id']}>" if cfg.get("log_channel_id") else "not set")
        embed.add_field(name="Staff role (from LOA)", value=f"<@&{lcfg['staff_role_id']}>" if lcfg.get("staff_role_id") else "not set")
        embed.add_field(name="LOA plugin", value="loaded" if self.bot.get_cog("LOA") else "NOT loaded (checks on hold)")
        embed.set_footer(text="?reactaddchannel, ?reactremovechannel, ?reactsetlog")
        await ctx.send(embed=embed)

    @commands.command(name="reactaddchannel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactaddchannel(self, ctx, channel: discord.TextChannel):
        """Auto-track messages in this channel that ping the staff role."""
        self.watch_ids.add(channel.id)
        await self.set_config("watch_channel_ids", sorted(self.watch_ids))
        await ctx.send(
            f"New messages in {channel.mention} that ping the staff role are now tracked. "
            f"Staff get {CHECK_AFTER_HOURS} hours to react. Other messages are ignored."
        )

    @commands.command(name="reactremovechannel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactremovechannel(self, ctx, channel: discord.TextChannel):
        """Stop auto-tracking a channel (messages already tracked are still checked)."""
        self.watch_ids.discard(channel.id)
        await self.set_config("watch_channel_ids", sorted(self.watch_ids))
        await ctx.send(f"No longer auto-tracking new messages in {channel.mention}.")

    @commands.command(name="reactsetlog")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactsetlog(self, ctx, channel: discord.TextChannel):
        """Set the channel for strike and disciplinary logs."""
        await self.set_config("log_channel_id", channel.id)
        await ctx.send(f"Strike log channel set to {channel.mention}.")


async def setup(bot):
    await bot.add_cog(ReactCheck(bot))
