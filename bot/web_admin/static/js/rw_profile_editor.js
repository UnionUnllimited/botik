/* Remnawave config profile editor — чистая сборка по образцу 3X-UI / Xray-core */
(function () {
  'use strict';

  const root = document.getElementById('rwPeRoot');
  if (!root) return;

  // Секретный путь берём из data-атрибута шаблона (Jinja подставляет его там),
  // т.к. статический JS не видит {{ }} и глобальный SECRET_PATH может быть ещё не
  // определён на момент выполнения этого скрипта.
  let secretPath = root.dataset.secretPath;
  if (secretPath === undefined) {
    secretPath = (typeof SECRET_PATH !== 'undefined' && SECRET_PATH) ? SECRET_PATH : '';
  }
  // cleanPath: '' (нет секретного пути) → запросы вида '/api/...';
  // иначе '/<secret>' → '/<secret>/api/...'. Без двойного слэша (//api = другой хост).
  const cleanPath = secretPath ? (secretPath.startsWith('/') ? secretPath : '/' + secretPath) : '';

  const MODE = root.dataset.mode || 'new';
  const PROFILE_UUID = root.dataset.uuid || '';
  const initialEl = document.getElementById('rwPeInitialConfig');

  let config = {};
  let cm = null;
  let modalCtx = null;

  // Транспорты по ТЗ: RAW (tcp), WS, XHTTP, gRPC
  const NETWORKS = [
    { value: 'raw', label: 'RAW (TCP)' },
    { value: 'ws', label: 'WebSocket' },
    { value: 'xhttp', label: 'XHTTP' },
    { value: 'grpc', label: 'gRPC' },
  ];
  const NETWORK_VALUES = NETWORKS.map(n => n.value);

  // Протоколы по ТЗ
  const INBOUND_PROTOCOLS = ['vless', 'trojan', 'shadowsocks', 'hysteria2'];
  const OUTBOUND_PROTOCOLS = ['freedom', 'blackhole', 'vless', 'trojan', 'shadowsocks', 'hysteria2'];

  const SS_METHODS = [
    'aes-256-gcm', 'aes-128-gcm', 'chacha20-poly1305', 'chacha20-ietf-poly1305',
    '2022-blake3-aes-256-gcm', '2022-blake3-aes-128-gcm', '2022-blake3-chacha20-poly1305',
  ];
  const UTLS_FP = ['chrome', 'firefox', 'safari', 'ios', 'android', 'edge', '360', 'qq', 'random', 'randomized', 'randomizednoalpn', 'unsafe'];
  const SECURITIES = ['none', 'tls', 'reality'];
  const TLS_VERSIONS = ['1.0', '1.1', '1.2', '1.3'];
  const ALPN_OPTS = ['h3', 'h2', 'http/1.1'];
  const USAGE_OPTS = ['encipherment', 'verify', 'issue'];
  const TLS_CIPHERS = [
    'TLS_AES_128_GCM_SHA256', 'TLS_AES_256_GCM_SHA384', 'TLS_CHACHA20_POLY1305_SHA256',
    'TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA', 'TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA',
    'TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA', 'TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA',
    'TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256', 'TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384',
    'TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256', 'TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384',
    'TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256', 'TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256',
  ];

  function randomHex(len) {
    let s = '';
    const c = '0123456789abcdef';
    for (let i = 0; i < len; i++) s += c[Math.floor(Math.random() * 16)];
    return s;
  }
  function randomShortIds() {
    const lens = [0, 2, 4, 6, 8, 10, 12, 14, 16];
    return lens.map(l => randomHex(l));
  }

  const DEFAULT_LOG_LEVEL = 'warning';

  const INBOUND_PRESETS = {
    vless_reality: {
      tag: 'VLESS_REALITY', port: 443, listen: '0.0.0.0', protocol: 'vless',
      settings: { clients: [], decryption: 'none' },
      sniffing: { enabled: true, destOverride: ['http', 'tls', 'quic'] },
      streamSettings: {
        network: 'raw', security: 'reality',
        realitySettings: { target: 'www.microsoft.com:443', serverNames: ['www.microsoft.com'], shortIds: [''], privateKey: '', fingerprint: 'firefox' },
      },
    },
    vless_ws: {
      tag: 'VLESS_WS', port: 443, listen: '0.0.0.0', protocol: 'vless',
      settings: { clients: [], decryption: 'none' },
      sniffing: { enabled: true, destOverride: ['http', 'tls', 'quic'] },
      streamSettings: { network: 'ws', security: 'tls', wsSettings: { path: '/', host: '' }, tlsSettings: { serverName: '' } },
    },
    shadowsocks: {
      tag: 'SS', port: 8388, listen: '0.0.0.0', protocol: 'shadowsocks',
      settings: { method: 'chacha20-ietf-poly1305', password: '', network: 'tcp,udp' },
      sniffing: { enabled: true, destOverride: ['http', 'tls'] },
    },
    trojan_tls: {
      tag: 'TROJAN', port: 443, listen: '0.0.0.0', protocol: 'trojan',
      settings: { clients: [] },
      sniffing: { enabled: true, destOverride: ['http', 'tls'] },
      streamSettings: { network: 'raw', security: 'tls', tlsSettings: { serverName: '' } },
    },
    hysteria2: {
      tag: 'HYSTERIA2', port: 36712, listen: '0.0.0.0', protocol: 'hysteria2',
      settings: { clients: [] },
      sniffing: { enabled: true, destOverride: ['http', 'tls', 'quic'] },
      streamSettings: { network: 'udp', security: 'tls', tlsSettings: { alpn: ['h3'], certificates: [{ certificateFile: '', keyFile: '' }] } },
    },
  };

  const OUTBOUND_PRESETS = {
    freedom: { tag: 'DIRECT', protocol: 'freedom' },
    blackhole: { tag: 'BLOCK', protocol: 'blackhole' },
  };

  const RULE_PRESETS = {
    block_private: { type: 'field', ip: ['geoip:private'], outboundTag: 'BLOCK' },
    block_bt: { type: 'field', protocol: ['bittorrent'], outboundTag: 'BLOCK' },
  };

  /* ── utils ── */
  function deepClone(o) { return JSON.parse(JSON.stringify(o)); }
  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function linesToArr(t) { return String(t || '').split('\n').map(s => s.trim()).filter(Boolean); }
  function arrToLines(a) { return Array.isArray(a) ? a.join('\n') : ''; }
  function val(id) { const e = document.getElementById(id); return e ? e.value : undefined; }
  function checked(id) { const e = document.getElementById(id); return e ? e.checked : undefined; }

  function uniqueTag(base, existing) {
    const tags = new Set(existing.map(x => x.tag).filter(Boolean));
    if (!tags.has(base)) return base;
    let i = 2;
    while (tags.has(`${base}${i}`)) i++;
    return `${base}${i}`;
  }

  function ensureConfigStructure(c) {
    if (!c || typeof c !== 'object') c = {};
    if (!c.log || typeof c.log !== 'object') c.log = {};
    if (!c.log.loglevel) c.log.loglevel = DEFAULT_LOG_LEVEL;
    if (!Array.isArray(c.inbounds)) c.inbounds = [];
    if (!Array.isArray(c.outbounds)) c.outbounds = [{ tag: 'DIRECT', protocol: 'freedom' }, { tag: 'BLOCK', protocol: 'blackhole' }];
    if (!c.routing || typeof c.routing !== 'object') c.routing = { domainStrategy: 'AsIs', rules: [] };
    if (!c.routing.domainStrategy) c.routing.domainStrategy = 'AsIs';
    if (!Array.isArray(c.routing.rules)) c.routing.rules = [];
    return c;
  }

  function splitDomainField(arr) {
    const matchers = [], plain = [];
    for (const d of (arr || [])) (/^(geosite:|regexp:|full:|domain:|ext:)/.test(d) ? matchers : plain).push(d);
    return { matchers, plain };
  }
  function splitIpField(arr) {
    const geoip = [], plain = [];
    for (const i of (arr || [])) (i.startsWith('geoip:') || i.startsWith('ext:') ? geoip : plain).push(i);
    return { geoip, plain };
  }

  /* ── summaries ── */
  function inboundSummary(ib) {
    if (ib.protocol === 'hysteria2') {
      const sec = ib.streamSettings?.security || 'tls';
      const sni = ib.streamSettings?.tlsSettings?.serverName;
      const obfs = ib.settings?.obfs?.type;
      const up = ib.settings?.up, down = ib.settings?.down;
      const parts = ['udp', sec];
      if (sni) parts.push(sni);
      if (obfs) parts.push('obfs:' + obfs);
      if (up || down) parts.push([up, down].filter(Boolean).join('/'));
      return parts.join(' · ');
    }
    const ss = ib.streamSettings || {};
    const net = ss.network || 'raw';
    const sec = ss.security || 'none';
    const tr = transportHint(ss);
    return tr + (sec !== 'none' ? ' · ' + sec : '');
  }
  function transportHint(ss) {
    const net = ss?.network || 'raw';
    if (net === 'raw') {
      const hdr = readTransport(ss, net).header?.type;
      return hdr && hdr !== 'none' ? `raw/${hdr}` : 'raw';
    }
    const t = readTransport(ss, net);
    if (net === 'ws' || net === 'xhttp') {
      const bits = [net];
      if (t.host) bits.push(t.host);
      if (t.path && t.path !== '/') bits.push(t.path);
      if (net === 'xhttp' && t.mode && t.mode !== 'auto') bits.push(t.mode);
      return bits.join(' · ');
    }
    if (net === 'grpc') return t.serviceName ? `grpc · ${t.serviceName}` : 'grpc';
    if (net === 'udp') return 'udp';
    return net;
  }
  function outboundSummary(ob) {
    if (ob.protocol === 'freedom' || ob.protocol === 'blackhole') return '—';
    const addr = outboundAddress(ob);
    const ss = ob.streamSettings || {};
    const parts = [addr];
    if (ob.protocol === 'hysteria2') {
      parts.push('udp');
      if (ss.security && ss.security !== 'none') parts.push(ss.security);
      const sni = ss.tlsSettings?.serverName;
      if (sni) parts.push(sni);
      return parts.join(' · ');
    }
    const tr = transportHint(ss);
    if (tr && tr !== 'raw') parts.push(tr);
    else if (ss.network && ss.network !== 'raw') parts.push(ss.network);
    if (ss.security && ss.security !== 'none') parts.push(ss.security);
    return parts.join(' · ');
  }
  function outboundAddress(ob) {
    if (ob.protocol === 'freedom' || ob.protocol === 'blackhole') return '—';
    const s = ob.settings || {};
    if (s.servers?.length) return `${s.servers[0].address || ''}:${s.servers[0].port || ''}`;
    if (s.vnext?.length) return `${s.vnext[0].address || ''}:${s.vnext[0].port || ''}`;
    if (s.address) return `${s.address}:${s.port || ''}`;
    return '—';
  }
  function ruleMatchSummary(rule) {
    const parts = [];
    if (rule.inboundTag) {
      const t = Array.isArray(rule.inboundTag) ? rule.inboundTag.join(', ') : rule.inboundTag;
      parts.push('in: ' + t);
    }
    if (rule.domain?.length) parts.push('domain: ' + rule.domain.slice(0, 2).join(', ') + (rule.domain.length > 2 ? '…' : ''));
    if (rule.ip?.length) parts.push('ip: ' + rule.ip.slice(0, 2).join(', ') + (rule.ip.length > 2 ? '…' : ''));
    if (rule.source?.length) parts.push('src: ' + rule.source.slice(0, 2).join(', ') + (rule.source.length > 2 ? '…' : ''));
    if (rule.protocol?.length) parts.push('proto: ' + rule.protocol.join(', '));
    if (rule.network) parts.push('net: ' + rule.network);
    if (rule.port) parts.push('port: ' + rule.port);
    if (rule.sourcePort) parts.push('srcPort: ' + rule.sourcePort);
    return parts.join(' · ') || '—';
  }
  function ruleInboundLabel(rule) {
    const t = rule?.inboundTag;
    if (!t || (Array.isArray(t) && !t.length)) return 'все';
    if (Array.isArray(t)) return t.join(', ');
    return String(t);
  }
  function ruleInboundFormValue(d) {
    const t = d?.inboundTag;
    if (!t) return '';
    if (Array.isArray(t)) return t[0] || '';
    return String(t);
  }

  /* ── transport key ── */
  function normalizeNetwork(type) {
    const t = (type || '').toLowerCase();
    if (!t || t === 'tcp' || t === 'none' || t === 'raw') return 'raw';
    if (t === 'http') return 'raw';
    if (t === 'hysteria' || t === 'hysteria2') return 'udp';
    if (NETWORK_VALUES.includes(t)) return t;
    return 'raw';
  }
  function normalizeStreamSettings(d) {
    const ss = d?.streamSettings;
    if (!ss || typeof ss !== 'object') return;
    if (ss.network === 'tcp') ss.network = 'raw';
    if (ss.network === 'hysteria') ss.network = 'udp';
    if (ss.tcpSettings && !ss.rawSettings) {
      ss.rawSettings = ss.tcpSettings;
      delete ss.tcpSettings;
    }
  }
  function networkKey(net) {
    return { raw: 'rawSettings', ws: 'wsSettings', xhttp: 'xhttpSettings', grpc: 'grpcSettings' }[net] || null;
  }
  function readTransport(ss, net) {
    if (net === 'raw') return ss.rawSettings || ss.tcpSettings || {};
    return ss[networkKey(net)] || {};
  }

  function normalizeInboundDraft(d) {
    if (!d || typeof d !== 'object') return d;
    normalizeStreamSettings(d);
    if (d.protocol === 'hysteria2') {
      d.settings = d.settings || {};
      if (!Array.isArray(d.settings.clients)) d.settings.clients = d.settings.clients || [];
      d.streamSettings = d.streamSettings || {};
      if (!d.streamSettings.network || d.streamSettings.network === 'hysteria') d.streamSettings.network = 'udp';
      if (!d.streamSettings.security) d.streamSettings.security = 'tls';
      d.streamSettings.tlsSettings = d.streamSettings.tlsSettings || { certificates: [] };
      if (!d.streamSettings.tlsSettings.alpn?.length) d.streamSettings.tlsSettings.alpn = ['h3'];
    }
    return d;
  }
  function normalizeOutboundDraft(d) {
    if (!d || typeof d !== 'object') return d;
    normalizeStreamSettings(d);
    if (d.protocol === 'hysteria2') {
      d.streamSettings = d.streamSettings || {};
      if (!d.streamSettings.network || d.streamSettings.network === 'hysteria') d.streamSettings.network = 'udp';
      if (!d.streamSettings.security) d.streamSettings.security = 'tls';
      d.streamSettings.tlsSettings = d.streamSettings.tlsSettings || {};
      if (!d.streamSettings.tlsSettings.alpn?.length) d.streamSettings.tlsSettings.alpn = ['h3'];
    }
    return d;
  }
  function cleanConfig(c) {
    c = ensureConfigStructure(deepClone(c));
    c.inbounds = (c.inbounds || []).map(cleanInbound);
    c.outbounds = (c.outbounds || []).map(cleanOutbound);
    if (c.routing?.rules) c.routing.rules = c.routing.rules.map(cleanRule);
    return c;
  }

  /* ── CLEAN builders: только релевантные поля ── */
  function cleanInbound(d) {
    const out = { tag: d.tag, port: d.port, listen: d.listen || '0.0.0.0', protocol: d.protocol };
    const s = d.settings || {};

    if (d.protocol === 'vless') out.settings = { clients: s.clients || [], decryption: s.decryption || 'none' };
    else if (d.protocol === 'trojan') out.settings = { clients: s.clients || [] };
    else if (d.protocol === 'shadowsocks') out.settings = { method: s.method || 'chacha20-ietf-poly1305', password: s.password || '', network: s.network || 'tcp,udp' };
    else if (d.protocol === 'hysteria2') {
      out.settings = { clients: s.clients || [] };
      if (s.ignoreClientBandwidth) out.settings.ignoreClientBandwidth = true;
      if (s.up) out.settings.up = s.up;
      if (s.down) out.settings.down = s.down;
      if (s.obfs?.type) out.settings.obfs = { type: s.obfs.type, password: s.obfs.password || '' };
      const ss = d.streamSettings || {};
      const sec = ss.security === 'none' ? 'none' : 'tls';
      const stream = { network: 'udp', security: sec };
      if (sec === 'tls') {
        const tls = cleanTls(ss.tlsSettings || {}, { pathsOnly: true, hysteria: true });
        if (!tls.alpn?.length) tls.alpn = ['h3'];
        stream.tlsSettings = tls;
      }
      out.streamSettings = stream;
    } else out.settings = s;

    out.sniffing = {
      enabled: d.sniffing?.enabled !== false,
      destOverride: (d.sniffing?.destOverride && d.sniffing.destOverride.length) ? d.sniffing.destOverride : ['http', 'tls', 'quic'],
    };

    if (d.protocol !== 'hysteria2') {
      const ss = d.streamSettings || {};
      const net = NETWORK_VALUES.includes(ss.network) ? ss.network : 'raw';
      const sec = SECURITIES.includes(ss.security) ? ss.security : 'none';
      const stream = { network: net, security: sec };
      const t = readTransport(ss, net);

      if (net === 'raw') {
        if (t.header?.type === 'http') stream.rawSettings = { header: t.header };
      } else if (net === 'ws') {
        stream.wsSettings = { path: t.path || '/', host: t.host || '' };
      } else if (net === 'xhttp') {
        stream.xhttpSettings = { path: t.path || '/', host: t.host || '', mode: t.mode || 'auto' };
      } else if (net === 'grpc') {
        stream.grpcSettings = { serviceName: t.serviceName || '', authority: t.authority || '', multiMode: !!t.multiMode };
      }

      if (sec === 'reality') stream.realitySettings = cleanReality(ss.realitySettings || {});
      else if (sec === 'tls') stream.tlsSettings = cleanTls(ss.tlsSettings || {}, { pathsOnly: true });
      out.streamSettings = stream;
    }
    return out;
  }

  function cleanOutbound(d) {
    const out = { tag: d.tag, protocol: d.protocol };
    if (d.protocol === 'freedom' || d.protocol === 'blackhole') return out;
    const s = d.settings || {};

    if (d.protocol === 'vless' || d.protocol === 'vmess') {
      const vn = s.vnext?.[0] || {}; const u = vn.users?.[0] || {};
      out.settings = { vnext: [{ address: vn.address || '', port: vn.port || 443, users: [{ id: u.id || '', encryption: u.encryption || 'none', flow: u.flow || '' }] }] };
    } else if (d.protocol === 'trojan') {
      const srv = s.servers?.[0] || {};
      out.settings = { servers: [{ address: srv.address || '', port: srv.port || 443, password: srv.password || '' }] };
    } else if (d.protocol === 'shadowsocks') {
      const srv = s.servers?.[0] || {};
      out.settings = { servers: [{ address: srv.address || '', port: srv.port || 443, password: srv.password || '', method: srv.method || 'chacha20-ietf-poly1305' }] };
    } else if (d.protocol === 'hysteria2') {
      const srv = s.servers?.[0] || {};
      out.settings = { servers: [{ address: srv.address || '', port: srv.port || 443, password: srv.password || '' }] };
    } else {
      out.settings = s;
    }

    const ss = d.streamSettings || {};
    if (ss.network || ss.security || d.protocol === 'hysteria2') {
      const net = d.protocol === 'hysteria2' ? 'udp' : (NETWORK_VALUES.includes(ss.network) ? ss.network : 'raw');
      const sec = d.protocol === 'hysteria2' ? (ss.security === 'none' ? 'none' : 'tls') : (SECURITIES.includes(ss.security) ? ss.security : 'none');
      const stream = { network: net, security: sec };
      const t = readTransport(ss, net);
      if (net === 'raw') {
        if (t.header?.type === 'http') stream.rawSettings = { header: t.header };
      } else if (net === 'ws') {
        stream.wsSettings = { path: t.path || '/', host: t.host || '' };
      } else if (net === 'xhttp') {
        stream.xhttpSettings = { path: t.path || '/', host: t.host || '', mode: t.mode || 'auto' };
      } else if (net === 'grpc') {
        stream.grpcSettings = { serviceName: t.serviceName || '', authority: t.authority || '', multiMode: !!t.multiMode };
      }
      if (sec === 'reality') {
        const r = ss.realitySettings || {};
        stream.realitySettings = { serverName: r.serverName || '', publicKey: r.publicKey || '', shortId: r.shortId || '', fingerprint: r.fingerprint || 'firefox' };
      } else if (sec === 'tls') {
        stream.tlsSettings = { serverName: ss.tlsSettings?.serverName || '' };
        const fp = ss.tlsSettings?.fingerprint || ss.tlsSettings?.settings?.fingerprint;
        if (fp) stream.tlsSettings.fingerprint = fp;
        const alpn = ss.tlsSettings?.alpn;
        if (alpn?.length) stream.tlsSettings.alpn = alpn;
        else if (d.protocol === 'hysteria2') stream.tlsSettings.alpn = ['h3'];
        if (ss.tlsSettings?.allowInsecure) stream.tlsSettings.allowInsecure = true;
      }
      if (sec !== 'none' || net !== 'raw' || d.protocol === 'hysteria2') out.streamSettings = stream;
    }
    return out;
  }

  function cleanRule(d) {
    const out = { type: d.type || 'field' };
    if (d.domain?.length) out.domain = d.domain;
    if (d.ip?.length) out.ip = d.ip;
    if (d.source?.length) out.source = d.source;
    if (d.protocol?.length) out.protocol = d.protocol;
    if (d.network) out.network = d.network;
    if (d.port) out.port = d.port;
    if (d.sourcePort) out.sourcePort = d.sourcePort;
    if (d.outboundTag) out.outboundTag = d.outboundTag;
    if (d.inboundTag) {
      if (Array.isArray(d.inboundTag) && d.inboundTag.length) out.inboundTag = d.inboundTag;
      else if (typeof d.inboundTag === 'string' && d.inboundTag.trim()) out.inboundTag = d.inboundTag.trim();
    }
    return out;
  }

  /* ── config sync ── */
  function loadInitialConfig() {
    try {
      config = ensureConfigStructure(JSON.parse(initialEl ? initialEl.textContent : '{}'));
      config.inbounds = (config.inbounds || []).map(ib => cleanInbound(normalizeInboundDraft(deepClone(ib))));
      config.outbounds = (config.outbounds || []).map(ob => cleanOutbound(normalizeOutboundDraft(deepClone(ob))));
      if (config.routing?.rules) config.routing.rules = config.routing.rules.map(cleanRule);
    }
    catch (e) { config = ensureConfigStructure({}); showToast('Ошибка JSON: ' + e.message, true); }
  }
  function syncGeneralFromConfig() {
    document.getElementById('rwPeLogLevel').value = config.log?.loglevel || DEFAULT_LOG_LEVEL;
    document.getElementById('rwPeLogError').value = config.log?.error || '';
    document.getElementById('rwPeLogAccess').value = config.log?.access || '';
    document.getElementById('rwPeDomainStrategy').value = config.routing?.domainStrategy || 'AsIs';
    const hasDns = !!config.dns;
    document.getElementById('rwPeDnsEnabled').checked = hasDns;
    document.getElementById('rwPeDnsBody').style.display = hasDns ? '' : 'none';
    document.getElementById('rwPeDnsOffHint').style.display = hasDns ? 'none' : '';
    if (hasDns) {
      const lines = (config.dns.servers || []).map(s => typeof s === 'string' ? s : s.address || JSON.stringify(s));
      document.getElementById('rwPeDnsServers').value = lines.join('\n');
      document.getElementById('rwPeDnsQueryStrategy').value = config.dns.queryStrategy || '';
    }
  }
  function syncGeneralToConfig() {
    config.log = config.log || {};
    config.log.loglevel = document.getElementById('rwPeLogLevel').value;
    const errPath = (document.getElementById('rwPeLogError').value || '').trim();
    const accPath = (document.getElementById('rwPeLogAccess').value || '').trim();
    if (errPath) config.log.error = errPath; else delete config.log.error;
    if (accPath) config.log.access = accPath; else delete config.log.access;
    config.routing = config.routing || { rules: [] };
    config.routing.domainStrategy = document.getElementById('rwPeDomainStrategy').value;
    if (document.getElementById('rwPeDnsEnabled').checked) {
      config.dns = { servers: linesToArr(document.getElementById('rwPeDnsServers').value) };
      const qs = document.getElementById('rwPeDnsQueryStrategy').value;
      if (qs) config.dns.queryStrategy = qs; else delete config.dns.queryStrategy;
    } else { delete config.dns; }
  }
  function buildConfigJson() { syncGeneralToConfig(); return cleanConfig(config); }
  function updateJsonEditor() {
    const json = JSON.stringify(buildConfigJson(), null, 2);
    if (cm) cm.setValue(json); else document.getElementById('rwPeJsonTextarea').value = json;
  }

  /* ── render tables ── */
  function renderInbounds() {
    const tbody = document.getElementById('rwPeInboundTbody');
    const list = config.inbounds || [];
    document.getElementById('rwPeInboundEmpty').style.display = list.length ? 'none' : '';
    tbody.innerHTML = list.map((ib, i) => `
      <tr>
        <td><code>${esc(ib.tag)}</code></td><td>${esc(ib.protocol)}</td><td>${esc(ib.port)}</td>
        <td class="text-muted2">${esc(inboundSummary(ib))}</td>
        <td><div class="rw-pe-row-actions">
          <button type="button" class="rw-pe-icon-btn" data-in-edit="${i}"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="rw-pe-icon-btn is-danger" data-in-del="${i}"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        </div></td>
      </tr>`).join('');
    refreshIcons();
  }
  function renderOutbounds() {
    const tbody = document.getElementById('rwPeOutboundTbody');
    const list = config.outbounds || [];
    document.getElementById('rwPeOutboundEmpty').style.display = list.length ? 'none' : '';
    tbody.innerHTML = list.map((ob, i) => `
      <tr>
        <td><code>${esc(ob.tag)}</code></td><td>${esc(ob.protocol)}</td>
        <td class="text-muted2">${esc(outboundSummary(ob))}</td>
        <td><div class="rw-pe-row-actions">
          <button type="button" class="rw-pe-icon-btn" data-out-edit="${i}"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="rw-pe-icon-btn is-danger" data-out-del="${i}"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        </div></td>
      </tr>`).join('');
    refreshIcons();
  }
  function renderRules() {
    const tbody = document.getElementById('rwPeRuleTbody');
    const list = config.routing?.rules || [];
    document.getElementById('rwPeRuleEmpty').style.display = list.length ? 'none' : '';
    tbody.innerHTML = list.map((rule, i) => `
      <tr class="rw-pe-rule-row" data-rule-row="${i}">
        <td class="rw-pe-drag-cell">
          <span class="rw-pe-drag-handle" data-rule-drag draggable="true" title="Перетащите для изменения порядка">
            <i data-lucide="grip-vertical" class="w-3.5 h-3.5"></i>
          </span>
        </td>
        <td>${i + 1}</td><td>${esc(rule.type || 'field')}</td>
        <td><code>${esc(ruleInboundLabel(rule))}</code></td>
        <td class="text-muted2">${esc(ruleMatchSummary(rule))}</td><td><code>${esc(rule.outboundTag)}</code></td>
        <td><div class="rw-pe-row-actions">
          <button type="button" class="rw-pe-icon-btn" data-rule-up="${i}" ${i === 0 ? 'disabled' : ''}><i data-lucide="chevron-up" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="rw-pe-icon-btn" data-rule-down="${i}" ${i >= list.length - 1 ? 'disabled' : ''}><i data-lucide="chevron-down" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="rw-pe-icon-btn" data-rule-edit="${i}"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="rw-pe-icon-btn is-danger" data-rule-del="${i}"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        </div></td>
      </tr>`).join('');
    refreshIcons();
  }
  function renderAll() { syncGeneralFromConfig(); renderInbounds(); renderOutbounds(); renderRules(); updateJsonEditor(); }
  function refreshIcons() { if (window.lucide) lucide.createIcons(); }
  function moveRule(oldIdx, newIdx) {
    const r = config.routing.rules;
    if (newIdx < 0 || newIdx >= r.length) return;
    r.splice(newIdx, 0, r.splice(oldIdx, 1)[0]);
    renderAll();
  }

  let ruleDragIndex = null;

  function bindRuleDragDrop() {
    const tbody = document.getElementById('rwPeRuleTbody');
    if (!tbody || tbody.dataset.dndBound) return;
    tbody.dataset.dndBound = '1';

    tbody.addEventListener('dragstart', (e) => {
      if (!e.target.closest('[data-rule-drag]')) return;
      const row = e.target.closest('tr[data-rule-row]');
      if (!row) return;
      ruleDragIndex = +row.dataset.ruleRow;
      row.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(ruleDragIndex));
    });

    tbody.addEventListener('dragend', (e) => {
      const row = e.target.closest('tr[data-rule-row]');
      if (row) row.classList.remove('is-dragging');
      ruleDragIndex = null;
      tbody.querySelectorAll('.is-drop-target').forEach((el) => el.classList.remove('is-drop-target'));
    });

    tbody.addEventListener('dragover', (e) => {
      if (ruleDragIndex === null) return;
      const row = e.target.closest('tr[data-rule-row]');
      if (!row) return;
      e.preventDefault();
      tbody.querySelectorAll('.is-drop-target').forEach((el) => el.classList.remove('is-drop-target'));
      row.classList.add('is-drop-target');
    });

    tbody.addEventListener('drop', (e) => {
      e.preventDefault();
      const row = e.target.closest('tr[data-rule-row]');
      if (!row || ruleDragIndex === null) return;
      const toIdx = +row.dataset.ruleRow;
      row.classList.remove('is-drop-target');
      if (toIdx !== ruleDragIndex) moveRule(ruleDragIndex, toIdx);
      ruleDragIndex = null;
    });
  }

  /* ── modal ── */
  function openItemModal(title, kind, index, draft) {
    modalCtx = { kind, index, draft };
    document.getElementById('rwPeModalTitle').textContent = title;
    document.getElementById('rwPeModal').classList.add('is-open');
    renderModalBody();
  }
  function renderModalBody() {
    if (!modalCtx) return;
    let html = '';
    if (modalCtx.kind === 'inbound') { html = inboundFormHtml(normalizeInboundDraft(modalCtx.draft)); }
    else if (modalCtx.kind === 'outbound') { html = outboundFormHtml(normalizeOutboundDraft(modalCtx.draft)); }
    else { html = ruleFormHtml(modalCtx.draft); }
    document.getElementById('rwPeModalBody').innerHTML = html;
    refreshIcons();
    bindModalDynamic();
  }
  function closeModal() { document.getElementById('rwPeModal').classList.remove('is-open'); modalCtx = null; }

  function bindModalDynamic() {
    if (!modalCtx) return;
    if (modalCtx.kind === 'inbound') {
      ['rwPeMProtocol', 'rwPeMNetwork', 'rwPeMSecurity'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', () => { collectInbound(modalCtx.draft); renderModalBody(); });
      });
      document.getElementById('rwPeMGenRealityKeys')?.addEventListener('click', genRealityKeys);
      document.getElementById('rwPeMGenShortIds')?.addEventListener('click', () => {
        const ta = document.getElementById('rwPeMRealityShort'); if (ta) ta.value = randomShortIds().join('\n');
      });
      document.getElementById('rwPeMReRandTarget')?.addEventListener('click', () => {
        const host = REALITY_TARGETS[Math.floor(Math.random() * REALITY_TARGETS.length)];
        const t = document.getElementById('rwPeMRealityTarget'); if (t) t.value = host + ':443';
        const sni = document.getElementById('rwPeMRealitySni'); if (sni && !sni.value.trim()) sni.value = host;
      });
      document.querySelectorAll('#rwPeMTlsAlpn [data-alpn]').forEach(cb => {
        cb.addEventListener('change', () => cb.closest('.rw-pe-chip')?.classList.toggle('is-active', cb.checked));
      });
      const certs = document.getElementById('rwPeMCerts');
      if (certs) {
        certs.addEventListener('click', e => {
          const add = e.target.closest('[data-cert-add]');
          const del = e.target.closest('[data-cert-del]');
          if (add) { collectInbound(modalCtx.draft); ensureTls(modalCtx.draft).certificates.push({ certificateFile: '', keyFile: '', usage: 'encipherment', ocspStapling: 3600 }); renderModalBody(); }
          if (del) { collectInbound(modalCtx.draft); ensureTls(modalCtx.draft).certificates.splice(+del.dataset.certDel, 1); renderModalBody(); }
        });
        certs.addEventListener('change', e => {
          if (e.target.matches('[data-cert-usage]')) { collectInbound(modalCtx.draft); renderModalBody(); }
        });
      }
    }
    if (modalCtx.kind === 'outbound') {
      ['rwPeMProtocol', 'rwPeMOutNetwork', 'rwPeMOutSecurity'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', () => { collectOutbound(modalCtx.draft); renderModalBody(); });
      });
    }
  }

  function ensureTls(d) {
    d.streamSettings = d.streamSettings || {};
    d.streamSettings.tlsSettings = d.streamSettings.tlsSettings || { certificates: [] };
    if (!Array.isArray(d.streamSettings.tlsSettings.certificates)) d.streamSettings.tlsSettings.certificates = [];
    return d.streamSettings.tlsSettings;
  }

  async function genRealityKeys(e) {
    const btn = e.currentTarget; btn.disabled = true;
    try {
      const resp = await fetch(`${cleanPath}/api/remnawave/profiles/x25519`, { method: 'POST', credentials: 'same-origin' });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || 'Ошибка');
      if (document.getElementById('rwPeMRealityPrivate')) document.getElementById('rwPeMRealityPrivate').value = data.privateKey;
      if (document.getElementById('rwPeMRealityPublic')) document.getElementById('rwPeMRealityPublic').value = data.publicKey || '';
    } catch (err) { alert(err.message || String(err)); }
    finally { btn.disabled = false; }
  }

  /* ── inbound form ── */
  function inboundProtocolBlock(p, s) {
    if (p === 'vless') return `<div class="rw-pe-field"><label>decryption</label><input id="rwPeMDecryption" value="${esc(s.decryption || 'none')}" placeholder="none"></div>`;
    if (p === 'trojan') return `<p class="text-[12px] text-muted2">Клиенты Trojan управляются Remnawave (settings.clients).</p>`;
    if (p === 'shadowsocks') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>method</label><select id="rwPeMSSMethod">${SS_METHODS.map(m => `<option value="${m}"${s.method === m ? ' selected' : ''}>${m}</option>`).join('')}</select></div>
      <div class="rw-pe-field"><label>password</label><input id="rwPeMSSPassword" value="${esc(s.password || '')}"></div>
      <div class="rw-pe-field"><label>network</label><input id="rwPeMSSNetwork" value="${esc(s.network || 'tcp,udp')}"></div></div>`;
    if (p === 'hysteria2') {
      const obfs = s.obfs || {};
      return `
        <p class="text-[12px] text-muted2 mb-2">Клиенты Hysteria2 управляются Remnawave (<code>settings.clients</code>).</p>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>up (↑ скорость)</label><input id="rwPeMHyUp" value="${esc(s.up || '')}" placeholder="100 Mbps"></div>
          <div class="rw-pe-field"><label>down (↓ скорость)</label><input id="rwPeMHyDown" value="${esc(s.down || '')}" placeholder="100 Mbps"></div>
        </div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>obfs type</label>
            <select id="rwPeMHyObfsType">
              <option value=""${!obfs.type ? ' selected' : ''}>— нет —</option>
              <option value="salamander"${obfs.type === 'salamander' ? ' selected' : ''}>salamander</option>
            </select></div>
          <div class="rw-pe-field"><label>obfs password</label><input id="rwPeMHyObfsPass" value="${esc(obfs.password || '')}"></div>
        </div>
        <label class="flex items-center gap-2 text-[12px]"><input type="checkbox" id="rwPeMHyIgnoreBw" ${s.ignoreClientBandwidth ? 'checked' : ''}> ignoreClientBandwidth</label>`;
    }
    return '';
  }

  function inboundTransportBlock(net, t) {
    if (net === 'ws') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>host</label><input id="rwPeMTrHost" value="${esc(t.host || '')}"></div>
      <div class="rw-pe-field"><label>path</label><input id="rwPeMTrPath" value="${esc(t.path || '/')}"></div></div>`;
    if (net === 'xhttp') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>host</label><input id="rwPeMTrHost" value="${esc(t.host || '')}"></div>
      <div class="rw-pe-field"><label>path</label><input id="rwPeMTrPath" value="${esc(t.path || '/')}"></div>
      <div class="rw-pe-field"><label>mode</label><select id="rwPeMTrMode">${['auto', 'packet-up', 'stream-up', 'stream-one'].map(m => `<option value="${m}"${(t.mode || 'auto') === m ? ' selected' : ''}>${m}</option>`).join('')}</select></div></div>`;
    if (net === 'grpc') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>serviceName</label><input id="rwPeMTrSvc" value="${esc(t.serviceName || '')}"></div>
      <div class="rw-pe-field"><label>authority</label><input id="rwPeMTrAuth" value="${esc(t.authority || '')}"></div>
      <div class="rw-pe-field"><label>multiMode</label><label class="flex items-center gap-2 text-[12px] mt-1"><input type="checkbox" id="rwPeMTrMulti" ${t.multiMode ? 'checked' : ''}> включить</label></div></div>`;
    if (net === 'raw') return `<div class="rw-pe-field"><label>header type</label><select id="rwPeMTrHdr">${['none', 'http'].map(h => `<option value="${h}"${(t.header?.type || 'none') === h ? ' selected' : ''}>${h}</option>`).join('')}</select></div>`;
    return '';
  }

  /* ── option/select helpers ── */
  function opts(arr, cur, autoLabel) {
    let h = autoLabel ? `<option value=""${!cur ? ' selected' : ''}>${autoLabel}</option>` : '';
    h += arr.map(x => `<option value="${esc(x)}"${cur === x ? ' selected' : ''}>${esc(x)}</option>`).join('');
    return h;
  }
  const REALITY_TARGETS = ['www.microsoft.com', 'www.amazon.com', 'www.cloudflare.com', 'www.apple.com', 'dl.google.com', 'www.nvidia.com'];

  /* ── TLS rich form (по образцу 3X-UI) ── */
  function hasInlineCert(c) {
    const cert = c?.certificate;
    const key = c?.key;
    const hasCert = Array.isArray(cert) ? cert.some(Boolean) : !!cert;
    const hasKey = Array.isArray(key) ? key.some(Boolean) : !!key;
    return (hasCert || hasKey) && !c.certificateFile && !c.keyFile;
  }

  function certBlockHtml(c, i, total) {
    return `
      <div class="rw-pe-subcard" data-cert="${i}">
        <div class="flex items-center justify-between gap-2 mb-2">
          <span class="text-[11.5px] font-semibold text-muted2">Сертификат #${i + 1}</span>
          <div class="flex gap-1">
            <button type="button" class="rw-pe-icon-btn" data-cert-add title="Добавить"><i data-lucide="plus" class="w-3.5 h-3.5"></i></button>
            ${total > 1 ? `<button type="button" class="rw-pe-icon-btn is-danger" data-cert-del="${i}" title="Удалить"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>` : ''}
          </div>
        </div>
        <div class="rw-pe-field"><label>Путь к сертификату (certificateFile)</label><input data-cert-certfile="${i}" value="${esc(c.certificateFile || '')}" placeholder="/etc/ssl/fullchain.pem"></div>
        <div class="rw-pe-field"><label>Путь к ключу (keyFile)</label><input data-cert-keyfile="${i}" value="${esc(c.keyFile || '')}" placeholder="/etc/ssl/privkey.pem"></div>
        ${hasInlineCert(c) ? '<p class="text-[11px] text-amber-600 dark:text-amber-400 mb-0">В JSON был inline-сертификат — укажите пути; содержимое PEM в профиль не сохраняется.</p>' : ''}
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>Usage</label><select data-cert-usage="${i}">${opts(USAGE_OPTS, c.usage || 'encipherment')}</select></div>
          <div class="rw-pe-field"><label>OCSP Stapling (сек)</label><input type="number" data-cert-ocsp="${i}" value="${esc(c.ocspStapling != null ? c.ocspStapling : 3600)}"></div>
        </div>
        <div class="flex flex-wrap gap-4 mt-1">
          <label class="flex items-center gap-2 text-[12px]"><input type="checkbox" data-cert-otl="${i}" ${c.oneTimeLoading ? 'checked' : ''}> Однократная загрузка</label>
          ${(c.usage || 'encipherment') === 'issue' ? `<label class="flex items-center gap-2 text-[12px]"><input type="checkbox" data-cert-build="${i}" ${c.buildChain ? 'checked' : ''}> Build Chain</label>` : ''}
        </div>
      </div>`;
  }

  function tlsFormHtml(tls, formOpts = {}) {
    const s = tls.settings || {};
    const alpnDefault = formOpts.hysteria ? ['h3'] : ['h2', 'http/1.1'];
    const alpn = (tls.alpn && tls.alpn.length) ? tls.alpn : alpnDefault;
    const certs = (tls.certificates && tls.certificates.length) ? tls.certificates : [{ certificateFile: '', keyFile: '', usage: 'encipherment' }];
    const peer = tls.pinnedPeerCertificateChainSha256 || [];
    return `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="lock" class="w-3.5 h-3.5"></i> TLS</div>
        <div class="rw-pe-field"><label>SNI (serverName)</label><input id="rwPeMTlsSni" value="${esc(tls.serverName || '')}"></div>
        <div class="rw-pe-field"><label>Cipher Suites</label><select id="rwPeMTlsCipher">${opts(TLS_CIPHERS, tls.cipherSuites || '', 'Авто')}</select></div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>Min версия</label><select id="rwPeMTlsMin">${opts(TLS_VERSIONS, tls.minVersion || '1.2')}</select></div>
          <div class="rw-pe-field"><label>Max версия</label><select id="rwPeMTlsMax">${opts(TLS_VERSIONS, tls.maxVersion || '1.3')}</select></div>
        </div>
        <div class="rw-pe-field"><label>uTLS (fingerprint)</label><select id="rwPeMTlsFp">${opts(UTLS_FP, s.fingerprint || 'chrome', 'None')}</select></div>
        <div class="rw-pe-field"><label>ALPN</label>
          <div class="rw-pe-chip-row" id="rwPeMTlsAlpn">
            ${ALPN_OPTS.map(a => `<label class="rw-pe-chip ${alpn.includes(a) ? 'is-active' : ''}"><input type="checkbox" class="hidden" data-alpn="${a}" ${alpn.includes(a) ? 'checked' : ''}>${a}</label>`).join('')}
          </div>
        </div>
        <div class="flex flex-wrap gap-4 my-2">
          <label class="flex items-center gap-2 text-[12px]"><input type="checkbox" id="rwPeMTlsReject" ${tls.rejectUnknownSni ? 'checked' : ''}> Reject Unknown SNI</label>
          <label class="flex items-center gap-2 text-[12px]"><input type="checkbox" id="rwPeMTlsDisableRoot" ${tls.disableSystemRoot ? 'checked' : ''}> Disable System Root</label>
          <label class="flex items-center gap-2 text-[12px]"><input type="checkbox" id="rwPeMTlsResume" ${tls.enableSessionResumption ? 'checked' : ''}> Session Resumption</label>
        </div>
        <div id="rwPeMCerts">${certs.map((c, i) => certBlockHtml(c, i, certs.length)).join('')}</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>ECH key (echServerKeys)</label><input id="rwPeMTlsEchKeys" value="${esc(tls.echServerKeys || '')}"></div>
          <div class="rw-pe-field"><label>ECH config (echConfigList)</label><input id="rwPeMTlsEchConfig" value="${esc(s.echConfigList || '')}"></div>
        </div>
        <div class="rw-pe-field"><label>SHA-256 сертификата пира (через строку)</label><textarea id="rwPeMTlsPeerSha" rows="2">${esc(arrToLines(peer))}</textarea></div>
      </div>`;
  }

  function realityFormHtml(rs) {
    return `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="shield" class="w-3.5 h-3.5"></i> Reality</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>uTLS (fingerprint)</label><select id="rwPeMRealityFp">${opts(UTLS_FP, rs.fingerprint || 'firefox')}</select></div>
          <div class="rw-pe-field"><label>Xver</label><input type="number" id="rwPeMReXver" value="${esc(rs.xver != null ? rs.xver : 0)}"></div>
        </div>
        <div class="rw-pe-field"><label>target (dest)
          <button type="button" class="rw-pe-mini-btn" id="rwPeMReRandTarget"><i data-lucide="dices" class="w-3 h-3"></i></button></label>
          <input id="rwPeMRealityTarget" value="${esc(rs.target || '')}" placeholder="www.microsoft.com:443"></div>
        <div class="rw-pe-field"><label>serverNames / SNI (по строке)</label><textarea id="rwPeMRealitySni" rows="2">${esc(arrToLines(rs.serverNames))}</textarea></div>
        <div class="rw-pe-field"><label>shortIds (по строке)
          <button type="button" class="rw-pe-mini-btn" id="rwPeMGenShortIds"><i data-lucide="dices" class="w-3 h-3"></i></button></label>
          <textarea id="rwPeMRealityShort" rows="3">${esc(arrToLines(rs.shortIds))}</textarea></div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>privateKey</label><textarea id="rwPeMRealityPrivate" rows="2">${esc(rs.privateKey || '')}</textarea></div>
          <div class="rw-pe-field"><label>publicKey (для клиента)</label><textarea id="rwPeMRealityPublic" rows="2">${esc(rs.publicKey || '')}</textarea></div>
        </div>
        <div class="rw-pe-field" style="align-items:flex-start;"><label>&nbsp;</label>
          <button type="button" class="tw-btn tw-btn-secondary tw-btn-sm" id="rwPeMGenRealityKeys"><i data-lucide="key-round" class="w-3.5 h-3.5"></i> Сгенерировать пару ключей</button></div>
        <div class="rw-pe-field"><label>spiderX</label><input id="rwPeMReSpiderX" value="${esc(rs.spiderX || '/')}"></div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>Max Time Diff (мс)</label><input type="number" id="rwPeMReMaxDiff" value="${esc(rs.maxTimediff != null ? rs.maxTimediff : 0)}"></div>
          <div class="rw-pe-field"><label>Show</label><label class="flex items-center gap-2 text-[12px] mt-1"><input type="checkbox" id="rwPeMReShow" ${rs.show ? 'checked' : ''}> включить</label></div>
        </div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>Min Client Ver</label><input id="rwPeMReMinVer" value="${esc(rs.minClientVer || '')}" placeholder="25.9.11"></div>
          <div class="rw-pe-field"><label>Max Client Ver</label><input id="rwPeMReMaxVer" value="${esc(rs.maxClientVer || '')}"></div>
        </div>
      </div>`;
  }

  function collectCerts() {
    const blocks = document.querySelectorAll('#rwPeMCerts [data-cert]');
    const out = [];
    blocks.forEach(b => {
      const i = b.getAttribute('data-cert');
      const usage = document.querySelector(`[data-cert-usage="${i}"]`)?.value || 'encipherment';
      const cert = {
        usage,
        ocspStapling: parseInt(document.querySelector(`[data-cert-ocsp="${i}"]`)?.value, 10) || 0,
        oneTimeLoading: !!document.querySelector(`[data-cert-otl="${i}"]`)?.checked,
        certificateFile: document.querySelector(`[data-cert-certfile="${i}"]`)?.value.trim() || '',
        keyFile: document.querySelector(`[data-cert-keyfile="${i}"]`)?.value.trim() || '',
      };
      if (usage === 'issue') cert.buildChain = !!document.querySelector(`[data-cert-build="${i}"]`)?.checked;
      out.push(cert);
    });
    return out;
  }

  function collectTls() {
    const alpn = [];
    document.querySelectorAll('#rwPeMTlsAlpn [data-alpn]').forEach(cb => { if (cb.checked) alpn.push(cb.getAttribute('data-alpn')); });
    return {
      serverName: (val('rwPeMTlsSni') || '').trim(),
      minVersion: val('rwPeMTlsMin') || '1.2',
      maxVersion: val('rwPeMTlsMax') || '1.3',
      cipherSuites: val('rwPeMTlsCipher') || '',
      rejectUnknownSni: !!checked('rwPeMTlsReject'),
      disableSystemRoot: !!checked('rwPeMTlsDisableRoot'),
      enableSessionResumption: !!checked('rwPeMTlsResume'),
      alpn,
      certificates: collectCerts(),
      echServerKeys: (val('rwPeMTlsEchKeys') || '').trim(),
      pinnedPeerCertificateChainSha256: linesToArr(val('rwPeMTlsPeerSha')),
      settings: { fingerprint: val('rwPeMTlsFp') || '', echConfigList: (val('rwPeMTlsEchConfig') || '').trim() },
    };
  }

  function collectReality() {
    return {
      show: !!checked('rwPeMReShow'),
      xver: parseInt(val('rwPeMReXver'), 10) || 0,
      target: (val('rwPeMRealityTarget') || '').trim(),
      serverNames: linesToArr(val('rwPeMRealitySni')),
      privateKey: (val('rwPeMRealityPrivate') || '').trim(),
      publicKey: (val('rwPeMRealityPublic') || '').trim(),
      shortIds: linesToArr(val('rwPeMRealityShort')),
      fingerprint: val('rwPeMRealityFp') || 'firefox',
      spiderX: (val('rwPeMReSpiderX') || '/').trim(),
      maxTimediff: parseInt(val('rwPeMReMaxDiff'), 10) || 0,
      minClientVer: (val('rwPeMReMinVer') || '').trim(),
      maxClientVer: (val('rwPeMReMaxVer') || '').trim(),
    };
  }

  function cleanTls(tls, opts = {}) {
    const pathsOnly = opts.pathsOnly !== false;
    const defaultAlpn = opts.hysteria ? ['h3'] : ['h2', 'http/1.1'];
    const out = {
      serverName: tls.serverName || '',
      minVersion: tls.minVersion || '1.2',
      maxVersion: tls.maxVersion || '1.3',
      alpn: (tls.alpn && tls.alpn.length) ? tls.alpn : defaultAlpn,
      certificates: (tls.certificates || []).map(c => {
        const base = { usage: c.usage || 'encipherment', ocspStapling: c.ocspStapling || 0, oneTimeLoading: !!c.oneTimeLoading };
        if (c.usage === 'issue') base.buildChain = !!c.buildChain;
        if (pathsOnly) {
          base.certificateFile = c.certificateFile || '';
          base.keyFile = c.keyFile || '';
        } else if (c.certificateFile || c.keyFile) {
          base.certificateFile = c.certificateFile || '';
          base.keyFile = c.keyFile || '';
        } else {
          base.certificate = Array.isArray(c.certificate) ? c.certificate : String(c.certificate || '').split('\n');
          base.key = Array.isArray(c.key) ? c.key : String(c.key || '').split('\n');
        }
        return base;
      }).filter(c => !pathsOnly || c.certificateFile || c.keyFile || c.usage === 'issue'),
    };
    if (!out.certificates.length) delete out.certificates;
    if (tls.cipherSuites) out.cipherSuites = tls.cipherSuites;
    if (tls.rejectUnknownSni) out.rejectUnknownSni = true;
    if (tls.disableSystemRoot) out.disableSystemRoot = true;
    if (tls.enableSessionResumption) out.enableSessionResumption = true;
    if (tls.echServerKeys) out.echServerKeys = tls.echServerKeys;
    if (tls.pinnedPeerCertificateChainSha256?.length) out.pinnedPeerCertificateChainSha256 = tls.pinnedPeerCertificateChainSha256;
    const fp = tls.settings?.fingerprint, ech = tls.settings?.echConfigList;
    if (fp || ech) out.settings = {}; if (fp) out.settings.fingerprint = fp; if (ech) out.settings.echConfigList = ech;
    return out;
  }

  function cleanReality(rs) {
    const out = {
      target: rs.target || '',
      serverNames: rs.serverNames || [],
      shortIds: (rs.shortIds && rs.shortIds.length) ? rs.shortIds : [''],
      privateKey: rs.privateKey || '',
      fingerprint: rs.fingerprint || 'firefox',
    };
    if (rs.publicKey) out.publicKey = rs.publicKey;
    if (rs.show) out.show = true;
    if (rs.xver) out.xver = rs.xver;
    if (rs.spiderX && rs.spiderX !== '/') out.spiderX = rs.spiderX;
    if (rs.maxTimediff) out.maxTimediff = rs.maxTimediff;
    if (rs.minClientVer) out.minClientVer = rs.minClientVer;
    if (rs.maxClientVer) out.maxClientVer = rs.maxClientVer;
    return out;
  }

  function inboundFormHtml(d) {
    const ss = d.streamSettings || {};
    const isHy = d.protocol === 'hysteria2';
    const net = isHy ? 'udp' : (ss.network || 'raw');
    const sec = isHy ? (ss.security || 'tls') : (ss.security || 'none');
    const rs = ss.realitySettings || {};
    const tls = ss.tlsSettings || {};
    const t = readTransport(ss, net);

    return `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="settings-2" class="w-3.5 h-3.5"></i> Основное</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>tag</label><input id="rwPeMTag" value="${esc(d.tag)}"></div>
          <div class="rw-pe-field"><label>port</label><input id="rwPeMPort" type="number" value="${esc(d.port)}"></div>
          <div class="rw-pe-field"><label>listen</label><input id="rwPeMListen" value="${esc(d.listen || '0.0.0.0')}"></div>
          <div class="rw-pe-field"><label>protocol</label>
            <select id="rwPeMProtocol">${INBOUND_PROTOCOLS.map(p => `<option value="${p}"${d.protocol === p ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        </div>
      </div>
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="sliders-horizontal" class="w-3.5 h-3.5"></i> Протокол <span class="rw-pe-card-tag">${esc(d.protocol)}</span></div>
        ${inboundProtocolBlock(d.protocol, d.settings || {})}
      </div>
      ${isHy ? `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="radio" class="w-3.5 h-3.5"></i> Транспорт</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>network</label><input value="udp" disabled></div>
          <div class="rw-pe-field"><label>security</label>
            <select id="rwPeMSecurity">${['none', 'tls'].map(x => `<option value="${x}"${sec === x ? ' selected' : ''}>${x}</option>`).join('')}</select></div>
        </div>
      </div>
      ${sec === 'tls' ? tlsFormHtml(tls, { hysteria: true }) : ''}` : `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="radio" class="w-3.5 h-3.5"></i> Транспорт</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>network</label>
            <select id="rwPeMNetwork">${NETWORKS.map(n => `<option value="${n.value}"${net === n.value ? ' selected' : ''}>${n.label}</option>`).join('')}</select></div>
          <div class="rw-pe-field"><label>security</label>
            <select id="rwPeMSecurity">${SECURITIES.map(x => `<option value="${x}"${sec === x ? ' selected' : ''}>${x}</option>`).join('')}</select></div>
        </div>
        ${inboundTransportBlock(net, t)}
      </div>
      ${sec === 'tls' ? tlsFormHtml(tls) : ''}
      ${sec === 'reality' ? realityFormHtml(rs) : ''}`}
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="search" class="w-3.5 h-3.5"></i> Sniffing</div>
        <label class="flex items-center gap-2 text-[12px] mb-2"><input type="checkbox" id="rwPeMSniffEnabled" ${d.sniffing?.enabled !== false ? 'checked' : ''}> enabled</label>
        <div class="rw-pe-field"><label>destOverride (по строке)</label><textarea id="rwPeMSniffDest" rows="2">${esc(arrToLines(d.sniffing?.destOverride || ['http', 'tls', 'quic']))}</textarea></div>
      </div>`;
  }

  function collectInbound(d) {
    if (val('rwPeMTag') !== undefined) d.tag = val('rwPeMTag').trim();
    if (val('rwPeMPort') !== undefined) d.port = parseInt(val('rwPeMPort'), 10) || d.port || 443;
    if (val('rwPeMListen') !== undefined) d.listen = val('rwPeMListen').trim() || '0.0.0.0';
    if (val('rwPeMProtocol') !== undefined) d.protocol = val('rwPeMProtocol');
    d.settings = d.settings || {};

    if (d.protocol === 'vless') {
      if (val('rwPeMDecryption') !== undefined) d.settings.decryption = val('rwPeMDecryption').trim() || 'none';
    } else if (d.protocol === 'shadowsocks') {
      if (val('rwPeMSSMethod') !== undefined) d.settings.method = val('rwPeMSSMethod');
      if (val('rwPeMSSPassword') !== undefined) d.settings.password = val('rwPeMSSPassword');
      if (val('rwPeMSSNetwork') !== undefined) d.settings.network = val('rwPeMSSNetwork').trim() || 'tcp,udp';
    } else if (d.protocol === 'hysteria2') {
      if (val('rwPeMHyUp') !== undefined) {
        const up = val('rwPeMHyUp').trim();
        if (up) d.settings.up = up; else delete d.settings.up;
      }
      if (val('rwPeMHyDown') !== undefined) {
        const down = val('rwPeMHyDown').trim();
        if (down) d.settings.down = down; else delete d.settings.down;
      }
      if (checked('rwPeMHyIgnoreBw') !== undefined) {
        if (checked('rwPeMHyIgnoreBw')) d.settings.ignoreClientBandwidth = true;
        else delete d.settings.ignoreClientBandwidth;
      }
      const obfsType = val('rwPeMHyObfsType');
      const obfsPass = (val('rwPeMHyObfsPass') || '').trim();
      if (obfsType) d.settings.obfs = { type: obfsType, password: obfsPass };
      else delete d.settings.obfs;
      const ss = d.streamSettings = d.streamSettings || { network: 'udp', security: 'tls' };
      ss.network = 'udp';
      if (val('rwPeMSecurity') !== undefined) ss.security = val('rwPeMSecurity') || 'tls';
      if (ss.security === 'tls' && document.getElementById('rwPeMTlsSni')) ss.tlsSettings = collectTls();
      else delete ss.tlsSettings;
    }

    if (d.protocol !== 'hysteria2') {
      const ss = d.streamSettings = d.streamSettings || {};
      if (val('rwPeMNetwork') !== undefined) ss.network = val('rwPeMNetwork');
      if (val('rwPeMSecurity') !== undefined) ss.security = val('rwPeMSecurity');
      const net = ss.network || 'raw';
      const key = networkKey(net);
      if (net === 'ws' || net === 'xhttp') {
        ss[key] = ss[key] || {};
        if (val('rwPeMTrHost') !== undefined) ss[key].host = val('rwPeMTrHost').trim();
        if (val('rwPeMTrPath') !== undefined) ss[key].path = val('rwPeMTrPath').trim();
        if (net === 'xhttp' && val('rwPeMTrMode') !== undefined) ss[key].mode = val('rwPeMTrMode');
      } else if (net === 'grpc') {
        ss[key] = ss[key] || {};
        if (val('rwPeMTrSvc') !== undefined) ss[key].serviceName = val('rwPeMTrSvc').trim();
        if (val('rwPeMTrAuth') !== undefined) ss[key].authority = val('rwPeMTrAuth').trim();
        if (checked('rwPeMTrMulti') !== undefined) ss[key].multiMode = checked('rwPeMTrMulti');
      } else if (net === 'raw') {
        if (val('rwPeMTrHdr') !== undefined) { ss.rawSettings = { header: { type: val('rwPeMTrHdr') } }; }
      }
      if (ss.security === 'reality' && document.getElementById('rwPeMRealityTarget')) {
        ss.realitySettings = collectReality();
      } else if (ss.security === 'tls' && document.getElementById('rwPeMTlsSni')) {
        ss.tlsSettings = collectTls();
      }
    }

    if (checked('rwPeMSniffEnabled') !== undefined) {
      d.sniffing = { enabled: checked('rwPeMSniffEnabled'), destOverride: linesToArr(val('rwPeMSniffDest')) };
    }
    return d;
  }

  /* ── outbound form ── */
  function outboundProtocolBlock(d) {
    const p = d.protocol, s = d.settings || {};
    if (p === 'freedom' || p === 'blackhole') return '<p class="text-[12px] text-muted2">Без settings.</p>';
    if (p === 'vless') { const vn = s.vnext?.[0] || {}, u = vn.users?.[0] || {};
      return `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>address</label><input id="rwPeMAddr" value="${esc(vn.address || '')}"></div>
        <div class="rw-pe-field"><label>port</label><input id="rwPeMPort" type="number" value="${esc(vn.port || 443)}"></div>
        <div class="rw-pe-field"><label>uuid</label><input id="rwPeMUser" value="${esc(u.id || '')}"></div>
        <div class="rw-pe-field"><label>flow</label><input id="rwPeMFlow" value="${esc(u.flow || '')}" placeholder="xtls-rprx-vision"></div></div>`; }
    if (p === 'trojan') { const srv = s.servers?.[0] || {};
      return `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>address</label><input id="rwPeMAddr" value="${esc(srv.address || '')}"></div>
        <div class="rw-pe-field"><label>port</label><input id="rwPeMPort" type="number" value="${esc(srv.port || 443)}"></div>
        <div class="rw-pe-field"><label>password</label><input id="rwPeMUser" value="${esc(srv.password || '')}"></div></div>`; }
    if (p === 'shadowsocks') { const srv = s.servers?.[0] || {};
      return `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>address</label><input id="rwPeMAddr" value="${esc(srv.address || '')}"></div>
        <div class="rw-pe-field"><label>port</label><input id="rwPeMPort" type="number" value="${esc(srv.port || 443)}"></div>
        <div class="rw-pe-field"><label>method</label><select id="rwPeMSSMethod">${SS_METHODS.map(m => `<option value="${m}"${srv.method === m ? ' selected' : ''}>${m}</option>`).join('')}</select></div>
        <div class="rw-pe-field"><label>password</label><input id="rwPeMUser" value="${esc(srv.password || '')}"></div></div>`; }
    if (p === 'hysteria2') { const srv = s.servers?.[0] || {};
      return `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>address</label><input id="rwPeMAddr" value="${esc(srv.address || '')}"></div>
        <div class="rw-pe-field"><label>port</label><input id="rwPeMPort" type="number" value="${esc(srv.port || 443)}"></div>
        <div class="rw-pe-field"><label>password</label><input id="rwPeMUser" value="${esc(srv.password || '')}"></div></div>`; }
    return '';
  }
  function outboundTransportBlock(net, t) {
    if (net === 'ws') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>host</label><input id="rwPeMOutTrHost" value="${esc(t.host || '')}"></div>
      <div class="rw-pe-field"><label>path</label><input id="rwPeMOutTrPath" value="${esc(t.path || '/')}"></div></div>`;
    if (net === 'xhttp') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>host</label><input id="rwPeMOutTrHost" value="${esc(t.host || '')}"></div>
      <div class="rw-pe-field"><label>path</label><input id="rwPeMOutTrPath" value="${esc(t.path || '/')}"></div>
      <div class="rw-pe-field"><label>mode</label><select id="rwPeMOutTrMode">${['auto', 'packet-up', 'stream-up', 'stream-one'].map(m => `<option value="${m}"${(t.mode || 'auto') === m ? ' selected' : ''}>${m}</option>`).join('')}</select></div></div>`;
    if (net === 'grpc') return `<div class="rw-pe-field-grid">
      <div class="rw-pe-field"><label>serviceName</label><input id="rwPeMOutTrSvc" value="${esc(t.serviceName || '')}"></div>
      <div class="rw-pe-field"><label>authority</label><input id="rwPeMOutTrAuth" value="${esc(t.authority || '')}"></div>
      <div class="rw-pe-field"><label>multiMode</label><label class="flex items-center gap-2 text-[12px] mt-1"><input type="checkbox" id="rwPeMOutTrMulti" ${t.multiMode ? 'checked' : ''}> включить</label></div></div>`;
    if (net === 'raw') return `<div class="rw-pe-field"><label>header type</label><select id="rwPeMOutTrHdr">${['none', 'http'].map(h => `<option value="${h}"${(t.header?.type || 'none') === h ? ' selected' : ''}>${h}</option>`).join('')}</select></div>`;
    return '';
  }

  function outboundStreamBlock(d) {
    const ss = d.streamSettings || {}, net = ss.network || (d.protocol === 'hysteria2' ? 'udp' : 'raw');
    const sec = ss.security || (d.protocol === 'hysteria2' ? 'tls' : 'none');
    const rs = ss.realitySettings || {}, tls = ss.tlsSettings || {};
    const t = readTransport(ss, net);
    const isHy = d.protocol === 'hysteria2';
    const nets = isHy ? [{ value: 'udp', label: 'UDP (Hysteria2)' }] : NETWORKS;
    const secs = isHy ? ['none', 'tls'] : SECURITIES;
    return `
      <div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>network</label><select id="rwPeMOutNetwork">${nets.map(n => `<option value="${n.value}"${net === n.value ? ' selected' : ''}>${n.label}</option>`).join('')}</select></div>
        <div class="rw-pe-field"><label>security</label><select id="rwPeMOutSecurity">${secs.map(x => `<option value="${x}"${sec === x ? ' selected' : ''}>${x}</option>`).join('')}</select></div>
      </div>
      ${!isHy ? outboundTransportBlock(net, t) : ''}
      ${sec === 'tls' ? `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>serverName (SNI)</label><input id="rwPeMOutTlsSni" value="${esc(tls.serverName || '')}"></div>
        <div class="rw-pe-field"><label>fingerprint</label><select id="rwPeMOutTlsFp">${opts(UTLS_FP, tls.fingerprint || tls.settings?.fingerprint || '', 'None')}</select></div>
        <div class="rw-pe-field"><label>ALPN</label><input id="rwPeMOutTlsAlpn" value="${esc((tls.alpn || (isHy ? ['h3'] : [])).join(','))}" placeholder="${isHy ? 'h3' : 'h2,http/1.1'}"></div>
        <div class="rw-pe-field"><label>allowInsecure</label><label class="flex items-center gap-2 text-[12px] mt-1"><input type="checkbox" id="rwPeMOutTlsInsecure" ${tls.allowInsecure ? 'checked' : ''}> включить</label></div>
      </div>` : ''}
      ${sec === 'reality' ? `<div class="rw-pe-field-grid">
        <div class="rw-pe-field"><label>serverName</label><input id="rwPeMOutReSni" value="${esc(rs.serverName || '')}"></div>
        <div class="rw-pe-field"><label>publicKey</label><input id="rwPeMOutRePbk" value="${esc(rs.publicKey || '')}"></div>
        <div class="rw-pe-field"><label>shortId</label><input id="rwPeMOutReSid" value="${esc(rs.shortId || '')}"></div>
        <div class="rw-pe-field"><label>fingerprint</label><select id="rwPeMOutReFp">${UTLS_FP.map(f => `<option value="${f}"${(rs.fingerprint || 'firefox') === f ? ' selected' : ''}>${f}</option>`).join('')}</select></div>
      </div>` : ''}`;
  }
  function outboundFormHtml(d) {
    const isProxy = !['freedom', 'blackhole'].includes(d.protocol);
    return `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="settings-2" class="w-3.5 h-3.5"></i> Основное</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>tag</label><input id="rwPeMTag" value="${esc(d.tag)}"></div>
          <div class="rw-pe-field"><label>protocol</label><select id="rwPeMProtocol">${OUTBOUND_PROTOCOLS.map(p => `<option value="${p}"${d.protocol === p ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        </div>
      </div>
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="sliders-horizontal" class="w-3.5 h-3.5"></i> Настройки <span class="rw-pe-card-tag">${esc(d.protocol)}</span></div>
        ${outboundProtocolBlock(d)}
      </div>
      ${isProxy ? `<div class="rw-pe-card"><div class="rw-pe-card-title"><i data-lucide="radio" class="w-3.5 h-3.5"></i> Stream</div>${outboundStreamBlock(d)}</div>` : ''}`;
  }
  function collectOutbound(d) {
    if (val('rwPeMTag') !== undefined) d.tag = val('rwPeMTag').trim();
    if (val('rwPeMProtocol') !== undefined) d.protocol = val('rwPeMProtocol');
    if (d.protocol === 'freedom' || d.protocol === 'blackhole') { delete d.settings; delete d.streamSettings; return d; }
    d.settings = d.settings || {};
    if (val('rwPeMAddr') !== undefined) {
      const addr = val('rwPeMAddr').trim(), port = parseInt(val('rwPeMPort'), 10) || 443, user = (val('rwPeMUser') || '').trim();
      if (d.protocol === 'vless') d.settings = { vnext: [{ address: addr, port, users: [{ id: user, encryption: 'none', flow: (val('rwPeMFlow') || '').trim() }] }] };
      else if (d.protocol === 'trojan') d.settings = { servers: [{ address: addr, port, password: user }] };
      else if (d.protocol === 'shadowsocks') d.settings = { servers: [{ address: addr, port, password: user, method: val('rwPeMSSMethod') || 'chacha20-ietf-poly1305' }] };
      else if (d.protocol === 'hysteria2') d.settings = { servers: [{ address: addr, port, password: user }] };
    }
    if (val('rwPeMOutNetwork') !== undefined) {
      const ss = d.streamSettings = d.streamSettings || {};
      ss.network = val('rwPeMOutNetwork') || (d.protocol === 'hysteria2' ? 'udp' : 'raw');
      ss.security = val('rwPeMOutSecurity') || 'none';
      const net = ss.network || 'raw';
      const key = networkKey(net);
      if (net === 'ws' || net === 'xhttp') {
        ss[key] = ss[key] || {};
        if (val('rwPeMOutTrHost') !== undefined) ss[key].host = val('rwPeMOutTrHost').trim();
        if (val('rwPeMOutTrPath') !== undefined) ss[key].path = val('rwPeMOutTrPath').trim();
        if (net === 'xhttp' && val('rwPeMOutTrMode') !== undefined) ss[key].mode = val('rwPeMOutTrMode');
      } else if (net === 'grpc') {
        ss[key] = ss[key] || {};
        if (val('rwPeMOutTrSvc') !== undefined) ss[key].serviceName = val('rwPeMOutTrSvc').trim();
        if (val('rwPeMOutTrAuth') !== undefined) ss[key].authority = val('rwPeMOutTrAuth').trim();
        if (checked('rwPeMOutTrMulti') !== undefined) ss[key].multiMode = checked('rwPeMOutTrMulti');
      } else if (net === 'raw' && val('rwPeMOutTrHdr') !== undefined) {
        ss.rawSettings = { header: { type: val('rwPeMOutTrHdr') } };
      }
      if (ss.security === 'tls' && val('rwPeMOutTlsSni') !== undefined) {
        const alpnRaw = (val('rwPeMOutTlsAlpn') || '').split(',').map(s => s.trim()).filter(Boolean);
        ss.tlsSettings = {
          serverName: val('rwPeMOutTlsSni').trim(),
          allowInsecure: !!checked('rwPeMOutTlsInsecure'),
        };
        const fp = val('rwPeMOutTlsFp');
        if (fp) ss.tlsSettings.fingerprint = fp;
        if (alpnRaw.length) ss.tlsSettings.alpn = alpnRaw;
      } else if (ss.security === 'reality' && val('rwPeMOutReSni') !== undefined) {
        ss.realitySettings = { serverName: val('rwPeMOutReSni').trim(), publicKey: val('rwPeMOutRePbk').trim(), shortId: val('rwPeMOutReSid').trim(), fingerprint: val('rwPeMOutReFp') };
      }
    }
    return d;
  }

  /* ── rule form ── */
  function ruleFormHtml(d) {
    const dom = splitDomainField(d.domain), ip = splitIpField(d.ip);
    const netVal = (d.network || '').toUpperCase();
    const inVal = ruleInboundFormValue(d);
    const inboundOpts = (config.inbounds || []).map(ib => {
      const tag = ib.tag || '';
      return `<option value="${esc(tag)}"${inVal === tag ? ' selected' : ''}>${esc(tag)}</option>`;
    }).join('');
    return `
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="git-fork" class="w-3.5 h-3.5"></i> Маршрут → аутбаунд</div>
        <div class="rw-pe-field"><label>Инбаунд (inboundTag)</label>
          <select id="rwPeMRuleIn">
            <option value=""${!inVal ? ' selected' : ''}>— все инбаунды —</option>
            ${inboundOpts}
          </select>
          <p class="text-[11px] text-muted2 mt-1 mb-0">Пусто — правило для любого входящего подключения.</p>
        </div>
        <div class="rw-pe-field"><label>Аутбаунд (outboundTag)</label>
          <select id="rwPeMRuleOut">${(config.outbounds || []).map(ob => `<option value="${esc(ob.tag)}"${d.outboundTag === ob.tag ? ' selected' : ''}>${esc(ob.tag)}</option>`).join('')}</select></div>
      </div>
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="globe" class="w-3.5 h-3.5"></i> Домены</div>
        <div class="rw-pe-field"><label>geosite / regexp / full (по строке)</label>
          <textarea id="rwPeMRuleGeosite" rows="3" placeholder="geosite:youtube&#10;geosite:google&#10;regexp:.*\\.ru$">${esc(arrToLines(dom.matchers))}</textarea></div>
        <div class="rw-pe-field"><label>Точные домены (по строке)</label>
          <textarea id="rwPeMRuleDomain" rows="2" placeholder="google.com">${esc(arrToLines(dom.plain))}</textarea></div>
      </div>
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="network" class="w-3.5 h-3.5"></i> IP</div>
        <div class="rw-pe-field"><label>geoip (по строке)</label>
          <textarea id="rwPeMRuleGeoip" rows="2" placeholder="geoip:private&#10;geoip:ru">${esc(arrToLines(ip.geoip))}</textarea></div>
        <div class="rw-pe-field"><label>IP / CIDR (по строке)</label>
          <textarea id="rwPeMRuleIp" rows="2" placeholder="10.0.0.0/8">${esc(arrToLines(ip.plain))}</textarea></div>
      </div>
      <div class="rw-pe-card">
        <div class="rw-pe-card-title"><i data-lucide="filter" class="w-3.5 h-3.5"></i> Прочее</div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>protocol</label><textarea id="rwPeMRuleProto" rows="2" placeholder="bittorrent">${esc(arrToLines(d.protocol))}</textarea></div>
          <div class="rw-pe-field"><label>network (L4)</label>
            <select id="rwPeMRuleNet">${['', 'TCP', 'UDP', 'TCP,UDP'].map(n => `<option value="${n}"${netVal === n ? ' selected' : ''}>${n || '— любой —'}</option>`).join('')}</select></div>
        </div>
        <div class="rw-pe-field-grid">
          <div class="rw-pe-field"><label>port</label><input id="rwPeMRulePort" value="${esc(d.port || '')}" placeholder="53,443,1000-2000"></div>
          <div class="rw-pe-field"><label>sourcePort</label><input id="rwPeMRuleSrcPort" value="${esc(d.sourcePort || '')}" placeholder="1024-65535"></div>
        </div>
        <div class="rw-pe-field"><label>source IP / CIDR (по строке)</label>
          <textarea id="rwPeMRuleSource" rows="2" placeholder="10.0.0.0/8">${esc(arrToLines(d.source))}</textarea></div>
      </div>`;
  }
  function collectRule(d) {
    d.type = 'field';
    d.outboundTag = val('rwPeMRuleOut');
    const inTag = (val('rwPeMRuleIn') || '').trim();
    if (inTag) d.inboundTag = inTag; else delete d.inboundTag;
    const domain = [...linesToArr(val('rwPeMRuleGeosite')), ...linesToArr(val('rwPeMRuleDomain'))];
    if (domain.length) d.domain = domain; else delete d.domain;
    const ips = [...linesToArr(val('rwPeMRuleGeoip')), ...linesToArr(val('rwPeMRuleIp'))];
    if (ips.length) d.ip = ips; else delete d.ip;
    const proto = linesToArr(val('rwPeMRuleProto'));
    if (proto.length) d.protocol = proto; else delete d.protocol;
    const net = val('rwPeMRuleNet');
    if (net) d.network = net; else delete d.network;
    const port = (val('rwPeMRulePort') || '').trim();
    if (port) d.port = port; else delete d.port;
    const srcPort = (val('rwPeMRuleSrcPort') || '').trim();
    if (srcPort) d.sourcePort = srcPort; else delete d.sourcePort;
    const source = linesToArr(val('rwPeMRuleSource'));
    if (source.length) d.source = source; else delete d.source;
    return d;
  }

  /* ── edit launchers ── */
  function editInbound(i) { openItemModal('Инбаунд: ' + (config.inbounds[i].tag || ''), 'inbound', i, deepClone(config.inbounds[i])); }
  function editOutbound(i) { openItemModal('Аутбаунд: ' + (config.outbounds[i].tag || ''), 'outbound', i, deepClone(config.outbounds[i])); }
  function editRule(i) { openItemModal('Правило #' + (i + 1), 'rule', i, deepClone(config.routing.rules[i])); }

  function applyModal() {
    if (!modalCtx) return;
    const { kind, index, draft } = modalCtx;
    if (kind === 'inbound') config.inbounds[index] = cleanInbound(collectInbound(draft));
    else if (kind === 'outbound') config.outbounds[index] = cleanOutbound(collectOutbound(draft));
    else config.routing.rules[index] = cleanRule(collectRule(draft));
    closeModal();
    renderAll();
  }

  /* ── link import ── */
  function applyLinkTransport(stream, params) {
    const net = stream.network || 'raw';
    const host = params.get('host') || '';
    const path = params.get('path') || '/';
    if (net === 'ws') stream.wsSettings = { host, path };
    else if (net === 'xhttp') stream.xhttpSettings = { host, path, mode: params.get('mode') || 'auto' };
    else if (net === 'grpc') {
      stream.grpcSettings = {
        serviceName: params.get('serviceName') || params.get('path') || '',
        authority: params.get('authority') || '',
        multiMode: params.get('mode') === 'multi',
      };
    } else if (net === 'raw' && (params.get('headerType') === 'http' || params.get('type') === 'http')) {
      stream.rawSettings = { header: { type: 'http' } };
    }
  }
  function applyLinkTls(stream, params) {
    if (stream.security === 'reality') {
      stream.realitySettings = {
        serverName: params.get('sni') || '',
        publicKey: params.get('pbk') || '',
        shortId: params.get('sid') || '',
        fingerprint: params.get('fp') || 'firefox',
      };
    } else if (stream.security === 'tls') {
      stream.tlsSettings = { serverName: params.get('sni') || '' };
      const fp = params.get('fp');
      if (fp) stream.tlsSettings.fingerprint = fp;
      const alpn = params.get('alpn');
      if (alpn) stream.tlsSettings.alpn = alpn.split(',').map(s => s.trim()).filter(Boolean);
      if (params.get('insecure') === '1') stream.tlsSettings.allowInsecure = true;
    }
  }
  function parseHysteria2OutboundLink(link) {
    let raw = (link || '').trim();
    if (raw.startsWith('hy2://')) raw = 'hysteria2://' + raw.slice(6);
    if (!raw.toLowerCase().startsWith('hysteria2://')) return null;
    raw = raw.slice('hysteria2://'.length);

    let fragment = '';
    const hashIdx = raw.indexOf('#');
    if (hashIdx >= 0) {
      try { fragment = decodeURIComponent(raw.slice(hashIdx + 1)).trim(); } catch (_) { fragment = raw.slice(hashIdx + 1).trim(); }
      raw = raw.slice(0, hashIdx);
    }

    let query = '';
    const qIdx = raw.indexOf('?');
    if (qIdx >= 0) {
      query = raw.slice(qIdx + 1);
      raw = raw.slice(0, qIdx);
    }
    raw = raw.replace(/\/+$/, '');

    const atIdx = raw.lastIndexOf('@');
    if (atIdx < 0) return null;
    let password;
    try { password = decodeURIComponent(raw.slice(0, atIdx)); } catch (_) { password = raw.slice(0, atIdx); }

    const hostPart = raw.slice(atIdx + 1);
    let address, port;
    if (hostPart.startsWith('[')) {
      const m = hostPart.match(/^\[([^\]]+)\]:(\d+)$/);
      if (!m) return null;
      address = m[1];
      port = parseInt(m[2], 10);
    } else {
      const colonIdx = hostPart.lastIndexOf(':');
      if (colonIdx < 0) return null;
      address = hostPart.slice(0, colonIdx);
      port = parseInt(hostPart.slice(colonIdx + 1), 10);
    }
    if (!address || !Number.isFinite(port) || port <= 0) return null;

    const params = new URLSearchParams(query);
    const tagFromFrag = fragment ? fragment.replace(/\s+/g, ' ').trim().slice(0, 40) : '';
    const tag = val('rwPeLinkTag').trim() || tagFromFrag || `HY2-${port}`;
    const tls = {
      serverName: params.get('sni') || address,
      alpn: (params.get('alpn') || 'h3').split(',').map(s => s.trim()).filter(Boolean),
    };
    const fp = params.get('fp') || params.get('fingerprint');
    if (fp) tls.fingerprint = fp;
    if (params.get('insecure') === '1' || params.get('allowInsecure') === '1') tls.allowInsecure = true;

    const settings = { servers: [{ address, port, password }] };
    const obfsType = params.get('obfs');
    const obfsPass = params.get('obfs-password') || params.get('obfsPassword');
    if (obfsType) settings.obfs = { type: obfsType, password: obfsPass || '' };

    const fmRaw = params.get('fm');
    if (fmRaw) {
      try {
        const fm = JSON.parse(decodeURIComponent(fmRaw));
        const qp = fm.quicParams || fm;
        if (qp.up) settings.up = String(qp.up);
        if (qp.down) settings.down = String(qp.down);
      } catch (_) {}
    }

    return cleanOutbound(normalizeOutboundDraft({
      tag,
      protocol: 'hysteria2',
      settings,
      streamSettings: { network: 'udp', security: 'tls', tlsSettings: tls },
    }));
  }
  function parseOutboundLink(link) {
    link = (link || '').trim();
    if (!link) return null;
    if (link.startsWith('hysteria2://') || link.startsWith('hy2://')) {
      return parseHysteria2OutboundLink(link);
    }
    const match = link.match(/^(vless|trojan|ss):\/\/([^@]+)@([^:]+):(\d+)(\?[^#]*)?(#.*)?$/i);
    if (!match) return null;
    const [, scheme, user, address, portStr, query] = match;
    const port = parseInt(portStr, 10);
    const params = new URLSearchParams((query || '').replace(/^\?/, ''));
    const tag = val('rwPeLinkTag').trim() || `${scheme.toUpperCase()}-${port}`;
    if (scheme === 'vless') {
      const stream = { network: normalizeNetwork(params.get('type')), security: params.get('security') || 'none' };
      applyLinkTransport(stream, params);
      applyLinkTls(stream, params);
      return cleanOutbound({
        tag,
        protocol: 'vless',
        settings: { vnext: [{ address, port, users: [{ id: decodeURIComponent(user), encryption: params.get('encryption') || 'none', flow: params.get('flow') || '' }] }] },
        streamSettings: stream,
      });
    }
    if (scheme === 'trojan') {
      const stream = { network: normalizeNetwork(params.get('type') || 'tcp'), security: params.get('security') || 'tls' };
      applyLinkTransport(stream, params);
      applyLinkTls(stream, params);
      return cleanOutbound({
        tag,
        protocol: 'trojan',
        settings: { servers: [{ address, port, password: decodeURIComponent(user) }] },
        streamSettings: stream,
      });
    }
    if (scheme === 'ss') {
      let method = 'chacha20-ietf-poly1305', password = user;
      try { const p = atob(user).split(':'); method = p[0] || method; password = p.slice(1).join(':') || password; } catch (_) {}
      return cleanOutbound({ tag, protocol: 'shadowsocks', settings: { servers: [{ address, port, method, password }] } });
    }
    return null;
  }
  function importLink() {
    const ob = parseOutboundLink(val('rwPeLinkInput').trim());
    if (!ob) { alert('Не удалось разобрать ссылку'); return; }
    ob.tag = uniqueTag(ob.tag, config.outbounds);
    config.outbounds.push(ob);
    document.getElementById('rwPeLinkModal').classList.remove('is-open');
    document.getElementById('rwPeLinkInput').value = '';
    document.getElementById('rwPeLinkTag').value = '';
    renderAll();
  }

  function applyJsonToGui() {
    const errEl = document.getElementById('rwPeJsonError');
    try {
      const parsed = ensureConfigStructure(JSON.parse(cm ? cm.getValue() : document.getElementById('rwPeJsonTextarea').value));
      parsed.inbounds = (parsed.inbounds || []).map(ib => cleanInbound(normalizeInboundDraft(deepClone(ib))));
      parsed.outbounds = (parsed.outbounds || []).map(ob => cleanOutbound(normalizeOutboundDraft(deepClone(ob))));
      if (parsed.routing?.rules) parsed.routing.rules = parsed.routing.rules.map(cleanRule);
      config = parsed;
      errEl.style.display = 'none';
      renderAll();
    } catch (e) { errEl.textContent = 'JSON: ' + e.message; errEl.style.display = ''; }
  }

  function showToast(msg, isError) {
    if (isError) { alert(msg); return; }
    const el = document.createElement('div');
    el.className = 'fixed bottom-4 right-4 z-[300] px-4 py-2 rounded-lg text-[13px] shadow-lg';
    el.style.cssText = 'background:#10b981;color:#fff';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  async function saveProfile() {
    const btn = document.getElementById('rwPeSaveBtn');
    const name = document.getElementById('rwPeName').value.trim();
    if (!name || name.length < 2 || name.length > 30 || !/^[A-Za-z0-9_\s-]+$/.test(name)) {
      alert('Имя: 2–30 символов, латиница, цифры, пробел, _ и -'); return;
    }
    const cleaned = buildConfigJson();
    const body = { name, config: cleaned };
    btn.disabled = true;
    try {
      const url = MODE === 'new' ? `${cleanPath}/api/remnawave/profiles` : `${cleanPath}/api/remnawave/profiles/${PROFILE_UUID}`;
      const resp = await fetch(url, { method: MODE === 'new' ? 'POST' : 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || 'Ошибка сохранения');
      config = cleaned;
      renderAll();
      showToast('Профиль сохранён');
      if (MODE === 'new' && data.profile?.uuid) window.location.href = `${cleanPath}/remnawave/profiles/${data.profile.uuid}/edit`;
    } catch (e) { alert(e.message || String(e)); }
    finally { btn.disabled = false; }
  }

  function bindTabs() {
    document.querySelectorAll('.rw-pe-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const id = tab.dataset.tab;
        document.querySelectorAll('.rw-pe-tab').forEach(t => t.classList.toggle('is-active', t === tab));
        document.querySelectorAll('.rw-pe-pane').forEach(p => p.classList.toggle('is-active', p.dataset.pane === id));
        if (id === 'json') updateJsonEditor();
        if (cm) setTimeout(() => cm.refresh(), 50);
      });
    });
  }

  function bindEvents() {
    document.getElementById('rwPeSaveBtn').addEventListener('click', saveProfile);
    ['rwPeLogLevel', 'rwPeDomainStrategy'].forEach(id => document.getElementById(id)?.addEventListener('change', () => { syncGeneralToConfig(); updateJsonEditor(); }));
    document.getElementById('rwPeDnsEnabled')?.addEventListener('change', e => {
      document.getElementById('rwPeDnsBody').style.display = e.target.checked ? '' : 'none';
      document.getElementById('rwPeDnsOffHint').style.display = e.target.checked ? 'none' : '';
      syncGeneralToConfig(); updateJsonEditor();
    });
    ['rwPeDnsServers', 'rwPeDnsQueryStrategy'].forEach(id => document.getElementById(id)?.addEventListener('input', () => { syncGeneralToConfig(); updateJsonEditor(); }));

    document.querySelectorAll('[data-preset]').forEach(btn => btn.addEventListener('click', () => {
      const ib = INBOUND_PRESETS[btn.dataset.preset]; if (!ib) return;
      const c = deepClone(ib); c.tag = uniqueTag(c.tag, config.inbounds);
      config.inbounds.push(c); renderAll();
    }));
    document.getElementById('rwPeInboundAdd')?.addEventListener('click', () => {
      const c = deepClone(INBOUND_PRESETS.vless_reality); c.tag = uniqueTag('VLESS', config.inbounds);
      config.inbounds.push(c); renderAll(); editInbound(config.inbounds.length - 1);
    });
    document.getElementById('rwPeInboundTbody')?.addEventListener('click', e => {
      const edit = e.target.closest('[data-in-edit]'), del = e.target.closest('[data-in-del]');
      if (edit) editInbound(+edit.dataset.inEdit);
      if (del && confirm('Удалить инбаунд?')) { config.inbounds.splice(+del.dataset.inDel, 1); renderAll(); }
    });

    document.querySelectorAll('[data-out-preset]').forEach(btn => btn.addEventListener('click', () => {
      const ob = OUTBOUND_PRESETS[btn.dataset.outPreset]; if (!ob) return;
      const c = deepClone(ob); c.tag = uniqueTag(c.tag, config.outbounds);
      config.outbounds.push(c); renderAll();
    }));
    document.getElementById('rwPeOutboundAdd')?.addEventListener('click', () => {
      const c = { tag: uniqueTag('OUT', config.outbounds), protocol: 'vless', settings: { vnext: [{ address: '', port: 443, users: [{ id: '', encryption: 'none' }] }] }, streamSettings: { network: 'raw', security: 'none' } };
      config.outbounds.push(c); renderAll(); editOutbound(config.outbounds.length - 1);
    });
    document.getElementById('rwPeOutboundImport')?.addEventListener('click', () => document.getElementById('rwPeLinkModal').classList.add('is-open'));
    document.getElementById('rwPeOutboundTbody')?.addEventListener('click', e => {
      const edit = e.target.closest('[data-out-edit]'), del = e.target.closest('[data-out-del]');
      if (edit) editOutbound(+edit.dataset.outEdit);
      if (del && confirm('Удалить аутбаунд?')) { config.outbounds.splice(+del.dataset.outDel, 1); renderAll(); }
    });

    document.querySelectorAll('[data-rule-preset]').forEach(btn => btn.addEventListener('click', () => {
      const p = RULE_PRESETS[btn.dataset.rulePreset]; if (p) { config.routing.rules.push(deepClone(p)); renderAll(); }
    }));
    document.getElementById('rwPeRuleAdd')?.addEventListener('click', () => {
      config.routing.rules.push({ type: 'field', outboundTag: config.outbounds[0]?.tag || 'DIRECT' });
      renderAll(); editRule(config.routing.rules.length - 1);
    });
    document.getElementById('rwPeRuleTbody')?.addEventListener('click', e => {
      const up = e.target.closest('[data-rule-up]'), down = e.target.closest('[data-rule-down]');
      const edit = e.target.closest('[data-rule-edit]'), del = e.target.closest('[data-rule-del]');
      if (up) moveRule(+up.dataset.ruleUp, +up.dataset.ruleUp - 1);
      if (down) moveRule(+down.dataset.ruleDown, +down.dataset.ruleDown + 1);
      if (edit) editRule(+edit.dataset.ruleEdit);
      if (del && confirm('Удалить правило?')) { config.routing.rules.splice(+del.dataset.ruleDel, 1); renderAll(); }
    });

    document.getElementById('rwPeJsonFromGui')?.addEventListener('click', updateJsonEditor);
    document.getElementById('rwPeJsonToGui')?.addEventListener('click', applyJsonToGui);
    document.getElementById('rwPeJsonFormat')?.addEventListener('click', () => {
      try {
        const f = JSON.stringify(JSON.parse(cm ? cm.getValue() : document.getElementById('rwPeJsonTextarea').value), null, 2);
        if (cm) cm.setValue(f); else document.getElementById('rwPeJsonTextarea').value = f;
      } catch (e) { document.getElementById('rwPeJsonError').textContent = e.message; document.getElementById('rwPeJsonError').style.display = ''; }
    });

    document.getElementById('rwPeModalClose')?.addEventListener('click', closeModal);
    document.getElementById('rwPeModalCancel')?.addEventListener('click', closeModal);
    document.getElementById('rwPeModalSave')?.addEventListener('click', applyModal);
    document.getElementById('rwPeLinkModalClose')?.addEventListener('click', () => document.getElementById('rwPeLinkModal').classList.remove('is-open'));
    document.getElementById('rwPeLinkCancel')?.addEventListener('click', () => document.getElementById('rwPeLinkModal').classList.remove('is-open'));
    document.getElementById('rwPeLinkImport')?.addEventListener('click', importLink);
  }

  function initCodeMirror() {
    const ta = document.getElementById('rwPeJsonTextarea');
    if (!ta || typeof CodeMirror === 'undefined') return;
    cm = CodeMirror.fromTextArea(ta, { mode: { name: 'javascript', json: true }, theme: 'monokai', lineNumbers: true, matchBrackets: true, autoCloseBrackets: true, tabSize: 2 });
    cm.on('change', () => { document.getElementById('rwPeJsonError').style.display = 'none'; });
  }

  function init() { loadInitialConfig(); bindTabs(); bindEvents(); bindRuleDragDrop(); initCodeMirror(); renderAll(); refreshIcons(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
