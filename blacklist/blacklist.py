import datetime
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger("Modmail")

# Invalid requests get an "auto denied" reply and are deleted (the sender's text is copied into the reply so
# they can fix and resend it). Set to False to leave invalid messages in the channel.
DELETE_INVALID_REQUESTS = True

UTC = datetime.timezone.utc

TEMPLATE = (
    "Your username:\n"
    "Your rank:\n"
    "\n"
    "Their username:\n"
    "Their user id:\n"
    "\n"
    "Blacklist Reason:\n"
    "Evidence: (must include one screenshot at least)"
)

# key, label shown to people
FIELDS = [
    ("your_username", "Your username"),
    ("your_rank", "Your rank"),
    ("their_username", "Their username"),
    ("their_id", "Their user id"),
    ("reason", "Blacklist Reason"),
    ("evidence", "Evidence"),
]
# what a typed label is normalised to (lowercase letters only) -> key
ALIASES = {
    "yourusername": "your_username",
    "yourrank": "your_rank",
    "theirusername": "their_username",
    "theiruserid": "their_id",
    "theirid": "their_id",
    "blacklistreason": "reason",
    "evidence": "evidence",
}
EVIDENCE_PLACEHOLDER = re.compile(r"\(\s*must include one screenshot at least\s*\)", re.I)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")
IMAGE_HOSTS = ("cdn.discordapp.com", "media.discordapp.net", "i.imgur.com", "imgur.com", "i.gyazo.com", "gyazo.com", "prnt.sc")


def utcnow():
    """Naive UTC datetime (matches what MongoDB hands back)."""
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def pretty_date(dt):
    return f"{dt:%B} {dt.day}, {dt.year}"


# ---------- reading and checking a request ----------

def parse_request(content):
    """Split a message into {field key: text}. Labels are case-insensitive; values can span several lines."""
    fields = {}
    current = None
    for raw in (content or "").splitlines():
        label, sep, rest = raw.partition(":")
        key = ALIASES.get(re.sub(r"[^a-z]", "", label.lower())) if sep else None
        if key and key not in fields:
            current = key
            fields[key] = re.sub(r"^\s*\*+", "", rest).strip()  # tolerate **Label:** value
        elif current:
            fields[current] += "\n" + raw
    if "evidence" in fields:
        fields["evidence"] = EVIDENCE_PLACEHOLDER.sub("", fields["evidence"]).strip()
    return {k: v.strip() for k, v in fields.items()}


def clean_id(value):
    digits = re.sub(r"[<@!>`\s]", "", value or "")
    return int(digits) if re.fullmatch(r"\d{17,20}", digits) else None


def is_image_url(url):
    url = url.lower().split("?")[0].rstrip(").,>")
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return url.endswith(IMAGE_EXT) or host in IMAGE_HOSTS


def evidence_links(attachments, evidence_text):
    """Image attachments and image links that count as screenshots."""
    links = [
        a.url for a in attachments
        if (a.content_type or "").startswith("image/") or a.filename.lower().endswith(IMAGE_EXT)
    ]
    links += [u for u in re.findall(r"https?://\S+", evidence_text or "") if is_image_url(u)]
    return links


def check_request(fields, attachments):
    """Returns a list of problems (empty = the request is correctly formatted)."""
    problems = []
    missing = [label for key, label in FIELDS if key not in fields]
    if missing:
        problems.append(
            f"The format needs all {len(FIELDS)} fields ({', '.join(label for _, label in FIELDS)}). "
            f"Missing: {', '.join(missing)}."
        )
    for key, label in FIELDS:
        if key in fields and key != "evidence" and not fields[key].strip():
            problems.append(f"**{label}** is empty.")
    if fields.get("their_id") and clean_id(fields["their_id"]) is None:
        problems.append("**Their user id** must be a Discord user ID (17-20 digits).")
    if not evidence_links(attachments, fields.get("evidence", "")):
        problems.append("**Evidence** must include at least one screenshot (attach an image or paste an image link).")
    return problems


# ---------- override gate (same idea as the LOA / reaction-check plugins) ----------

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


# ---------- the cog ----------

class Blacklist(commands.Cog):
    """Staff blacklist: a live list, a request channel with format checking, and management approval."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self.request_channel_id = None

    async def cog_load(self):
        self.request_channel_id = (await self.get_config()).get("request_channel_id")

    async def cog_app_command_error(self, interaction, error):
        logger.error("Blacklist command failed", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ----- config + management -----

    async def get_config(self):
        return await self.coll.find_one({"_id": "config"}) or {}

    async def set_config(self, key, value):
        await self.coll.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)

    async def loa_config(self):
        """Management is defined by the LOA plugin's manager role (plus anyone with Administrator)."""
        loa = self.bot.get_cog("LOA")
        return (await loa.get_config()) if loa else {}

    def is_manager(self, member, lcfg):
        if member.guild_permissions.administrator:
            return True
        role_id = lcfg.get("manager_role_id")
        return bool(role_id) and any(r.id == role_id for r in member.roles)

    @staticmethod
    def can_override(user_id, lcfg):
        return user_id in lcfg.get("override_user_ids", [])

    # ----- the list -----

    def list_pages(self, entries):
        lines = [
            f"• **{discord.utils.escape_markdown(e['username'])}** (`{e['user_id']}`) — added {pretty_date(e['added_at'])}"
            for e in entries
        ]
        pages, current, size = [], [], 0
        for line in lines:
            if current and size + len(line) + 1 > 3800:
                pages.append(current)
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        pages.append(current)

        embeds = []
        for number, chunk in enumerate(pages):
            embed = discord.Embed(color=discord.Color.red(), description="\n".join(chunk) or "No one is on the blacklist.")
            if number == 0:
                embed.title = "🚫 Staff Blacklist"
            embeds.append(embed)
        embeds[-1].set_footer(text=f"{len(entries)} on the list")
        embeds[-1].timestamp = discord.utils.utcnow()
        return embeds

    async def refresh_list(self):
        """Create or update the list message(s). Returns an error message, or None if it worked."""
        cfg = await self.get_config()
        channel = self.bot.get_channel(cfg.get("list_channel_id", 0))
        if channel is None:
            return "The list channel isn't set, or I can't see it. Run `?blacklistsetlist #channel`."
        entries = await self.coll.find({"type": "entry"}).sort("added_at", 1).to_list(length=None)
        pages = self.list_pages(entries)
        old_ids = list(cfg.get("list_message_ids", []))
        new_ids = []
        for number, embed in enumerate(pages):
            message = None
            if number < len(old_ids):
                try:
                    message = await channel.fetch_message(old_ids[number])
                except discord.HTTPException:
                    message = None  # deleted: post a fresh one
            try:
                if message:
                    await message.edit(embed=embed)
                else:
                    message = await channel.send(embed=embed)
            except discord.HTTPException:
                return "I couldn't post in the list channel. Check that I can send messages and embed links there."
            new_ids.append(message.id)
        for extra in old_ids[len(pages):]:
            try:
                await (await channel.fetch_message(extra)).delete()
            except discord.HTTPException:
                pass
        await self.set_config("list_message_ids", new_ids)
        return None

    # ----- the request channel -----

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.channel.id != self.request_channel_id or message.guild is None:
            return
        if message.author.bot or message.reference is not None:
            return  # replies are discussion, not requests
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if self.is_manager(message.author, await self.loa_config()):
            return  # management can talk in the channel
        problems = check_request(parse_request(message.content), message.attachments)
        if not problems:
            return  # a correctly formatted request: the bot stays quiet
        await self.auto_deny(message, problems)

    async def auto_deny(self, message, problems):
        embed = discord.Embed(
            title="❌ Blacklist request auto denied",
            description=f"{message.author.mention}, this request was **auto denied** for the reason:\n"
            + "\n".join(f"• {p}" for p in problems),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Required format", value=f"```\n{TEMPLATE}\n```", inline=False)
        if DELETE_INVALID_REQUESTS and message.content.strip():
            sent = message.content.replace("```", "'''")[:900]
            embed.add_field(
                name="What you sent (copy it, fix it, and send it again with your screenshot)",
                value=f"```\n{sent}\n```",
                inline=False,
            )
        try:
            await message.reply(embed=embed, mention_author=True)
        except discord.HTTPException:
            await message.channel.send(content=message.author.mention, embed=embed)
        if DELETE_INVALID_REQUESTS:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.warning("Blacklist: couldn't delete an invalid request (missing Manage Messages?)")

    # ----- approving / denying -----

    async def apply_decision(self, message, fields, approve, reason, manager, override_used=False):
        """Record the decision, update the list, and announce it. Returns a message for the manager."""
        target_id = clean_id(fields["their_id"])
        if await self.coll.find_one({"type": "request", "message_id": message.id}):
            return "⚠️ That request was already decided."
        if approve and await self.coll.find_one({"type": "entry", "user_id": target_id}):
            return "⚠️ That person is already on the blacklist."

        now = utcnow()
        links = evidence_links(message.attachments, fields.get("evidence", ""))
        await self.coll.insert_one(
            {
                "type": "request",
                "message_id": message.id,
                "channel_id": message.channel.id,
                "status": "approved" if approve else "denied",
                "decided_by": manager.id,
                "decision_reason": reason,
                "decided_at": now,
                "target_id": target_id,
                "requester_id": message.author.id,
            }
        )
        if approve:
            await self.coll.insert_one(
                {
                    "type": "entry",
                    "user_id": target_id,
                    "username": fields["their_username"],
                    "added_at": now,
                    "added_by": manager.id,
                    "requester_username": fields["your_username"],
                    "requester_rank": fields["your_rank"],
                    "requester_id": message.author.id,
                    "reason": fields["reason"],
                    "evidence": links,
                    "request_message_id": message.id,
                    "approval_note": reason,
                }
            )
            problem = await self.refresh_list()
        else:
            problem = None

        who = f"**{discord.utils.escape_markdown(fields['their_username'])}** (`{target_id}`)"
        if approve:
            embed = discord.Embed(
                title="✅ Blacklist request accepted",
                description=f"{message.author.mention}, your request for {who} was **accepted** by "
                f"{manager.mention}. They are now on the staff blacklist.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Reason", value=fields["reason"][:1000], inline=False)
            if reason:
                embed.add_field(name="Note from management", value=reason, inline=False)
        else:
            embed = discord.Embed(
                title="❌ Blacklist request denied",
                description=f"{message.author.mention}, your request for {who} was **denied** by {manager.mention}.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Reason for denial", value=reason, inline=False)
        if override_used:
            embed.add_field(name="⚠️ Override", value="The manager decided a request that involves them.", inline=False)
        try:
            await message.reply(embed=embed, mention_author=True)
        except discord.HTTPException:
            try:
                await message.channel.send(content=message.author.mention, embed=embed)
            except discord.HTTPException:
                pass

        text = "✅ Approved. They're on the list now." if approve else "✅ Denied."
        if problem:
            text += f"\n⚠️ {problem}"
        return text

    @app_commands.command(name="blacklistapproval", description="Approve or deny a blacklist request (management only).")
    @app_commands.describe(
        request="The request (pick from the list, or paste its message link or ID)",
        decision="Approve or deny",
        reason="Your reasoning (required when denying)",
    )
    @app_commands.choices(
        decision=[app_commands.Choice(name="Approve", value="approve"), app_commands.Choice(name="Deny", value="deny")]
    )
    @app_commands.guild_only()
    async def blacklistapproval(
        self,
        interaction: discord.Interaction,
        request: str,
        decision: app_commands.Choice[str],
        reason: Optional[str] = None,
    ):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        approve = decision.value == "approve"
        reason = reason.strip() if reason else None
        if not approve and not reason:
            return await interaction.response.send_message("Please give a reason when denying a request.", ephemeral=True)
        if reason and len(reason) > 300:
            return await interaction.response.send_message("Keep the reason under 300 characters.", ephemeral=True)

        cfg = await self.get_config()
        channel = self.bot.get_channel(cfg.get("request_channel_id", 0))
        if channel is None or (approve and self.bot.get_channel(cfg.get("list_channel_id", 0)) is None):
            return await interaction.response.send_message(
                "The blacklist isn't set up yet. An admin needs to run `?blacklistsetlist` and `?blacklistsetrequests`.",
                ephemeral=True,
            )
        found = re.search(r"(\d{17,20})\s*$", request.strip())
        if not found:
            return await interaction.response.send_message(
                "Pick a request from the list, or paste a message link or ID.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            message = await channel.fetch_message(int(found.group(1)))
        except discord.HTTPException:
            return await interaction.followup.send("I can't find that request in the requests channel.", ephemeral=True)
        previous = await self.coll.find_one({"type": "request", "message_id": message.id})
        if previous:
            return await interaction.followup.send(f"That request was already {previous['status']}.", ephemeral=True)
        fields = parse_request(message.content)
        problems = check_request(fields, message.attachments)
        if problems:
            return await interaction.followup.send(
                "That message isn't a correctly formatted request:\n" + "\n".join(f"• {p}" for p in problems),
                ephemeral=True,
            )

        involved = interaction.user.id in (message.author.id, clean_id(fields["their_id"]))
        if involved and not self.can_override(interaction.user.id, lcfg):
            return await interaction.followup.send(
                "You can't decide a request that involves you. Another manager has to.", ephemeral=True
            )
        if involved:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                text = await self.apply_decision(message, fields, approve, reason, interaction.user, override_used=True)
                await inter.edit_original_response(content=text)

            return await interaction.followup.send(
                "⚠️ This request involves **you**. Press **Override** to continue.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.followup.send(
            await self.apply_decision(message, fields, approve, reason, interaction.user), ephemeral=True
        )

    @blacklistapproval.autocomplete("request")
    async def blacklistapproval_request_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self.is_manager(interaction.user, await self.loa_config()):
            return []
        channel = self.bot.get_channel(self.request_channel_id or 0)
        if channel is None:
            return []
        decided = {d["message_id"] for d in await self.coll.find({"type": "request"}).to_list(length=None)}
        choices = []
        try:
            async for message in channel.history(limit=50):
                if message.author.bot or message.id in decided:
                    continue
                fields = parse_request(message.content)
                if check_request(fields, message.attachments):
                    continue
                name = f"{fields['their_username']} ({clean_id(fields['their_id'])}) · by {fields['your_username']}"
                choices.append(app_commands.Choice(name=name[:100], value=str(message.id)))
        except discord.HTTPException:
            return []
        return [c for c in choices if current.lower() in c.name.lower()][:25]

    # ----- removing -----

    async def remove_entry(self, doc):
        await self.coll.delete_one({"_id": doc["_id"], "type": "entry"})
        return await self.refresh_list()

    async def add_entry(self, user_id, username, added_by, reason, source="request"):
        """Add someone to the blacklist from another plugin (e.g. /terminate). Returns (added, problem)."""
        if await self.coll.find_one({"type": "entry", "user_id": user_id}):
            return False, None  # already on the list
        await self.coll.insert_one(
            {
                "type": "entry",
                "user_id": user_id,
                "username": username,
                "added_at": utcnow(),
                "added_by": added_by,
                "requester_username": None,
                "requester_rank": None,
                "requester_id": None,
                "reason": reason,
                "evidence": [],
                "request_message_id": None,
                "approval_note": None,
                "source": source,
            }
        )
        return True, await self.refresh_list()

    async def remove_entry_for(self, user_id):
        """Remove someone by user ID from another plugin. Returns (removed, problem)."""
        doc = await self.coll.find_one({"type": "entry", "user_id": user_id})
        if not doc:
            return False, None
        return True, await self.remove_entry(doc)

    @app_commands.command(name="blacklistremove", description="Remove someone from the staff blacklist (management only).")
    @app_commands.describe(entry="Who to remove (pick from the list, or type their user ID)")
    @app_commands.guild_only()
    async def blacklistremove(self, interaction: discord.Interaction, entry: str):
        lcfg = await self.loa_config()
        if not self.is_manager(interaction.user, lcfg):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        user_id = clean_id(entry)
        doc = await self.coll.find_one({"type": "entry", "user_id": user_id}) if user_id else None
        if not doc:
            return await interaction.response.send_message("That person isn't on the blacklist.", ephemeral=True)
        own = user_id == interaction.user.id
        if own and not self.can_override(interaction.user.id, lcfg):
            return await interaction.response.send_message(
                "You can't remove yourself from the blacklist. Another manager has to.", ephemeral=True
            )

        async def remove():
            problem = await self.remove_entry(doc)
            text = f"✅ Removed **{discord.utils.escape_markdown(doc['username'])}** (`{doc['user_id']}`) from the blacklist."
            return text + (f"\n⚠️ {problem}" if problem else "")

        if own:
            async def go(inter):
                await inter.response.edit_message(content="⏳ Working…", view=None)
                await inter.edit_original_response(content=await remove())

            return await interaction.response.send_message(
                "⚠️ This is **you**. Press **Override** to remove yourself.",
                view=GateView(interaction.user.id, go),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await remove(), ephemeral=True)

    @blacklistremove.autocomplete("entry")
    async def blacklistremove_entry_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self.is_manager(interaction.user, await self.loa_config()):
            return []
        docs = await self.coll.find({"type": "entry"}).sort("added_at", 1).to_list(length=None)
        choices = [
            app_commands.Choice(name=f"{d['username']} ({d['user_id']})"[:100], value=str(d["user_id"])) for d in docs
        ]
        return [c for c in choices if current.lower() in c.name.lower()][:25]

    # ----- hiring check -----

    @app_commands.command(name="staffeligibility", description="Check whether someone is allowed to be hired (management only).")
    @app_commands.describe(user_id="The person's Discord user ID")
    @app_commands.guild_only()
    async def staffeligibility(self, interaction: discord.Interaction, user_id: str):
        if not self.is_manager(interaction.user, await self.loa_config()):
            return await interaction.response.send_message("This command is for management only.", ephemeral=True)
        target = clean_id(user_id)
        if target is None:
            return await interaction.response.send_message(
                "Enter a Discord user ID (17-20 digits).", ephemeral=True
            )
        entry = await self.coll.find_one({"type": "entry", "user_id": target})
        if entry:
            embed = discord.Embed(
                title="🚫 On the blacklist",
                description=f"**{discord.utils.escape_markdown(entry['username'])}** (`{target}`) is on the staff "
                f"blacklist (added {pretty_date(entry['added_at'])}). They are not eligible to be hired.",
                color=discord.Color.red(),
            )
        else:
            embed = discord.Embed(
                title="✅ Allowed to be hired",
                description=f"`{target}` is not on the staff blacklist.",
                color=discord.Color.green(),
            )
        embed.set_footer(text="Checked against the staff blacklist only.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----- setup (admins) -----

    @commands.command(name="blacklistsettings")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def blacklistsettings(self, ctx):
        """Show blacklist settings."""
        cfg = await self.get_config()
        count = await self.coll.count_documents({"type": "entry"})
        embed = discord.Embed(title="Blacklist settings", color=self.bot.main_color)
        embed.add_field(name="List channel", value=f"<#{cfg['list_channel_id']}>" if cfg.get("list_channel_id") else "not set")
        embed.add_field(name="Requests channel", value=f"<#{cfg['request_channel_id']}>" if cfg.get("request_channel_id") else "not set")
        embed.add_field(name="On the list", value=str(count))
        embed.add_field(name="Management", value="the LOA plugin's manager role" if self.bot.get_cog("LOA") else "Administrators only (LOA plugin not loaded)", inline=False)
        embed.set_footer(text="?blacklistsetlist, ?blacklistsetrequests, ?blacklistrefresh")
        await ctx.send(embed=embed)

    @commands.command(name="blacklistsetlist")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def blacklistsetlist(self, ctx, channel: discord.TextChannel):
        """Set the channel that shows the blacklist (a normal text channel, not a forum)."""
        cfg = await self.get_config()
        old = self.bot.get_channel(cfg.get("list_channel_id", 0))
        if old and old.id != channel.id:
            for message_id in cfg.get("list_message_ids", []):
                try:
                    await (await old.fetch_message(message_id)).delete()
                except discord.HTTPException:
                    pass
            await self.set_config("list_message_ids", [])
        await self.set_config("list_channel_id", channel.id)
        problem = await self.refresh_list()
        await ctx.send(problem or f"The blacklist will be shown and kept up to date in {channel.mention}.")

    @commands.command(name="blacklistsetrequests")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def blacklistsetrequests(self, ctx, channel: discord.TextChannel):
        """Set the channel where blacklist requests are submitted."""
        self.request_channel_id = channel.id
        await self.set_config("request_channel_id", channel.id)
        await ctx.send(f"Blacklist requests go in {channel.mention}. I'll auto-deny anything that isn't in the right format.")

    @commands.command(name="blacklistrefresh")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def blacklistrefresh(self, ctx):
        """Rebuild the blacklist embed (e.g. if someone deleted it)."""
        problem = await self.refresh_list()
        await ctx.send(problem or "The blacklist embed is up to date.")


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
