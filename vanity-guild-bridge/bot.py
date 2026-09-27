import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound
import os
import json
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
EMBED_ERROR = 0xFF4D6D
EMBED_INFO = 0x5B8CFF

BOT_ALLOWED_USERS = {"hahaxdlolezfkbrh"}

INVITED_FILE = "invited_igns.json"
RECRUIT_INTERVAL_SECONDS = 45  # how often to scan online players
RECRUIT_BATCH_SIZE = 3         # max invites per scan (looks less botty)

PM_VARIATIONS = [
    "yo do /g join vnty",
    "yo join up /g join vnty",
    "yo real quick do /g join vnty",
    "yo hop in the guild /g join vnty",
    "yo /g join vnty when u can",
]

WELCOME_VARIATIONS = [
    "yo gng welcome to vanity lmk your discord username",
    "welcome to vanity lmk whats ur discord username",
    "yo g wlcm to vanity lmk ur discord user",
    "yo welcome to vanity drop ur discord username",
    "wlcm to vanity gng lmk ur discord",
    "yo gng u in vanity now lmk ur discord username",
]


def _clean_env(name: str, default: str = "") -> str:
    val = os.getenv(name, default) or default
    val = val.strip().strip('"').strip("'")
    val = "".join(ch for ch in val if ord(ch) >= 32 or ch == "\t")
    return val


MC_AGENT_URL = _clean_env("MC_AGENT_URL").rstrip("/")
MC_AGENT_SECRET = _clean_env("MC_AGENT_SECRET")
GUILD_BRIDGE_CHANNEL_ID = int(_clean_env("GUILD_BRIDGE_CHANNEL_ID", "0") or 0)
GUILD_INVITE_CHANNEL_ID = int(_clean_env("GUILD_INVITE_CHANNEL_ID", "0") or 0)
MC_GUILD_CHAT_PREFIX = _clean_env("MC_GUILD_CHAT_PREFIX", "/g chat ")
if MC_GUILD_CHAT_PREFIX and not MC_GUILD_CHAT_PREFIX.endswith(" "):
    MC_GUILD_CHAT_PREFIX += " "

# Comma-separated IGNs the bot should never invite (own account, alts, etc.)
RECRUIT_IGNORE = {
    x.strip().lower()
    for x in _clean_env("RECRUIT_IGNORE_IGNS", "").split(",")
    if x.strip()
}

BRIDGE_COOLDOWN_SECONDS = 2.0
_bridge_last_send = {}

MC_COLOR_CODE_RE = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_REGEX = re.compile(r"^[A-Za-z0-9_]{2,16}$")
snipe_cache = {}

# recruit state
_recruit_enabled = True


# ==========================================
# PERMISSIONS
# ==========================================
def is_admin(member: discord.Member) -> bool:
    return bool(
        getattr(member, "guild_permissions", None)
        and member.guild_permissions.administrator
    )


def can_use_bot(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if is_admin(member):
        return True
    name = (member.name or "").lower()
    display = (member.display_name or "").lower()
    global_name = (getattr(member, "global_name", None) or "").lower()
    return (
        name in BOT_ALLOWED_USERS
        or display in BOT_ALLOWED_USERS
        or global_name in BOT_ALLOWED_USERS
    )


def vanity_embed(
    *,
    title=None,
    description=None,
    color=EMBED_COLOR,
    footer="Vanity · Guild Bridge",
    author_name=None,
    author_icon=None,
):
    embed = discord.Embed(color=color, timestamp=discord.utils.utcnow())
    if title:
        embed.title = title
    if description:
        embed.description = description
    if footer:
        embed.set_footer(
            text=footer,
            icon_url=bot.user.display_avatar.url if bot.user else None,
        )
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon)
    return embed


def error_embed(message: str):
    return vanity_embed(
        title="Something went wrong",
        description=f"```\n{message}\n```",
        color=EMBED_ERROR,
        footer="Vanity · Error",
    )


def success_embed(message: str, title: str = "Done"):
    return vanity_embed(title=title, description=message, color=EMBED_COLOR)


@bot.check
async def global_permission_check(ctx: commands.Context) -> bool:
    if can_use_bot(ctx.author):
        return True
    await ctx.send(
        embed=vanity_embed(
            title="Access denied",
            description="Only **administrators** (and approved users) can use this bot.",
            color=EMBED_ERROR,
            footer="Vanity · Restricted",
        )
    )
    return False


# ==========================================
# INVITED TRACKING
# ==========================================
def load_invited() -> set:
    if not os.path.exists(INVITED_FILE):
        return set()
    try:
        with open(INVITED_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).lower() for x in data}
        if isinstance(data, dict):
            return {str(x).lower() for x in data.get("invited", [])}
    except Exception:
        pass
    return set()


def save_invited(invited: set):
    with open(INVITED_FILE, "w") as f:
        json.dump({"invited": sorted(invited)}, f, indent=2)


def mark_invited(ign: str):
    invited = load_invited()
    invited.add(ign.lower())
    save_invited(invited)


def was_invited(ign: str) -> bool:
    return ign.lower() in load_invited()


# ==========================================
# MC AGENT HELPERS
# ==========================================
def strip_mc_colors(text: str) -> str:
    return MC_COLOR_CODE_RE.sub("", text or "")


def _agent_headers():
    secret = "".join(ch for ch in (MC_AGENT_SECRET or "") if ord(ch) >= 32)
    return {"x-agent-secret": secret, "Content-Type": "application/json"}


async def mc_agent_command(command: str, timeout_ms: int = 8000, quiet_ms: int = 1200):
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")
    url = f"{MC_AGENT_URL}/command"
    payload = {"command": command, "timeoutMs": timeout_ms, "quietMs": quiet_ms}
    try:
        timeout = aiohttp.ClientTimeout(total=(timeout_ms / 1000) + 5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=_agent_headers()) as resp:
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
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")
    url = f"{MC_AGENT_URL}/chat"
    payload = {"message": text}
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=_agent_headers()) as resp:
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


async def mc_agent_players() -> list:
    """Online player IGNs from the Minecraft client tab list."""
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET aren't configured on this bot.")
    url = f"{MC_AGENT_URL}/players"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_agent_headers()) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(
                        data.get("error", f"mc-agent returned {resp.status}")
                        if isinstance(data, dict)
                        else f"mc-agent returned {resp.status}"
                    )
                return list(data.get("players", [])) if isinstance(data, dict) else []
    except asyncio.TimeoutError:
        raise RuntimeError("Timed out fetching players.")
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
        embeds.append(
            vanity_embed(
                title=title if i == 0 else f"{title} · continued",
                description=f"```\n{chunk}\n```",
                footer="Vanity · Live from stray.gg",
            )
        )
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
    last = _bridge_last_send.get(user_id, 0.0)
    return max(0.0, BRIDGE_COOLDOWN_SECONDS - (time.monotonic() - last))


def mark_bridge_sent(user_id: int):
    _bridge_last_send[user_id] = time.monotonic()


# ==========================================
# AUTO RECRUIT
# ==========================================
async def recruit_one(ign: str) -> str:
    """Invite + PM one player. Returns status string."""
    ign = ign.strip()
    if not IGN_REGEX.match(ign):
        return f"skip invalid `{ign}`"
    if was_invited(ign) or ign.lower() in RECRUIT_IGNORE:
        return f"skip already `{ign}`"

    try:
        lines = await mc_agent_command(f"/g invite {ign}", timeout_ms=4000, quiet_ms=800)
        reply = " ".join(strip_mc_colors(l) for l in lines).lower()
        # still mark invited so we don't spam even on soft failures
        mark_invited(ign)

        # human delay before msg
        await asyncio.sleep(random.uniform(0.8, 1.8))
        pm = random.choice(PM_VARIATIONS)
        await mc_agent_chat(f"/msg {ign} {pm}")

        if any(
            x in reply
            for x in ("already", "not online", "no such", "cannot", "doesn't exist", "does not exist")
        ):
            return f"invited `{ign}` (server said: {reply[:80] or 'ok'})"
        return f"invited + msg'd `{ign}`"
    except Exception as e:
        mark_invited(ign)  # avoid hammering on errors too
        return f"fail `{ign}`: {e}"


async def run_recruit_scan() -> list:
    """Scan online players and invite a small batch of new ones."""
    results = []
    try:
        players = await mc_agent_players()
    except Exception as e:
        return [f"players fetch failed: {e}"]

    candidates = []
    for p in players:
        if not p or not isinstance(p, str):
            continue
        ign = p.strip()
        if not IGN_REGEX.match(ign):
            continue
        if was_invited(ign) or ign.lower() in RECRUIT_IGNORE:
            continue
        candidates.append(ign)

    random.shuffle(candidates)
    batch = candidates[:RECRUIT_BATCH_SIZE]
    if not batch:
        return ["no new online players to invite"]

    for ign in batch:
        results.append(await recruit_one(ign))
        await asyncio.sleep(random.uniform(1.5, 3.0))
    return results


@tasks.loop(seconds=RECRUIT_INTERVAL_SECONDS)
async def auto_recruit_loop():
    global _recruit_enabled
    if not _recruit_enabled:
        return
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        return
    try:
        results = await run_recruit_scan()
        # quiet logs only
        for r in results:
            print(f"[recruit] {r}")
    except Exception as e:
        print(f"[recruit] loop error: {e}")


@auto_recruit_loop.before_loop
async def before_recruit():
    await bot.wait_until_ready()
    await asyncio.sleep(10)


# ==========================================
# EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"MC_AGENT_URL set: {bool(MC_AGENT_URL)} | secret set: {bool(MC_AGENT_SECRET)}")
    print(f"Guild bridge channel: {GUILD_BRIDGE_CHANNEL_ID or 'NOT SET'}")
    print(f"Allowed extra users: {', '.join(sorted(BOT_ALLOWED_USERS))}")
    print(f"Auto-recruit: {'ON' if _recruit_enabled else 'OFF'}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="guild chat · ?help")
    )
    if not auto_recruit_loop.is_running():
        auto_recruit_loop.start()


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

    if GUILD_BRIDGE_CHANNEL_ID and message.guild and message.channel.id == GUILD_BRIDGE_CHANNEL_ID:
        content = (message.content or "").strip()
        if content.startswith("?") or (
            bot.user
            and (
                content.startswith(f"<@{bot.user.id}>")
                or content.startswith(f"<@!{bot.user.id}>")
            )
        ):
            await bot.process_commands(message)
            return
        if not content:
            return
        if not can_use_bot(message.author):
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
        if len(content) > 200:
            content = content[:200]
        mark_bridge_sent(message.author.id)
        try:
            await mc_agent_chat(f"{MC_GUILD_CHAT_PREFIX}{content}")
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        except Exception as e:
            await message.channel.send(embed=error_embed(str(e)), delete_after=8)
        return

    if GUILD_INVITE_CHANNEL_ID and message.guild and message.channel.id == GUILD_INVITE_CHANNEL_ID:
        if not can_use_bot(message.author):
            return
        content = message.content.strip()
        if IGN_REGEX.match(content):
            try:
                status = await recruit_one(content)
                await message.channel.send(
                    embed=vanity_embed(title="Recruit", description=status)
                )
            except Exception as e:
                await message.channel.send(embed=error_embed(str(e)))
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(embed=error_embed(f"Missing argument: `{error.param.name}`"))
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
# GUILD COMMANDS
# ==========================================
@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    desc = (
        "**`?g list`** — guild members\n"
        "**`?g invite <ign>`** — invite + msg once\n\n"
        "**`?recruit on/off/scan/status`** — auto-recruit online players\n"
        "**`?region eu|as`** · **`?server sword|nethpot`**"
    )
    if GUILD_BRIDGE_CHANNEL_ID:
        desc += f"\n\nBridge: <#{GUILD_BRIDGE_CHANNEL_ID}>"
    await ctx.send(embed=vanity_embed(title="Guild Commands", description=desc))


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
    if was_invited(ign):
        return await ctx.send(
            embed=vanity_embed(
                title="Already invited",
                description=f"`{ign}` was already invited before (won't spam).",
                color=EMBED_INFO,
            )
        )
    async with ctx.typing():
        status = await recruit_one(ign)
    await ctx.send(embed=vanity_embed(title="Recruit", description=status))


# ==========================================
# RECRUIT CONTROLS
# ==========================================
@bot.group(name="recruit", invoke_without_command=True)
async def recruit_group(ctx):
    await ctx.send(
        embed=vanity_embed(
            title="Auto Recruit",
            description=(
                f"Status: **{'ON' if _recruit_enabled else 'OFF'}**\n"
                f"Interval: every **{RECRUIT_INTERVAL_SECONDS}s** · batch **{RECRUIT_BATCH_SIZE}**\n"
                f"Tracked invites: **{len(load_invited())}**\n\n"
                "`?recruit on` · `?recruit off` · `?recruit scan` · `?recruit status` · `?recruit clear`"
            ),
        )
    )


@recruit_group.command(name="on")
async def recruit_on(ctx):
    global _recruit_enabled
    _recruit_enabled = True
    if not auto_recruit_loop.is_running():
        auto_recruit_loop.start()
    await ctx.send(embed=success_embed("Auto-recruit **enabled**.", title="Recruit"))


@recruit_group.command(name="off")
async def recruit_off(ctx):
    global _recruit_enabled
    _recruit_enabled = False
    await ctx.send(embed=success_embed("Auto-recruit **disabled**.", title="Recruit"))


@recruit_group.command(name="scan")
async def recruit_scan(ctx):
    async with ctx.typing():
        results = await run_recruit_scan()
    body = "\n".join(f"• {r}" for r in results) or "_nothing_"
    await ctx.send(embed=vanity_embed(title="Recruit scan", description=body))


@recruit_group.command(name="status")
async def recruit_status(ctx):
    invited = load_invited()
    sample = ", ".join(f"`{x}`" for x in sorted(invited)[:20]) or "_none_"
    extra = f"\n… +{len(invited) - 20} more" if len(invited) > 20 else ""
    await ctx.send(
        embed=vanity_embed(
            title="Recruit status",
            description=(
                f"**Running:** {'yes' if _recruit_enabled else 'no'}\n"
                f"**Invited total:** {len(invited)}\n{sample}{extra}"
            ),
        )
    )


@recruit_group.command(name="clear")
async def recruit_clear(ctx):
    save_invited(set())
    await ctx.send(embed=success_embed("Cleared invited list (will invite people again).", title="Recruit"))


# ==========================================
# REGION / SERVER SWITCH
# ==========================================
@bot.command(name="region")
async def region_cmd(ctx, region: str):
    region = region.strip().lower()
    if region not in ("eu", "as"):
        return await ctx.send(
            embed=error_embed("Use `?region eu` or `?region as`.")
        )
    async with ctx.typing():
        try:
            lines = await mc_agent_command(f"/region {region}", timeout_ms=6000, quiet_ms=1000)
        except Exception as e:
            return await ctx.send(embed=error_embed(str(e)))
    body = "\n".join(strip_mc_colors(l) for l in lines if strip_mc_colors(l).strip()) or "_sent_"
    await ctx.send(
        embed=vanity_embed(
            title=f"Region → {region.upper()}",
            description=f"Sent `/region {region}`\n```\n{body[:900]}\n```",
        )
    )


@bot.command(name="server")
async def server_cmd(ctx, server: str):
    server = server.strip().lower()
    if server not in ("sword", "nethpot"):
        return await ctx.send(
            embed=error_embed("Use `?server sword` or `?server nethpot`.")
        )
    async with ctx.typing():
        try:
            # fire-and-forget style still via command collect; server switch may reconnect
            await mc_agent_chat(f"/server {server}")
        except Exception as e:
            return await ctx.send(embed=error_embed(str(e)))
    await ctx.send(
        embed=vanity_embed(
            title=f"Server → {server}",
            description=f"Sent `/server {server}` — bot should move shortly.",
        )
    )


# ==========================================
# UTILITY
# ==========================================
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(
        embed=vanity_embed(
            title="Pong",
            description=f"Gateway latency **`{round(bot.latency * 1000)}ms`**",
            footer="Vanity · Status",
        )
    )


@bot.command(name="help")
async def help_cmd(ctx):
    embed = vanity_embed(
        title="Vanity",
        description="Admin-only · prefix **`?`** · allowed: admins + `hahaxdlolezfkbrh`",
    )
    embed.add_field(
        name="▸ Guild",
        value="`?g list` · `?g invite <ign>`",
        inline=True,
    )
    embed.add_field(
        name="▸ Recruit",
        value="`?recruit on/off/scan/status/clear`",
        inline=True,
    )
    embed.add_field(
        name="▸ World",
        value="`?region eu|as`\n`?server sword|nethpot`",
        inline=True,
    )
    embed.add_field(
        name="▸ Bridge",
        value=(
            f"<#{GUILD_BRIDGE_CHANNEL_ID}> → `/g chat`"
            if GUILD_BRIDGE_CHANNEL_ID
            else "Set `GUILD_BRIDGE_CHANNEL_ID`"
        ),
        inline=False,
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
        return await ctx.send(
            embed=vanity_embed(title="Snipe", description="_Nothing to snipe._", color=EMBED_INFO)
        )
    embed = vanity_embed(description=data["content"] or "_no text_", footer="Vanity · Sniped")
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
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
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
    await ctx.send(
        embed=vanity_embed(
            description=f"**{user1.display_name}** × **{user2.display_name}**\n`{bar}` **{percent}%** · {comment}"
        )
    )


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
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
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
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
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
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 999)
    score = random.randint(40, 160)
    comment = (
        "smooth brain"
        if score < 80
        else "a bit slow"
        if score < 100
        else "average"
        if score < 120
        else "very smart"
        if score < 140
        else "genius"
    )
    embed = vanity_embed(description=f"**{score} IQ**\n{comment}")
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
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
            desc, color = f"**BANG.** {ctx.author.mention} — muted **2 minutes**.", EMBED_ERROR
        except Exception:
            desc, color = f"**BANG.** {ctx.author.mention} — couldn't timeout.", EMBED_ERROR
    else:
        desc, color = f"*click.* {ctx.author.mention} survives — **{chamber}/6**.", EMBED_COLOR
    await ctx.send(
        embed=vanity_embed(
            title="Roulette", description=desc, color=color, footer="Vanity · 20s cooldown"
        )
    )


# ==========================================
# MOD
# ==========================================
@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
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
    try:
        await member.timeout(None)
        await ctx.send(embed=success_embed(f"{member.mention} unmuted.", title="Unmuted"))
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


@bot.command(name="kick")
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=error_embed("Cannot action user with higher/equal role."))
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(
            embed=vanity_embed(
                title="Kicked", description=f"**{member}**\n{reason}", color=EMBED_ERROR
            )
        )
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


@bot.command(name="ban")
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=error_embed("Cannot action user with higher/equal role."))
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(
            embed=vanity_embed(
                title="Banned", description=f"**{member}**\n{reason}", color=EMBED_ERROR
            )
        )
    except Exception as e:
        await ctx.send(embed=error_embed(str(e)))


async def main():
    token = _clean_env("BOT_TOKEN")
    if not token:
        print("CRITICAL: BOT_TOKEN is missing in environment variables.")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
