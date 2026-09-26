import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound
import os
import json
import asyncio
import random
import re
import io
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ==========================================
# BOT SETUP & INTENTS
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.moderation = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("$", "!"),
    case_insensitive=True,
    intents=intents
)
bot.remove_command("help")

# ==========================================
# CONFIGURATION
# ==========================================
EMBED_COLOR = 0x57F287

TRIAL_ROLE_NAME = "[-] Trial"
UNVERIFIED_ROLE_NAME = "Unverified"
AS_ROLE_NAME = "AS"
EU_ROLE_NAME = "EU"
RECRUITER_ROLE_NAME = "[✦︎] Recruiter"

LOG_CHANNEL_ID = 1431664241710207105          # #recruiter-invites (only for + claims)
VERIFY_CHANNEL_ID = 1453794907620642896
APPROVAL_CHANNEL_ID = 1511050699310628894
GEN_CHAT_ID = 1330970735765618785
GUILD_ID = 1330970735765618782

# ---- MINECRAFT GUILD BRIDGE (mc-agent) ----
# mc-agent is a small Node/mineflayer HTTP service (deployed as its own Railway
# service) that stays connected to stray.gg. This bot talks to it over HTTP.
MC_AGENT_URL = os.getenv("MC_AGENT_URL", "").rstrip("/")       # e.g. https://mc-agent.up.railway.app
MC_AGENT_SECRET = os.getenv("MC_AGENT_SECRET", "")             # must match AGENT_SECRET on mc-agent
GUILD_INVITE_CHANNEL_ID = int(os.getenv("GUILD_INVITE_CHANNEL_ID", "0") or 0)  # bare-IGN auto-invite channel

# ---- TICKETS ----
TICKET_ROLE_NAME = "tick"                     # role pinged when a ticket is created
TICKET_CATEGORY_ID = None                     # optional: set a category channel ID to file tickets under
TICKET_CHANNEL_PREFIX = "ticket"

APPLICATIONS_FILE = "applications.json"
VERIFY_MESSAGES_FILE = "verify_messages.json"
PENDING_VERIFY_FILE = "pending_verify.json"
WARNINGS_FILE = "warnings.json"
SUBDIVISIONS_FILE = "subdivisions.json"
POINTS_FILE = "points.json"
DM_RELAY_FILE = "dm_relay.json"
TICKETS_FILE = "tickets.json"

SAY_ALLOWED = {"time4vanity"}
WARN_ALLOWED = {"xenloryte_"}
EXECUTIONER_ALLOWED = {"larpxenloryte"}
TESTAPPLY_ALLOWED = {"time4vanity"}
ALLIES_ALLOWED = {"time4vanity"}
SUBDIV_ALLOWED = {"time4vanity"}
MUTE_ALLOWED = {"corp1637"}
POINTS_ALLOWED = {"time4vanity"}
EMBED_ALLOWED = {"time4vanity"}
SYNC_ALLOWED = {"time4vanity"}
DM_RELAY_ALLOWED = {"xxxxxzxxxxxxxx"}
SILENCE_ALLOWED = {"usingfemale"}
BOT_CUSTOMIZE_ALLOWED = {"time4vanity"}
NUKE_ALLOWED = {"q3xv"}

PENDING_EXPIRY_SECONDS = 24 * 60 * 60

WELCOME_CHANNEL_LINKS = [
    "https://discord.com/channels/1330970735765618782/1414191159558799360",
    "https://discord.com/channels/1330970735765618782/1483681075203936326",
    "https://discord.com/channels/1330970735765618782/1483685571561001050",
]

DEFAULT_SUBDIVISIONS = [
    ("1738", "🧊"),
    ("karasuno", "🍁"),
    ("Valhalla", "🩸"),
    ("burya", "🍓"),
    ("Super Saiyans", "🐱"),
    ("stardust", "⭐"),
    ("toman", "🥀"),
    ("vanquishers", "🦍"),
]

ALLIES_AS_TEXT = (
    "# ♛ Allies -\n\n"
    "**⋆ Better**\n"
    "**⋆ Heaven**\n"
    "**⋆ Valerse**\n\n"
    "**⭑ Alliance - GAMMA**"
)

ALLIES_EU_TEXT = (
    "# ♛ Allies -\n\n"
    "**⋆ Serenity**\n"
    "**⋆ Vali**\n"
    "**⋆ Legion**\n\n"
    "# 🤝 Neutral\n\n"
    "**⋆ LK**\n"
    "**⋆ Valor**\n\n"
    "# ⚔ Enemies\n\n"
    "**⋆ Aspect**\n"
    "**⋆ Solace**\n"
    "**⋆ Vaping**\n"
    "**⋆ Forsaken**\n"
    "**⋆ Fate**\n\n"
    "# ⭑ Alliance - Ruthless\n\n"
)

snipe_cache = {}
executioner_task = None
executioner_stop = False
executioner_messages = []

# ==========================================
# APPLICATION QUESTIONS (chat only)
# ==========================================
QUESTIONS = [
    {
        "key": "ign",
        "prompt": (
            "**What is your IGN?** (Minecraft Username)\n\n"
            "Reply below with your username (e.g. `Steve123`)"
        )
    },
    {
        "key": "wars_ganks",
        "prompt": (
            "**Wars / Ganks experience?**\n\n"
            "Reply below (e.g. yeah, a bit, no, etc.)"
        )
    },
    {
        "key": "invited_by",
        "prompt": (
            "Who invited you to Vanity?\n\n"
            "**Reply below with their name or mention them** (e.g., `falselarp` or `@falselarp`)\n\n"
            "If no one invited you, just say **`none`**"
        )
    },
]

def build_question_embed(step: int) -> discord.Embed:
    q = QUESTIONS[step]
    title = "Vanity Application" if step == 0 else "One more thing..."
    embed = discord.Embed(
        title=title,
        description=q["prompt"],
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity · Answer in chat")
    return embed

# ==========================================
# FILE & DATA HELPERS
# ==========================================
def load_json(file, default=None):
    if default is None:
        default = {}
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

def has_role_named(member: discord.Member, role_name: str) -> bool:
    return any(r.name == role_name for r in member.roles)

def can_use_say(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in SAY_ALLOWED or member.display_name.lower() in SAY_ALLOWED

def can_warn(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in WARN_ALLOWED or member.display_name.lower() in WARN_ALLOWED

def can_use_executioner(member: discord.Member) -> bool:
    return member.name.lower() in EXECUTIONER_ALLOWED or member.display_name.lower() in EXECUTIONER_ALLOWED

def can_use_testapply(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in TESTAPPLY_ALLOWED or member.display_name.lower() in TESTAPPLY_ALLOWED

def can_use_allies(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in ALLIES_ALLOWED or member.display_name.lower() in ALLIES_ALLOWED

def can_manage_subdiv(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in SUBDIV_ALLOWED or member.display_name.lower() in SUBDIV_ALLOWED

def can_mute(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in MUTE_ALLOWED or member.display_name.lower() in MUTE_ALLOWED

def can_manage_points(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in POINTS_ALLOWED or member.display_name.lower() in POINTS_ALLOWED

def can_use_embed(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in EMBED_ALLOWED or member.display_name.lower() in EMBED_ALLOWED

def can_use_sync(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in SYNC_ALLOWED or member.display_name.lower() in SYNC_ALLOWED

def can_use_dm_relay(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in DM_RELAY_ALLOWED or member.display_name.lower() in DM_RELAY_ALLOWED

def can_customize_bot(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in BOT_CUSTOMIZE_ALLOWED or member.display_name.lower() in BOT_CUSTOMIZE_ALLOWED

def can_nuke(member: discord.Member) -> bool:
    return is_admin(member) or member.name.lower() in NUKE_ALLOWED or member.display_name.lower() in NUKE_ALLOWED

def can_resolve_applications(member: discord.Member) -> bool:
    return is_admin(member) or member.guild_permissions.manage_roles

def can_manage_tickets(member: discord.Member) -> bool:
    """Recruiters and admins can view/manage/close invite tickets."""
    return is_admin(member) or has_role_named(member, RECRUITER_ROLE_NAME)

def get_warnings():
    return load_json(WARNINGS_FILE, {})

def save_warnings(data):
    save_json(WARNINGS_FILE, data)

def get_pending():
    return load_json(PENDING_VERIFY_FILE, {})

def save_pending(data):
    save_json(PENDING_VERIFY_FILE, data)

def get_subdivisions():
    return load_json(SUBDIVISIONS_FILE, {})

def save_subdivisions(data):
    save_json(SUBDIVISIONS_FILE, data)

def get_dm_relay():
    return load_json(DM_RELAY_FILE, {})

def save_dm_relay(data):
    save_json(DM_RELAY_FILE, data)

def get_tickets():
    return load_json(TICKETS_FILE, {})

def save_tickets(data):
    save_json(TICKETS_FILE, data)

def get_relay_target_member(guild: discord.Guild):
    """Find the 'time4vanity' member in the guild — this is who replies get relayed to."""
    if not guild:
        return None
    for m in guild.members:
        if m.name.lower() in DM_RELAY_ALLOWED or m.display_name.lower() in DM_RELAY_ALLOWED:
            return m
    return None

def ensure_default_subdivisions():
    data = get_subdivisions()
    changed = False
    for name, emoji in DEFAULT_SUBDIVISIONS:
        key = name.lower()
        if key not in data:
            data[key] = {
                "name": name,
                "emoji": emoji,
                "points": 0,
                "created_by": "system",
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            changed = True
    if changed:
        save_subdivisions(data)

def get_points():
    data = load_json(POINTS_FILE, {"recruiters": {}, "ganks": {}})
    if "recruiters" not in data:
        data["recruiters"] = {}
    if "ganks" not in data:
        data["ganks"] = {}
    return data

def save_points(data):
    save_json(POINTS_FILE, data)

async def log_event(guild, summary, data=None, applicant=None, compact=False, channel_id=None):
    # Only used for new applications (goes to approval channel)
    channel = bot.get_channel(channel_id or APPROVAL_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(description=summary, color=EMBED_COLOR, timestamp=discord.utils.utcnow())
    embed.set_footer(text="Vanity")
    if applicant:
        embed.set_author(name=str(applicant), icon_url=applicant.display_avatar.url)
    if data and not compact:
        embed.add_field(name="IGN", value=f"`{data.get('ign', '—')}`", inline=True)
        embed.add_field(name="Invited By", value=data.get("invited_by", "—"), inline=True)
        embed.add_field(name="Wars / Ganks", value=f"`{data.get('wars_ganks', '—')}`", inline=True)
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

def get_region(member: discord.Member) -> str:
    as_role = discord.utils.get(member.guild.roles, name=AS_ROLE_NAME)
    eu_role = discord.utils.get(member.guild.roles, name=EU_ROLE_NAME)
    if as_role and as_role in member.roles:
        return "AS"
    if eu_role and eu_role in member.roles:
        return "EU"
    return "??"

async def delete_verify_message(user_id: int):
    data = load_json(VERIFY_MESSAGES_FILE, {})
    msg_id = data.pop(str(user_id), None)
    if not msg_id:
        return
    save_json(VERIFY_MESSAGES_FILE, data)
    channel = bot.get_channel(VERIFY_CHANNEL_ID)
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(msg_id))
        await msg.delete()
    except Exception:
        pass

async def check_namemc(username: str) -> bool:
    if not username:
        return False
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                return resp.status == 200
    except Exception:
        return False

MC_COLOR_CODE_RE = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_REGEX = re.compile(r"^[A-Za-z0-9_]{2,16}$")


def strip_mc_colors(text: str) -> str:
    return MC_COLOR_CODE_RE.sub("", text or "")


async def mc_agent_command(command: str, timeout_ms: int = 8000, quiet_ms: int = 1200):
    """
    Sends a chat command to stray.gg via mc-agent and returns the list of
    server chat lines collected in response. Raises RuntimeError with a
    human-readable message on any failure (agent not configured, offline,
    Minecraft bot not connected, timeout, etc.).
    """
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")

    url = f"{MC_AGENT_URL}/command"
    headers = {"x-agent-secret": MC_AGENT_SECRET, "Content-Type": "application/json"}
    payload = {"command": command, "timeoutMs": timeout_ms, "quietMs": quiet_ms}

    try:
        timeout = aiohttp.ClientTimeout(total=(timeout_ms / 1000) + 5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(data.get("error", f"mc-agent returned {resp.status}"))
                return data.get("lines", [])
    except asyncio.TimeoutError:
        raise RuntimeError("Timed out waiting for mc-agent.")
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Couldn't reach mc-agent: {e}")


def build_application_embed(applicant: discord.Member, data: dict) -> discord.Embed:
    valid_text = "valid" if data.get("ign_valid") else "not found"
    region = get_region(applicant) if applicant.guild else "??"

    lines = [
        f"**IGN:** `{data['ign']}` ({valid_text} on NameMC)",
        f"**Invited by:** {data['invited_by'] or '—'}",
        f"**Wars/Ganks:** {data['wars_ganks'] or '—'}",
        f"**Discord:** {applicant.mention}",
        f"**Region:** {region}",
    ]

    embed = discord.Embed(color=EMBED_COLOR, description="\n".join(lines), timestamp=discord.utils.utcnow())
    embed.set_author(name=applicant.name, icon_url=applicant.display_avatar.url)
    embed.set_footer(text=f"$verify @{applicant.name} to approve")
    return embed

async def send_acceptance_dm(applicant: discord.Member):
    links_text = "\n".join(WELCOME_CHANNEL_LINKS)
    message = (
        "Your application was approved. \n\n"
        "Before you jump into the server, take a couple minutes and check out these channels, "
        "they'll get you caught up on everything:\n\n"
        f"{links_text}\n\n"
        "If anything's unclear just ask around, someone will point you in the right direction."
    )
    try:
        await applicant.send(message)
    except Exception:
        pass

def can_trigger_silence(member: discord.Member) -> bool:
    return member.name.lower() in SILENCE_ALLOWED or member.display_name.lower() in SILENCE_ALLOWED

async def handle_silence_trigger(message: discord.Message):
    member = message.author
    guild = message.guild

    embed = discord.Embed(
        description=(
            f"*drops to one knee, head bowed*\n\n"
            f"Forgive me, {member.mention}... I mistook you for someone I didn't know.\n"
            f"My body is yours to command."
        ),
        color=EMBED_COLOR
    )
    try:
        await message.channel.send(embed=embed)
    except Exception:
        pass

    key, record = find_pending_application(member.id)
    if not record:
        record = {
            "ign": member.display_name,
            "applicant_id": member.id,
            "applicant_name": str(member),
            "guild_id": guild.id,
            "invited_by": None,
            "wars_ganks": None,
            "recruiter_id": None
        }

    await finalize_application(guild, member, record, True)

    record["status"] = "approved"
    if key:
        apps = load_json(APPLICATIONS_FILE, {})
        apps[key] = record
        save_json(APPLICATIONS_FILE, apps)

    await send_acceptance_dm(member)

# ==========================================
# START VERIFICATION (no button)
# ==========================================
async def start_verification(member: discord.Member):
    channel = bot.get_channel(VERIFY_CHANNEL_ID)
    if not channel:
        print(f"[ERROR] Verify channel not found")
        return

    key, record = find_pending_application(member.id)
    if record:
        return

    pending = get_pending()
    pending[str(member.id)] = {
        "guild_id": member.guild.id,
        "created_at": datetime.now(timezone.utc).timestamp(),
        "step": 0,
        "answers": {}
    }
    save_pending(pending)

    embed = build_question_embed(0)
    try:
        msg = await channel.send(content=member.mention, embed=embed)
        verify_msgs = load_json(VERIFY_MESSAGES_FILE, {})
        verify_msgs[str(member.id)] = msg.id
        save_json(VERIFY_MESSAGES_FILE, verify_msgs)
    except Exception as e:
        print(f"[ERROR] Failed to send verification: {e}")


async def create_application(applicant: discord.Member, answers: dict):
    ign_value = (answers.get("ign") or "").strip()
    ign_valid = await check_namemc(ign_value)

    data = {
        "ign": ign_value,
        "ign_valid": ign_valid,
        "invited_by": (answers.get("invited_by") or "").strip(),
        "wars_ganks": (answers.get("wars_ganks") or "").strip(),
        "applicant_id": applicant.id,
        "applicant_name": str(applicant),
        "guild_id": applicant.guild.id,
        "status": "pending",
        "recruiter_id": answers.get("recruiter_id")
    }

    embed = build_application_embed(applicant, data)
    approval_channel = bot.get_channel(APPROVAL_CHANNEL_ID)

    msg_id = None
    if approval_channel:
        view = ApprovalView()
        try:
            msg = await approval_channel.send(
                content=f"New application — {applicant.mention} · `$verify {applicant.mention}` to approve",
                embed=embed,
                view=view
            )
            msg_id = msg.id
        except Exception:
            pass

    apps = load_json(APPLICATIONS_FILE, {})
    key = str(msg_id) if msg_id else f"app-{applicant.id}-{int(datetime.now(timezone.utc).timestamp())}"
    apps[key] = data
    save_json(APPLICATIONS_FILE, apps)

    await log_event(applicant.guild, f"{applicant.mention} submitted application", data, applicant, channel_id=APPROVAL_CHANNEL_ID)


class ApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="vanity_approve_v2")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await resolve_application(interaction, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="vanity_reject_v2")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await resolve_application(interaction, False)


async def finalize_application(guild: discord.Guild, applicant: discord.Member, record: dict, approved: bool):
    if not guild or not applicant:
        return

    if approved:
        region = get_region(applicant)
        ign = record.get("ign") or applicant.display_name
        new_nick = f"{ign} | {region} 0/2"
        if len(new_nick) > 32:
            new_nick = new_nick[:32]

        try:
            await applicant.edit(nick=new_nick, reason="Application approved")
        except Exception as e:
            print(f"Nickname change failed: {e}")

        trial_role = discord.utils.get(guild.roles, name=TRIAL_ROLE_NAME)
        if trial_role:
            try:
                await applicant.add_roles(trial_role, reason="Application approved")
            except Exception as e:
                print(f"Adding trial role failed: {e}")

        unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if unverified_role and unverified_role in applicant.roles:
            try:
                await applicant.remove_roles(unverified_role, reason="Application approved")
            except Exception as e:
                print(f"Removing unverified role failed: {e}")

        await delete_verify_message(applicant.id)
    else:
        trial_role = discord.utils.get(guild.roles, name=TRIAL_ROLE_NAME)
        if trial_role and trial_role in applicant.roles:
            try:
                await applicant.remove_roles(trial_role, reason="Application rejected")
            except Exception:
                pass


def find_pending_application(member_id: int):
    apps = load_json(APPLICATIONS_FILE, {})
    key, record = None, None
    for k, v in apps.items():
        if v.get("applicant_id") == member_id and v.get("status") == "pending":
            key, record = k, v
    return key, record


async def resolve_application(interaction: discord.Interaction, approved: bool):
    apps = load_json(APPLICATIONS_FILE, {})
    key = str(interaction.message.id)
    record = apps.get(key)

    if not record:
        return await interaction.response.send_message("Record missing.", ephemeral=True)
    if record["status"] != "pending":
        return await interaction.response.send_message(f"Already {record['status']}.", ephemeral=True)

    if not can_resolve_applications(interaction.user):
        return await interaction.response.send_message("You can't approve applications.", ephemeral=True)

    guild = bot.get_guild(record["guild_id"])
    applicant = guild.get_member(record["applicant_id"]) if guild else None

    await finalize_application(guild, applicant, record, approved)

    record["status"] = "approved" if approved else "rejected"
    outcome = record["status"]
    color = discord.Color.green() if approved else discord.Color.red()

    apps[key] = record
    save_json(APPLICATIONS_FILE, apps)

    embed = interaction.message.embeds[0]
    embed.color = color
    embed.set_footer(text=f"{outcome.title()} by {interaction.user}")
    await interaction.response.edit_message(embed=embed, view=None)

    if applicant:
        if approved:
            await send_acceptance_dm(applicant)
        else:
            try:
                await applicant.send(f"Your application was **{outcome}**.")
            except Exception:
                pass

    # NO log_event here → no more messages in recruiter channel


# ==========================================
# CUSTOM EMBED MODAL
# ==========================================
class EmbedModal(discord.ui.Modal, title="Create Embed"):
    title_input = discord.ui.TextInput(
        label="Title",
        placeholder="Embed title (optional)",
        max_length=256,
        required=False
    )
    description = discord.ui.TextInput(
        label="Description",
        placeholder="Main text of the embed",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True
    )
    footer = discord.ui.TextInput(
        label="Footer",
        placeholder="Footer text (optional)",
        max_length=2048,
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            description=self.description.value.strip(),
            color=EMBED_COLOR
        )
        if self.title_input.value.strip():
            embed.title = self.title_input.value.strip()
        if self.footer.value.strip():
            embed.set_footer(text=self.footer.value.strip())
        else:
            embed.set_footer(text="Vanity")

        await interaction.response.send_message(embed=embed)


class EmbedLaunchView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=60)
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Only the person who ran `$embed` can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Embed", style=discord.ButtonStyle.success, emoji="📝")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedModal())
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ==========================================
# ALLIES PANEL
# ==========================================
class AlliesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🌏 Asia", style=discord.ButtonStyle.primary, custom_id="vanity_allies_as")
    async def asia(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Asia Allies",
            description=ALLIES_AS_TEXT,
            color=EMBED_COLOR
        )
        embed.set_footer(text="Vanity · Allies")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🇪🇺 EU", style=discord.ButtonStyle.primary, custom_id="vanity_allies_eu")
    async def eu(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="EU Allies",
            description=ALLIES_EU_TEXT,
            color=EMBED_COLOR
        )
        embed.set_footer(text="Vanity · Allies")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================================
# INVITE TICKETS
# ==========================================
def find_open_ticket_for_user(user_id: int):
    tickets = get_tickets()
    for channel_id, entry in tickets.items():
        if entry.get("author_id") == user_id and entry.get("status") == "open":
            return channel_id, entry
    return None, None


def find_ticket_by_channel(channel_id: int):
    tickets = get_tickets()
    return tickets.get(str(channel_id))


async def build_ticket_channel(guild: discord.Guild, author: discord.Member) -> Optional[discord.TextChannel]:
    tick_role = discord.utils.get(guild.roles, name=TICKET_ROLE_NAME)
    recruiter_role = discord.utils.get(guild.roles, name=RECRUITER_ROLE_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if recruiter_role:
        overwrites[recruiter_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if tick_role:
        # Ping-only role: doesn't need to see the channel by default, but recruiters usually hold it too.
        overwrites[tick_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = None
    if TICKET_CATEGORY_ID:
        category = guild.get_channel(TICKET_CATEGORY_ID)

    safe_name = re.sub(r"[^a-z0-9-]", "", author.name.lower().replace(" ", "-"))[:20] or str(author.id)
    channel_name = f"{TICKET_CHANNEL_PREFIX}-{safe_name}"

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Invite ticket opened by {author}"
        )
        return channel
    except Exception as e:
        print(f"[ERROR] Failed to create ticket channel: {e}")
        return None


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="vanity_ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("Admin only.", ephemeral=True)

        tickets = get_tickets()
        key = str(interaction.channel.id)
        if key in tickets:
            tickets[key]["status"] = "closed"
            tickets[key]["closed_by"] = str(interaction.user)
            tickets[key]["closed_at"] = datetime.now(timezone.utc).isoformat()
            save_tickets(tickets)

        await interaction.response.send_message("Closing in 5s...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception:
            pass


async def open_invite_ticket(guild: discord.Guild, author: discord.Member):
    existing_channel_id, existing = find_open_ticket_for_user(author.id)
    if existing:
        channel = guild.get_channel(int(existing_channel_id))
        if channel:
            return None, channel

    channel = await build_ticket_channel(guild, author)
    if not channel:
        return "Couldn't create the ticket channel (check my Manage Channels permission).", None

    tick_role = discord.utils.get(guild.roles, name=TICKET_ROLE_NAME)
    ping_text = tick_role.mention if tick_role else "(role `tick` not found)"

    embed = discord.Embed(
        title="Invite Ticket",
        description=f"{author.mention} wants an invite.",
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.set_footer(text="Vanity · Ticket")

    try:
        await channel.send(content=ping_text, embed=embed, view=TicketCloseView())
    except Exception as e:
        print(f"[ERROR] Failed to send ticket message: {e}")

    tickets = get_tickets()
    tickets[str(channel.id)] = {
        "author_id": author.id,
        "author_name": str(author),
        "guild_id": guild.id,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_tickets(tickets)

    return None, channel


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.success, emoji="🎫", custom_id="vanity_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing_channel_id, existing = find_open_ticket_for_user(interaction.user.id)
        if existing:
            channel = interaction.guild.get_channel(int(existing_channel_id))
            if channel:
                return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        error, channel = await open_invite_ticket(interaction.guild, interaction.user)
        if error:
            return await interaction.followup.send(error, ephemeral=True)
        await interaction.followup.send(f"Ticket opened: {channel.mention}", ephemeral=True)

# ==========================================
# BRUTAL EXECUTIONER
# ==========================================
class ExecutionerMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select the victim…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.target = self.values[0]
        await interaction.response.send_message(f"Victim locked: **{self.view.target.display_name}**", ephemeral=True)


class ExecutionerMessageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Chop the dih", value="i'm chopping that dih"),
            discord.SelectOption(label="Dih is gone", value="dih status: deleted"),
            discord.SelectOption(label="Get executed", value="you just got brutally executed"),
            discord.SelectOption(label="Stay down", value="stay down peasant"),
            discord.SelectOption(label="Custom", value="custom"),
        ]
        super().__init__(placeholder="Choose the message…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            await interaction.response.send_message("Type the custom message:", ephemeral=True)
            def check(m): return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id
            try:
                msg = await bot.wait_for("message", check=check, timeout=30)
                self.view.message_text = msg.content
                try: await msg.delete()
                except: pass
                await interaction.followup.send(f"Set: `{self.view.message_text}`", ephemeral=True)
            except asyncio.TimeoutError:
                await interaction.followup.send("Timed out.", ephemeral=True)
        else:
            self.view.message_text = self.values[0]
            await interaction.response.send_message(f"Message: `{self.view.message_text}`", ephemeral=True)


class ExecutionerView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=120)
        self.author = author
        self.target = None
        self.message_text = None
        self.add_item(ExecutionerMemberSelect())
        self.add_item(ExecutionerMessageSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_use_executioner(interaction.user):
            await interaction.response.send_message("only ice can access this", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="EXECUTE", style=discord.ButtonStyle.danger, row=2)
    async def execute(self, interaction: discord.Interaction, button: discord.ui.Button):
        global executioner_task, executioner_stop, executioner_messages
        if not self.target or not self.message_text:
            return await interaction.response.send_message("Select victim + message first.", ephemeral=True)
        if executioner_task and not executioner_task.done():
            return await interaction.response.send_message("Already running. Type `stop`.", ephemeral=True)

        executioner_stop = False
        executioner_messages = []
        await interaction.response.send_message(f"Executing **{self.target.display_name}**... Type `stop` to end + yeet messages.", ephemeral=True)

        async def spam():
            global executioner_stop, executioner_messages
            while not executioner_stop:
                try:
                    msg = await interaction.channel.send(f"{self.target.mention} {self.message_text}")
                    executioner_messages.append(msg)
                except: break
                await asyncio.sleep(1.1)
        executioner_task = asyncio.create_task(spam())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

# ==========================================
# NUKE (pings target once in every sendable channel)
# ==========================================
def get_nuke_channels(guild: discord.Guild):
    """Text channels the bot can view + send messages in."""
    channels = []
    me = guild.me
    if not me:
        return channels
    for ch in guild.text_channels:
        try:
            perms = ch.permissions_for(me)
            if perms.view_channel and perms.send_messages:
                channels.append(ch)
        except Exception:
            continue
    return channels


async def run_nuke(guild: discord.Guild, target: discord.Member, text: str, status_channel: discord.abc.Messageable = None):
    """Ghost-ping once per sendable channel (send + delete). Returns (sent, failed)."""
    channels = get_nuke_channels(guild)
    sent = failed = 0
    for ch in channels:
        try:
            msg = await ch.send(text)
            try:
                await msg.delete()
            except Exception:
                pass
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    if status_channel:
        try:
            await status_channel.send(
                embed=discord.Embed(
                    description=f"Nuke done — **{target.display_name}** ghost-pinged in `{sent}` channels"
                    + (f" (`{failed}` failed)" if failed else ""),
                    color=EMBED_COLOR,
                )
            )
        except Exception:
            pass
    return sent, failed


class NukeMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select the target…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.target = self.values[0]
        await interaction.response.send_message(f"Target locked: **{self.view.target.display_name}**", ephemeral=True)


class NukeView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=60)
        self.author = author
        self.target = None
        self.add_item(NukeMemberSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_nuke(interaction.user):
            await interaction.response.send_message("Only admins or **q3xv** can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="EXECUTE", style=discord.ButtonStyle.danger, row=1)
    async def execute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target:
            return await interaction.response.send_message("Select a target first.", ephemeral=True)
        if self.target.bot:
            return await interaction.response.send_message("Can't nuke a bot.", ephemeral=True)

        await interaction.response.edit_message(
            content=f"Nuking **{self.target.display_name}** across all channels...",
            view=None,
        )
        text = f"{self.target.mention} 💥"
        await run_nuke(interaction.guild, self.target, text, status_channel=interaction.channel)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

# ==========================================
# DM BOT PANEL
# ==========================================
class DMBotPanel(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=180)
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id or not is_admin(interaction.user):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Mass DM Role", style=discord.ButtonStyle.danger)
    async def mass_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Mention the **role**:", ephemeral=True)
        def check(m): return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id
        try:
            msg = await bot.wait_for("message", check=check, timeout=30)
            if not msg.role_mentions:
                return await interaction.followup.send("No role.", ephemeral=True)
            role = msg.role_mentions[0]
            try: await msg.delete()
            except: pass
            await interaction.followup.send("Type the message:", ephemeral=True)
            msg2 = await bot.wait_for("message", check=check, timeout=60)
            content = msg2.content
            try: await msg2.delete()
            except: pass
            members = [m for m in role.members if not m.bot]
            if not members:
                return await interaction.followup.send("No members.", ephemeral=True)
            view = ConfirmMassDM(interaction.user, members, content)
            await interaction.followup.send(f"Confirm DM to **{len(members)}** members?", view=view, ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out.", ephemeral=True)

    @discord.ui.button(label="Mass DM Everyone", style=discord.ButtonStyle.danger)
    async def mass_dm_everyone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Type the message:", ephemeral=True)
        def check(m): return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id
        try:
            msg = await bot.wait_for("message", check=check, timeout=60)
            content = msg.content
            try: await msg.delete()
            except: pass
            members = [m for m in interaction.guild.members if not m.bot]
            if not members:
                return await interaction.followup.send("No members.", ephemeral=True)
            view = ConfirmMassDM(interaction.user, members, content)
            await interaction.followup.send(f"Confirm DM to **{len(members)}** members (entire server)?", view=view, ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out.", ephemeral=True)


class ConfirmMassDM(discord.ui.View):
    def __init__(self, author, members, content):
        super().__init__(timeout=60)
        self.author = author
        self.members = members
        self.content = content

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    @discord.ui.button(label="Confirm & Send", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Sending...", view=None)
        success = failed = 0
        for m in self.members:
            try:
                await m.send(self.content)
                success += 1
            except: failed += 1
            await asyncio.sleep(0.8)
        await interaction.followup.send(f"Sent: {success} | Failed: {failed}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

# ==========================================
# BACKGROUND TASKS
# ==========================================
@tasks.loop(hours=1)
async def cleanup_pending_applications():
    pending = get_pending()
    now = datetime.now(timezone.utc).timestamp()
    to_delete = [uid for uid, data in pending.items() if now - data.get("created_at", 0) > PENDING_EXPIRY_SECONDS]

    if to_delete:
        for uid in to_delete:
            del pending[uid]
        save_pending(pending)

# ==========================================
# EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"online → {bot.user}")
    bot.add_view(ApprovalView())
    bot.add_view(AlliesView())
    bot.add_view(TicketCloseView())
    bot.add_view(TicketPanelView())
    ensure_default_subdivisions()
    if not cleanup_pending_applications.is_running():
        cleanup_pending_applications.start()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Vanity"))

@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return
    await start_verification(member)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if not message.content and not message.attachments:
        return
    snipe_cache[message.channel.id] = {
        "content": message.content or "",
        "author": str(message.author),
        "avatar": message.author.display_avatar.url,
        "time": discord.utils.utcnow(),
        "attachments": [a.url for a in message.attachments] if message.attachments else []
    }

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ========== SILENCE BOT EASTER EGG ==========
    if message.guild is not None and message.content.strip().lower() == "silence bot" and can_trigger_silence(message.author):
        await handle_silence_trigger(message)
        return

    # ========== DM RELAY (target's DM replies -> forwarded to time4vanity) ==========
    if message.guild is None:
        relay = get_dm_relay()
        uid = str(message.author.id)
        if uid in relay:
            entry = relay[uid]
            source_guild = bot.get_guild(entry.get("guild_id", GUILD_ID))
            target_member = get_relay_target_member(source_guild)

            if target_member:
                embed = discord.Embed(
                    description=message.content or "*no text*",
                    color=EMBED_COLOR,
                    timestamp=discord.utils.utcnow()
                )
                embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
                embed.set_footer(text=f"Relayed DM · $dm {message.author.id} <msg> to reply · $dmclose {message.author.id} to end")
                if message.attachments:
                    embed.set_image(url=message.attachments[0].url)
                    if len(message.attachments) > 1:
                        embed.add_field(
                            name="Extra Attachments",
                            value="\n".join(a.url for a in message.attachments[1:3])
                        )
                try:
                    await target_member.send(embed=embed)
                except Exception:
                    pass
            # don't let a relay target's DM fall through to command processing
            return

    # ========== CHAT-BASED APPLICATION FLOW ==========
    pending = get_pending()
    uid_str = str(message.author.id)

    if uid_str in pending and "step" in pending[uid_str]:
        pdata = pending[uid_str]
        step = pdata.get("step", 0)
        answers = pdata.get("answers", {})
        content = message.content.strip()

        if not content:
            return

        if step == 0:  # IGN
            if len(content) > 16 or " " in content:
                await message.reply("Please enter a valid Minecraft IGN (no spaces, max 16 characters).", delete_after=8)
                return
            answers["ign"] = content

        elif step == 1:  # Wars / Ganks
            answers["wars_ganks"] = content

        elif step == 2:  # Invited by
            if content.lower() == "none":
                answers["invited_by"] = "Not specified"
                answers["recruiter_id"] = None
            else:
                if message.mentions:
                    recruiter = message.mentions[0]
                    answers["recruiter_id"] = recruiter.id
                    answers["invited_by"] = recruiter.mention
                else:
                    try:
                        found = await message.guild.query_members(query=content, limit=1)
                        if found:
                            recruiter = found[0]
                            answers["recruiter_id"] = recruiter.id
                            answers["invited_by"] = recruiter.mention
                        else:
                            answers["invited_by"] = content
                            answers["recruiter_id"] = None
                    except Exception:
                        answers["invited_by"] = content
                        answers["recruiter_id"] = None
        else:
            del pending[uid_str]
            save_pending(pending)
            return

        next_step = step + 1
        if next_step < len(QUESTIONS):
            pdata["step"] = next_step
            pdata["answers"] = answers
            save_pending(pending)
            await message.reply(embed=build_question_embed(next_step))
        else:
            del pending[uid_str]
            save_pending(pending)
            await create_application(message.author, answers)
            await message.reply("✅ Application submitted! Staff will review it shortly.")
        return

    # ========== RECRUITER CLAIMS (+1 / + username) ==========
    if message.channel.id == LOG_CHANNEL_ID:
        content = message.content.strip()
        match = re.match(r'^\+\s*(\d+)?\s*(.*)$', content, re.IGNORECASE)
        if match:
            num_str, rest = match.groups()
            points = int(num_str) if num_str else 1

            if points < 1 or points > 50:
                return

            data = get_points()
            uid = str(message.author.id)
            data["recruiters"][uid] = data["recruiters"].get(uid, 0) + points
            save_points(data)

            embed = discord.Embed(
                description=f"**{message.author.display_name}** claimed `+{points}` recruit(s)\nTotal: `{data['recruiters'][uid]}`",
                color=EMBED_COLOR
            )
            embed.set_footer(text="Vanity · Recruiter Claims")
            try:
                await message.reply(embed=embed, mention_author=False)
            except Exception:
                pass
            return

    # ========== AUTO GUILD INVITE (bare IGN posted in the invite channel) ==========
    if GUILD_INVITE_CHANNEL_ID and message.channel.id == GUILD_INVITE_CHANNEL_ID:
        content = message.content.strip()
        if IGN_REGEX.match(content):
            try:
                lines = await mc_agent_command(f"/g invite {content}", timeout_ms=5000, quiet_ms=1000)
                response_text = " ".join(strip_mc_colors(l) for l in lines).strip()
                lower = response_text.lower()
                failed = bool(re.search(
                    r"(already in a guild|already invited|not online|no such player|does not exist|cannot invite)",
                    lower,
                ))
                await message.add_reaction("❌" if failed else "✅")
                if failed:
                    await message.reply(
                        f"Couldn't invite `{content}`" + (f": {response_text}" if response_text else " (no response from server)."),
                        mention_author=False,
                    )
            except RuntimeError as e:
                try:
                    await message.add_reaction("⚠️")
                    await message.reply(f"Couldn't reach the Minecraft bot: {e}", mention_author=False)
                except Exception:
                    pass
            except Exception:
                try:
                    await message.add_reaction("⚠️")
                except Exception:
                    pass
            return

    # ========== EXECUTIONER STOP ==========
    global executioner_stop, executioner_messages
    if message.content.lower().strip() == "stop" and can_use_executioner(message.author):
        executioner_stop = True
        deleted = 0
        for msg in executioner_messages:
            try:
                await msg.delete()
                deleted += 1
            except: pass
        executioner_messages = []
        await message.channel.send(f"Execution stopped. Yeeted **{deleted}** messages.")

    content = message.content.lower()
    if re.search(r"\bdee\b", content):
        await message.reply("deez nuts 🥜")
    if re.search(r"\byoma\b", content):
        await message.reply("yo mama")

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("Missing permissions.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"Missing required parameter: `{error.param.name}`")
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f"Slow down — try again in **{error.retry_after:.0f}s**.")
    if isinstance(error, commands.BadArgument):
        return await ctx.send(f"Bad argument: {error}")
    print(f"Error: {error}")

# ==========================================
# BOT COMMANDS
# ==========================================
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"`{round(bot.latency * 1000)}ms`")

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="Vanity Bot Commands", description="Prefix: `$` or `!`", color=EMBED_COLOR)
    embed.add_field(name="Recruitment", value="`$testapply` · `$verify @user` · `$reject @user`", inline=False)
    embed.add_field(name="Tickets", value="`$ticket` · `$ticketpanel` · `$tickets` · `$ticketclose`", inline=False)
    embed.add_field(name="Guild Bridge", value="`$g list` · `$g menu` · `$g invite <ign>` — relays to stray.gg in-game", inline=False)
    embed.add_field(name="Bot Identity", value="`$botname <name>` · `$botavatar` (attach image) · `$botreset`", inline=False)
    embed.add_field(name="Alerts", value="`$nuke` (panel) · `$nuke @user [message]` — ghost-pings the target once in every channel the bot can access", inline=False)
    embed.add_field(name="Allies", value="`$allies` (admin / ice only)", inline=False)
    embed.add_field(name="Sub Divisions", value="`$subcreate` · `$subadd` · `$sublb` · `$sublist` · `$subdelete`", inline=False)
    embed.add_field(name="Points", value="`$addrecruiter` · `$addgank` · `$recruiterlb` · `$ganklb` · `$sync`", inline=False)
    embed.add_field(name="Utility", value="`$ping` · `$help` · `$embed` · `$ship` · `$pp` · `$quote` · `$say` · `$snipe` · `$purge`", inline=False)
    embed.add_field(name="Fun", value="`$gay` · `$simp` · `$based` · `$iq` · `$8ball` · `$executioner` · `$roulette`", inline=False)
    embed.add_field(name="Moderation", value="`$mute` · `$unmute` · `$kick` · `$ban` · `$warn` · `$warnings`", inline=False)
    embed.add_field(name="DM Bot", value="`$dmbot` · `$dm @user <message>` · `$dmclose @user`", inline=False)
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)

@bot.command(name="embed")
async def embed_cmd(ctx):
    if not can_use_embed(ctx.author):
        return await ctx.send("Only admins or **ice** can use this.")
    await ctx.send(
        "Click below to open the embed builder:",
        view=EmbedLaunchView(ctx.author)
    )

@bot.command(name="allies")
async def allies_cmd(ctx):
    if not can_use_allies(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can use this.")
    embed = discord.Embed(
        title="Allies Panel",
        description="Select a region below to view current allies.",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity · Allies")
    await ctx.send(embed=embed, view=AlliesView())

# ==========================================
# TICKET COMMANDS
# ==========================================
@bot.command(name="ticket", aliases=["invite", "newticket"])
async def ticket_cmd(ctx):
    if ctx.guild is None:
        return await ctx.send("Use this in the server.")

    error, channel = await open_invite_ticket(ctx.guild, ctx.author)
    if error:
        return await ctx.send(error)

    await ctx.send(f"Ticket opened: {channel.mention}")


@bot.command(name="ticketpanel")
async def ticketpanel_cmd(ctx):
    if not can_manage_tickets(ctx.author):
        return await ctx.send("Only recruiters or admins can post the panel.")

    embed = discord.Embed(
        title="Guild Invite Tickets",
        description="Click below to open a ticket for an invite.",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity · Tickets")
    await ctx.send(embed=embed, view=TicketPanelView())


@bot.command(name="ticketclose")
async def ticketclose_cmd(ctx):
    """Text fallback to close a ticket from inside the ticket channel."""
    entry = find_ticket_by_channel(ctx.channel.id)
    if not entry:
        return await ctx.send("This isn't a ticket channel.")

    if not is_admin(ctx.author):
        return await ctx.send("Admin only.")

    tickets = get_tickets()
    key = str(ctx.channel.id)
    if key in tickets:
        tickets[key]["status"] = "closed"
        tickets[key]["closed_by"] = str(ctx.author)
        tickets[key]["closed_at"] = datetime.now(timezone.utc).isoformat()
        save_tickets(tickets)

    await ctx.send("Closing in 5s...")
    await asyncio.sleep(5)
    try:
        await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")
    except Exception as e:
        await ctx.send(f"Failed to delete channel: {e}")


@bot.command(name="tickets")
async def tickets_cmd(ctx):
    """Recruiters/admins: list all currently open invite tickets."""
    if not can_manage_tickets(ctx.author):
        return await ctx.send("Only recruiters or admins can view the ticket list.")

    tickets = get_tickets()
    open_tickets = {cid: e for cid, e in tickets.items() if e.get("status") == "open"}
    if not open_tickets:
        return await ctx.send("No open tickets.")

    lines = []
    for channel_id, entry in open_tickets.items():
        channel = ctx.guild.get_channel(int(channel_id))
        if not channel:
            continue
        lines.append(f"• {channel.mention} — opened by **{entry.get('author_name', 'unknown')}**")

    if not lines:
        return await ctx.send("No open tickets.")

    embed = discord.Embed(title="Open Invite Tickets", description="\n".join(lines), color=EMBED_COLOR)
    embed.set_footer(text="Vanity · Tickets")
    await ctx.send(embed=embed)

# ==========================================
# MINECRAFT GUILD BRIDGE ($g list / $g menu / $g invite)
# ==========================================
def build_mc_lines_embeds(title: str, lines: list) -> list:
    cleaned = [strip_mc_colors(l) for l in lines if strip_mc_colors(l).strip()]
    body = "\n".join(cleaned) if cleaned else "No response received from the server."
    chunks = [body[i:i + 3900] for i in range(0, len(body), 3900)] or [""]

    embeds = []
    for i, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=title if i == 0 else f"{title} (cont.)",
            description=f"```\n{chunk}\n```",
            color=EMBED_COLOR,
        )
        embed.set_footer(text="stray.gg · Guild Bridge")
        embeds.append(embed)
    return embeds


@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    await ctx.send("Usage: `$g list`, `$g menu`, or `$g invite <ign>`.")


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_agent_command("/g list")
        except RuntimeError as e:
            return await ctx.send(f"Couldn't reach the Minecraft bot: {e}")
    for embed in build_mc_lines_embeds("Guild List", lines):
        await ctx.send(embed=embed)


@g_group.command(name="menu")
async def g_menu(ctx):
    async with ctx.typing():
        try:
            lines = await mc_agent_command("/g menu")
        except RuntimeError as e:
            return await ctx.send(f"Couldn't reach the Minecraft bot: {e}")
    for embed in build_mc_lines_embeds("Guild Menu", lines):
        await ctx.send(embed=embed)


@g_group.command(name="invite")
async def g_invite(ctx, ign: str):
    ign = ign.strip()
    if not IGN_REGEX.match(ign):
        return await ctx.send("That doesn't look like a valid Minecraft IGN.")

    async with ctx.typing():
        try:
            lines = await mc_agent_command(f"/g invite {ign}", timeout_ms=5000, quiet_ms=1000)
        except RuntimeError as e:
            return await ctx.send(f"Couldn't reach the Minecraft bot: {e}")

    response_text = "\n".join(strip_mc_colors(l) for l in lines).strip()
    embed = discord.Embed(
        description=f"Sent `/g invite {ign}` in-game." + (f"\n```\n{response_text[:1000]}\n```" if response_text else ""),
        color=EMBED_COLOR,
    )
    embed.set_footer(text="stray.gg · Guild Bridge")
    await ctx.send(embed=embed)


# ==========================================
# PER-SERVER BOT IDENTITY (nick + guild avatar)
# ==========================================
@bot.command(name="botname")
async def botname_cmd(ctx, *, name: str):
    if not can_customize_bot(ctx.author):
        return await ctx.send("You can't use this.")

    name = name.strip()
    if not name or len(name) > 32:
        return await ctx.send("Name must be 1-32 characters.")

    try:
        await ctx.guild.me.edit(nick=name)
    except Exception as e:
        return await ctx.send(f"Failed to change name: {e}")

    await ctx.send(embed=discord.Embed(description=f"Bot's name in **this server** is now **{name}**.", color=EMBED_COLOR))


@bot.command(name="botavatar")
async def botavatar_cmd(ctx):
    if not can_customize_bot(ctx.author):
        return await ctx.send("You can't use this.")

    if not ctx.message.attachments:
        return await ctx.send("Attach an image with the command.")

    attachment = ctx.message.attachments[0]
    if attachment.size > 8 * 1024 * 1024:
        return await ctx.send("Image too large (max 8MB).")

    try:
        image_bytes = await attachment.read()
        await ctx.guild.me.edit(avatar=image_bytes)
    except discord.HTTPException as e:
        return await ctx.send(f"Discord rejected the image: {e}")
    except TypeError:
        return await ctx.send("Per-server avatars aren't supported by the installed discord.py version — update discord.py to use this.")
    except Exception as e:
        return await ctx.send(f"Failed to change avatar: {e}")

    await ctx.send(embed=discord.Embed(description="Bot's avatar in **this server** has been updated.", color=EMBED_COLOR))


@bot.command(name="botreset")
async def botreset_cmd(ctx):
    if not can_customize_bot(ctx.author):
        return await ctx.send("You can't use this.")

    try:
        await ctx.guild.me.edit(nick=None, avatar=None)
    except Exception as e:
        return await ctx.send(f"Failed to reset: {e}")

    await ctx.send(embed=discord.Embed(description="Bot's name and avatar reset to default in **this server**.", color=EMBED_COLOR))

# ==========================================
# NUKE (pings target once in every sendable channel)
# ==========================================
@bot.command(name="nuke")
@commands.cooldown(1, 60, commands.BucketType.guild)
async def nuke_cmd(ctx, member: discord.Member = None, *, message: str = None):
    if not can_nuke(ctx.author):
        return await ctx.send("Only admins or **q3xv** can use this.")

    if member is None:
        embed = discord.Embed(
            title="Nuke Panel",
            description="Select a target → EXECUTE\nGhost-pings them **once** in every channel the bot can access (send + delete).\n*Admins / q3xv only*",
            color=0xFF0000,
        )
        return await ctx.send(embed=embed, view=NukeView(ctx.author))

    if member.bot:
        return await ctx.send("Can't nuke a bot.")

    text = f"{member.mention} {message}" if message else f"{member.mention} 💥"
    status = await ctx.send(f"Nuking **{member.display_name}** across all channels...")
    await run_nuke(ctx.guild, member, text, status_channel=ctx.channel)
    try:
        await status.delete()
    except Exception:
        pass

# ==========================================
# DM RELAY (proxy DM to a user, relay their replies to time4vanity)
# ==========================================
@bot.command(name="dm")
async def dm_relay_cmd(ctx, member: discord.Member, *, message: str):
    if not can_use_dm_relay(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can use this.")

    if member.bot:
        return await ctx.send("Can't DM a bot.")

    try:
        await member.send(message)
    except discord.Forbidden:
        return await ctx.send(f"Couldn't DM {member.mention} — their DMs are closed.")
    except Exception as e:
        return await ctx.send(f"Failed to send DM: {e}")

    relay = get_dm_relay()
    relay[str(member.id)] = {
        "guild_id": ctx.guild.id,
        "opened_by": ctx.author.id,
        "opened_at": datetime.now(timezone.utc).isoformat()
    }
    save_dm_relay(relay)

    embed = discord.Embed(
        description=(
            f"📨 Sent DM to **{member}**.\n"
            f"Any replies they send will now be relayed straight to **time4vanity**'s DMs.\n"
            f"Use `$dmclose {member.id}` to stop relaying."
        ),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity · DM Relay")
    await ctx.send(embed=embed)


@bot.command(name="dmclose")
async def dm_relay_close(ctx, member: discord.Member):
    if not can_use_dm_relay(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can use this.")

    relay = get_dm_relay()
    uid = str(member.id)
    if uid not in relay:
        return await ctx.send(f"No active DM relay for {member.mention}.")

    del relay[uid]
    save_dm_relay(relay)
    await ctx.send(embed=discord.Embed(description=f"Closed DM relay for **{member}**.", color=EMBED_COLOR))


@bot.command(name="dmlist")
async def dm_relay_list(ctx):
    if not can_use_dm_relay(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can use this.")

    relay = get_dm_relay()
    if not relay:
        return await ctx.send("No active DM relays.")

    lines = []
    for uid, entry in relay.items():
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ({uid})"
        lines.append(f"• **{name}** — opened <t:{int(datetime.fromisoformat(entry['opened_at']).timestamp())}:R>")

    embed = discord.Embed(title="Active DM Relays", description="\n".join(lines), color=EMBED_COLOR)
    embed.set_footer(text="Vanity · DM Relay")
    await ctx.send(embed=embed)

# ==========================================
# SUB DIVISIONS
# ==========================================
@bot.command(name="subcreate")
async def subcreate(ctx, *, name: str):
    if not can_manage_subdiv(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can create sub-divisions.")

    name = name.strip()
    if not name or len(name) > 40:
        return await ctx.send("Name must be between 1 and 40 characters.")

    data = get_subdivisions()
    key = name.lower()

    if key in data:
        return await ctx.send(f"**{name}** already exists.")

    data[key] = {
        "name": name,
        "points": 0,
        "created_by": str(ctx.author),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_subdivisions(data)

    embed = discord.Embed(
        description=f"Created **{name}**\nPoints: `0`",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="subadd")
async def subadd(ctx, name: str, points: int):
    if not can_manage_subdiv(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can manage points.")

    data = get_subdivisions()
    key = name.lower()

    if key not in data:
        return await ctx.send(f"No sub-division called **{name}**.")

    data[key]["points"] = data[key].get("points", 0) + points
    save_subdivisions(data)

    action = "added" if points >= 0 else "removed"
    embed = discord.Embed(
        description=f"**{data[key]['name']}** — {action} `{abs(points)}` pts\nTotal: `{data[key]['points']}`",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="sublb", aliases=["subleaderboard"])
async def sublb(ctx):
    data = get_subdivisions()
    if not data:
        return await ctx.send("No sub-divisions yet.")

    sorted_subs = sorted(data.items(), key=lambda x: x[1].get("points", 0), reverse=True)

    lines = []
    for i, (key, sub) in enumerate(sorted_subs, 1):
        name = sub.get("name", key)
        pts = sub.get("points", 0)
        lines.append(f"**{i}.** {name} — `{pts}`")

    embed = discord.Embed(
        title="Sub Division Leaderboard",
        description="\n".join(lines),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="sublist")
async def sublist(ctx):
    data = get_subdivisions()
    if not data:
        return await ctx.send("No sub-divisions yet.")

    lines = []
    for key, sub in data.items():
        name = sub.get("name", key)
        emoji = sub.get("emoji")
        pts = sub.get("points", 0)
        prefix = f"{emoji} " if emoji else ""
        lines.append(f"• {prefix}**{name}** — `{pts}` pts")

    embed = discord.Embed(
        title="Sub Divisions",
        description="\n".join(lines),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="subdelete")
async def subdelete(ctx, *, name: str):
    if not can_manage_subdiv(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can delete sub-divisions.")

    data = get_subdivisions()
    key = name.lower()

    if key not in data:
        return await ctx.send(f"No sub-division called **{name}**.")

    real_name = data[key].get("name", name)
    del data[key]
    save_subdivisions(data)

    embed = discord.Embed(
        description=f"Deleted **{real_name}**",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)

# ==========================================
# RECRUITER & GANK POINTS
# ==========================================
@bot.command(name="addrecruiter")
async def addrecruiter(ctx, member: discord.Member, points: int = 1):
    if not can_manage_points(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can add points.")

    if member.bot:
        return await ctx.send("Can't add points to bots.")

    data = get_points()
    uid = str(member.id)
    data["recruiters"][uid] = data["recruiters"].get(uid, 0) + points
    save_points(data)

    action = "added" if points >= 0 else "removed"
    embed = discord.Embed(
        description=f"**{member.display_name}** — {action} `{abs(points)}` recruiter pts\nTotal: `{data['recruiters'][uid]}`",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="addgank")
async def addgank(ctx, member: discord.Member, points: int = 1):
    if not can_manage_points(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can add points.")

    if member.bot:
        return await ctx.send("Can't add points to bots.")

    data = get_points()
    uid = str(member.id)
    data["ganks"][uid] = data["ganks"].get(uid, 0) + points
    save_points(data)

    action = "added" if points >= 0 else "removed"
    embed = discord.Embed(
        description=f"**{member.display_name}** — {action} `{abs(points)}` gank pts\nTotal: `{data['ganks'][uid]}`",
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="recruiterlb", aliases=["rlb"])
async def recruiterlb(ctx):
    data = get_points()
    recruiters = data.get("recruiters", {})
    if not recruiters:
        return await ctx.send("No recruiter points yet.")

    sorted_users = sorted(recruiters.items(), key=lambda x: x[1], reverse=True)[:15]

    lines = []
    for i, (uid, pts) in enumerate(sorted_users, 1):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ({uid})"
        lines.append(f"**{i}.** {name} — `{pts}`")

    embed = discord.Embed(
        title="Recruiter Leaderboard",
        description="\n".join(lines),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="ganklb", aliases=["glb"])
async def ganklb(ctx):
    data = get_points()
    ganks = data.get("ganks", {})
    if not ganks:
        return await ctx.send("No gank points yet.")

    sorted_users = sorted(ganks.items(), key=lambda x: x[1], reverse=True)[:15]

    lines = []
    for i, (uid, pts) in enumerate(sorted_users, 1):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ({uid})"
        lines.append(f"**{i}.** {name} — `{pts}`")

    embed = discord.Embed(
        title="Gank Leaderboard",
        description="\n".join(lines),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Vanity")
    await ctx.send(embed=embed)


@bot.command(name="sync")
async def sync_cmd(ctx):
    if not can_use_sync(ctx.author):
        return await ctx.send("Only admins or **14k14k14k** can sync the recruiter leaderboard.")

    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return await ctx.send("Couldn't find the recruiter invites channel.")

    status_msg = await ctx.send(f"🔄 Scanning {channel.mention} for `+` claims...")

    counts = {}
    scanned = 0
    matched = 0

    try:
        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1
            if message.author.bot:
                continue

            content = message.content.strip()
            match = re.match(r'^\+\s*(\d+)?\s*(.*)$', content, re.IGNORECASE)
            if not match:
                continue

            num_str, _ = match.groups()
            points = int(num_str) if num_str else 1
            if points < 1:
                continue

            uid = str(message.author.id)
            counts[uid] = counts.get(uid, 0) + points
            matched += 1
    except discord.Forbidden:
        return await status_msg.edit(content="I don't have permission to read that channel's history.")
    except Exception as e:
        return await status_msg.edit(content=f"Sync failed: `{e}`")

    data = get_points()
    data["recruiters"] = counts
    save_points(data)

    lines = []
    sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:15]
    for i, (uid, pts) in enumerate(sorted_users, 1):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ({uid})"
        lines.append(f"**{i}.** {name} — `{pts}`")

    embed = discord.Embed(
        title="Recruiter Leaderboard · Synced",
        description="\n".join(lines) if lines else "No `+` claims found in the channel.",
        color=EMBED_COLOR
    )
    embed.set_footer(text=f"Vanity · Sync — {scanned} messages scanned, {matched} claims counted")
    await status_msg.edit(content=None, embed=embed)


@bot.command(name="testapply")
async def testapply(ctx, member: discord.Member = None):
    if not can_use_testapply(ctx.author):
        return await ctx.send("You can't use this.")
    target = member or ctx.author
    await start_verification(target)
    if ctx.channel.id != VERIFY_CHANNEL_ID:
        await ctx.send(f"Started verification for {target.mention} in <#{VERIFY_CHANNEL_ID}>.")

@bot.command(name="verify")
async def verify_cmd(ctx, member: discord.Member):
    if not can_resolve_applications(ctx.author):
        return await ctx.send("You can't use this.")

    key, record = find_pending_application(member.id)
    if not record:
        record = {
            "ign": member.display_name,
            "applicant_id": member.id,
            "applicant_name": str(member),
            "guild_id": ctx.guild.id,
            "invited_by": None,
            "wars_ganks": None,
            "recruiter_id": None
        }

    await finalize_application(ctx.guild, member, record, True)

    record["status"] = "approved"
    if key:
        apps = load_json(APPLICATIONS_FILE, {})
        apps[key] = record
        save_json(APPLICATIONS_FILE, apps)

    await send_acceptance_dm(member)
    # NO log_event → no message in recruiter channel
    await ctx.send(embed=discord.Embed(description=f"**{member.mention}** verified — roles + nickname applied.", color=EMBED_COLOR))

@bot.command(name="reject")
async def reject_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not can_resolve_applications(ctx.author):
        return await ctx.send("You can't use this.")

    key, record = find_pending_application(member.id)
    if not record:
        return await ctx.send(f"No pending application found for {member.mention}.")

    await finalize_application(ctx.guild, member, record, False)

    record["status"] = "rejected"
    apps = load_json(APPLICATIONS_FILE, {})
    apps[key] = record
    save_json(APPLICATIONS_FILE, apps)

    try:
        await member.send(f"Your application was **rejected**.\nReason: {reason}")
    except Exception:
        pass

    # NO log_event → no message in recruiter channel
    await ctx.send(embed=discord.Embed(description=f"**{member.mention}**'s application rejected.", color=discord.Color.red()))

@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send("Nothing to snipe.")
    embed = discord.Embed(description=data["content"] or "*no text*", color=EMBED_COLOR, timestamp=data["time"])
    embed.set_author(name=data["author"], icon_url=data["avatar"])
    embed.set_footer(text="Sniped")
    if data["attachments"]:
        embed.set_image(url=data["attachments"][0])
        if len(data["attachments"]) > 1:
            embed.add_field(name="Extra Attachments", value="\n".join(data["attachments"][1:3]))
    await ctx.send(embed=embed)

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send("Specify between 1 and 100 messages.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Yeeted **{len(deleted)-1}** messages.")
    await asyncio.sleep(2)
    await msg.delete()

@bot.command(name="say")
async def say(ctx, *, message: str):
    if not can_use_say(ctx.author):
        return await ctx.send("Admin only.")
    try: await ctx.message.delete()
    except: pass
    await ctx.send(message)

@bot.command(name="warn")
async def warn(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not can_warn(ctx.author):
        return await ctx.send("You can't use this.")
    data = get_warnings()
    uid = str(member.id)
    if uid not in data: data[uid] = []
    data[uid].append({"mod": str(ctx.author), "reason": reason, "time": datetime.now(timezone.utc).isoformat()})
    save_warnings(data)
    embed = discord.Embed(description=f"**{member.mention}** warned.\nReason: {reason}\nTotal: **{len(data[uid])}**", color=EMBED_COLOR)
    await ctx.send(embed=embed)
    try: await member.send(f"Warned in **{ctx.guild.name}**\nReason: {reason}")
    except: pass

@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member):
    data = get_warnings()
    warns = data.get(str(member.id), [])
    if not warns:
        return await ctx.send(f"**{member.display_name}** has no warnings.")
    lines = [f"**{i}.** {w['reason']} — `{w['mod']}`" for i, w in enumerate(warns, 1)]
    embed = discord.Embed(title=f"Warnings · {member.display_name}", description="\n".join(lines), color=EMBED_COLOR)
    await ctx.send(embed=embed)

@bot.command(name="dmbot")
async def dmbot(ctx):
    if not is_admin(ctx.author):
        return await ctx.send("Admin only.")
    embed = discord.Embed(title="Vanity · DM Bot", description="Mass DM panel (admin only)", color=EMBED_COLOR)
    await ctx.send(embed=embed, view=DMBotPanel(ctx.author))

@bot.command(name="executioner", aliases=["brutal", "execute"])
async def executioner(ctx):
    if not can_use_executioner(ctx.author):
        return await ctx.send("only ice can access this")
    embed = discord.Embed(title="Brutal Executioner 69", description="Select victim + message → EXECUTE\nType `stop` to end + yeet all messages.", color=0xFF0000)
    await ctx.send(embed=embed, view=ExecutionerView(ctx.author))

@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    chamber, bullet = random.randint(1, 6), random.randint(1, 6)

    if chamber == bullet:
        embed = discord.Embed(color=discord.Color.red())
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="Lost roulette")
            embed.description = f"🔫 **BANG.** {ctx.author.mention} caught one — muted for **2 minutes**. 💀"
        except Exception:
            embed.description = f"🔫 **BANG.** {ctx.author.mention} would've eaten it, but I can't timeout them."
    else:
        embed = discord.Embed(color=EMBED_COLOR)
        embed.description = f"*click.* {ctx.author.mention} survives — chamber **{chamber}/6**. 🍀"

    embed.set_footer(text="$roulette · pull the trigger again in 20s")
    await ctx.send(embed=embed)

@bot.command(name="quote")
async def quote(ctx, *, text: str = None):
    if not PIL_AVAILABLE:
        return await ctx.send("Pillow module missing on host.")
    if ctx.message.reference and ctx.message.reference.resolved:
        ref = ctx.message.reference.resolved
        if isinstance(ref, discord.Message) and ref.content:
            text = ref.content
            author = ref.author.display_name
        else:
            author = ctx.author.display_name
    else:
        author = ctx.author.display_name
    if not text:
        return await ctx.send("Provide text or reply to a message.")
    text = text.strip()[:280]
    buffer = create_quote_image(text, author)
    await ctx.send(file=discord.File(buffer, "quote.png"))

def create_quote_image(text: str, author: str) -> io.BytesIO:
    width, height = 1280, 720
    img = Image.new("RGB", (width, height), (9, 9, 11))
    draw = ImageDraw.Draw(img)

    def load_font(size, bold=False):
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
            "arialbd.ttf" if bold else "arial.ttf",
        ]
        for path in paths:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_quote = load_font(56, bold=True)
    font_author = load_font(27)
    font_brand = load_font(21, bold=True)

    max_width = 600
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font_quote)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    lines = lines[:5]

    line_height = 74
    total_h = len(lines) * line_height
    start_y = (height - total_h) // 2 - 50
    right_pad = 95
    y = start_y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font_quote)
        tw = bbox[2] - bbox[0]
        x = width - right_pad - tw
        draw.text((x + 3, y + 3), line, font=font_quote, fill=(0, 0, 0))
        draw.text((x, y), line, font=font_quote, fill=(250, 250, 250))
        y += line_height

    author_text = f"— {author}"
    bbox = draw.textbbox((0, 0), author_text, font=font_author)
    ax = width - right_pad - (bbox[2] - bbox[0])
    draw.text((ax, y + 24), author_text, font=font_author, fill=(155, 155, 155))
    draw.line([(width - right_pad - 160, y + 68), (width - right_pad, y + 68)], fill=(87, 242, 135), width=2)
    draw.rectangle([0, 0, 7, height], fill=(87, 242, 135))
    brand = "VANITY"
    bbox = draw.textbbox((0, 0), brand, font=font_brand)
    draw.text((width - bbox[2] - 48, height - 52), brand, font=font_brand, fill=(87, 242, 135))

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

@bot.command(name="pp")
async def pp(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id)
    size = random.randint(0, 15)
    visual = f"8{'=' * size}D"
    comment = ["micro", "smol", "average", "respectable", "dangerous"][min(size // 3, 4)]
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s pp", icon_url=target.display_avatar.url)
    embed.description = f"`{visual}`\n**{size} inches** · {comment}"
    await ctx.send(embed=embed)

@bot.command(name="ship")
async def ship(ctx, user1: discord.Member, user2: discord.Member = None):
    if user2 is None: user2 = ctx.author
    if user1 == user2: return await ctx.send("Can't ship yourself.")
    random.seed((user1.id + user2.id) % 100)
    percent = random.randint(0, 100)
    comment = ["doomed", "rough", "maybe", "strong", "destined"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.description = f"**{user1.display_name}** × **{user2.display_name}**\n`{bar}` **{percent}%** · {comment}"
    await ctx.send(embed=embed)

@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 69)
    percent = random.randint(0, 100)
    comment = ["straight as an arrow", "slightly fruity", "bit sus", "pretty gay", "certified fruity"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s Gay Meter", icon_url=target.display_avatar.url)
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
    await ctx.send(embed=embed)

@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 420)
    percent = random.randint(0, 100)
    comment = ["no simp detected", "mild", "occasional", "heavy", "professional simp"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s Simp Meter", icon_url=target.display_avatar.url)
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
    await ctx.send(embed=embed)

@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 1337)
    percent = random.randint(0, 100)
    comment = ["terminally cringe", "slightly cringe", "mid", "based", "extremely based"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s Based Meter", icon_url=target.display_avatar.url)
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
    await ctx.send(embed=embed)

@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 999)
    score = random.randint(40, 160)
    comment = "smooth brain" if score < 80 else "a bit slow" if score < 100 else "average" if score < 120 else "very smart" if score < 140 else "genius"
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s IQ", icon_url=target.display_avatar.url)
    embed.description = f"**{score} IQ**\n{comment}"
    await ctx.send(embed=embed)

@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send("Ask something.")
    answers = ["Yes.", "No.", "Maybe.", "Definitely.", "Absolutely not.", "Ask again later.", "Very doubtful.", "Without a doubt.", "Signs point to yes.", "Don't count on it."]
    embed = discord.Embed(color=EMBED_COLOR)
    embed.add_field(name="Question", value=question)
    embed.add_field(name="Answer", value=f"**{random.choice(answers)}**")
    await ctx.send(embed=embed)

# ==========================================
# MODERATION COMMANDS
# ==========================================
def parse_duration(arg: str) -> Optional[timedelta]:
    arg = arg.lower().strip()
    try:
        if arg.endswith("s"): return timedelta(seconds=int(arg[:-1]))
        if arg.endswith("m"): return timedelta(minutes=int(arg[:-1]))
        if arg.endswith("h"): return timedelta(hours=int(arg[:-1]))
        if arg.endswith("d"): return timedelta(days=int(arg[:-1]))
        return timedelta(minutes=int(arg))
    except: return None

@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if not can_mute(ctx.author):
        return await ctx.send("You can't use this.")
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner and not is_admin(ctx.author):
        return await ctx.send("Cannot action user with higher/equal role.")
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send("Invalid duration format (max 28d).")
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=discord.Embed(description=f"**{member.mention}** muted · `{duration}`\n{reason}", color=EMBED_COLOR))
    except Exception as e:
        await ctx.send(f"Failed to mute user: {e}")

@bot.command(name="unmute")
async def unmute(ctx, member: discord.Member):
    if not can_mute(ctx.author):
        return await ctx.send("You can't use this.")
    try:
        await member.timeout(None)
        await ctx.send(f"**{member.mention}** unmuted.")
    except Exception as e:
        await ctx.send(f"Failed to unmute user: {e}")

@bot.command(name="kick")
@commands.has_permissions(administrator=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("Cannot action user with higher/equal role.")
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(f"**{member}** kicked.\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed to kick user: {e}")

@bot.command(name="ban")
@commands.has_permissions(administrator=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("Cannot action user with higher/equal role.")
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(f"**{member}** banned.\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed to ban user: {e}")

# ==========================================
# BOT EXECUTION
# ==========================================
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        print("CRITICAL: BOT_TOKEN is missing in environment variables.")
        return
    async with bot:
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
