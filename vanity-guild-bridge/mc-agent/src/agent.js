require('dotenv').config();
const express = require('express');
const MinecraftClient = require('./mcClient');

const {
  AGENT_SECRET,
  AGENT_PORT,
  MC_HOST,
  MC_PORT,
  MC_USERNAME,
  MC_AUTH,
  MC_VERSION,
  DISCORD_BRIDGE_WEBHOOK_URL,
} = process.env;

function requireEnv(name, value) {
  if (!value) {
    console.error(`CRITICAL: ${name} is missing from the environment.`);
    process.exit(1);
  }
  return value;
}

requireEnv('AGENT_SECRET', AGENT_SECRET);
requireEnv('MC_HOST', MC_HOST);
requireEnv('MC_USERNAME', MC_USERNAME);

const mc = new MinecraftClient({
  host: MC_HOST,
  port: MC_PORT ? Number(MC_PORT) : 25565,
  username: MC_USERNAME,
  auth: MC_AUTH || 'microsoft',
  version: MC_VERSION || false,
});

mc.on('kicked', (reason) => console.warn('[MC] Kicked, will reconnect:', reason));
mc.connect();

const COLOR_RE = /\u00A7[0-9A-FK-ORa-fk-or]/gi;
const GUILD_PATTERNS = [
  /^\[(?:Guild|G|GC)\]\s*(?:\[.*?\]\s*)?([A-Za-z0-9_]{2,16})\s*[:»>\-]\s*(.+)$/i,
  /^(?:Guild|GC)\s*[>»|]\s*([A-Za-z0-9_]{2,16})\s*[:»>\-]\s*(.+)$/i,
  /^([A-Za-z0-9_]{2,16})\s*(?:\[G\]|\[Guild\])\s*[:»>\-]\s*(.+)$/i,
];
const JOIN_PATTERNS = [
  /^([A-Za-z0-9_]{2,16})\s+(?:has\s+)?joined\s+(?:the\s+)?guild/i,
  /^([A-Za-z0-9_]{2,16})\s+joined\s+Vanity/i,
  /(?:guild|vanity).*?\b([A-Za-z0-9_]{2,16})\s+(?:has\s+)?joined/i,
  /^\[(?:Guild|G)\].*?\b([A-Za-z0-9_]{2,16})\s+(?:has\s+)?joined/i,
];

const welcomedRecently = new Map();
const WELCOME_COOLDOWN_MS = 10 * 60 * 1000;
const joinLog = []; // { ign, at }
const JOIN_LOG_MAX = 200;

function stripColors(s) {
  return (s || '').replace(COLOR_RE, '').trim();
}

function parseGuildChat(raw) {
  const line = stripColors(raw);
  if (!line) return null;
  for (const re of GUILD_PATTERNS) {
    const m = line.match(re);
    if (m) return { ign: m[1], message: m[2].trim(), raw: line };
  }
  if (/\b(?:guild|\[g\])\b/i.test(line) && line.includes(':')) {
    const idx = line.indexOf(':');
    const left = line.slice(0, idx).trim();
    const right = line.slice(idx + 1).trim();
    const ignMatch = left.match(/([A-Za-z0-9_]{2,16})$/);
    if (ignMatch && right) return { ign: ignMatch[1], message: right, raw: line };
  }
  return null;
}

function parseGuildJoin(raw) {
  const line = stripColors(raw);
  if (!line) return null;
  for (const re of JOIN_PATTERNS) {
    const m = line.match(re);
    if (m) return m[1];
  }
  return null;
}

async function postGuildToDiscord(ign, message, kind) {
  const webhook = (DISCORD_BRIDGE_WEBHOOK_URL || '').trim();
  if (!webhook) return;

  const isJoin = kind === 'join';
  const embed = {
    author: {
      name: isJoin ? `${ign} joined the guild` : ign,
      icon_url: `https://mc-heads.net/avatar/${encodeURIComponent(ign)}/64`,
    },
    description: isJoin ? `**${ign}** is in vanity now.` : message.slice(0, 2000),
    color: isJoin ? 0x5b8cff : 0x57f287,
    footer: { text: isJoin ? 'Vanity · Guild join' : 'Vanity · In-game guild chat' },
    timestamp: new Date().toISOString(),
  };

  try {
    await fetch(webhook, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: isJoin ? 'Guild Joins' : 'Guild Chat',
        embeds: [embed],
      }),
    });
  } catch (err) {
    console.warn('[Bridge] Webhook error:', err.message);
  }
}

function recordJoin(ign) {
  if (!ign) return;
  const key = ign.toLowerCase();
  const last = welcomedRecently.get(key) || 0;
  if (Date.now() - last < WELCOME_COOLDOWN_MS) return;
  welcomedRecently.set(key, Date.now());

  joinLog.push({ ign, at: new Date().toISOString() });
  if (joinLog.length > JOIN_LOG_MAX) joinLog.splice(0, joinLog.length - JOIN_LOG_MAX);

  // log + discord webhook only — no in-game welcome message
  postGuildToDiscord(ign, '', 'join');
  console.log('[MC] Recorded guild join (no welcome msg):', ign);
}

mc.on('chatline', (line) => {
  const joined = parseGuildJoin(line);
  if (joined) {
    console.log('[MC] Guild join:', joined, '|', stripColors(line));
    recordJoin(joined);
  }
  const parsed = parseGuildChat(line);
  if (parsed) postGuildToDiscord(parsed.ign, parsed.message, 'chat');
});

const app = express();
app.use(express.json());

app.use((req, res, next) => {
  if (req.headers['x-agent-secret'] !== AGENT_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  next();
});

app.get('/health', (req, res) => {
  res.json({
    connected: mc.isReady(),
    players: mc.isReady() ? mc.getOnlinePlayers().length : 0,
    bridgeWebhook: Boolean((DISCORD_BRIDGE_WEBHOOK_URL || '').trim()),
  });
});

app.get('/players', (req, res) => {
  try {
    if (!mc.isReady()) {
      return res.status(503).json({ error: 'Minecraft bot is not connected yet.' });
    }
    res.json({ players: mc.getOnlinePlayers() });
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
});

app.get('/joins', (req, res) => {
  res.json({ joins: joinLog.slice(-100) });
});

app.post('/command', async (req, res) => {
  const { command, timeoutMs, quietMs } = req.body || {};
  if (!command || typeof command !== 'string') {
    return res.status(400).json({ error: 'missing "command" string in body' });
  }
  try {
    const lines = await mc.runCommandAndCollect(command, {
      timeoutMs: timeoutMs || 8000,
      quietMs: quietMs || 1200,
    });
    res.json({ lines });
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
});

app.post('/chat', (req, res) => {
  const { message } = req.body || {};
  if (!message || typeof message !== 'string') {
    return res.status(400).json({ error: 'missing "message" string in body' });
  }
  try {
    mc.sendChat(message.slice(0, 256));
    res.json({ ok: true });
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
});

const port = AGENT_PORT ? Number(AGENT_PORT) : 3000;
app.listen(port, () => {
  console.log(`[Agent] Listening on port ${port}`);
});
