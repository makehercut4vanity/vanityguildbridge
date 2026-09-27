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
from datetime import datetime, timedelta, timezone
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

DATA_FILE = "recruit_data.json"

# Recruit engine — tuned for joins, not spam flags
SCAN_EVERY = 15          # seconds between scans
MAX_PER_SCAN = 8         # hard cap per scan
MIN_GAP = 0.55           # min seconds between invite actions
MAX_GAP = 1.35           # max seconds between invite actions
FAIL_COOLDOWN = 8 * 60   # retry soft-fails after 8 min
PM_ONLY_NO_GUILD = True  # never msg people already in a guild

# Short, normal messages — not corporate, not essay
PM_LINES = [
    "yo /g join vnty",
    "g /g join vnty",
    "yo join vanity /g join vnty",
    "/g join vnty",
    "yo hop in /g join vnty",
]


def _clean_env(name: str, default: str = "") -> str:
    val = os.getenv(name, default) or default
    val = val.strip().strip('"').strip("'")
    return "".join(ch for ch in val if ord(ch) >= 32 or ch == "\t")


MC_AGENT_URL = _clean_env("MC_AGENT_URL").rstrip("/")
MC_AGENT_SECRET = _clean_env("MC_AGENT_SECRET")
GUILD_BRIDGE_CHANNEL_ID = int(_clean_env("GUILD_BRIDGE_CHANNEL_ID", "0") or 0)
GUILD_INVITE_CHANNEL_ID = int(_clean_env("GUILD_INVITE_CHANNEL_ID", "0") or 0)
MC_GUILD_CHAT_PREFIX = _clean_env("MC_GUILD_CHAT_PREFIX", "/g chat ")
if MC_GUILD_CHAT_PREFIX and not MC_GUILD_CHAT_PREFIX.endswith(" "):
    MC_GUILD_CHAT_PREFIX += " "

RECRUIT_IGNORE = {
    x.strip().lower()
    for x in _clean_env("RECRUIT_IGNORE_IGNS", "").split(",")
    if x.strip()
}

BRIDGE_COOLDOWN = 2.0
_bridge_last = {}
_recruit_on = True
_fail_until = {}  # ign -> epoch
_scan_lock = asyncio.Lock()

MC_COLOR_RE = re.compile(r"\u00A7[0-9A-FK-ORa-fk-or]")
IGN_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")
snipe_cache = {}


# ==========================================
# PERMS / UI
# ==========================================
def is_admin(m: discord.Member) -> bool:
    return bool(getattr(m, "guild_permissions", None) and m.guild_permissions.administrator)


def can_use_bot(m) -> bool:
    if not isinstance(m, discord.Member):
        return False
    if is_admin(m):
        return True
    names = {
        (m.name or "").lower(),
        (m.display_name or "").lower(),
        (getattr(m, "global_name", None) or "").lower(),
    }
    return bool(names & BOT_ALLOWED_USERS)


def emb(*, title=None, desc=None, color=EMBED_COLOR, footer="Vanity"):
    e = discord.Embed(color=color, timestamp=discord.utils.utcnow())
    if title:
        e.title = title
    if desc:
        e.description = desc
    e.set_footer(text=footer, icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


def err(msg: str):
    return emb(title="Error", desc=f"```\n{msg}\n```", color=EMBED_ERROR, footer="Vanity · Error")


@bot.check
async def _gate(ctx: commands.Context) -> bool:
    if can_use_bot(ctx.author):
        return True
    await ctx.send(
        embed=emb(
            title="Locked",
            desc="Admins + approved only.",
            color=EMBED_ERROR,
            footer="Vanity · Restricted",
        )
    )
    return False


# ==========================================
# DATA
# ==========================================
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"done": {}, "log": [], "stats": {"invites": 0, "msgs": 0, "scans": 0}}
    try:
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"done": {}, "log": [], "stats": {"invites": 0, "msgs": 0, "scans": 0}}
        d.setdefault("done", {})
        d.setdefault("log", [])
        d.setdefault("stats", {"invites": 0, "msgs": 0, "scans": 0})
        # migrate old invited_igns shape if someone dropped it here
        if "invited" in d and not d["done"]:
            inv = d.pop("invited")
            if isinstance(inv, dict):
                d["done"] = inv
            elif isinstance(inv, list):
                d["done"] = {str(x).lower(): {"at": None, "status": "ok"} for x in inv}
        return d
    except Exception:
        return {"done": {}, "log": [], "stats": {"invites": 0, "msgs": 0, "scans": 0}}


def save_data(d: dict):
    log = d.get("log", [])
    if len(log) > 600:
        d["log"] = log[-600:]
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)


def is_done(ign: str) -> bool:
    return ign.lower() in load_data().get("done", {})


def mark_done(ign: str, status: str, note: str = "", action: str = "invite"):
    d = load_data()
    key = ign.lower()
    now = _now_iso()
    d["done"][key] = {"ign": ign, "at": now, "status": status}
    d["log"].append({"ign": ign, "action": action, "at": now, "note": (note or status)[:120]})
    if action == "invite":
        d["stats"]["invites"] = d["stats"].get("invites", 0) + 1
    if action == "msg":
        d["stats"]["msgs"] = d["stats"].get("msgs", 0) + 1
    save_data(d)


def bump_scan():
    d = load_data()
    d["stats"]["scans"] = d["stats"].get("scans", 0) + 1
    save_data(d)


def cooling(ign: str) -> bool:
    return time.time() < _fail_until.get(ign.lower(), 0)


def cool(ign: str, seconds: int = FAIL_COOLDOWN):
    _fail_until[ign.lower()] = time.time() + seconds


# ==========================================
# MC AGENT
# ==========================================
def strip_mc(t: str) -> str:
    return MC_COLOR_RE.sub("", t or "")


def _hdrs():
    secret = "".join(c for c in (MC_AGENT_SECRET or "") if ord(c) >= 32)
    return {"x-agent-secret": secret, "Content-Type": "application/json"}


async def agent_post(path: str, payload: dict, timeout_s: float = 12.0):
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET not set")
    url = f"{MC_AGENT_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=_hdrs()) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(
                    data.get("error", f"agent {resp.status}")
                    if isinstance(data, dict)
                    else f"agent {resp.status}"
                )
            return data if isinstance(data, dict) else {}


async def agent_get(path: str, timeout_s: float = 8.0):
    if not MC_AGENT_URL or not MC_AGENT_SECRET:
        raise RuntimeError("MC_AGENT_URL / MC_AGENT_SECRET not set")
    url = f"{MC_AGENT_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_hdrs()) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(
                    data.get("error", f"agent {resp.status}")
                    if isinstance(data, dict)
                    else f"agent {resp.status}"
                )
            return data if isinstance(data, dict) else {}


async def mc_cmd(command: str, timeout_ms: int = 4000, quiet_ms: int = 700):
    data = await agent_post(
        "/command",
        {"command": command, "timeoutMs": timeout_ms, "quietMs": quiet_ms},
        timeout_s=(timeout_ms / 1000) + 4,
    )
    return list(data.get("lines", []))


async def mc_chat(text: str):
    await agent_post("/chat", {"message": text}, timeout_s=8)


async def mc_players() -> list:
    data = await agent_get("/players")
    return list(data.get("players", []))


async def mc_joins() -> list:
    try:
        data = await agent_get("/joins")
        return list(data.get("joins", []))
    except Exception:
        return []


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
# RECRUIT ENGINE
# ==========================================
def classify_invite_reply(reply: str) -> str:
    """Return: ok | already_guild | already_invited | offline | error"""
    r = (reply or "").lower()
    if any(
        x in r
        for x in (
            "not online",
            "offline",
            "no such",
            "does not exist",
            "doesn't exist",
            "unknown player",
            "cannot find",
            "never joined",
            "invalid",
        )
    ):
        return "offline"
    if any(
        x in r
        for x in (
            "already in a guild",
            "already in another",
            "in another guild",
            "has a guild",
            "must leave",
            "leave their guild",
            "leave your guild",
            "currently in a guild",
            "in a guild",
        )
    ):
        return "already_guild"
    if any(
        x in r
        for x in (
            "already invited",
            "already a member",
            "in your guild",
            "already in your",
        )
    ):
        return "already_invited"
    if any(x in r for x in ("invited", "invite sent", "has been invited", "sent invite")):
        return "ok"
    # empty / unknown — treat as ok so we still PM once (player was online in tab list)
    if not r.strip():
        return "ok"
    return "ok"


async def recruit_one(ign: str) -> str:
    ign = ign.strip()
    if not IGN_RE.match(ign):
        return f"bad ign `{ign}`"
    if ign.lower() in RECRUIT_IGNORE:
        return f"ignored `{ign}`"
    if is_done(ign):
        return f"done `{ign}`"
    if cooling(ign):
        return f"wait `{ign}`"

    try:
        lines = await mc_cmd(f"/g invite {ign}", timeout_ms=3200, quiet_ms=550)
        reply = " ".join(strip_mc(l) for l in lines)
        kind = classify_invite_reply(reply)

        if kind == "offline":
            cool(ign)
            return f"offline `{ign}`"

        if kind in ("already_guild", "already_invited"):
            mark_done(ign, kind, note=reply)
            return f"{kind} `{ign}` (no msg)"

        # invitable — lock them + optional PM
        mark_done(ign, "ok", note=reply or "invited", action="invite")

        if PM_ONLY_NO_GUILD:
            await asyncio.sleep(random.uniform(0.25, 0.7))
            line = random.choice(PM_LINES)
            try:
                await mc_chat(f"/msg {ign} {line}")
                d = load_data()
                d["stats"]["msgs"] = d["stats"].get("msgs", 0) + 1
                d["log"].append(
                    {"ign": ign, "action": "msg", "at": _now_iso(), "note": line}
                )
                save_data(d)
            except Exception as e:
                return f"invited `{ign}` · msg fail: {e}"

        return f"invited `{ign}`"

    except Exception as e:
        cool(ign, 120)
        return f"err `{ign}`: {e}"


async def recruit_scan() -> list:
    async with _scan_lock:
        bump_scan()
        try:
            players = await mc_players()
        except Exception as e:
            return [f"tab list fail: {e}"]

        print(f"[recruit] tab={len(players)} sample={players[:15]}")

        pool = []
        for p in players:
            if not isinstance(p, str):
                continue
            ign = p.strip()
            if not IGN_RE.match(ign):
                continue
            if ign.lower() in RECRUIT_IGNORE:
                continue
            if is_done(ign) or cooling(ign):
                continue
            pool.append(ign)

        random.shuffle(pool)
        # adaptive batch: more online → slightly more invites, still capped
        target = min(MAX_PER_SCAN, max(3, len(pool) // 4 or len(pool)))
        batch = pool[:target]

        if not batch:
            d = load_data()
            return [
                f"idle · online={len(players)} locked={len(d.get('done', {}))} cooling={len(_fail_until)}"
            ]

        out = []
        for ign in batch:
            out.append(await recruit_one(ign))
            await asyncio.sleep(random.uniform(MIN_GAP, MAX_GAP))
        return out


@tasks.loop(seconds=SCAN_EVERY)
async def recruit_loop():
    if not _recruit_on or not MC_AGENT_URL:
        return
    try:
        for line in await recruit_scan():
            print(f"[recruit] {line}")
    except Exception as e:
        print(f"[recruit] loop: {e}")


@recruit_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()
    await asyncio.sleep(4)


# ==========================================
# EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"in as {bot.user} | recruit every {SCAN_EVERY}s batch≤{MAX_PER_SCAN}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="?help")
    )
    if not recruit_loop.is_running():
        recruit_loop.start()


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
        if not content or not can_use_bot(message.author):
            return
        left = BRIDGE_COOLDOWN - (time.monotonic() - _bridge_last.get(message.author.id, 0))
        if left > 0:
            w = await message.channel.send(
                embed=emb(title="Slow down", desc=f"{message.author.mention} **{left:.1f}s**", color=EMBED_INFO)
            )
            await asyncio.sleep(2)
            try:
                await w.delete()
            except Exception:
                pass
            return
        _bridge_last[message.author.id] = time.monotonic()
        try:
            await mc_chat(f"{MC_GUILD_CHAT_PREFIX}{content[:200]}")
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        except Exception as e:
            await message.channel.send(embed=err(str(e)), delete_after=8)
        return

    if GUILD_INVITE_CHANNEL_ID and message.guild and message.channel.id == GUILD_INVITE_CHANNEL_ID:
        if not can_use_bot(message.author):
            return
        content = message.content.strip()
        if IGN_RE.match(content):
            await message.channel.send(embed=emb(title="Recruit", desc=await recruit_one(content)))
            return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, CommandNotFound)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(embed=err(f"missing `{error.param.name}`"))
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(
            embed=emb(title="Cooldown", desc=f"**{error.retry_after:.0f}s**", color=EMBED_INFO)
        )
    print("cmd error", ctx.command, error)
    try:
        await ctx.send(embed=err(str(error)))
    except Exception:
        pass


# ==========================================
# GUILD / RECRUIT COMMANDS
# ==========================================
@bot.group(name="g", invoke_without_command=True)
async def g_group(ctx):
    await ctx.send(
        embed=emb(
            title="Guild",
            desc="`?g list` · `?g invite <ign>`\n`?recruit` · `?joinlog` · `?region` · `?server`",
        )
    )


@g_group.command(name="list")
async def g_list(ctx):
    async with ctx.typing():
        try:
            lines = await mc_cmd("/g list", timeout_ms=8000, quiet_ms=1200)
        except Exception as e:
            return await ctx.send(embed=err(str(e)))
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip()) or "empty"
    for i in range(0, max(len(body), 1), 3800):
        await ctx.send(
            embed=emb(title="Guild list" if i == 0 else "…", desc=f"```\n{body[i:i+3800]}\n```")
        )


@g_group.command(name="invite")
async def g_invite(ctx, ign: str):
    ign = ign.strip()
    if not IGN_RE.match(ign):
        return await ctx.send(embed=err("bad ign"))
    async with ctx.typing():
        msg = await recruit_one(ign)
    await ctx.send(embed=emb(title="Recruit", desc=msg))


@bot.group(name="recruit", invoke_without_command=True)
async def recruit_grp(ctx):
    d = load_data()
    st = d.get("stats", {})
    await ctx.send(
        embed=emb(
            title="Recruit",
            desc=(
                f"**{'ON' if _recruit_on else 'OFF'}** · every **{SCAN_EVERY}s** · up to **{MAX_PER_SCAN}**/scan\n"
                f"Locked: **{len(d.get('done', {}))}** · scans: **{st.get('scans', 0)}** · "
                f"invites: **{st.get('invites', 0)}** · msgs: **{st.get('msgs', 0)}**\n\n"
                "`on` `off` `scan` `status` `clear` · `?joinlog`"
            ),
        )
    )


@recruit_grp.command(name="on")
async def recruit_on(ctx):
    global _recruit_on
    _recruit_on = True
    if not recruit_loop.is_running():
        recruit_loop.start()
    await ctx.send(embed=emb(title="Recruit", desc="**on**"))


@recruit_grp.command(name="off")
async def recruit_off(ctx):
    global _recruit_on
    _recruit_on = False
    await ctx.send(embed=emb(title="Recruit", desc="**off**"))


@recruit_grp.command(name="scan")
async def recruit_scan_cmd(ctx):
    async with ctx.typing():
        rows = await recruit_scan()
    await ctx.send(embed=emb(title="Scan", desc="\n".join(f"· {r}" for r in rows)[:4000]))


@recruit_grp.command(name="status")
async def recruit_status(ctx):
    d = load_data()
    try:
        online = len(await mc_players())
    except Exception:
        online = "?"
    keys = sorted(d.get("done", {}).keys())
    sample = ", ".join(f"`{k}`" for k in keys[:20]) or "—"
    more = f"\n+{len(keys)-20} more" if len(keys) > 20 else ""
    st = d.get("stats", {})
    await ctx.send(
        embed=emb(
            title="Status",
            desc=(
                f"engine **{'on' if _recruit_on else 'off'}** · online **{online}**\n"
                f"invites **{st.get('invites', 0)}** · msgs **{st.get('msgs', 0)}** · scans **{st.get('scans', 0)}**\n"
                f"locked **{len(keys)}**\n{sample}{more}"
            ),
        )
    )


@recruit_grp.command(name="clear")
async def recruit_clear(ctx):
    d = load_data()
    d["done"] = {}
    save_data(d)
    _fail_until.clear()
    await ctx.send(embed=emb(title="Recruit", desc="cleared locklist (log kept)"))


@bot.command(name="joinlog", aliases=["joins", "invitelog", "joinlogs"])
async def joinlog_cmd(ctx, limit: int = 25):
    limit = max(5, min(limit, 40))
    d = load_data()
    log = list(reversed(d.get("log", [])))[:limit]
    lines = []
    for e in log:
        at = e.get("at") or "?"
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00")).strftime("%m/%d %H:%M")
        except Exception:
            at = str(at)[:16]
        note = e.get("note") or ""
        if len(note) > 36:
            note = note[:36] + "…"
        lines.append(
            f"`{at}` **{e.get('action', '?')}** `{e.get('ign', '?')}`"
            + (f" — {note}" if note else "")
        )

    joins = list(reversed(await mc_joins()))[:limit]
    jlines = []
    for j in joins:
        at = j.get("at") or "?"
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00")).strftime("%m/%d %H:%M")
        except Exception:
            at = str(at)[:16]
        jlines.append(f"`{at}` **joined** `{j.get('ign', '?')}`")

    desc = "**Sent**\n" + ("\n".join(lines) if lines else "_none_")
    desc += "\n\n**Joined (in-game)**\n" + ("\n".join(jlines) if jlines else "_none_")
    await ctx.send(embed=emb(title="Join log", desc=desc[:4000], footer="Vanity · ?joinlog"))


@bot.command(name="region")
async def region_cmd(ctx, region: str):
    region = region.lower().strip()
    if region not in ("eu", "as"):
        return await ctx.send(embed=err("eu | as"))
    async with ctx.typing():
        try:
            lines = await mc_cmd(f"/region {region}", timeout_ms=6000, quiet_ms=1000)
        except Exception as e:
            return await ctx.send(embed=err(str(e)))
    body = "\n".join(strip_mc(l) for l in lines if strip_mc(l).strip()) or "sent"
    await ctx.send(embed=emb(title=f"region {region}", desc=f"```\n{body[:900]}\n```"))


@bot.command(name="server")
async def server_cmd(ctx, server: str):
    server = server.lower().strip()
    if server not in ("sword", "nethpot"):
        return await ctx.send(embed=err("sword | nethpot"))
    try:
        await mc_chat(f"/server {server}")
    except Exception as e:
        return await ctx.send(embed=err(str(e)))
    await ctx.send(embed=emb(title=f"server {server}", desc="sent"))


# ==========================================
# UTIL / FUN / MOD
# ==========================================
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(embed=emb(title="pong", desc=f"**{round(bot.latency*1000)}ms**"))


@bot.command(name="help")
async def help_cmd(ctx):
    e = emb(title="Vanity", desc="admins + `hahaxdlolezfkbrh` · prefix `?`")
    e.add_field(name="guild", value="`?g list` `?g invite`", inline=True)
    e.add_field(name="recruit", value="`?recruit` `?joinlog`", inline=True)
    e.add_field(name="world", value="`?region` `?server`", inline=True)
    e.add_field(name="other", value="`?ping` `?snipe` `?purge` `?say` · fun · mod", inline=False)
    await ctx.send(embed=e)


@bot.command(name="snipe")
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=emb(title="snipe", desc="_empty_", color=EMBED_INFO))
    e = emb(desc=data["content"] or "_no text_", footer="sniped")
    e.set_author(name=data["author"], icon_url=data["avatar"])
    e.timestamp = data["time"]
    if data["attachments"]:
        e.set_image(url=data["attachments"][0])
    await ctx.send(embed=e)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send(embed=err("1-100"))
    deleted = await ctx.channel.purge(limit=amount + 1)
    m = await ctx.send(embed=emb(title="purged", desc=f"**{len(deleted)-1}**"))
    await asyncio.sleep(2)
    await m.delete()


@bot.command(name="say")
async def say(ctx, *, message: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(message)


@bot.command(name="pp")
async def pp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id)
    n = random.randint(0, 15)
    e = emb(desc=f"`8{'='*n}D` · **{n}**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="ship")
async def ship(ctx, a: discord.Member, b: discord.Member = None):
    b = b or ctx.author
    if a == b:
        return await ctx.send(embed=err("no"))
    random.seed((a.id + b.id) % 100)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    await ctx.send(embed=emb(desc=f"**{a.display_name}** × **{b.display_name}**\n`{bar}` **{p}%**"))


@bot.command(name="gay", aliases=["howgay"])
async def gay(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 69)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = emb(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="simp")
async def simp(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 420)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = emb(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="based")
async def based(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 1337)
    p = random.randint(0, 100)
    bar = "█" * round(p / 10) + "░" * (10 - round(p / 10))
    e = emb(desc=f"`{bar}` **{p}%**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="iq")
async def iq(ctx, member: discord.Member = None):
    t = member or ctx.author
    random.seed(t.id + 999)
    e = emb(desc=f"**{random.randint(40,160)}**")
    e.set_author(name=t.display_name, icon_url=t.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    if not question:
        return await ctx.send(embed=err("ask something"))
    ans = ["Yes.", "No.", "Maybe.", "Definitely.", "Nah.", "Later.", "Doubt.", "Without a doubt.", "Yes signs.", "Don't count on it."]
    e = emb(title="8ball")
    e.add_field(name="q", value=question)
    e.add_field(name="a", value=f"**{random.choice(ans)}**")
    await ctx.send(embed=e)


@bot.command(name="roulette", aliases=["rr"])
@commands.cooldown(1, 20, commands.BucketType.user)
async def roulette(ctx):
    c, b = random.randint(1, 6), random.randint(1, 6)
    if c == b:
        try:
            await ctx.author.timeout(timedelta(minutes=2), reason="rr")
            d, col = f"**bang** {ctx.author.mention}", EMBED_ERROR
        except Exception:
            d, col = f"**bang** (no perms)", EMBED_ERROR
    else:
        d, col = f"*click* {ctx.author.mention} **{c}/6**", EMBED_COLOR
    await ctx.send(embed=emb(title="rr", desc=d, color=col))


@bot.command(name="mute")
async def mute(ctx, member: discord.Member, duration: str = "1h", *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner and not is_admin(ctx.author):
        return await ctx.send(embed=err("role"))
    delta = parse_duration(duration)
    if not delta or delta > timedelta(days=28):
        return await ctx.send(embed=err("duration"))
    try:
        await member.timeout(delta, reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=emb(title="muted", desc=f"{member.mention} `{duration}`\n{reason}"))
    except Exception as e:
        await ctx.send(embed=err(str(e)))


@bot.command(name="unmute")
async def unmute(ctx, member: discord.Member):
    try:
        await member.timeout(None)
        await ctx.send(embed=emb(title="unmuted", desc=member.mention))
    except Exception as e:
        await ctx.send(embed=err(str(e)))


@bot.command(name="kick")
async def kick(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=err("role"))
    try:
        await member.kick(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=emb(title="kicked", desc=f"**{member}**\n{reason}", color=EMBED_ERROR))
    except Exception as e:
        await ctx.send(embed=err(str(e)))


@bot.command(name="ban")
async def ban(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send(embed=err("role"))
    try:
        await member.ban(reason=f"{reason} · {ctx.author}")
        await ctx.send(embed=emb(title="banned", desc=f"**{member}**\n{reason}", color=EMBED_ERROR))
    except Exception as e:
        await ctx.send(embed=err(str(e)))


async def main():
    token = _clean_env("BOT_TOKEN")
    if not token:
        print("CRITICAL: BOT_TOKEN missing")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
