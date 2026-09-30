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
STAFF_USERS = {"hahaxdlolezfkbrh"}
FATE_ONLY = {"hahaxdlolezfkbrh"}
FATE_TARGET_NAME = env("FATE_TARGET", "kqttin").lower()
FATE_CHANNEL_ID = int(env("FATE_CHANNEL_ID", "0") or 0)

# Global task reference for managing the repeat ping loop
fate_ping_task: Optional[asyncio.Task] = None


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


def is_fate_user(member: discord.Member) -> bool:
    names = {
        (member.name or "").lower(),
        (member.display_name or "").lower(),
        (getattr(member, "global_name", None) or "").lower(),
    }
    return bool(names & FATE_ONLY)


def find_member_by_name(guild: discord.Guild, name: str):
    name = (name or "").lower()
    for m in guild.members:
        if name in {
            (m.name or "").lower(),
            (m.display_name or "").lower(),
            (getattr(m, "global_name", None) or "").lower(),
        }:
            return m
    return None


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
                    (data or {}).get("error", f"agent {r.status}")
                    if isinstance(data, dict)
                    else f"agent {r.status}"
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

    if BRIDGE_CHANNEL_ID and message.guild and message.channel.id == BRIDGE_CHANNEL_ID:
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

        wait = bridge_wait(message.author.id)
        if wait > 0:
            notice = await message.channel.send(f"{message.author.mention} wait {wait:.1f}s cooldown")
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
        except Exception as err:
            await message.channel.send(f"Error: {err}", delete_after=8)
        return

    if INVITE_CHANNEL_ID and message.guild and message.channel.id == INVITE_CHANNEL_ID:
        content = message.content.strip()
        if IGN_RE.match(content):
            try:
                lines = await mc_command(f"/g invite {content}", timeout_ms=5000, quiet_ms=900)
                body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip())
                await message.channel.send(f"Invite for {content}:\n{body or 'sent'}")
            except Exception as err:
                await message.channel.send(f"Error: {err}")
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, CommandNotFound)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"Missing argument: `{error.param.name}`")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("Bad argument.")
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f"Wait {error.retry_after:.0f}s")
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("No permission.")
    print(f"error in {ctx.command}: {error}")
    try:
        await ctx.send(f"Error: {error}")
    except Exception:
        pass


@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    msg = "?g list — members\n?g invite <ign> — invite"
    if BRIDGE_CHANNEL_ID:
        msg += f"\nBridge channel: <#{BRIDGE_CHANNEL_ID}>"
    await ctx.send(msg)


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_command("/g list")
        except Exception as err:
            return await ctx.send(f"Error: {err}")
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip()) or "empty"
    for i in range(0, len(body), 1900):
        await ctx.send(f"```\n{body[i:i+1900]}\n```")


@g_group.command(name="invite")
async def g_invite(ctx, ign: str):
    ign = ign.strip()
    if not IGN_RE.match(ign):
        return await ctx.send("Invalid IGN.")
    async with ctx.typing():
        try:
            lines = await mc_command(f"/g invite {ign}", timeout_ms=5000, quiet_ms=900)
        except Exception as err:
            return await ctx.send(f"Error: {err}")
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip())
    await ctx.send(f"Invite sent to {ign}:\n```\n{(body or 'sent')[:900]}\n```")


@bot.command(name="region")
async def region_cmd(ctx, region: str):
    if not is_staff(ctx.author):
        return await ctx.send("Staff only.")
    region = region.lower().strip()
    if region not in ("eu", "as"):
        return await ctx.send("Use `eu` or `as`")
    server = "nethpot" if region == "eu" else "sword"
    async with ctx.typing():
        try:
            data = await mc_switch(region=region, server=server)
        except Exception as err:
            return await ctx.send(f"Error: {err}")
    await ctx.send(f"Switched region to {region.upper()} ({data.get('host', '?')}) - {server}")


@bot.command(name="server")
async def server_cmd(ctx, server: str):
    if not is_staff(ctx.author):
        return await ctx.send("Staff only.")
    server = server.lower().strip()
    if server not in ("sword", "nethpot"):
        return await ctx.send("Use `sword` or `nethpot`")
    async with ctx.typing():
        try:
            data = await mc_switch(server=server)
        except Exception as err:
            return await ctx.send(f"Error: {err}")
    await ctx.send(f"Switched server to {server} on {data.get('host', '?')}")


async def ping_loop(channel, mention: str, requester_name: str):
    try:
        while True:
            await channel.send(f"{mention} {requester_name} is asking for a Fate clan invite, drop it when you can")
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass


@bot.command(name="fateinvite", aliases=["fate", "fateinv"])
async def fate_invite(ctx):
    """Pings the target user every 2 seconds in plain text."""
    global fate_ping_task

    if not is_fate_user(ctx.author):
        return await ctx.send("You can't use this.")

    channel = ctx.channel
    if FATE_CHANNEL_ID:
        ch = ctx.guild.get_channel(FATE_CHANNEL_ID)
        if ch is None:
            return await ctx.send("FATE_CHANNEL_ID not found in this server.")
        channel = ch

    target = find_member_by_name(ctx.guild, FATE_TARGET_NAME)
    mention = target.mention if target else f"@{FATE_TARGET_NAME}"

    if fate_ping_task and not fate_ping_task.done():
        return await ctx.send("Ping loop is already running! Use `?fatestop` to stop it.")

    fate_ping_task = bot.loop.create_task(
        ping_loop(channel, mention, ctx.author.display_name)
    )
    await ctx.send(f"Started pinging {mention} every 2 seconds. Use `?fatestop` to cancel.")


@bot.command(name="fatestop")
async def fate_stop(ctx):
    """Stops the repeating ping loop."""
    global fate_ping_task

    if not is_fate_user(ctx.author):
        return await ctx.send("You can't use this.")

    if fate_ping_task and not fate_ping_task.done():
        fate_ping_task.cancel()
        fate_ping_task = None
        await ctx.send("Stopped the ping loop.")
    else:
        await ctx.send("No active ping loop running.")


@bot.command(name="help")
async def help_cmd(ctx):
    msg = (
        "Commands (Prefix ?):\n"
        "Guild: ?g list | ?g invite <ign>\n"
        "Tools: ?ping | ?snipe | ?say <msg>\n"
        "Fun: ?pp | ?ship | ?gay | ?simp | ?based | ?iq | ?8ball | ?rr\n"
        "Staff: ?region | ?server | ?purge <amount> | ?mute | ?unmute | ?kick | ?ban"
    )
    if is_fate_user(ctx.author):
        msg += "\nFate: ?fateinvite | ?fatestop"
    await ctx.send(msg)


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send("Nothing here.")
    
    msg = f"Sniped message from {data['author']}:\n{data['content'] or '*empty*'}"
    if data["attachments"]:
        msg += f"\nAttachment: {data['attachments'][0]}"
    await ctx.send(msg)


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
        return await ctx.send("Amount must be 1–100.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Purged {len(deleted) - 1} messages.")
    await asyncio.sleep(2)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.command(name="pp")
async def pp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id)
    n = random.randint(0, 15)
    labels = ["micro", "small", "average", "solid", "dangerous"]
    await ctx.send(f"{t.display_name}'s pp: 8{'=' * n}D ({n} - {labels[min(n // 3, 4)]})")


@bot.command(name="ship")
async def ship(ctx, a: discord.Member, b: discord.Member = None):
    b = b or ctx.author
    if a.id == b.id:
        return await ctx.send("Pick two different people.")
    random.seed((a.id + b.id) % 10_000)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    vibes = ["nope", "rough", "maybe", "strong", "locked in"]
    await ctx.send(f"{a.display_name} x {b.display_name}: [{bar}] {p}% - {vibes[min(p // 20, 4)]}")


@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 69)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    await ctx.send(f"{t.display_name} is [{bar}] {p}% gay")


@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 420)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    await ctx.send(f"{t.display_name} is [{bar}] {p}% simp")


@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 1337)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    await ctx.send(f"{t.display_name} is [{bar}] {p}% based")


@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 999)
    await ctx.send(f"{t.display_name}'s IQ is {random.randint(40, 160)}")


@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send("Ask something.")
    answers = [
        "Yes.", "No.", "Maybe.", "Definitely.", "Nah.",
        "Later.", "Looks good.", "Don't count on it.",
        "Without a doubt.", "Very doubtful.",
    ]
    await ctx.send(f"Q: {question}\nA: {random.choice(answers)}")


@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    chamber, bullet = random.randint(1, 6), random.randint(1, 6)
    if chamber == bullet:
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="roulette")
            await ctx.send(f"Bang! {ctx.author.mention} got timed out for 2 minutes.")
        except Exception:
            await ctx.send(f"Bang! {ctx.author.mention} died.")
    else:
        await ctx.send(f"Click. {ctx.author.mention} survived ({chamber}/6).")


@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.moderate_members:
        return await ctx.send("Can't mute.")
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner and not is_staff(ctx.author):
        return await ctx.send("Role hierarchy error.")
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send("Duration error (`10m`, `1h`, `1d` - max 28d).")
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(f"Muted {member.mention} for {duration}. Reason: {reason}")
    except Exception as err:
        await ctx.send(f"Error: {err}")


@bot.command(name="unmute")
async def unmute(ctx, member: discord.Member):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.moderate_members:
        return await ctx.send("Can't unmute.")
    try:
        await member.timeout(None)
        await ctx.send(f"Unmuted {member.mention}")
    except Exception as err:
        await ctx.send(f"Error: {err}")


@bot.command(name="kick")
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.kick_members:
        return await ctx.send("Can't kick.")
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("Role hierarchy error.")
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(f"Kicked {member}. Reason: {reason}")
    except Exception as err:
        await ctx.send(f"Error: {err}")


@bot.command(name="ban")
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.ban_members:
        return await ctx.send("Can't ban.")
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("Role hierarchy error.")
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(f"Banned {member}. Reason: {reason}")
    except Exception as err:
        await ctx.send(f"Error: {err}")


async def main():
    if not TOKEN:
        print("CRITICAL: BOT_TOKEN missing")
        return
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
