// muve — Software update (Settings) and the Wi-Fi screen. Loaded after app.js.
// Python side: Api.update_* / Api.wifi_* in main.py; state arrives in get_state()
// as s.update, s.wifi, s.update_notice and s.app_version.

// ------------------------------------------------------------------ dialog --
function showDialog({ title, text, notes = "", buttons }) {
  return new Promise((resolve) => {
    el("modalTitle").textContent = title;
    el("modalText").textContent = text;
    const notesEl = el("modalNotes");
    notesEl.hidden = !notes;
    notesEl.textContent = notes || "";
    const actions = el("modalActions");
    actions.innerHTML = "";
    buttons.forEach((b) => {
      const btn = document.createElement("button");
      btn.className = b.primary ? "btn-primary" : "btn-ghost";
      btn.textContent = b.label;
      btn.addEventListener("click", () => {
        el("modal").hidden = true;
        resolve(b.value);
      });
      actions.appendChild(btn);
    });
    el("modal").hidden = false;
  });
}

// --------------------------------------------------------- software update --
let lastUpdate = null;
let lastUpdateSig = "";
let updateNoticeShown = false;

function renderUpdate(s) {
  if (s.app_version) {
    const v = el("brandVersion");
    if (v) v.textContent = `v${s.app_version}`;
  }
  if (s.update_notice && !updateNoticeShown) {
    updateNoticeShown = true;
    showUpdateNotice(s.update_notice);
  }
  const u = s.update;
  if (!u || !el("updateCard")) return;
  lastUpdate = u;
  const sig = JSON.stringify(u);
  if (sig === lastUpdateSig) return;
  lastUpdateSig = sig;

  let text = `Version ${u.version}`;
  let tone = "";
  let label = "Check for updates";
  let busy = false;
  let disabled = false;
  let ghost = false;
  let progress = null;
  let wifiBtn = false;
  switch (u.state) {
    case "checking":
      text = `Version ${u.version} · Checking for updates…`;
      label = "Checking…"; busy = true; disabled = true;
      break;
    case "up_to_date":
      text = `Version ${u.version} · Up to date`;
      tone = "ok"; label = "Check again";
      break;
    case "available":
      text = `Update available: version ${u.available}`;
      tone = "accent"; label = "Update now";
      break;
    case "downloading":
      text = `Downloading version ${u.available}… ${u.percent}%`;
      progress = u.percent; label = "Cancel"; ghost = true;
      break;
    case "installing":
      text = "Installing — Muvi will restart in a moment";
      tone = "accent"; progress = 100; label = "Installing…"; busy = true; disabled = true;
      break;
    case "offline":
      text = "No internet connection. Connect to Wi-Fi, then try again.";
      tone = "error"; label = "Try again"; wifiBtn = true;
      break;
    case "error":
      text = u.message || "Update failed";
      tone = "error"; label = "Try again";
      break;
    default:
      break;
  }
  el("updateCard").classList.toggle("available", u.state === "available" || u.state === "downloading");
  el("updateCard").classList.toggle("error", u.state === "error" || u.state === "offline");
  const status = el("updateStatus");
  status.textContent = text;
  status.className = `update-status ${tone}`;
  const btn = el("updateBtn");
  btn.disabled = disabled;
  btn.classList.toggle("busy", busy);
  btn.classList.toggle("ghosty", ghost);
  el("updateBtnLabel").textContent = label;
  el("updateWifiBtn").hidden = !wifiBtn;
  el("updateProgress").hidden = progress === null;
  el("updateBar").style.width = `${progress || 0}%`;
}

async function onUpdateButton() {
  const u = lastUpdate || {};
  if (u.state === "downloading") {
    render(await api.update_cancel());
    return;
  }
  if (u.state === "available") {
    const playing = Boolean(lastState && lastState.playing);
    const choice = await showDialog({
      title: `Update to version ${u.available}?`,
      text:
        "Muvi closes, installs the update and opens again. This takes about a minute." +
        (playing ? "\nPlayback stops during the update." : ""),
      notes: u.notes || "",
      buttons: [
        { label: "Not now", value: false },
        { label: "Update now", value: true, primary: true },
      ],
    });
    if (choice) render(await api.update_install());
    return;
  }
  render(await api.update_check());
}

async function showUpdateNotice(notice) {
  const ok = notice.kind === "updated";
  await showDialog({
    title: ok ? "Muvi updated" : "Update not installed",
    text: ok
      ? `Muvi has been updated to version ${notice.version}.`
      : `The update to version ${notice.version} could not be installed. The previous version is still running.`,
    buttons: [{ label: "OK", value: true, primary: true }],
  });
  if (api.clear_update_notice) render(await api.clear_update_notice());
}

// ------------------------------------------------------------------- Wi-Fi --
let lastWifi = null;
let lastWifiSig = "";
let wifiPasswordFor = null;   // ssid whose password field is open
let wifiShowPassword = false;

const escapeHtml = (t) =>
  String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function wifiBars(bars) {
  const on = (n) => (bars >= n ? "" : ' class="off"');
  return `<svg class="wifi-bars" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
    <path${on(4)} d="M2 8.5a15 15 0 0120 0"/><path${on(3)} d="M5 12a10 10 0 0114 0"/>
    <path${on(2)} d="M8.5 15.5a5 5 0 017 0"/><circle${on(1)} cx="12" cy="19" r="1.4" fill="currentColor" stroke="none"/></svg>`;
}

function wifiBusy(w) {
  return Boolean(w.scanning || w.connecting_ssid || w.disconnecting);
}

function renderWifi(s) {
  const w = s.wifi;
  if (!w || !el("screen-wifi")) return;
  lastWifi = w;
  const busy = wifiBusy(w);

  const scanBtn = el("wifiScanBtn");
  scanBtn.classList.toggle("scanning", Boolean(w.scanning));
  scanBtn.disabled = busy || !w.available;
  el("wifiScanLabel").textContent = w.scanning ? "Scanning…" : "Scan";

  const card = el("wifiStatusCard");
  const connected = Boolean(w.current_ssid);
  card.classList.toggle("online", connected && w.internet === "internet");
  card.classList.toggle("warn", connected && w.internet !== "internet");
  el("wifiStatusTitle").textContent = connected ? `Connected to ${w.current_ssid}` : "Not connected";
  el("wifiStatusSub").textContent = connected
    ? w.internet_text
    : w.available
      ? "Scan to see the networks nearby."
      : "Wi-Fi is not available on this tablet.";
  const disc = el("wifiDisconnectBtn");
  disc.hidden = !connected;
  disc.disabled = busy;
  disc.textContent = w.disconnecting ? "Disconnecting…" : "Disconnect";

  const msg = el("wifiMessage");
  const showMsg = Boolean(w.message) && !w.error_ssid;
  msg.hidden = !showMsg;
  msg.textContent = showMsg ? w.message : "";
  msg.className = `wifi-message ${w.message_kind || ""}`;

  renderWifiList(w);
}

function renderWifiList(w) {
  const list = el("wifiList");
  const sig = JSON.stringify([w.networks, w.connecting_ssid, w.error_ssid, w.message, w.scanning,
    w.scanned, w.disconnecting, wifiPasswordFor, wifiShowPassword]);
  if (sig === lastWifiSig) return;
  lastWifiSig = sig;

  // Keep what's typed (and the keyboard focus) when the list is rebuilt.
  const oldInput = el("wifiPassword");
  const typed = oldInput ? oldInput.value : "";
  const hadFocus = oldInput && document.activeElement === oldInput;

  list.innerHTML = "";
  const nets = w.networks || [];
  const busy = wifiBusy(w);
  if (!nets.length) {
    const li = document.createElement("li");
    li.className = "device empty" + (w.scanning ? " scanning" : "");
    const title = w.scanning ? "Scanning…" : w.scanned ? "No Wi-Fi networks found" : "No networks yet";
    const sub = w.scanning ? "Looking for networks nearby." : "Press Scan to see the networks nearby.";
    li.innerHTML = `<div class="device-meta"><span class="device-name">${title}</span><span class="device-sub">${sub}</span></div>`;
    list.appendChild(li);
    return;
  }

  nets.forEach((n) => {
    const isConnecting = n.ssid === w.connecting_ssid;
    const failed = n.ssid === w.error_ssid;
    const li = document.createElement("li");
    li.className = "device wifi-item" + (n.connected ? " connected" : "") + (isConnecting ? " connecting" : "");
    let sub = n.connected ? "Connected" : n.saved ? "Saved" : n.secured ? "Secured" : "Open";
    if (n.enterprise) sub = "Needs a username – not supported";
    if (isConnecting) sub = "Connecting…";
    if (failed) sub = w.message || "Could not connect";
    let action;
    if (n.connected) action = '<span class="badge">Connected</span>';
    else if (isConnecting) action = '<button class="btn-connect busy" disabled><span class="btn-spin"></span>Connecting…</button>';
    else action = `<button class="btn-connect" data-ssid="${escapeHtml(n.ssid)}" ${busy || n.enterprise ? "disabled" : ""}>Connect</button>`;
    li.innerHTML = `
      <div class="device-ico">${wifiBars(n.bars)}</div>
      <div class="device-meta">
        <span class="device-name">${escapeHtml(n.ssid)}</span>
        <span class="device-sub${failed ? " error" : ""}">${escapeHtml(sub)}</span>
      </div>
      <div class="device-action">${action}</div>`;
    if (wifiPasswordFor === n.ssid && !n.connected) {
      const pass = document.createElement("div");
      pass.className = "wifi-pass";
      pass.innerHTML = `
        <input id="wifiPassword" type="${wifiShowPassword ? "text" : "password"}" placeholder="Password"
               autocomplete="off" autocapitalize="off" spellcheck="false" />
        <button class="btn-ghost" id="wifiPassShow">${wifiShowPassword ? "Hide" : "Show"}</button>
        <button class="btn-ghost" id="wifiPassCancel">Cancel</button>
        <button class="btn-primary" id="wifiPassConnect" ${busy ? "disabled" : ""}>Connect</button>`;
      li.appendChild(pass);
    }
    list.appendChild(li);
  });

  list.querySelectorAll(".btn-connect[data-ssid]").forEach((b) =>
    b.addEventListener("click", () => onWifiConnect(b.dataset.ssid))
  );
  const input = el("wifiPassword");
  if (input) {
    input.value = typed;
    if (hadFocus || !typed) input.focus();
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submitWifiPassword(); });
    el("wifiPassShow").addEventListener("click", () => {
      wifiShowPassword = !wifiShowPassword;
      lastWifiSig = "";
      renderWifiList(lastWifi);
    });
    el("wifiPassCancel").addEventListener("click", () => {
      wifiPasswordFor = null;
      lastWifiSig = "";
      renderWifiList(lastWifi);
    });
    el("wifiPassConnect").addEventListener("click", submitWifiPassword);
  }
}

async function onWifiConnect(ssid) {
  const w = lastWifi || {};
  const net = (w.networks || []).find((n) => n.ssid === ssid);
  if (!net || wifiBusy(w)) return;
  // A password is needed for a new secured network, or when the saved one was rejected.
  if (net.secured && (!net.saved || w.error_ssid === ssid)) {
    wifiPasswordFor = ssid;
    wifiShowPassword = false;
    lastWifiSig = "";
    renderWifiList(w);
    return;
  }
  wifiPasswordFor = null;
  render(await api.wifi_connect(ssid, null));
}

async function submitWifiPassword() {
  const input = el("wifiPassword");
  const ssid = wifiPasswordFor;
  if (!input || !ssid || !input.value) return;
  const password = input.value;
  wifiPasswordFor = null;
  lastWifiSig = "";
  render(await api.wifi_connect(ssid, password));
}

function openWifiScreen() {
  navigateToScreen("wifi");
  const w = lastWifi;
  if (w && w.available && !w.scanned && !wifiBusy(w)) {
    Promise.resolve(api.wifi_scan()).then(render).catch(() => {});
  }
}

window.addEventListener("DOMContentLoaded", () => {
  el("updateBtn")?.addEventListener("click", onUpdateButton);
  el("updateWifiBtn")?.addEventListener("click", openWifiScreen);
  document.querySelector('.nav-item[data-screen="wifi"]')?.addEventListener("click", openWifiScreen);
  el("wifiScanBtn")?.addEventListener("click", async () => {
    wifiPasswordFor = null;
    render(await api.wifi_scan());
  });
  el("wifiDisconnectBtn")?.addEventListener("click", async () => {
    const w = lastWifi || {};
    const ok = await showDialog({
      title: "Disconnect Wi-Fi?",
      text: `Disconnect from ${w.current_ssid}? The network stays saved.`,
      buttons: [
        { label: "Cancel", value: false },
        { label: "Disconnect", value: true, primary: true },
      ],
    });
    if (ok) render(await api.wifi_disconnect());
  });
});

// ------------------------------------------- browser preview (MockApi) --
(() => {
  if (typeof MockApi === "undefined") return;
  const upd = {
    state: "idle", percent: 0, message: "", available: null, notes: "", can_install: true,
    version: typeof APP_VERSION === "string" ? APP_VERSION : "1.0.0",
  };
  const wifi = {
    available: true, scanning: false, scanned: false, networks: [], current_ssid: "",
    internet: "none", internet_text: "No internet access", connecting_ssid: null,
    disconnecting: false, message: "", message_kind: "info", error_ssid: null,
  };
  const NETS = [
    { ssid: "Gharieni Office", bars: 4, secured: true, enterprise: false, saved: true, connected: false },
    { ssid: "Gharieni Guest", bars: 3, secured: true, enterprise: false, saved: false, connected: false },
    { ssid: "Spa Lounge", bars: 2, secured: false, enterprise: false, saved: false, connected: false },
    { ssid: "Corp-Secure", bars: 2, secured: true, enterprise: true, saved: false, connected: false },
  ];
  const baseGet = MockApi.get_state;
  MockApi.get_state = async () => ({
    ...(await baseGet()),
    app_version: upd.version,
    update: { ...upd },
    update_notice: null,
    wifi: JSON.parse(JSON.stringify(wifi)),
  });
  const mark = () => wifi.networks.forEach((n) => { n.connected = n.ssid === wifi.current_ssid; });
  Object.assign(MockApi, {
    update_check: async () => {
      upd.state = "checking";
      setTimeout(() => {
        if (!wifi.current_ssid) { upd.state = "offline"; return; }
        upd.state = "available";
        upd.available = "1.0.2";
        upd.notes = "- Software update from Settings\n- New Wi-Fi screen\n- Restarts automatically after a crash";
      }, 900);
      return MockApi.get_state();
    },
    update_install: async () => {
      upd.state = "downloading";
      upd.percent = 0;
      const t = setInterval(() => {
        if (upd.state !== "downloading") { clearInterval(t); return; }
        upd.percent = Math.min(100, upd.percent + 9);
        if (upd.percent >= 100) { clearInterval(t); upd.state = "installing"; }
      }, 300);
      return MockApi.get_state();
    },
    update_cancel: async () => { upd.state = "available"; return MockApi.get_state(); },
    clear_update_notice: async () => MockApi.get_state(),
    wifi_scan: async () => {
      wifi.scanning = true;
      setTimeout(() => {
        wifi.scanning = false;
        wifi.scanned = true;
        wifi.networks = JSON.parse(JSON.stringify(NETS));
        mark();
      }, 900);
      return MockApi.get_state();
    },
    wifi_connect: async (ssid, password) => {
      wifi.connecting_ssid = ssid;
      wifi.error_ssid = null;
      wifi.message = "";
      setTimeout(() => {
        wifi.connecting_ssid = null;
        if (ssid === "Gharieni Guest" && password !== "guest1234") {
          wifi.error_ssid = ssid;
          wifi.message = "Wrong password";
          wifi.message_kind = "error";
          return;
        }
        wifi.current_ssid = ssid;
        wifi.internet = "internet";
        wifi.internet_text = "Internet OK";
        wifi.message = `Connected to ${ssid}`;
        wifi.message_kind = "ok";
        const net = wifi.networks.find((n) => n.ssid === ssid);
        if (net) net.saved = true;
        mark();
      }, 1200);
      return MockApi.get_state();
    },
    wifi_disconnect: async () => {
      wifi.disconnecting = true;
      setTimeout(() => {
        wifi.disconnecting = false;
        wifi.current_ssid = "";
        wifi.internet = "none";
        wifi.internet_text = "No internet access";
        wifi.message = "";
        mark();
      }, 700);
      return MockApi.get_state();
    },
  });
})();
