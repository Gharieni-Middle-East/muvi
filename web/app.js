// muve — frontend logic. Talks to Python via window.pywebview.api.*
// Falls back to a local mock so the UI is previewable in a plain browser too.

const el = (id) => document.getElementById(id);

// --------------------------------------------------------------- API bridge --
const MockApi = (() => {
  const state = {
    playing: false, play_busy: false, volume: 0.70, vibration: 0.27, highpass_hz: 200, cutoff_hz: 200, position: 0,
    speaker_route: "headphones", vibration_mode: "haptics", output_layout: null,
    live_mode: null,
    zone_enabled: { head: true, upper: true, mid: true, legs: true },
    audio_muted: false,
    vibration_muted: false,
    live_input: {
      ready: true,
      mode: "cable",
      capture_name: "CABLE Output",
      message: "Bluetooth ready — play music on the phone, then press Play",
    },
    track: { title: "Weightless", artist: "Marconi Union", album: "Ambient Transmissions", duration: 486, artwork: "assets/artwork.svg" },
    bluetooth: {
      connected_id: null, connecting_id: null, scanning: false,
      discovering: false, pairing_id: null, forgetting: false, error: null, supported: false,
    },
    media: { ok: true, can_seek: true, can_prev: true, can_next: true },
    demo: { available: true, error: null },
    hardware: {
      gigaport_connected: true,
      gigaport_count: 1,
      gigaport_name: "GIGAPORT eX",
      codec_connected: false,
      codec_name: null,
      headphones_kind: null,
      roles_flipped: false,
      can_swap_gigaports: false,
      battery_percent: 72,
      charging: false,
      battery_available: true,
    },
    devices: [],
    nearby_devices: [],
  };
  let last = performance.now();
  const snap = () => {
    const now = performance.now();
    if (state.playing && state.live_mode === "demo") {
      state.position = Math.min(state.track.duration || 30, state.position + (now - last) / 1000);
    } else if (state.media.ok) {
      state.position = Math.min(state.track.duration, state.position + (now - last) / 1000);
    }
    last = now;
    // Fake per-zone shaker levels so the visualization previews in a browser.
    const t = now / 1000;
    state.zone_rms = state.playing
      ? {
          head: 0.10 + 0.09 * Math.abs(Math.sin(t * 1.3)),
          upper: 0.10 + 0.09 * Math.abs(Math.sin(t * 1.7 + 1)),
          mid: 0.10 + 0.09 * Math.abs(Math.sin(t * 2.1 + 2)),
          legs: 0.10 + 0.09 * Math.abs(Math.sin(t * 0.9 + 3)),
          left: state.volume * (0.14 + 0.13 * Math.abs(Math.sin(t * 1.5 + 4))),
          right: state.volume * (0.14 + 0.13 * Math.abs(Math.sin(t * 1.1 + 5))),
        }
      : { head: 0, upper: 0, mid: 0, legs: 0, left: 0, right: 0 };
    return JSON.parse(JSON.stringify(state));
  };
  return {
    get_state: async () => snap(),
    toggle_play: async () => {
      if (state.playing && state.live_mode === "demo") {
        state.playing = false;
        state.live_mode = null;
      } else {
        state.playing = !state.playing;
        state.live_mode = state.playing ? "cable" : null;
      }
      last = performance.now();
      return snap();
    },
    toggle_demo_play: async () => {
      const on = !(state.playing && state.live_mode === "demo");
      state.playing = on;
      state.live_mode = on ? "demo" : null;
      if (on) {
        state.track = {
          title: "Demo", artist: "MUVI test track", album: "",
          duration: 30, artwork: "assets/artwork.svg",
        };
        state.position = 0;
        state.media = { ok: true, can_seek: true, can_prev: true, can_next: true, via: "demo" };
      }
      last = performance.now();
      return snap();
    },
    seek: async (s) => { state.position = Math.max(0, Math.min(state.track.duration, s)); return snap(); },
    previous_track: async () => { state.position = 0; return snap(); },
    next_track: async () => { state.position = 0; if (state.live_mode !== "demo") state.track.title = "Next Track"; return snap(); },
    set_volume: async (v) => {
      state.volume = v;
      state.audio_muted = Number(v) <= 0;
      return snap();
    },
    set_vibration: async (v) => {
      state.vibration = v;
      state.vibration_muted = Number(v) <= 0;
      return snap();
    },
    set_zone_enabled: async (zone, enabled) => {
      const k = String(zone).toLowerCase();
      if (state.zone_enabled[k] !== undefined) state.zone_enabled[k] = Boolean(enabled);
      if (enabled) state.vibration_muted = false;
      return snap();
    },
    set_audio_muted: async (muted) => {
      state.audio_muted = Boolean(muted);
      return snap();
    },
    set_vibration_muted: async (muted) => {
      state.vibration_muted = Boolean(muted);
      return snap();
    },
    set_highpass_hz: async (v) => { state.highpass_hz = v; state.cutoff_hz = v; return snap(); },
    set_speaker_route: async (route) => {
      state.speaker_route = route === "secondary" ? "secondary" : "headphones";
      return snap();
    },
    set_vibration_mode: async (mode) => {
      state.vibration_mode =
        mode === "stereo" ? "stereo" : mode === "zones" ? "zones" : "haptics";
      return snap();
    },
    get_audio_settings: async () => ({
      ok: true,
      backend: "wasapi/wdm-ks",
      asio_enabled: false,
      active: state.playing,
      output_layout: state.output_layout || "single",
      selected_vibration_index: null,
      selected_audio_index: null,
      devices: [],
      warnings: [],
      auto_pick_ok: true,
      auto_pick_error: null,
    }),
    refresh_audio_devices: async () => MockApi.get_audio_settings(),
    set_output_devices: async () => snap(),
    swap_gigaport_roles: async () => {
      state.hardware.roles_flipped = !state.hardware.roles_flipped;
      state.hardware.can_swap_gigaports = true;
      state.hardware.gigaport_count = 2;
      if (state.hardware.roles_flipped) {
        state.hardware.gigaport_name = "GIGAPORT eX (B)";
        state.hardware.codec_name = "GIGAPORT eX (A)";
      } else {
        state.hardware.gigaport_name = "GIGAPORT eX (A)";
        state.hardware.codec_name = "GIGAPORT eX (B)";
      }
      state.hardware.headphones_kind = "gigaport";
      state.hardware.codec_connected = true;
      return snap();
    },
    scan_bluetooth: async () => {
      state.bluetooth.scanning = true;
      state.bluetooth.discovering = true;
      state.nearby_devices = [];
      setTimeout(() => {
        state.bluetooth.scanning = false;
        state.bluetooth.discovering = false;
        state.nearby_devices = [
          { id: "demo-nearby", name: "Demo Phone", profile: "Nearby — tap Pair", paired: false, nearby: true, can_pair: true, kind: "phone" },
        ];
      }, 900);
      return snap();
    },
    discover_nearby: async () => MockApi.scan_bluetooth(),
    pair_device: async (id) => {
      state.bluetooth.pairing_id = id;
      setTimeout(() => {
        state.bluetooth.pairing_id = null;
        state.nearby_devices = [];
        state.devices = [{ id: "demo-paired", name: "Demo Phone", profile: "Audio ready — tap Connect", paired: true, audio_ready: true, kind: "phone" }];
        state.bluetooth.error = "Paired — tap Connect on the phone below.";
      }, 1200);
      return snap();
    },
    open_bluetooth_settings: async () => {
      state.bluetooth.error = "Fallback Settings opened.";
      return snap();
    },
    connect_device: async (id) => {
      state.bluetooth.connecting_id = id;
      setTimeout(() => {
        state.bluetooth.connecting_id = null;
        state.bluetooth.connected_id = id;
      }, 900);
      return snap();
    },
    disconnect_device: async () => {
      state.bluetooth.connected_id = null;
      state.bluetooth.connecting_id = null;
      state.playing = false;
      state.live_mode = null;
      return snap();
    },
    forget_device: async (id) => {
      state.bluetooth.forgetting = true;
      setTimeout(() => {
        state.bluetooth.forgetting = false;
        state.bluetooth.connected_id = null;
        state.devices = (state.devices || []).filter((d) => d.id !== id);
        state.bluetooth.error = "Device forgotten from Windows.";
      }, 700);
      return snap();
    },
    forget_all_devices: async () => {
      state.bluetooth.forgetting = true;
      setTimeout(() => {
        state.bluetooth.forgetting = false;
        state.bluetooth.connected_id = null;
        state.devices = [];
        state.bluetooth.error = "Forgot all paired Bluetooth devices from Windows.";
      }, 900);
      return snap();
    },
    clear_bluetooth_alert: async () => {
      state.bluetooth.alert = null;
      return snap();
    },
  };
})();

let api = MockApi;
function bindApi() {
  if (window.pywebview && window.pywebview.api) api = window.pywebview.api;
}

// ----------------------------------------------------------------- helpers --
const fmtTime = (s) => {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
};
const deviceIcon = (kind) =>
  kind === "laptop"
    ? '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M2 20h20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
    : '<svg viewBox="0 0 24 24"><rect x="7" y="2" width="10" height="20" rx="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M11 18h2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

let seeking = false;
let demoSeeking = false;
let playClickLock = false;
let demoPlayClickLock = false;
let lastState = { playing: false };
let lastDevicesSig = "";
let lastNearbySig = "";
let lastBtAlert = "";

// Bass cutoff UI: 0% = deepest (40 Hz), 100% = brightest (250 Hz). Default 76% ≈ 200 Hz.
const CUTOFF_HZ_MIN = 40;
const CUTOFF_HZ_MAX = 250;
const pctToCutoffHz = (pct) =>
  CUTOFF_HZ_MIN + (Math.max(0, Math.min(100, Number(pct))) / 100) * (CUTOFF_HZ_MAX - CUTOFF_HZ_MIN);
const cutoffHzToPct = (hz) =>
  Math.round(
    (100 * (Math.max(CUTOFF_HZ_MIN, Math.min(CUTOFF_HZ_MAX, Number(hz))) - CUTOFF_HZ_MIN) /
      (CUTOFF_HZ_MAX - CUTOFF_HZ_MIN))
  );

function isDemoPlaying(s) {
  return Boolean(s && s.playing && s.live_mode === "demo");
}
function isLivePlaying(s) {
  return Boolean(s && s.playing && s.live_mode !== "demo");
}

// ------------------------------------------------- shaker visualization --
// 8 shakers over the bed image. Each row of 2 shakers shares one signal
// (zone_rms from the engine): head, upper back, mid back, legs.
//
// VIZ_EDIT_MODE = true  → shakers are always visible and draggable; the
// current positions are shown bottom-left as ready-to-paste VIZ_ZONES lines
// and saved in localStorage. Set to false once positions are final.
const VIZ_EDIT_MODE = false;
const VIZ_POS_KEY = "muvi-shaker-positions-v1";

const VIZ_ZONES = [
  { key: "audioL", zone: "left",  x: 45.1, y: 13.8, color: "#EE82EE" },
  { key: "audioR", zone: "right", x: 53.5, y: 13.8, color: "#EE82EE" },
  { key: "headL",  zone: "head",  x: 45.3, y: 27.0, color: "#008000" },
  { key: "headR",  zone: "head",  x: 52.9, y: 26.7, color: "#008000" },
  { key: "upperL", zone: "upper", x: 45.5, y: 44.4, color: "#FFFF00" },
  { key: "upperR", zone: "upper", x: 53.2, y: 44.3, color: "#FFFF00" },
  { key: "midL",   zone: "mid",   x: 45.0, y: 58.9, color: "#FFA500" },
  { key: "midR",   zone: "mid",   x: 53.5, y: 58.7, color: "#FFA500" },
  { key: "legL",   zone: "legs",  x: 46.1, y: 80.2, color: "#FF0000" },
  { key: "legR",   zone: "legs",  x: 53.0, y: 80.1, color: "#FF0000" },
  // Stereo audio channels (speakers) — driven by the music itself.
 
];

const VIB_ZONE_UI = [
  { key: "head", label: "Head", color: "#008000" },
  { key: "upper", label: "Upper back", color: "#FFFF00" },
  { key: "mid", label: "Mid back", color: "#FFA500" },
  { key: "legs", label: "Legs", color: "#FF0000" },
];

const zoneMuteOnSvg =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9v6h4l5 5V4L9 9H5z" fill="currentColor"/><path d="M17 8a5 5 0 010 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
const zoneMuteOffSvg =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9v6h4l5 5V4L9 9H5z" fill="currentColor"/><path d="M23 3L3 23" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

function buildVibZoneRows(containerId) {
  const container = el(containerId);
  if (!container || container.dataset.built) return;
  container.dataset.built = "1";
  VIB_ZONE_UI.forEach((z) => {
    const row = document.createElement("div");
    row.className = "vib-zone-row";
    row.dataset.zone = z.key;
    const dot = document.createElement("span");
    dot.className = "vib-zone-dot";
    dot.style.backgroundColor = z.color;
    dot.style.color = z.color;
    const label = document.createElement("span");
    label.className = "vib-zone-label";
    label.textContent = z.label;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "zone-mute-btn";
    btn.dataset.zone = z.key;
    btn.title = `Mute ${z.label} vibration`;
    const iconOn = document.createElement("span");
    iconOn.className = "zone-mute-icon icon-on";
    iconOn.innerHTML = zoneMuteOnSvg;
    const iconOff = document.createElement("span");
    iconOff.className = "zone-mute-icon icon-off";
    iconOff.innerHTML = zoneMuteOffSvg;
    btn.appendChild(iconOn);
    btn.appendChild(iconOff);
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const nextEnabled = btn.classList.contains("muted");
      if (api.set_zone_enabled) {
        render(await api.set_zone_enabled(z.key, nextEnabled));
      }
    });
    row.appendChild(dot);
    row.appendChild(label);
    row.appendChild(btn);
    container.appendChild(row);
  });
}

function renderHeadphonesMute(s) {
  const muted = Boolean(s.audio_muted);
  ["headphonesMuteBtn", "demoHeadphonesMuteBtn"].forEach((id) => {
    const btn = el(id);
    if (!btn) return;
    const stateKey = muted ? "1" : "0";
    if (btn.dataset.muteState === stateKey) return;
    btn.dataset.muteState = stateKey;
    btn.classList.toggle("muted", muted);
    btn.title = muted ? "Unmute audio" : "Mute audio";
    btn.setAttribute("aria-label", btn.title);
  });
}

function renderVibrationMute(s) {
  const muted = Boolean(s.vibration_muted);
  ["vibrationMuteBtn", "demoVibrationMuteBtn"].forEach((id) => {
    const btn = el(id);
    if (!btn) return;
    const stateKey = muted ? "1" : "0";
    if (btn.dataset.muteState === stateKey) return;
    btn.dataset.muteState = stateKey;
    btn.classList.toggle("muted", muted);
    btn.title = muted ? "Unmute vibration" : "Mute vibration";
    btn.setAttribute("aria-label", btn.title);
  });
}

function renderVibZoneRows(s) {
  const enabled = s.zone_enabled || {};
  const vibMuted = Boolean(s.vibration_muted);
  ["vibZoneRows", "demoVibZoneRows"].forEach((id) => {
    const container = el(id);
    if (!container) return;
    container.classList.toggle("all-muted", vibMuted);
    container.querySelectorAll(".vib-zone-row").forEach((row) => {
      const zone = row.dataset.zone;
      const on = Boolean(enabled[zone]) && !vibMuted;
      row.classList.toggle("muted", !on);
      const btn = row.querySelector(".zone-mute-btn");
      if (!btn) return;
      const stateKey = `zone:${zone}:${Boolean(enabled[zone])}:vm:${vibMuted}`;
      if (btn.dataset.zoneState === stateKey) return;
      btn.dataset.zoneState = stateKey;
      btn.classList.toggle("muted", !Boolean(enabled[zone]));
      const label = VIB_ZONE_UI.find((z) => z.key === zone)?.label || zone;
      btn.title = enabled[zone] ? `Mute ${label} vibration` : `Enable ${label} vibration`;
      btn.setAttribute("aria-label", btn.title);
    });
  });
  renderHeadphonesMute(s);
  renderVibrationMute(s);
}

function loadVizPositions() {
  if (!VIZ_EDIT_MODE) return {};
  try {
    return JSON.parse(localStorage.getItem(VIZ_POS_KEY) || "{}") || {};
  } catch (e) {
    return {};
  }
}

function vizPosition(z) {
  const saved = loadVizPositions()[z.key];
  return saved ? { x: saved.x, y: saved.y } : { x: z.x, y: z.y };
}

function setShakerPosition(key, x, y) {
  vizInstances.forEach((shakers) =>
    shakers.forEach((sh) => {
      if (sh.key === key) {
        sh.wrap.style.left = `${x}%`;
        sh.wrap.style.top = `${y}%`;
      }
    })
  );
  const pos = loadVizPositions();
  pos[key] = { x, y };
  try { localStorage.setItem(VIZ_POS_KEY, JSON.stringify(pos)); } catch (e) {}
  updateVizDump();
}

function updateVizDump() {
  if (!VIZ_EDIT_MODE) return;
  let dump = document.getElementById("vizPosDump");
  if (!dump) {
    dump = document.createElement("pre");
    dump.id = "vizPosDump";
    dump.className = "viz-pos-dump";
    document.body.appendChild(dump);
  }
  dump.textContent =
    "EDIT MODE — drag the glowing dots. Paste into VIZ_ZONES:\n" +
    VIZ_ZONES.map((z) => {
      const p = vizPosition(z);
      const key = `"${z.key}",`.padEnd(9);
      return `{ key: ${key} zone: "${z.zone}", x: ${p.x.toFixed(1)}, y: ${p.y.toFixed(1)}, color: "${z.color}" },`;
    }).join("\n");
}

function makeDraggable(sh, container) {
  const hit = document.createElement("div");
  hit.className = "shaker-hit";
  sh.wrap.appendChild(hit);
  let dragging = false;
  hit.addEventListener("pointerdown", (e) => {
    dragging = true;
    e.preventDefault();
    try { hit.setPointerCapture(e.pointerId); } catch (err) {}
  });
  hit.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100));
    const y = Math.max(0, Math.min(100, ((e.clientY - rect.top) / rect.height) * 100));
    setShakerPosition(sh.key, x, y);
  });
  const end = () => { dragging = false; };
  hit.addEventListener("pointerup", end);
  hit.addEventListener("pointercancel", end);
}

const hexToRgb = (hex) => {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  if (!m) return { r: 160, g: 228, b: 255 };
  return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
};

const vizInstances = [];

function buildViz(container) {
  const shakers = VIZ_ZONES.map((z) => {
    const wrap = document.createElement("div");
    wrap.className = "shaker";
    const pos = vizPosition(z);
    wrap.style.left = `${pos.x}%`;
    wrap.style.top = `${pos.y}%`;
    const rings = [];
    for (let i = 0; i < 3; i++) {
      const ring = document.createElement("div");
      ring.className = "shaker-ring";
      ring.style.setProperty("--wave-alpha", i === 0 ? "1" : "0");
      wrap.appendChild(ring);
      rings.push(ring);
    }
    const core = document.createElement("div");
    core.className = "shaker-core";
    wrap.appendChild(core);
    container.appendChild(wrap);
    const sh = {
      key: z.key,
      zone: z.zone,
      rgb: hexToRgb(z.color),
      wrap,
      rings,
      core,
      level: 0,
      waveAlpha: [1, 0, 0],
      playbackRate: 1,
    };
    if (VIZ_EDIT_MODE) makeDraggable(sh, container);
    return sh;
  });
  vizInstances.push(shakers);
}

function initViz() {
  ["npViz", "demoViz"].forEach((id) => {
    const c = el(id);
    if (c) buildViz(c);
  });
  updateVizDump();
}

// Engine RMS (~0–0.3 typical) → 0..1 wave intensity.
// Steep at the low end so quiet/mid signals still produce visible waves.
function rmsToIntensity(rms) {
  const v = Math.max(0, Number(rms) || 0);
  return Math.max(0, Math.min(1, Math.pow(v * 5, 0.5)));
}

function applyShaker(sh) {
  // Edit mode keeps every shaker visible so it can be dragged while idle.
  let t = Math.max(0, Math.min(1, sh.level));
  if (VIZ_EDIT_MODE) t = Math.max(t, 0.3);
  const sizeT = t;
  const { r, g, b } = sh.rgb;
  sh.wrap.style.opacity = VIZ_EDIT_MODE || t > 0.02 ? "1" : "0";
  const st = sh.wrap.style;

  // Keep the display clean when every channel is strong: one wave normally,
  // at most two above 75%, and never a third simultaneous wave.
  const waveTarget = [1, t >= 0.75 ? 1 : 0, 0];
  sh.waveAlpha = sh.waveAlpha.map((a, i) => a + (waveTarget[i] - a) * 0.28);
  sh.rings.forEach((ring, i) => {
    ring.style.setProperty("--wave-alpha", sh.waveAlpha[i].toFixed(3));
  });

  // Speed via playbackRate — does not restart the CSS animation.
  // ~0.65x at quiet → ~1.3x at full signal. Combined with the shorter
  // animation lifetime, stopped zones clear before another zone takes over.
  const rate = 0.65 + t * 0.65;
  if (Math.abs(rate - sh.playbackRate) > 0.03) {
    sh.playbackRate = rate;
    try {
      sh.wrap.getAnimations({ subtree: true }).forEach((anim) => {
        anim.playbackRate = rate;
      });
    } catch (e) {}
  }

  // Size via scale (not width/height) so the ripple stays locked on center.
  st.setProperty("--ring-scale", (0.25 + sizeT * 0.65).toFixed(3));
  st.setProperty("--ring-color", `rgba(${r},${g},${b},${(0.38 + t * 0.42).toFixed(3)})`);
  st.setProperty("--glow", `${(8 + t * 20).toFixed(1)}px`);
  st.setProperty("--glow-color", `rgba(${r},${g},${b},${(0.16 + t * 0.34).toFixed(3)})`);
  st.setProperty("--core-scale", (0.45 + sizeT * 0.45).toFixed(3));
  st.setProperty("--core-a", `rgba(${r},${g},${b},${(0.72 + t * 0.28).toFixed(3)})`);
  st.setProperty("--core-b", `rgba(${r},${g},${b},0)`);
}

function updateViz(s) {
  const playing = Boolean(s && s.playing);
  const zoneRms = s && s.zone_rms;
  const zoneEnabled = s && s.zone_enabled;
  const audioMuted = Boolean(s && s.audio_muted);
  const vibMuted = Boolean(s && s.vibration_muted);
  vizInstances.forEach((shakers) =>
    shakers.forEach((sh) => {
      let rms = 0;
      if (playing) {
        // Older backends don't send zone_rms — fall back to overall input level.
        rms = zoneRms ? zoneRms[sh.zone] : Number(s.input_rms || 0);
        if (sh.zone === "left" || sh.zone === "right") {
          if (audioMuted) rms = 0;
        } else if (vibMuted || (zoneEnabled && zoneEnabled[sh.zone] === false)) {
          rms = 0;
        }
      }
      const target = rmsToIntensity(rms);
      // Gentle attack/release so wave size drifts instead of jumping,
      // but drop quickly to zero when the signal goes fully silent.
      const rate = target > sh.level ? 0.4 : target < 0.02 ? 0.7 : 0.25;
      sh.level += (target - sh.level) * rate;
      if (target === 0 && sh.level < 0.03) sh.level = 0;
      applyShaker(sh);
    })
  );
}

const busyIcon = '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.35"/><path d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>';
const pauseIcon = '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/>';
const playIconSvg = '<path d="M8 5v14l11-7z" fill="currentColor"/>';

const statusIconNowPlaying =
  '<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="6" cy="18" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="18" cy="16" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const statusIconDemo =
  '<svg viewBox="0 0 24 24"><path d="M12 3v10M8 7l4-4 4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><rect x="4" y="14" width="16" height="7" rx="2" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const statusIconBluetooth =
  '<svg viewBox="0 0 24 24"><path d="M7 7l10 10-5 4V3l5 4L7 17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const statusIconIdle =
  '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

function navigateToScreen(screenId) {
  const navBtn = document.querySelector(`.nav-item[data-screen="${screenId}"]`);
  if (!navBtn || !el(`screen-${screenId}`)) return;
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.remove("active"));
  navBtn.classList.add("active");
  document.querySelectorAll(".screen").forEach((sc) => sc.classList.remove("active"));
  el(`screen-${screenId}`).classList.add("active");
}

function shortCaptureName(name) {
  return String(name || "").replace(/\s*\[.*?\]\s*/g, "").trim();
}

// ------------------------------------------------------------------ render --
let lastHardwareSig = "";
let lastSettingsSig = "";

function setHwItem(rootId, connected, title) {
  const item = el(rootId);
  if (!item) return;
  item.classList.toggle("ok", connected);
  item.classList.toggle("bad", !connected);
  if (title) item.title = title;
}

function formatHwDeviceName(name, fallback) {
  const n = shortCaptureName(name);
  if (n) return n;
  return fallback || "Not detected";
}

function setHwDetailRow(rowId, connected, deviceName, inUse) {
  const row = el(rowId);
  const deviceEl = el(`${rowId}Device`);
  const statusEl = el(`${rowId}Status`);
  if (!row) return;
  row.classList.toggle("ok", connected && !inUse);
  row.classList.toggle("bad", !connected);
  row.classList.toggle("live", inUse);
  if (deviceEl) {
    deviceEl.textContent = connected ? deviceName : "Not detected";
  }
  if (statusEl) {
    if (inUse) {
      statusEl.textContent = "Playing";
      statusEl.className = "hw-detail-status live";
    } else if (connected) {
      statusEl.textContent = "Connected";
      statusEl.className = "hw-detail-status ok";
    } else {
      statusEl.textContent = "Not connected";
      statusEl.className = "hw-detail-status bad";
    }
  }
}

function renderSettingsStatus(hw, state) {
  if (!el("hwRowGigaport")) return;
  const flipped = Boolean(hw?.roles_flipped || state?.gigaport_roles_flipped);
  const canSwap = Boolean(hw?.can_swap_gigaports) || Number(hw?.gigaport_count || 0) >= 2;
  const sig = JSON.stringify({ hw, playing: Boolean(state?.playing), flipped, canSwap });
  if (sig === lastSettingsSig) return;
  lastSettingsSig = sig;

  const playing = Boolean(state?.playing);
  const gOk = Boolean(hw?.gigaport_connected);
  const cOk = Boolean(hw?.codec_connected);
  const gCount = Number(hw?.gigaport_count || 0);
  let gName = formatHwDeviceName(hw?.gigaport_name, "Gigaport");
  if (gCount > 1) gName = `${gName} (${gCount} units)`;
  const cName = formatHwDeviceName(
    hw?.codec_name,
    hw?.headphones_kind === "gigaport" ? "Gigaport" : "USB Audio CODEC",
  );

  setHwDetailRow("hwRowGigaport", gOk, gName, playing && gOk);
  setHwDetailRow("hwRowCodec", cOk, cName, playing && cOk);

  const swapCard = el("gigaportSwapCard");
  if (swapCard) swapCard.hidden = !canSwap;
  const swapBtn = el("swapGigaportsBtn");
  if (swapBtn) {
    swapBtn.textContent = flipped ? "Swap back" : "Swap";
  }
  const hint = el("gigaportSwapHint");
  if (hint) {
    hint.textContent = flipped
      ? "Roles swapped — vibration and headphones Gigaports are flipped."
      : "Swap which unit drives vibration vs headphones.";
  }

  const batRow = el("hwRowBattery");
  const batVisible = Boolean(hw?.battery_available);
  if (batRow) batRow.hidden = !batVisible;
  if (!batVisible) return;

  const pctNum = hw.battery_percent;
  const pct = pctNum != null ? `${pctNum}%` : "—";
  const charging = Boolean(hw.charging);
  const deviceEl = el("hwRowBatteryDevice");
  const statusEl = el("hwRowBatteryStatus");
  if (deviceEl) deviceEl.textContent = pct;
  if (statusEl) {
    statusEl.textContent = charging ? "Charging" : "On battery";
    statusEl.className = charging ? "hw-detail-status ok" : "hw-detail-status bad";
  }
  if (batRow) {
    batRow.classList.toggle("ok", charging);
    batRow.classList.toggle("bad", !charging);
    batRow.classList.remove("live");
  }
}

function renderHardwareStatus(hw, playing = false, state = null) {
  if (!hw) return;
  const sig = JSON.stringify(hw);
  if (sig !== lastHardwareSig) {
    lastHardwareSig = sig;
    lastSettingsSig = "";

    const gCount = Number(hw.gigaport_count || 0);
    const gTitle = hw.gigaport_connected
      ? `Gigaport · ${formatHwDeviceName(hw.gigaport_name, "connected")}${gCount > 1 ? ` (${gCount})` : ""}`
      : "Gigaport not detected";
    const cTitle = hw.codec_connected
      ? `Headphones · ${formatHwDeviceName(hw.codec_name, "connected")}`
      : "Headphones not detected";

    setHwItem("hwGigaport", hw.gigaport_connected, gTitle);
    setHwItem("hwCodec", hw.codec_connected, cTitle);

    const batVisible = Boolean(hw.battery_available);
    const batEl = el("hwBattery");
    if (batEl) batEl.hidden = !batVisible;
    if (batVisible) {
      const pctNum = hw.battery_percent;
      const pct = pctNum != null ? `${pctNum}%` : "—";
      const pctEl = el("hwBatteryPct");
      if (pctEl) pctEl.textContent = pct;
      const charging = Boolean(hw.charging);
      const batTitle = charging ? `Charging · ${pct}` : `Battery · ${pct}`;
      if (batEl) {
        batEl.classList.toggle("ok", charging);
        batEl.classList.toggle("bad", !charging);
        batEl.title = batTitle;
      }
    }
  }
  renderSettingsStatus(hw, state || lastState);
}

function render(s) {
  if (!s) return;
  lastState = s;
  const liveOn = isLivePlaying(s);
  const demoOn = isDemoPlaying(s);
  renderHardwareStatus(s.hardware, liveOn || demoOn, s);
  // Pop pairing guidance once (e.g. status 19 FAILED → forget on phone).
  const alertMsg = s.bluetooth && s.bluetooth.alert;
  if (alertMsg && alertMsg !== lastBtAlert) {
    lastBtAlert = alertMsg;
    window.alert(alertMsg);
    if (api.clear_bluetooth_alert) {
      Promise.resolve(api.clear_bluetooth_alert()).catch(() => {});
    }
  } else if (!alertMsg) {
    lastBtAlert = "";
  }

  const busy = Boolean(s.play_busy) || playClickLock;
  const demoBusy = Boolean(s.play_busy) || demoPlayClickLock;

  const liveInput = s.live_input || {};
  const liveInputReady = Boolean(liveInput.ready)
    && (liveInput.mode === "aux" || liveInput.mode === "cable");
  const canStartLive = liveInputReady;

  // Now playing (Live / AUX / Bluetooth only)
  el("npTitle").textContent = demoOn ? "Ready" : s.track.title;
  el("npArtist").textContent = demoOn ? "Phone audio" : s.track.artist;
  const albumText = demoOn ? "" : (s.track.album || "");
  el("npAlbum").textContent = albumText;
  el("npAlbum").hidden = !albumText;

  updateViz(s);

  const connected = (s.devices || []).find((d) => d.id === s.bluetooth.connected_id);
  const online = Boolean(connected);
  const btSession = Boolean(s.bluetooth.connected_id);
  const connectingId = s.bluetooth.connecting_id || null;
  const connectingDev = connectingId
    ? (s.devices || []).find((d) => d.id === connectingId)
    : null;

  const playBtn = el("playBtn");
  // Bluetooth session owns Live — no Play/Stop while phone is connected.
  // AUX (no BT) still uses Play/Stop manually.
  if (btSession) {
    playBtn.hidden = true;
    playBtn.disabled = true;
    playBtn.title = "Live runs automatically while Bluetooth is connected";
  } else {
    playBtn.hidden = false;
    playBtn.disabled = busy || (!liveOn && !canStartLive);
    playBtn.title = liveOn
      ? "Stop Live"
      : (canStartLive ? "Start Live" : (liveInput.message || "Connect AUX or Bluetooth to start"));
  }
  playBtn.classList.toggle("disabled", playBtn.disabled && !busy);
  playBtn.classList.toggle("busy", busy && !btSession);
  playBtn.setAttribute("aria-busy", busy && !btSession ? "true" : "false");
  el("playIcon").innerHTML = busy ? busyIcon : (liveOn ? pauseIcon : playIconSvg);

  const hint = el("liveHint");
  if (hint) {
    if (btSession && liveOn) {
      hint.textContent = "Bluetooth Live — playing until you disconnect";
    } else if (btSession && busy) {
      hint.textContent = "Starting Bluetooth Live…";
    } else if (btSession && s.engine_error) {
      hint.textContent = s.engine_error;
    } else if (busy && !demoOn) {
      hint.textContent = liveOn ? "Stopping…" : "Starting…";
    } else if (s.engine_error && !demoOn) {
      hint.textContent = s.engine_error;
    } else if (!liveOn && !liveInputReady) {
      hint.textContent = liveInput.message || "Connect AUX or Bluetooth to start.";
    } else {
      hint.textContent = "";
    }
  }

  if (!seeking) {
    const frac = (!demoOn && s.track.duration) ? s.position / s.track.duration : 0;
    el("seek").value = Math.round(Math.max(0, Math.min(1, frac)) * 1000);
  }
  el("timeCur").textContent = fmtTime(demoOn ? 0 : s.position);
  el("timeDur").textContent = (!demoOn && Number(s.track.duration) > 0) ? fmtTime(s.track.duration) : "LIVE";

  const media = s.media || {};
  const hasTimeline = !demoOn && Boolean(media.ok) && Number(s.track.duration) > 0;
  const seekEl = el("seek");
  seekEl.disabled = !hasTimeline;
  seekEl.classList.toggle("disabled", seekEl.disabled);
  const prevBtn = el("prevBtn");
  const nextBtn = el("nextBtn");
  const btConnected = Boolean(s.bluetooth && s.bluetooth.connected_id);
  prevBtn.disabled = demoOn || !(media.can_prev || btConnected);
  nextBtn.disabled = demoOn || !(media.can_next || btConnected);
  prevBtn.classList.toggle("disabled", prevBtn.disabled);
  nextBtn.classList.toggle("disabled", nextBtn.disabled);

  el("volume").value = Math.round(s.volume * 100);
  el("volVal").textContent = `${Math.round(s.volume * 100)}%`;
  el("vibration").value = Math.round(s.vibration * 100);
  el("vibVal").textContent = `${Math.round(s.vibration * 100)}%`;
  renderVibZoneRows(s);
  const cutoffPct = cutoffHzToPct(s.cutoff_hz ?? s.highpass_hz);
  el("highpass").value = cutoffPct;
  el("hpVal").textContent = `${cutoffPct}%`;

  const dualOut = s.output_layout === "dual_native";
  const audioCh = Number(s.audio_output_channels || 0);
  // Headphones = CH1&2, Speakers = CH3&4 on the audio Gigaport (needs >=4 ch;
  // ideally 8 so Windows does not remap Speakers onto CH5&6).
  const routeCapable = dualOut && audioCh >= 4;
  const route = s.speaker_route === "secondary" ? "secondary" : "headphones";
  [
    ["speakerRouteCard", "routeHeadphones", "routeSpeaker"],
    ["demoSpeakerRouteCard", "demoRouteHeadphones", "demoRouteSpeaker"],
  ].forEach(([cardId, hpId, spkId]) => {
    const routeCard = el(cardId);
    if (!routeCard) return;
    routeCard.classList.toggle("hidden", !routeCapable);
    el(hpId)?.classList.toggle("active", route === "headphones");
    el(spkId)?.classList.toggle("active", route === "secondary");
  });

  // Vibration routing: "zones" | "stereo" | "haptics".
  const vibMode =
    s.vibration_mode === "stereo"
      ? "stereo"
      : s.vibration_mode === "zones"
        ? "zones"
        : "haptics";
  [
    ["vibModeCard", "vibModeZones", "vibModeStereo", "vibModeHaptics"],
    ["demoVibModeCard", "demoVibModeZones", "demoVibModeStereo", "demoVibModeHaptics"],
  ].forEach(([cardId, zonesId, stereoId, hapticsId]) => {
    el(cardId)?.classList.toggle("hidden", !SHOW_VIBRATION_MODE_SWITCH);
    el(zonesId)?.classList.toggle("active", vibMode === "zones");
    el(stereoId)?.classList.toggle("active", vibMode === "stereo");
    el(hapticsId)?.classList.toggle("active", vibMode === "haptics");
  });

  // Demo screen
  if (el("demoTitle")) {
    const demoTrack = demoOn
      ? s.track
      : { title: "Demo", artist: "MUVI test track", album: "", duration: s.track?.duration || 0, artwork: "assets/artwork.svg" };
    el("demoTitle").textContent = demoTrack.title || "Demo";
    el("demoArtist").textContent = demoTrack.artist || "MUVI test track";
    const demoAlbum = el("demoAlbum");
    if (demoAlbum) {
      demoAlbum.textContent = "";
      demoAlbum.hidden = true;
    }

    const demoPlayBtn = el("demoPlayBtn");
    demoPlayBtn.disabled = demoBusy;
    demoPlayBtn.classList.toggle("busy", demoBusy);
    el("demoPlayIcon").innerHTML = demoBusy ? busyIcon : (demoOn ? pauseIcon : playIconSvg);

    if (!demoSeeking) {
      const dFrac = demoOn && s.track.duration ? s.position / s.track.duration : 0;
      el("demoSeek").value = Math.round(Math.max(0, Math.min(1, dFrac)) * 1000);
    }
    el("demoTimeCur").textContent = fmtTime(demoOn ? s.position : 0);
    el("demoTimeDur").textContent = Number(demoOn ? s.track.duration : 0) > 0
      ? fmtTime(s.track.duration)
      : (demoOn ? fmtTime(s.track.duration || 0) : "—");

    const demoSeekEl = el("demoSeek");
    demoSeekEl.disabled = !demoOn;
    demoSeekEl.classList.toggle("disabled", demoSeekEl.disabled);
    el("demoPrevBtn").disabled = !demoOn;
    el("demoNextBtn").disabled = !demoOn;
    el("demoPrevBtn").classList.toggle("disabled", !demoOn);
    el("demoNextBtn").classList.toggle("disabled", !demoOn);

    el("demoVolume").value = Math.round(s.volume * 100);
    el("demoVolVal").textContent = `${Math.round(s.volume * 100)}%`;
    el("demoVibration").value = Math.round(s.vibration * 100);
    el("demoVibVal").textContent = `${Math.round(s.vibration * 100)}%`;
    el("demoHighpass").value = cutoffPct;
    el("demoHpVal").textContent = `${cutoffPct}%`;

    const demoHint = el("demoHint");
    if (demoHint) {
      if (demoBusy) {
        demoHint.textContent = demoOn ? "Stopping…" : "Starting…";
      } else if (s.demo && s.demo.error) {
        demoHint.textContent = s.demo.error;
      } else if (s.demo && s.demo.available === false) {
        demoHint.textContent = "Demo track not found";
      } else {
        demoHint.textContent = "";
      }
    }
  }

  // Sidebar status — tap to open Now Playing, Demo, or Bluetooth
  const sidebarConn = el("sidebarConn");
  const mode = s.live_mode || "";
  const capture = String(s.capture_name || "");
  const captureShort = shortCaptureName(capture).slice(0, 36);
  const isAuxLive = liveOn && (mode === "aux" || /behringer|umc|usb audio|line/i.test(capture));
  const rms = Number(s.input_rms || 0);
  const hasSignal = rms > 0.002;

  let statusTitle = "Ready";
  let statusSub = "AUX or Bluetooth";
  let statusIcon = statusIconIdle;
  let statusClass = "idle";
  let navigateTarget = "bluetooth";

  if (demoOn) {
    statusClass = "live-demo";
    statusTitle = "Demo playing";
    statusSub = "Tap to open";
    statusIcon = statusIconDemo;
    navigateTarget = "demo";
  } else if (liveOn) {
    statusClass = "live-now";
    statusIcon = statusIconNowPlaying;
    navigateTarget = "now-playing";
    if (isAuxLive) {
      statusTitle = "Now Playing";
      statusSub = hasSignal ? "AUX" : "Waiting for audio…";
    } else if (mode === "cable") {
      statusTitle = "Now Playing";
      statusSub = hasSignal ? "Bluetooth" : "Waiting for audio…";
    } else {
      statusTitle = "Now Playing";
      statusSub = captureShort || "Live";
    }
  } else if (online) {
    statusClass = "bt-connected";
    statusTitle = connected.name;
    statusSub = "Connected";
    statusIcon = statusIconBluetooth;
    navigateTarget = "bluetooth";
  } else if (liveInput.ready && liveInput.mode === "aux") {
    statusClass = "ready";
    statusTitle = "AUX ready";
    statusSub = shortCaptureName(liveInput.capture_name) || "Ready to play";
    statusIcon = statusIconNowPlaying;
    navigateTarget = "now-playing";
  } else if (liveInput.ready && liveInput.mode === "cable") {
    statusClass = "ready";
    statusTitle = "Bluetooth ready";
    statusSub = "Ready to play";
    statusIcon = statusIconNowPlaying;
    navigateTarget = "now-playing";
  } else if (s.bluetooth?.error && /bluetooth is off|disconnected|unavailable/i.test(String(s.bluetooth.error))) {
    statusSub = String(s.bluetooth.error).split(".")[0];
    navigateTarget = "bluetooth";
  } else if (liveInput.message) {
    statusSub = liveInput.message;
    navigateTarget = "bluetooth";
  }

  if (sidebarConn) {
    sidebarConn.className = "conn-pill";
    sidebarConn.classList.add(statusClass);
    if (navigateTarget) sidebarConn.classList.add("navigable");
    sidebarConn.dataset.navigate = navigateTarget || "";
    const iconEl = el("sidebarConnIcon");
    if (iconEl) iconEl.innerHTML = statusIcon;
    const titleEl = el("sidebarConnTitle");
    if (titleEl) titleEl.textContent = statusTitle;
    const subEl = el("sidebarConnSub");
    if (subEl) subEl.textContent = statusSub;
  }

  el("statusCard").classList.toggle("online", online || isAuxLive);
  const bt = s.bluetooth;
  const pairingId = bt.pairing_id || null;
  el("statusCard").classList.toggle("connecting", Boolean(connectingId) || Boolean(pairingId));
  if (pairingId) {
    el("statusTitle").textContent = "Waiting for confirmation…";
  } else if (isAuxLive) {
    el("statusTitle").textContent = "AUX Live";
  } else if (connectingId) {
    el("statusTitle").textContent = connectingDev
      ? `Connecting to ${connectingDev.name}…`
      : "Connecting…";
  } else {
    el("statusTitle").textContent = online ? `Connected to ${connected.name}` : "Not connected";
  }
  let sub = online ? connected.profile : "Paired phones appear automatically. Scan to find a new phone.";
  if (isAuxLive) {
    sub = rms > 0.002
      ? `Capturing ${capture || "AUX"} → Gigaport`
      : "AUX Live — play music on the phone";
  } else if (pairingId) {
    sub = "Waiting for confirmation on the phone…";
  } else if (connectingId) {
    sub = "Please wait — opening Bluetooth audio connection…";
  }   else if (online && s.engine_error) sub = `BT connected, but Live failed: ${s.engine_error}`;
  else if (online && liveOn && s.capture_name) sub = `Live ON · capturing ${s.capture_name} — disconnect phone to stop`;
  else if (online && liveOn) sub = "Live ON — play music on the phone. Disconnect to stop.";
  else if (online && s.engine_ok === false) sub = `BT connected, engine unavailable: ${s.engine_error || "unknown"}`;
  else if (!online && bt.error) sub = bt.error;
  else if (!online && bt.supported === false) sub = "Bluetooth receiver unavailable on this machine (Windows only).";
  else if (online) sub = "Connected — Live starts automatically. Play music on the phone.";
  else if (bt.scanning || bt.discovering) sub = "Scanning nearby — put the phone in pairing mode…";
  el("statusSub").textContent = sub;
  const forgetting = Boolean(bt.forgetting);
  el("disconnectBtn").hidden = !online || Boolean(connectingId) || Boolean(pairingId);
  el("disconnectBtn").disabled = forgetting;
  // Single-device Forget hidden for now — use Forget all.
  const forgetBtn = el("forgetBtn");
  if (forgetBtn) {
    forgetBtn.hidden = true;
    forgetBtn.disabled = true;
  }

  const scanBtn = el("scanBtn");
  const busyBt = Boolean(bt.scanning) || Boolean(bt.discovering) || Boolean(connectingId) || Boolean(pairingId) || forgetting;
  scanBtn.classList.toggle("scanning", Boolean(bt.scanning) || Boolean(bt.discovering));
  scanBtn.disabled = busyBt;
  el("scanLabel").textContent = (bt.scanning || bt.discovering) ? "Scanning…" : "Scan";
  const forgetAllBtn = el("forgetAllBtn");
  if (forgetAllBtn) {
    forgetAllBtn.disabled = busyBt;
    forgetAllBtn.textContent = forgetting ? "Forgetting…" : "Forget all";
  }

  renderDevices(s);
  renderNearby(s);
}

function devicesSig(s) {
  const devices = s.devices || [];
  const ids = devices.map((d) => `${d.id}|${d.name}|${d.profile}|${d.paired}|${d.audio_ready}`).join(";");
  return [
    ids,
    s.bluetooth.connected_id || "",
    s.bluetooth.connecting_id || "",
    s.bluetooth.scanning ? "1" : "0",
    s.bluetooth.pairing_id || "",
    s.bluetooth.forgetting ? "1" : "0",
  ].join("::");
}

function nearbySig(s) {
  const devices = s.nearby_devices || [];
  const ids = devices.map((d) => `${d.id}|${d.name}|${d.profile}`).join(";");
  return [
    ids,
    s.bluetooth.discovering ? "1" : "0",
    s.bluetooth.pairing_id || "",
  ].join("::");
}

function renderDevices(s) {
  const sig = devicesSig(s);
  if (sig === lastDevicesSig) return;
  lastDevicesSig = sig;

  const list = el("deviceList");
  list.innerHTML = "";
  const devices = s.devices || [];
  const connectingId = s.bluetooth.connecting_id || null;
  const pairingId = s.bluetooth.pairing_id || null;
  const scanning = Boolean(s.bluetooth.scanning);
  const busy = Boolean(connectingId) || Boolean(pairingId) || Boolean(s.bluetooth.forgetting);

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = "No paired phones yet — Scan below to find a phone, then Pair.";
    li.innerHTML = `<div class="device-meta"><span class="device-name">No paired phones</span><span class="device-sub">${msg}</span></div>`;
    list.appendChild(li);
    return;
  }

  devices.forEach((d) => {
    const isConn = d.id === s.bluetooth.connected_id;
    const isConnecting = d.id === connectingId;
    const li = document.createElement("li");
    li.className = "device"
      + (isConn ? " connected" : "")
      + (isConnecting ? " connecting" : "");
    let action = "";
    if (isConn) {
      action = '<span class="badge">Connected</span>';
    } else if (isConnecting) {
      action = '<button class="btn-connect busy" disabled><span class="btn-spin"></span>Connecting…</button>';
    } else {
      action = `<button class="btn-connect" data-id="${d.id}" ${busy ? "disabled" : ""}>Connect</button>`;
    }
    li.innerHTML = `
      <div class="device-ico">${deviceIcon(d.kind)}</div>
      <div class="device-meta">
        <span class="device-name">${d.name}</span>
        <span class="device-sub">${isConnecting ? "Opening connection…" : d.profile}</span>
      </div>
      <div class="device-action">${action}</div>`;
    list.appendChild(li);
  });

  list.querySelectorAll(".btn-connect:not(.busy)").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!b.dataset.id || busy) return;
      lastDevicesSig = "";
      render(await api.connect_device(b.dataset.id));
    })
  );
}

function renderNearby(s) {
  const list = el("nearbyList");
  if (!list) return;
  const sig = nearbySig(s);
  if (sig === lastNearbySig) return;
  lastNearbySig = sig;

  list.innerHTML = "";
  const devices = s.nearby_devices || [];
  const discovering = Boolean(s.bluetooth.discovering);
  const pairingId = s.bluetooth.pairing_id || null;
  const busy = Boolean(pairingId) || Boolean(s.bluetooth.connecting_id);

  if (discovering && !devices.length) {
    const li = document.createElement("li");
    li.className = "device empty scanning";
    li.innerHTML = `<div class="device-meta"><span class="device-name">Scanning nearby…</span><span class="device-sub">Put the phone in pairing / discoverable mode.</span></div>`;
    list.appendChild(li);
    return;
  }

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = "Press Scan to search for nearby phones.";
    li.innerHTML = `<div class="device-meta"><span class="device-name">No nearby phones</span><span class="device-sub">${msg}</span></div>`;
    list.appendChild(li);
    return;
  }

  devices.forEach((d) => {
    const isPairing = d.id === pairingId;
    const li = document.createElement("li");
    li.className = "device" + (isPairing ? " connecting" : "");
    let action = "";
    if (isPairing) {
      action = '<button class="btn-connect busy" disabled><span class="btn-spin"></span>Pairing…</button>';
    } else if (d.paired || d.can_pair === false) {
      action = '<span class="badge">Already paired</span>';
    } else {
      action = `<button class="btn-connect" data-pair-id="${d.id}" ${busy ? "disabled" : ""}>Pair</button>`;
    }
    li.innerHTML = `
      <div class="device-ico">${deviceIcon(d.kind)}</div>
      <div class="device-meta">
        <span class="device-name">${d.name}</span>
        <span class="device-sub">${isPairing ? "Waiting for confirmation…" : d.profile}</span>
      </div>
      <div class="device-action">${action}</div>`;
    list.appendChild(li);
  });

  list.querySelectorAll("[data-pair-id]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!b.dataset.pairId || busy) return;
      lastNearbySig = "";
      lastDevicesSig = "";
      render(await api.pair_device(b.dataset.pairId));
    })
  );
}

// ------------------------------------------------------------------ events --
function wire() {
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.addEventListener("click", () => navigateToScreen(b.dataset.screen))
  );

  el("sidebarConn")?.addEventListener("click", () => {
    const target = el("sidebarConn")?.dataset.navigate;
    if (target) navigateToScreen(target);
  });

  el("hwStatusRow")?.addEventListener("click", () => {
    navigateToScreen("settings");
  });

  el("playBtn").addEventListener("click", async () => {
    if (playClickLock) return;
    // Bluetooth session owns Live — Play/Stop not used while phone is connected.
    if (lastState.bluetooth && lastState.bluetooth.connected_id) return;
    const wasPlaying = Boolean(lastState.playing);
    const inputReady = Boolean(lastState.live_input && lastState.live_input.ready);
    if (!wasPlaying && !inputReady) return;
    playClickLock = true;
    el("playBtn").disabled = true;
    el("playBtn").classList.add("busy");
    const hint = el("liveHint");
    if (hint) hint.textContent = wasPlaying ? "Stopping…" : "Starting…";
    try {
      render(await api.toggle_play());
    } finally {
      playClickLock = false;
      render(await api.get_state());
    }
  });

  el("seek").addEventListener("input", () => {
    seeking = true;
    const dur = Number(lastState.track?.duration || 0);
    if (dur > 0) {
      const pos = (el("seek").value / 1000) * dur;
      el("timeCur").textContent = fmtTime(pos);
    }
  });
  el("seek").addEventListener("change", async () => {
    const dur = Number(lastState.track?.duration || 0);
    try {
      if (dur > 0) {
        const pos = (el("seek").value / 1000) * dur;
        render(await api.seek(pos));
      }
    } finally {
      seeking = false;
    }
  });

  el("prevBtn").addEventListener("click", async () => {
    if (el("prevBtn").disabled) return;
    render(await api.previous_track());
  });
  el("nextBtn").addEventListener("click", async () => {
    if (el("nextBtn").disabled) return;
    render(await api.next_track());
  });

  el("volume").addEventListener("input", async () => {
    el("volVal").textContent = `${el("volume").value}%`;
    render(await api.set_volume(el("volume").value / 100));
  });

  const toggleHeadphonesMute = async (btn) => {
    if (!btn || !api.set_audio_muted) return;
    const muted = !btn.classList.contains("muted");
    render(await api.set_audio_muted(muted));
  };
  el("headphonesMuteBtn")?.addEventListener("click", async (e) => {
    e.preventDefault();
    await toggleHeadphonesMute(el("headphonesMuteBtn"));
  });
  el("demoHeadphonesMuteBtn")?.addEventListener("click", async (e) => {
    e.preventDefault();
    await toggleHeadphonesMute(el("demoHeadphonesMuteBtn"));
  });

  const toggleVibrationMute = async (btn) => {
    if (!btn || !api.set_vibration_muted) return;
    const muted = !btn.classList.contains("muted");
    render(await api.set_vibration_muted(muted));
  };
  el("vibrationMuteBtn")?.addEventListener("click", async (e) => {
    e.preventDefault();
    await toggleVibrationMute(el("vibrationMuteBtn"));
  });
  el("demoVibrationMuteBtn")?.addEventListener("click", async (e) => {
    e.preventDefault();
    await toggleVibrationMute(el("demoVibrationMuteBtn"));
  });
  el("vibration").addEventListener("input", async () => {
    el("vibVal").textContent = `${el("vibration").value}%`;
    render(await api.set_vibration(el("vibration").value / 100));
  });
  el("highpass").addEventListener("input", async () => {
    const pct = Number(el("highpass").value);
    el("hpVal").textContent = `${pct}%`;
    render(await api.set_highpass_hz(pctToCutoffHz(pct)));
  });

  document.querySelectorAll(".route-icon-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const route = btn.getAttribute("data-route") || "headphones";
      if (api.set_speaker_route) {
        render(await api.set_speaker_route(route));
      }
    });
  });

  document.querySelectorAll(".vib-mode-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const mode = btn.getAttribute("data-vibmode") || "haptics";
      if (api.set_vibration_mode) {
        render(await api.set_vibration_mode(mode));
      }
    });
  });

  // Demo tab — same controls, plays demo.wav through Live path
  if (el("demoPlayBtn")) {
    el("demoPlayBtn").addEventListener("click", async () => {
      if (demoPlayClickLock) return;
      demoPlayClickLock = true;
      const wasDemo = isDemoPlaying(lastState);
      el("demoPlayBtn").disabled = true;
      el("demoPlayBtn").classList.add("busy");
      const hint = el("demoHint");
      if (hint) hint.textContent = wasDemo ? "Stopping…" : "Starting…";
      try {
        render(await api.toggle_demo_play());
      } finally {
        demoPlayClickLock = false;
        render(await api.get_state());
      }
    });

    el("demoSeek").addEventListener("input", () => {
      demoSeeking = true;
      const dur = Number(lastState.track?.duration || 0);
      if (dur > 0 && isDemoPlaying(lastState)) {
        const pos = (el("demoSeek").value / 1000) * dur;
        el("demoTimeCur").textContent = fmtTime(pos);
      }
    });
    el("demoSeek").addEventListener("change", async () => {
      const dur = Number(lastState.track?.duration || 0);
      try {
        if (dur > 0 && isDemoPlaying(lastState)) {
          const pos = (el("demoSeek").value / 1000) * dur;
          render(await api.seek(pos));
        }
      } finally {
        demoSeeking = false;
      }
    });

    el("demoPrevBtn").addEventListener("click", async () => {
      if (el("demoPrevBtn").disabled) return;
      render(await api.previous_track());
    });
    el("demoNextBtn").addEventListener("click", async () => {
      if (el("demoNextBtn").disabled) return;
      render(await api.next_track());
    });

    el("demoVolume").addEventListener("input", async () => {
      el("demoVolVal").textContent = `${el("demoVolume").value}%`;
      render(await api.set_volume(el("demoVolume").value / 100));
    });
    el("demoVibration").addEventListener("input", async () => {
      el("demoVibVal").textContent = `${el("demoVibration").value}%`;
      render(await api.set_vibration(el("demoVibration").value / 100));
    });
    el("demoHighpass").addEventListener("input", async () => {
      const pct = Number(el("demoHighpass").value);
      el("demoHpVal").textContent = `${pct}%`;
      render(await api.set_highpass_hz(pctToCutoffHz(pct)));
    });
  }

  el("scanBtn").addEventListener("click", async () => {
    if (el("scanBtn").disabled) return;
    lastNearbySig = "";
    lastDevicesSig = "";
    render(await api.scan_bluetooth());
  });
  el("disconnectBtn").addEventListener("click", async () => {
    lastDevicesSig = "";
    render(await api.disconnect_device());
  });
  el("forgetAllBtn").addEventListener("click", async () => {
    if (el("forgetAllBtn").disabled) return;
    if (!window.confirm("Forget ALL paired Bluetooth devices from Windows on this tablet?")) return;
    lastDevicesSig = "";
    lastNearbySig = "";
    render(await api.forget_all_devices());
  });

  el("swapGigaportsBtn")?.addEventListener("click", async () => {
    const btn = el("swapGigaportsBtn");
    if (btn) btn.disabled = true;
    lastHardwareSig = "";
    lastSettingsSig = "";
    try {
      render(await api.swap_gigaport_roles());
    } finally {
      if (btn) btn.disabled = false;
    }
  });

}

// -------------------------------------------------------------------- boot --
async function boot() {
  bindApi();
  const verEl = el("brandVersion");
  if (verEl && typeof APP_VERSION === "string") verEl.textContent = `v${APP_VERSION}`;
  buildVibZoneRows("vibZoneRows");
  buildVibZoneRows("demoVibZoneRows");
  wire();
  initViz();
  render(await api.get_state());
  setInterval(async () => {
    if (!seeking && !demoSeeking) render(await api.get_state());
  }, 400);
}

window.addEventListener("pywebviewready", bindApi);
window.addEventListener("DOMContentLoaded", boot);
