// Haptics calibration (Settings): per-channel level, on/off and body position, plus a
// step-by-step sheet that pulses one transducer at a time. Same model as the Pi app.
// Uses `api`, `el` and `render` from app.js.

const CAL_N = 8;
let calIdx = null;        // sheet position 0..8 (8 = review), null = sheet closed
let calDragging = false;  // don't overwrite a slider while the user drags it
let calListBuilt = false;

const calFmt = (db) => `${Number(db) > 0 ? "+" : ""}${Number(db).toFixed(1)} dB`;
const calSideClass = (side) => (side === "Left" ? "cal-left" : side === "Right" ? "cal-right" : "cal-center");

async function calCall(name, ...args) {
  if (!api || typeof api[name] !== "function") return;
  try {
    render(await api[name](...args));
  } catch (err) {
    console.warn(`[calibration] ${name} failed`, err);
  }
}

function calThrottle(fn, ms) {
  let timer = null;
  let pending = null;
  return (...args) => {
    pending = args;
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      fn(...pending);
    }, ms);
  };
}

function bindCalSlider(input, onValue, onLabel) {
  const send = calThrottle(onValue, 90);
  input.addEventListener("pointerdown", () => { calDragging = true; });
  input.addEventListener("pointerup", () => { calDragging = false; onValue(Number(input.value)); });
  input.addEventListener("pointercancel", () => { calDragging = false; });
  input.addEventListener("input", () => {
    onLabel(Number(input.value));
    send(Number(input.value));
  });
}

// ---------------------------------------------------------------- Settings card --
function buildCalList() {
  const box = el("calList");
  if (!box || calListBuilt) return;
  box.innerHTML = "";
  for (let n = 1; n <= CAL_N; n++) {
    const row = document.createElement("div");
    row.className = "cal-row";
    row.id = `calRow${n}`;
    row.innerHTML = `
      <div class="cal-num">${n}</div>
      <div class="cal-label"><span class="cal-part"></span><small class="cal-sideTxt"></small></div>
      <input type="range" class="range" min="-30" max="12" step="0.5" aria-label="Channel ${n} level" />
      <span class="cal-dbv"></span>
      <button type="button" class="btn-ghost sm cal-test">Test</button>
      <label class="cal-switch" title="Use this channel"><input type="checkbox" /><span></span></label>`;
    box.appendChild(row);
    const range = row.querySelector(".range");
    const dbv = row.querySelector(".cal-dbv");
    bindCalSlider(range, (v) => calCall("set_haptic_channel", n, v, null, null, null), (v) => { dbv.textContent = calFmt(v); });
    row.querySelector(".cal-switch input").addEventListener("change", (e) =>
      calCall("set_haptic_channel", n, null, e.target.checked, null, null));
    row.querySelector(".cal-test").addEventListener("click", () => {
      const s = typeof lastState !== "undefined" ? lastState : null;
      calCall("haptic_test", s && s.calibrating === n ? null : n);
    });
  }
  calListBuilt = true;
}

function renderCalCard(s) {
  const cal = s.haptic_cal;
  if (!cal) return;
  buildCalList();
  const master = el("calMaster");
  if (master && !calDragging) master.value = cal.master_gain_db;
  if (el("calMasterVal")) el("calMasterVal").textContent = calFmt(master ? master.value : cal.master_gain_db);
  for (let n = 1; n <= CAL_N; n++) {
    const row = el(`calRow${n}`);
    if (!row) continue;
    const [part, side] = cal.channel_labels[n - 1];
    const on = Boolean(cal.channel_enabled[n - 1]);
    row.querySelector(".cal-part").textContent = part;
    const st = row.querySelector(".cal-sideTxt");
    st.textContent = side;
    st.className = `cal-sideTxt ${calSideClass(side)}`;
    const range = row.querySelector(".range");
    if (!calDragging) range.value = cal.channel_gain_db[n - 1];
    row.querySelector(".cal-dbv").textContent = calFmt(range.value);
    row.querySelector(".cal-switch input").checked = on;
    row.classList.toggle("off", !on);
    row.classList.toggle("testing", s.calibrating === n);
    const test = row.querySelector(".cal-test");
    test.textContent = s.calibrating === n ? "Stop" : "Test";
    test.disabled = Boolean(s.calibration_busy);
  }
  const saveState = el("calSaveState");
  if (saveState) {
    saveState.textContent = s.haptic_cal_dirty ? "Unsaved changes" : "All changes saved";
    saveState.classList.toggle("dirty", Boolean(s.haptic_cal_dirty));
  }
  if (el("calSaveBtn")) el("calSaveBtn").disabled = !s.haptic_cal_dirty;
  if (el("calRevertBtn")) el("calRevertBtn").disabled = !s.haptic_cal_dirty;
  const err = el("calError");
  if (err) {
    err.hidden = !s.haptic_cal_error;
    err.textContent = s.haptic_cal_error || "";
  }
  if (el("calStartBtn")) el("calStartBtn").disabled = Boolean(s.calibration_busy);
}

// ------------------------------------------------------------- calibration sheet --
function calHighlight(part, side) {
  const hit = el("cbHit");
  if (!hit) return;
  let ids;
  if (part === "Arms") ids = side === "Right" ? ["cb-ArmR"] : side === "Left" ? ["cb-ArmL"] : ["cb-ArmL", "cb-ArmR"];
  else if (part === "Legs") ids = ["cb-Thighs", "cb-Calves"];
  else ids = [`cb-${part}`];
  const boxes = ids.map((id) => document.getElementById(id)).filter(Boolean).map((e) => e.getBBox());
  if (!boxes.length) { hit.setAttribute("width", 0); return; }
  let x0 = Math.min(...boxes.map((b) => b.x));
  let x1 = Math.max(...boxes.map((b) => b.x + b.width));
  const y0 = Math.min(...boxes.map((b) => b.y));
  const y1 = Math.max(...boxes.map((b) => b.y + b.height));
  if (part !== "Arms" && side !== "Center") {
    const mid = (x0 + x1) / 2;
    if (side === "Left") x1 = mid; else x0 = mid;
  }
  hit.setAttribute("x", x0); hit.setAttribute("y", y0);
  hit.setAttribute("width", x1 - x0); hit.setAttribute("height", y1 - y0);
}

function calFillSelect(sel, items, value) {
  if (!sel) return;
  const key = items.join("|");
  if (sel.dataset.items !== key) {
    sel.innerHTML = items.map((i) => `<option>${i}</option>`).join("");
    sel.dataset.items = key;
  }
  if (document.activeElement !== sel) sel.value = value;
}

function renderCalSheet(s) {
  const sheet = el("calSheet");
  if (!sheet) return;
  sheet.hidden = calIdx === null;
  if (calIdx === null) return;
  const cal = s.haptic_cal;
  const done = calIdx >= CAL_N;
  el("calStepBody").hidden = done;
  el("calDoneBody").hidden = !done;
  el("calProgress").style.width = `${(Math.min(calIdx, CAL_N) / CAL_N) * 100}%`;
  el("calStep").textContent = done ? "Review" : `Channel ${calIdx + 1} of ${CAL_N}`;
  el("calPrevBtn").disabled = calIdx === 0;
  el("calNextBtn").textContent = done ? "Save and finish" : calIdx === CAL_N - 1 ? "Finish" : "Next channel";

  if (done) {
    el("calSummary").innerHTML = cal.channel_labels.map(([part, side], i) => `
      <div class="cal-sum-row">
        <span><b>${i + 1}</b> · ${part} <span class="${calSideClass(side)}">${side}</span>${cal.channel_enabled[i] ? "" : ' <span class="cal-center">(off)</span>'}</span>
        <span class="cal-dbv">${calFmt(cal.channel_gain_db[i])}</span>
      </div>`).join("");
    return;
  }
  const n = calIdx + 1;
  const [part, side] = cal.channel_labels[calIdx];
  el("calWhere").textContent = part;
  const sideEl = el("calSide");
  sideEl.textContent = side === "Center" ? "center" : `${side.toUpperCase()} side`;
  sideEl.className = `cal-side ${calSideClass(side)}`;
  const pulsing = s.calibrating === n;
  el("calPulse").classList.toggle("waiting", !pulsing);
  el("calPulseText").textContent = pulsing
    ? `Vibrating now on channel ${n}`
    : s.calibration_busy ? "Opening the Gigaport…" : `Waiting for channel ${n}…`;
  el("calInstruction").textContent =
    `Sit or lie on the bed. You should feel a pulsing vibration on your ${part.toLowerCase()}` +
    (side === "Center" ? "." : `, ${side.toLowerCase()} side.`) +
    " Move the slider until it feels right — it applies instantly.";
  calHighlight(part, side);
  const slider = el("calSlider");
  if (!calDragging) slider.value = cal.channel_gain_db[calIdx];
  el("calDb").textContent = calFmt(slider.value);
  calFillSelect(el("calPartSel"), cal.parts || [], part);
  calFillSelect(el("calSideSel"), cal.sides || [], side);
  el("calEnabled").checked = Boolean(cal.channel_enabled[calIdx]);
}

function calGoStep() {
  calCall("haptic_test", calIdx < CAL_N ? calIdx + 1 : null);
}

function wireCalibration() {
  bindCalSlider(el("calMaster"), (v) => calCall("set_haptic_master", v), (v) => { el("calMasterVal").textContent = calFmt(v); });
  bindCalSlider(el("calSlider"), (v) => calCall("set_haptic_channel", calIdx + 1, v, null, null, null), (v) => { el("calDb").textContent = calFmt(v); });
  el("calSaveBtn").addEventListener("click", () => calCall("save_haptic_calibration"));
  el("calRevertBtn").addEventListener("click", () => calCall("revert_haptic_calibration"));
  el("calStartBtn").addEventListener("click", () => { calIdx = 0; calGoStep(); });
  el("calExitBtn").addEventListener("click", () => { calIdx = null; calCall("haptic_test", null); });
  el("calPrevBtn").addEventListener("click", () => { calIdx = Math.max(0, calIdx - 1); calGoStep(); });
  el("calNextBtn").addEventListener("click", async () => {
    if (calIdx >= CAL_N) {
      calIdx = null;
      await calCall("haptic_test", null);
      await calCall("save_haptic_calibration");
      return;
    }
    calIdx += 1;
    calGoStep();
  });
  el("calPartSel").addEventListener("change", (e) => calCall("set_haptic_channel", calIdx + 1, null, null, e.target.value, null));
  el("calSideSel").addEventListener("change", (e) => calCall("set_haptic_channel", calIdx + 1, null, null, null, e.target.value));
  el("calEnabled").addEventListener("change", (e) => calCall("set_haptic_channel", calIdx + 1, null, e.target.checked, null, null));
}

function renderCalibration(s) {
  if (!s || !s.haptic_cal) return;
  renderCalCard(s);
  renderCalSheet(s);
}

window.addEventListener("DOMContentLoaded", wireCalibration);
