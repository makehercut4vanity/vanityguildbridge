const mineflayer = require('mineflayer');
const EventEmitter = require('events');

const RECONNECT_BASE_DELAY_MS = 5000;
const RECONNECT_MAX_DELAY_MS = 60000;
const JOIN_SERVER_DELAY_MS = 2500;

class MinecraftClient extends EventEmitter {
  constructor(opts) {
    super();
    this.opts = { ...opts };
    this.bot = null;
    this.reconnectAttempts = 0;
    this.manuallyStopped = false;
    this.pendingCollectors = new Set();
    this.hasSentJoin = false;
    this.pendingServerCommand = null; // e.g. "nethpot" to run instead of default sword
  }

  connect() {
    this.manuallyStopped = false;
    this.hasSentJoin = false;
    console.log(`[MC] connect ${this.opts.host}:${this.opts.port || 25565} as ${this.opts.username}`);

    this.bot = mineflayer.createBot({
      host: this.opts.host,
      port: this.opts.port || 25565,
      username: this.opts.username,
      auth: this.opts.auth || 'microsoft',
      version: this.opts.version || false,
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

    this.bot.on('kicked', (reason) => {
      console.warn('[MC] kicked', reason);
      this.emit('kicked', reason);
    });

    this.bot.on('error', (err) => console.error('[MC]', err.message));

    this.bot.on('end', (reason) => {
      console.warn('[MC] end', reason);
      this.emit('disconnected', reason);
      if (!this.manuallyStopped) this.scheduleReconnect();
    });
  }

  /** Hard reconnect to a different host (region switch). */
  reconnectTo(host, port) {
    console.log(`[MC] reconnectTo ${host}:${port || this.opts.port || 25565}`);
    this.opts.host = host;
    if (port) this.opts.port = port;
    this.manuallyStopped = true; // prevent auto-reconnect using old session
    this.hasSentJoin = false;
    try {
      if (this.bot) this.bot.quit();
    } catch (_) {}
    this.bot = null;
    // allow connect() to run fresh
    setTimeout(() => {
      this.manuallyStopped = false;
      this.connect();
    }, 800);
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

  /** Next hub spawn will /server this instead of sword. */
  setPendingServer(name) {
    this.pendingServerCommand = name;
  }

  scheduleReconnect() {
    this.reconnectAttempts += 1;
    const delay = Math.min(RECONNECT_BASE_DELAY_MS * this.reconnectAttempts, RECONNECT_MAX_DELAY_MS);
    console.log(`[MC] reconnect ${delay / 1000}s`);
    setTimeout(() => this.connect(), delay);
  }

  stop() {
    this.manuallyStopped = true;
    if (this.bot) this.bot.quit();
  }

  isReady() {
    return !!(this.bot && this.bot.player);
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
