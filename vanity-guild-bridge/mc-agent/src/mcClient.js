const mineflayer = require('mineflayer');
const EventEmitter = require('events');

const RECONNECT_BASE_DELAY_MS = 5000;
const RECONNECT_MAX_DELAY_MS = 60000;
const JOIN_SERVER_DELAY_MS = 2500;

function isPartialRead(err) {
  if (!err) return false;
  const msg = err.message || String(err);
  const name = err.name || '';
  return (
    name === 'PartialReadError' ||
    msg.includes('PartialReadError') ||
    msg.includes('partial packet') ||
    msg.includes('Missing characters in string') ||
    msg.includes('Chunk size is')
  );
}

// Don't let protocol noise kill the process
process.on('uncaughtException', (err) => {
  if (isPartialRead(err)) {
    console.warn('[MC] suppressed PartialReadError:', (err.message || '').slice(0, 120));
    return;
  }
  console.error('[MC] uncaughtException:', err);
});

process.on('unhandledRejection', (err) => {
  if (isPartialRead(err)) {
    console.warn('[MC] suppressed PartialRead rejection:', String(err).slice(0, 120));
    return;
  }
  console.error('[MC] unhandledRejection:', err);
});

class MinecraftClient extends EventEmitter {
  constructor(opts) {
    super();
    this.opts = { ...opts };
    this.bot = null;
    this.reconnectAttempts = 0;
    this.manuallyStopped = false;
    this.pendingCollectors = new Set();
    this.hasSentJoin = false;
    this.pendingServerCommand = null;
  }

  connect() {
    this.manuallyStopped = false;
    this.hasSentJoin = false;
    const host = this.opts.host;
    const port = this.opts.port || 25565;
    console.log(`[MC] connect ${host}:${port} as ${this.opts.username}`);

    const options = {
      host,
      port,
      username: this.opts.username,
      auth: this.opts.auth || 'microsoft',
      hideErrors: true,
      checkTimeoutInterval: 60_000,
      // Keep physics light — we only need chat/commands
      physicsEnabled: false,
    };

    // Pin version if set (e.g. "1.20.4" / "1.21.1"). Empty/false = auto-detect.
    if (this.opts.version) {
      options.version = this.opts.version;
    }

    this.bot = mineflayer.createBot(options);

    // Swallow protocol parse noise so tab-list / hat packets don't crash us
    this._patchClientErrors(this.bot);

    this.bot.once('login', () => {
      console.log('[MC] login ok');
    });

    this.bot.once('spawn', () => {
      this.reconnectAttempts = 0;
      console.log('[MC] spawn (hub)');
      this.emit('spawn');
      this.scheduleJoin();
    });

    this.bot.on('spawn', () => {
      if (this.hasSentJoin) {
        console.log('[MC] spawn (destination)');
        this.emit('spawn');
      }
    });

    this.bot.on('message', (jsonMsg) => {
      const line = jsonMsg.toString();
      if (!line) return;
      this.emit('chatline', line);
      for (const c of this.pendingCollectors) c.push(line);
    });

    // Some servers use system chat / playerChat instead of classic message
    this.bot.on('messagestr', (msg) => {
      if (!msg) return;
      this.emit('chatline', msg);
      for (const c of this.pendingCollectors) c.push(msg);
    });

    this.bot.on('kicked', (reason) => {
      console.warn('[MC] kicked', reason);
      this.emit('kicked', reason);
    });

    this.bot.on('error', (err) => {
      if (isPartialRead(err)) {
        console.warn('[MC] protocol noise (ignored):', (err.message || '').slice(0, 100));
        return;
      }
      console.error('[MC] error:', err.message || err);
      this.emit('error', err);
    });

    this.bot.on('end', (reason) => {
      console.warn('[MC] end', reason);
      this.emit('disconnected', reason);
      if (!this.manuallyStopped) this.scheduleReconnect();
    });
  }

  _patchClientErrors(bot) {
    try {
      const client = bot._client;
      if (!client || client.__vanityPatched) return;
      client.__vanityPatched = true;

      const originalEmit = client.emit.bind(client);
      client.emit = (event, ...args) => {
        if (event === 'error' && args[0] && isPartialRead(args[0])) {
          console.warn('[MC] client PartialRead suppressed');
          return true;
        }
        return originalEmit(event, ...args);
      };
    } catch (e) {
      console.warn('[MC] could not patch client errors:', e.message);
    }
  }

  /** Hard reconnect to a different host (region switch). */
  reconnectTo(host, port) {
    console.log(`[MC] reconnectTo ${host}:${port || this.opts.port || 25565}`);
    this.opts.host = host;
    if (port) this.opts.port = port;
    this.manuallyStopped = true;
    this.hasSentJoin = false;
    try {
      if (this.bot) this.bot.quit('region switch');
    } catch (_) {}
    this.bot = null;
    setTimeout(() => {
      this.manuallyStopped = false;
      this.connect();
    }, 1000);
  }

  scheduleJoin() {
    if (this.hasSentJoin) return;
    setTimeout(() => {
      if (!this.isReady() || this.hasSentJoin) return;
      this.hasSentJoin = true;
      const target = this.pendingServerCommand || 'sword';
      this.pendingServerCommand = null;
      try {
        console.log(`[MC] /server ${target}`);
        this.sendChat(`/server ${target}`);
      } catch (e) {
        this.hasSentJoin = false;
        console.error('[MC] join fail', e.message);
      }
    }, JOIN_SERVER_DELAY_MS);
  }

  setPendingServer(name) {
    this.pendingServerCommand = name;
  }

  scheduleReconnect() {
    this.reconnectAttempts += 1;
    const delay = Math.min(
      RECONNECT_BASE_DELAY_MS * this.reconnectAttempts,
      RECONNECT_MAX_DELAY_MS
    );
    console.log(`[MC] reconnect in ${delay / 1000}s (attempt ${this.reconnectAttempts})`);
    setTimeout(() => this.connect(), delay);
  }

  stop() {
    this.manuallyStopped = true;
    if (this.bot) {
      try {
        this.bot.quit();
      } catch (_) {}
    }
  }

  isReady() {
    return !!(this.bot && this.bot.entity);
  }

  sendChat(command) {
    if (!this.isReady()) throw new Error('not connected');
    this.bot.chat(command);
  }

  getOnlinePlayers() {
    if (!this.isReady()) return [];
    const self = (this.bot.username || '').toLowerCase();
    const out = [];
    const seen = new Set();
    for (const name of Object.keys(this.bot.players || {})) {
      if (!name) continue;
      const low = name.toLowerCase();
      if (low === self || seen.has(low)) continue;
      seen.add(low);
      out.push(name);
    }
    return out;
  }

  runCommandAndCollect(command, { timeoutMs = 8000, quietMs = 1200 } = {}) {
    return new Promise((resolve, reject) => {
      if (!this.isReady()) return reject(new Error('not connected'));
      const lines = [];
      let quietTimer = null;
      let hardTimer = null;
      const collector = {
        push: (line) => {
          lines.push(line);
          if (quietTimer) clearTimeout(quietTimer);
          quietTimer = setTimeout(finish, quietMs);
        },
      };
      const finish = () => {
        clearTimeout(quietTimer);
        clearTimeout(hardTimer);
        this.pendingCollectors.delete(collector);
        resolve(lines);
      };
      hardTimer = setTimeout(finish, timeoutMs);
      this.pendingCollectors.add(collector);
      try {
        this.sendChat(command);
      } catch (err) {
        this.pendingCollectors.delete(collector);
        clearTimeout(quietTimer);
        clearTimeout(hardTimer);
        return reject(err);
      }
      quietTimer = setTimeout(finish, quietMs);
    });
  }
}

module.exports = MinecraftClient;
