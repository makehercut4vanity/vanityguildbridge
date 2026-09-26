
import discord
from discord.ext import commands
from discord.ext.commands import CommandNotFound
import os
import asyncio
import random
import re
import aiohttp
from datetime import timedelta
from typing import Optional

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
    intents=intents,
)
bot.remove_command("help")

# ==========================================
# CONFIGURATION
# ==========================================
EMBED_COLOR = 0x57F287

SAY_ALLOWED = {"time4vanity"}
MUTE_ALLOWED = {"corp1637"}

# ---- MINECRAFT GUILD BRIDGE (mc-agent) ----
MC_AGENT_URL = os.getenv("MC_AGENT_URL", "").rstrip("/")
MC_AGENT_SECRET = os.getenv("MC_AGENT_SECRET", "")
GUILD_INVITE_CHANNEL_ID = int(os.getenv("GUILD_INVITE_CHANNEL_ID", "0") or 0)

MC_COLOR_CODE_RE = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_REGEX = re.compile(r"^[A-Za-z0-9_]{2,16}$")

snipe_cache = {}


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
    """
    Sends a chat command to the Minecraft account via mc-agent and returns
    the list of server chat lines collected in response.
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
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(data.get("error", f"mc-agent returned {resp.status}"))
                return data.get("lines", [])
    except asyncio.TimeoutError:
        raise RuntimeError("Timed out waiting for mc-agent.")
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Couldn't reach mc-agent: {e}")
    except Exception as e:
        raise RuntimeError(str(e))


def build_mc_lines_embeds(title: str, lines: list) -> list:
    cleaned = [strip_mc_colors(l) for l in lines if strip_mc_colors(l).strip()]
    body = "\n".join(cleaned) if cleaned else "No response received from the server."
    chunks = [body[i : i + 3900] for i in range(0, len(body), 3900)] or [""]

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


# ==========================================
# EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"MC_AGENT_URL set: {bool(MC_AGENT_URL)} | secret set: {bool(MC_AGENT_SECRET)}")
    await bot.change_presence(activity=discord.Game(name="$g list · $help"))


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

    # Auto guild invite: bare IGN posted in the invite channel
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
                embed = discord.Embed(
                    description=(
                        f"Sent `/g invite {content}` in-game."
                        + (f"\n```\n{response_text[:1000]}\n```" if response_text else "")
                    ),
                    color=EMBED_COLOR,
                )
                embed.set_footer(text="stray.gg · Guild Bridge")
                await message.channel.send(embed=embed)
            except Exception as e:
                await message.channel.send(f"Couldn't invite `{content}`: {e}")
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"Missing argument: `{error.param.name}`")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("Invalid argument.")
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f"Cooldown — try again in **{error.retry_after:.0f}s**.")
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("You don't have permission to use this.")
    print(f"Command error in {ctx.command}: {error}")
    try:
        await ctx.send(f"Error: `{error}`")
    except Exception:
        pass


# ==========================================
# MINECRAFT GUILD BRIDGE
# ==========================================
@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    await ctx.send("Usage: `$g list`, `$g menu`, or `$g invite <ign>`.")


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_agent_command("/g list")
        except Exception as e:
            return await ctx.send(f"Couldn't reach the Minecraft bot: {e}")
    for embed in build_mc_lines_embeds("Guild List", lines):
        await ctx.send(embed=embed)


@g_group.command(name="menu")
async def g_menu(ctx):
    async with ctx.typing():
        try:
            lines = await mc_agent_command("/g menu")
        except Exception as e:
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
            lines = await mc_agent_command(
                f"/g invite {ign}", timeout_ms=5000, quiet_ms=1000
            )
        except Exception as e:
            return await ctx.send(f"Couldn't reach the Minecraft bot: {e}")

    response_text = "\n".join(strip_mc_colors(l) for l in lines).strip()
    embed = discord.Embed(
        description=(
            f"Sent `/g invite {ign}` in-game."
            + (f"\n```\n{response_text[:1000]}\n```" if response_text else "")
        ),
        color=EMBED_COLOR,
    )
    embed.set_footer(text="stray.gg · Guild Bridge")
    await ctx.send(embed=embed)


# ==========================================
# UTILITY
# ==========================================
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong · `{round(bot.latency * 1000)}ms`")


@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="Vanity Bot", color=EMBED_COLOR)
    embed.add_field(
        name="Guild Bridge",
        value="`$g list` · `$g menu` · `$g invite <ign>`",
        inline=False,
    )
    embed.add_field(
        name="Utility",
        value="`$ping` · `$snipe` · `$purge <1-100>` · `$say <text>`",
        inline=False,
    )
    embed.add_field(
        name="Fun",
        value="`$pp` · `$ship` · `$gay` · `$simp` · `$based` · `$iq` · `$8ball` · `$roulette`",
        inline=False,
    )
    embed.add_field(
        name="Mod",
        value="`$mute` · `$unmute` · `$kick` · `$ban`",
        inline=False,
    )
    embed.set_footer(text="Prefix: $ or !")
    await ctx.send(embed=embed)


@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send("Nothing to snipe.")
    embed = discord.Embed(
        description=data["content"] or "*no text*",
        color=EMBED_COLOR,
        timestamp=data["time"],
    )
    embed.set_author(name=data["author"], icon_url=data["avatar"])
    embed.set_footer(text="Sniped")
    if data["attachments"]:
        embed.set_image(url=data["attachments"][0])
        if len(data["attachments"]) > 1:
            embed.add_field(
                name="Extra Attachments",
                value="\n".join(data["attachments"][1:3]),
            )
    await ctx.send(embed=embed)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send("Specify between 1 and 100 messages.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted **{len(deleted) - 1}** messages.")
    await asyncio.sleep(2)
    await msg.delete()


@bot.command(name="say")
async def say(ctx, *, message: str):
    if not can_use_say(ctx.author):
        return await ctx.send("Admin only.")
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
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=f"{target.display_name}'s pp", icon_url=target.display_avatar.url)
    embed.description = f"`{visual}`\n**{size} inches** · {comment}"
    await ctx.send(embed=embed)


@bot.command(name="ship")
async def ship(ctx, user1: discord.Member, user2: discord.Member = None):
    if user2 is None:
        user2 = ctx.author
    if user1 == user2:
        return await ctx.send("Can't ship yourself.")
    random.seed((user1.id + user2.id) % 100)
    percent = random.randint(0, 100)
    comment = ["doomed", "rough", "maybe", "strong", "destined"][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.description = (
        f"**{user1.display_name}** × **{user2.display_name}**\n"
        f"`{bar}` **{percent}%** · {comment}"
    )
    await ctx.send(embed=embed)


@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 69)
    percent = random.randint(0, 100)
    comment = [
        "straight as an arrow",
        "slightly fruity",
        "bit sus",
        "pretty gay",
        "certified fruity",
    ][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(
        name=f"{target.display_name}'s Gay Meter",
        icon_url=target.display_avatar.url,
    )
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
    await ctx.send(embed=embed)


@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 420)
    percent = random.randint(0, 100)
    comment = [
        "no simp detected",
        "mild",
        "occasional",
        "heavy",
        "professional simp",
    ][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(
        name=f"{target.display_name}'s Simp Meter",
        icon_url=target.display_avatar.url,
    )
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
    await ctx.send(embed=embed)


@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    target = member or ctx.author
    random.seed(target.id + 1337)
    percent = random.randint(0, 100)
    comment = [
        "terminally cringe",
        "slightly cringe",
        "mid",
        "based",
        "extremely based",
    ][min(percent // 20, 4)]
    bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(
        name=f"{target.display_name}'s Based Meter",
        icon_url=target.display_avatar.url,
    )
    embed.description = f"`{bar}` **{percent}%**\n{comment}"
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
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(
        name=f"{target.display_name}'s IQ",
        icon_url=target.display_avatar.url,
    )
    embed.description = f"**{score} IQ**\n{comment}"
    await ctx.send(embed=embed)


@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send("Ask something.")
    answers = [
        "Yes.",
        "No.",
        "Maybe.",
        "Definitely.",
        "Absolutely not.",
        "Ask again later.",
        "Very doubtful.",
        "Without a doubt.",
        "Signs point to yes.",
        "Don't count on it.",
    ]
    embed = discord.Embed(color=EMBED_COLOR)
    embed.add_field(name="Question", value=question)
    embed.add_field(name="Answer", value=f"**{random.choice(answers)}**")
    await ctx.send(embed=embed)


@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    chamber, bullet = random.randint(1, 6), random.randint(1, 6)
    if chamber == bullet:
        embed = discord.Embed(color=discord.Color.red())
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="Lost roulette")
            embed.description = (
                f"🔫 **BANG.** {ctx.author.mention} caught one — muted for **2 minutes**. 💀"
            )
        except Exception:
            embed.description = (
                f"🔫 **BANG.** {ctx.author.mention} would've eaten it, but I can't timeout them."
            )
    else:
        embed = discord.Embed(color=EMBED_COLOR)
        embed.description = (
            f"*click.* {ctx.author.mention} survives — chamber **{chamber}/6**. 🍀"
        )
    embed.set_footer(text="$roulette · pull the trigger again in 20s")
    await ctx.send(embed=embed)


# ==========================================
# MODERATION
# ==========================================
@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if not can_mute(ctx.author):
        return await ctx.send("You can't use this.")
    if (
        member.top_role >= ctx.author.top_role
        and ctx.author != ctx.guild.owner
        and not is_admin(ctx.author)
    ):
        return await ctx.send("Cannot action user with higher/equal role.")
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send("Invalid duration format (max 28d).")
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(
            embed=discord.Embed(
                description=f"**{member.mention}** muted · `{duration}`\n{reason}",
                color=EMBED_COLOR,
            )
        )
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
# START
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
ENDOFFILE
wc -l /home/workdir/attachments/bot.py
python3 -m py_compile /home/workdir/attachments/bot.py && echo "OK syntax"
592 /home/workdir/attachments/bot.py
OK syntax
