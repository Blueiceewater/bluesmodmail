import datetime
import logging
from typing import Optional

import discord
from bson import ObjectId
from discord import app_commands
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger("Modmail")

CHECK_AFTER_HOURS = 24    # how long staff have to react
STRIKE_DECAY_DAYS = 7     # one strike is removed per this many days without a new strike
MAX_STRIKES = 3           # a strike while already holding this many = disciplinary action
TRACKED_KEEP_DAYS = 30    # finished message records are cleaned up after this long
UTC = datetime.timezone.utc


def utcnow():
    """Naive UTC datetime (matches what MongoDB hands back)."""
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def ts(dt, style="f"):
    return f"<t:{int(dt.replace(tzinfo=UTC).timestamp())}:{style}>"


def strike_line(s):
    """Markdown description of a strike."""
    if s.get("url"):
        return f"[message]({s['url']})"
    return f"manual: {s.get('reason') or 'no reason given'}"


def strike_plain(s):
    """Plain-text description of a strike (autocomplete can't render markdown)."""
    return "missed a message" if s.get("url") else f"manual: {s.get('reason') or 'no reason given'}"


def discipline_line(a):
    trigger = f"[trigger]({a['trigger_url']})" if a.get("trigger_url") else "manual"
    punishment = f" · {a['punishment']}" if a.get("punishment") else ""
    return f"{ts(a['created_at'], 'd')} · {trigger} · {len(a['strikes'])} strike(s) cleared{punishment}"


def discipline_detail(a):
    """Full multi-line description of a disciplinary record (for the view commands)."""
    if a.get("trigger_url"):
        lines = [f"**Trigger:** [missed message]({a['trigger_url']})"]
    elif a.get("trigger_reason"):
        lines = [f"**Reason:** {a['trigger_reason']}"]
    else:
        lines = ["**Trigger:** manual"]
    if a.get("strikes"):
        lines.append(f"**Strikes cleared:** {len(a['strikes'])}")
    if a.get("punishment"):
        lines.append(f"**Punishment:** {a['punishment']}" + (f" ({a['duration']})" if a.get("duration") else ""))
        if a.get("punishment_reason"):
            lines.append(f"**Reasoning:** {a['punishment_reason']}")
        if a.get("punished_by"):
            lines.append(f"**Decided by:** <@{a['punished_by']}>")
    else:
        lines.append("**Punishment:** not recorded yet")
    return "\n".join(lines)


class GateView(discord.ui.View):
    """One-button gate: `action(interaction)` only runs if the invoking manager presses the button."""

    def __init__(self, user_id, action, label="Override"):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.action = action
        self.go.label = label

    @discord.ui.button(label="Override", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't yours to confirm.", ephemeral=True)
        self.stop()
        await self.action(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)


class PunishModal(discord.ui.Modal, title="Record punishment"):
    punishment = discord.ui.TextInput(label="Punishment", max_length=100, placeholder="e.g. Suspension")
    duration = discord.ui.TextInput(
        label="Duration", max_length=50, required=False, placeholder="e.g. 7 days (leave blank if none)"
    )
    reasoning = discord.ui.TextInput(label="Short reasoning", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, cog, record_id, override_used=False):
        super().__init__()
        self.cog = cog
        self.record_id = record_id
        self.override_used = override_used

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.save_punishment(
            interaction,
            self.record_id,
            self.punishment.value.strip(),
            self.duration.value.strip() or None,
            self.reasoning.value.strip(),
            self.override_used,
        )


class PunishView(discord.ui.View):
    """The button under a disciplinary-action log message. Persistent: it keeps working after restarts."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Record punishment", style=discord.ButtonStyle.danger, custom_id="reactcheck:punish")
    async def punish(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = await self.cog.coll.find_one({"type": "discipline", "log_message_id": interaction.message.id})
        if not record:
            return await interaction.response.send_message(
                "I can't find the record for this message.", ephemeral=True
            )
        await self.cog.open_punishment(interaction, record)


class ReactCheck(commands.Cog):
    """Strikes staff who don't react to tracked messages in time (unless they're on LOA)."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self.watch_ids = set()
        self.warned_loa = False
        self.punish_view = None
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
        self.punish_view = PunishView(self)
        self.bot.add_view(self.punish_view)
        self.check_loop.start()
        self.decay_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()
        self.decay_loop.cancel()
        if self.punish_view:
            self.punish_view.stop()
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

    @staticmethod
    def can_override(user_id, lcfg):
        """The (max 2) people chosen with ?loasetoverride in the LOA plugin may act on their own record."""
        return user_id in lcfg.get("override_user_ids", [])

    @staticmethod
    def namespace_user_id(interaction):
        member = getattr(interaction.namespace, "member", None)
        try:
            return int(getattr(member, "id", member))
        except (TypeError, ValueError):
            return None

    async def dm(self, member, embed):
        try:
            await member.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    async def dm_id(self, user_id, embed):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
        return await self.dm(user, embed)

    async def log(self, embed, ping_managers=False, view=discord.utils.MISSING, ping_text=""):
        """Post to the log channel (optionally pinging management). Returns the sent message (or None)."""
        cfg = await self.get_config()
        channel = self.bot.get_channel(cfg.get("log_channel_id", 0))
        if channel is None:
            return None
        content = None
        if ping_managers:
            role_id = (await self.loa_config()).get("manager_role_id")
            if role_id:
                content = f"<@&{role_id}> {ping_text}".strip()
        try:
            return await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
        except discord.HTTPException:
            return None

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

        bypass_id = (await self.get_config()).get("bypass_role_id")
        targets = [
            m for m in role.members
            if not m.bot and m.id != doc["author_id"] and m.id not in reacted and m.id not in on_loa
            and not self.has_role(m, bypass_id)
        ]

        claim = await self.coll.update_one(
            {"_id": doc["_id"], "checked": False}, {"$set": {"checked": True, "checked_at": now}}
        )
        if not claim.modified_count:
            return
        for member in targets:
            await self.give_strike(member, message)

    # ----- strikes + discipline -----

    async def give_strike(self, member, message=None, reason=None, actor=None, override_used=False):
        """Strike a member (missed message, or manual via /addstrike). Returns a short summary."""
        now = utcnow()
        active = await self.coll.count_documents({"type": "strike", "user_id": member.id})
        if active >= MAX_STRIKES:
            await self.discipline(
                member, now, message=message, reason=reason, actor=actor, override_used=override_used
            )
            return (
                f"{member.mention} already had {MAX_STRIKES} strikes, so this triggered disciplinary action "
                "and their strikes were cleared. A button to record the punishment was posted in the log channel."
            )

        number = active + 1
        await self.coll.insert_one(
            {
                "type": "strike",
                "user_id": member.id,
                "channel_id": message.channel.id if message else None,
                "message_id": message.id if message else None,
                "url": message.jump_url if message else None,
                "reason": reason,
                "added_by": actor.id if actor else None,
                "created_at": now,
            }
        )
        # A new strike restarts the clean-streak timer that removes strikes one at a time.
        await self.coll.update_one(
            {"type": "decay", "user_id": member.id}, {"$set": {"last_event": now}}, upsert=True
        )

        if message:
            what = (
                f"You didn't react to [this message]({message.jump_url}) within "
                f"{CHECK_AFTER_HOURS} hours and weren't on LOA."
            )
            what_log = f"{member.mention} missed [this message]({message.jump_url})."
        else:
            what = f"{actor.display_name if actor else 'A manager'} gave you a strike: {reason}"
            what_log = f"{actor.mention if actor else 'A manager'} gave {member.mention} a strike: {reason}"

        next_removal = now + datetime.timedelta(days=STRIKE_DECAY_DAYS)
        dm = discord.Embed(
            title=f"Strike {number}/{MAX_STRIKES}",
            description=what,
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
            description=what_log,
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager struck themselves.", inline=False)
        if not delivered:
            log.set_footer(text="Couldn't DM them (DMs closed).")
        await self.log(log)
        return f"Gave {member.mention} strike {number}/{MAX_STRIKES}."

    async def discipline(
        self, member, now, message=None, reason=None, actor=None, manual=False,
        punishment=None, duration=None, override_used=False,
    ):
        """Create a disciplinary record: automatic (4th strike), via /addstrike at 3, or manual (/adddiscipline).

        Strikes are cleared, the member is DM'd, and management gets a log message with a
        'Record punishment' button unless the punishment is already known. Returns the record id.
        """
        strikes = (
            await self.coll.find({"type": "strike", "user_id": member.id})
            .sort("created_at", 1)
            .to_list(length=50)
        )
        by = actor.display_name if actor else "A manager"
        by_log = actor.mention if actor else "A manager"
        if manual:
            cause = f"{by} recorded disciplinary action against you: {reason}."
            cause_log = f"{by_log} recorded disciplinary action against {member.mention}: {reason}."
        elif message:
            cause = f"You missed [this message]({message.jump_url}) after already reaching {MAX_STRIKES} strikes."
            cause_log = (
                f"{member.mention} missed [this message]({message.jump_url}) while holding {MAX_STRIKES} strikes."
            )
        else:
            cause = f"{by} gave you another strike ({reason}) after you already reached {MAX_STRIKES} strikes."
            cause_log = f"{by_log} gave {member.mention} another strike ({reason}) while they held {MAX_STRIKES}."

        # The record is kept forever; the strikes it replaces are cleared.
        result = await self.coll.insert_one(
            {
                "type": "discipline",
                "user_id": member.id,
                "created_at": now,
                "manual": manual,
                "trigger_message_id": message.id if message else None,
                "trigger_url": message.jump_url if message else None,
                "trigger_reason": reason,
                "triggered_by": actor.id if actor else None,
                "strikes": [
                    {"url": s.get("url"), "reason": s.get("reason"), "created_at": s["created_at"]}
                    for s in strikes
                ],
                "punishment": punishment,
                "duration": duration,
                "punishment_reason": reason if punishment else None,
                "punished_by": actor.id if (punishment and actor) else None,
                "punished_at": now if punishment else None,
            }
        )
        record_id = result.inserted_id
        await self.coll.delete_many({"type": "strike", "user_id": member.id})
        await self.coll.delete_many({"type": "decay", "user_id": member.id})

        tail = " Your strikes have been cleared."
        if not manual:
            tail = " Disciplinary action has been logged and management has been notified." + tail
        if not punishment:
            tail += " Management will send you the punishment details."
        dm = discord.Embed(title="Disciplinary action", description=cause + tail, color=discord.Color.dark_red())
        if punishment:
            dm.add_field(name="Punishment", value=punishment)
            if duration:
                dm.add_field(name="Duration", value=duration)
        delivered = await self.dm(member, dm)

        log = discord.Embed(
            title="Disciplinary action added" if manual else "Disciplinary action required",
            description=f"{cause_log} Their strikes were cleared.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        if strikes:
            log.add_field(
                name="Strikes that led here",
                value="\n".join(f"{i}. {strike_line(s)} · {ts(s['created_at'], 'd')}" for i, s in enumerate(strikes, 1)),
                inline=False,
            )
        if punishment:
            log.add_field(name="Punishment", value=punishment + (f" ({duration})" if duration else ""), inline=False)
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager did this to themselves.", inline=False)
        notes = []
        if not manual and not (await self.loa_config()).get("manager_role_id"):
            notes.append("No manager role is set in the LOA plugin, so nobody was pinged.")
        if not punishment:
            notes.append("Press the button to record the punishment.")
        if not delivered:
            notes.append("Couldn't DM them (DMs closed).")
        if notes:
            log.set_footer(text=" ".join(notes))
        sent = await self.log(
            log,
            ping_managers=not manual,
            view=PunishView(self) if not punishment else discord.utils.MISSING,
            ping_text="A punishment needs to be recorded.",
        )
        if sent:
            await self.coll.update_one(
                {"_id": record_id}, {"$set": {"log_message_id": sent.id, "log_channel_id": sent.channel.id}}
            )
        return record_id

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
        """For every full week without a new strike, remove that member's oldest strike (and tell them)."""
        now = utcnow()
        week = datetime.timedelta(days=STRIKE_DECAY_DAYS)
        due = await self.coll.find({"type": "decay", "last_event": {"$lte": now - week}}).to_list(length=None)
        for state in due:
            last = state["last_event"]
            removed = 0
            while last <= now - week:
                oldest = (
                    await self.coll.find({"type": "strike", "user_id": state["user_id"]})
                    .sort("created_at", 1)
                    .to_list(length=1)
                )
                if not oldest:
                    break
                await self.coll.delete_one({"_id": oldest[0]["_id"]})
                removed += 1
                last += week
            remaining = await self.coll.count_documents({"type": "strike", "user_id": state["user_id"]})
            if remaining:
                await self.coll.update_one({"_id": state["_id"]}, {"$set": {"last_event": last}})
            else:
                await self.coll.delete_one({"_id": state["_id"]})
            if removed:
                await self.dm_id(
                    state["user_id"],
                    discord.Embed(
                        title="A strike was removed",
                        description=f"{removed} of your strike(s) were removed after a week with no new strikes. "
                        f"You now have {remaining}/{MAX_STRIKES} active strike(s).",
                        color=discord.Color.green(),
                    ),
                )

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
        lines = [f"{i}. {strike_line(s)} · {ts(s['created_at'], 'd')}" for i, s in enumerate(strikes, 1)]
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
                .to_list(length=5)
            )
            embed.title = f"Strikes and discipline: {member.display_name}"
            embed.description += f" **{total}** disciplinary action(s) on record."
            for a in actions:
                embed.add_field(
                    name=f"Disciplinary action · {a['created_at']:%b %d, %Y}",
                    value=discipline_detail(a)[:800],
                    inline=False,
                )
            if not actions:
                embed.add_field(name="Disciplinary actions", value="None", inline=False)
            elif total > len(actions):
                embed.set_footer(text=f"Showing the latest {len(actions)} disciplinary actions.")
        else:
            embed.set_footer(
                text=f"One strike is removed after each week without a new strike. "
                f"A strike while at {MAX_STRIKES} means disciplinary action."
            )
        return embed

    async def discipline_embed(self, member):
        total = await self.coll.count_documents({"type": "discipline", "user_id": member.id})
        actions = (
            await self.coll.find({"type": "discipline", "user_id": member.id})
            .sort("created_at", -1)
            .to_list(length=5)
        )
        embed = discord.Embed(
            title=f"Disciplinary actions: {member.display_name}",
            description=f"**{total}** on record.",
            color=self.bot.main_color,
        )
        for a in actions:
            embed.add_field(name=f"{a['created_at']:%b %d, %Y}", value=discipline_detail(a)[:900], inline=False)
        if total > len(actions):
            embed.set_footer(text=f"Showing the latest {len(actions)}.")
        return embed

    async def open_punishment(self, interaction, record):
        """Open the punishment form for a disciplinary record (from the Record punishment button)."""
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This is for management only.", ephemeral=True)
        if record.get("punishment"):
            return await interaction.response.send_message(
                f"A punishment is already recorded for that: **{record['punishment']}**.", ephemeral=True
            )
        own = record["user_id"] == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't set your own punishment. Another manager has to.", ephemeral=True
            )
        if own:
            async def go(inter):
                await inter.response.send_modal(PunishModal(self, record["_id"], override_used=True))

            return await interaction.response.send_message(
                "⚠️ This is **your own disciplinary action**. Press **Override** to continue.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.send_modal(PunishModal(self, record["_id"]))

    async def save_punishment(self, interaction, record_id, punishment, duration, reasoning, override_used=False):
        await interaction.response.defer(ephemeral=True)
        now = utcnow()
        result = await self.coll.update_one(
            {"_id": record_id, "type": "discipline", "punishment": None},
            {
                "$set": {
                    "punishment": punishment,
                    "duration": duration,
                    "punishment_reason": reasoning,
                    "punished_by": interaction.user.id,
                    "punished_at": now,
                }
            },
        )
        if not result.modified_count:
            return await interaction.followup.send(
                "⚠️ A punishment was already recorded for that, or the record is gone.", ephemeral=True
            )
        record = await self.coll.find_one({"_id": record_id})

        fields = [("Punishment", punishment)]
        if duration:
            fields.append(("Duration", duration))
        fields += [("Reasoning", reasoning), ("Decided by", interaction.user.mention)]

        dm = discord.Embed(
            title="Disciplinary action: punishment",
            description="Management has decided your punishment.",
            color=discord.Color.dark_red(),
        )
        for name, value in fields:
            dm.add_field(name=name, value=value if name != "Decided by" else interaction.user.display_name, inline=False)
        delivered = await self.dm_id(record["user_id"], dm)

        # Under the original log message: remove its button, then post the update with the punishment.
        update = discord.Embed(
            title="Punishment recorded",
            description=f"{interaction.user.mention} recorded the punishment for <@{record['user_id']}>.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        for name, value in fields:
            update.add_field(name=name, value=value, inline=False)
        if override_used:
            update.add_field(name="⚠️ Override", value="The manager set their own punishment.", inline=False)

        original = None
        channel = self.bot.get_channel(record.get("log_channel_id") or 0)
        if channel and record.get("log_message_id"):
            try:
                original = await channel.fetch_message(record["log_message_id"])
                embed = original.embeds[0] if original.embeds else discord.Embed(title="Disciplinary action")
                embed.set_footer(text=f"Punishment recorded by {interaction.user.display_name}")
                await original.edit(embed=embed, view=None)
            except (discord.HTTPException, IndexError):
                original = None
        try:
            if original:
                await original.reply(embed=update, mention_author=False)
            else:
                await self.log(update)
        except discord.HTTPException:
            await self.log(update)

        await interaction.followup.send(
            f"✅ Saved and sent to <@{record['user_id']}>." + ("" if delivered else " (Couldn't DM them: DMs closed.)"),
            ephemeral=True,
        )

    async def remove_strike(self, doc, member, actor, override_used=False):
        result = await self.coll.delete_one({"_id": doc["_id"], "type": "strike"})
        if not result.deleted_count:
            return "⚠️ That strike was already removed."
        if not await self.coll.count_documents({"type": "strike", "user_id": member.id}):
            await self.coll.delete_many({"type": "decay", "user_id": member.id})

        await self.dm(
            member,
            discord.Embed(
                title="A strike was removed",
                description=f"{actor.display_name} removed one of your strikes ({strike_line(doc)}).",
                color=discord.Color.green(),
            ),
        )
        log = discord.Embed(
            title="Strike removed",
            description=f"{actor.mention} removed a strike ({strike_line(doc)}) from {member.mention}.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager removed their own strike.", inline=False)
        await self.log(log)
        return f"✅ Removed that strike from {member.mention}."

    async def remove_disciplinary(self, query, member, actor, override_used=False):
        result = await self.coll.delete_many(query)
        count = result.deleted_count
        if not count:
            return "⚠️ Nothing to delete (already removed)."
        await self.dm(
            member,
            discord.Embed(
                title="Disciplinary record removed",
                description=f"{actor.display_name} removed {count} disciplinary record(s) from your file.",
                color=discord.Color.green(),
            ),
        )
        log = discord.Embed(
            title="Disciplinary record deleted",
            description=f"{actor.mention} deleted {count} disciplinary record(s) for {member.mention}.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager deleted their own disciplinary record.", inline=False)
        await self.log(log)
        return f"🗑️ Deleted {count} disciplinary record(s) for {member.mention}."

    # ----- slash commands -----

    @app_commands.command(name="mystrikes", description="See your active strikes.")
    @app_commands.guild_only()
    async def mystrikes(self, interaction: discord.Interaction):
        if not self.is_staff(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("You need the staff role to use this.", ephemeral=True)
        await interaction.response.send_message(embed=await self.strike_embed(interaction.user), ephemeral=True)

    @app_commands.command(name="addstrike", description="Give a member a strike (management only).")
    @app_commands.describe(member="Who gets the strike", reason="Why they're being struck")
    @app_commands.guild_only()
    async def addstrike(
        self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]
    ):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("Bots can't be struck.", ephemeral=True)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't strike yourself. Another manager has to.", ephemeral=True
            )
        if own:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                text = await self.give_strike(member, reason=reason, actor=interaction.user, override_used=True)
                await inter.edit_original_response(content=text)

            return await interaction.response.send_message(
                "⚠️ You're about to give **yourself** a strike. Press **Override** to continue.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        text = await self.give_strike(member, reason=reason, actor=interaction.user)
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="removestrike", description="Remove one of a member's strikes (management only).")
    @app_commands.describe(member="Whose strike to remove", entry="Which strike (pick from the list)")
    @app_commands.guild_only()
    async def removestrike(self, interaction: discord.Interaction, member: discord.Member, entry: str):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't remove your own strikes. Another manager has to.", ephemeral=True
            )
        try:
            doc_id = ObjectId(entry)
        except Exception:
            return await interaction.response.send_message("Pick a strike from the list.", ephemeral=True)
        doc = await self.coll.find_one({"_id": doc_id, "type": "strike", "user_id": member.id})
        if not doc:
            return await interaction.response.send_message("Couldn't find that strike.", ephemeral=True)

        if own:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                text = await self.remove_strike(doc, member, interaction.user, override_used=True)
                await inter.edit_original_response(content=text)

            return await interaction.response.send_message(
                "⚠️ This is **your own strike**. Press **Override** to remove it.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.remove_strike(doc, member, interaction.user), ephemeral=True)

    @removestrike.autocomplete("entry")
    async def removestrike_entry_autocomplete(self, interaction: discord.Interaction, current: str):
        lcfg = await self.loa_config()
        user_id = self.namespace_user_id(interaction)
        if user_id is None or not self.is_manager(interaction.user, lcfg):
            return []
        if user_id == interaction.user.id and not self.can_override(user_id, lcfg):
            return []
        docs = await self.coll.find({"type": "strike", "user_id": user_id}).sort("created_at", 1).to_list(length=25)
        choices = [
            app_commands.Choice(name=f"{d['created_at']:%b %d} · {strike_plain(d)}"[:100], value=str(d["_id"]))
            for d in docs
        ]
        return [c for c in choices if current.lower() in c.name.lower()]

    @app_commands.command(
        name="removedisciplinaryactions", description="Delete a member's disciplinary record(s) (management only)."
    )
    @app_commands.describe(member="Whose record", entry="Which record, or all of them (pick from the list)")
    @app_commands.guild_only()
    async def removedisciplinaryactions(self, interaction: discord.Interaction, member: discord.Member, entry: str):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't delete your own disciplinary records. Another manager has to.", ephemeral=True
            )
        query = {"type": "discipline", "user_id": member.id}
        if entry != "all":
            try:
                query["_id"] = ObjectId(entry)
            except Exception:
                return await interaction.response.send_message("Pick a record from the list.", ephemeral=True)
        count = await self.coll.count_documents(query)
        if not count:
            return await interaction.response.send_message("Couldn't find that record.", ephemeral=True)

        what = f"all {count} disciplinary record(s)" if entry == "all" else "this disciplinary record"
        prompt = f"Permanently delete {what} for {member.mention}? This can't be undone."

        async def delete(inter):
            await inter.response.edit_message(content="⏳ Working…", view=None)
            text = await self.remove_disciplinary(query, member, interaction.user, override_used=own)
            await inter.edit_original_response(content=text)

        if own:
            # Step 1: the Override button. Step 2 (after pressing it): the Delete button.
            async def step_two(inter):
                await inter.response.edit_message(
                    content=f"⚠️ **Override accepted.** {prompt}",
                    view=GateView(interaction.user.id, delete, label="Delete"),
                )

            return await interaction.response.send_message(
                "⚠️ This is **your own disciplinary record**. Press **Override** to continue.",
                view=GateView(interaction.user.id, step_two),
                ephemeral=True,
            )
        await interaction.response.send_message(
            prompt, view=GateView(interaction.user.id, delete, label="Delete"), ephemeral=True
        )

    @removedisciplinaryactions.autocomplete("entry")
    async def removedisciplinary_entry_autocomplete(self, interaction: discord.Interaction, current: str):
        lcfg = await self.loa_config()
        user_id = self.namespace_user_id(interaction)
        if user_id is None or not self.is_manager(interaction.user, lcfg):
            return []
        if user_id == interaction.user.id and not self.can_override(user_id, lcfg):
            return []
        docs = (
            await self.coll.find({"type": "discipline", "user_id": user_id}).sort("created_at", -1).to_list(length=24)
        )
        choices = [app_commands.Choice(name=f"All {len(docs)} records", value="all")] if len(docs) > 1 else []
        for d in docs:
            trigger = "missed a message" if d.get("trigger_url") else "manual strike"
            name = f"{d['created_at']:%b %d %Y} · {trigger} · {len(d['strikes'])} strike(s) cleared"
            choices.append(app_commands.Choice(name=name[:100], value=str(d["_id"])))
        return [c for c in choices if current.lower() in c.name.lower()]

    @app_commands.command(name="mydisciplinary", description="See your disciplinary actions.")
    @app_commands.guild_only()
    async def mydisciplinary(self, interaction: discord.Interaction):
        if not self.is_staff(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("You need the staff role to use this.", ephemeral=True)
        await interaction.response.send_message(embed=await self.discipline_embed(interaction.user), ephemeral=True)

    @app_commands.command(name="adddiscipline", description="Add a disciplinary action to a member (management only).")
    @app_commands.describe(
        member="Who it's for",
        reason="Why",
        punishment="The punishment",
        duration="How long the punishment lasts",
    )
    @app_commands.guild_only()
    async def adddiscipline(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: app_commands.Range[str, 1, 300],
        punishment: app_commands.Range[str, 1, 100],
        duration: app_commands.Range[str, 1, 50],
    ):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("Bots can't be disciplined.", ephemeral=True)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't add disciplinary action to yourself. Another manager has to.", ephemeral=True
            )

        async def create():
            await self.discipline(
                member, utcnow(), reason=reason, actor=interaction.user, manual=True,
                punishment=punishment, duration=duration, override_used=own,
            )
            return f"✅ Disciplinary action added for {member.mention}. Their strikes were cleared and they were DM'd."

        if own:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                await inter.edit_original_response(content=await create())

            return await interaction.response.send_message(
                "⚠️ You're about to add disciplinary action to **yourself**. Press **Override** to continue.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await create(), ephemeral=True)

    @app_commands.command(name="viewdiscipline", description="View a member's strikes and disciplinary actions (management only).")
    @app_commands.describe(member="Whose record to view")
    @app_commands.guild_only()
    async def viewdiscipline(self, interaction: discord.Interaction, member: discord.Member):
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
        embed.add_field(name="Bypass role", value=f"<@&{cfg['bypass_role_id']}>" if cfg.get("bypass_role_id") else "none")
        embed.set_footer(text="?reactaddchannel, ?reactremovechannel, ?reactsetlog, ?reactsetbypass")
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

    @commands.command(name="reactsetbypass")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactsetbypass(self, ctx, role: Optional[discord.Role] = None):
        """Members with this role are never struck for missing a reaction. Run with no role to remove it."""
        await self.set_config("bypass_role_id", role.id if role else None)
        if role:
            await ctx.send(
                f"Members with {role.mention} won't be struck for missing reactions.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send("Bypass role removed.")

    @commands.command(name="reactsetlog")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def reactsetlog(self, ctx, channel: discord.TextChannel):
        """Set the channel for strike and disciplinary logs."""
        await self.set_config("log_channel_id", channel.id)
        await ctx.send(f"Strike log channel set to {channel.mention}.")


async def setup(bot):
    await bot.add_cog(ReactCheck(bot))
