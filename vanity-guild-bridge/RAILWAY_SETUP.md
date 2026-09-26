# Deploying on Railway

You'll run **two services in the same Railway project**:

1. **`mc-agent`** (Node.js) — stays connected to stray.gg, exposes a tiny HTTP API.
2. **your existing Python bot** (`bot.py`) — now edited to call `mc-agent` for guild commands.

They talk to each other over HTTP, so no shared filesystem or process needed.

## 1. Deploy `mc-agent`

- In Railway, add a new service from the `mc-agent/` folder (separate repo or a
  subfolder of a monorepo — Railway lets you set a custom root/build directory
  per service).
- Set these environment variables on that service:
  - `AGENT_SECRET` — a long random string you make up (e.g. `openssl rand -hex 32`)
  - `MC_HOST` = `stray.gg`
  - `MC_USERNAME` = the Microsoft account email for your bot account
  - `MC_AUTH` = `microsoft`
  - Railway sets `PORT` automatically — the agent reads `AGENT_PORT` first, then
    falls back to `3000`. If Railway's `PORT` isn't picked up, just set
    `AGENT_PORT` to whatever Railway's assigned port is, or update `src/agent.js`
    to read `process.env.PORT` as a fallback.
- Start command: `npm start` (Railway auto-detects `package.json`).
- **First deploy will need interactive Microsoft login.** Since Railway logs
  are viewable but not interactive, do the *first* Microsoft device-code login
  locally on your own machine once (run `npm start` locally with the same
  `MC_USERNAME`), which caches an auth token in a local folder. Then either:
  - copy that cache folder up as part of your deploy (simplest), or
  - use a Railway volume to persist the auth cache between deploys.
  Without this, the agent will print a login link in the Railway logs but
  nobody will complete it, and the connection will hang.
- Once deployed, Railway gives you a public URL like
  `https://mc-agent-production-xxxx.up.railway.app`, or you can reach it
  privately from another service in the same project at
  `http://mc-agent.railway.internal:<port>` (Railway's private networking).

## 2. Update your Python bot's environment variables

On your existing bot's Railway service, add:

```
MC_AGENT_URL=https://mc-agent-production-xxxx.up.railway.app
# or, using Railway private networking within the same project:
# MC_AGENT_URL=http://mc-agent.railway.internal:3000

MC_AGENT_SECRET=<the same AGENT_SECRET you set on mc-agent>
GUILD_INVITE_CHANNEL_ID=<channel id for the bare-IGN auto-invite channel>
```

If you use Railway's private networking URL, traffic between the two services
never leaves Railway's internal network, so the public internet never sees your
`mc-agent` service at all — that's the more secure option if your plan supports it.

## 3. Redeploy the Python bot

Nothing else changes about how you deploy `bot.py` — same `BOT_TOKEN`, same
`requirements.txt` (`discord.py`, `aiohttp`, `Pillow` if you use `$quote`).

## 4. Test

- `$g list`, `$g menu`, `$g invite SomeIGN` in Discord.
- Post a bare IGN in the channel matching `GUILD_INVITE_CHANNEL_ID` — you should
  get a ✅ or ❌ reaction within a few seconds.

## Troubleshooting

- **"Couldn't reach the Minecraft bot"** in Discord → `mc-agent` is down, the
  URL/secret is wrong, or the Minecraft connection dropped. Check `mc-agent`'s
  Railway logs for `[MC] Spawned in world.`
- **`mc-agent` stuck waiting for login** → the Microsoft auth cache didn't
  persist across deploys; see the volume/cache note above.
- **`/g menu` comes back empty** → if stray.gg opens `/g menu` as an inventory
  GUI rather than printing chat text, the current chat-listener won't catch it.
  Let me know and I'll switch that one command to read the open GUI window
  instead of chat.
