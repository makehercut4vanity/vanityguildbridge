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

const app = express();
app.use(express.json());

// Simple shared-secret auth so nobody else can drive your Minecraft account
// if this service ends up with a public Railway URL.
app.use((req, res, next) => {
  if (req.headers['x-agent-secret'] !== AGENT_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  next();
});

app.get('/health', (req, res) => {
  res.json({ connected: mc.isReady() });
});

// Body: { "command": "/g list", "timeoutMs": 8000, "quietMs": 1200 }
// Response: { "lines": ["...", "..."] }
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

const port = AGENT_PORT ? Number(AGENT_PORT) : 3000;
app.listen(port, () => {
  console.log(`[Agent] Listening on port ${port}`);
});
