'use strict';

const $ = (id) => document.getElementById(id);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const clamp = (value, min, max) => Math.max(min, Math.min(max, Number(value) || 0));

const renderedText = new WeakMap();
const text = (node, value) => {
  if (!node) return;
  const next = String(value ?? '');
  if (renderedText.get(node) === next) return;
  renderedText.set(node, next);
  node.textContent = next;
};
const setClass = (node, name, enabled) => node && node.classList.toggle(name, Boolean(enabled));
const finite = (value) => Number.isFinite(Number(value));
const plainObject = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
function round2(value) { return Math.round((Number(value) + Number.EPSILON) * 100) / 100; }
function cmFromMm(mm) { return round2(Number(mm || 0) / 10); }
function mmFromCm(cm) { return round2(Number(cm) * 10); }
function inchesFromCm(cm) { return round2(Number(cm) / 2.54); }
function formatLength(mm) { return `${cmFromMm(mm).toFixed(2)} cm`; }
function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
function orcaSlot(tool) {
  const value = Number(tool);
  return Number.isInteger(value) && value >= 0 ? value + 1 : null;
}
function orcaSlotLabel(tool, diagnostic = false) {
  const slot = orcaSlot(tool);
  if (slot === null) return diagnostic ? 'unknown' : '?';
  return `${slot} (T${Number(tool)})`;
}

function firmwareBlockReason(device) {
  if (device?.firmware_compatible !== false) return '';
  return String(device?.firmware_error || 'Compatible BMCU firmware required').trim();
}
function syncLengthConversions() {
  const sync = (sourceId, targetId) => {
    const source = $(sourceId);
    const target = $(targetId);
    if (!source || !target) return;
    const raw = source.value.trim();
    const value = Number(raw);
    target.value = raw !== '' && finite(value) ? inchesFromCm(value).toFixed(2) : '';
  };
  sync('editUnloadRetract', 'editUnloadRetractInches');
  sync('editAutoload', 'editAutoloadInches');
}
function deviceNumericId(deviceOrName) {
  const name = typeof deviceOrName === 'string' ? deviceOrName : deviceOrName?.name;
  const match = /^bmcu(\d+)$/i.exec(String(name || ''));
  return match ? Number(match[1]) : null;
}

function compareDevices(left, right) {
  const leftId = deviceNumericId(left);
  const rightId = deviceNumericId(right);
  if (leftId !== null && rightId !== null && leftId !== rightId) return leftId - rightId;
  if (leftId !== null && rightId === null) return -1;
  if (leftId === null && rightId !== null) return 1;
  return String(left?.name || '').localeCompare(String(right?.name || ''));
}

function deviceLabel(deviceOrName) {
  const name = typeof deviceOrName === 'string' ? deviceOrName : deviceOrName?.name;
  const numericId = deviceNumericId(deviceOrName);
  let index = numericId !== null ? numericId : devices().findIndex((device) => device.name === name);
  if (index < 0) index = 0;
  let value = index + 1;
  let letters = '';
  while (value > 0) { value -= 1; letters = String.fromCharCode(65 + (value % 26)) + letters; value = Math.floor(value / 26); }
  return `BMCU-${letters || 'A'}`;
}

function cloneState(value) {
  if (Array.isArray(value)) return value.map(cloneState);
  if (!plainObject(value)) return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) result[key] = cloneState(item);
  return result;
}

const CAL_STAGE = {
  0: 'Preparing',
  1: 'Release all buffers - reading neutral position',
  2: 'Move the lit buffer to one end, then release it',
  3: 'Release the lit buffer',
  4: 'Move the lit buffer to the opposite end, then release it',
  5: 'Release the lit buffer',
  6: 'Testing motor and magnetic encoder, then saving',
};

class StateEngine {
  constructor() {
    this.state = {
      status: null,
      config: {},
      connection: 'starting',
      lastUpdate: 0,
    };
    this.listeners = new Set();
    this.frame = 0;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  patch(values) {
    Object.assign(this.state, values);
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      for (const listener of this.listeners) listener(this.state);
    });
  }

  replaceStatus(status, connection = this.state.connection) {
    this.patch({status: cloneState(status), connection, lastUpdate: Date.now()});
  }

  mergeStatus(update, connection = this.state.connection) {
    if (!plainObject(update)) return;
    const current = plainObject(this.state.status) ? this.state.status : {};
    const merged = {...current};
    for (const [key, value] of Object.entries(update)) merged[key] = cloneState(value);
    this.patch({status: merged, connection, lastUpdate: Date.now()});
  }

  mergeTransportStatus(payload) {
    if (!plainObject(payload) || !Array.isArray(payload.devices) ||
        !plainObject(this.state.status)) return;
    const merged = cloneState(this.state.status);
    const liveByName = new Map(payload.devices
      .filter((item) => plainObject(item) && item.name)
      .map((item) => [String(item.name), item]));
    if (!Array.isArray(merged.devices)) return;
    for (const device of merged.devices) {
      const live = liveByName.get(String(device.name));
      if (!live) continue;
      device.transport_online = Boolean(live.online);
      device.transport_process_online = Boolean(live.process_online);
      device.transport_socket_online = Boolean(live.socket_online);
      device.transport_generation = Number(live.generation || 0);
      device.transport_updated_at = Number(live.updated_at || 0);
      device.transport_port = String(live.port || '');
      const raw = plainObject(live.status) ? live.status : null;
      if (!raw) continue;
      device.transport_session_id = Number(raw.session_id || live.session_id || 0);
      device.nvm_fault = Boolean(raw.nvm_fault);
      device.error_flags = Number(raw.error_flags || 0);
      const arrays = ['present', 'buffer_pct', 'buffer_raw',
        'motor_pwm', 'motion'];
      const routeNames = ['EMPTY', 'LOADED', 'UNCERTAIN'];
      for (const channel of (device.channels || [])) {
        const index = Number(channel.channel);
        if (!Number.isInteger(index) || index < 0 || index > 3) continue;
        for (const key of arrays) {
          if (Array.isArray(raw[key]) && raw[key].length > index) {
            channel[key] = key === 'present'
              ? Boolean(Number(raw[key][index]))
              : raw[key][index];
          }
        }
        if (Array.isArray(raw.meters) && raw.meters.length > index) {
          channel.travel_meters = raw.meters[index];
        }
        if (Array.isArray(raw.route_state) && raw.route_state.length > index) {
          const route = Number(raw.route_state[index]);
          channel.raw_route_state = route;
          channel.route_state_name = routeNames[route] || 'UNKNOWN';
          channel.loaded = route === 1;
          channel.uncertain = route === 2;
        }
        if (finite(raw.connected_mask)) {
          channel.connected = Boolean(Number(raw.connected_mask) & (1 << index));
        }
        if (finite(raw.encoder_io_mask)) {
          channel.encoder_io_ok = Boolean(Number(raw.encoder_io_mask) & (1 << index));
        }
        if (finite(raw.calibration_valid_mask)) {
          channel.calibration_valid = Boolean(Number(raw.calibration_valid_mask) & (1 << index));
        }
      }
    }
    this.patch({status: merged, lastUpdate: Date.now()});
  }
}

class LiveTransport {
  constructor(store) {
    this.store = store;
    this.socket = null;
    this.socketGeneration = 0;
    this.lastEventtime = null;
    this.snapshotGeneration = 0;
    this.awaitingSnapshot = false;
    this.pollTimer = 0;
    this.reconnectTimer = 0;
    this.polling = false;
    this.refreshPending = false;
    this.failures = 0;
    this.config = null;
    this.rpcId = 1;
    this.transportPollTimer = 0;
    this.transportPolling = false;
  }

  async start(config) {
    this.config = config || {};
    await this.refreshNow();
    await this.refreshTransportNow();
    this.connectWebSocket();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.refreshSoon(0);
        this.scheduleTransportPoll(0);
        if (typeof WebSocket !== 'undefined' &&
            (!this.socket || this.socket.readyState > WebSocket.OPEN)) {
          this.connectWebSocket();
        }
      }
    });
  }

  scheduleTransportPoll(delay = 750) {
    clearTimeout(this.transportPollTimer);
    this.transportPollTimer = setTimeout(
      () => this.refreshTransportNow(), Math.max(0, Number(delay) || 0));
  }

  async refreshTransportNow() {
    if (this.transportPolling) return;
    this.transportPolling = true;
    try {
      const response = await fetch('/api/transport-status', {cache: 'no-store'});
      const payload = await response.json();
      if (response.ok) this.store.mergeTransportStatus(payload);
    } catch (_) {

    } finally {
      this.transportPolling = false;
      this.scheduleTransportPoll(document.hidden ? 5000 : 750);
    }
  }

  websocketUrl() {
    const cfg = this.config?.moonraker_websocket;
    if (!cfg) return '';
    const host = cfg.use_page_host ? location.hostname : cfg.host;
    const port = Number(cfg.port);
    if (!host || !port) return '';
    return `${cfg.scheme || 'ws'}://${host}:${port}${cfg.path || '/websocket'}`;
  }

  socketLive() {
    return typeof WebSocket !== 'undefined' &&
      this.socket?.readyState === WebSocket.OPEN;
  }

  connectWebSocket() {
    clearTimeout(this.reconnectTimer);
    const url = this.websocketUrl();
    if (!url || typeof WebSocket === 'undefined') {
      this.schedulePoll(1800);
      return;
    }
    if (this.socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(this.socket.readyState)) return;
    try {
      const socket = new WebSocket(url);
      const generation = ++this.socketGeneration;
      this.socket = socket;
      socket.addEventListener('open', () => {
        if (this.socket !== socket || generation !== this.socketGeneration) return;
        this.failures = 0;
        this.awaitingSnapshot = true;
        this.store.patch({connection: 'starting'});
        socket.send(JSON.stringify({
          jsonrpc: '2.0',
          method: 'printer.objects.subscribe',
          params: {objects: {bmcu: null}},
          id: this.rpcId++,
        }));
      });
      socket.addEventListener('message', (event) => {
        if (this.socket === socket && generation === this.socketGeneration) {
          this.onSocketMessage(event, generation);
        }
      });
      socket.addEventListener('error', () => socket.close());
      socket.addEventListener('close', () => {
        if (this.socket !== socket || generation !== this.socketGeneration) return;
        this.socket = null;
        this.socketGeneration += 1;
        this.awaitingSnapshot = true;
        this.store.patch({connection: 'starting'});
        this.schedulePoll(0);
        this.reconnectTimer = setTimeout(
          () => this.connectWebSocket(),
          Math.min(15000, 1500 * (this.failures + 1)));
      });
    } catch (_) {
      this.schedulePoll(800);
    }
  }

  onSocketMessage(event, generation) {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    const initial = payload?.result?.status?.bmcu;
    if (plainObject(initial)) {
      if (generation !== this.socketGeneration) return;
      const eventtime = Number(payload?.result?.eventtime);
      this.lastEventtime = finite(eventtime) ? eventtime : null;
      this.awaitingSnapshot = false;
      this.store.replaceStatus(initial, 'live');
      return;
    }
    if (payload?.method !== 'notify_status_update') return;
    const update = payload.params?.[0]?.bmcu;
    if (!plainObject(update) || this.awaitingSnapshot) return;
    const eventtime = Number(payload.params?.[1]);
    if (finite(eventtime) && finite(this.lastEventtime)) {
      if (eventtime < this.lastEventtime - 5) {

        this.lastEventtime = null;
        this.snapshotGeneration += 1;
        this.awaitingSnapshot = true;
        this.store.patch({connection: 'starting'});
        this.refreshSoon(0);
        return;
      }
      if (eventtime < this.lastEventtime) return;
    }
    if (finite(eventtime)) this.lastEventtime = eventtime;
    this.store.mergeStatus(update, 'live');
  }

  schedulePoll(delay = 2500) {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.refreshNow(), delay);
  }

  refreshSoon(delay = 120) {
    this.schedulePoll(delay);
  }

  async refreshNow() {
    if (this.polling) {
      this.refreshPending = true;
      return;
    }
    this.polling = true;
    const requestGeneration = this.socketGeneration;
    const requestSnapshotGeneration = this.snapshotGeneration;
    try {
      const response = await fetch('/moonraker/printer/objects/query?bmcu', {cache: 'no-store'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || `HTTP ${response.status}`);
      this.failures = 0;
      const socketLive = this.socketLive();
      if (requestSnapshotGeneration !== this.snapshotGeneration) return;
      if (socketLive && requestGeneration !== this.socketGeneration) return;
      const eventtime = Number(payload?.result?.eventtime);
      if (socketLive && !this.awaitingSnapshot && finite(eventtime) &&
          finite(this.lastEventtime) && eventtime < this.lastEventtime) {
        if (eventtime >= this.lastEventtime - 5) return;

        this.lastEventtime = null;
      }
      if (finite(eventtime)) this.lastEventtime = eventtime;
      this.awaitingSnapshot = false;
      this.store.replaceStatus(payload?.result?.status?.bmcu || null,
        socketLive ? 'live' : 'polling');
    } catch (_) {
      this.failures += 1;
      if (!this.socketLive() || this.awaitingSnapshot) {
        this.store.patch({connection: 'offline'});
      }
    } finally {
      this.polling = false;
      if (this.refreshPending) {
        this.refreshPending = false;
        setTimeout(() => this.refreshNow(), 0);
        return;
      }
      const active = this.store.state.status?.active_operations;
      const operationActive = plainObject(active) && Object.keys(active).length > 0;
      if (this.socketLive()) {

        this.schedulePoll(operationActive ? 15000 :
          (document.hidden ? 120000 : 60000));
      } else if (operationActive) {
        this.schedulePoll(400);
      } else {
        const base = document.hidden ? 8000 : 2500;
        this.schedulePoll(Math.min(15000, base + this.failures * 1500));
      }
    }
  }
}

const store = new StateEngine();
const transport = new LiveTransport(store);
const ui = {
  view: 'dashboard',
  busy: false,
  routeDraft: new Map(),
  ledDraft: new Map(),
  lightingProfile: new Map(),
  preferenceDraft: null,
  u1GcodeDraft: new Map(),
  u1MaterialProfile: 'DEFAULT',
  editing: null,
  confirmCallback: null,
  confirmCancelCallback: null,
  updateTimer: 0,
  pendingFirmwareUpload: null,
  firmwareUploadRunning: false,
  updateInProgress: false,
  updatePort: '',
  suppressUnknownPort: '',
  suppressUnknownUntil: 0,
  flashRestartRequested: false,
  pendingCalibration: null,
  calibrationStarting: false,
  channelOperations: new Map(),
  ledPreview: new Map(),
  lightingPicker: null,
  genericEndpointDraft: new Map(),
  remoteVersions: null,
  versionChecking: false,
};

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`.trim();
  node.textContent = String(message || '');
  $('toastRegion').appendChild(node);
  setTimeout(() => node.remove(), 5000);
}

function gcodeValue(value) {
  const clean = String(value ?? '')
    .replace(/[\r\n;#*\x00-\x1f\x7f]/g, ' ')
    .replace(/\s+/g, ' ').trim().slice(0, 160);
  if (!clean) return '""';
  return /\s/.test(clean) ? `"${clean.replace(/["\\]/g, '_')}"` : clean;
}

function openDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.showModal === 'function') {
    if (!dialog.open) dialog.showModal();
    return;
  }
  dialog.setAttribute('open', '');
  dialog.classList.add('dialog-fallback-open');
  document.documentElement.classList.add('dialog-open');
}

function closeDialog(dialog) {
  if (!dialog) return;
  if (dialog.id === 'channelDialog') ui.editing = null;
  if (typeof dialog.close === 'function' && dialog.open) dialog.close();
  else dialog.removeAttribute('open');
  dialog.classList.remove('dialog-fallback-open');
  if (!document.querySelector('dialog[open]')) document.documentElement.classList.remove('dialog-open');
}

function status() { return store.state.status || {}; }
function devices() { return Array.isArray(status().devices) ? status().devices.slice().sort(compareDevices) : []; }
function endpoints() { return status().endpoints && typeof status().endpoints === 'object' ? status().endpoints : {}; }
function printerInfo() { return status().printer_analysis || {}; }
function allChannels() {
  return devices().flatMap((device) => (device.channels || []).map((channel) => ({device, channel})));
}
function keyFor(device, channel) { return `${device.name}:${Number(channel.channel)}`; }
function deviceReady(device) { return Boolean(device?.connected && device?.ready && device?.runtime_configured); }
function routeState(channel) { return String(channel?.route_state_name || channel?.state || 'UNKNOWN').toUpperCase(); }
function physicalChannel(channel) { return Number(channel?.channel) + 1; }
function virtualTool(channel) {
  const value = Number(channel?.virtual_tool);
  return Number.isInteger(value) && value >= 0 ? value : null;
}
function sourceTitle(channel) { return `Channel ${physicalChannel(channel)}`; }
function endpointFor(channel) { return endpoints()[channel?.endpoint] || {}; }

function channelOperation(device, channel) {
  const key = keyFor(device, channel);
  const local = ui.channelOperations.get(key);
  if (local) {
    const finished = (local.kind === 'loading' && channel.loaded) ||
      (local.kind === 'unloading' && !channel.loaded && routeState(channel) === 'EMPTY') ||
      (local.kind === 'retracting' && !channel.present && routeState(channel) === 'EMPTY');
    const timeoutMs = local.kind === 'retracting' ? 310000 : 180000;
    if (finished || Date.now() - Number(local.started || 0) > timeoutMs) {
      ui.channelOperations.delete(key);
    } else {
      return local;
    }
  }
  const active = status().active_operations?.[device.name];
  if (!plainObject(active)) return null;
  const phase = String(active.phase || '').toUpperCase();
  if (active.background) {
    const sourceMatch = String(active.source_device || '') === String(device.name) &&
      Number(active.source_channel) === Number(channel.channel);
    const targetMatch = String(active.target_device || '') === String(device.name) &&
      Number(active.target_channel) === Number(channel.channel);
    if (sourceMatch && ['BACKGROUND_RELEASE', 'BACKGROUND_PULLBACK'].includes(phase)) {
      return {kind: 'unloading', label: 'Preparing next filament - unloading', started: Date.now(), remote: true};
    }
    if (targetMatch && phase === 'BACKGROUND_PRESTAGE') {
      return {kind: 'loading', label: 'Preparing next filament - loading', started: Date.now(), remote: true};
    }
  }
  if (String(active.endpoint || '') !== String(channel.endpoint || '') ||
      Number(active.channel) !== Number(channel.channel)) return null;
  const name = String(active.name || '').toUpperCase();
  if (name.startsWith('UNLOAD ')) return {kind: 'unloading', started: Date.now(), remote: true};
  if (name.startsWith('RETRACT_INPUT ')) return {kind: 'retracting', started: Date.now(), remote: true};
  if (name.startsWith('LOAD ') || name.startsWith('TOOL_SWITCH ')) return {kind: 'loading', started: Date.now(), remote: true};
  return null;
}

function setChannelOperation(deviceName, channelIndex, kind) {
  ui.channelOperations.set(`${deviceName}:${Number(channelIndex)}`, {kind, started: Date.now()});
  renderDashboardChannels();
  renderDiagnostics();
}

function clearChannelOperation(deviceName, channelIndex) {
  ui.channelOperations.delete(`${deviceName}:${Number(channelIndex)}`);
  renderDashboardChannels();
  renderDiagnostics();
}

function endpointLabel(name) {
  if (!name) return 'Not connected';
  const endpoint = endpoints()[name] || {};
  const head = finite(endpoint.head_index) ? Number(endpoint.head_index) : /^u1_head\d+$/.test(name) ? Number(name.slice(7)) : -1;
  if (endpoint.driver === 'snapmaker_u1' || head >= 0) return `Head ${head + 1}`;
  if (name === 'left') return 'Left extruder';
  if (name === 'right') return 'Right extruder';
  return endpoint.extruder || name;
}

function endpointNames() {
  const existing = Object.keys(endpoints());
  if (printerInfo().topology === 'snapmaker_u1') {
    const routed = existing.filter((name) => endpoints()[name]?.driver === 'snapmaker_u1');
    return [...new Set([...Array.from({length: 4}, (_, index) => `u1_head${index}`), ...routed])];
  }
  return existing.length ? existing : ['extruder'];
}

function ownerFor(channel) {
  const endpoint = endpointFor(channel);
  return endpoint.owner || {owner: 'unknown', reason: 'Ownership is not reconciled'};
}

function assignedSources(endpointName, endpoint) {
  const routes = Array.isArray(endpoint?.assigned_channels)
    ? endpoint.assigned_channels
    : Array.isArray(endpoint?.owner?.assigned_channels) ? endpoint.owner.assigned_channels : [];
  return routes.map((route) => {
    const device = devices().find((item) => String(item.name) === String(route.device));
    const channel = device?.channels?.find((item) => Number(item.channel) === Number(route.channel));
    return device && channel ? {device, channel} : null;
  }).filter(Boolean).filter(({channel}) => String(channel.endpoint || '') === String(endpointName)).sort((a, b) => {
    const at = virtualTool(a.channel) ?? 999;
    const bt = virtualTool(b.channel) ?? 999;
    return at - bt || deviceLabel(a.device).localeCompare(deviceLabel(b.device)) || Number(a.channel.channel) - Number(b.channel.channel);
  });
}

function u1EndpointOperation(endpointName) {
  return Object.values(status().active_operations || {}).find((operation) => plainObject(operation) &&
    (String(operation.endpoint || '') === String(endpointName) ||
      (Array.isArray(operation.endpoints) && operation.endpoints.map(String).includes(String(endpointName))))) || null;
}

function u1OperationLabel(operation) {
  if (!operation) return '';
  const name = String(operation.name || '').toUpperCase();
  if (operation.refill || name.startsWith('TOOL_SWITCH ')) return 'Changing BMCU source';
  if (name.startsWith('PRINT_END_UNLOAD ') || name.startsWith('UNLOAD ')) return 'Unloading BMCU source';
  if (name.startsWith('LOAD ')) return 'Loading BMCU source';
  if (name.startsWith('PRESTAGE ')) return 'Preparing BMCU source';
  if (name.startsWith('CLEAR_PRESTAGE')) return 'Clearing prepared source';
  if (name.startsWith('ROUTE_FEED ')) return 'Feeding BMCU route';
  return 'BMCU operation in progress';
}

function u1SelectedSource(endpointName, headIndex) {
  const tool = Number(status().active_tool);
  if (!Number.isInteger(tool) || tool < 0) return '';
  if (tool >= 0 && tool < 4) return tool === Number(headIndex) ? `Selected: Stock T${tool}` : '';
  const source = allChannels().find(({channel}) => virtualTool(channel) === tool && String(channel.endpoint || '') === String(endpointName));
  return source ? `Selected: ${deviceLabel(source.device)} Ch ${physicalChannel(source.channel)} (T${tool})` : '';
}

function u1MaterialText(source, captured = false) {
  const material = String(source?.material || '').trim();
  const vendor = String(source?.vendor || '').trim();
  const subtype = String(source?.subtype || '').trim();
  const parts = [];
  if (material) parts.push(material);
  if (vendor && vendor.toLowerCase() !== 'generic' && vendor.toLowerCase() !== material.toLowerCase()) parts.push(vendor);
  if (subtype && subtype.toLowerCase() !== 'generic' && subtype.toLowerCase() !== material.toLowerCase()) parts.push(subtype);
  const value = parts.length ? parts.join(' · ') : 'Material not set';
  return captured ? `Last stock profile: ${value}` : value;
}

function u1PathSummary(endpointName, endpoint, sources) {
  const owner = endpoint?.owner || {};
  const path = owner.path || endpoint?.capabilities?.u1_native_path || {};
  const operation = u1EndpointOperation(endpointName);
  const loaded = sources.filter(({channel}) => routeState(channel) === 'LOADED');
  const uncertain = sources.filter(({channel}) => routeState(channel) === 'UNCERTAIN');
  const occupied = sources.filter(({channel}) => routeState(channel) !== 'EMPTY');
  const tailRepair = sources.find(({channel}) => channel.tail_unrouted);
  const tail = sources.find(({channel}) => channel.tail_detached);
  if (owner.owner === 'error') return {label: 'Ownership error', cls: 'bad', detail: owner.reason || 'Shared path ownership is not valid.'};
  if (owner.owner === 'safety_hold') return {label: 'Safety hold', cls: 'bad', detail: owner.reason || 'Shared path needs attention.'};
  if (occupied.length > 1) return {label: 'Conflicting BMCU routes', cls: 'bad', detail: 'More than one BMCU route reports an occupied or uncertain path for this Head.'};
  if (tailRepair) return {label: 'Route repair required', cls: 'bad', detail: `${deviceLabel(tailRepair.device)} Ch ${physicalChannel(tailRepair.channel)} has an unrouted filament tail.`};
  if (tail) return {label: 'Filament tail in Head path', cls: 'warn', detail: `${deviceLabel(tail.device)} Ch ${physicalChannel(tail.channel)} has a detached downstream tail.`};
  if (operation) return {label: u1OperationLabel(operation), cls: 'warn', detail: 'The shared path is changing state.'};
  if (uncertain.length) return {label: 'BMCU route uncertain', cls: 'warn', detail: `${deviceLabel(uncertain[0].device)} Ch ${physicalChannel(uncertain[0].channel)} needs route confirmation.`};
  if (loaded.length === 1) {
    const source = loaded[0];
    const online = deviceReady(source.device) && source.device.transport_online !== false && source.channel.connected !== false;
    if (owner.owner !== 'bmcu' && owner.owner !== 'bmcu_busy') return {label: 'Ownership mismatch', cls: 'bad', detail: `${deviceLabel(source.device)} Ch ${physicalChannel(source.channel)} reports LOADED but BMCU ownership is not confirmed.`};
    return {label: `${deviceLabel(source.device)} Ch ${physicalChannel(source.channel)} loaded`, cls: online ? 'good' : 'bad', detail: online ? `Shared path is occupied by T${virtualTool(source.channel) ?? '?'}.` : 'Loaded route is last-known because the BMCU source is not fully online.'};
  }
  if (owner.owner === 'bmcu_busy') return {label: 'BMCU path busy', cls: 'warn', detail: owner.reason || 'BMCU controls the shared path.'};
  if (owner.owner === 'bmcu') {
    if (path.known === true && path.busy === false) return {label: 'BMCU reserved · path empty', cls: '', detail: 'Stock can be restored after BMCU releases the shared path.'};
    return {label: 'BMCU controls path', cls: 'warn', detail: owner.reason || 'The source cannot be reduced to a confirmed loaded route.'};
  }
  if (owner.owner === 'native_busy' || owner.owner === 'native') {
    if (path.known !== true) return {label: 'Stock path state unknown', cls: 'warn', detail: owner.reason || 'Snapmaker feeder state is unavailable.'};
    const channelState = String(path.channel_state || '').toLowerCase();
    if (path.busy === true) return channelState === 'load_finish'
      ? {label: 'Stock loaded', cls: 'good', detail: 'Snapmaker stock filament occupies the shared path and remains there until it is manually retracted or actually runs out.'}
      : channelState === 'unload_finish'
        ? {label: 'Stock still in path', cls: 'warn', detail: 'Snapmaker finished its hotend retract/cut sequence, but the stock feeder cannot pull this filament back through the shared branch. Manual retract is required before BMCU uses this Head.'}
        : {label: 'Stock path occupied', cls: 'warn', detail: `Snapmaker feeder state: ${channelState || 'busy'}.`};
    if (path.input_released === true) return {label: 'Stock input clear', cls: 'good', detail: 'The stock feeder reports its input clear. Manual stock withdrawal is user-managed; automatic runout handoff is entered only from the Snapmaker Head runout callback.'};
    return {label: 'Stock feeder available', cls: 'good', detail: 'Snapmaker is not reporting an occupied stock-feed state. This does not claim that the complete manual PTFE path is physically empty.'};
  }
  return {label: 'Path state unknown', cls: 'warn', detail: owner.reason || 'Shared path ownership has not been reconciled.'};
}

function motionLabel(value) {
  const number = Number(value) || 0;
  return ({
    0: 'Stopped',
    1: 'Loading / send out',
    2: 'Before on use',
    3: 'On use / following',
    4: 'Before retracting',
    5: 'Retracting',
    6: 'Stopped on use',
  })[number] || `Unknown (${number})`;
}

function channelState(channel, operation = null) {
  if (operation?.kind === 'loading') return {label: operation.label || 'Loading filament', cls: 'warn'};
  if (operation?.kind === 'unloading') return {label: operation.label || 'Unloading filament', cls: 'warn'};
  if (operation?.kind === 'retracting') return {label: operation.label || 'Retracting input filament', cls: 'warn'};
  if (!channel.connected) return {label: 'Disconnected', cls: 'bad'};
  if (channel.prestaged?.at_entry) return {label: 'Prepared at toolhead entry', cls: 'warn'};
  if (!channel.calibration_valid) return {label: 'Calibration required', cls: 'warn'};
  if (channel.tail_unrouted) return {label: 'Tail route repair required', cls: 'bad'};
  if (channel.tail_detached) return {label: 'Tail in toolhead path', cls: 'warn'};
  if (channel.loaded) return {label: 'Loaded to toolhead', cls: 'good'};
  if (channel.uncertain || routeState(channel) === 'UNCERTAIN') return {label: 'Toolhead route check required', cls: 'warn'};
  if (channel.present) return {label: 'Parked in BMCU', cls: 'good'};
  return {label: 'No filament detected', cls: ''};
}

function u1UncertainLoadCanRecover(channel) {
  if (!channel.present || !(channel.uncertain || routeState(channel) === 'UNCERTAIN')) return false;
  const endpoint = endpoints()[channel.endpoint] || {};
  if (endpoint.driver !== 'snapmaker_u1') return false;
  const path = endpoint?.owner?.path || endpoint?.capabilities?.u1_native_path || {};
  const printBusy = Boolean(status().print_map_active || status().print_plan_open ||
    status().print_transaction_phase || validRecoveryPending());
  return !printBusy && path.known === true && path.busy === false;
}

function loadBlockReason(device, channel) {
  if ((status().controller_mode || 'standalone') !== 'standalone') return status().controller_block_reason || 'BMCU motion ownership is blocked for this printer.';
  if (!deviceReady(device)) return 'BMCU is not ready.';
  if (!channel.endpoint) return 'Connect this channel in Routing.';
  if (!channel.calibration_valid) return 'Calibrate this channel first.';
  if (channel.loaded) return 'This channel is already loaded.';
  if ((channel.uncertain || routeState(channel) === 'UNCERTAIN') && !u1UncertainLoadCanRecover(channel)) {
    return 'The toolhead path is not confirmed empty. Unload the path before loading.';
  }

  const endpoint = endpointFor(channel);
  if (endpoint.driver === 'generic_single_extruder' &&
      (!String(endpoint.toolhead_prepare_macro || '').trim() ||
       !String(endpoint.before_pullback_macro || '').trim())) {
    return 'Configure Toolhead preparation macro and Before pullback macro in Settings.';
  }
  const owner = ownerFor(channel);
  if (endpoint.driver === 'snapmaker_u1') {
    if (owner.owner === 'native') {

      const path = owner.path || endpoint?.capabilities?.u1_native_path || {};
      if (path.known !== true) return owner.reason || 'The native toolhead path state is unknown.';
      if (path.busy === true) return 'Stock filament still occupies this Head. Retract it manually before using BMCU.';
    } else if (owner.owner !== 'bmcu') {
      return owner.reason || 'BMCU cannot safely acquire this toolhead path.';
    }
  }

  if (!channel.present) return `No filament is detected in Channel ${physicalChannel(channel)}.`;
  return '';
}

function validRecoveryPending() {
  const pending = status().u1_cross_refill_pending;
  if (!pending || typeof pending !== 'object' || Array.isArray(pending) || Object.keys(pending).length === 0) return null;
  if (String(pending.kind || '').toLowerCase() !== 'bmcu_cross') return null;
  const phase = String(pending.phase || '').trim();
  const source = Number(pending.source_head);
  const replacement = Number(pending.replacement_head);
  return {
    raw: pending,
    phase: phase || 'invalid journal',
    source: Number.isInteger(source) && source >= 0 ? source : null,
    replacement: Number.isInteger(replacement) && replacement >= 0 ? replacement : null,
    complete: Boolean(phase) && Number.isInteger(source) && source >= 0 && Number.isInteger(replacement) && replacement >= 0,
  };
}

function syncKeyed(container, items, keyFn, createFn, updateFn) {
  const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
  const ordered = [];
  for (const item of items) {
    const key = String(keyFn(item));
    let node = existing.get(key);
    if (!node) {
      node = createFn(item, key);
      node.dataset.key = key;
    }
    updateFn(node, item);
    ordered.push(node);
    existing.delete(key);
  }
  for (const node of existing.values()) node.remove();
  ordered.forEach((node, index) => {
    const current = container.children[index] || null;
    if (current !== node) container.insertBefore(node, current);
  });
}

function badge(label, cls = '') {
  const node = document.createElement('span');
  node.className = `badge ${cls}`.trim();
  node.textContent = label;
  return node;
}

function replaceBadges(container, values) {
  const items = values.map(([label, cls], index) => ({index, label, cls}));
  syncKeyed(container, items, (item) => item.index, () => badge(''), (node, item) => {
    node.className = `badge ${item.cls || ''}`.trim();
    text(node, item.label);
  });
}

function setView(name) {
  ui.view = name;
  qsa('.nav-item').forEach((item) => setClass(item, 'active', item.dataset.view === name));
  qsa('.view').forEach((view) => setClass(view, 'active', view.dataset.view === name));
  if (name === 'settings') {
    pollUpdate(false);
    refreshSerialPorts(false);
  }
}

function renderHeader() {
  const list = devices();
  const ready = list.filter(deviceReady).length;
  const dot = $('brandDot');
  dot.className = 'status-dot';
  if (store.state.connection === 'offline') dot.classList.add('error');
  else if (store.state.connection === 'starting' || !store.state.status) dot.classList.add('pending');
  else if (list.length && ready === list.length) dot.classList.add('online');
  else dot.classList.add('warning');
}

function renderNotices() {
  const notices = [];
  if (store.state.connection === 'offline') {
    notices.push({
      key: 'connection-offline', cls: 'error',
      title: 'Printer connection unavailable',
      copy: 'Showing the last known state. Controls will return when Moonraker reconnects.',
    });
  }
  const manualRefill = status().refill?.manual_pending;
  if (manualRefill && Number.isInteger(Number(manualRefill.channel))) {
    const device = devices().find((item) => item.name === manualRefill.device);
    const channelNumber = Number(manualRefill.channel) + 1;
    notices.push({
      key: 'manual-refill', cls: 'warning',
      title: 'Replacement filament required',
      copy: `Insert replacement filament into ${deviceLabel(device || {name: manualRefill.device})} Channel ${channelNumber}, then use the printer's normal Resume. BMCU will complete Load before the print continues.`,
    });
  }
  const recovery = validRecoveryPending();
  if (recovery) {
    if (recovery.complete) {
      const canResume = ['committed', 'resume_preparing', 'resume_ready', 'resume_failed'].includes(recovery.phase);
      notices.push({
        key: 'refill-recovery', cls: 'warning', title: 'U1 BMCU cross-head refill recovery required',
        copy: `Phase ${recovery.phase}: source Head ${recovery.source + 1}, replacement Head ${recovery.replacement + 1}. ${canResume ? 'Verify both physical routes and keep the print paused before resuming.' : 'The transaction cannot be resumed automatically. Do not move filament until the saved print transaction is restored.'}`,
        action: canResume ? ['resume-refill', 'Resume verified refill'] : null,
      });
    } else {
      notices.push({key: 'invalid-recovery', cls: 'error', title: 'Invalid refill recovery journal', copy: 'Recovery data exists but does not contain valid source and replacement toolhead indexes. The panel will not display fabricated Head NaN values.'});
    }
  }
  if (status().print_terminal_unload_pending) {
    const terminalRoutes = new Set(Array.isArray(status().print_loaded_routes) ? status().print_loaded_routes : []);
    const uncertain = allChannels().some(({device, channel}) => {
      const uid = String(device?.uid || '').toUpperCase();
      const key = uid ? `uid:${uid}:${Number(channel?.channel)}` : '';
      return Boolean(key && terminalRoutes.has(key) && routeState(channel) === 'UNCERTAIN');
    });
    if (uncertain) {
      notices.push({key: 'terminal-unload', cls: 'warning', title: 'Previous print left an uncertain route', copy: 'The next print remains blocked until the physical path is confirmed EMPTY or LOADED in Diagnostics.', action: ['open-terminal-recovery', 'Resolve route']});
    } else if (terminalRoutes.size) {
      notices.push({key: 'terminal-unload', cls: 'warning', title: 'Loaded route preserved after previous print', copy: 'A confirmed BMCU route remains loaded. The next print will reuse it or unload it automatically before loading another source on the same path.', action: ['open-terminal-recovery', 'Inspect route']});
    } else {
      notices.push({key: 'terminal-unload', cls: 'warning', title: 'Previous print cleanup marker remains', copy: 'No loaded route is recorded. The next print will clear this stale marker automatically.', action: ['open-terminal-recovery', 'Inspect state']});
    }
  }
  const knownUids = new Set(devices().map((device) => String(device.uid || '').toUpperCase()).filter(Boolean));
  const identityMismatch = devices().filter((device) => {
    const error = String(device.last_error || '');
    const match = error.match(/UID mismatch expected=([0-9A-F]+) got=([0-9A-F]+)/i);
    if (!match) return false;
    const got = String(match[2] || '').toUpperCase();
    return !knownUids.has(got);
  });
  const firmwareBlocked = devices().filter((device) => Boolean(firmwareBlockReason(device)));
  if (identityMismatch.length) {
    notices.push({
      key: 'wrong-bmcu-device', cls: 'error',
      title: 'Different BMCU connected',
      copy: identityMismatch.map((device) => `${deviceLabel(device)}: ${device.last_error}`).join(' | '),
    });
  }
  if (firmwareBlocked.length) {
    notices.push({
      key: 'firmware-required', cls: 'warning',
      title: 'BMCU firmware flash required',
      copy: `BMCU motion, loading, unloading and refill are blocked. Required BMCU firmware: ${store.state.config.required_firmware_version || 'unknown'}. Open Settings and flash the compatible firmware.`,
      action: ['open-firmware-settings', 'Open firmware settings'],
    });
  } else if (!devices().length && rawSerialCandidates().length) {
    notices.push({
      key: 'unknown-ch340', cls: 'warning',
      title: 'USB-TTL adapter detected - firmware unknown',
      copy: 'The host and panel are installed. Identify the adapter by unplugging and reconnecting it, then flash BMCU-Klipper firmware in Settings.',
      action: ['open-firmware-settings', 'Open firmware settings'],
    });
  }
  const uncalibrated = allChannels().filter(({channel}) =>
    channel.connected && !channel.calibration_valid);
  if (uncalibrated.length) {
    const labels = uncalibrated.map(({device, channel}) =>
      `${deviceLabel(device)} Channel ${physicalChannel(channel)}`);
    notices.push({
      key: 'calibration-required', cls: 'warning',
      title: 'BMCU module calibration required',
      copy: `${labels.join(', ')} ${labels.length === 1 ? 'is' : 'are'} not calibrated. Open Settings and calibrate the modules before loading or printing.`,
      action: ['open-calibration-settings', 'Open calibration settings'],
    });
  }
  const unroutedTails = allChannels().filter(({channel}) => channel.tail_unrouted);
  if (unroutedTails.length) {
    notices.push({
      key: 'tail-unrouted', cls: 'error',
      title: 'Restore the tail route',
      copy: unroutedTails.map(({device, channel}) =>
        `${deviceLabel(device)} Channel ${physicalChannel(channel)} -> ${endpointLabel(channel.tail_endpoint)}`).join(' | '),
    });
  }
  const lastError = status().last_error;

  const unresolvedUncertainRoutes = allChannels().filter(({device, channel}) =>
    (Boolean(channel.uncertain) || routeState(channel) === 'UNCERTAIN') &&
    !channelOperation(device, channel));
  const routeStillUncertain = unresolvedUncertainRoutes.length > 0;
  const interruptedPlan = Boolean(status().print_plan_interrupted && !status().print_map_active);
  if (routeStillUncertain) {
    notices.push({
      key: 'route-uncertain', cls: 'warning',
      title: 'Check the toolhead route',
      copy: 'A movement stopped before the route to the toolhead was committed. Open Diagnostics and choose the real physical state: no filament in BMCU, filament in BMCU only, or filament loaded to the toolhead.',
    });
  } else if (interruptedPlan) {
    notices.push({
      key: 'plan-interrupted', cls: 'warning',
      title: 'Print sources not prepared',
      copy: 'The BMCU source plan did not finish. Start the print again after checking the selected materials.',
    });
  }
  if (lastError?.code && !String(lastError.code).includes('ROUTE_UNCERTAIN')) {
    const code = String(lastError.code);
    const suppressLastError = unroutedTails.length && ['TAIL_CHANNEL_UNROUTED', 'ROUTE_RECONCILE_REQUIRED'].includes(code);
    if (!suppressLastError) notices.push({
      key: 'last-error', cls: 'error',
      title: code === 'SNAPMAKER_AUTO_FEED_FAILED'
        ? 'Snapmaker auto-feed failed'
        : (code === 'SIDECAR_IPC_UNAVAILABLE'
          ? 'BMCU control link unavailable'
          : 'BMCU operation failed'),
      copy: String(lastError.details || lastError.message || 'BMCU reported an error.'),
    });
  }
  syncKeyed($('noticeRegion'), notices, (item) => item.key, (item) => {
    const node = document.createElement('div');
    node.className = 'notice';
    node.innerHTML = '<span class="notice-icon">!</span><div><h3></h3><p></p><button class="button danger hidden" type="button"></button></div>';
    return node;
  }, (node, item) => {
    node.className = `notice ${item.cls || ''}`.trim();
    text(node.querySelector('h3'), item.title);
    text(node.querySelector('p'), item.copy);
    const button = node.querySelector('button');
    setClass(button, 'hidden', !item.action);
    if (item.action) {
      button.dataset.action = item.action[0];
      text(button, item.action[1]);
    }
  });
}

function renderSummary() {
  const channels = allChannels();
  const readyDevices = devices().filter(deviceReady).length;
  const present = channels.filter(({channel}) => channel.present).length;
  const loaded = channels.filter(({channel}) => channel.loaded).length;
  const connectedChannels = channels.filter(({channel}) => channel.connected);
  const calibrated = connectedChannels.filter(({channel}) => channel.calibration_valid).length;
  const values = [
    ['BMCU', devices().length ? (readyDevices === devices().length ? `${readyDevices} ready` : `${readyDevices} of ${devices().length} ready`) : 'Not detected', readyDevices === devices().length && readyDevices ? 'good' : 'warn'],
    ['Filament detected', channels.length ? `${present} of ${channels.length}` : 'No channels', present ? 'good' : ''],
    ['Loaded routes', String(loaded), loaded ? 'good' : ''],
    ['Module calibration', connectedChannels.length ? `${calibrated} of ${connectedChannels.length}` : 'No connected channels', calibrated === connectedChannels.length && connectedChannels.length ? 'good' : 'warn'],
  ];
  syncKeyed($('summaryGrid'), values, (item) => item[0], () => {
    const node = document.createElement('div');
    node.className = 'summary-card';
    node.innerHTML = '<span></span><strong></strong>';
    return node;
  }, (node, item) => {
    node.className = `summary-card ${item[2]}`.trim();
    text(node.querySelector('span'), item[0]);
    text(node.querySelector('strong'), item[1]);
  });
}

function createDashboardChannel() {
  const node = document.createElement('article');
  node.className = 'live-channel';
  node.innerHTML = '<span class="swatch"></span><div><h3 class="channel-heading"><span class="channel-device"></span><span class="badge channel-chip"></span><span class="badge tool-chip"></span></h3><p></p></div><div class="channel-badges"></div><div class="channel-actions"><button class="small-action edit" type="button">Edit</button><button class="small-action retract-input" type="button" hidden>Retract filament</button><button class="small-action primary load-toggle" type="button"></button></div>';
  return node;
}

function updateDashboardChannel(node, item) {
  const {device, channel} = item;
  const operation = channelOperation(device, channel);
  const state = channelState(channel, operation);
  node.querySelector('.swatch').style.background = channel.color || '#ffffff';
  text(node.querySelector('.channel-device'), deviceLabel(device));
  text(node.querySelector('.channel-chip'), sourceTitle(channel));
  const tool = virtualTool(channel);
  text(node.querySelector('.tool-chip'), orcaSlotLabel(tool));
  const tail = Boolean(channel.tail_detached);
  const reason = loadBlockReason(device, channel);
  const channelCopy = channel.tail_unrouted
    ? `${channel.material || 'Unknown'} - Tail route needs ${endpointLabel(channel.tail_endpoint)}`
    : tail
      ? `${channel.material || 'Unknown'} - Tail remains in ${endpointLabel(channel.tail_endpoint || channel.endpoint)}`
      : `${channel.material || 'Unknown'} - ${endpointLabel(channel.endpoint)}`;
  text(node.querySelector('p'), !operation && reason && !channel.loaded && !tail
    ? `${channelCopy} - Load unavailable: ${reason}`
    : channelCopy);
  replaceBadges(node.querySelector('.channel-badges'), [
    [state.label, state.cls],
    [`Buffer ${Math.round(clamp(channel.buffer_pct, 0, 100))}%`, ''],
    [`Retract ${formatLength(channel.unload_retract_mm || 200)}`, ''],
    [`Autoload ${formatLength(channel.autoload_mm || 120)}${channel.autoload_runtime_supported === false && Number(channel.autoload_mm || 120) !== 120 ? ' (12.00 cm active)' : ''}`, ''],
    ...(channel.calibration_valid && channel.encoder_status === 'UNTESTED'
      ? [['Movement check pending', 'warn']]
      : []),
  ]);
  const edit = node.querySelector('.edit');
  edit.dataset.action = 'edit-channel';
  edit.dataset.device = device.name;
  edit.dataset.channel = String(channel.channel);
  edit.disabled = Boolean(operation);
  const retract = node.querySelector('.retract-input');
  const prestaged = plainObject(channel.prestaged) && Object.keys(channel.prestaged).length > 0;
  const deviceOperation = status().active_operations?.[device.name];
  const deviceMotionIdle = (device.channels || []).every(
    (item) => Number(item.motion || 0) === 0);
  const parkedInBmcu = !operation && Boolean(channel.present) &&
    routeState(channel) === 'EMPTY' && !channel.loaded && !channel.uncertain &&
    !tail && !channel.tail_unrouted && !prestaged;
  const canRetractInput = parkedInBmcu && deviceReady(device) &&
    !plainObject(deviceOperation) && deviceMotionIdle &&
    channel.channel_retract_supported === true &&
    channel.calibration_valid === true && channel.encoder_io_ok !== false &&
    Number(channel.motion || 0) === 0;
  retract.hidden = !parkedInBmcu && operation?.kind !== 'retracting';
  retract.dataset.action = 'retract-channel';
  retract.dataset.device = device.name;
  retract.dataset.channel = String(channel.channel);
  retract.disabled = Boolean(operation) || !canRetractInput;
  retract.title = operation?.kind === 'retracting'
    ? 'Input filament is being withdrawn from this BMCU channel.'
    : !deviceReady(device)
      ? 'BMCU is not ready yet.'
      : channel.channel_retract_supported !== true
        ? 'The connected BMCU firmware does not support explicit input retract.'
        : channel.calibration_valid !== true
          ? 'Calibrate this channel before retracting filament.'
          : channel.encoder_io_ok === false
            ? 'The channel movement sensor has an electrical fault.'
            : plainObject(deviceOperation) || !deviceMotionIdle || Number(channel.motion || 0) !== 0
              ? 'BMCU is busy. Retract will be available as soon as motion stops.'
              : 'Withdraw the filament completely from this BMCU input while the toolhead route is empty.';
  text(retract, operation?.kind === 'retracting' ? 'Retracting…' : 'Retract filament');

  const button = node.querySelector('.load-toggle');
  const unload = Boolean(channel.loaded || tail);
  const endpointConfig = endpointFor(channel);
  const genericContractMissing = endpointConfig.driver === 'generic_single_extruder' &&
    (!String(endpointConfig.toolhead_prepare_macro || '').trim() ||
     !String(endpointConfig.before_pullback_macro || '').trim());
  const genericContractReason = genericContractMissing
    ? 'Configure Toolhead preparation macro and Before pullback macro in Settings.'
    : '';
  button.dataset.action = unload ? 'unload-channel' : 'load-channel';
  button.dataset.device = device.name;
  button.dataset.channel = String(channel.channel);
  button.disabled = Boolean(operation) || Boolean(channel.tail_unrouted) ||
    (!unload && Boolean(reason)) || (unload && genericContractMissing);
  button.title = operation
    ? (operation.kind === 'loading' ? 'Filament loading is in progress.' :
      operation.kind === 'retracting' ? 'Input filament retract is in progress.' :
      'Filament unloading is in progress.')
    : channel.tail_unrouted
      ? 'Restore the original Head routing before continuing.'
      : tail
        ? 'Continue the existing tail handoff.'
        : unload ? (genericContractReason || 'Unload filament.') : reason;
  text(button, operation
    ? (operation.kind === 'loading' ? 'Loading…' :
      operation.kind === 'retracting' ? 'Busy…' : 'Unloading…')
    : channel.tail_unrouted
      ? 'Restore route first'
      : (tail ? 'Continue tail handoff' : (unload ? 'Unload' : 'Load')));
}

function renderDashboardChannels() {
  syncKeyed($('dashboardChannels'), allChannels(), ({device, channel}) => keyFor(device, channel), createDashboardChannel, updateDashboardChannel);
}

function nativeHeadCount() {
  if (printerInfo().topology === 'snapmaker_u1') return 4;
  const count = Number(printerInfo().extruders?.length || 0);
  return Number.isInteger(count) && count > 0 ? count : 1;
}

function renderToolMap() {
  const u1 = printerInfo().topology === 'snapmaker_u1';
  const rows = u1
    ? Array.from({length: 4}, (_, index) => [index, `Physical Head ${index + 1}`])
    : [[0, 'External / manual filament']];
  syncKeyed($('toolMap'), rows, (item) => item[0], () => {
    const node = document.createElement('div');
    node.className = 'mapping-row native-tool-row';
    node.innerHTML = '<strong></strong><span></span>';
    return node;
  }, (node, item) => {
    text(node.querySelector('strong'), orcaSlotLabel(item[0]));
    text(node.querySelector('span'), item[1]);
  });
}

function syncSelectOptions(select, options) {
  const signature = JSON.stringify(options);
  if (select.dataset.options === signature) return;
  const current = select.value;
  select.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label; return option;
  }));
  select.dataset.options = signature;
  if (options.some(([value]) => value === current)) select.value = current;
}

function createDashboardRouteRow() {
  const node = document.createElement('div');
  node.className = 'route-row dashboard-route-row';
  node.innerHTML = '<div class="route-source"><strong class="route-tool"></strong><span class="route-name"></span></div><select class="route-select"></select>';
  return node;
}

function routeChanges() {
  const changes = [];
  for (const {device, channel} of allChannels()) {
    const key = keyFor(device, channel);
    if (!ui.routeDraft.has(key)) continue;
    const from = String(channel.endpoint || '');
    const to = String(ui.routeDraft.get(key) || '');
    if (from === to) continue;
    changes.push({device, channel, from, to});
  }
  return changes;
}

function renderDashboardRouting() {
  const u1 = printerInfo().topology === 'snapmaker_u1';
  text($('routingHelp'), u1
    ? 'Stock heads use 1 (T0)-4 (T3). BMCU channels use persistent slots 5 (T4)-32 (T31).'
    : 'External/manual filament is 1 (T0). BMCU channels use persistent slots from 2 (T1) upward.');
  const rows = allChannels().slice().sort((a, b) => {
    const at = virtualTool(a.channel) ?? 999;
    const bt = virtualTool(b.channel) ?? 999;
    return at - bt || deviceLabel(a.device).localeCompare(deviceLabel(b.device)) || Number(a.channel.channel) - Number(b.channel.channel);
  });
  syncKeyed($('dashboardRouting'), rows, ({device, channel}) => keyFor(device, channel), createDashboardRouteRow, (node, item) => {
    const {device, channel} = item;
    const key = keyFor(device, channel);
    const tool = virtualTool(channel);
    text(node.querySelector('.route-tool'), orcaSlotLabel(tool));
    text(node.querySelector('.route-name'), `${deviceLabel(device)} - ${sourceTitle(channel)}`);
    const select = node.querySelector('select');
    select.dataset.routeDevice = device.name;
    select.dataset.routeChannel = String(channel.channel);
    const blocked = (status().controller_mode || 'standalone') !== 'standalone';
    const current = String(channel.endpoint || '');
    const tailRestore = Boolean(channel.tail_unrouted && channel.tail_endpoint);
    const options = blocked
      ? [['', 'Not connected'], ...(current ? [[current, `${endpointLabel(current)} - disconnect only`]] : [])]
      : tailRestore
        ? [['', 'Select original Head'], [String(channel.tail_endpoint), `${endpointLabel(channel.tail_endpoint)} - restore tail route`]]
        : [['', 'Not connected'], ...endpointNames().map((name) => [name, endpointLabel(name)])];
    syncSelectOptions(select, options);
    const selected = ui.routeDraft.has(key) ? String(ui.routeDraft.get(key) || '') : current;
    if (document.activeElement !== select && select.value !== selected) select.value = selected;
    select.disabled = blocked && !current;
  });
  const changes = routeChanges();
  text($('routeSaveStatus'), changes.length ? `${changes.length} unsaved change${changes.length === 1 ? '' : 's'}` : 'No unsaved routing changes');
  const disconnectOnly = changes.length > 0 && changes.every((change) => !change.to);
  const blocked = (status().controller_mode || 'standalone') !== 'standalone';
  $('saveRoutes').disabled = changes.length === 0 || ui.busy || (blocked && !disconnectOnly);
  $('discardRoutes').disabled = changes.length === 0 || ui.busy;
}

function resetHeadSourceCard(node, sourceLabel) {
  node.className = 'head-source-card';
  node.innerHTML = `<div class="head-source-head"><div><div class="head-title"><h3></h3><span class="badge head-tool"></span></div><small class="head-selected"></small></div><span class="badge head-path-badge"></span></div><div class="head-path-copy"></div><div class="head-source-section stock-section"><div class="source-section-label">${sourceLabel}</div><div class="head-source-row stock-source-row"><span class="source-swatch"></span><div class="source-main"><strong></strong><small></small></div><div class="source-badges"></div></div></div><div class="head-source-section bmcu-section"><div class="source-section-label">Assigned BMCU sources</div><div class="bmcu-source-list"></div></div>`;
  return node;
}

function ensureHeadSourceCard(node, sourceLabel) {
  if (!node.querySelector('.head-path-badge') || !node.querySelector('.stock-source-row') || !node.querySelector('.bmcu-source-list'))
    resetHeadSourceCard(node, sourceLabel);
  return node;
}

function renderOwnership() {
  const panel = $('sourcePathPanel');
  const u1 = printerInfo().topology === 'snapmaker_u1';
  setClass(panel, 'hidden', false);
  text($('sourcePathHelp'), u1
    ? 'Routing does not reserve a Head. Stock remains usable whenever the shared path is free.'
    : 'External T0 is manual and never reserved or tracked by BMCU. BMCU only reports its own physical routes.');

  if (!u1) {
    let rows = Object.entries(endpoints())
      .filter(([, endpoint]) => endpoint?.driver !== 'snapmaker_u1');
    if (!rows.length) rows = [['extruder', {driver: 'generic_single_extruder', assigned_channels: []}]];
    syncKeyed($('ownershipList'), rows, (item) => item[0], () =>
      resetHeadSourceCard(document.createElement('article'), 'Manual source'),
    (node, [name, endpoint]) => {
      ensureHeadSourceCard(node, 'Manual source');
      const sources = assignedSources(name, endpoint);
      const loaded = sources.filter(({channel}) => routeState(channel) === 'LOADED');
      const uncertain = sources.filter(({channel}) => routeState(channel) === 'UNCERTAIN');
      const operating = sources.find(({device, channel}) => channelOperation(device, channel));
      text(node.querySelector('h3'), endpointLabel(name));
      text(node.querySelector('.head-tool'), 'Shared path');
      text(node.querySelector('.head-selected'), `T0 External + ${sources.length} BMCU source${sources.length === 1 ? '' : 's'}`);
      const pathBadge = node.querySelector('.head-path-badge');
      let pathLabel = 'BMCU path clear';
      let pathClass = 'good';
      let pathCopy = 'No BMCU source owns this path. External T0 remains completely manual.';
      if (uncertain.length) {
        pathLabel = 'BMCU route uncertain';
        pathClass = 'warn';
        pathCopy = 'A BMCU route cannot be proven empty or loaded. External remains manual and is not used as ownership evidence.';
      } else if (loaded.length) {
        const source = loaded[0];
        pathLabel = `${deviceLabel(source.device)} Ch ${physicalChannel(source.channel)} loaded`;
        pathClass = deviceReady(source.device) ? 'good' : 'bad';
        pathCopy = `BMCU reports its own route to ${endpointLabel(name)} as loaded. External T0 remains untracked.`;
      } else if (operating) {
        const operation = channelOperation(operating.device, operating.channel);
        pathLabel = operation?.label || 'BMCU route changing';
        pathClass = 'warn';
        pathCopy = 'BMCU is moving one of its assigned filament routes.';
      }
      pathBadge.className = `badge ${pathClass}`;
      text(pathBadge, pathLabel);
      text(node.querySelector('.head-path-copy'), pathCopy);

      const externalRow = node.querySelector('.stock-source-row');
      externalRow.querySelector('.source-swatch').style.background = '#ffffff';
      text(externalRow.querySelector('strong'), 'External T0');
      text(externalRow.querySelector('small'), `Manual filament · ${endpointLabel(name)}`);
      const externalBadges = [['Manual source', '']];
      if (loaded.length || uncertain.length || operating) {
        externalBadges.push(['BMCU path active', 'warn']);
      } else if (endpoint.external_sensor_detected === true) {
        externalBadges.push(['Toolhead sensor detects filament', 'good']);
      } else if (endpoint.external_sensor_detected === false) {
        externalBadges.push(['Toolhead sensor clear', '']);
      } else {
        externalBadges.push(['Not monitored', '']);
      }
      replaceBadges(externalRow.querySelector('.source-badges'), externalBadges);

      const sourceList = node.querySelector('.bmcu-source-list');
      if (!sources.length) {
        syncKeyed(sourceList, [{key: 'empty'}], (item) => item.key, () => {
          const empty = document.createElement('div'); empty.className = 'empty-source-note'; return empty;
        }, (empty) => text(empty, 'No BMCU channel assigned to this toolhead.'));
        return;
      }
      syncKeyed(sourceList, sources, ({device, channel}) => keyFor(device, channel), () => {
        const row = document.createElement('div');
        row.className = 'head-source-row';
        row.innerHTML = '<span class="source-swatch"></span><div class="source-main"><strong></strong><small></small></div><div class="source-badges"></div>';
        return row;
      }, (row, {device, channel}) => {
        row.querySelector('.source-swatch').style.background = channel.color || '#ffffff';
        const tool = virtualTool(channel);
        text(row.querySelector('strong'), `${deviceLabel(device)} · Ch ${physicalChannel(channel)}${tool === null ? '' : ` · T${tool}`}`);
        text(row.querySelector('small'), u1MaterialText(channel));
        const badges = [];
        const online = deviceReady(device) && device.transport_online !== false && channel.connected !== false;
        const route = routeState(channel);
        const operation = channelOperation(device, channel);
        if (!online) badges.push([route === 'EMPTY' ? 'BMCU offline' : `Last known route: ${route}`, 'bad']);
        else if (channel.tail_unrouted) badges.push(['Route repair required', 'bad']);
        else if (channel.tail_detached) badges.push(['Tail in toolhead path', 'warn']);
        else if (operation?.kind === 'loading') badges.push([operation.label || 'Loading', 'warn']);
        else if (operation?.kind === 'unloading') badges.push([operation.label || 'Unloading', 'warn']);
        else if (route === 'UNCERTAIN') badges.push(['Route uncertain', 'warn']);
        else if (route === 'LOADED') badges.push(['Loaded to toolhead', 'good']);
        else if (channel.prestaged?.at_entry) badges.push(['Prepared at toolhead entry', 'warn']);
        else badges.push([channel.present ? 'Parked in BMCU' : 'Route empty', channel.present ? 'good' : '']);
        if (online) badges.push([channel.present ? 'Input detected' : 'Input empty', channel.present ? 'good' : '']);
        if (online && !channel.calibration_valid) badges.push(['Calibration required', 'warn']);
        replaceBadges(row.querySelector('.source-badges'), badges);
      });
    });
    return;
  }

  let rows = Object.entries(endpoints())
    .filter(([, endpoint]) => endpoint?.driver === 'snapmaker_u1')
    .sort((a, b) => Number(a[1].head_index) - Number(b[1].head_index));
  if (rows.length === 0) {
    rows = Array.from({length: nativeHeadCount()}, (_, index) => [
      `u1_head${index}`,
      {driver: 'snapmaker_u1', head_index: index, assigned_channels: [], owner: {owner: 'unknown'}},
    ]);
  }
  syncKeyed($('ownershipList'), rows, (item) => item[0], () =>
    resetHeadSourceCard(document.createElement('article'), 'Stock source'),
  (node, [name, endpoint]) => {
    ensureHeadSourceCard(node, 'Stock source');
    const head = Number(endpoint.head_index);
    const owner = endpoint.owner || {};
    const stock = owner.stock_source || {};
    const path = owner.path || endpoint?.capabilities?.u1_native_path || {};
    const sources = assignedSources(name, endpoint);
    const summary = u1PathSummary(name, endpoint, sources);
    text(node.querySelector('h3'), `Head ${head + 1}`);
    text(node.querySelector('.head-tool'), `Stock T${head}`);
    const selected = u1SelectedSource(name, head);
    text(node.querySelector('.head-selected'), selected || `${sources.length} assigned BMCU source${sources.length === 1 ? '' : 's'}`);
    const pathBadge = node.querySelector('.head-path-badge');
    pathBadge.className = `badge ${summary.cls || ''}`.trim();
    text(pathBadge, summary.label);
    text(node.querySelector('.head-path-copy'), summary.detail);

    const stockRow = node.querySelector('.stock-source-row');
    const stockSwatch = stockRow.querySelector('.source-swatch');
    stockSwatch.style.background = stock.color || '#ffffff';
    const module = String(stock.module || '').toLowerCase() === 'right' ? 'Right' : 'Left';
    const nativeChannel = Number.isInteger(Number(stock.channel)) ? Number(stock.channel) + 1 : '?';
    text(stockRow.querySelector('strong'), `Stock T${head}`);
    const captured = stock.material_origin === 'captured';
    text(stockRow.querySelector('small'), `${u1MaterialText(stock, captured)} · ${module} feeder Ch ${nativeChannel}`);
    const stockBadges = [];
    const nativeOwner = owner.owner === 'native' || owner.owner === 'native_busy';
    const bmcuOwner = owner.owner === 'bmcu' || owner.owner === 'bmcu_busy';
    const channelState = String(path.channel_state || stock.channel_state || '').toLowerCase();
    if (nativeOwner && path.known === true && path.busy === true) stockBadges.push([channelState === 'load_finish' ? 'Loaded to Head' : channelState === 'unload_finish' ? 'Manual retract required' : 'Path occupied', channelState === 'load_finish' ? 'good' : 'warn']);
    else if (nativeOwner && path.known === true && path.busy === false) stockBadges.push([path.input_released === true ? 'Input clear / released' : 'Available', 'good']);
    else if (bmcuOwner) stockBadges.push(['Waiting for shared path', '']);
    else stockBadges.push(['Availability unknown', 'warn']);
    if (stock.input_detected === true) stockBadges.push(['Input detected', 'good']);
    else if (stock.input_detected === false) stockBadges.push(['No input detected', '']);
    else stockBadges.push(['Input sensor unknown', 'warn']);
    if (stock.user_auto_enabled === true) stockBadges.push(['Auto feed on', '']);
    else if (stock.user_auto_enabled === false) stockBadges.push(['Auto feed off', '']);
    replaceBadges(stockRow.querySelector('.source-badges'), stockBadges);

    const sourceList = node.querySelector('.bmcu-source-list');
    if (!sources.length) {
      syncKeyed(sourceList, [{key: 'empty'}], (item) => item.key, () => {
        const empty = document.createElement('div'); empty.className = 'empty-source-note'; return empty;
      }, (empty) => text(empty, 'No BMCU channel assigned to this Head.'));
      return;
    }
    syncKeyed(sourceList, sources, ({device, channel}) => keyFor(device, channel), () => {
      const row = document.createElement('div');
      row.className = 'head-source-row';
      row.innerHTML = '<span class="source-swatch"></span><div class="source-main"><strong></strong><small></small></div><div class="source-badges"></div>';
      return row;
    }, (row, {device, channel}) => {
      row.querySelector('.source-swatch').style.background = channel.color || '#ffffff';
      const tool = virtualTool(channel);
      text(row.querySelector('strong'), `${deviceLabel(device)} · Ch ${physicalChannel(channel)}${tool === null ? '' : ` · T${tool}`}`);
      text(row.querySelector('small'), u1MaterialText(channel));
      const badges = [];
      const deviceOnline = deviceReady(device) && device.transport_online !== false;
      const channelOnline = deviceOnline && channel.connected !== false;
      const route = routeState(channel);
      const operation = channelOperation(device, channel);
      if (!deviceOnline) {
        badges.push([route === 'EMPTY' ? 'BMCU offline' : `Last known route: ${route}`, 'bad']);
      } else if (!channelOnline) {
        badges.push([route === 'EMPTY' ? 'Channel unavailable' : `Last known route: ${route}`, 'bad']);
      } else if (channel.tail_unrouted) {
        badges.push(['Route repair required', 'bad']);
      } else if (channel.tail_detached) {
        badges.push(['Tail in Head path', 'warn']);
      } else if (operation?.kind === 'loading') {
        badges.push([operation.label || 'Loading', 'warn']);
      } else if (operation?.kind === 'unloading') {
        badges.push([operation.label || 'Unloading', 'warn']);
      } else if (route === 'UNCERTAIN') {
        badges.push(['Route uncertain', 'warn']);
      } else if (route === 'LOADED') {
        badges.push(['Loaded to Head', 'good']);
      } else if (channel.prestaged?.at_entry) {
        badges.push(['Prepared at Head entry', 'warn']);
      } else {
        badges.push([channel.present ? 'Parked in BMCU' : 'Route empty', channel.present ? 'good' : '']);
      }
      if (channelOnline) badges.push([channel.present ? 'Input detected' : 'Input empty', channel.present ? 'good' : '']);
      if (channelOnline && !channel.calibration_valid) badges.push(['Calibration required', 'warn']);
      replaceBadges(row.querySelector('.source-badges'), badges);
    });
  });
}

function createDeviceDiagnostic() {
  const node = document.createElement('article');
  node.className = 'diagnostic-card';
  node.innerHTML = '<div class="section-head"><div><h2></h2><p></p></div><span class="badge"></span></div><div class="metric-grid"></div>';
  return node;
}

function setMetrics(container, values) {
  syncKeyed(container, values, (item) => item[0], () => {
    const node = document.createElement('div'); node.className = 'metric'; node.innerHTML = '<span></span><strong></strong>'; return node;
  }, (node, item) => { text(node.querySelector('span'), item[0]); text(node.querySelector('strong'), item[1]); });
}

function updateDeviceDiagnostic(node, device) {
  text(node.querySelector('h2'), deviceLabel(device));
  text(node.querySelector('.section-head p'), device.port || 'No serial path');
  const badgeNode = node.querySelector('.badge');
  const transportKnown = Object.prototype.hasOwnProperty.call(device, 'transport_online');
  const ready = deviceReady(device) && (!transportKnown || device.transport_online);
  badgeNode.className = `badge ${ready ? 'good' : 'bad'}`;
  text(badgeNode, ready ? 'Ready' : 'Offline');
  const perf = status().performance?.per_device?.[device.name] || {};
  setMetrics(node.querySelector('.metric-grid'), [
    ['Firmware', device.firmware || 'unknown'],
    ['UID', device.uid || 'unknown'],
    ['Session', device.transport_session_id || device.session_id || 'unknown'],
    ['Transport process', device.transport_process_online ? 'running' : 'stopped'],
    ['Transport socket', device.transport_socket_online ? 'ready' : 'missing'],
    ['BMCU link', device.transport_online ? 'online' : 'offline'],
    ['Live sensor age', finite(device.transport_updated_at) && device.transport_updated_at > 0
      ? `${Math.max(0, Date.now() / 1000 - Number(device.transport_updated_at)).toFixed(2)} s`
      : 'unknown'],
    ['RX / TX packets', `${perf.rx_packets ?? 0} / ${perf.tx_packets ?? 0}`],
    ['Packet errors', perf.packet_errors ?? 0],
    ['Missed events', device.missed_events ?? perf.missed_events ?? 0],
    ['Last RX age', finite(perf.last_rx_age_s) ? `${Number(perf.last_rx_age_s).toFixed(2)} s` : 'unknown'],
    ['NVM fault', device.nvm_fault ? 'YES' : 'no'],
    ['Last error', device.last_error || 'none'],
  ]);
}

function createChannelDiagnostic() {
  const node = document.createElement('article');
  node.className = 'diagnostic-card';
  node.innerHTML = '<div class="section-head"><div><h2></h2><p></p></div><span class="badge"></span></div><div class="meter"><i></i></div><div class="metric-grid"></div><div class="diagnostic-actions"></div>';
  return node;
}

function updateChannelDiagnostic(node, item) {
  const {device, channel} = item;
  const cal = channel.calibration || {};
  text(node.querySelector('h2'), `${deviceLabel(device)} - ${sourceTitle(channel)}`);
  text(node.querySelector('.section-head p'), `${endpointLabel(channel.endpoint)} - ${channel.material || 'Unknown'}`);
  const state = channelState(channel, channelOperation(device, channel));
  const statusBadge = node.querySelector('.badge'); statusBadge.className = `badge ${state.cls}`; text(statusBadge, state.label);
  node.querySelector('.meter i').style.width = `${clamp(channel.buffer_pct, 0, 100)}%`;
  setMetrics(node.querySelector('.metric-grid'), [
    ['Buffer', `${Number(channel.buffer_pct || 0).toFixed(1)}%`],
    ['Buffer raw', finite(channel.buffer_raw) ? `${Number(channel.buffer_raw).toFixed(4)} V` : 'unknown'],
    ['Calibration', channel.calibration_valid ? 'valid' : 'required'],
    ['Current raw', finite(cal.current_raw) ? `${Number(cal.current_raw).toFixed(4)} V` : 'unknown'],
    ['Cal min / neutral / max', [cal.minimum, cal.neutral, cal.maximum].every(finite) ? `${Number(cal.minimum).toFixed(3)} / ${Number(cal.neutral).toFixed(3)} / ${Number(cal.maximum).toFixed(3)} V` : 'unknown'],
    ['Cal offset / polarity', `${finite(cal.offset) ? Number(cal.offset).toFixed(4) : 'unknown'} / ${cal.polarity ?? 'unknown'}`],
    ['Motor PWM', channel.motor_pwm ?? 0],
    ['Motor motion', motionLabel(channel.motion)],
    ['Measured filament travel', `${finite(channel.travel_meters) ? Number(channel.travel_meters).toFixed(3) : '0.000'} m`],
    ['Filament movement sensor', channel.encoder_test?.ok === true
      ? `Working - measured ${Number(channel.encoder_test.measured_mm || 0).toFixed(1)} mm`
      : channel.encoder_status === 'FAULT'
        ? 'Fault'
        : 'Not tested - verified automatically during the next BMCU filament move'],
    ['Last path measurement', finite(channel.path_length_mm) && Number(channel.path_length_mm) > 0
      ? `${Number(channel.path_length_mm).toFixed(1)} mm`
      : 'not measured'],
    ['Tail ownership', channel.tail_detached
      ? `forward-only on ${channel.tail_endpoint || channel.endpoint || 'endpoint'}`
      : 'normal / gripped'],
    ['Input detector', channel.present ? 'ON - filament present' : 'OFF - no filament'],
    ['Configured colour', (channel.color || '#FFFFFF').toUpperCase()],
    ['Slot', orcaSlotLabel(virtualTool(channel), true)],
    ['Route', `${routeState(channel)} (${channel.route_state ?? '?'})`],
  ]);
  const configured = Boolean(channel.endpoint);
  const actions = [];
  if (configured) {
    actions.push({
      key: 'confirm-empty',
      action: 'route-confirm-empty',
      label: 'No filament in BMCU',
      disabled: Boolean(channel.present),
      title: channel.present
        ? 'The BMCU input detector still sees filament. Use "Filament in BMCU only" instead.'
        : 'Confirm that this channel and the complete downstream route are empty.',
    });
    actions.push({
      key: 'confirm-parked',
      action: 'route-confirm-parked',
      label: 'Filament in BMCU only',
      disabled: !channel.present,
      title: channel.present
        ? 'Confirm that filament remains in BMCU, but does not occupy the downstream toolhead path.'
        : 'The BMCU input detector is clear. Use "No filament in BMCU" instead.',
    });
    actions.push({
      key: 'confirm-loaded',
      action: 'route-confirm-loaded',
      label: 'Filament loaded to toolhead',
      disabled: false,
      title: 'Confirm only when filament really occupies or reaches the downstream toolhead path.',
    });
  }
  syncKeyed(node.querySelector('.diagnostic-actions'), actions, (entry) => entry.key, () => {
    const button = document.createElement('button');
    button.className = 'button secondary';
    button.type = 'button';
    return button;
  }, (button, entry) => {
    button.dataset.action = entry.action;
    button.dataset.device = device.name;
    button.dataset.channel = String(channel.channel);
    button.disabled = Boolean(entry.disabled) || ui.busy;
    button.title = entry.title || '';
    text(button, entry.label);
  });
}

function downloadDiagnosticsArchive() {
  const include = qsa('[data-diagnostics-option]:checked').map((input) => input.dataset.diagnosticsOption).filter(Boolean);
  const link = document.createElement('a');
  link.href = `/api/diagnostics/export?include=${encodeURIComponent(include.join(','))}`;
  link.download = '';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  closeDialog($('diagnosticsExportDialog'));
}

function setDiagnosticsSelection(checked) {
  qsa('[data-diagnostics-option]').forEach((input) => { input.checked = checked; });
}

function renderDiagnostics() {
  syncKeyed($('diagnosticDevices'), devices(), (device) => device.name, createDeviceDiagnostic, updateDeviceDiagnostic);
  syncKeyed($('diagnosticChannels'), allChannels(), ({device, channel}) => keyFor(device, channel), createChannelDiagnostic, updateChannelDiagnostic);
}

function createCalibrationDevice() {
  const node = document.createElement('div');
  node.className = 'calibration-device';
  node.innerHTML = '<div class="section-head"><div><h3></h3><p></p></div><span class="badge"></span></div><div class="progress"><i></i></div><div class="calibration-actions"></div>';
  return node;
}

function updateCalibrationDevice(node, device) {
  const cal = device.auto_calibration || {};
  const active = Boolean(cal.active || Number(cal.state) === 1);
  const connectedChannels = (device.channels || []).filter((channel) => channel.connected);
  const valid = connectedChannels.filter((channel) => channel.calibration_valid).length;
  const ready = connectedChannels.length > 0 && valid === connectedChannels.length;
  text(node.querySelector('h3'), deviceLabel(device));
  text(node.querySelector('.section-head p'), active ? CAL_STAGE[cal.stage] || 'Calibrating' : `${valid}/${connectedChannels.length} connected channels calibrated`);
  const badgeNode = node.querySelector('.badge'); badgeNode.className = `badge ${active ? 'warn' : ready ? 'good' : 'bad'}`; text(badgeNode, active ? `${clamp(cal.progress, 0, 100)}%` : ready ? 'Ready' : 'Required');
  node.querySelector('.progress i').style.width = `${active ? clamp(cal.progress, 0, 100) : connectedChannels.length ? valid * 100 / connectedChannels.length : 0}%`;
  const actions = [['ALL', 'Calibrate all channels'], ...[0,1,2,3].map((channel) => [String(channel), `Channel ${channel + 1}`])];
  syncKeyed(node.querySelector('.calibration-actions'), actions, (item) => item[0], () => {
    const button = document.createElement('button'); button.className = 'small-action'; button.type = 'button'; return button;
  }, (button, item) => {
    button.dataset.action = item[0] === 'ALL' ? 'calibrate-device' : 'calibrate-channel';
    button.dataset.device = device.name; button.dataset.channel = item[0];
    const selectedChannel = item[0] === 'ALL' ? null
      : (device.channels || []).find((channel) => Number(channel.channel) === Number(item[0]));
    button.disabled = active || (item[0] === 'ALL'
      ? connectedChannels.length === 0
      : !selectedChannel?.connected);
    text(button, item[1]);
  });
}

function renderSetupStatus() {
  const channels = allChannels();
  const connectedChannels = channels.filter(({channel}) => channel.connected);
  const checks = [
    ['BMCU connection', devices().length > 0 && devices().every(deviceReady), devices().length ? `${devices().filter(deviceReady).length} of ${devices().length} ready` : 'Not detected'],
    ['PTFE routing', channels.length > 0 && channels.every(({channel}) => Boolean(channel.endpoint)), `${channels.filter(({channel}) => channel.endpoint).length}/${channels.length} connected`],
    ['Slots', channels.length > 0 && channels.every(({channel}) => virtualTool(channel) !== null), channels.length ? `${channels.filter(({channel}) => virtualTool(channel) !== null).length}/${channels.length} assigned in 5-32` : 'No channels'],
    ['Module calibration', connectedChannels.length > 0 && connectedChannels.every(({channel}) => channel.calibration_valid), `${connectedChannels.filter(({channel}) => channel.calibration_valid).length}/${connectedChannels.length} connected calibrated`],
  ];
  syncKeyed($('setupStatus'), checks, (item) => item[0], () => {
    const node = document.createElement('div'); node.className = 'check-row'; node.innerHTML = '<span></span><strong></strong>'; return node;
  }, (node, item) => {
    text(node.querySelector('span'), item[0]);
    const value = node.querySelector('strong'); value.className = item[1] ? 'good' : 'warn'; text(value, item[2]);
  });
}

function serialCandidates() {
  const ports = Array.isArray(store.state.config.serial_ports) ? store.state.config.serial_ports : [];
  return ports.map((item) => {
    if (typeof item === 'string') return {path: item, device: item, label: item, aliases: [item], ch340: false};
    const path = String(item?.path || '');
    return {
      path,
      device: String(item?.device || path),
      label: String(item?.label || path),
      aliases: Array.isArray(item?.aliases) ? item.aliases.map(String) : [],
      ch340: Boolean(item?.ch340),
      usb_ttl: Boolean(item?.usb_ttl),
      usb_vid: String(item?.usb_vid || ''),
      usb_pid: String(item?.usb_pid || ''),
      driver: String(item?.driver || ''),
    };
  }).filter((item) => item.path);
}

function candidatePaths(candidate) {
  return new Set([candidate.path, candidate.device, ...(candidate.aliases || [])].filter(Boolean));
}

function candidateMatchesPort(candidate, port) {
  return Boolean(port) && candidatePaths(candidate).has(String(port));
}

function managedUpdateDevices() {
  return devices().filter(deviceReady);
}

function rawSerialCandidates(mode = selectedFlashMode()) {

  const candidates = serialCandidates();
  const managed = managedUpdateDevices();
  const claimed = managed.map((device) => String(device.transport_port || device.port || '')).filter(Boolean);

  if (ui.suppressUnknownPort) {
    const managedAgain = managed.some((device) =>
      String(device.transport_port || device.port || '') === ui.suppressUnknownPort ||
      candidates.some((candidate) =>
        candidateMatchesPort(candidate, ui.suppressUnknownPort) &&
        candidateMatchesPort(candidate, device.transport_port || device.port)));
    if (managedAgain || Date.now() >= ui.suppressUnknownUntil) {
      ui.suppressUnknownPort = '';
      ui.suppressUnknownUntil = 0;
    }
  }

  return candidates.filter((candidate) => (mode === 'usb' ? candidate.usb_ttl : true) &&
    !claimed.some((port) => candidateMatchesPort(candidate, port)) &&
    !(ui.suppressUnknownPort && Date.now() < ui.suppressUnknownUntil &&
      candidateMatchesPort(candidate, ui.suppressUnknownPort)));
}

function syncDeviceSelect(select) {
  if (ui.updateInProgress) return;
  const options = managedUpdateDevices().map((device) => [device.name, `${deviceLabel(device)} - firmware ${device.firmware || 'unknown'}`]);
  for (const candidate of rawSerialCandidates()) {
    const identity = [candidate.usb_vid && candidate.usb_pid ? `${candidate.usb_vid}:${candidate.usb_pid}` : '', candidate.driver].filter(Boolean).join(' ');
    const type = selectedFlashMode() === 'usb' ? 'USB-TTL' : 'TTL candidate';
    options.push([`raw:${candidate.path}`, `${type} - unknown firmware${identity ? ` [${identity}]` : ''} - ${candidate.path}`]);
  }
  const empty = selectedFlashMode() === 'usb' ? 'No BMCU or USB-TTL adapter detected' : 'No BMCU or serial port detected';
  syncSelectOptions(select, options.length ? options : [['', empty]]);
}

function genericMacroNames() {
  return Array.isArray(printerInfo().gcode_macro_names)
    ? printerInfo().gcode_macro_names
      .map((value) => String(value || '').trim())
      .filter((value) => /^[A-Za-z_]+$/.test(value))
    : [];
}

function genericSensorNames() {
  return Array.isArray(printerInfo().filament_sensors)
    ? printerInfo().filament_sensors.map((value) => String(value || '').trim()).filter(Boolean)
    : [];
}

function genericExtruderNames() {
  const values = Array.isArray(printerInfo().extruders)
    ? printerInfo().extruders.map((value) => String(value || '').trim()).filter(Boolean)
    : [];
  return values.length ? [...new Set(values)] : ['extruder'];
}

function syncGenericDetectedSelect(select, values, current, emptyLabel, required = false) {
  if (!select) return;
  const clean = [...new Set((values || []).map((value) => String(value || '').trim()).filter(Boolean))];
  const selected = String(current || '').trim();
  const options = [];
  if (!required) options.push(new Option(emptyLabel || 'None', ''));
  else options.push(new Option(clean.length ? 'Choose...' : 'None detected', ''));

  for (const value of clean) options.push(new Option(value, value));

  if (selected && !clean.includes(selected)) {
    options.push(new Option(`${selected} - not detected / invalid`, selected));
  }

  select.replaceChildren(...options);
  select.value = selected;
  if (!selected && required) select.value = '';
  select.disabled = required && clean.length === 0 && !selected;
}

function syncGenericDetectedFields(node, value) {
  syncGenericDetectedSelect(
    node.querySelector('.extruder-object'),
    genericExtruderNames(), value.extruder, 'None', true);
  syncGenericDetectedSelect(
    node.querySelector('.post-gears-sensor'),
    genericSensorNames(), value.post_gears_sensor, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.toolhead-prepare-macro'),
    genericMacroNames(), value.toolhead_prepare_macro, 'None', true);
  syncGenericDetectedSelect(
    node.querySelector('.before-pullback-macro'),
    genericMacroNames(), value.before_pullback_macro, 'None', true);
  syncGenericDetectedSelect(
    node.querySelector('.entry-sensor'),
    genericSensorNames(), value.entry_sensor, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.motion-sensor'),
    genericSensorNames(), value.motion_sensor, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.select-macro'),
    genericMacroNames(), value.select_macro, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.deselect-macro'),
    genericMacroNames(), value.deselect_macro, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.verify-macro'),
    genericMacroNames(), value.verify_selected_macro, 'None');
  syncGenericDetectedSelect(
    node.querySelector('.expected-extruder'),
    genericExtruderNames(), value.expected_active_extruder, 'None');
}

function setGenericField(node, selector, value) {
  const field = node.querySelector(selector);
  if (!field || value === undefined || value === null) return;
  if (field.type === 'checkbox') {
    field.checked = Boolean(value);
    return;
  }
  field.value = String(value);
}

function readGenericEndpointForm(node) {
  const num = (selector, fallback) => {
    const value = Number(node.querySelector(selector)?.value);
    return finite(value) ? value : fallback;
  };
  const val = (selector) => String(node.querySelector(selector)?.value || '').trim();
  return {
    extruder: val('.extruder-object') || 'extruder',
    toolhead_prepare_macro: val('.toolhead-prepare-macro'),
    before_pullback_macro: val('.before-pullback-macro'),
    post_gears_sensor: val('.post-gears-sensor'),
    entry_sensor: val('.entry-sensor'),
    motion_sensor: val('.motion-sensor'),
    sensor_policy: (val('.sensor-policy') === 'external' ? 'observe' : (val('.sensor-policy') || 'managed')),
    select_macro: val('.select-macro'),
    deselect_macro: val('.deselect-macro'),
    verify_selected_macro: val('.verify-macro'),
    expected_active_extruder: val('.expected-extruder'),
    require_select_macro: Boolean(node.querySelector('.require-select')?.checked),
    verify_active_extruder: Boolean(node.querySelector('.verify-active')?.checked),
    shared_path_group: val('.shared-path') || String(node.dataset.endpoint || 'extruder'),
    max_route_mm: num('.max-route', 1500),
    final_search_mm: num('.final-search', 250),
    contact_timeout: num('.contact-timeout', 45),
  };
}

function createTailTrackingSetting() {
  const node = document.createElement('article');
  node.className = 'panel-card compact-card generic-endpoint-card';
  node.innerHTML = `
    <div class="section-head"><div><h3></h3><p></p></div><span class="badge"></span></div>
    <div class="form-grid">
      <h4 class="generic-advanced-heading">Generic Klipper toolhead</h4>
      <label class="field"><span>Extruder object</span><select class="extruder-object"></select><small>Choose the printer-owned extruder detected by Klipper. BMCU never sends extruder moves or guesses hotend geometry for generic Klipper.</small></label>
      <label class="field"><span>Loading arrival sensor</span><select class="entry-sensor"></select><small>Optional. If this sensor detects filament during Loading, SEND OUT stops and Toolhead preparation starts immediately. Without it, the buffer threshold ends Loading.</small></label>
      <label class="field full"><span>Toolhead preparation macro</span><select class="toolhead-prepare-macro"></select><small><strong>Required.</strong> Choose one detected Klipper macro. Runs while BMCU is in <strong>Preparing / BEFORE_ON_USE</strong>, after SEND OUT reaches this toolhead. This one macro owns the complete toolhead-side load: heating, extruder capture, movement to the melt zone/nozzle and any seat/prime needed to be print-ready. BMCU waits for the macro to finish before switching to In use.</small></label>
      <label class="field full"><span>Before pullback macro</span><select class="before-pullback-macro"></select><small><strong>Required.</strong> Choose one detected Klipper macro. Runs while BMCU is in <strong>BEFORE_PULL_BACK</strong>. This one macro owns the complete toolhead-side unload: heating, tip-forming or cutting, retracting through the hotend and releasing the filament from the extruder. It must finish only when BMCU may safely perform the long PTFE pullback.</small></label>
      <p class="generic-note full">Both macros receive <code>ENDPOINT</code>, <code>MATERIAL</code> and <code>REASON</code> parameters. They may ignore parameters they do not need. BMCU never adds hidden extrusion distances, temperatures, purge moves or cutter logic around these macros.</p>
      <h4 class="generic-advanced-heading">Filament runout</h4>
      <p class="generic-note full"><strong>Automatic behavior.</strong> With a configured endpoint filament sensor, BMCU follows the old filament until that sensor confirms the tail boundary and uses any compatible ready refill Channel automatically. If no compatible replacement is ready, the printer stays paused: insert replacement filament into the same exhausted BMCU Channel and press the printer's normal Resume. Without an endpoint sensor, BMCU pauses immediately when its own input becomes empty and uses the same manual Resume flow. In both manual cases BMCU completes Load before the original Resume is released.</p>
    </div>
    <details class="generic-advanced"><summary>Advanced - toolchanger selection, extra sensors and route limits</summary>
      <p class="generic-note">A normal single-extruder printer usually leaves this section alone. These settings describe routing/selection around the toolhead; they do not replace either preparation macro.</p>
      <div class="form-grid advanced-grid">
        <h4 class="generic-advanced-heading">Sensors and selection</h4>
        <label class="field"><span>Post-gears sensor</span><select class="post-gears-sensor"></select><small>Optional separate sensor after the drive gears. Used for tail tracking/runout when configured.</small></label>
        <label class="field"><span>Motion sensor</span><select class="motion-sensor"></select></label>
        <label class="field"><span>Sensor policy</span><select class="sensor-policy"><option value="managed">Managed while BMCU owns path</option><option value="observe">Observe only</option></select></label>
        <label class="field"><span>Select macro</span><select class="select-macro"></select></label>
        <label class="field"><span>Deselect macro</span><select class="deselect-macro"></select></label>
        <label class="field"><span>Verify selected macro</span><select class="verify-macro"></select></label>
        <label class="field"><span>Expected active extruder</span><select class="expected-extruder"></select></label>
        <label class="switch-row"><span><strong>Require select macro</strong></span><input class="require-select" type="checkbox"></label>
        <label class="switch-row"><span><strong>Verify active extruder</strong></span><input class="verify-active" type="checkbox"></label>
        <p class="generic-note full">Use Select/Verify/Deselect only when the printer itself must activate, park or pick a physical toolhead before BMCU can route filament to it. BMCU does not guess a toolchanger brand or topology.</p>
        <h4 class="generic-advanced-heading">Local BMCU route limits</h4>
        <label class="field"><span>Shared path group</span><input class="shared-path" maxlength="64"></label>
        <label class="field"><span>Maximum SEND OUT search</span><div class="input-with-unit"><input class="max-route" type="number" min="10" max="5000" step="10"><span>mm</span></div></label>
        <label class="field"><span>Final search after prestage</span><div class="input-with-unit"><input class="final-search" type="number" min="5" max="1000" step="5"><span>mm</span></div></label>
        <label class="field"><span>Contact timeout</span><div class="input-with-unit"><input class="contact-timeout" type="number" min="1" max="180" step="1"><span>s</span></div></label>
      </div>
    </details>
    <div class="setting-actions"><button class="button secondary cancel-tail-setting" type="button">Discard changes</button><button class="button primary save-tail-setting" type="button">Save endpoint</button></div>`;
  node.addEventListener('input', (event) => {
    if (!node.dataset.endpoint) return;
    ui.genericEndpointDraft.set(node.dataset.endpoint, readGenericEndpointForm(node));
  });
  node.addEventListener('change', (event) => {
    if (!node.dataset.endpoint) return;
    ui.genericEndpointDraft.set(node.dataset.endpoint, readGenericEndpointForm(node));
  });
  node.querySelector('.cancel-tail-setting').addEventListener('click', () => {
    ui.genericEndpointDraft.delete(node.dataset.endpoint);
    renderAll();
  });
  node.querySelector('.save-tail-setting').addEventListener('click', async () => {
    const endpoint = node.dataset.endpoint;
    const v = readGenericEndpointForm(node);
    const macros = new Set(genericMacroNames().map((name) => name.toUpperCase()));
    const required = [v.toolhead_prepare_macro, v.before_pullback_macro];
    if (required.some((name) => !name)) return toast('Choose both Toolhead preparation macro and Before pullback macro.', 'error');
    const configuredMacros = [
      v.toolhead_prepare_macro, v.before_pullback_macro,
      v.select_macro, v.deselect_macro, v.verify_selected_macro,
    ].filter(Boolean);
    const invalidNames = configuredMacros.filter((name) => !/^[A-Za-z_]+$/.test(String(name)));
    if (invalidNames.length) return toast(`Macro names must use letters and underscores only: ${invalidNames.join(', ')}`, 'error');
    const missing = configuredMacros.filter((name) => !macros.has(String(name).toUpperCase()));
    if (missing.length) return toast(`Printer macro not found: ${missing.join(', ')}`, 'error');
    const prefix = ['BMCU_SET_ENDPOINT', `NAME=${gcodeValue(endpoint)}`, 'DRIVER=generic_single_extruder'];
    const groups = [
      [
        `EXTRUDER=${gcodeValue(v.extruder)}`,
        `TOOLHEAD_PREPARE_MACRO=${gcodeValue(v.toolhead_prepare_macro)}`,
        `BEFORE_PULLBACK_MACRO=${gcodeValue(v.before_pullback_macro)}`,
        `POST_GEARS_SENSOR=${gcodeValue(v.post_gears_sensor)}`,
        `ENTRY_SENSOR=${gcodeValue(v.entry_sensor)}`,
        `MOTION_SENSOR=${gcodeValue(v.motion_sensor)}`,
        `SENSOR_POLICY=${gcodeValue(v.sensor_policy)}`,
      ],
      [
        `SELECT_MACRO=${gcodeValue(v.select_macro)}`,
        `DESELECT_MACRO=${gcodeValue(v.deselect_macro)}`,
        `VERIFY_MACRO=${gcodeValue(v.verify_selected_macro)}`,
        `EXPECTED_ACTIVE_EXTRUDER=${gcodeValue(v.expected_active_extruder)}`,
        `REQUIRE_SELECT_MACRO=${v.require_select_macro ? 1 : 0}`,
        `VERIFY_ACTIVE_EXTRUDER=${v.verify_active_extruder ? 1 : 0}`,
        `SHARED_PATH_GROUP=${gcodeValue(v.shared_path_group)}`,
        `MAX_ROUTE_MM=${v.max_route_mm.toFixed(2)}`,
        `FINAL_SEARCH_MM=${v.final_search_mm.toFixed(2)}`,
        `CONTACT_TIMEOUT=${v.contact_timeout.toFixed(2)}`,
      ],
    ];
    try {
      for (let index = 0; index < groups.length; index += 1) {
        await run([...prefix, ...groups[index]].join(' '), {
          noRefresh: index < groups.length - 1,
          silent: index < groups.length - 1,
          success: index === groups.length - 1 ? `${endpoint} toolhead integration saved` : undefined,
        });
      }
      ui.genericEndpointDraft.delete(endpoint);
    } catch (error) {
      toast(error?.message || String(error), 'error');
    }
  });
  return node;
}

function updateTailTrackingSetting(node, item) {
  const [name, endpoint] = item;
  node.dataset.endpoint = name;
  text(node.querySelector('h3'), endpointLabel(name));
  const caps = endpoint.capabilities || {};
  const assigned = Array.isArray(endpoint.assigned_channels) ? endpoint.assigned_channels.length : 0;
  const savedSensors = [endpoint.post_gears_sensor, endpoint.motion_sensor, endpoint.entry_sensor].filter(Boolean);
  text(node.querySelector('.section-head p'), `${assigned} assigned channel${assigned === 1 ? '' : 's'}; ${savedSensors.length ? `sensor: ${savedSensors.join(', ')}` : 'no toolhead sensor'}`);
  const draft = ui.genericEndpointDraft.get(name);
  const value = plainObject(draft) ? draft : {
    extruder: endpoint.extruder || 'extruder',
    toolhead_prepare_macro: endpoint.toolhead_prepare_macro || '',
    before_pullback_macro: endpoint.before_pullback_macro || '',
    post_gears_sensor: endpoint.post_gears_sensor || '',
    entry_sensor: endpoint.entry_sensor || '',
    motion_sensor: endpoint.motion_sensor || '',
    sensor_policy: endpoint.sensor_policy === 'external' ? 'observe' : (endpoint.sensor_policy || 'managed'),
    contact_buffer_pct: Number(endpoint.contact_buffer_pct ?? 82),
    select_macro: endpoint.select_macro || '',
    deselect_macro: endpoint.deselect_macro || '',
    verify_selected_macro: endpoint.verify_selected_macro || '',
    expected_active_extruder: endpoint.expected_active_extruder || '',
    require_select_macro: endpoint.require_select_macro === true,
    verify_active_extruder: endpoint.verify_active_extruder === true,
    shared_path_group: endpoint.shared_path_group || name,
    max_route_mm: Number(endpoint.max_route_mm ?? 1500),
    final_search_mm: Number(endpoint.final_search_mm ?? 250),
    contact_timeout: Number(endpoint.contact_timeout ?? 45),
  };
  syncGenericDetectedFields(node, value);
  const set = (selector, next) => setGenericField(node, selector, next);
  set('.extruder-object', value.extruder);
  set('.toolhead-prepare-macro', value.toolhead_prepare_macro);
  set('.before-pullback-macro', value.before_pullback_macro);
  set('.post-gears-sensor', value.post_gears_sensor);
  set('.entry-sensor', value.entry_sensor);
  set('.motion-sensor', value.motion_sensor);
  set('.sensor-policy', value.sensor_policy);
  set('.select-macro', value.select_macro);
  set('.deselect-macro', value.deselect_macro);
  set('.verify-macro', value.verify_selected_macro);
  set('.expected-extruder', value.expected_active_extruder);
  set('.require-select', value.require_select_macro);
  set('.verify-active', value.verify_active_extruder);
  set('.shared-path', value.shared_path_group);
  set('.max-route', value.max_route_mm);
  set('.final-search', value.final_search_mm);
  set('.contact-timeout', value.contact_timeout);
  const badge = node.querySelector('.badge');
  const effectiveMode = savedSensors.length ? 'sensor' : 'manual';
  const macrosReady = Boolean(value.toolhead_prepare_macro && value.before_pullback_macro);
  badge.className = `badge ${macrosReady ? (effectiveMode === 'sensor' ? 'good' : 'warning') : 'error'}`;
  text(badge, macrosReady ? (effectiveMode === 'sensor' ? 'Ready · sensor refill' : 'Ready · manual refill') : 'Macros required');
  node.querySelector('.save-tail-setting').disabled = ui.busy || Boolean(status().active_operation);
  node.querySelector('.cancel-tail-setting').disabled = ui.busy || !ui.genericEndpointDraft.has(name);
}

const LIGHTING_DEFAULTS = {
  system_color: '#FFFFFF', system_brightness: 255,
  filament_brightness: 96,
  buffer_colors: {minimum: '#0000FF', neutral: '#FF8000', maximum: '#FF0000'},
  status_colors: {
    idle: '#383532', before_load: '#FFFF00', loading: '#00D52A', active: '#00B0FF',
    before_unload: '#FFA000', unloading: '#A02DFF', redetect: '#FFFF00', error: '#FF0000', empty: '#000000', pullback: '#A02DFF',
  },
};
const FILAMENT_BRIGHTNESS_LEVELS = [0, 64, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240, 255];

function validLightColor(value, fallback) {
  return /^#[0-9A-Fa-f]{6}$/.test(String(value || '')) ? String(value).toUpperCase() : fallback;
}

function normalizeLightingValue(raw, device = null) {
  raw = plainObject(raw) ? raw : {};
  const buffer = plainObject(raw.buffer_colors) ? raw.buffer_colors : {};
  const statuses = plainObject(raw.status_colors) ? raw.status_colors : {};
  const bufferColors = {};
  Object.entries(LIGHTING_DEFAULTS.buffer_colors).forEach(([key, value]) => {
    bufferColors[key] = validLightColor(buffer[key], value);
  });
  const statusColors = {};
  Object.entries(LIGHTING_DEFAULTS.status_colors).forEach(([key, value]) => {
    statusColors[key] = key === 'redetect' ? value : validLightColor(statuses[key], value);
  });
  statusColors.pullback = statusColors.unloading;
  return {
    system_color: validLightColor(raw.system_color || device?.system_led_color, LIGHTING_DEFAULTS.system_color),
    system_brightness: 255,
    filament_brightness: FILAMENT_BRIGHTNESS_LEVELS.includes(Number(raw.filament_brightness)) ? Number(raw.filament_brightness) : 96,
    buffer_colors: bufferColors,
    status_colors: statusColors,
  };
}

function normalizeLighting(device) {
  return normalizeLightingValue(device?.lighting, device);
}

function lightingEqual(left, right) {
  return JSON.stringify(normalizeLightingValue(left)) === JSON.stringify(normalizeLightingValue(right));
}

function lightingDefault(device) {
  return normalizeLightingValue(status().lighting_default || LIGHTING_DEFAULTS, device);
}

function lightingProfiles(device) {
  const result = {DEFAULT: lightingDefault(device)};
  if (plainObject(device?.lighting_profiles)) {
    Object.entries(device.lighting_profiles).forEach(([name, value]) => {
      if (name && name.toUpperCase() !== 'DEFAULT') result[name] = normalizeLightingValue(value);
    });
  }
  return result;
}

function lightingDraftKey(device, profile) { return `${device}:${profile}`; }

function selectedLightingProfile(device) {
  const profiles = lightingProfiles(device);
  const selected = ui.lightingProfile.get(device.name) || String(device.lighting_profile || 'DEFAULT');
  if (Object.prototype.hasOwnProperty.call(profiles, selected) ||
      ui.ledDraft.has(lightingDraftKey(device.name, selected))) return selected;
  return 'DEFAULT';
}

function lightingPreviewLocked() {
  return ['printing', 'paused', 'pause'].includes(String(status().print_state || '').toLowerCase()) ||
    Object.keys(status().active_operations || {}).length > 0;
}

function queueLightingPreview(device, group, key, value, draft) {
  if (!device?.led_preview_supported || !deviceReady(device) || lightingPreviewLocked()) return;
  let target = '';
  let scale = 255;
  let color = String(value || '').replace('#', '').toUpperCase();
  if (group === 'status_colors') { target = 'STATUS'; scale = 128; }
  else if (group === 'buffer_colors') { target = 'SECOND'; scale = 16; }
  else if (!group && key === 'system_color') {
    target = 'BOARD';
    scale = clamp(draft?.system_brightness ?? device?.lighting?.system_brightness ?? 255, 0, 255);
  } else if (!group && key === 'filament_brightness' && device?.led_filament_preview_supported) {
    target = 'FILAMENT';
    scale = clamp(value, 0, 255);
    color = '';
  }
  if (!target) return;

  const name = String(device.name || '');
  const state = ui.ledPreview.get(name) || {timer: 0, sending: false, pending: null, last: 0};
  state.pending = {target, color, scale};
  ui.ledPreview.set(name, state);
  const flush = async () => {
    state.timer = 0;
    if (state.sending || !state.pending || lightingPreviewLocked()) return;
    const next = state.pending;
    state.pending = null;
    state.sending = true;
    state.last = performance.now();
    try {
      const command = next.target === 'FILAMENT'
        ? `BMCU_LED_PREVIEW DEVICE=${gcodeValue(name)} TARGET=FILAMENT SCALE=${next.scale}`
        : `BMCU_LED_PREVIEW DEVICE=${gcodeValue(name)} TARGET=${next.target} COLOR=${next.color} SCALE=${next.scale}`;
      await run(command, {allowBusy: true, noRefresh: true, silent: true});
    } catch (_) {}
    state.sending = false;
    if (state.pending && !lightingPreviewLocked()) {
      const delay = Math.max(0, 100 - (performance.now() - state.last));
      state.timer = setTimeout(flush, delay);
    }
  };
  if (!state.timer && !state.sending) {
    const delay = Math.max(0, 100 - (performance.now() - state.last));
    state.timer = setTimeout(flush, delay);
  }
}

function lightingHexToHsv(hex) {
  const value = validLightColor(hex, '#FFFFFF').slice(1);
  const red = parseInt(value.slice(0, 2), 16) / 255;
  const green = parseInt(value.slice(2, 4), 16) / 255;
  const blue = parseInt(value.slice(4, 6), 16) / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  let hue = 0;
  if (delta) {
    if (max === red) hue = 60 * (((green - blue) / delta) % 6);
    else if (max === green) hue = 60 * (((blue - red) / delta) + 2);
    else hue = 60 * (((red - green) / delta) + 4);
  }
  if (hue < 0) hue += 360;
  return {h: hue, s: max ? delta / max : 0, v: max};
}

function lightingHsvToHex(hue, saturation, value) {
  const h = ((Number(hue) % 360) + 360) % 360;
  const s = clamp(Number(saturation), 0, 1);
  const v = clamp(Number(value), 0, 1);
  const chroma = v * s;
  const x = chroma * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - chroma;
  let red = 0, green = 0, blue = 0;
  if (h < 60) [red, green] = [chroma, x];
  else if (h < 120) [red, green] = [x, chroma];
  else if (h < 180) [green, blue] = [chroma, x];
  else if (h < 240) [green, blue] = [x, chroma];
  else if (h < 300) [red, blue] = [x, chroma];
  else [red, blue] = [chroma, x];
  return `#${[red, green, blue].map((part) => Math.round((part + m) * 255).toString(16).padStart(2, '0')).join('').toUpperCase()}`;
}

function syncLightingPickerVisual(value) {
  const picker = ui.lightingPicker;
  if (!picker) return;
  const hsv = lightingHexToHsv(value);
  picker.h = hsv.h;
  picker.s = hsv.s;
  picker.v = hsv.v;
  const sv = $('lightingPickerSv');
  const cursor = $('lightingPickerCursor');
  sv.style.setProperty('--picker-hue', `${hsv.h}`);
  cursor.style.left = `${hsv.s * 100}%`;
  cursor.style.top = `${(1 - hsv.v) * 100}%`;
  $('lightingPickerHue').value = String(Math.round(hsv.h));
  $('lightingPickerHex').value = value;
  $('lightingPickerPreview').style.background = value;
}

function lightingPickerSvValue(event) {
  const picker = ui.lightingPicker;
  const box = $('lightingPickerSv');
  if (!picker || !box) return;
  const rect = box.getBoundingClientRect();
  picker.s = clamp((event.clientX - rect.left) / rect.width, 0, 1);
  picker.v = clamp(1 - ((event.clientY - rect.top) / rect.height), 0, 1);
  setLightingPickerValue(lightingHsvToHex(picker.h, picker.s, picker.v));
}

function lightingValueAt(lighting, group, field) {
  return group ? lighting?.[group]?.[field] : lighting?.[field];
}

function closeLightingPicker() {
  ui.lightingPicker = null;
  closeDialog($('lightingColorDialog'));
  renderSettings();
}

function closeLightingPickerForDevice(deviceName) {
  if (ui.lightingPicker?.device === String(deviceName || '')) closeLightingPicker();
}

function openLightingPicker(trigger) {
  if (!trigger || trigger.disabled) return;
  const deviceName = String(trigger.dataset.lightDevice || '');
  const profile = String(trigger.dataset.lightProfile || 'DEFAULT');
  if (!deviceName) return;
  if (profile === 'DEFAULT') {
    toast('Default lighting profile is read-only. Add or select a profile to edit colours.', 'error');
    return;
  }
  if (lightingPreviewLocked()) {
    toast('Lighting changes are locked while printing or BMCU motion is active.', 'error');
    return;
  }
  const device = devices().find((item) => item.name === deviceName);
  if (!device) return;
  const group = String(trigger.dataset.lightGroup || '');
  const field = String(trigger.dataset.lightKey || '');
  const profiles = lightingProfiles(device);
  const saved = profiles[profile] || normalizeLighting(device);
  const draft = ui.ledDraft.get(lightingDraftKey(deviceName, profile)) || saved;
  const current = validLightColor(lightingValueAt(draft, group, field), '#FFFFFF');
  const defaultValue = validLightColor(lightingValueAt(lightingDefault(device), group, field), current);
  ui.lightingPicker = {device: deviceName, profile, group, field, original: current, defaultValue};
  text($('lightingPickerTitle'), trigger.closest('.lighting-color, .lighting-system-field')?.querySelector('span')?.textContent || 'Colour');
  syncLightingPickerVisual(current);
  $('lightingPickerBack').title = `Back to ${current}`;
  $('lightingPickerDefault').title = `Default ${defaultValue}`;
  openDialog($('lightingColorDialog'));
  queueLightingPreview(device, group, field, current, draft);
}

function setLightingPickerValue(rawValue) {
  const picker = ui.lightingPicker;
  if (!picker) return;
  if (lightingPreviewLocked()) { closeLightingPicker(); return; }
  const value = validLightColor(rawValue, '');
  if (!value) return;
  const device = devices().find((item) => item.name === picker.device);
  if (!device) return;
  const profiles = lightingProfiles(device);
  const key = lightingDraftKey(picker.device, picker.profile);
  const saved = profiles[picker.profile] || normalizeLighting(device);
  const draft = cloneState(ui.ledDraft.get(key) || saved);
  if (picker.group) draft[picker.group][picker.field] = value;
  else draft[picker.field] = value;
  if (Object.prototype.hasOwnProperty.call(profiles, picker.profile) && lightingEqual(draft, saved)) ui.ledDraft.delete(key);
  else ui.ledDraft.set(key, draft);
  syncLightingPickerVisual(value);
  queueLightingPreview(device, picker.group, picker.field, value, draft);
}

function lightingProfileCommand(device, profile, lighting, action = 'SET') {
  const params = [
    `BMCU_LIGHTING_PROFILE DEVICE=${gcodeValue(device)} ACTION=${action} PROFILE=${gcodeValue(profile)}`,
  ];
  if (action === 'SET') params.push(
    `SYSTEM_COLOR=${lighting.system_color.slice(1)}`,
    `FILAMENT_BRIGHTNESS=${lighting.filament_brightness}`,
    `BUFFER_MIN_COLOR=${lighting.buffer_colors.minimum.slice(1)}`,
    `BUFFER_NEUTRAL_COLOR=${lighting.buffer_colors.neutral.slice(1)}`,
    `BUFFER_MAX_COLOR=${lighting.buffer_colors.maximum.slice(1)}`,
    `STATUS_IDLE_COLOR=${lighting.status_colors.idle.slice(1)}`,
    `STATUS_BEFORE_LOAD_COLOR=${lighting.status_colors.before_load.slice(1)}`,
    `STATUS_LOADING_COLOR=${lighting.status_colors.loading.slice(1)}`,
    `STATUS_ACTIVE_COLOR=${lighting.status_colors.active.slice(1)}`,
    `STATUS_BEFORE_UNLOAD_COLOR=${lighting.status_colors.before_unload.slice(1)}`,
    `STATUS_RETRACTING_COLOR=${lighting.status_colors.unloading.slice(1)}`,
    `STATUS_ERROR_COLOR=${lighting.status_colors.error.slice(1)}`,
    `STATUS_EMPTY_COLOR=${lighting.status_colors.empty.slice(1)}`,
  );
  return params.join(' ');
}

function createLightingColor(label, group, key, help = '') {
  const name = help
    ? `<span class="lighting-color-name"><span>${label}</span><span class="lighting-help" tabindex="0" role="note" data-tip="${help}" aria-label="${label}: ${help}">?</span></span>`
    : `<span>${label}</span>`;
  return `<div class="lighting-color">${name}<button class="lighting-color-trigger" type="button" data-light-color data-light-group="${group}" data-light-key="${key}"><i class="lighting-color-swatch"></i><code class="lighting-color-value"></code></button></div>`;
}

function createSystemLedSetting() {
  const node = document.createElement('article');
  node.className = 'lighting-setting';
  node.innerHTML = `
    <div class="lighting-head"><div><strong></strong><small>Profiles are saved by Klipper and applied whenever this BMCU is ready.</small></div><span class="badge lighting-state"></span></div>
    <div class="u1-profile-layout lighting-profile-layout">
      <div class="u1-profile-list">
        <div class="u1-profile-table-wrap"><table class="u1-profile-table"><thead><tr><th>Profile</th></tr></thead><tbody class="lighting-profile-rows"></tbody></table></div>
        <div class="u1-profile-add"><label class="field"><span>Add profile</span><input class="lighting-profile-name" type="text" maxlength="40" placeholder="Night, Colourful..."></label><button class="button secondary" type="button" data-action="add-lighting-profile">Add</button></div>
      </div>
      <div class="u1-profile-editor lighting-profile-editor">
        <div class="u1-profile-editor-head"><div><h3 class="lighting-profile-title">Default</h3><small class="lighting-profile-rule"></small></div><span class="badge lighting-editor-state"></span></div>
        <div class="lighting-controls aligned-fields">
          <div class="field lighting-system-field"><span>System colour</span><div class="lighting-system-control"><button class="lighting-color-trigger" type="button" data-light-color data-light-key="system_color"><i class="lighting-color-swatch"></i><code class="lighting-color-value"></code></button></div><small>Choose a colour closer to black for a dimmer system light.</small></div>
          <label class="field"><span>Filament colour level</span><select data-light-key="filament_brightness"><option value="0">0% - Off</option><option value="64">25%</option><option value="96">38%</option><option value="112">44%</option><option value="128">50%</option><option value="144">56%</option><option value="160">63%</option><option value="176">69%</option><option value="192">75%</option><option value="208">82%</option><option value="224">88%</option><option value="240">94%</option><option value="255">100%</option></select><small>Brightness applied to configured filament colours. Changing it previews red, yellow, green and blue on the four channel LEDs.</small></label>
        </div>
        <div class="lighting-colours">
          <h4>Buffer position</h4><div class="lighting-colour-grid buffer-colours"></div>
          <div class="status-colours"></div>
          <p class="muted lighting-note">Red system breathing means no host communication. Yellow means calibration or setup is required.</p>
        </div>
        <div class="lighting-actions"><button class="button secondary" type="button" data-action="delete-lighting-profile">Delete</button><button class="button secondary" type="button" data-action="apply-lighting-profile">Use profile</button><span></span><button class="button secondary" type="button" data-action="cancel-lighting">Cancel</button><button class="button primary" type="button" data-action="save-lighting-profile">Save and use</button></div>
      </div>
    </div>`;
  node.querySelector('.buffer-colours').innerHTML = [
    createLightingColor('Minimum', 'buffer_colors', 'minimum'),
    createLightingColor('Neutral', 'buffer_colors', 'neutral'),
    createLightingColor('Maximum', 'buffer_colors', 'maximum'),
  ].join('');
  node.querySelector('.status-colours').innerHTML = `
    <h4>Channel status</h4><div class="lighting-colour-grid">${[
      createLightingColor('Idle', 'status_colors', 'idle', 'Filament is detected in this BMCU channel, but the channel is not currently loading, unloading or in use.'),
      createLightingColor('Empty', 'status_colors', 'empty', 'No filament is detected in this BMCU channel.'),
      createLightingColor('Error', 'status_colors', 'error', 'The channel is in a fault or stop state, for example after a failed autoload sequence, an inconsistent sensor state or a motion/buffer fault.'),
    ].join('')}</div>
    <h4>Loading</h4><div class="lighting-colour-grid">${[
      createLightingColor('Loading', 'status_colors', 'loading', 'BMCU is feeding filament forward until it reaches the toolhead loading point.'),
      createLightingColor('Toolhead preparation', 'status_colors', 'before_load', 'Filament has reached the toolhead stage. BMCU manages the buffer while the printer completes the toolhead-side loading sequence and sensor handling before In use.'),
      createLightingColor('In use', 'status_colors', 'active', 'Filament is correctly loaded and this channel is active for printing. This is the normal status while the printer uses this filament.'),
    ].join('')}</div>
    <h4>Unloading</h4><div class="lighting-colour-grid">${[
      createLightingColor('Before retracting', 'status_colors', 'before_unload', 'Pre-unload stage before the final BMCU retract. Toolhead-side unloading happens here, including tip forming when configured, while BMCU manages the buffer.'),
      createLightingColor('Retracting', 'status_colors', 'unloading', 'BMCU is retracting filament by the configured retract distance for this channel.'),
    ].join('')}</div>`;
  return node;
}

function updateSystemLedSetting(node, device) {
  node.dataset.device = device.name;
  text(node.querySelector('.lighting-head strong'), deviceLabel(device));
  const profiles = lightingProfiles(device);
  const defaults = lightingDefault(device);
  const selected = selectedLightingProfile(device);
  ui.lightingProfile.set(device.name, selected);
  const key = lightingDraftKey(device.name, selected);
  const draft = ui.ledDraft.get(key);
  const exists = Object.prototype.hasOwnProperty.call(profiles, selected);
  const saved = exists ? profiles[selected] : normalizeLighting(device);
  const value = draft || saved;
  const dirty = Boolean(draft && (!exists || !lightingEqual(draft, saved)));
  const active = String(device.lighting_profile || 'DEFAULT');

  const rows = node.querySelector('.lighting-profile-rows');
  const names = new Set(Object.keys(profiles));
  for (const draftKey of ui.ledDraft.keys()) {
    const prefix = `${device.name}:`;
    if (draftKey.startsWith(prefix)) names.add(draftKey.slice(prefix.length));
  }
  rows.replaceChildren(...[...names].sort((a, b) => {
    if (a === 'DEFAULT') return -1;
    if (b === 'DEFAULT') return 1;
    return a.localeCompare(b);
  }).map((name) => {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'u1-profile-row';
    button.dataset.lightProfile = name;
    button.dataset.device = device.name;
    button.classList.toggle('active', name === selected);
    button.classList.toggle('unsaved', ui.ledDraft.has(lightingDraftKey(device.name, name)));
    const strong = document.createElement('strong');
    const small = document.createElement('small');
    strong.textContent = name === 'DEFAULT' ? 'Default' : name;
    small.textContent = name === active ? 'In use' : name === 'DEFAULT' ? 'Built-in · read only' : 'Saved profile';
    button.append(strong, small); td.append(button); tr.append(td); return tr;
  }));

  const immutable = selected === 'DEFAULT';
  const lightingLocked = lightingPreviewLocked();
  text(node.querySelector('.lighting-profile-title'), immutable ? 'Default' : selected);
  text(node.querySelector('.lighting-profile-rule'), immutable
    ? 'Built-in package profile · cannot be changed or deleted'
    : 'Named profile for this BMCU module');
  const editorState = node.querySelector('.lighting-editor-state');
  editorState.className = `badge lighting-editor-state ${dirty ? 'warning' : active === selected ? 'good' : ''}`;
  text(editorState, dirty ? 'Unsaved' : active === selected ? 'In use' : 'Saved');
  const headState = node.querySelector('.lighting-state');
  headState.className = `badge lighting-state ${device.lighting_runtime_error ? 'bad' : 'good'}`;
  text(headState, device.lighting_runtime_error ? 'Sync pending' : 'Ready');
  const note = node.querySelector('.lighting-note');
  const noteText = lightingLocked
    ? 'Lighting changes are locked while printing or BMCU motion is active.'
    : device.led_preview_supported ? '' : 'Live colour preview requires matching BMCU firmware.';
  text(note, noteText);
  note.classList.toggle('hidden', !noteText);

  qsa('[data-light-key]', node).forEach((control) => {
    control.dataset.lightDevice = device.name;
    control.dataset.lightProfile = selected;
    const group = control.dataset.lightGroup;
    const raw = group ? value[group][control.dataset.lightKey] : value[control.dataset.lightKey];
    const next = String(raw);
    const colorTrigger = control.dataset.lightColor !== undefined;
    control.disabled = ui.busy || (!colorTrigger && (immutable || lightingLocked));
    if (colorTrigger) {
      const swatch = control.querySelector('.lighting-color-swatch');
      const label = control.querySelector('.lighting-color-value');
      if (swatch) swatch.style.background = next;
      if (label) label.textContent = next;
      control.title = `Choose ${next}`;
    } else if (document.activeElement !== control && control.value !== next) {
      control.value = next;
    }
  });
  qsa('[data-action]', node).forEach((button) => { button.dataset.device = device.name; button.dataset.profile = selected; });
  node.querySelector('[data-action="save-lighting-profile"]').disabled = ui.busy || lightingLocked || immutable || !dirty;
  node.querySelector('[data-action="cancel-lighting"]').disabled = ui.busy || lightingLocked || !dirty;
  node.querySelector('[data-action="delete-lighting-profile"]').disabled = ui.busy || lightingLocked || immutable || !exists;
  node.querySelector('[data-action="apply-lighting-profile"]').disabled = ui.busy || lightingLocked || dirty || !exists || active === selected;
  node.querySelector('[data-action="add-lighting-profile"]').disabled = ui.busy || lightingLocked;
}

const U1_MOVEMENT_FIELDS = [
  {key: 'rigid_standard', input: 'u1MoveRigidStandard', preview: 'u1DefaultRigidStandard', snapmakerPreview: 'u1SnapmakerRigidStandard', timing: 'u1TimingRigidStandard', timingLines: 'u1TimingRigidStandardLines'},
  {key: 'soft_standard', input: 'u1MoveSoftStandard', preview: 'u1DefaultSoftStandard', snapmakerPreview: 'u1SnapmakerSoftStandard', timing: 'u1TimingSoftStandard', timingLines: 'u1TimingSoftStandardLines'},
  {key: 'rigid_fine', input: 'u1MoveRigidFine', preview: 'u1DefaultRigidFine', snapmakerPreview: 'u1SnapmakerRigidFine', timing: 'u1TimingRigidFine', timingLines: 'u1TimingRigidFineLines'},
  {key: 'soft_fine', input: 'u1MoveSoftFine', preview: 'u1DefaultSoftFine', snapmakerPreview: 'u1SnapmakerSoftFine', timing: 'u1TimingSoftFine', timingLines: 'u1TimingSoftFineLines'},
];

function u1TipTimingLimits() {
  const raw = plainObject(u1ProfilesPayload().timing) ? u1ProfilesPayload().timing : {};
  const acceleration = Number(raw.max_e_accel_mm_s2);
  const velocity = Number(raw.max_e_velocity_mm_s);
  return {
    acceleration: finite(acceleration) && acceleration > 0 ? acceleration : 5000,
    velocity: finite(velocity) && velocity > 0 ? velocity : 100,
    live: finite(acceleration) && acceleration > 0 && finite(velocity) && velocity > 0,
  };
}

function u1TipMoveDuration(distance, feedMmMin, limits) {
  const length = Math.abs(Number(distance));
  const requested = Number(feedMmMin) / 60;
  if (!finite(length) || length <= 0 || !finite(requested) || requested <= 0) return null;
  const speed = Math.min(requested, limits.velocity);
  const peak = Math.min(speed, Math.sqrt(limits.acceleration * length));
  if (!finite(peak) || peak <= 0) return null;
  const accelTime = peak / limits.acceleration;
  const accelDistance = peak * peak / (2 * limits.acceleration);
  const cruiseDistance = Math.max(0, length - 2 * accelDistance);
  return 2 * accelTime + cruiseDistance / peak;
}

function u1TipTiming(script) {
  const limits = u1TipTimingLimits();
  const entries = [];
  let total = 0;
  let markerTime = null;
  const lines = String(script || '').split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const source = lines[index].trim();
    if (!source || source.startsWith(';') || source.startsWith('#')) continue;
    const upper = source.toUpperCase();
    let seconds = 0;
    let note = '';
    let validLine = true;
    if (upper === 'BMCU_PARK_HEAD') {
      if (markerTime == null) markerTime = total;
      note = 'park boundary';
    } else if (upper === 'BMCU_TEMP_RESET' || upper.startsWith('BMCU_TEMP ')) {
      note = 'non-blocking temperature target';
    } else if (upper === 'BMCU_HOTEND_FAN_RESET' || upper.startsWith('BMCU_HOTEND_FAN ')) {
      note = 'non-blocking source hotend-fan override';
    } else if (/^G[01](?:\s|$)/.test(upper)) {
      const e = upper.match(/(?:^|\s)E([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s|$)/);
      const f = upper.match(/(?:^|\s)F([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s|$)/);
      seconds = e && f ? u1TipMoveDuration(Number(e[1]), Number(f[1]), limits) : null;
      validLine = seconds != null;
      if (validLine) total += seconds;
    } else if (/^G4(?:\s|$)/.test(upper)) {
      const dwell = upper.match(/^G4\s+([PS])([+]?(?:\d+(?:\.\d*)?|\.\d+))$/);
      if (dwell) {
        seconds = Number(dwell[2]) * (dwell[1] === 'P' ? 0.001 : 1);
        total += seconds;
        note = 'dwell';
      } else {
        seconds = null;
        validLine = false;
      }
    } else {
      seconds = null;
      validLine = false;
    }
    entries.push({line: index + 1, source, seconds, note, valid: validLine});
  }
  return {
    total,
    markerTime,
    postMarker: markerTime == null ? null : Math.max(0, total - markerTime),
    entries,
    limits,
  };
}

function u1FormatSeconds(seconds) {
  if (!finite(seconds)) return '—';
  if (seconds < 0.001) return '0.000 s';
  return `${seconds.toFixed(seconds < 10 ? 3 : 2)} s`;
}

function renderU1MovementTiming(field, script) {
  const summary = $(field.timing);
  const lines = $(field.timingLines);
  if (!summary || !lines) return;
  const result = u1TipTiming(script);
  const suffix = result.limits.live ? '' : ' · fallback limits';
  if (result.markerTime == null) {
    text(summary, `≈ ${u1FormatSeconds(result.total)} total · no BMCU_PARK_HEAD${suffix}`);
  } else {
    text(summary, `≈ ${u1FormatSeconds(result.total)} total · ${u1FormatSeconds(result.markerTime)} to park marker · ${u1FormatSeconds(result.postMarker)} after marker${suffix}`);
  }
  const rows = result.entries.map((entry) => {
    const row = document.createElement('div');
    row.className = `u1-timing-row${entry.valid ? '' : ' invalid'}`;
    const command = document.createElement('code');
    command.textContent = `${entry.line}. ${entry.source}`;
    const duration = document.createElement('span');
    duration.textContent = `${u1FormatSeconds(entry.seconds)}${entry.note ? ` · ${entry.note}` : ''}`;
    row.append(command, duration);
    return row;
  });
  lines.replaceChildren(...rows);
}

function u1ProfileDraftKey(profile) {
  return String(profile || 'DEFAULT');
}

function u1ProfilesPayload() {
  return plainObject(status().u1_tip_profiles) ? status().u1_tip_profiles : {};
}

function normalizeU1TipProfile(raw, fallback = {}) {
  const fallbackMovements = plainObject(fallback?.movements) ? fallback.movements : {};
  const rawMovements = plainObject(raw?.movements) ? raw.movements : {};
  const movements = {};
  for (const {key} of U1_MOVEMENT_FIELDS) {
    movements[key] = String(rawMovements[key] ?? fallbackMovements[key] ?? '');
  }
  const mode = String(raw?.temperature_mode ?? fallback?.temperature_mode ?? 'project');
  const temperatureMode = ['default', 'project', 'custom'].includes(mode) ? mode : 'project';
  const rawTemperature = raw?.temperature ?? fallback?.temperature ?? null;
  return {
    mode: 'movements',
    gcode: String(fallback?.gcode ?? raw?.gcode ?? ''),
    movements,
    temperature_mode: temperatureMode,
    temperature: temperatureMode === 'custom' && rawTemperature != null ? Number(rawTemperature) : null,
  };
}

function u1ProfileSavedFromPayload(profileName, payload) {
  payload = plainObject(payload) ? payload : {};
  const packageDefault = normalizeU1TipProfile(payload.package_default);
  const globalDefault = normalizeU1TipProfile(payload.default, packageDefault);
  const isDefault = profileName === 'DEFAULT';
  const material = isDefault ? null : payload.materials?.[profileName];
  return {
    value: isDefault ? globalDefault : normalizeU1TipProfile(material, globalDefault),
    packageDefault,
    exists: isDefault || Boolean(material),
    custom: isDefault ? Boolean(payload.default_is_custom) : Boolean(material),
    valid: payload.valid !== false,
    error: String(payload.error || ''),
  };
}

function u1ProfileSaved(profileName) {
  return u1ProfileSavedFromPayload(profileName, u1ProfilesPayload());
}

function u1ProfileCurrent(profileName) {
  const draft = ui.u1GcodeDraft.get(u1ProfileDraftKey(profileName));
  return draft?.value || u1ProfileSaved(profileName).value;
}

function u1ProfileNames() {
  const payload = u1ProfilesPayload();
  const names = new Set(Object.keys(payload.materials || {}));
  for (const key of ui.u1GcodeDraft.keys()) {
    if (key !== 'DEFAULT') names.add(key);
  }
  return ['DEFAULT', ...[...names].sort((a, b) => a.localeCompare(b))];
}

function u1ProfilesEqual(left, right) {
  return JSON.stringify(normalizeU1TipProfile(left)) === JSON.stringify(normalizeU1TipProfile(right));
}

function normalizeMaterialEditorValue(value) {
  return String(value || '').trim().toUpperCase();
}

function materialEditorValid(value) {
  return /^[A-Z0-9][A-Z0-9 ._+/-]{0,39}$/.test(value);
}

function createU1ProfileRow() {
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'u1-profile-row';
  button.dataset.u1Profile = '';
  const name = document.createElement('strong');
  const rule = document.createElement('small');
  button.append(name, rule);
  cell.appendChild(button);
  row.appendChild(cell);
  return row;
}

function updateU1ProfileRow(row, profile) {
  const button = row.querySelector('.u1-profile-row');
  button.dataset.u1Profile = profile;
  button.classList.toggle('active', profile === ui.u1MaterialProfile);
  button.classList.toggle('unsaved', ui.u1GcodeDraft.has(u1ProfileDraftKey(profile)));
  text(button.querySelector('strong'), profile === 'DEFAULT' ? 'Default' : profile);
  text(button.querySelector('small'), profile === 'DEFAULT' ? 'Fallback' : 'Material override');
}

function renderU1GcodeSettings() {
  const visible = printerInfo().topology === 'snapmaker_u1';
  setClass($('u1GcodeSection'), 'hidden', !visible);
  if (!visible) return;
  const names = u1ProfileNames();
  if (!names.includes(ui.u1MaterialProfile)) ui.u1MaterialProfile = 'DEFAULT';
  syncKeyed($('u1ProfileRows'), names, (profile) => profile,
    createU1ProfileRow, updateU1ProfileRow);

  const profile = ui.u1MaterialProfile;
  const saved = u1ProfileSaved(profile);
  const value = u1ProfileCurrent(profile);
  const draftKey = u1ProfileDraftKey(profile);
  const dirty = ui.u1GcodeDraft.has(draftKey);

  const editor = $('u1ProfileEditor');
  editor.classList.toggle('is-dirty', dirty);
  text($('u1ProfileTitle'), profile === 'DEFAULT' ? 'Default' : profile);
  text($('u1ProfileRule'), profile === 'DEFAULT'
    ? 'Used when no material override exists'
    : 'Used only for an exact material match');
  const badge = $('u1GcodeState');
  badge.className = `badge ${!saved.valid ? 'bad' : dirty ? 'warning' : 'good'}`;
  text(badge, !saved.valid ? 'Invalid' : dirty ? 'Unsaved' : 'Saved');

  const temperatureMode = ['default', 'project', 'custom'].includes(value.temperature_mode)
    ? value.temperature_mode : 'project';
  if (document.activeElement !== $('u1TipTempMode')) $('u1TipTempMode').value = temperatureMode;
  if (document.activeElement !== $('u1TipTemp')) {
    $('u1TipTemp').value = value.temperature == null ? '220' : String(Math.round(Number(value.temperature)));
  }
  setClass($('u1TipTempCustomField'), 'hidden', temperatureMode !== 'custom');
  const temperatureHelp = {
    default: 'First empty-Head load uses Snapmaker stock preparation; later loads and unload tip forming use Snapmaker material temperatures.',
    project: 'First empty-Head load uses Snapmaker stock preparation; later loads and unload tip forming use the current print / G-code target.',
    custom: 'First empty-Head load uses Snapmaker stock preparation; later loads and unload tip forming use the custom temperature.',
  };
  text($('u1TipTempExplanation'), temperatureHelp[temperatureMode]);

  const snapmakerMovements = plainObject(u1ProfilesPayload().snapmaker_stock_movements)
    ? u1ProfilesPayload().snapmaker_stock_movements : {};
  for (const movementField of U1_MOVEMENT_FIELDS) {
    const {key, input, preview, snapmakerPreview} = movementField;
    const field = $(input);
    const next = String(value.movements?.[key] || '');
    if (document.activeElement !== field && field.value !== next) field.value = next;
    text($(preview), saved.packageDefault.movements?.[key] || '');
    if (snapmakerPreview) text($(snapmakerPreview), snapmakerMovements[key] || '');
    renderU1MovementTiming(movementField, field.value);
  }
  $('u1GcodeSave').disabled = ui.busy || !dirty;
  $('u1GcodeCancel').disabled = ui.busy || !dirty;
  const canResetSavedDefault = profile === 'DEFAULT' && saved.custom;
  $('u1GcodeReset').disabled = ui.busy ||
    (!canResetSavedDefault && u1ProfilesEqual(value, saved.packageDefault));
  text($('u1GcodeReset'), profile === 'DEFAULT' ? 'Restore BMCU stock default' : 'Set to BMCU stock values');
  text($('u1GcodeDelete'), 'Use Default profile');
  $('u1GcodeDelete').disabled = ui.busy;
  setClass($('u1GcodeDelete'), 'hidden', profile === 'DEFAULT');
}

function utf8UrlSafeBase64(value) {
  const bytes = new TextEncoder().encode(String(value || ''));
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x4000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x4000));
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function readU1MovementInputs() {
  const movements = {};
  for (const {key, input} of U1_MOVEMENT_FIELDS) movements[key] = $(input).value;
  return movements;
}

function updateU1ProfileDraft() {
  const profile = ui.u1MaterialProfile;
  if (!profile) return;
  const key = u1ProfileDraftKey(profile);
  const saved = u1ProfileSaved(profile);
  const existing = ui.u1GcodeDraft.get(key);
  const rawMode = $('u1TipTempMode').value;
  const temperatureMode = ['default', 'project', 'custom'].includes(rawMode) ? rawMode : 'project';
  const value = normalizeU1TipProfile({
    mode: 'movements',
    movements: readU1MovementInputs(),
    temperature_mode: temperatureMode,
    temperature: temperatureMode === 'custom' ? Number($('u1TipTemp').value) : null,
  }, saved.value);
  if (!existing?.isNew && u1ProfilesEqual(value, saved.value)) ui.u1GcodeDraft.delete(key);
  else ui.u1GcodeDraft.set(key, {value, isNew: Boolean(existing?.isNew)});
  renderU1GcodeSettings();
}

function resetU1Movement(key) {
  const field = U1_MOVEMENT_FIELDS.find((item) => item.key === key);
  if (!field) return;
  const defaults = u1ProfileSaved(ui.u1MaterialProfile).packageDefault;
  $(field.input).value = String(defaults.movements?.[key] || '');
  updateU1ProfileDraft();
}

function u1ProfileSaveConfirmedFromPayload(profile, expected, resetDefault, payload) {
  const saved = u1ProfileSavedFromPayload(profile, payload);
  if (!saved.valid) return false;
  if (resetDefault && profile === 'DEFAULT') {
    return !saved.custom && u1ProfilesEqual(saved.value, saved.packageDefault);
  }
  return saved.custom && u1ProfilesEqual(saved.value, expected);
}

function waitMs(delay) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(delay) || 0)));
}

async function readBmcuSnapshotDirect() {
  const response = await fetch('/moonraker/printer/objects/query?bmcu', {cache: 'no-store'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  const bmcu = payload?.result?.status?.bmcu;
  if (!plainObject(bmcu)) throw new Error('BMCU status snapshot is unavailable');
  return bmcu;
}

async function confirmU1ProfileSave(profile, expected, resetDefault = false) {

  for (const delay of [0, 80, 180]) {
    if (delay) await waitMs(delay);
    try {
      const snapshot = await readBmcuSnapshotDirect();
      const payload = snapshot.u1_tip_profiles;
      if (u1ProfileSaveConfirmedFromPayload(
          profile, expected, resetDefault, payload)) {

        transport.store.mergeStatus({u1_tip_profiles: cloneState(payload)},
          transport.socketLive() ? 'live' : 'polling');
        return true;
      }
    } catch (_) {

    }
  }
  return false;
}

async function saveU1Gcode() {
  const profile = ui.u1MaterialProfile;
  const key = u1ProfileDraftKey(profile);
  const draft = ui.u1GcodeDraft.get(key);
  if (!draft) return;
  const expected = normalizeU1TipProfile(draft.value);
  const resetDefault = draft.reset === true && profile === 'DEFAULT';
  const temperature = Number(draft.value.temperature);
  if (draft.value.temperature_mode === 'custom' &&
      (!finite(temperature) || temperature < 170 || temperature > 300)) {
    throw new Error('Tip-forming temperature must be between 170 and 300 C');
  }
  ui.busy = true;
  renderU1GcodeSettings();
  try {
    if (resetDefault) {
      await run('BMCU_TIP_PROFILE PROFILE=DEFAULT ACTION=RESET', {allowBusy: true, noRefresh: true, silent: true});
    } else {
      const payload = JSON.stringify(draft.value);
      await run(`BMCU_TIP_PROFILE PROFILE=${gcodeValue(profile)} ACTION=SET DATA=${utf8UrlSafeBase64(payload)}`, {allowBusy: true, noRefresh: true, silent: true});
    }

    const livePayload = cloneState(u1ProfilesPayload());
    const packageDefault = normalizeU1TipProfile(
      livePayload?.package_default || u1ProfileSaved(profile).packageDefault);
    if (resetDefault && profile === 'DEFAULT') {
      livePayload.default = cloneState(packageDefault);
      livePayload.default_is_custom = false;
    } else if (profile === 'DEFAULT') {
      livePayload.default = cloneState(expected);
      livePayload.default_is_custom = true;
    } else {
      if (!plainObject(livePayload.materials)) livePayload.materials = {};
      livePayload.materials[profile] = cloneState(expected);
    }
    transport.store.mergeStatus({u1_tip_profiles: livePayload},
      transport.socketLive() ? 'live' : 'polling');
    ui.u1GcodeDraft.delete(key);
    toast(resetDefault ? 'Snapmaker defaults restored' : 'Tip-forming profile saved', 'success');
    void (async () => {
      for (const delay of [300, 1000, 2500]) {
        await waitMs(delay);
        if (await confirmU1ProfileSave(profile, expected, resetDefault)) return;
      }
    })();
  } finally {
    ui.busy = false;
    renderU1GcodeSettings();
  }
}

function cancelU1Gcode() {
  ui.u1GcodeDraft.delete(u1ProfileDraftKey(ui.u1MaterialProfile));
  renderU1GcodeSettings();
}

function resetU1Gcode() {
  const profile = ui.u1MaterialProfile;
  const key = u1ProfileDraftKey(profile);
  const existing = ui.u1GcodeDraft.get(key);
  ui.u1GcodeDraft.set(key, {
    value: normalizeU1TipProfile(u1ProfileSaved(profile).packageDefault),
    isNew: Boolean(existing?.isNew),
    reset: profile === 'DEFAULT',
  });
  renderU1GcodeSettings();
}

function addU1MaterialProfile() {
  const material = normalizeMaterialEditorValue($('u1MaterialName').value);
  if (material === 'DEFAULT' || !/^[A-Z0-9][A-Z0-9 ._+/-]{0,39}$/.test(material)) {
    toast('Enter a material such as PLA, PETG or TPU. Default is reserved.', 'error');
    return;
  }
  ui.u1MaterialProfile = material;
  if (!u1ProfileNames().includes(material)) {
    ui.u1GcodeDraft.set(u1ProfileDraftKey(material), {
      value: normalizeU1TipProfile(u1ProfileSaved('DEFAULT').value),
      isNew: true,
    });
  }
  $('u1MaterialName').value = '';
  renderU1GcodeSettings();
}

function deleteU1MaterialProfile() {
  const profile = ui.u1MaterialProfile;
  if (profile === 'DEFAULT') return;
  const saved = u1ProfileSaved(profile);
  const key = u1ProfileDraftKey(profile);
  if (!saved.exists) {
    ui.u1GcodeDraft.delete(key);
    ui.u1MaterialProfile = 'DEFAULT';
    renderU1GcodeSettings();
    return;
  }
  confirmAction('Use Default profile?', `${profile} material override will be removed.`, async () => {
    ui.busy = true;
    try {
      await run(`BMCU_TIP_PROFILE PROFILE=${gcodeValue(profile)} ACTION=DELETE`, {allowBusy: true, noRefresh: true});
      ui.u1GcodeDraft.delete(key);
      ui.u1MaterialProfile = 'DEFAULT';
      toast(`${profile} now uses Default`, 'success');
      await transport.refreshNow();
    } finally {
      ui.busy = false;
      renderU1GcodeSettings();
    }
  });
}

function createLoadingHandoffSetting() {
  const node = document.createElement('div');
  node.className = 'bmcu-setting-control bmcu-handoff-control';
  const options = Array.from({length: 39}, (_, index) => {
    const value = index + 60;
    return `<option value="${value}">${value}%</option>`;
  }).join('');
  node.innerHTML = `
    <label class="field"><span>Threshold</span><select class="loading-handoff-target">${options}</select></label>
    <button class="button primary save-loading-handoff" type="button">Save</button>`;
  node.querySelector('.save-loading-handoff').addEventListener('click', async () => {
    const device = String(node.dataset.device || '');
    const target = Number(node.querySelector('.loading-handoff-target').value);
    if (!device || !Number.isInteger(target) || target < 60 || target > 98) {
      toast('Loading handoff threshold must be an integer from 60% to 98%.', 'error');
      return;
    }
    await run(`BMCU_HANDOFF DEVICE=${gcodeValue(device)} TARGET=${target}`, {
      success: `${deviceLabel(devices().find((item) => item.name === device) || {name: device})} loading handoff set to ${target}%`,
    });
  });
  return node;
}

function updateLoadingHandoffSetting(node, device) {
  node.dataset.device = device.name;
  const target = Math.round(clamp(Number(device.motion_config?.loading_handoff_pct ?? 82), 60, 98));
  const select = node.querySelector('.loading-handoff-target');
  if (document.activeElement !== select) select.value = String(target);
  const disabled = ui.busy || Boolean(status().active_operation);
  select.disabled = disabled;
  node.querySelector('.save-loading-handoff').disabled = disabled;
}

function createLoadPressureSetting() {
  const node = document.createElement('div');
  node.className = 'bmcu-setting-control bmcu-pressure-control';
  const options = Array.from({length: 21}, (_, index) => {
    const value = index + 75;
    return `<option value="${value}">${value}%</option>`;
  }).join('');
  node.innerHTML = `
    <label class="field bmcu-pressure-field"><span>Pressure</span><select class="load-pressure-target">${options}</select></label>
    <button class="button primary save-load-pressure" type="button">Save</button>
    <span class="badge warning bmcu-pressure-status hidden">Firmware update required</span>`;
  node.querySelector('.save-load-pressure').addEventListener('click', async () => {
    const device = String(node.dataset.device || '');
    const target = Number(node.querySelector('.load-pressure-target').value);
    if (!device || !Number.isInteger(target) || target < 75 || target > 95) {
      toast('Buffer pressure must be an integer from 75% to 95%.', 'error');
      return;
    }
    await run(`BMCU_PRESSURE DEVICE=${gcodeValue(device)} TARGET=${target} SAVE=1`, {
      success: `${deviceLabel(devices().find((item) => item.name === device) || {name: device})} pressure set to ${target}%`,
    });
  });
  return node;
}

function updateLoadPressureSetting(node, device) {
  node.dataset.device = device.name;
  const ready = deviceReady(device);
  const supported = device.load_pressure_runtime_supported === true;
  const target = Math.round(clamp(Number(device.motion_config?.load_pressure_pct ?? 82), 75, 95));
  const select = node.querySelector('.load-pressure-target');
  if (document.activeElement !== select) select.value = String(target);
  const disabled = ui.busy || !ready || !supported || Boolean(status().active_operation);
  select.disabled = disabled;
  const button = node.querySelector('.save-load-pressure');
  button.disabled = disabled;
  const badge = node.querySelector('.bmcu-pressure-status');
  if (!ready) {
    text(badge, 'Connect BMCU to change');
    setClass(badge, 'hidden', false);
  } else if (!supported) {
    text(badge, 'Firmware update required');
    setClass(badge, 'hidden', false);
  } else {
    setClass(badge, 'hidden', true);
  }
}

function createBmcuSettingsDevice() {
  const node = document.createElement('article');
  node.className = 'bmcu-settings-device';
  node.innerHTML = `
    <div class="section-head bmcu-settings-device-head"><div><h3></h3><p class="bmcu-settings-device-state"></p></div><span class="badge bmcu-settings-device-badge"></span></div>
    <section class="bmcu-tuning-grid">
      <div class="bmcu-tuning-card">
        <div class="bmcu-settings-group-head"><strong>Loading -> preparation threshold</strong><small>Ends Loading at this buffer level. A toolhead arrival sensor can end Loading earlier.</small></div>
        <div class="bmcu-handoff-slot"></div>
      </div>
      <div class="bmcu-tuning-card">
        <div class="bmcu-settings-group-head"><strong>Preparation pressure</strong><small>Buffer level maintained during Toolhead preparation.</small></div>
        <div class="bmcu-pressure-slot"></div>
      </div>
    </section>
    <section class="bmcu-settings-group bmcu-calibration-group">
      <div class="bmcu-settings-group-head"><strong>Module calibration</strong><small>Remove filament. Calibration measures the buffer and empty-filament detector. Filament movement is verified automatically during autoload or a real load.</small></div>
      <div class="bmcu-calibration-slot"></div>
    </section>
    <section class="bmcu-settings-remove">
      <div><strong>Remove BMCU</strong><small class="bmcu-remove-state"></small></div>
      <button class="button danger" type="button" data-action="forget-device">Remove and forget</button>
    </section>`;
  const handoff = createLoadingHandoffSetting();
  handoff.classList.add('bmcu-settings-embedded', 'bmcu-handoff-setting');
  node.querySelector('.bmcu-handoff-slot').append(handoff);
  const pressure = createLoadPressureSetting();
  pressure.classList.add('bmcu-settings-embedded', 'bmcu-pressure-setting');
  node.querySelector('.bmcu-pressure-slot').append(pressure);
  const calibration = createCalibrationDevice();
  calibration.classList.add('bmcu-settings-embedded', 'bmcu-calibration-setting');
  node.querySelector('.bmcu-calibration-slot').append(calibration);
  return node;
}

function updateBmcuSettingsDevice(node, device) {
  node.dataset.device = device.name;
  text(node.querySelector('.bmcu-settings-device-head h3'), deviceLabel(device));
  const ready = deviceReady(device);
  const state = node.querySelector('.bmcu-settings-device-state');
  text(state, ready
    ? `Connected - firmware ${device.firmware || 'unknown'}`
    : `Disconnected - ${device.port || 'last configured serial port'}`);
  const badge = node.querySelector('.bmcu-settings-device-badge');
  badge.className = `badge bmcu-settings-device-badge ${ready ? 'good' : 'bad'}`;
  text(badge, ready ? 'Ready' : 'Disconnected');

  updateLoadingHandoffSetting(node.querySelector('.bmcu-handoff-setting'), device);
  updateLoadPressureSetting(node.querySelector('.bmcu-pressure-setting'), device);
  updateCalibrationDevice(node.querySelector('.bmcu-calibration-setting'), device);

  const removeState = node.querySelector('.bmcu-remove-state');
  text(removeState, device.connected
    ? 'Disconnect this module before removing it from the configuration.'
    : 'Removes saved routing, calibration, lighting and module settings for this hardware.');
  const remove = node.querySelector('[data-action="forget-device"]');
  remove.dataset.device = device.name;
  remove.disabled = ui.busy || Boolean(device.connected);
}

function versionTuple(value) {
  const parts = String(value || '').split('.');
  if (parts.length !== 3 || parts.some((part) => !/^\d+$/.test(part))) return null;
  return parts.map(Number);
}

function versionNewer(remote, current) {
  const left = versionTuple(remote);
  const right = versionTuple(current);
  if (!left || !right) return false;
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index];
  }
  return false;
}

function firmwareUpdateCount() {
  const remote = ui.remoteVersions?.firmware;
  if (!remote) return 0;
  return devices().filter((device) => versionNewer(remote, device.firmware)).length;
}

function renderReleaseStatus() {
  const node = $('releaseStatus');
  const label = $('releaseText');
  if (ui.versionChecking) {
    setClass(node, 'hidden', false);
    node.className = 'release-status';
    text(label, 'Checking updates');
  } else if (!ui.remoteVersions) {
    setClass(node, 'hidden', true);
  } else {
    const packageNew = versionNewer(
      ui.remoteVersions.package, store.state.config.package_version);
    const firmwareNew = firmwareUpdateCount() > 0;
    setClass(node, 'hidden', false);
    node.className = `release-status ready ${packageNew || firmwareNew ? 'warn' : 'good'}`;
    text(label, packageNew || firmwareNew ? 'Update available' : 'Up to date');
  }

  const actions = $('packageUpdateActions');
  const statusNode = $('packageUpdateStatus');
  const packageNew = Boolean(ui.remoteVersions?.package) && versionNewer(
    ui.remoteVersions.package, store.state.config.package_version);
  setClass(actions, 'hidden', !packageNew);
  if (packageNew) {
    text(statusNode, `BMCU-Klipper ${store.state.config.package_version} -> ${ui.remoteVersions.package}`);
  }
}

async function checkReleaseVersions() {
  ui.versionChecking = true;
  renderReleaseStatus();
  try {
    const response = await fetch('/api/version', {cache: 'no-store'});
    const value = await response.json();
    ui.remoteVersions = response.ok && value?.ok ? {
      package: String(value.package || ''),
      firmware: String(value.firmware || ''),
    } : null;
  } catch (_) {
    ui.remoteVersions = null;
  } finally {
    ui.versionChecking = false;
    renderReleaseStatus();
    render();
  }
}

function renderSettings() {
  renderSetupStatus();
  renderU1GcodeSettings();
  setClass($('systemLedEmpty'), 'hidden', devices().length > 0);
  syncKeyed($('systemLedSettings'), devices(), (device) => device.name, createSystemLedSetting, updateSystemLedSetting);
  const snapmakerTailHidden = printerInfo().topology === 'snapmaker_u1';
  setClass($('tailTrackingSection'), 'hidden', snapmakerTailHidden);
  const tailEntries = snapmakerTailHidden
    ? []
    : Object.entries(endpoints()).filter(([, endpoint]) => endpoint?.driver !== 'snapmaker_u1');
  syncKeyed($('tailTrackingSettings'), tailEntries, (item) => item[0], createTailTrackingSetting, updateTailTrackingSetting);
  syncKeyed($('bmcuSettings'), devices(), (device) => device.name, createBmcuSettingsDevice, updateBmcuSettingsDevice);
  syncDeviceSelect($('updateDevice'));
  syncUpdatePortOptions();
  syncFlashModeUI();
  renderFirmwareSafetyDialog();
  renderCalibrationSafetyDialog();
  const ready = devices().filter(deviceReady).length;
  const topology = String(printerInfo().topology || 'unknown');
  const topologyLabels = {
    snapmaker_u1: 'Snapmaker U1',
    generic: 'Generic Klipper',
  };
  const packageVersion = store.state.config.package_version || '';
  const remotePackage = ui.remoteVersions?.package || '';
  const packageVersionText = packageVersion
    ? (remotePackage
      ? (versionNewer(remotePackage, packageVersion)
        ? `${packageVersion} - New version available: ${remotePackage}`
        : `${packageVersion} - Up to date`)
      : packageVersion)
    : 'Unavailable';
  const connectedFirmware = devices().map((device) => String(device.firmware || '')).filter(Boolean);
  const firmwareVersion = connectedFirmware.length === 1
    ? connectedFirmware[0]
    : (store.state.config.required_firmware_version || 'unknown');
  const remoteFirmware = ui.remoteVersions?.firmware || '';
  const firmwareVersionText = remoteFirmware
    ? (versionNewer(remoteFirmware, firmwareVersion)
      ? `${firmwareVersion} - New version available: ${remoteFirmware}`
      : `${firmwareVersion} - Up to date`)
    : firmwareVersion;
  const info = [
    ['Detected printer', topologyLabels[topology] || topology],
    ['Filament sensors', Array.isArray(printerInfo().filament_sensors) ? String(printerInfo().filament_sensors.length) : 'unknown'],
    ['Package version', packageVersionText],
    ['BMCU firmware', firmwareVersionText],
    ['Connected BMCU', devices().length ? `${ready} of ${devices().length} ready` : 'Not detected'],
  ];
  syncKeyed($('systemInfo'), info, (item) => item[0], () => {
    const node = document.createElement('div'); node.className = 'info-row'; node.innerHTML = '<span></span><strong></strong>'; return node;
  }, (node, item) => { text(node.querySelector('span'), item[0]); text(node.querySelector('strong'), item[1]); });
  renderReleaseStatus();
  const advice = $('printerIntegrationAdvice');
  const isU1 = topology === 'snapmaker_u1';
  const isGeneric = topology === 'generic';
  const ownershipBlocked = (status().controller_mode || 'standalone') !== 'standalone';
  setClass(advice, 'hidden', !isU1 && !isGeneric && !ownershipBlocked);
  if (ownershipBlocked) {
    advice.className = 'notice error compact';
    text(advice.querySelector('h3'), 'Printer integration blocked');
    text(advice.querySelector('p'), status().controller_block_reason || 'BMCU controller mode is blocked.');
  } else if (isU1) {
    advice.className = 'notice warning compact';
    text(advice.querySelector('h3'), 'Snapmaker U1 detected');
    text(advice.querySelector('p'), 'Verify PTFE routing and configure OrcaSlicer before the first automatic BMCU print.');
  } else if (isGeneric) {
    advice.className = 'notice warning compact';
    text(advice.querySelector('h3'), 'Generic Klipper detected');
    text(advice.querySelector('p'), 'Configure the shared extruder route and OrcaSlicer tool-change G-code before the first automatic BMCU print.');
  }
  const leaveFinal = $('leaveFinalFilamentLoaded');
  if (leaveFinal) {
    const savedPreference = status().preferences?.leave_final_filament_loaded === true;
    const displayedPreference = ui.preferenceDraft == null ? savedPreference : Boolean(ui.preferenceDraft);
    leaveFinal.checked = displayedPreference;
    leaveFinal.disabled = ui.busy || Boolean(status().active_operation);
    const dirtyPreference = ui.preferenceDraft != null;
    text($('setupPreferenceState'), dirtyPreference ? 'Unsaved change' : 'Saved');
    $('setupPreferenceState').className = dirtyPreference ? 'badge warning' : 'muted';
    $('saveSetupPreferences').disabled = ui.busy || !dirtyPreference || Boolean(status().active_operation);
    $('cancelSetupPreferences').disabled = ui.busy || !dirtyPreference;
  }
  renderPrinterHelp();
}

function orcaAssignedTools() {
  const u1 = printerInfo().topology === 'snapmaker_u1';
  const tools = allChannels().map(({channel}) => virtualTool(channel))
    .filter((tool) => Number.isInteger(tool) && tool >= (u1 ? 4 : 1) && tool <= (u1 ? 31 : 255));
  return [...new Set([...(u1 ? [0, 1, 2, 3] : [0]), ...tools])].sort((a, b) => a - b);
}

function orcaToolCount() {
  const assigned = orcaAssignedTools();
  const floor = printerInfo().topology === 'snapmaker_u1' ? 4 : 1;
  return Math.max(floor, assigned.length ? Math.max(...assigned) + 1 : floor);
}

function renderPrinterHelp() {
  const body = $('printerHelpBody');
  if (!body) return;
  const isU1 = printerInfo().topology === 'snapmaker_u1';
  const isGeneric = printerInfo().topology === 'generic';
  setClass($('printerHelpU1'), 'hidden', !isU1);
  setClass($('printerHelpGeneric'), 'hidden', !isGeneric);
  text($('printerHelpTitle'), isGeneric ? 'Generic Klipper setup and OrcaSlicer' : 'Snapmaker U1 setup and slicer profiles');

  const bmcuCount = Math.max(0, devices().length);
  const toolCount = orcaToolCount();
  text($('printerHelpProfileBmcuCount'), String(bmcuCount));
  text($('printerHelpProfileToolCount'), String(toolCount));
  const lastSlot = Math.max(isU1 ? 4 : 1, toolCount);
  text($('printerHelpProfileTools'), `1 (T0)-${lastSlot} (T${lastSlot - 1})`);
  text($('printerHelpProfileNote'), bmcuCount
    ? `Generated for ${bmcuCount} BMCU module${bmcuCount === 1 ? '' : 's'} and slots 1 (T0)-${toolCount} (T${toolCount - 1}). PTFE routing is read from Dashboard when the print starts. The profile contains no printer IP address. Generate it again after changing the number of BMCU modules or any BMCU slot assignment.`
    : 'Connect and configure at least one BMCU before generating the profile.');
  for (const id of ['downloadOrcaProfileJson', 'downloadOrcaProfileBundle', 'downloadSnapmakerOrcaProfile']) {
    const download = $(id);
    if (download) download.disabled = bmcuCount < 1;
  }

  if (isGeneric) {
    const tools = orcaAssignedTools();
    const startLines = ['BMCU_PRINT_BEGIN SCHEMA=1 RESET=1'];
    for (const tool of tools) {
      startLines.push(tool === 0
        ? `{if is_extruder_used[0]}BMCU_PRINT_MAP TOOL=0{endif}`
        : `{if is_extruder_used[${tool}]}BMCU_PRINT_MAP TOOL=${tool} MATERIAL="{filament_type[${tool}]}" COLOR="{filament_colour[${tool}]}"{endif}`);
    }
    startLines.push('BMCU_PRINT_COMMIT');
    text($('genericStartPlan'), startLines.join('\n'));
    text($('genericInitialTool'), 'BMCU_TOOL_CHANGE TOOL={initial_extruder}');
    const genericCount = $('genericExtruderCount');
    if (genericCount) text(genericCount, String(toolCount));
    text($('genericToolChange'), '{\n"BMCU_TOOL_CHANGE TOOL=" + next_extruder + "\\n";\n}');
    text($('genericPrintEnd'), 'BMCU_PRINT_END MODE=AUTO CLEAR=1');

    const map = $('genericToolMap');
    if (map) {
      const rows = [];
      const external = document.createElement('div');
      external.className = 'info-row';
      const externalLeft = document.createElement('span');
      const externalRight = document.createElement('strong');
      externalLeft.textContent = 'T0 / Orca slot 1';
      externalRight.textContent = 'External - manual filament';
      external.append(externalLeft, externalRight);
      rows.push(external);
      const channels = allChannels()
        .filter(({channel}) => Number.isInteger(virtualTool(channel)) && virtualTool(channel) >= 1)
        .sort((a, b) => virtualTool(a.channel) - virtualTool(b.channel));
      for (const {device, channel} of channels) {
        const row = document.createElement('div');
        row.className = 'info-row';
        const tool = virtualTool(channel);
        const route = String(channel?.endpoint || 'Not routed');
        const label = String(channel?.name || channel?.label || channel?.material || '').trim();
        const left = document.createElement('span');
        const right = document.createElement('strong');
        left.textContent = `T${tool} / Orca slot ${tool + 1}`;
        right.textContent = `${deviceLabel(device)} Channel ${physicalChannel(channel)} -> ${route}${label ? ` - ${label}` : ''}`;
        row.append(left, right);
        rows.push(row);
      }
      map.replaceChildren(...rows);
    }
  }
}

function orcaProfileSources() {
  return allChannels()
    .filter(({channel}) => Number.isInteger(virtualTool(channel)))
    .sort((left, right) => virtualTool(left.channel) - virtualTool(right.channel))
    .map(({channel}) => {
      const value = String(channel?.color || channel?.colour || '#FCE94F').trim();
      return {
        tool: virtualTool(channel),
        color: /^#[0-9A-Fa-f]{6}$/.test(value) ? value.toUpperCase() : '#FCE94F',
      };
    });
}

function downloadOrcaProfile(format = 'json') {
  const bmcuCount = Math.max(0, devices().length);
  if (!bmcuCount) {
    toast('Connect and configure at least one BMCU first', 'error');
    return;
  }
  const normalizedFormat = format === 'bundle' ? 'bundle' : 'json';
  const sources = orcaProfileSources().slice(0, bmcuCount * 4);
  const query = new URLSearchParams({
    bmcu_count: String(bmcuCount),
    tools: sources.map((source) => source.tool).join(','),
    colors: sources.map((source) => source.color).join(','),
    format: normalizedFormat,
  });
  const link = document.createElement('a');
  link.href = `/api/orca/profile?${query.toString()}`;
  link.download = normalizedFormat === 'bundle'
    ? 'Snapmaker U1 BMCU.orca_printer'
    : 'Snapmaker U1 BMCU.json';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function downloadSnapmakerOrcaProfile() {
  const bmcuCount = Math.max(0, devices().length);
  if (!bmcuCount) {
    toast('Connect and configure at least one BMCU first', 'error');
    return;
  }
  const sources = orcaProfileSources().slice(0, bmcuCount * 4);
  const query = new URLSearchParams({
    bmcu_count: String(bmcuCount),
    tools: sources.map((source) => source.tool).join(','),
    colors: sources.map((source) => source.color).join(','),
  });
  const link = document.createElement('a');
  link.href = `/api/snapmaker-orca/profile?${query.toString()}`;
  link.download = 'Snapmaker U1 BMCU.orca_printer';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function selectCopyFallback() {
  const field = $('copyFallbackText');
  if (!field) return;
  field.focus();
  field.select();
  field.setSelectionRange(0, field.value.length);
}

async function copyTextPortable(value) {
  const textValue = String(value || '');
  if (!textValue) throw new Error('Nothing to copy');
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(textValue);
    return true;
  }
  const field = $('copyFallbackText');
  if (!field) throw new Error('Clipboard access is unavailable');
  field.value = textValue;
  openDialog($('copyFallbackDialog'));
  requestAnimationFrame(selectCopyFallback);
  return false;
}

function render() {
  renderHeader();
  renderNotices();
  renderSummary();
  renderDashboardChannels();
  renderToolMap();
  renderOwnership();
  renderDashboardRouting();
  renderDiagnostics();
  renderSettings();
}

async function run(script, options = {}) {
  if (!store.state.status || ['offline', 'starting'].includes(store.state.connection)) {
    const message = 'Printer connection is not ready.';
    if (!options.silent) toast(message, 'error');
    throw new Error(message);
  }
  if (ui.busy && !options.allowBusy) {
    const message = 'Another panel command is already in progress.';
    if (!options.silent) toast(message, 'error');
    throw new Error(message);
  }
  const ownsBusy = !options.allowBusy;
  if (ownsBusy) ui.busy = true;
  try {
    const response = await fetch('/moonraker/printer/gcode/script', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({script}),
    });
    const raw = await response.text();
    let payload;
    try { payload = JSON.parse(raw); } catch (_) { payload = {raw}; }
    if (!response.ok) throw new Error(payload?.error?.message || payload?.error || payload?.raw || `HTTP ${response.status}`);
    if (options.success) toast(options.success, 'success');
    if (!options.noRefresh) transport.refreshSoon(100);
    return payload;
  } catch (error) {
    if (!options.silent) toast(error.message || String(error), 'error');
    throw error;
  } finally {
    if (ownsBusy) ui.busy = false;
  }
}

async function runMany(scripts) {
  for (const script of scripts) await run(script, {allowBusy: true});
  transport.refreshSoon(100);
}

function resetConfirmVisual() {
  const visual = $('confirmVisual');
  visual.replaceChildren();
  visual.classList.add('hidden');
  $('confirmText').classList.remove('hidden');
}

function slotArrow(kind = 'change') {
  const node = document.createElement('span');
  node.className = `replace-arrow slot-flow-arrow${kind === 'swap' ? ' swap' : ''}`;
  node.setAttribute('aria-hidden', 'true');
  if (kind === 'swap') {
    const forward = document.createElement('span');
    const backward = document.createElement('span');
    forward.textContent = '→';
    backward.textContent = '←';
    node.append(forward, backward);
  } else {
    node.textContent = '→';
  }
  return node;
}

function slotFlowValue(value) {
  const node = document.createElement('div');
  node.className = 'slot-flow-value';
  node.textContent = value;
  return node;
}

function confirmSlotChange(title, from, to, note, callback, options = {}) {
  confirmAction(title, '', callback, options);
  const visual = $('confirmVisual');
  const flow = document.createElement('div');
  flow.className = 'slot-change-flow';
  flow.append(slotFlowValue(from), slotArrow('change'), slotFlowValue(to));
  const copy = document.createElement('p');
  copy.className = 'slot-flow-note';
  copy.textContent = note;
  visual.append(flow, copy);
  visual.classList.remove('hidden');
  $('confirmText').classList.add('hidden');
}

function confirmSlotSwap(title, first, second, note, callback, options = {}) {
  confirmAction(title, '', callback, options);
  const visual = $('confirmVisual');
  const flow = document.createElement('div');
  flow.className = 'slot-swap-flow';
  flow.append(slotFlowValue(first), slotArrow('swap'), slotFlowValue(second));
  const copy = document.createElement('p');
  copy.className = 'slot-flow-note';
  copy.textContent = note;
  visual.append(flow, copy);
  visual.classList.remove('hidden');
  $('confirmText').classList.add('hidden');
}

function confirmAction(title, copy, callback, options = {}) {
  resetConfirmVisual();
  text($('confirmTitle'), title);
  text($('confirmText'), copy);
  text($('confirmCancel'), options.cancel || 'Cancel');
  text($('confirmAction'), options.confirm || 'Continue');
  ui.confirmCallback = callback;
  ui.confirmCancelCallback = typeof options.onCancel === 'function' ? options.onCancel : null;
  openDialog($('confirmDialog'));
}

function openChannelEditor(deviceName, channelIndex) {
  const device = devices().find((candidate) => candidate.name === deviceName);
  const channel = device?.channels?.find((candidate) => Number(candidate.channel) === Number(channelIndex));
  if (!channel) return;
  ui.editing = {
    device: deviceName,
    channel: Number(channelIndex),
    autoloadRuntimeSupported: channel.autoload_runtime_supported !== false,
  };
  text($('channelDialogTitle'), `${deviceLabel(device)} - ${sourceTitle(channel)}`);
  $('editName').value = /^channel\s+\d+$/i.test(channel.name || '') ? '' : channel.name || '';
  $('editMaterial').value = normalizeMaterialEditorValue(channel.material || 'PLA');
  $('editColor').value = /^#[0-9a-f]{6}$/i.test(channel.color || '') ? channel.color : '#ffffff';
  $('editRefill').checked = channel.refill_enabled !== false;
  $('editRefillGroup').value = channel.refill_group || '';
  $('editRefillPriority').value = Number(channel.refill_priority ?? channelIndex);
  const snapmaker = printerInfo().topology === 'snapmaker_u1';
  setClass($('editLogicalToolRow'), 'hidden', !snapmaker);
  if (snapmaker) {
    syncSelectOptions($('editLogicalTool'), Array.from({length: 28}, (_, index) => {
      const tool = index + 4;
      const owner = allChannels().find(({device: candidateDevice, channel: candidateChannel}) =>
        Number(candidateChannel.virtual_tool) === tool &&
        !(candidateDevice.name === deviceName && Number(candidateChannel.channel) === Number(channelIndex)));
      const suffix = owner ? ` - ${deviceLabel(owner.device)} / ${sourceTitle(owner.channel)}` : '';
      return [String(tool), `${orcaSlot(tool)}${suffix}`];
    }));
    $('editLogicalTool').value = String(virtualTool(channel) ?? 4);
    const currentTool = virtualTool(channel);
    text($('editLogicalToolHelp'), currentTool === null
      ? 'Physical heads use 1 (T0)-4 (T3). BMCU uses 5 (T4)-32 (T31). Changing this requires updating the OrcaSlicer printer profile or Machine G-code.'
      : `Current slot: ${orcaSlotLabel(currentTool)}. Changing it requires updating the OrcaSlicer printer profile or Machine G-code.`);
  }
  $('editUnloadRetract').value = cmFromMm(channel.unload_retract_mm || 200).toFixed(2);
  const autoloadSupported = channel.autoload_runtime_supported !== false;
  $('editAutoload').value = cmFromMm(channel.autoload_mm || 120).toFixed(2);
  $('editAutoload').disabled = false;
  text($('editAutoloadHelp'), autoloadSupported
    ? 'Initial feed after filament is detected. Default: 12 cm.'
    : 'Saved per channel. This connected firmware currently applies 12 cm.');
  syncLengthConversions();
  const detector = $('editDetectorNotice');
  detector.className = `notice compact ${channel.present ? 'good' : 'warning'}`;
  detector.replaceChildren();
  const detectorIcon = document.createElement('span');
  detectorIcon.className = 'notice-icon';
  text(detectorIcon, channel.present ? '✓' : '!');
  const detectorBody = document.createElement('div');
  const detectorTitle = document.createElement('h3');
  text(detectorTitle, channel.present
    ? `Filament detected in ${sourceTitle(channel)}`
    : `No filament detected in ${sourceTitle(channel)}`);
  const detectorCopy = document.createElement('p');
  text(detectorCopy, 'The selected colour belongs only to this physical channel. Its LED can show that colour only when this channel is the detected/active source.');
  detectorBody.append(detectorTitle, detectorCopy);
  detector.append(detectorIcon, detectorBody);
  openDialog($('channelDialog'));
}

async function saveChannelEditor() {
  if (!ui.editing) return;
  const retract = mmFromCm($('editUnloadRetract').value);
  const autoload = mmFromCm($('editAutoload').value);
  if (!finite(retract) || retract < 10 || retract > 2000) {
    toast('Retraction must be between 10 and 2000 mm.', 'error');
    return;
  }
  if (!finite(autoload) || autoload < 10 || autoload > 1000) {
    toast('Autoload must be between 10 and 1000 mm.', 'error');
    return;
  }
  const snapmaker = printerInfo().topology === 'snapmaker_u1';
  const material = normalizeMaterialEditorValue($('editMaterial').value);
  if (!materialEditorValid(material)) {
    toast('Enter a material such as PLA, PETG, TPU or PLA-CF.', 'error');
    return;
  }
  $('editMaterial').value = material;
  const {device, channel} = ui.editing;
  const name = $('editName').value.trim();
  const group = $('editRefillGroup').value.trim();
  const originalDevice = devices().find((candidate) => candidate.name === device);
  const originalChannel = originalDevice?.channels?.find((candidate) => Number(candidate.channel) === Number(channel));
  const filamentScript = [
    'BMCU_SET_FILAMENT',
    `DEVICE=${gcodeValue(device)}`,
    `CHANNEL=${channel}`,
    `MATERIAL=${gcodeValue(material)}`,
    `COLOR=${gcodeValue($('editColor').value)}`,
    `REFILL=${$('editRefill').checked ? 1 : 0}`,
    `REFILL_PRIORITY=${Number($('editRefillPriority').value || channel)}`,
    `UNLOAD_RETRACT_MM=${retract.toFixed(2)}`,
    `AUTOLOAD_MM=${autoload.toFixed(2)}`,
    name ? `NAME=${gcodeValue(name)}` : 'CLEAR_NAME=1',
    group ? `REFILL_GROUP=${gcodeValue(group)}` : 'CLEAR_REFILL_GROUP=1',
  ].filter(Boolean).join(' ');

  const currentTool = virtualTool(originalChannel);
  const selectedTool = snapmaker ? Number($('editLogicalTool').value) : currentTool;
  if (snapmaker && (!Number.isInteger(selectedTool) || selectedTool < 4 || selectedTool > 31)) {
    toast('BMCU slot must be between 5 and 32.', 'error');
    return;
  }
  const owner = snapmaker && selectedTool !== currentTool
    ? allChannels().find(({device: candidateDevice, channel: candidateChannel}) =>
      Number(candidateChannel.virtual_tool) === selectedTool &&
      !(candidateDevice.name === device && Number(candidateChannel.channel) === Number(channel)))
    : null;

  const save = async (swap = false) => {
    const script = [
      filamentScript,
      ...(snapmaker && selectedTool !== currentTool
        ? [`TOOL=${selectedTool}`, `SWAP=${swap ? 1 : 0}`, 'CONFIRM_ORCA=1']
        : []),
    ].join(' ');
    const toolNote = snapmaker && selectedTool !== currentTool
      ? (swap ? ' and slots swapped' : ` and changed to slot ${orcaSlotLabel(selectedTool)}`)
      : '';
    await run(script, {
      success: `Channel ${channel + 1} saved${toolNote} - ${$('editColor').value.toUpperCase()}`,
      noRefresh: true,
    });
    await transport.refreshNow();
    closeDialog($('channelDialog'));
  };

  if (snapmaker && selectedTool !== currentTool) {
    const warning = 'After this change, regenerate and import the current Snapmaker U1 BMCU preset before printing.';
    if (owner) {
      const left = `${deviceLabel(originalDevice)} - ${sourceTitle(originalChannel)}: slot ${orcaSlot(currentTool)}`;
      const right = `${deviceLabel(owner.device)} - ${sourceTitle(owner.channel)}: slot ${orcaSlot(selectedTool)}`;
      confirmSlotSwap(
        'Swap slots?', left, right, warning,
        () => save(true),
        {cancel: 'Cancel change', confirm: 'Confirm swap', onCancel: () => { $('editLogicalTool').value = String(currentTool); }});
      return;
    }
    confirmSlotChange(
      'Change slot?', orcaSlotLabel(currentTool), orcaSlotLabel(selectedTool), warning,
      () => save(false),
      {cancel: 'Cancel change', confirm: 'Confirm change', onCancel: () => { $('editLogicalTool').value = String(currentTool); }});
    return;
  }
  await save(false);
}

function discardRoutes() {
  ui.routeDraft.clear();
  renderDashboardRouting();
}

async function applyRouteChanges(changes) {
  if (!changes.length) return;
  if (ui.busy) {
    toast('Another panel command is already in progress.', 'error');
    return;
  }
  const blocked = (status().controller_mode || 'standalone') !== 'standalone';
  if (blocked && !changes.every((change) => !change.to)) {
    toast((printerInfo().blockers || []).join(' ') || 'Only disconnecting an existing route is allowed while integration is blocked.', 'error');
    return;
  }
  const scripts = [];
  let pending = changes.slice();
  const currentEndpoints = endpoints();
  const firstMissing = pending.find((change) =>
    change.to && !Object.prototype.hasOwnProperty.call(currentEndpoints, change.to));

  if (firstMissing) {
    const info = printerInfo();
    const preset = info.topology === 'snapmaker_u1'
      ? 'snapmaker_u1'
      : (info.recommended_preset || 'generic_single_extruder');
    const count = info.topology === 'snapmaker_u1'
      ? 4
      : Math.max(1, Number(info.extruders?.length || 0), nativeHeadCount());

    scripts.push([
      'BMCU_APPLY_PRESET',
      `PRESET=${gcodeValue(preset)}`,
      `COUNT=${count}`,
      'MISSING_ONLY=1',
      `DEVICE=${gcodeValue(firstMissing.device.name)}`,
      `CHANNEL=${Number(firstMissing.channel.channel)}`,
      `ENDPOINT=${gcodeValue(firstMissing.to)}`,
    ].join(' '));
    pending = pending.filter((change) => change !== firstMissing);
  }

  for (const change of pending) {
    scripts.push(`BMCU_SET_ROUTE DEVICE=${gcodeValue(change.device.name)} CHANNEL=${Number(change.channel.channel)} ENDPOINT=${gcodeValue(change.to || 'NONE')}`);
  }
  const restoringTail = changes.some((change) =>
    change.channel.tail_unrouted &&
    String(change.to || '') === String(change.channel.tail_endpoint || ''));
  if (restoringTail) scripts.push('BMCU_RECONCILE');
  try {
    ui.busy = true;
    renderDashboardRouting();
    await runMany(scripts);
    ui.routeDraft.clear();
    toast('Routing saved', 'success');
  } finally {
    ui.busy = false;
    transport.refreshSoon(100);
    renderDashboardRouting();
  }
}

function saveRoutes() {
  if (ui.busy) {
    toast('Another panel command is already in progress.', 'error');
    return;
  }
  const changes = routeChanges();
  if (!changes.length) return;
  const lines = changes.map(({device, channel, from, to}) => {
    const tool = virtualTool(channel);
    const source = `${deviceLabel(device)} ${sourceTitle(channel)}${tool === null ? '' : ` (${orcaSlotLabel(tool)})`}`;
    return `${source}: ${endpointLabel(from)} -> ${endpointLabel(to)}`;
  });
  confirmAction('Save routing changes', `Apply these changes?\n\n${lines.join('\n')}`, () => applyRouteChanges(changes));
}

function calibrationSafetySnapshot(deviceName, selection) {
  const device = devices().find((item) => item.name === deviceName);
  const ready = deviceReady(device);
  const indexes = String(selection).toUpperCase() === 'ALL'
    ? [0, 1, 2, 3]
    : [Number(selection)];
  const channels = indexes.filter((index) => Number.isInteger(index) && index >= 0 && index < 4)
    .map((index) => {
      const channel = device?.channels?.find((item) => Number(item.channel) === index);
      const connected = Boolean(channel?.connected);
      const sensorKnown = Boolean(channel) && Object.prototype.hasOwnProperty.call(channel, 'present');
      const present = sensorKnown ? Boolean(channel.present) : null;
      const route = channel ? routeState(channel) : 'UNKNOWN';
      const skipped = !connected;
      const safe = skipped || (ready && sensorKnown && present === false && route === 'EMPTY');
      return {index, connected, present, route, skipped, safe};
    });
  const connected = channels.filter((channel) => channel.connected);
  return {
    device, ready, channels,
    hasConnected: connected.length > 0,
    allSafe: ready && connected.length > 0 && channels.every((channel) => channel.safe),
  };
}

function renderCalibrationSafetyDialog() {
  const request = ui.pendingCalibration;
  if (!request) return;
  const snapshot = calibrationSafetySnapshot(request.deviceName, request.selection);
  text($('calibrationSafetyTitle'), String(request.selection).toUpperCase() === 'ALL'
    ? 'Prepare all connected channels for calibration'
    : `Prepare Channel ${Number(request.selection) + 1} for calibration`);
  syncKeyed($('calibrationSafetyChannels'), snapshot.channels, (channel) => channel.index, () => {
    const row = document.createElement('tr');
    row.innerHTML = '<th scope="row"></th><td class="flash-sensor"></td><td class="flash-route"></td>';
    return row;
  }, (row, channel) => {
    text(row.querySelector('th'), `Channel ${channel.index + 1}`);
    const sensor = row.querySelector('.flash-sensor');
    const route = row.querySelector('.flash-route');
    sensor.className = 'flash-sensor';
    route.className = 'flash-route';
    if (channel.skipped) {
      sensor.classList.add('warn');
      route.classList.add('warn');
      text(sensor, 'Disconnected - skipped');
      text(route, 'Skipped');
    } else if (!snapshot.ready || channel.present === null) {
      sensor.classList.add('warn');
      text(sensor, 'Status unavailable');
      route.classList.add('warn');
      text(route, channel.route === 'UNKNOWN' ? 'Unknown' : channel.route);
    } else {
      sensor.classList.add(channel.present ? 'bad' : 'good');
      text(sensor, channel.present ? 'Filament detected' : 'Removed / not detected');
      route.classList.add(channel.route === 'EMPTY' ? 'good' : 'bad');
      text(route, channel.route);
    }
  });

  const statusNode = $('calibrationSafetyStatus');
  statusNode.className = `flash-safety-status ${snapshot.allSafe ? 'good' : 'warn'}`;
  if (!snapshot.hasConnected) {
    text(statusNode, 'No selected channel is connected. There is nothing to calibrate.');
  } else if (!snapshot.ready) {
    text(statusNode, 'BMCU is not ready. Live calibration safety verification is unavailable.');
  } else if (snapshot.allSafe) {
    text(statusNode, 'Selected connected channels are empty. Calibration may start.');
  } else {
    const remaining = snapshot.channels.filter((channel) => channel.connected && !channel.safe);
    text(statusNode, `${remaining.length} Channel${remaining.length === 1 ? '' : 's'} still require complete filament removal or an EMPTY route.`);
  }
  $('calibrationSafetyContinue').disabled = !snapshot.allSafe || ui.calibrationStarting;
}

function startCalibration(device, channel) {
  ui.pendingCalibration = {deviceName: device, selection: String(channel)};
  renderCalibrationSafetyDialog();
  openDialog($('calibrationSafetyDialog'));
}

async function continueCalibration() {
  const request = ui.pendingCalibration;
  if (!request || ui.calibrationStarting) return;
  const snapshot = calibrationSafetySnapshot(request.deviceName, request.selection);
  renderCalibrationSafetyDialog();
  if (!snapshot.allSafe) return;
  ui.calibrationStarting = true;
  $('calibrationSafetyContinue').disabled = true;
  try {
    await run(`BMCU_CALIBRATE DEVICE=${gcodeValue(request.deviceName)} CHANNEL=${request.selection}`,
      {success: 'Calibration started'});
    ui.pendingCalibration = null;
    closeDialog($('calibrationSafetyDialog'));
  } finally {
    ui.calibrationStarting = false;
    renderCalibrationSafetyDialog();
  }
}

function selectedUpdateDevice() {
  const managed = managedUpdateDevices();
  const value = $('updateDevice').value || managed[0]?.name || '';
  if (value.startsWith('raw:')) {
    const path = value.slice(4);
    const candidate = rawSerialCandidates().find((item) => item.path === path) || {path};
    return {name: 'raw_ch340', port: candidate.path || path, raw: true, candidate};
  }
  const device = managed.find((item) => item.name === value) || managed[0] || {};
  return {name: device.name || value || 'bmcu0',
    port: device.transport_port || device.port || '', raw: false, device};
}

function selectedFlashMode() {
  return document.querySelector('input[name="updateMode"]:checked')?.value === 'ttl' ? 'ttl' : 'usb';
}

function configuredSerialPorts() {
  const values = [];
  const seen = new Set();
  for (const candidate of serialCandidates()) {
    if (seen.has(candidate.path)) continue;
    seen.add(candidate.path);
    const identity = [candidate.usb_vid && candidate.usb_pid ? `${candidate.usb_vid}:${candidate.usb_pid}` : '', candidate.driver].filter(Boolean).join(' ');
    const type = candidate.usb_ttl ? 'USB-TTL' : (candidate.driver || 'serial');
    values.push([candidate.path, `${type}${identity ? ` [${identity}]` : ''} - ${candidate.path}`]);
  }
  return values;
}

function syncUpdatePortOptions() {
  const select = $('updatePort');
  if (!select) return;
  if (ui.updateInProgress) {
    select.disabled = true;
    $('updateDevice').disabled = true;
    $('updatePortsRefresh').disabled = true;
    $('updateFileLocal').disabled = true;
    $('updateUpload').disabled = true;
    $('updateOnline').disabled = true;
    for (const radio of qsa('input[name="updateMode"]')) radio.disabled = true;
    setClass($('unknownCh340Notice'), 'hidden', true);
    return;
  }
  $('updateDevice').disabled = false;
  $('updatePortsRefresh').disabled = false;
  $('updateFileLocal').disabled = false;
  $('updateUpload').disabled = ui.firmwareUploadRunning;
  $('updateOnline').disabled = ui.firmwareUploadRunning;
  for (const radio of qsa('input[name="updateMode"]')) radio.disabled = false;
  const options = configuredSerialPorts();
  syncSelectOptions(select, options.length ? options : [['', 'No serial port detected']]);
  const mode = selectedFlashMode();
  const device = selectedUpdateDevice();
  if (device.raw && device.port && select.value !== device.port) select.value = device.port;
  else if (device.port) {
    const candidate = serialCandidates().find((item) => candidateMatchesPort(item, device.port));
    if (candidate && select.value !== candidate.path) select.value = candidate.path;
  }
  select.disabled = Boolean(device.raw || (mode === 'usb' && device.port));
  setClass($('unknownCh340Notice'), 'hidden', !device.raw);
}

function syncFlashModeUI() {
  for (const card of qsa('.flash-mode-card')) {
    setClass(card, 'selected', Boolean(card.querySelector('input')?.checked));
  }
  syncDeviceSelect($('updateDevice'));
  syncUpdatePortOptions();
}

async function refreshSerialPorts(notify = false) {
  if (ui.updateInProgress) return;
  try {
    const response = await fetch('/api/update/ports', {cache: 'no-store'});
    const value = await response.json();
    if (!response.ok) throw new Error(value?.error?.message || `HTTP ${response.status}`);
    store.state.config.serial_ports = Array.isArray(value.ports) ? value.ports : [];
    syncDeviceSelect($('updateDevice'));
    syncUpdatePortOptions();
    if (notify) toast('Serial ports refreshed', 'success');
  } catch (error) {
    updateMessage(error.message || String(error), true);
  }
}

function updateMessage(message, error = false) {
  text($('updateStatus'), message);
  $('updateStatus').className = `update-status ${error ? 'bad' : ''}`;
}

function firmwareFlashRequest(online = false) {
  const file = online ? null : $('updateFileLocal').files?.[0];
  if (!online && !file) { updateMessage('Select a .bin firmware file.', true); return null; }
  if (file && (file.size < 1 || (file.size > 61440 && file.size !== 65536))) {
    updateMessage('Application firmware must contain 1-61440 bytes, or use a complete 65536-byte flash image.', true);
    return null;
  }
  const device = selectedUpdateDevice();
  const mode = selectedFlashMode();
  const port = $('updatePort').value || (mode === 'usb' ? device.port : '');
  if (!port) { updateMessage('Select a serial port.', true); return null; }
  return {file, online, deviceName: device.name, mode, port, raw: Boolean(device.raw),
    eraseNvm: false, replaceNvm: false, confirmTtlTarget: false};
}

function firmwareSafetySnapshot(deviceName) {
  const device = devices().find((item) => item.name === deviceName);
  const ready = deviceReady(device);
  const channels = [];
  for (let index = 0; index < 4; index += 1) {
    const channel = device?.channels?.find((item) => Number(item.channel) === index);
    const sensorKnown = Boolean(channel) && Object.prototype.hasOwnProperty.call(channel, 'present');
    const present = sensorKnown ? Boolean(channel.present) : null;
    const route = channel ? routeState(channel) : 'UNKNOWN';
    const safe = ready && sensorKnown && present === false && route === 'EMPTY';
    channels.push({index, present, route, safe});
  }
  return {device, ready, channels, allSafe: channels.every((channel) => channel.safe)};
}

function firmwareDestructiveRequirements(request) {
  const raw = Boolean(request?.raw);
  const fullImage = Number(request?.file?.size) === 65536;
  return {
    unknownTarget: raw,
    forceErase: raw && !fullImage,
    forceReplace: raw && fullImage,
    optionalErase: !raw,
    ttl: request?.mode === 'ttl',
  };
}

function firmwareConfirmationsReady(request) {
  const required = firmwareDestructiveRequirements(request);
  return $('flashTargetRisk').checked
    && (!required.unknownTarget || $('flashUnknownTarget').checked)
    && (!required.ttl || $('flashTtlTarget').checked);
}

function renderFirmwareSafetyDialog() {
  const request = ui.pendingFirmwareUpload;
  if (!request) return;
  const raw = Boolean(request.raw);
  const required = firmwareDestructiveRequirements(request);
  setClass($('flashConfirmations'), 'hidden', false);
  setClass($('flashUnknownTargetRow'), 'hidden', !required.unknownTarget);
  setClass($('flashEraseNvmRow'), 'hidden', !(required.forceErase || required.optionalErase));
  setClass($('flashReplaceNvmRow'), 'hidden', !required.forceReplace);
  setClass($('flashTtlTargetRow'), 'hidden', !required.ttl);
  $('flashEraseNvm').disabled = required.forceErase;
  $('flashReplaceNvm').disabled = required.forceReplace;
  if (required.forceErase) $('flashEraseNvm').checked = true;
  if (required.forceReplace) $('flashReplaceNvm').checked = true;
  text($('flashEraseNvmTitle'), required.forceErase ? 'Calibration data will be cleared' : 'Clear calibration data');
  text($('flashEraseNvmHelp'), required.forceErase
    ? 'The current firmware is unknown, so its calibration format cannot be trusted or preserved.'
    : 'Optional. Leave unchecked to preserve the verified 4096-byte BMCU calibration area.');
  text($('flashReplaceNvmTitle'), 'Calibration data will be replaced');
  text($('flashReplaceNvmHelp'), 'A complete 65536-byte image contains its own final 4096-byte NVM area.');
  setClass($('flashSafetyTableWrap'), 'hidden', raw);
  text($('flashSafetyTitle'), raw ? 'Danger - confirm the exact serial device' : 'Remove all filament before flashing');
  text($('flashSafetyCopy'), raw
    ? 'The current firmware cannot be queried. A raw flash targets the selected serial device directly. Unplug the intended adapter, refresh and confirm the selected port disappears. Reconnect it, refresh and confirm the same port returns. Continue only if you know exactly what device you selected and all filament is completely removed.'
    : 'Completely remove every filament from the BMCU, including filament still inside its input path. Verify the selected physical target before continuing. The status below updates automatically.');
  const statusNode = $('flashSafetyStatus');
  if (raw) {
    $('flashSafetyChannels').replaceChildren();
    statusNode.className = 'flash-safety-status warn';
    text(statusNode, 'Unknown firmware - live channel verification is unavailable. The wrong serial port may reprogram another device. Continue only after physically identifying the target and removing all filament.');
    $('flashSafetyContinue').disabled = ui.firmwareUploadRunning || !firmwareConfirmationsReady(request);
    return;
  }

  const snapshot = firmwareSafetySnapshot(request.deviceName);
  syncKeyed($('flashSafetyChannels'), snapshot.channels, (channel) => channel.index, () => {
    const row = document.createElement('tr');
    row.innerHTML = '<th scope="row"></th><td class="flash-sensor"></td><td class="flash-route"></td>';
    return row;
  }, (row, channel) => {
    text(row.querySelector('th'), `Channel ${channel.index + 1}`);
    const sensor = row.querySelector('.flash-sensor');
    const route = row.querySelector('.flash-route');
    sensor.className = 'flash-sensor';
    route.className = 'flash-route';
    if (!snapshot.ready || channel.present === null) {
      sensor.classList.add('warn');
      text(sensor, 'Status unavailable');
    } else if (channel.present) {
      sensor.classList.add('bad');
      text(sensor, 'Filament detected');
    } else {
      sensor.classList.add('good');
      text(sensor, 'Removed / not detected');
    }
    if (channel.route === 'EMPTY') {
      route.classList.add('good');
      text(route, 'EMPTY');
    } else if (channel.route === 'UNKNOWN') {
      route.classList.add('warn');
      text(route, 'Unknown');
    } else {
      route.classList.add('bad');
      text(route, channel.route);
    }
  });

  const remaining = snapshot.channels.filter((channel) => !channel.safe);
  statusNode.className = `flash-safety-status ${snapshot.allSafe ? 'good' : 'warn'}`;
  if (snapshot.allSafe) text(statusNode, 'All four channels are completely empty. You may continue.');
  else if (!snapshot.ready) text(statusNode, 'BMCU is not ready. Live empty-channel verification is unavailable.');
  else text(statusNode, `${remaining.length} channel${remaining.length === 1 ? '' : 's'} still require removal or route confirmation.`);
  $('flashSafetyContinue').disabled = !snapshot.allSafe || ui.firmwareUploadRunning || !firmwareConfirmationsReady(request);
}

async function performFirmwareUpload(request) {
  if (!request || ui.firmwareUploadRunning || ui.updateInProgress) return;
  ui.firmwareUploadRunning = true;
  ui.updateInProgress = true;
  ui.updatePort = String(request.port || '');
  $('updateUpload').disabled = true;
  $('updateOnline').disabled = true;
  setClass($('unknownCh340Notice'), 'hidden', true);
  syncUpdatePortOptions();
  const method = request.mode === 'usb' ? 'USB automatic bootloader' : 'TTL manual bootloader';
  let accepted = false;
  try {
    let response;
    if (request.online) {
      updateMessage(`Downloading latest firmware - ${method}`);
      response = await fetch('/api/update/online', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          port: request.port, mode: request.mode, device: request.deviceName,
          raw: request.raw, flash_target: request.confirmFlashTarget,
          erase_nvm: request.eraseNvm, replace_nvm: request.replaceNvm,
          ttl_target: request.confirmTtlTarget,
        }),
      });
    } else {
      updateMessage(`Uploading ${request.file.name} - ${method}`);
      response = await fetch('/api/update/upload', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-BMCU-Filename': request.file.name,
          'X-BMCU-Port': request.port,
          'X-BMCU-Mode': request.mode,
          'X-BMCU-Variant': 'universal',
          'X-BMCU-Device': request.deviceName,
          'X-BMCU-Raw': request.raw ? '1' : '0',
          'X-BMCU-Flash-Target': request.confirmFlashTarget ? '1' : '0',
          'X-BMCU-Erase-NVM': request.eraseNvm ? '1' : '0',
          'X-BMCU-Replace-NVM': request.replaceNvm ? '1' : '0',
          'X-BMCU-TTL-Target': request.confirmTtlTarget ? '1' : '0',
        },
        body: request.file,
      });
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.error?.message || payload?.error || `HTTP ${response.status}`);
    accepted = true;
    updateMessage(`Firmware flash started - ${method}`);
    pollUpdate(true);
  } catch (error) {
    updateMessage(error.message || String(error), true);
  } finally {
    ui.firmwareUploadRunning = false;
    if (!accepted) {
      ui.updateInProgress = false;
      ui.updatePort = '';
    }
    syncUpdatePortOptions();
    renderFirmwareSafetyDialog();
  }
}

async function continueFirmwareUpload() {
  const request = ui.pendingFirmwareUpload;
  if (!request) return;
  const required = firmwareDestructiveRequirements(request);
  request.eraseNvm = required.forceErase || $('flashEraseNvm').checked;
  request.replaceNvm = required.forceReplace || $('flashReplaceNvm').checked;
  request.confirmFlashTarget = $('flashTargetRisk').checked;
  request.confirmTtlTarget = $('flashTtlTarget').checked;
  if (!firmwareConfirmationsReady(request)) return;
  if (!request.raw) {
    const snapshot = firmwareSafetySnapshot(request.deviceName);
    renderFirmwareSafetyDialog();
    if (!snapshot.allSafe) return;
  }
  ui.pendingFirmwareUpload = null;
  closeDialog($('flashSafetyDialog'));
  await performFirmwareUpload(request);
}

async function beginFirmwareFlash(online = false) {
  if (ui.updateInProgress || ui.firmwareUploadRunning) return;
  try {
    const statusResponse = await fetch('/api/update/status', {cache: 'no-store'});
    const status = await statusResponse.json();
    if (!statusResponse.ok) throw new Error(status?.error?.message || `HTTP ${statusResponse.status}`);
    if (status.recovery_required) {
      ui.updateInProgress = true;
      const response = await fetch('/api/update/recover', {method: 'POST'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || `HTTP ${response.status}`);
      updateMessage('Retrying the interrupted flash using the preserved image.');
      pollUpdate(true);
      return;
    }
  } catch (error) {
    ui.updateInProgress = false;
    updateMessage(error.message || String(error), true);
    return;
  }
  const request = firmwareFlashRequest(online);
  if (!request) return;
  ui.flashRestartRequested = false;
  $('flashTargetRisk').checked = false;
  $('flashUnknownTarget').checked = false;
  $('flashEraseNvm').checked = false;
  $('flashEraseNvm').disabled = false;
  $('flashReplaceNvm').checked = false;
  $('flashReplaceNvm').disabled = false;
  $('flashTtlTarget').checked = false;
  ui.pendingFirmwareUpload = request;
  renderFirmwareSafetyDialog();
  openDialog($('flashSafetyDialog'));
}

function uploadFirmware() { return beginFirmwareFlash(false); }
function onlineFirmware() { return beginFirmwareFlash(true); }

async function pollUpdate(force = false) {
  clearTimeout(ui.updateTimer);
  if (store.state.config.firmware_update_available === false) return;
  try {
    const response = await fetch('/api/update/status', {cache: 'no-store'});
    const value = await response.json();
    if (!response.ok) throw new Error(value?.error?.message || `HTTP ${response.status}`);
    const result = value.result || {};
    if (value.running) {
      ui.updateInProgress = true;
      setClass($('unknownCh340Notice'), 'hidden', true);
      syncUpdatePortOptions();
      updateMessage(`${value.stage || 'Updating'} - ${value.percent || 0}%${value.message ? ` - ${value.message}` : ''}`);
      ui.updateTimer = setTimeout(() => pollUpdate(false), 900);
    } else if (value.recovery_required) {
      ui.updateInProgress = false;
      ui.updatePort = '';
      syncUpdatePortOptions();
      updateMessage('Previous flash was interrupted. Press Flash firmware to retry safely using the preserved image.', true);
    } else if (result.ok === true) {
      if (ui.updatePort) {
        ui.suppressUnknownPort = ui.updatePort;
        ui.suppressUnknownUntil = Date.now() + 15000;
      }
      ui.updateInProgress = false;
      ui.updatePort = '';
      syncUpdatePortOptions();
      const message = result.message || 'Firmware update completed.';
      updateMessage(message, Boolean(result.adoption_error));
      if (result.restart_required && !ui.flashRestartRequested) {
        ui.flashRestartRequested = true;
        const jobId = String(value.job_id || '');
        setTimeout(async () => {
          try {
            const restartResponse = await fetch('/api/update/restart', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({job_id: jobId}),
            });
            const restartValue = await restartResponse.json();
            if (!restartResponse.ok) {
              throw new Error(restartValue?.error?.message || `HTTP ${restartResponse.status}`);
            }
          } catch (error) {
            ui.flashRestartRequested = false;
            updateMessage(`Firmware flashed, but Klipper restart failed: ${error.message || String(error)}`, true);
          }
          transport.refreshSoon(1500);
        }, 500);
      } else {
        transport.refreshSoon(400);
        setTimeout(() => refreshSerialPorts(), 1200);
        setTimeout(() => refreshSerialPorts(), 5000);
      }
    } else if (result.ok === false) {
      ui.updateInProgress = false;
      ui.updatePort = '';
      syncUpdatePortOptions();
      updateMessage(result.error || result.message || 'Firmware update failed.', true);
    } else if (ui.updateInProgress) {

      setClass($('unknownCh340Notice'), 'hidden', true);
      syncUpdatePortOptions();
      ui.updateTimer = setTimeout(() => pollUpdate(false), 400);
    } else if (force) {
      updateMessage('No firmware update is running.');
    }
  } catch (error) {
    if (ui.updateInProgress) {
      ui.updateTimer = setTimeout(() => pollUpdate(false), 900);
    } else if (force) {
      updateMessage(error.message || String(error), true);
    }
  }
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    store.patch({config: await response.json()});
  } catch (_) {}
}

function patchLightingDevice(deviceName, profile, lighting, action) {
  if (!plainObject(status()) || !Array.isArray(status().devices)) return;
  const next = cloneState(status());
  const device = next.devices.find((item) => item.name === deviceName);
  if (!device) return;
  const profiles = plainObject(device.lighting_profiles) ? device.lighting_profiles : {};
  if (action === 'SET') {
    profiles[profile] = normalizeLightingValue(lighting, device);
    device.lighting_profiles = profiles;
    device.lighting_profile = profile;
    device.lighting = cloneState(profiles[profile]);
    device.system_led_color = device.lighting.system_color;
  } else if (action === 'APPLY') {
    const selected = profile === 'DEFAULT' ? lightingDefault(device) : profiles[profile];
    if (!selected) return;
    device.lighting_profile = profile;
    device.lighting = normalizeLightingValue(selected, device);
    device.system_led_color = device.lighting.system_color;
  } else if (action === 'DELETE') {
    delete profiles[profile];
    device.lighting_profiles = profiles;
    if (String(device.lighting_profile || 'DEFAULT') === profile) {
      device.lighting_profile = 'DEFAULT';
      device.lighting = lightingDefault(device);
      device.system_led_color = device.lighting.system_color;
    }
  }
  store.replaceStatus(next, store.state.connection);
}

async function handleAction(target) {
  const action = target.dataset.action;
  const device = target.dataset.device;
  const channel = Number(target.dataset.channel);
  if (action === 'edit-channel') openChannelEditor(device, channel);
  else if (action === 'save-lighting-profile') {
    const profile = String(target.dataset.profile || '');
    const key = lightingDraftKey(device, profile);
    const draft = ui.ledDraft.get(key);
    if (!draft || profile === 'DEFAULT') return;
    await run(lightingProfileCommand(device, profile, draft, 'SET'), {success: `${profile} lighting saved and selected`});
    patchLightingDevice(device, profile, draft, 'SET');
    ui.ledDraft.delete(key);
    closeLightingPickerForDevice(device);
    renderSettings();
    transport.refreshSoon(0);
  }
  else if (action === 'cancel-lighting') {
    const profile = String(target.dataset.profile || 'DEFAULT');
    const key = lightingDraftKey(device, profile);
    const deviceState = devices().find((item) => item.name === device);
    const exists = Object.prototype.hasOwnProperty.call(lightingProfiles(deviceState), profile);
    ui.ledDraft.delete(key);
    closeLightingPickerForDevice(device);
    if (!exists) ui.lightingProfile.set(device, String(deviceState?.lighting_profile || 'DEFAULT'));
    renderSettings();
  }
  else if (action === 'apply-lighting-profile') {
    const profile = String(target.dataset.profile || 'DEFAULT');
    await run(lightingProfileCommand(device, profile, null, 'APPLY'), {success: `${profile === 'DEFAULT' ? 'Default' : profile} lighting selected`});
    patchLightingDevice(device, profile, null, 'APPLY');
    closeLightingPickerForDevice(device);
    renderSettings();
    transport.refreshSoon(0);
  }
  else if (action === 'delete-lighting-profile') {
    const profile = String(target.dataset.profile || '');
    if (!profile || profile === 'DEFAULT') return;
    confirmAction('Delete lighting profile', `Delete ${profile}?`, async () => {
      await run(lightingProfileCommand(device, profile, null, 'DELETE'), {success: `${profile} deleted`});
      patchLightingDevice(device, profile, null, 'DELETE');
      ui.ledDraft.delete(lightingDraftKey(device, profile));
      closeLightingPickerForDevice(device);
      ui.lightingProfile.set(device, 'DEFAULT');
      renderSettings();
      transport.refreshSoon(0);
    });
  }
  else if (action === 'add-lighting-profile') {
    const card = target.closest('.lighting-setting');
    const input = card?.querySelector('.lighting-profile-name');
    const name = String(input?.value || '').trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9 ._+()\-]{0,39}$/.test(name) || name.toUpperCase() === 'DEFAULT') {
      throw new Error('Use 1–40 letters, numbers, spaces, dot, +, parentheses, _ or -');
    }
    const deviceState = devices().find((item) => item.name === device);
    if (Object.prototype.hasOwnProperty.call(lightingProfiles(deviceState), name) ||
        ui.ledDraft.has(lightingDraftKey(device, name))) {
      throw new Error(`Lighting profile ${name} already exists`);
    }
    const sourceName = selectedLightingProfile(deviceState);
    const sourceKey = lightingDraftKey(device, sourceName);
    const source = ui.ledDraft.get(sourceKey) || lightingProfiles(deviceState)[sourceName] || normalizeLighting(deviceState);
    ui.ledDraft.set(lightingDraftKey(device, name), cloneState(source));
    ui.lightingProfile.set(device, name);
    if (input) input.value = '';
    renderSettings();
  }
  else if (action === 'load-channel') {
    setChannelOperation(device, channel, 'loading');
    try {
      await run(`BMCU_LOAD DEVICE=${gcodeValue(device)} CHANNEL=${channel}`, {success: `Channel ${channel + 1} loaded`});
      clearChannelOperation(device, channel);
      await transport.refreshNow();
    } catch (error) {
      clearChannelOperation(device, channel);
      transport.refreshSoon(0);
      throw error;
    }
  }
  else if (action === 'retract-channel') confirmAction(
    'Retract input filament',
    `Withdraw the filament completely from ${deviceLabel(device)} Channel ${channel + 1} until the BMCU input detector is clear? This is available only while the filament is parked in BMCU and the toolhead route is EMPTY.`,
    async () => {
      setChannelOperation(device, channel, 'retracting');
      try {
        await run(`BMCU_CHANNEL_RETRACT DEVICE=${gcodeValue(device)} CHANNEL=${channel}`, {success: `Channel ${channel + 1} input filament retracted`});
        clearChannelOperation(device, channel);
        await transport.refreshNow();
      } catch (error) {
        clearChannelOperation(device, channel);
        transport.refreshSoon(0);
        throw error;
      }
    });
  else if (action === 'unload-channel') confirmAction('Unload filament', `Unload ${deviceLabel(device)} Channel ${channel + 1}?`, async () => {
    setChannelOperation(device, channel, 'unloading');
    try {
      await run(`BMCU_UNLOAD DEVICE=${gcodeValue(device)} CHANNEL=${channel}`, {success: `Channel ${channel + 1} unloaded`});
      clearChannelOperation(device, channel);
      await transport.refreshNow();
    } catch (error) {
      clearChannelOperation(device, channel);
      transport.refreshSoon(0);
      throw error;
    }
  });
  else if (action === 'calibrate-device') await startCalibration(device, 'ALL');
  else if (action === 'calibrate-channel') await startCalibration(device, target.dataset.channel);
  else if (action === 'open-terminal-recovery') {
    setView('diagnostics');
    requestAnimationFrame(() => $('diagnosticChannels')?.scrollIntoView({behavior: 'smooth', block: 'start'}));
  }
  else if (action === 'route-confirm-empty') confirmAction(
    'Confirm no filament in BMCU',
    `Use this only when ${deviceLabel(device)} Channel ${channel + 1} has no filament at the BMCU input and the complete downstream route is empty.`,
    async () => {
      await run(`BMCU_ROUTE_CONFIRM DEVICE=${gcodeValue(device)} CHANNEL=${channel} STATE=EMPTY`, {success: `Channel ${channel + 1} confirmed empty`});
      await transport.refreshNow();
    });
  else if (action === 'route-confirm-parked') confirmAction(
    'Confirm filament in BMCU only',
    `Use this when ${deviceLabel(device)} Channel ${channel + 1} still contains filament at the BMCU input, but no filament occupies the PTFE/toolhead path. The route remains available for Load and the separate Retract filament action can eject the input filament.`,
    async () => {
      await run(`BMCU_ROUTE_CONFIRM DEVICE=${gcodeValue(device)} CHANNEL=${channel} STATE=PARKED`, {success: `Channel ${channel + 1} confirmed parked in BMCU`});
      await transport.refreshNow();
    });
  else if (action === 'route-confirm-loaded') confirmAction(
    'Confirm filament loaded to toolhead',
    `Use this only if ${deviceLabel(device)} Channel ${channel + 1} filament really reaches or is captured by the toolhead/extruder. The route will be marked LOADED so normal Unload can remove it safely.`,
    async () => {
      await run(`BMCU_ROUTE_CONFIRM DEVICE=${gcodeValue(device)} CHANNEL=${channel} STATE=LOADED`, {success: `Channel ${channel + 1} confirmed loaded to toolhead`});
      await transport.refreshNow();
    });
  else if (action === 'resume-refill') confirmAction('Resume U1 refill', 'Verify the physical source/replacement routes and confirm the print is paused.', () => run('BMCU_REFILL_RESUME', {success: 'U1 refill resumed'}));
  else if (action === 'forget-device') {
    const current = devices().find((item) => item.name === device);
    if (!current || current.connected) return;
    confirmAction(
      'Remove and forget BMCU?',
      `${deviceLabel(current)} must stay disconnected. Saved routing, calibration, lighting and module settings for this hardware will be removed. The printer will restart Klipper after the configuration is updated.`,
      async () => {
        ui.busy = true;
        renderSettings();
        try {
          const response = await fetch('/api/devices/forget', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({device}),
          });
          const value = await response.json();
          if (!response.ok) throw new Error(value?.error?.message || `HTTP ${response.status}`);
          toast(value.warning
            ? `${deviceLabel(current)} removed. ${value.warning} Restarting Klipper...`
            : `${deviceLabel(current)} removed. Restarting Klipper...`,
          value.warning ? 'warning' : 'success');
          try {
            await run('RESTART', {allowBusy: true, noRefresh: true, silent: true});
          } catch (_) {}
        } catch (error) {
          toast(error?.message || String(error), 'error');
        } finally {
          ui.busy = false;
          renderSettings();
        }
      });
  }
  else if (action === 'open-calibration-settings') {
    setView('settings');
    requestAnimationFrame(() => $('bmcuSettingsSection')?.scrollIntoView({behavior: 'smooth', block: 'start'}));
  }
  else if (action === 'open-firmware-settings') {
    setView('settings');
    requestAnimationFrame(() => $('firmwareFlashSection')?.scrollIntoView({behavior: 'smooth', block: 'start'}));
  }
}

function bindEvents() {
  document.addEventListener('click', async (event) => {
    const dialogClose = event.target.closest('dialog button[value="cancel"]');
    if (dialogClose) {
      event.preventDefault();
      const dialog = dialogClose.closest('dialog');
      if (dialog?.id === 'flashSafetyDialog') ui.pendingFirmwareUpload = null;
      if (dialog?.id === 'lightingColorDialog') ui.lightingPicker = null;
      if (dialog?.id === 'calibrationSafetyDialog') ui.pendingCalibration = null;
      const cancelCallback = dialog?.id === 'confirmDialog' ? ui.confirmCancelCallback : null;
      if (dialog?.id === 'confirmDialog') {
        ui.confirmCallback = null;
        ui.confirmCancelCallback = null;
      }
      closeDialog(dialog);
      if (cancelCallback) cancelCallback();
      return;
    }
    const view = event.target.closest('button[data-view]');
    if (view) { setView(view.dataset.view); return; }
    const profileButton = event.target.closest('[data-u1-profile]');
    if (profileButton) {
      ui.u1MaterialProfile = String(profileButton.dataset.u1Profile || 'DEFAULT');
      renderU1GcodeSettings();
      return;
    }
    const lightingColor = event.target.closest('[data-light-color]');
    if (lightingColor) {
      openLightingPicker(lightingColor);
      return;
    }
    const lightingProfile = event.target.closest('[data-light-profile]');
    if (lightingProfile) {
      const deviceName = String(lightingProfile.dataset.device || '');
      closeLightingPickerForDevice(deviceName);
      ui.lightingProfile.set(deviceName, String(lightingProfile.dataset.lightProfile || 'DEFAULT'));
      renderSettings();
      return;
    }
    const movementReset = event.target.closest('[data-u1-reset-movement]');
    if (movementReset) {
      resetU1Movement(String(movementReset.dataset.u1ResetMovement || ''));
      return;
    }
    const action = event.target.closest('[data-action]');
    if (action) { try { await handleAction(action); } catch (_) {} }
  });
  document.addEventListener('input', (event) => {
    const u1Input = event.target.closest('#u1TipTemp, #u1MoveRigidStandard, #u1MoveSoftStandard, #u1MoveRigidFine, #u1MoveSoftFine');
    if (u1Input) {
      updateU1ProfileDraft();
      return;
    }
    const input = event.target.closest('[data-light-device]');
    if (!input) return;
    const deviceName = input.dataset.lightDevice;
    const profile = String(input.dataset.lightProfile || 'DEFAULT');
    if (profile === 'DEFAULT') return;
    const device = devices().find((item) => item.name === deviceName);
    if (!device) return;
    const profiles = lightingProfiles(device);
    const key = lightingDraftKey(deviceName, profile);
    const saved = profiles[profile] || normalizeLighting(device);
    const draft = cloneState(ui.ledDraft.get(key) || saved);
    const group = input.dataset.lightGroup;
    let value = input.value;
    if (input.type === 'range' || input.tagName === 'SELECT') value = Number(value);
    else value = String(value).toUpperCase();
    if (group) draft[group][input.dataset.lightKey] = value;
    else draft[input.dataset.lightKey] = value;
    if (Object.prototype.hasOwnProperty.call(profiles, profile) && lightingEqual(draft, saved)) ui.ledDraft.delete(key);
    else ui.ledDraft.set(key, draft);
    if (input.type === 'color' || input.dataset.lightKey === 'filament_brightness')
      queueLightingPreview(device, group, input.dataset.lightKey, value, draft);
    renderSettings();
  });
  $('lightingPickerSv')?.addEventListener('pointerdown', (event) => {
    if (!ui.lightingPicker) return;
    event.preventDefault();
    $('lightingPickerSv').setPointerCapture?.(event.pointerId);
    lightingPickerSvValue(event);
  });
  $('lightingPickerSv')?.addEventListener('pointermove', (event) => {
    if (!ui.lightingPicker || !event.buttons) return;
    lightingPickerSvValue(event);
  });
  $('lightingPickerHue')?.addEventListener('input', () => {
    const picker = ui.lightingPicker;
    if (!picker) return;
    picker.h = Number($('lightingPickerHue').value);
    setLightingPickerValue(lightingHsvToHex(picker.h, picker.s, picker.v));
  });
  $('lightingPickerHex')?.addEventListener('input', () => {
    const value = String($('lightingPickerHex').value || '').trim();
    if (/^#[0-9A-Fa-f]{6}$/.test(value)) setLightingPickerValue(value);
  });
  $('lightingPickerBack')?.addEventListener('click', () => {
    if (ui.lightingPicker) setLightingPickerValue(ui.lightingPicker.original);
  });
  $('lightingPickerDefault')?.addEventListener('click', () => {
    if (ui.lightingPicker) setLightingPickerValue(ui.lightingPicker.defaultValue);
  });
  $('lightingPickerDone')?.addEventListener('click', () => closeLightingPicker());
  $('lightingPickerClose')?.addEventListener('click', () => closeLightingPicker());
  $('lightingColorDialog')?.addEventListener('close', () => {
    if (!ui.lightingPicker) return;
    ui.lightingPicker = null;
    renderSettings();
  });
  document.addEventListener('change', (event) => {
    if (event.target.id === 'u1TipTempMode') {
      updateU1ProfileDraft();
      return;
    }
    const route = event.target.closest('[data-route-device]');
    if (route) {
      const key = `${route.dataset.routeDevice}:${Number(route.dataset.routeChannel)}`;
      const item = allChannels().find(({device, channel}) => keyFor(device, channel) === key);
      const current = String(item?.channel?.endpoint || '');
      if (String(route.value || '') === current) ui.routeDraft.delete(key);
      else ui.routeDraft.set(key, String(route.value || ''));
      renderDashboardRouting();
    }
  });
  $('leaveFinalFilamentLoaded').addEventListener('change', (event) => {
    const saved = status().preferences?.leave_final_filament_loaded === true;
    const desired = Boolean(event.currentTarget.checked);
    ui.preferenceDraft = desired === saved ? null : desired;
    renderSettings();
  });
  $('cancelSetupPreferences').addEventListener('click', () => {
    ui.preferenceDraft = null;
    renderSettings();
  });
  $('saveSetupPreferences').addEventListener('click', async () => {
    if (ui.preferenceDraft == null) return;
    const desired = Boolean(ui.preferenceDraft);
    ui.busy = true;
    renderSettings();
    try {
      await run(`BMCU_SET_PREFERENCES LEAVE_FINAL_FILAMENT_LOADED=${desired ? 1 : 0}`, {allowBusy: true, noRefresh: true, silent: true});
      ui.preferenceDraft = null;
      toast('Setup saved', 'success');
      await transport.refreshNow();
    } catch (error) {
      toast(error?.message || String(error), 'error');
    } finally {
      ui.busy = false;
      renderSettings();
    }
  });
  $('u1GcodeSave').addEventListener('click', () => saveU1Gcode().catch((error) => {
    toast(error?.message || String(error), 'error');
  }));
  $('u1GcodeCancel').addEventListener('click', cancelU1Gcode);
  $('u1GcodeReset').addEventListener('click', resetU1Gcode);
  $('u1GcodeDelete').addEventListener('click', deleteU1MaterialProfile);
  $('u1MaterialAdd').addEventListener('click', addU1MaterialProfile);
  for (const input of [$('u1MaterialName'), $('editMaterial')]) {
    input.addEventListener('input', () => {
      const start = input.selectionStart;
      const end = input.selectionEnd;
      const upper = String(input.value || '').toUpperCase();
      if (input.value !== upper) {
        input.value = upper;
        if (start != null && end != null) input.setSelectionRange(start, end);
      }
    });
    input.addEventListener('blur', () => {
      input.value = normalizeMaterialEditorValue(input.value);
    });
  }
  $('u1MaterialName').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      addU1MaterialProfile();
    }
  });
  $('saveRoutes').addEventListener('click', saveRoutes);
  $('discardRoutes').addEventListener('click', discardRoutes);
  $('saveChannel').addEventListener('click', () => saveChannelEditor().catch(() => {}));
  $('channelDialog').addEventListener('close', () => { ui.editing = null; });
  $('channelDialog').addEventListener('cancel', () => { ui.editing = null; });
  $('editUnloadRetract').addEventListener('input', syncLengthConversions);
  $('editAutoload').addEventListener('input', syncLengthConversions);
  $('confirmAction').addEventListener('click', async () => {
    const callback = ui.confirmCallback;
    ui.confirmCallback = null;
    ui.confirmCancelCallback = null;
    closeDialog($('confirmDialog'));
    if (callback) { try { await callback(); } catch (_) {} }
  });
  $('confirmDialog').addEventListener('cancel', (event) => {
    event.preventDefault();
    const callback = ui.confirmCancelCallback;
    ui.confirmCallback = null;
    ui.confirmCancelCallback = null;
    closeDialog($('confirmDialog'));
    if (callback) callback();
  });
  $('openPrinterHelp').addEventListener('click', () => {
    renderPrinterHelp();
    openDialog($('printerHelpDialog'));
  });
  $('openU1TipHelp').addEventListener('click', () => openDialog($('u1TipHelpDialog')));
  $('u1TipHelpDialog').addEventListener('click', (event) => {
    if (event.target === $('u1TipHelpDialog')) closeDialog($('u1TipHelpDialog'));
  });
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy-target]');
    if (!button) return;
    event.preventDefault();
    const target = $(button.dataset.copyTarget);
    try {
      const copied = await copyTextPortable(target?.textContent || '');
      if (copied) toast(button.dataset.copyMessage || 'Copied', 'success');
    } catch (error) {
      toast(error.message || 'Could not copy', 'error');
    }
  });
  $('downloadOrcaProfileJson').addEventListener('click', () => downloadOrcaProfile('json'));
  $('downloadOrcaProfileBundle').addEventListener('click', () => downloadOrcaProfile('bundle'));
  $('downloadSnapmakerOrcaProfile').addEventListener('click', downloadSnapmakerOrcaProfile);
  $('selectCopyFallback').addEventListener('click', selectCopyFallback);
  $('copyFallbackDialog').addEventListener('click', (event) => {
    if (event.target === $('copyFallbackDialog')) closeDialog($('copyFallbackDialog'));
  });
  $('updateUpload').addEventListener('click', uploadFirmware);
  $('updateOnline').addEventListener('click', onlineFirmware);
  $('flashSafetyContinue').addEventListener('click', () => continueFirmwareUpload().catch(() => {}));
  for (const id of ['flashTargetRisk', 'flashUnknownTarget', 'flashEraseNvm', 'flashReplaceNvm', 'flashTtlTarget']) $(id).addEventListener('change', renderFirmwareSafetyDialog);
  $('calibrationSafetyContinue').addEventListener('click', () => continueCalibration().catch(() => {}));
  $('calibrationSafetyDialog').addEventListener('cancel', () => { ui.pendingCalibration = null; });
  $('calibrationSafetyDialog').addEventListener('close', () => {
    if (!$('calibrationSafetyDialog').open) ui.pendingCalibration = null;
  });
  $('flashSafetyDialog').addEventListener('cancel', () => { ui.pendingFirmwareUpload = null; });
  $('flashSafetyDialog').addEventListener('close', () => {
    if (!$('flashSafetyDialog').open) ui.pendingFirmwareUpload = null;
  });
  $('updatePortsRefresh').addEventListener('click', () => refreshSerialPorts(true));
  $('updateDevice').addEventListener('change', syncUpdatePortOptions);
  $('openDiagnosticsExport').addEventListener('click', () => openDialog($('diagnosticsExportDialog')));
  $('diagnosticsSelectAll').addEventListener('click', () => setDiagnosticsSelection(true));
  $('diagnosticsSelectNone').addEventListener('click', () => setDiagnosticsSelection(false));
  $('diagnosticsExport').addEventListener('click', downloadDiagnosticsArchive);
  for (const radio of qsa('input[name="updateMode"]')) radio.addEventListener('change', syncFlashModeUI);
}

async function init() {
  bindEvents();
  store.subscribe(render);
  await loadConfig();
  await transport.start(store.state.config);
  await pollUpdate(false);
  checkReleaseVersions();
  render();
}

document.addEventListener('DOMContentLoaded', init);
