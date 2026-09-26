const mineflayer = require('mineflayer');
const EventEmitter = require('events');

const RECONNECT_BASE_DELAY_MS = 5000;
const RECONNECT_MAX_DELAY_MS = 60000;
// Delay after spawn before sending /server sword (lets the hub fully load)
const JOIN_SWORD_DELAY_MS = 2500;

class MinecraftClient extends EventEmitter {
  constructor(opts) {
    super();
    this.opts = opts;
    this.bot = null;
    this.reconnectAttempts = 0;
    this.manuallyStopped = false;
    this.pendingCollectors = new Set();
    this.hasSentJoinSword = false;
  }

  connect() {
    this.manuallyStopped = false;
    this.hasSentJoinSword = false;
    console.log(`[MC] Connecting to ${this.opts.host}:${this.opts.port || 25565} as ${this.opts.username}...`);

    this.bot = mineflayer.createBot({
      host: this.opts.host,
      port: this.opts.port || 25565,
      username: this.opts.username,
      auth: this.opts.auth || 'microsoft',
      version: this.opts.version || false,
    });

    this.bot.once('spawn', () => {
      this.reconnectAttempts = 0;
      console.log('[MC] Spawned in world.');
      this.emit('spawn');
      this.scheduleJoinSword();
    });

    // Also handle a second spawn after /server sword moves us to Sword FFA
    this.bot.on('spawn', () => {
      if (this.hasSentJoinSword) {
        console.log('[MC] Spawned on destination server (Sword FFA) — AFKing.');
        this.emit('spawn');
      }
    });

    this.bot.on('message', (jsonMsg) => {
      const line = jsonMsg.toString();
      if (!line) return;
      this.emit('chatline', line);
      for (const collector of this.pendingCollectors) collector.push(line);
    });

    this.bot.on('kicked', (reason) => {
      console.warn('[MC] Kicked:', reason);
      this.emit('kicked', reason);
    });

    this.bot.on('error', (err) => {
      console.error('[MC] Error:', err.message);
      this.emit('error', err);
    });

    this.bot.on('end', (reason) => {
      console.warn('[MC] Disconnected:', reason);
      this.emit('disconnected', reason);
      if (!this.manuallyStopped) this.scheduleReconnect();
    });
  }

  scheduleJoinSword() {
    if (this.hasSentJoinSword) return;
    setTimeout(() => {
      if (!this.isReady() || this.hasSentJoinSword) return;
      this.hasSentJoinSword = true;
      try {
        console.log('[MC] Sending /server sword → Sword FFA');
        this.sendChat('/server sword');
      } catch (err) {
        console.error('[MC] Failed to send /server sword:', err.message);
        this.hasSentJoinSword = false;
      }
    }, JOIN_SWORD_DELAY_MS);
  }

  scheduleReconnect() {
    this.reconnectAttempts += 1;
    const delay = Math.min(RECONNECT_BASE_DELAY_MS * this.reconnectAttempts, RECONNECT_MAX_DELAY_MS);
    console.log(`[MC] Reconnecting in ${delay / 1000}s (attempt ${this.reconnectAttempts})...`);
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
    if (!this.isReady()) throw new Error('Minecraft bot is not connected yet.');
    this.bot.chat(command);
  }

  runCommandAndCollect(command, { timeoutMs = 8000, quietMs = 1200 } = {}) {
    return new Promise((resolve, reject) => {
      if (!this.isReady()) return reject(new Error('Minecraft bot is not connected yet.'));

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