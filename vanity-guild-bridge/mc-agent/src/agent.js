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
    console.error(`CRITICAL: ${name} missing`);
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

mc.on('kicked', (reason) => console.warn('[MC] kicked:', reason));
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

const seenJoin = new Map();
const JOIN_CD = 10 * 60 * 1000;
const joinLog = [];

function strip(s) {
  return (s || '').replace(COLOR_RE, '').trim();
}

function parseGuildChat(raw) {
  const line = strip(raw);
  if (!line) return null;
  for (const re of GUILD_PATTERNS) {
    const m = line.match(re);
    if (m) return { ign: m[1], message: m[2].trim() };
  }
  return null;
}

function parseJoin(raw) {
  const line = strip(raw);
  if (!line) return null;
  for (const re of JOIN_PATTERNS) {
    const m = line.match(re);
    if (m) return m[1];
  }
  return null;
}

async function webhook(ign, message, kind) {
  const url = (DISCORD_BRIDGE_WEBHOOK_URL || '').trim();
  if (!url) return;
  const isJoin = kind === 'join';
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: isJoin ? 'Joins' : 'Guild',
        embeds: [
          {
            author: {
              name: isJoin ? `${ign} joined` : ign,
              icon_url: `https://mc-heads.net/avatar/${encodeURIComponent(ign)}/64`,
            },
            description: isJoin ? `**${ign}** in vanity` : String(message || '').slice(0, 2000),
            color: isJoin ? 0x5b8cff : 0x57f287,
            footer: { text: isJoin ? 'join' : 'guild chat' },
            timestamp: new Date().toISOString(),
          },
        ],
      }),
    });
  } catch (e) {
    console.warn('[hook]', e.message);
  }
}

function onJoin(ign) {
  if (!ign) return;
  const k = ign.toLowerCase();
  const last = seenJoin.get(k) || 0;
  if (Date.now() - last < JOIN_CD) return;
  seenJoin.set(k, Date.now());
  joinLog.push({ ign, at: new Date().toISOString() });
  if (joinLog.length > 200) joinLog.splice(0, joinLog.length - 200);
  webhook(ign, '', 'join');
  console.log('[join]', ign);
  // no welcome message
}

mc.on('chatline', (line) => {
  const j = parseJoin(line);
  if (j) onJoin(j);
  const g = parseGuildChat(line);
  if (g) webhook(g.ign, g.message, 'chat');
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
  });
});

app.get('/players', (req, res) => {
  try {
    if (!mc.isReady()) return res.status(503).json({ error: 'not connected' });
    res.json({ players: mc.getOnlinePlayers() });
  } catch (e) {
    res.status(503).json({ error: e.message });
  }
});

app.get('/joins', (req, res) => {
  res.json({ joins: joinLog.slice(-100) });
});

app.post('/command', async (req, res) => {
  const { command, timeoutMs, quietMs } = req.body || {};
  if (!command || typeof command !== 'string') {
    return res.status(400).json({ error: 'missing command' });
  }
  try {
    const lines = await mc.runCommandAndCollect(command, {
      timeoutMs: timeoutMs || 8000,
      quietMs: quietMs || 1200,
    });
    res.json({ lines });
  } catch (e) {
    res.status(503).json({ error: e.message });
  }
});

app.post('/chat', (req, res) => {
  const { message } = req.body || {};
  if (!message || typeof message !== 'string') {
    return res.status(400).json({ error: 'missing message' });
  }
  try {
    mc.sendChat(message.slice(0, 256));
    res.json({ ok: true });
  } catch (e) {
    res.status(503).json({ error: e.message });
  }
});

const port = AGENT_PORT ? Number(AGENT_PORT) : 3000;
app.listen(port, () => console.log(`[agent] :${port}`));
