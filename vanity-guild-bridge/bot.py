import discord
from discord.ext import commands
from discord.ext.commands import CommandNotFound
import os
import asyncio
import random
import re
import time
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.moderation = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("?"),
    case_insensitive=True,
    intents=intents,
    help_command=None,
)

# ── palette ────────────────────────────────────────────────────────────────
GREEN = 0x57F287
RED = 0xED4245
BLUE = 0x5865F2
GOLD = 0xFEE75C
DARK = 0x2B2D31

# ── env ────────────────────────────────────────────────────────────────────
def env(name: str, default: str = "") -> str:
    v = (os.getenv(name, default) or default).strip().strip('"').strip("'")
    return "".join(c for c in v if ord(c) >= 32 or c == "\t")


TOKEN = env("BOT_TOKEN")
MC_AGENT_URL = env("MC_AGENT_URL").rstrip("/")
MC_AGENT_SECRET = env("MC_AGENT_SECRET")
BRIDGE_CHANNEL_ID = int(env("GUILD_BRIDGE_CHANNEL_ID", "0") or 0)
INVITE_CHANNEL_ID = int(env("GUILD_INVITE_CHANNEL_ID", "0") or 0)
GCHAT_PREFIX = env("MC_GUILD_CHAT_PREFIX", "/g chat ")
if GCHAT_PREFIX and not GCHAT_PREFIX.endswith(" "):
    GCHAT_PREFIX += " "

BRIDGE_COOLDOWN = 2.0
_bridge_last: dict[int, float] = {}
snipe_cache: dict[int, dict] = {}

MC_COLOR = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")

# extra users who can use staff tools (username, case-insensitive)
STAFF_USERS = {"hahaxdlolezfkbrh"}


# ── helpers ────────────────────────────────────────────────────────────────
def strip_mc(text: str) -> str:
    return MC_COLOR.sub("", text or "")


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    names = {
        (member.name or "").lower(),
        (member.display_name or "").lower(),
        (getattr(member, "global_name", None) or "").lower(),
    }
    return bool(names & STAFF_USERS)


def embed(
    *,
    title: str = None,
    desc: str = None,
    color: int = GREEN,
    footer: str = None,
) -> discord.Embed:
    e = discord.Embed(color=color, timestamp=datetime.now(timezone.utc))
    if title:
        e.title = title
    if desc:
        e.description = desc
    if footer:
        e.set_footer(text=footer)
    elif bot.user:
        e.set_footer(text="vanity", icon_url=bot.user.display_avatar.url)
    return e


def fail(msg: str) -> discord.Embed:
    return embed(title="Couldn't do that", desc=msg, color=RED)


def ok(msg: str, title: str = "Done") -> discord.Embed:
    return embed(title=title, desc=msg, color=GREEN)


def agent_headers() -> dict:
    secret = "".join(c for c in MC_AGENT_SECRET if ord(c) >= 32)
    return {"x-agent-secret": secret, "Content-Type": "application/json"}


async def agent_post(path: str, body: dict, timeout: float = 12.0) -> dict:
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("Minecraft bridge isn't configured.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.post(f"{MC_AGENT_URL}{path}", json=body, headers=agent_headers()) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(
                    (data or {}).get("error", f"agent returned {r.status}")
                    if isinstance(data, dict)
                    else f"agent returned {r.status}"
                )
            return data if isinstance(data, dict) else {}


async def agent_get(path: str, timeout: float = 8.0) -> dict:
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("Minecraft bridge isn't configured.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(f"{MC_AGENT_URL}{path}", headers=agent_headers()) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(
                    (data or {}).get("error", f"agent returned {r.status}")
                    if isinstance(data, dict)
                    else f"agent returned {r.status}"
                )
            return data if isinstance(data, dict) else {}


async def mc_command(cmd: str, timeout_ms: int = 8000, quiet_ms: int = 1200) -> list:
    data = await agent_post(
        "/command",
        {"command": cmd, "timeoutMs": timeout_ms, "quietMs": quiet_ms},
        timeout=(timeout_ms / 1000) + 5,
    )
    return list(data.get("lines", []))


async def mc_chat(msg: str) -> None:
    await agent_post("/chat", {"message": msg}, timeout=8)


async def mc_switch(region: str = None, server: str = None) -> dict:
    body = {}
    if region:
        body["region"] = region
    if server:
        body["server"] = server
    return await agent_post("/switch", body, timeout=45)


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


def bridge_wait(uid: int) -> float:
    last = _bridge_last.get(uid, 0.0)
    return max(0.0, BRIDGE_COOLDOWN - (time.monotonic() - last))


# ── events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"ready · {bot.user} · bridge={BRIDGE_CHANNEL_ID or 'off'}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="guild chat")
    )


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    snipe_cache[message.channel.id] = {
        "content": message.content,
        "author": str(message.author),
        "avatar": message.author.display_avatar.url,
        "time": datetime.now(timezone.utc),
        "attachments": [a.url for a in message.attachments],
    }


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # guild bridge — open to everyone
    if BRIDGE_CHANNEL_ID and message.guild and message.channel.id == BRIDGE_CHANNEL_ID:
        content = (message.content or "").strip()

        # still allow commands in the bridge channel
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

        wait = bridge_wait(message.author.id)
        if wait > 0:
            notice = await message.channel.send(
                embed=embed(
                    desc=f"{message.author.mention} wait **{wait:.1f}s**",
                    color=GOLD,
                    footer="bridge cooldown",
                )
            )
            await asyncio.sleep(2)
            try:
                await notice.delete()
            except Exception:
                pass
            return

        _bridge_last[message.author.id] = time.monotonic()
        try:
            await mc_chat(f"{GCHAT_PREFIX}{content[:200]}")
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        except Exception as e:
            await message.channel.send(embed=fail(str(e)), delete_after=8)
        return

    # plain IGN in invite channel → /g invite
    if INVITE_CHANNEL_ID and message.guild and message.channel.id == INVITE_CHANNEL_ID:
        content = message.content.strip()
        if IGN_RE.match(content):
            try:
                lines = await mc_command(f"/g invite {content}", timeout_ms=5000, quiet_ms=900)
                body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip())
                await message.channel.send(
                    embed=embed(
                        title="Invite sent",
                        desc=f"**{content}**\n```\n{(body or 'ok')[:900]}\n```",
                    )
                )
            except Exception as e:
                await message.channel.send(embed=fail(str(e)))
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, CommandNotFound)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(embed=fail(f"Missing `{error.param.name}`"))
    if isinstance(error, commands.BadArgument):
        return await ctx.send(embed=fail("Bad argument."))
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(
            embed=embed(desc=f"Cooldown **{error.retry_after:.0f}s**", color=GOLD)
        )
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(embed=fail("You don't have permission for that."))
    print(f"error in {ctx.command}: {error}")
    try:
        await ctx.send(embed=fail(str(error)))
    except Exception:
        pass


# ── guild ──────────────────────────────────────────────────────────────────
@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    await ctx.send(
        embed=embed(
            title="Guild",
            desc=(
                "`?g list` — online members\n"
                "`?g invite <ign>` — invite in-game\n\n"
                + (
                    f"Live chat: <#{BRIDGE_CHANNEL_ID}>"
                    if BRIDGE_CHANNEL_ID
                    else "_Set GUILD_BRIDGE_CHANNEL_ID for live chat_"
                )
            ),
            color=BLUE,
        )
    )


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_command("/g list")
        except Exception as e:
            return await ctx.send(embed=fail(str(e)))
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip()) or "No response."
    for i in range(0, len(body), 3800):
        await ctx.send(
            embed=embed(
                title="Guild list" if i == 0 else "Guild list · cont.",
                desc=f"```\n{body[i:i+3800]}\n```",
            )
        )


@g_group.command(name="invite")
async def g_invite(ctx, ign: str):
    ign = ign.strip()
    if not IGN_RE.match(ign):
        return await ctx.send(embed=fail("That doesn't look like a Minecraft name."))
    async with ctx.typing():
        try:
            lines = await mc_command(f"/g invite {ign}", timeout_ms=5000, quiet_ms=900)
        except Exception as e:
            return await ctx.send(embed=fail(str(e)))
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip())
    await ctx.send(
        embed=ok(
            f"Invited **`{ign}`**\n```\n{(body or 'sent')[:900]}\n```",
            title="Invite",
        )
    )


# ── world (staff) ──────────────────────────────────────────────────────────
@bot.command(name="region")
async def region_cmd(ctx, region: str):
    if not is_staff(ctx.author):
        return await ctx.send(embed=fail("Staff only."))
    region = region.lower().strip()
    if region not in ("eu", "as"):
        return await ctx.send(embed=fail("Use `eu` or `as`."))
    server = "nethpot" if region == "eu" else "sword"
    async with ctx.typing():
        try:
            data = await mc_switch(region=region, server=server)
        except Exception as e:
            return await ctx.send(embed=fail(str(e)))
    await ctx.send(
        embed=ok(
            f"**{region.upper()}** · `{data.get('host', '?')}`\n"
            f"Lobby: **{server}**",
            title="Region",
        )
    )


@bot.command(name="server")
async def server_cmd(ctx, server: str):
    if not is_staff(ctx.author):
        return await ctx.send(embed=fail("Staff only."))
    server = server.lower().strip()
    if server not in ("sword", "nethpot"):
        return await ctx.send(embed=fail("Use `sword` or `nethpot`."))
    async with ctx.typing():
        try:
            data = await mc_switch(server=server)
        except Exception as e:
            return await ctx.send(embed=fail(str(e)))
    await ctx.send(
        embed=ok(
            f"Switched to **{server}** on `{data.get('host', '?')}`",
            title="Server",
        )
    )


# ── utility ────────────────────────────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx):
    e = embed(
        title="Vanity",
        desc="Prefix **`?`** · chat in the bridge channel goes in-game.",
        color=BLUE,
    )
    e.add_field(
        name="Guild",
        value="`?g list`\n`?g invite <ign>`",
        inline=True,
    )
    e.add_field(
        name="Bridge",
        value=(
            f"Talk in <#{BRIDGE_CHANNEL_ID}>"
            if BRIDGE_CHANNEL_ID
            else "Set bridge channel"
        ),
        inline=True,
    )
    e.add_field(
        name="Tools",
        value="`?ping` · `?snipe` · `?say`",
        inline=True,
    )
    e.add_field(
        name="Fun",
        value="`?pp` `?ship` `?gay` `?simp` `?based` `?iq` `?8ball` `?rr`",
        inline=False,
    )
    e.add_field(
        name="Staff",
        value="`?region eu|as` · `?server sword|nethpot`\n`?purge` `?mute` `?unmute` `?kick` `?ban`",
        inline=False,
    )
    await ctx.send(embed=e)


@bot.command(name="ping")
async def ping(ctx):
    ms = round(bot.latency * 1000)
    await ctx.send(embed=embed(title="Pong", desc=f"**{ms}ms**", color=BLUE))


@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=embed(desc="Nothing to snipe.", color=DARK))
    e = embed(desc=data["content"] or "*no text*", footer="sniped")
    e.set_author(name=data["author"], icon_url=data["avatar"])
    e.timestamp = data["time"]
    if data["attachments"]:
        e.set_image(url=data["attachments"][0])
    await ctx.send(embed=e)


@bot.command(name="say")
async def say(ctx, *, message: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(message)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send(embed=fail("Pick 1–100."))
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(embed=ok(f"Removed **{len(deleted) - 1}** messages.", title="Purged"))
    await asyncio.sleep(2)
    try:
        await msg.delete()
    except Exception:
        pass


# ── fun ────────────────────────────────────────────────────────────────────
@bot.command(name="pp")
async def pp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id)
    n = random.randint(0, 15)
    labels = ["micro", "small", "average", "solid", "dangerous"]
    e = embed(desc=f"`8{'=' * n}D`\n**{n}** · {labels[min(n // 3, 4)]}")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="ship")
async def ship(ctx, a: discord.Member, b: discord.Member = None):
    b = b or ctx.author
    if a.id == b.id:
        return await ctx.send(embed=fail("Pick two different people."))
    random.seed((a.id + b.id) % 10_000)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    vibes = ["nope", "rough", "maybe", "strong", "meant to be"]
    await ctx.send(
        embed=embed(
            desc=f"**{a.display_name}** × **{b.display_name}**\n`{bar}` **{p}%** · {vibes[min(p // 20, 4)]}"
        )
    )


@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 69)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = embed(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 420)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = embed(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 1337)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = embed(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 999)
    score = random.randint(40, 160)
    e = embed(desc=f"**{score}**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send(embed=fail("Ask a question."))
    answers = [
        "Yes.",
        "No.",
        "Maybe.",
        "Definitely.",
        "Not a chance.",
        "Ask again later.",
        "Looks good.",
        "Don't count on it.",
        "Without a doubt.",
        "Very doubtful.",
    ]
    e = embed(title="8-ball", color=BLUE)
    e.add_field(name="Question", value=question, inline=False)
    e.add_field(name="Answer", value=f"**{random.choice(answers)}**", inline=False)
    await ctx.send(embed=e)


@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    chamber, bullet = random.randint(1, 6), random.randint(1, 6)
    if chamber == bullet:
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="roulette")
            desc, color = f"**Bang.** {ctx.author.mention} — muted 2m", RED
        except Exception:
            desc, color = f"**Bang.** {ctx.author.mention}", RED
    else:
        desc, color = f"*Click.* {ctx.author.mention} · **{chamber}/6**", GREEN
    await ctx.send(embed=embed(title="Roulette", desc=desc, color=color))


# ── mod ────────────────────────────────────────────────────────────────────
@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.moderate_members:
        return await ctx.send(embed=fail("You can't mute people."))
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner and not is_staff(ctx.author):
        return await ctx.send(embed=fail("Can't mute someone with equal/higher role."))
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send(embed=fail("Duration like `10m`, `1h`, `1d` (max 28d)."))
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(
            embed=ok(f"{member.mention} · `{duration}`\n{reason}", title="Muted")
        )
    except Exception as e:
        await ctx.send(embed=fail(str(e)))


@bot.command(name="unmute")
async def unmute(ctx, member: discord.Member):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.moderate_members:
        return await ctx.send(embed=fail("You can't unmute people."))
    try:
        await member.timeout(None)
        await ctx.send(embed=ok(f"{member.mention}", title="Unmuted"))
    except Exception as e:
        await ctx.send(embed=fail(str(e)))


@bot.command(name="kick")
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.kick_members:
        return await ctx.send(embed=fail("You can't kick people."))
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=fail("Can't kick someone with equal/higher role."))
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=embed(title="Kicked", desc=f"**{member}**\n{reason}", color=RED))
    except Exception as e:
        await ctx.send(embed=fail(str(e)))


@bot.command(name="ban")
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.ban_members:
        return await ctx.send(embed=fail("You can't ban people."))
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=fail("Can't ban someone with equal/higher role."))
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=embed(title="Banned", desc=f"**{member}**\n{reason}", color=RED))
    except Exception as e:
        await ctx.send(embed=fail(str(e)))


# ── run ────────────────────────────────────────────────────────────────────
async def main():
    if not TOKEN:
        print("CRITICAL: BOT_TOKEN missing")
        return
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
