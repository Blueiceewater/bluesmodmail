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

MAX_DAYS = 7           # max length of a regular LOA
MAX_FORCED_DAYS = 21   # max length of a forced LOA
MAX_PER_MONTH = 2      # regular LOAs per calendar month (UTC)
RETENTION_DAYS = 120   # records are deleted ~4 months after they were created
UTC = datetime.timezone.utc


# ---------- helpers ----------

def utcnow():
    """Naive UTC datetime (matches what MongoDB hands back)."""
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def ts(dt, style="f"):
    """Discord timestamp markup for a naive-UTC datetime."""
    return f"<t:{int(dt.replace(tzinfo=UTC).timestamp())}:{style}>"


def fmt_duration(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h"
    return f"{max(rem // 60, 1)}m"


def month_label(key):
    return datetime.datetime.strptime(key, "%Y-%m").strftime("%B %Y")


def parse_start(text, max_ahead=30):
    """Returns (start, error). No text = start now (or on approval)."""
    now = utcnow()
    if not text:
        return now, None
    try:
        start = datetime.datetime.strptime(text.strip(), "%Y-%m-%d")
    except ValueError:
        return None, "Start date must look like `2026-12-01` (UTC)."
    if start.date() < now.date():
        return None, "The start date can't be in the past."
    if start > now + datetime.timedelta(days=max_ahead):
        return None, f"The start date can't be more than {max_ahead} days away."
    return max(start, now), None


def status_label(doc, now):
    status = doc["status"]
    if status == "pending":
        return "⏳ Pending"
    if status == "denied":
        return "❌ Denied"
    if status == "ended":
        return "⏹️ Ended early"
    if status == "cancelled":
        return "🚫 Cancelled"
    if now < doc["start"]:
        return "📅 Upcoming"
    if now < doc["end"]:
        return "🟢 Active"
    return "✅ Completed"


def describe_loa(doc, now):
    """One-line summary with Discord timestamps."""
    end = doc.get("ended_at") or doc["end"]
    if doc["status"] == "pending" and not doc["scheduled"]:
        when = "starts when approved"
    else:
        when = f"{ts(doc['start'], 'd')} → {ts(end, 'd')}"
    line = f"{status_label(doc, now)} · {when} · **{fmt_duration(doc['duration_seconds'])}**"
    return line + (" · forced" if doc["forced"] else "")


def plain_label(doc):
    """Plain-text summary (for autocomplete, which can't render timestamps)."""
    end = doc.get("ended_at") or doc["end"]
    label = f"{doc['start']:%b %d} → {end:%b %d} · {fmt_duration(doc['duration_seconds'])} · {doc['status']}"
    return label + (" · forced" if doc["forced"] else "")


# ---------- UI ----------

class ReasonModal(discord.ui.Modal):
    def __init__(self, view, approve):
        super().__init__(title="Approve request" if approve else "Deny request")
        self.manage_view = view
        self.approve = approve
        self.reason = discord.ui.TextInput(
            label="Reasoning",
            style=discord.TextStyle.paragraph,
            placeholder="Why are you approving this?" if approve else "Why are you denying this?",
            required=True,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.manage_view.decide(interaction, self.approve, self.reason.value.strip())


class ManageView(discord.ui.View):
    def __init__(self, cog, interaction, can_override=False):
        super().__init__(timeout=600)
        self.cog = cog
        self.interaction = interaction
        self.can_override = can_override
        self.doc = None

    async def load(self):
        query = {"type": "loa", "status": "pending"}
        if not self.can_override:
            query["user_id"] = {"$ne": self.interaction.user.id}  # nobody reviews their own request
        self.doc = await self.cog.coll.find_one(query, sort=[("created_at", 1)])

    async def embed(self):
        color = self.cog.bot.main_color
        if not self.doc:
            return discord.Embed(description="No pending LOA requests.", color=color)
        doc = self.doc
        used = await self.cog.count_used(doc["user_id"], doc["month"], include_pending=False)
        embed = discord.Embed(title="LOA request", color=color)
        embed.add_field(name="Member", value=f"<@{doc['user_id']}>")
        embed.add_field(name="Length", value=f"{doc['days']} day(s)")
        embed.add_field(name="Starts", value=ts(doc["start"], "D") if doc["scheduled"] else "When approved")
        embed.add_field(name=f"Used in {month_label(doc['month'])}", value=f"{used}/{MAX_PER_MONTH}")
        embed.add_field(name="Requested", value=ts(doc["created_at"], "R"))
        embed.add_field(name="Reason", value=doc["reason"], inline=False)
        if doc["user_id"] == self.interaction.user.id:
            embed.add_field(
                name="⚠️ Your own request",
                value="Only override users can review their own request, and you'll be asked to confirm first.",
                inline=False,
            )
        return embed

    async def start_decision(self, interaction, approve):
        if self.doc["user_id"] == interaction.user.id:
            await interaction.response.edit_message(
                content=f"⚠️ You're about to {'approve' if approve else 'deny'} **your own request**. Confirm the override?",
                view=OwnConfirmView(self, approve),
            )
        else:
            await interaction.response.send_modal(ReasonModal(self, approve))

    async def decide(self, interaction, approve, reason):
        note = await self.cog.review(
            self.doc["_id"], interaction.user, approve, reason, allow_self=self.can_override
        )
        await self.load()
        embed = await self.embed()
        if not self.doc:
            self.stop()
        await interaction.response.edit_message(content=note, embed=embed, view=self if self.doc else None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_decision(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_decision(interaction, False)

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, cog, doc, override_used=False):
        super().__init__(timeout=120)
        self.cog = cog
        self.doc = doc
        self.override_used = override_used

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ Deleting…", view=None)
        await self.cog.finish_delete(self.doc, interaction.user, self.override_used)
        await interaction.edit_original_response(content="🗑️ LOA deleted.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)
        self.stop()


class OwnConfirmView(discord.ui.View):
    """Shown in /loamanage when an override user is about to review their own request."""

    def __init__(self, manage_view, approve):
        super().__init__(timeout=120)
        self.manage_view = manage_view
        self.approve = approve

    @discord.ui.button(label="Override", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReasonModal(self.manage_view, self.approve))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content=None, view=self.manage_view)

    async def on_timeout(self):
        try:
            await self.manage_view.interaction.edit_original_response(content=None, view=self.manage_view)
        except discord.HTTPException:
            pass


class OverrideConfirmView(discord.ui.View):
    """'Are you sure?' gate for an override. `action(interaction)` only runs if Confirm is pressed."""

    def __init__(self, user_id, action):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.action = action

    @discord.ui.button(label="Override", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't yours to confirm.", ephemeral=True)
        self.stop()
        await self.action(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)


# ---------- the cog ----------

class LOA(commands.Cog):
    """Leave of absence (LOA) system for staff."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)

    async def cog_load(self):
        # Rolling retention: MongoDB deletes records ~4 months after creation.
        # The settings document has no "created_at", so it is never deleted.
        try:
            await self.coll.create_index(
                "created_at", expireAfterSeconds=RETENTION_DAYS * 24 * 60 * 60
            )
        except Exception:
            logger.exception("LOA: could not create retention index")
        self.notify_loop.start()

    def cog_unload(self):
        self.notify_loop.cancel()

    async def cog_app_command_error(self, interaction, error):
        logger.error("LOA command failed", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ----- start / end DMs -----

    async def claim(self, doc_id, flag):
        """Atomically mark a notification as sent; True if we were first."""
        result = await self.coll.update_one({"_id": doc_id, flag: {"$ne": True}}, {"$set": {flag: True}})
        return result.modified_count == 1

    @tasks.loop(seconds=60)
    async def notify_loop(self):
        try:
            now = utcnow()
            starting = await self.coll.find(
                {"type": "loa", "status": "approved", "start": {"$lte": now}, "end": {"$gt": now},
                 "start_notified": {"$ne": True}}
            ).to_list(length=100)
            for doc in starting:
                if await self.claim(doc["_id"], "start_notified"):
                    embed = discord.Embed(
                        title="Your LOA has started",
                        description=f"You're on leave until {ts(doc['end'], 'F')} ({ts(doc['end'], 'R')}).",
                        color=discord.Color.green(),
                    )
                    await self.dm(doc["user_id"], embed)

            ending = await self.coll.find(
                {"type": "loa", "status": "approved", "end": {"$lte": now}, "end_notified": {"$ne": True}}
            ).to_list(length=100)
            for doc in ending:
                if await self.claim(doc["_id"], "end_notified"):
                    embed = discord.Embed(
                        title="Your LOA has ended",
                        description="Welcome back! Your leave is over.",
                        color=discord.Color.blurple(),
                    )
                    await self.dm(doc["user_id"], embed)
        except Exception:
            logger.exception("LOA: notification loop error")

    @notify_loop.before_loop
    async def before_notify_loop(self):
        await self.bot.wait_until_ready()

    # ----- config / permissions -----

    async def get_config(self):
        return await self.coll.find_one({"_id": "config"}) or {}

    async def set_config(self, key, value):
        await self.coll.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)

    @staticmethod
    def has_role(member, role_id):
        return bool(role_id) and any(r.id == role_id for r in member.roles)

    def is_manager(self, member, cfg):
        return member.guild_permissions.administrator or self.has_role(member, cfg.get("manager_role_id"))

    def is_staff(self, member, cfg):
        return self.is_manager(member, cfg) or self.has_role(member, cfg.get("staff_role_id"))

    @staticmethod
    def can_override(user_id, cfg):
        """Only the (max 2) people set with ?loasetoverride may act on their own LOA."""
        return user_id in cfg.get("override_user_ids", [])

    @staticmethod
    async def no_staff(interaction):
        await interaction.response.send_message(
            "You need the staff role to use LOA commands. Admins can set it with `?loasetstaff @role`.",
            ephemeral=True,
        )

    @staticmethod
    async def no_manager(interaction):
        await interaction.response.send_message("This command is for management only.", ephemeral=True)

    # ----- db / messaging helpers -----

    async def count_used(self, user_id, month, include_pending=True):
        statuses = ["approved", "ended"] + (["pending"] if include_pending else [])
        return await self.coll.count_documents(
            {"type": "loa", "user_id": user_id, "month": month, "forced": False, "status": {"$in": statuses}}
        )

    async def dm(self, user_id, embed):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    async def log(self, embed, ping_manager=False):
        cfg = await self.get_config()
        channel = self.bot.get_channel(cfg.get("channel_id", 0))
        if channel is None:
            return
        content = f"<@&{cfg['manager_role_id']}>" if ping_manager and cfg.get("manager_role_id") else None
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            pass

    async def review(self, doc_id, reviewer, approve, reason, allow_self=False):
        """Approve/deny a pending request. Returns a note for the manager."""
        now = utcnow()
        doc = await self.coll.find_one({"_id": doc_id, "status": "pending"})
        if not doc:
            return "⚠️ That request was already handled."
        own = doc["user_id"] == reviewer.id
        if own and not allow_self:
            return "⚠️ You can't review your own request. Another manager has to."

        update = {
            "status": "approved" if approve else "denied",
            "reviewer_id": reviewer.id,
            "review_reason": reason,
            "reviewed_at": now,
        }
        start = end = None
        if approve:
            start = max(doc["start"], now)  # a start in the past moves to approval time
            end = start + datetime.timedelta(days=doc["days"])
            update.update(
                {"start": start, "end": end, "month": start.strftime("%Y-%m"),
                 "duration_seconds": doc["days"] * 86400}
            )
            if start <= now:
                update["start_notified"] = True  # the approval DM already says it starts now
        result = await self.coll.update_one({"_id": doc_id, "status": "pending"}, {"$set": update})
        if not result.modified_count:
            return "⚠️ That request was already handled."

        verb = "approved" if approve else "denied"
        color = discord.Color.green() if approve else discord.Color.red()

        dm = discord.Embed(title=f"Your LOA request was {verb}", color=color)
        if approve:
            dm.add_field(name="Dates", value=f"{ts(start)} → {ts(end)}", inline=False)
        dm.add_field(name="Reviewed by", value=reviewer.display_name)
        dm.add_field(name="Reasoning", value=reason, inline=False)
        await self.dm(doc["user_id"], dm)

        log = discord.Embed(
            title=f"LOA {verb}",
            description=f"<@{doc['user_id']}>'s request was {verb} by {reviewer.mention}.",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        log.add_field(name="Reasoning", value=reason, inline=False)
        if own:
            log.add_field(name="⚠️ Override", value="The reviewer reviewed their own request.", inline=False)
        await self.log(log)

        return f"{'✅ Approved' if approve else '❌ Denied'} <@{doc['user_id']}>'s request."

    async def finish_delete(self, doc, manager, override_used=False):
        await self.coll.delete_one({"_id": doc["_id"]})
        summary = describe_loa(doc, utcnow())

        dm = discord.Embed(
            title="An LOA was removed from your record",
            description=summary,
            color=discord.Color.orange(),
        )
        dm.set_footer(text=f"Removed by {manager.display_name}")
        await self.dm(doc["user_id"], dm)

        log = discord.Embed(
            title="LOA deleted",
            description=f"{manager.mention} deleted an LOA for <@{doc['user_id']}>:\n{summary}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager deleted their own LOA record.", inline=False)
        await self.log(log)

    # ----- slash commands -----

    @app_commands.command(name="loarequest", description=f"Request a leave of absence (max {MAX_DAYS} days).")
    @app_commands.describe(
        days=f"How many days (1-{MAX_DAYS})",
        reason="Why you need leave",
        start="Optional start date as YYYY-MM-DD (UTC). Default: when approved.",
    )
    @app_commands.guild_only()
    async def loarequest(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, MAX_DAYS],
        reason: app_commands.Range[str, 1, 500],
        start: Optional[str] = None,
    ):
        cfg = await self.get_config()
        user = interaction.user
        if not self.is_staff(user, cfg):
            return await self.no_staff(interaction)

        start_dt, error = parse_start(start)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        end_dt = start_dt + datetime.timedelta(days=days)
        month = start_dt.strftime("%Y-%m")

        if await self.coll.find_one({"type": "loa", "user_id": user.id, "status": "pending"}):
            return await interaction.response.send_message(
                "You already have a pending request. Wait for a decision first.", ephemeral=True
            )
        if await self.coll.find_one(
            {"type": "loa", "user_id": user.id, "status": "approved",
             "start": {"$lt": end_dt}, "end": {"$gt": start_dt}}
        ):
            return await interaction.response.send_message(
                "That overlaps with an LOA you already have.", ephemeral=True
            )
        used = await self.count_used(user.id, month)
        if used >= MAX_PER_MONTH:
            return await interaction.response.send_message(
                f"You've already used {used}/{MAX_PER_MONTH} LOAs for {month_label(month)}. "
                "Talk to management if you need an exception.",
                ephemeral=True,
            )

        await self.coll.insert_one(
            {
                "type": "loa",
                "user_id": user.id,
                "status": "pending",
                "forced": False,
                "days": days,
                "reason": reason,
                "start": start_dt,
                "end": end_dt,
                "scheduled": bool(start),
                "month": month,
                "duration_seconds": days * 86400,
                "created_at": utcnow(),
            }
        )

        await interaction.response.send_message(
            "Your LOA request was submitted. You'll get a DM when management reviews it "
            "(or check `/myloa`).",
            ephemeral=True,
        )
        embed = discord.Embed(
            title="New LOA request",
            description=f"{user.mention} requested **{days}** day(s).",
            color=self.bot.main_color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Starts", value=ts(start_dt, "D") if start else "When approved")
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text="Review with /loamanage")
        await self.log(embed, ping_manager=True)

    @app_commands.command(name="loamanage", description="Review pending LOA requests (management only).")
    @app_commands.guild_only()
    async def loamanage(self, interaction: discord.Interaction):
        cfg = await self.get_config()
        if not self.is_manager(interaction.user, cfg):
            return await self.no_manager(interaction)
        view = ManageView(self, interaction, self.can_override(interaction.user.id, cfg))
        await view.load()
        await interaction.response.send_message(
            embed=await view.embed(),
            view=view if view.doc else discord.utils.MISSING,
            ephemeral=True,
        )

    @app_commands.command(name="myloa", description="See your LOA usage by month and whether you're on LOA.")
    @app_commands.guild_only()
    async def myloa(self, interaction: discord.Interaction):
        cfg = await self.get_config()
        if not self.is_staff(interaction.user, cfg):
            return await self.no_staff(interaction)

        now = utcnow()
        docs = (
            await self.coll.find(
                {"type": "loa", "user_id": interaction.user.id, "status": {"$ne": "cancelled"}}
            )
            .sort("start", -1)
            .to_list(length=100)
        )
        active = next((d for d in docs if d["status"] == "approved" and d["start"] <= now < d["end"]), None)
        if active:
            description = f"🟢 **You're currently on LOA** until {ts(active['end'], 'F')} ({ts(active['end'], 'R')})."
        else:
            description = "You're not currently on LOA."

        embed = discord.Embed(title="Your LOA", description=description, color=self.bot.main_color)
        by_month = {}
        for d in docs:
            by_month.setdefault(d["month"], []).append(d)
        by_month.setdefault(now.strftime("%Y-%m"), [])

        for month in sorted(by_month, reverse=True):
            items = by_month[month]
            used = sum(1 for d in items if not d["forced"] and d["status"] in ("pending", "approved", "ended"))
            forced = sum(1 for d in items if d["forced"])
            title = f"{month_label(month)} - {used}/{MAX_PER_MONTH} used"
            if forced:
                title += f" (+{forced} forced)"
            lines = [describe_loa(d, now) for d in items]
            embed.add_field(name=title, value="\n".join(lines)[:1024] or "No LOA used.", inline=False)

        embed.set_footer(text=f"History is kept for about {RETENTION_DAYS // 30} months.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="loalist", description="Show everyone who is on LOA right now.")
    @app_commands.guild_only()
    async def loalist(self, interaction: discord.Interaction):
        cfg = await self.get_config()
        if not self.is_staff(interaction.user, cfg):
            return await self.no_staff(interaction)

        now = utcnow()
        docs = (
            await self.coll.find(
                {"type": "loa", "status": "approved", "start": {"$lte": now}, "end": {"$gt": now}}
            )
            .sort("end", 1)
            .to_list(length=100)
        )
        embed = discord.Embed(title="Currently on LOA", color=self.bot.main_color)
        if not docs:
            embed.description = "Nobody is on LOA right now."
        else:
            lines = [
                f"<@{d['user_id']}> · until {ts(d['end'], 'f')} ({ts(d['end'], 'R')})"
                + (" · forced" if d["forced"] else "")
                for d in docs[:40]
            ]
            if len(docs) > 40:
                lines.append(f"…and {len(docs) - 40} more")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"{len(docs)} on LOA")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def end_loa(self, doc, target, actor, override_used=False):
        """End (or cancel) an approved LOA, notify, and return a message for the actor."""
        now = utcnow()
        own = target.id == actor.id
        if doc["start"] <= now:
            used = (now - doc["start"]).total_seconds()
            update = {"status": "ended", "ended_at": now, "duration_seconds": used}
            title = "LOA ended early"
            detail = f"after **{fmt_duration(used)}**"
        else:
            update = {"status": "cancelled", "ended_at": now, "duration_seconds": 0}
            title = "LOA cancelled"
            detail = "before it started (it won't count toward the monthly limit)"
        result = await self.coll.update_one({"_id": doc["_id"], "status": "approved"}, {"$set": update})
        if not result.modified_count:
            return "⚠️ That LOA was already changed."

        log = discord.Embed(
            title=title,
            description=f"{target.mention}'s LOA ended {detail}. Ended by {actor.mention}.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if override_used:
            log.add_field(name="⚠️ Override", value="Ended their own forced leave.", inline=False)
        await self.log(log)

        # DM the member whenever their LOA ends (self-cancelling an LOA that never started needs no DM)
        if update["status"] == "ended" or not own:
            if update["status"] == "ended":
                dm_title, dm_text = "Your LOA has ended", f"It was ended early {detail}."
            else:
                dm_title, dm_text = "Your upcoming LOA was cancelled", f"It was cancelled {detail}."
            if not own:
                dm_text += f" (Done by {actor.display_name}.)"
            await self.dm(target.id, discord.Embed(title=dm_title, description=dm_text, color=discord.Color.orange()))

        who = "Your" if own else f"{target.mention}'s"
        return f"{who} LOA was ended {detail}."

    @app_commands.command(name="loaend", description="End your current LOA early.")
    @app_commands.describe(member="(Management only) end this member's LOA instead")
    @app_commands.guild_only()
    async def loaend(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        cfg = await self.get_config()
        if not self.is_staff(interaction.user, cfg):
            return await self.no_staff(interaction)
        manager = self.is_manager(interaction.user, cfg)
        override = manager and self.can_override(interaction.user.id, cfg)
        target = member or interaction.user
        own = target.id == interaction.user.id
        if not own and not manager:
            return await self.no_manager(interaction)

        now = utcnow()
        query = {"type": "loa", "user_id": target.id, "status": "approved", "end": {"$gt": now}}
        if own and not override:
            query["forced"] = False  # your own forced leave can only be ended by another manager
        doc = await self.coll.find_one(query, sort=[("start", 1)])
        if not doc:
            return await interaction.response.send_message(
                "No active or upcoming LOA found that you can end. "
                "(Forced leave can only be ended by another manager.)",
                ephemeral=True,
            )

        if own and doc["forced"]:  # only override users can get here
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                text = await self.end_loa(doc, target, interaction.user, override_used=True)
                await inter.edit_original_response(content=text)

            return await interaction.response.send_message(
                "⚠️ This is **your own forced leave**. Confirm the override to end it early?",
                view=OverrideConfirmView(interaction.user.id, go),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        text = await self.end_loa(doc, target, interaction.user)
        await interaction.followup.send(text, ephemeral=True)

    async def create_forced(self, member, actor, days, reason, scheduled, start_dt, end_dt, override_used=False):
        now = utcnow()
        await self.coll.insert_one(
            {
                "type": "loa",
                "user_id": member.id,
                "status": "approved",
                "forced": True,  # forced LOAs never count toward the monthly limit
                "days": days,
                "reason": reason,
                "start": start_dt,
                "end": end_dt,
                "scheduled": scheduled,
                "month": start_dt.strftime("%Y-%m"),
                "duration_seconds": days * 86400,
                "reviewer_id": actor.id,
                "review_reason": reason,
                "reviewed_at": now,
                "created_at": now,
                "start_notified": start_dt <= now,  # the DM below already covers an immediate start
            }
        )

        dm = discord.Embed(title="You've been placed on LOA", color=discord.Color.orange())
        dm.add_field(name="Dates", value=f"{ts(start_dt)} → {ts(end_dt)}", inline=False)
        dm.add_field(name="Reason", value=reason, inline=False)
        await self.dm(member.id, dm)

        log = discord.Embed(
            title="Forced LOA",
            description=f"{member.mention} was placed on forced leave by {actor.mention}.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        log.add_field(name="Dates", value=f"{ts(start_dt)} → {ts(end_dt)}", inline=False)
        log.add_field(name="Reason", value=reason, inline=False)
        if override_used:
            log.add_field(name="⚠️ Override", value="The manager forced leave on themselves.", inline=False)
        await self.log(log)

    @app_commands.command(name="forceloa", description="Force leave on a member / override LOA limits (management).")
    @app_commands.describe(
        member="Who goes on leave",
        days=f"Length in days (1-{MAX_FORCED_DAYS})",
        reason="Reason for the forced leave",
        start="Optional start date as YYYY-MM-DD (UTC). Default: now.",
    )
    @app_commands.guild_only()
    async def forceloa(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Range[int, 1, MAX_FORCED_DAYS],
        reason: app_commands.Range[str, 1, 500],
        start: Optional[str] = None,
    ):
        cfg = await self.get_config()
        if not self.is_manager(interaction.user, cfg):
            return await self.no_manager(interaction)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, cfg):
            return await interaction.response.send_message(
                "You can't force leave on yourself. Another manager has to.", ephemeral=True
            )

        start_dt, error = parse_start(start, max_ahead=60)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        end_dt = start_dt + datetime.timedelta(days=days)

        if own:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                await self.create_forced(
                    member, interaction.user, days, reason, bool(start), start_dt, end_dt, override_used=True
                )
                await inter.edit_original_response(content=f"✅ You're now on forced leave for **{days}** day(s).")

            return await interaction.response.send_message(
                f"⚠️ You're about to put **yourself** on forced leave for **{days}** day(s), "
                "ignoring the monthly limits. Confirm the override?",
                view=OverrideConfirmView(interaction.user.id, go),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        await self.create_forced(member, interaction.user, days, reason, bool(start), start_dt, end_dt)
        await interaction.followup.send(
            f"{member.mention} is now on forced leave for **{days}** day(s).", ephemeral=True
        )

    @app_commands.command(name="loadelete", description="Delete one specific LOA from a member (management only).")
    @app_commands.describe(member="Whose LOA to delete", entry="Which LOA (pick from the list)")
    @app_commands.guild_only()
    async def loadelete(self, interaction: discord.Interaction, member: discord.Member, entry: str):
        cfg = await self.get_config()
        if not self.is_manager(interaction.user, cfg):
            return await self.no_manager(interaction)
        own = member.id == interaction.user.id
        if own and not self.can_override(interaction.user.id, cfg):
            return await interaction.response.send_message(
                "You can't delete your own LOA records. Another manager has to.", ephemeral=True
            )
        try:
            doc_id = ObjectId(entry)
        except Exception:
            return await interaction.response.send_message("Pick an LOA from the list.", ephemeral=True)
        doc = await self.coll.find_one({"_id": doc_id, "type": "loa", "user_id": member.id})
        if not doc:
            return await interaction.response.send_message(
                "Couldn't find that LOA for that member.", ephemeral=True
            )
        prompt = (
            f"Delete this LOA for {member.mention}? This removes it from their record and the "
            f"database (it stops counting toward their monthly limit).\n\n{describe_loa(doc, utcnow())}"
        )
        if own:
            # Step 1: the Override button. Step 2 (after pressing it): the normal Delete / Cancel buttons.
            async def go(inter):
                await inter.response.edit_message(
                    content=f"⚠️ **Override accepted.** {prompt}",
                    view=ConfirmDeleteView(self, doc, override_used=True),
                )

            return await interaction.response.send_message(
                "⚠️ This is **your own LOA record**. Press **Override** to continue.\n\n"
                f"{describe_loa(doc, utcnow())}",
                view=OverrideConfirmView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.send_message(prompt, view=ConfirmDeleteView(self, doc), ephemeral=True)

    @loadelete.autocomplete("entry")
    async def loadelete_entry_autocomplete(self, interaction: discord.Interaction, current: str):
        cfg = await self.get_config()
        if not self.is_manager(interaction.user, cfg):
            return []
        member = getattr(interaction.namespace, "member", None)
        try:
            user_id = int(getattr(member, "id", member))
        except (TypeError, ValueError):
            return []
        if user_id == interaction.user.id and not self.can_override(user_id, cfg):
            return []
        docs = await self.coll.find({"type": "loa", "user_id": user_id}).sort("start", -1).to_list(length=25)
        return [
            app_commands.Choice(name=plain_label(d)[:100], value=str(d["_id"]))
            for d in docs
            if current.lower() in plain_label(d).lower()
        ]

    # ----- prefix commands for setup (admins) -----

    @commands.command(name="loasettings")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def loasettings(self, ctx):
        """Show LOA settings."""
        cfg = await self.get_config()
        embed = discord.Embed(title="LOA settings", color=self.bot.main_color)
        for name, key in (("Staff role", "staff_role_id"), ("Manager role", "manager_role_id")):
            embed.add_field(name=name, value=f"<@&{cfg[key]}>" if cfg.get(key) else "not set")
        embed.add_field(name="Log channel", value=f"<#{cfg['channel_id']}>" if cfg.get("channel_id") else "not set")
        override = ", ".join(f"<@{i}>" for i in cfg.get("override_user_ids", []))
        embed.add_field(name="Override users", value=override or "none", inline=False)
        embed.set_footer(text="?loasetstaff, ?loasetmanager, ?loasetchannel, ?loasetoverride, ?loasync")
        await ctx.send(embed=embed)

    @commands.command(name="loasetstaff")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def loasetstaff(self, ctx, role: discord.Role):
        """Set the role that can request LOA."""
        await self.set_config("staff_role_id", role.id)
        await ctx.send(f"Staff role set to {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="loasetmanager")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def loasetmanager(self, ctx, role: discord.Role):
        """Set the management role (review requests, force/delete LOA)."""
        await self.set_config("manager_role_id", role.id)
        await ctx.send(f"Manager role set to {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="loasetchannel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def loasetchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel for LOA requests and logs."""
        await self.set_config("channel_id", channel.id)
        await ctx.send(f"LOA log channel set to {channel.mention}.")

    @commands.command(name="loasetoverride")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def loasetoverride(self, ctx, user1: discord.Member, user2: Optional[discord.Member] = None):
        """Choose the (max 2) people who may act on their own LOA. Bot owner only."""
        ids = list(dict.fromkeys(u.id for u in (user1, user2) if u))
        await self.set_config("override_user_ids", ids)
        await ctx.send(
            "Override users set to " + ", ".join(f"<@{i}>" for i in ids) + ".",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="loasync")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def loasync(self, ctx):
        """Register the slash commands in this server."""
        self.bot.tree.copy_global_to(guild=ctx.guild)
        synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"Synced {len(synced)} slash command(s) to this server.")


async def setup(bot):
    await bot.add_cog(LOA(bot))
