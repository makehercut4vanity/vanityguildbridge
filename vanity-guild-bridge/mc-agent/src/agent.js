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

const REGION_HOSTS = {
  as: process.env.MC_HOST_AS || 'as.stray.gg',
  eu: process.env.MC_HOST_EU || 'eu.stray.gg',
};

let currentHost = MC_HOST;
let currentServer = 'sword';
let switchInFlight = false;

const mc = new MinecraftClient({
  host: currentHost,
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
  if (/\b(?:guild|\[g\])\b/i.test(line) && line.includes(':')) {
    const idx = line.indexOf(':');
    const left = line.slice(0, idx).trim();
    const right = line.slice(idx + 1).trim();
    const ignMatch = left.match(/([A-Za-z0-9_]{2,16})$/);
    if (ignMatch && right) return { ign: ignMatch[1], message: right };
  }
  return null;
}

/** Guild chat → Discord only. No join spam. */
async function postGuildChat(ign, message) {
  const url = (DISCORD_BRIDGE_WEBHOOK_URL || '').trim();
  if (!url) return;

  const head = `https://mc-heads.net/avatar/${encodeURIComponent(ign)}/128`;

  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'Vanity',
        avatar_url: 'https://mc-heads.net/avatar/MHF_Steve/128',
        embeds: [
          {
            author: {
              name: ign,
              icon_url: head,
            },
            description: String(message || '').slice(0, 2000),
            color: 0x57f287,
            footer: { text: 'guild chat · stray.gg' },
            timestamp: new Date().toISOString(),
          },
        ],
      }),
    });
  } catch (e) {
    console.warn('[bridge]', e.message);
  }
}

mc.on('chatline', (line) => {
  const g = parseGuildChat(line);
  if (g) postGuildChat(g.ign, g.message);
});

function waitForSpawn(timeoutMs = 25000) {
  return new Promise((resolve, reject) => {
    if (mc.isReady()) return resolve();
    const t = setTimeout(() => {
      mc.off('spawn', onSpawn);
      reject(new Error('timed out waiting for spawn'));
    }, timeoutMs);
    const onSpawn = () => {
      clearTimeout(t);
      resolve();
    };
    mc.once('spawn', onSpawn);
  });
}

async function switchRegionAndServer({ region, server }) {
  if (switchInFlight) {
    throw new Error('switch already in progress — wait a few seconds');
  }
  switchInFlight = true;
  try {
    if (server) {
      const srv = String(server).toLowerCase();
      if (!['sword', 'nethpot'].includes(srv)) {
        throw new Error('server must be sword or nethpot');
      }
      currentServer = srv;
      mc.setPendingServer(srv);
    }

    if (region) {
      const key = String(region).toLowerCase();
      const host = REGION_HOSTS[key];
      if (!host) throw new Error('unknown region "' + region + '" (use eu or as)');

      if (host !== currentHost) {
        console.log('[switch] region ' + key + ' → ' + host);
        currentHost = host;
        mc.reconnectTo(host, MC_PORT ? Number(MC_PORT) : 25565);
        await waitForSpawn(30000);
        await new Promise(function (r) { setTimeout(r, 2500); });
      } else if (server) {
        await new Promise(function (r) { setTimeout(r, 400); });
        if (!mc.isReady()) throw new Error('bot not ready');
        console.log('[switch] /server ' + currentServer);
        mc.sendChat('/server ' + currentServer);
      }
    } else if (server) {
      await new Promise(function (r) { setTimeout(r, 400); });
      if (!mc.isReady()) throw new Error('bot not ready');
      console.log('[switch] /server ' + currentServer);
      mc.sendChat('/server ' + currentServer);
    }

    return {
      ok: true,
      host: currentHost,
      server: currentServer || server || null,
      region: region || null,
    };
  } finally {
    switchInFlight = false;
  }
}

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
    host: currentHost,
    server: currentServer,
    players: mc.isReady() ? mc.getOnlinePlayers().length : 0,
  });
});

app.get('/players', (req, res) => {
  try {
    if (!mc.isReady()) return res.status(503).json({ error: 'not connected' });
    res.json({ players: mc.getOnlinePlayers(), host: currentHost, server: currentServer });
  } catch (e) {
    res.status(503).json({ error: e.message });
  }
});

app.post('/switch', async (req, res) => {
  const { region, server } = req.body || {};
  if (!region && !server) {
    return res.status(400).json({ error: 'need region and/or server' });
  }
  try {
    const result = await switchRegionAndServer({ region, server });
    res.json(result);
  } catch (e) {
    console.error('[switch]', e.message);
    res.status(503).json({ error: e.message });
  }
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
app.listen(port, () => console.log(`[agent] :${port} host=${currentHost}`));
