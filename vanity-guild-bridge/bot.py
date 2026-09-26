import discord
from discord.ext import commands
from discord.ext.commands import CommandNotFound
import os
import asyncio
import random
import re
import time
import aiohttp
from datetime import timedelta
from typing import Optional

# ==========================================
# BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.moderation = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("?"),
    case_insensitive=True,
    intents=intents,
)
bot.remove_command("help")

# ==========================================
# CONFIG
# ==========================================
EMBED_COLOR = 0x57F287
EMBED_DARK = 0x0B0B0F
EMBED_ACCENT = 0x57F287
EMBED_ERROR = 0xFF4D6D
EMBED_INFO = 0x5B8CFF

SAY_ALLOWED = {"time4vanity"}
MUTE_ALLOWED = {"corp1637"}


def _clean_env(name: str, default: str = "") -> str:
    val = os.getenv(name, default) or default
    val = val.strip().strip('"').strip("'")
    val = "".join(ch for ch in val if ord(ch) >= 32 or ch == "\t")
    return val


MC_AGENT_URL = _clean_env("MC_AGENT_URL").rstrip("/")
MC_AGENT_SECRET = _clean_env("MC_AGENT_SECRET")
# Discord channel where chat is bridged both ways (must be a CHANNEL ID, not an invite)
GUILD_BRIDGE_CHANNEL_ID = int(_clean_env("GUILD_BRIDGE_CHANNEL_ID", "0") or 0)
# Optional: still support bare-IGN auto invite in a separate channel
GUILD_INVITE_CHANNEL_ID = int(_clean_env("GUILD_INVITE_CHANNEL_ID", "0") or 0)
# In-game guild chat command prefix, e.g. "/gc " or "/g chat "
MC_GUILD_CHAT_PREFIX = _clean_env("MC_GUILD_CHAT_PREFIX", "/gc ")
if MC_GUILD_CHAT_PREFIX and not MC_GUILD_CHAT_PREFIX.endswith(" "):
    MC_GUILD_CHAT_PREFIX += " "

BRIDGE_COOLDOWN_SECONDS = 2.0
_bridge_last_send: dict[int, float] = {}

MC_COLOR_CODE_RE = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_REGEX = re.compile(r"^[A-Za-z0-9_]{2,16}$")
# Common guild-chat line shapes from practice/factions servers
GUILD_CHAT_PATTERNS = [
    re.compile(r"^\[(?:Guild|G|GC)\]\s*(?:\[.*?\]\s*)?([A-Za-z0-9_]{2,16})\s*[:»>\-]\s*(.+)$", re.I),
    re.compile(r"^(?:Guild|GC)\s*[>»|]\s*([A-Za-z0-9_]{2,16})\s*[:»>\-]\s*(.+)$", re.I),
    re.compile(r"^([A-Za-z0-9_]{2,16})\s*(?:\[G\]|\[Guild\])\s*[:»>\-]\s*(.+)$", re.I),
]

snipe_cache = {}


# ==========================================
# EMBED DESIGN SYSTEM
# ==========================================
def vanity_embed(
    *,
    title: str = None,
    description: str = None,
    color: int = EMBED_COLOR,
    footer: str = "Vanity · Guild Bridge",
    thumbnail: str = None,
    image: str = None,
    author_name: str = None,
    author_icon: str = None,
) -> discord.Embed:
    embed = discord.Embed(color=color, timestamp=discord.utils.utcnow())
    if title:
        embed.title = title
    if description:
        embed.description = description
    if footer:
        embed.set_footer(text=footer, icon_url=bot.user.display_avatar.url if bot.user else None)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon)
    return embed


def error_embed(message: str) -> discord.Embed:
    return vanity_embed(
        title="Something went wrong",
        description=f"```\n{message}\n```",
        color=EMBED_ERROR,
        footer="Vanity · Error",
    )


def success_embed(message: str, title: str = "Done") -> discord.Embed:
    return vanity_embed(title=title, description=message, color=EMBED_COLOR)


# ==========================================
# HELPERS
# ==========================================
def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def can_use_say(member: discord.Member) -> bool:
    return (
        is_admin(member)
        or member.name.lower() in SAY_ALLOWED
        or member.display_name.lower() in SAY_ALLOWED
    )


def can_mute(member: discord.Member) -> bool:
    return (
        is_admin(member)
        or member.name.lower() in MUTE_ALLOWED
        or member.display_name.lower() in MUTE_ALLOWED
    )


def strip_mc_colors(text: str) -> str:
    return MC_COLOR_CODE_RE.sub("", text or "")


async def mc_agent_command(command: str, timeout_ms: int = 8000, quiet_ms: int = 1200):
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")

    url = f"{MC_AGENT_URL}/command"
    secret = "".join(ch for ch in MC_AGENT_SECRET if ord(ch) >= 32)
    headers = {"x-agent-secret": secret, "Content-Type": "application/json"}
    payload = {"command": command, "timeoutMs": timeout_ms, "quietMs": quiet_ms}

    try:
        timeout = aiohttp.ClientTimeout(total=(timeout_ms / 1000) + 5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(
                        data.get("error", f"mc-agent returned {resp.status}")
                        if isinstance(data, dict)
                        else f"mc-agent returned {resp.status}"
                    )
                return data.get("lines", []) if isinstance(data, dict) else []
    except asyncio.TimeoutError:
        raise RuntimeError("Timed out waiting for mc-agent.")
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Couldn't reach mc-agent: {e}")
    except Exception as e:
        raise RuntimeError(str(e))


async def mc_agent_chat(text: str):
    """Send a chat line in-game without waiting for a long reply collection."""
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")

    url = f"{MC_AGENT_URL}/chat"
    secret = "".join(ch for ch in MC_AGENT_SECRET if ord(ch) >= 32)
    headers = {"x-agent-secret": secret, "Content-Type": "application/json"}
    payload = {"message": text}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(
                        data.get("error", f"mc-agent returned {resp.status}")
                        if isinstance(data, dict)
                        else f"mc-agent returned {resp.status}"
                    )
                return data
    except asyncio.TimeoutError:
        raise RuntimeError("Timed out sending chat to mc-agent.")
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Couldn't reach mc-agent: {e}")
    except Exception as e:
        raise RuntimeError(str(e))


def build_mc_lines_embeds(title: str, lines: list) -> list:
    cleaned = [strip_mc_colors(l) for l in lines if strip_mc_colors(l).strip()]
    body = "\n".join(cleaned) if cleaned else "No response received from the server."
    chunks = [body[i : i + 3800] for i in range(0, len(body), 3800)] or [""]

    embeds = []
    for i, chunk in enumerate(chunks):
        embed = vanity_embed(
            title=title if i == 0 else f"{title} · continued",
            description=f"```ansi\n\u001b[0;32m{chunk}\u001b[0m\n```",
            color=EMBED_COLOR,
            footer="Vanity · Live from stray.gg",
        )
        embeds.append(embed)
    return embeds


def parse_duration(arg: str) -> Optional[timedelta]:
    arg = arg.lower().strip()
    try:
        if arg.endswith("s"):
            return timedelta(seconds=int(arg[:-1]))
        if arg.endswith("m"):
            return timedelta(minutes=int(arg[:-1]))
        if arg.endswith("h"):
            return timedelta(hours=int(arg[:-1]))
        if arg.endswith("d"):
            return timedelta(days=int(arg[:-1]))
        return timedelta(minutes=int(arg))
    except Exception:
        return None


def bridge_on_cooldown(user_id: int) -> float:
    """Return remaining cooldown seconds, or 0 if ready."""
    last = _bridge_last_send.get(user_id, 0.0)
    remaining = BRIDGE_COOLDOWN_SECONDS - (time.monotonic() - last)
    return max(0.0, remaining)


def mark_bridge_sent(user_id: int):
    _bridge_last_send[user_id] = time.monotonic()


# ==========================================
# EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"MC_AGENT_URL set: {bool(MC_AGENT_URL)} | secret set: {bool(MC_AGENT_SECRET)}")
    print(f"Guild bridge channel: {GUILD_BRIDGE_CHANNEL_ID or 'NOT SET'}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="guild chat · ?help")
    )


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    snipe_cache[message.channel.id] = {
        "content": message.content,
        "author": str(message.author),
        "avatar": message.author.display_avatar.url,
        "time": discord.utils.utcnow(),
        "attachments": [a.url for a in message.attachments],
    }


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ---- Guild chat bridge: Discord → Minecraft ----
    if (
        GUILD_BRIDGE_CHANNEL_ID
        and message.guild
        and message.channel.id == GUILD_BRIDGE_CHANNEL_ID
    ):
        content = (message.content or "").strip()
        # Ignore bot commands in the bridge channel
        if content.startswith("?") or content.startswith(f"<@{bot.user.id}>") or content.startswith(f"<@!{bot.user.id}>"):
            await bot.process_commands(message)
            return

        if not content:
            return

        remaining = bridge_on_cooldown(message.author.id)
        if remaining > 0:
            warn = await message.channel.send(
                embed=vanity_embed(
                    title="Cooldown",
                    description=f"{message.author.mention} wait **{remaining:.1f}s** before sending again.",
                    color=EMBED_INFO,
                    footer="Vanity · 2s bridge cooldown",
                )
            )
            await asyncio.sleep(2)
            try:
                await warn.delete()
            except Exception:
                pass
            return

        # Cap length so we don't blow up in-game chat
        if len(content) > 200:
            content = content[:200]

        in_game = f"{MC_GUILD_CHAT_PREFIX}{content}"
        mark_bridge_sent(message.author.id)
        try:
            await mc_agent_chat(in_game)
            try:
                await message.add_reaction("<:check:0>")
            except Exception:
                try:
                    await message.add_reaction("✅")
                except Exception:
                    pass
        except Exception as e:
            await message.channel.send(
                embed=error_embed(str(e)),
                delete_after=8,
            )
        return

    # ---- Bare IGN auto-invite channel ----
    if (
        GUILD_INVITE_CHANNEL_ID
        and message.guild
        and message.channel.id == GUILD_INVITE_CHANNEL_ID
    ):
        content = message.content.strip()
        if IGN_REGEX.match(content):
            try:
                lines = await mc_agent_command(
                    f"/g invite {content}", timeout_ms=5000, quiet_ms=1000
                )
                response_text = "\n".join(strip_mc_colors(l) for l in lines).strip()
                embed = vanity_embed(
                    title="Guild Invite Sent",
                    description=(
                        f"**IGN** `{content}`\n"
                        + (f"```\n{response_text[:900]}\n```" if response_text else "_No server reply._")
                    ),
                    color=EMBED_COLOR,
                )
                await message.channel.send(embed=embed)
            except Exception as e:
                await message.channel.send(embed=error_embed(str(e)))
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            embed=error_embed(f"Missing argument: `{error.param.name}`")
        )
    if isinstance(error, commands.BadArgument):
        return await ctx.send(embed=error_embed("Invalid argument."))
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(
            embed=vanity_embed(
                title="Cooldown",
                description=f"Try again in **{error.retry_after:.0f}s**.",
                color=EMBED_INFO,
            )
        )
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(embed=error_embed("You don't have permission to use this."))
    print(f"Command error in {ctx.command}: {error}")
    try:
        await ctx.send(embed=error_embed(str(error)))
    except Exception:
        pass


# ==========================================
# INBOUND: mc-agent → Discord (guild chat)
# ==========================================
@bot.command(name="bridgehook")
@commands.is_owner()
async def bridgehook(ctx):
    """Owner helper: prints setup notes for the MC→Discord webhook path."""
    await ctx.send(
        embed=vanity_embed(
            title="Guild Bridge Setup",
            description=(
                f"**Bridge channel ID:** `{GUILD_BRIDGE_CHANNEL_ID or 'NOT SET'}`\n"
                f"**Chat prefix in-game:** `{MC_GUILD_CHAT_PREFIX}`\n\n"
                "Set on **Discord bot** service:\n"
                "```\nGUILD_BRIDGE_CHANNEL_ID=<this channel id>\n"
                "MC_AGENT_URL=https://...\nMC_AGENT_SECRET=...\n```\n"
                "Set on **mc-agent** service:\n"
                "```\nDISCORD_BRIDGE_WEBHOOK_URL=<webhook for this channel>\n```\n"
                "Create a webhook in this channel → channel settings → Integrations → Webhooks."
            ),
        )
    )


# ==========================================
# GUILD COMMANDS  (?g list / ?g invite)
# ==========================================
@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    await ctx.send(
        embed=vanity_embed(
            title="Guild Commands",
            description=(
                "**`?g list`** — show online guild members\n"
                "**`?g invite <ign>`** — invite a player in-game\n\n"
                f"Live chat bridge runs in <#{GUILD_BRIDGE_CHANNEL_ID}> "
                if GUILD_BRIDGE_CHANNEL_ID
                else "_Set `GUILD_BRIDGE_CHANNEL_ID` to enable live chat bridge._"
            ),
        )
    )


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_agent_command("/g list")
        except Exception as e:
            return await ctx.send(embed=error_embed(str(e)))
    for embed in build_mc_lines_embeds("Guild List", lines):
        await ctx.send(embed=embed)


@g_group.command(name="invite")
async def g_invite(ctx, ign: str):
    ign = ign.strip()
    if not IGN_REGEX.match(ign):
        return await ctx.send(embed=error_embed("That doesn't look like a valid Minecraft IGN."))

    async with ctx.typing():
        try:
            lines = await mc_agent_command(
                f"/g invite {ign}", timeout_ms=5000, quiet_ms=1000
            )
        except Exception as e:
            return await ctx.send(embed=error_embed(str(e)))

    response_text = "\n".join(strip_mc_colors(l) for l in lines).strip()
    embed = vanity_embed(
        title="Invite dispatched",
        description=(
            f"Sent **`/g invite {ign}`** in-game.\n"
            + (f"```\n{response_text[:1000]}\n```" if response_text else "_No server reply collected._")
        ),
    )
    await ctx.send(embed=embed)


# ==========================================
# UTILITY
# ==========================================
@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(
        embed=vanity_embed(
            title="Pong",
            description=f"Gateway latency **`{latency}ms`**",
            footer="Vanity · Status",
        )
    )


@bot.command(name="help")
async def help_cmd(ctx):
    embed = vanity_embed(
        title="Vanity",
        description="Clean controls. Live guild bridge. Prefix **`?`**",
    )
    embed.add_field(
        name="▸ Guild",
        value="`?g list`\n`?g invite <ign>`",
        inline=True,
    )
    embed.add_field(
        name="▸ Bridge",
        value=(
            f"Chat in <#{GUILD_BRIDGE_CHANNEL_ID}> → in-game guild chat\n"
            f"In-game `/g` chat → this channel"
            if GUILD_BRIDGE_CHANNEL_ID
            else "Set `GUILD_BRIDGE_CHANNEL_ID`"
        ),
        inline=True,
    )
    embed.add_field(
        name="▸ Utility",
        value="`?ping` · `?snipe` · `?purge` · `?say`",
        inline=False,
    )
    embed.add_field(
        name="▸ Fun",
        value="`?pp` · `?ship` · `?gay` · `?simp` · `?based` · `?iq` · `?8ball` · `?roulette`",
        inline=False,
    )
    embed.add_field(
        name="▸ Mod",
        value="`?mute` · `?unmute` · `?kick` · `?ban`",
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=vanity_embed(title="Snipe", description="_Nothing to snipe._", color=EMBED_INFO))
    embed = vanity_embed(
        description=data["content"] or "_no text_",
        footer="Vanity · Sniped",
    )
    embed.set_author(name=data["author"], icon_url=data["avatar"])
    embed.timestamp = data["time"]
    if data["attachments"]:
        embed.set_image(url=data["attachments"][0])
    await ctx.send(embed=embed)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send(embed=error_embed("Specify between 1 and 100 messages."))
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(
        embed=success_embed(f"Removed **{len(deleted) - 1}** messages.", title="Purged")
    )
    await asyncio.sleep(2)
    await msg.delete()


@bot.command(name="say")
async def say(ctx, *, message: str):
    if not can_use_say(ctx.author):
        return await ctx.send(embed=error_embed("Admin only."))
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(message)


# ==========================================
# FUN
# ==========================================
@bot.command(name="pp")
async def pp(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id)
    size = random.randint(0, 15)
    visual = f"8{'=' * size}D"
    comment = ["micro", "smol", "average", "respectable", "dangerous"][min(size // 3, 4)]
    embed = vanity_embed(description=f"`{visual}`\n**{size} in** · {comment}")
    embed.set_author(name=f"{target.display_name}", icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="ship")
async def ship(ctx, user1: discord.Member, user2: discord.Member = None):
    if user2 is None:
        user2 = ctx.author
    if user1 == user2:
        return await ctx.send(embed=error_embed("Can't ship yourself."))
    random.seed((user1.id + user2.id) % 100)
    percent = random.randint(0, 100)
    comment = ["doomed", "rough", "maybe", "strong", "destined"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = vanity_embed(
        description=(
            f"**{user1.display_name}** × **{user2.display_name}**\n"
            f"`{bar}` **{percent}%** · {comment}"
        )
    )
    await ctx.send(embed=embed)


@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 69)
    percent = random.randint(0, 100)
    comment = ["straight as an arrow", "slightly fruity", "bit sus", "pretty gay", "certified fruity"][
        min(percent // 20, 4)
    ]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = vanity_embed(description=f"`{bar}` **{percent}%**\n{comment}")
    embed.set_author(name=f"{target.display_name}", icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 420)
    percent = random.randint(0, 100)
    comment = ["no simp detected", "mild", "occasional", "heavy", "professional simp"][
        min(percent // 20, 4)
    ]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = vanity_embed(description=f"`{bar}` **{percent}%**\n{comment}")
    embed.set_author(name=f"{target.display_name}", icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 1337)
    percent = random.randint(0, 100)
    comment = ["terminally cringe", "slightly cringe", "mid", "based", "extremely based"][
        min(percent // 20, 4)
    ]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = vanity_embed(description=f"`{bar}` **{percent}%**\n{comment}")
    embed.set_author(name=f"{target.display_name}", icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 999)
    score = random.randint(40, 160)
    if score < 80:
        comment = "smooth brain"
    elif score < 100:
        comment = "a bit slow"
    elif score < 120:
        comment = "average"
    elif score < 140:
        comment = "very smart"
    else:
        comment = "genius"
    embed = vanity_embed(description=f"**{score} IQ**\n{comment}")
    embed.set_author(name=f"{target.display_name}", icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send(embed=error_embed("Ask something."))
    answers = [
        "Yes.", "No.", "Maybe.", "Definitely.", "Absolutely not.",
        "Ask again later.", "Very doubtful.", "Without a doubt.",
        "Signs point to yes.", "Don't count on it.",
    ]
    embed = vanity_embed(title="8 Ball")
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=f"**{random.choice(answers)}**", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    chamber, bullet = random.randint(1, 6), random.randint(1, 6)
    if chamber == bullet:
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="Lost roulette")
            desc = f"**BANG.** {ctx.author.mention} — muted **2 minutes**."
            color = EMBED_ERROR
        except Exception:
            desc = f"**BANG.** {ctx.author.mention} — couldn't timeout."
            color = EMBED_ERROR
    else:
        desc = f"*click.* {ctx.author.mention} survives — **{chamber}/6**."
        color = EMBED_COLOR
    await ctx.send(
        embed=vanity_embed(title="Roulette", description=desc, color=color, footer="Vanity · 20s cooldown")
    )


# ==========================================
# MOD
# ==========================================
@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if not can_mute(ctx.author):
        return await ctx.send(embed=error_embed("You can't use this."))
    if (
        member.top_role >= ctx.author.top_role
        and ctx.author != ctx.guild.owner
        and not is_admin(ctx.author)
    ):
        return await ctx.send(embed=error_embed("Cannot action user with higher/equal role."))
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send(embed=error_embed("Invalid duration (max 28d). Use 10m, 1h, 1d…"))
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(
            embed=vanity_embed(
                title="Muted",
                description=f"{member.mention} · `{duration}`\n**Reason:** {reason}",
            )
        )
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


@bot.command(name="unmute")
async def unmute(ctx, member: discord.Member):
    if not can_mute(ctx.author):
        return await ctx.send(embed=error_embed("You can't use this."))
    try:
        await member.timeout(None)
        await ctx.send(embed=success_embed(f"{member.mention} unmuted.", title="Unmuted"))
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


@bot.command(name="kick")
@commands.has_permissions(administrator=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=error_embed("Cannot action user with higher/equal role."))
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=vanity_embed(title="Kicked", description=f"**{member}**\n{reason}", color=EMBED_ERROR))
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


@bot.command(name="ban")
@commands.has_permissions(administrator=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=error_embed("Cannot action user with higher/equal role."))
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=vanity_embed(title="Banned", description=f"**{member}**\n{reason}", color=EMBED_ERROR))
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


# ==========================================
# START
# ==========================================
async def main():
    token = _clean_env("BOT_TOKEN")
    if not token:
        print("CRITICAL: BOT_TOKEN is missing in environment variables.")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
