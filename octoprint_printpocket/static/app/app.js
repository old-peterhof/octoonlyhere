/* PrintPocket web app. Plain JS, no build step. */
(() => {
  "use strict";

  const PLUGIN = "/plugin/printpocket/";
  // OctoPrint may live under a sub path behind a reverse proxy.
  const ROOT = location.pathname.split(PLUGIN)[0] + "/";
  const KEY_STORE = "printpocket.apikey";
  const LIVE_STORE = "printpocket.live";

  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
  };

  let apiKey = store.get(KEY_STORE);
  let info = null;
  let printer = null;     // last /api/printer (null when not operational)
  let job = null;         // last /api/job
  let connection = null;  // last /api/connection
  let pollTimer = null;
  let snapTimer = null;
  let files = [];
  let activeTab = "printer";

  // ---------- API ----------

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Api-Key": apiKey }, opts.headers || {});
    let body = opts.body;
    if (body !== undefined && typeof body !== "string") {
      body = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(ROOT + path, {
      method: opts.method || "GET",
      headers,
      body,
      cache: "no-store",
      credentials: "omit",
    });
    if (res.status === 401) {
      // Our endpoints answer 401 for a missing, wrong or revoked key.
      signOut("That key no longer works. Connect again.");
      throw new ApiError(401, "Not signed in");
    }
    if (res.status === 403) throw new ApiError(403, "Your OctoPrint user isn't allowed to do that");
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || j.description || msg; } catch (e) { /* not json */ }
      throw new ApiError(res.status, msg);
    }
    if (res.status === 204) return null;
    const type = res.headers.get("Content-Type") || "";
    return type.includes("json") ? res.json() : res.text();
  }

  const plugin = (p, opts) => api(PLUGIN.slice(1) + p, opts);

  // ---------- helpers ----------

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 2600);
  }

  function fmtDur(s) {
    if (s == null || isNaN(s)) return "-";
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m`;
    return `${s}s`;
  }

  function fmtClock(secondsFromNow) {
    if (secondsFromNow == null || isNaN(secondsFromNow)) return "-";
    const d = new Date(Date.now() + secondsFromNow * 1000);
    const sameDay = d.toDateString() === new Date().toDateString();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return sameDay ? time : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
  }

  function fmtSize(b) {
    if (!b) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  function fmtAgo(ts) {
    if (!ts) return "";
    const s = Date.now() / 1000 - ts;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  // "Benchy_0.2mm_PLA.gcode" -> "Benchy 0.2mm PLA" so long names wrap at words.
  function stripExt(name) {
    return (name || "").replace(/\.(gcode|gco|g|bgcode)$/i, "").replace(/_/g, " ");
  }

  function confirmDlg(title, body, okLabel = "OK", danger = false) {
    return new Promise((resolve) => {
      const d = $("confirm");
      $("confirm-title").textContent = title;
      $("confirm-body").textContent = body;
      const yes = $("confirm-yes");
      yes.textContent = okLabel;
      yes.className = "btn " + (danger ? "danger" : "primary");
      d.onclose = () => resolve(d.returnValue === "yes");
      d.returnValue = "";
      d.showModal();
    });
  }

  function tempDlg(title, current) {
    return new Promise((resolve) => {
      const d = $("tempdlg");
      $("temp-title").textContent = title;
      const input = $("temp-input");
      input.value = current ? Math.round(current) : "";
      d.onclose = () => {
        if (d.returnValue !== "yes") return resolve(null);
        const v = parseInt(input.value, 10);
        resolve(isNaN(v) ? null : Math.max(0, Math.min(350, v)));
      };
      d.returnValue = "";
      d.showModal();
      setTimeout(() => input.focus(), 50);
    });
  }

  function isIOS() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }

  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  }

  // ---------- sign in ----------

  let loginPoll = null;

  function showLogin(msg) {
    stopPolling();
    $("app").hidden = true;
    $("login").hidden = false;
    $("login-msg").textContent = msg || "Connect this device to OctoPrint.";
    $("login-start").hidden = false;
    $("login-wait").hidden = true;
  }

  function stopLoginPoll() {
    if (loginPoll) clearInterval(loginPoll);
    loginPoll = null;
  }

  async function startLogin() {
    stopLoginPoll();
    let res;
    try {
      res = await fetch(ROOT + "plugin/appkeys/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: "PrintPocket" }),
        credentials: "omit",
      });
    } catch (e) {
      return showLogin("Can't reach OctoPrint from here.");
    }
    if (res.status === 429) return showLogin("Too many attempts. Wait a minute and try again.");
    if (res.status !== 201) return showLogin("This OctoPrint doesn't allow app keys. Use an API key below.");
    const { app_token, auth_dialog } = await res.json();

    $("login-start").hidden = true;
    $("login-wait").hidden = false;
    $("login-approve").href = auth_dialog;
    $("login-msg").textContent = "Waiting for approval…";

    loginPoll = setInterval(async () => {
      let r;
      try {
        r = await fetch(ROOT + "plugin/appkeys/request/" + encodeURIComponent(app_token), { credentials: "omit", cache: "no-store" });
      } catch (e) { return; }
      if (r.status === 202) return;
      stopLoginPoll();
      if (r.status === 200) {
        const { api_key } = await r.json();
        await useKey(api_key);
      } else {
        showLogin("The request was denied or timed out. Try again.");
      }
    }, 1000);
  }

  async function useKey(key) {
    apiKey = (key || "").trim();
    if (!apiKey) return;
    try {
      info = await plugin("api/info");
    } catch (e) {
      apiKey = null;
      return showLogin(e.status === 403 || e.status === 401 ? "That key was not accepted." : "Couldn't load PrintPocket from OctoPrint.");
    }
    store.set(KEY_STORE, apiKey);
    startApp();
  }

  function signOut(msg) {
    apiKey = null;
    info = null;
    store.del(KEY_STORE);
    showLogin(msg);
  }

  // When the phone comes back from approving in another window the
  // pending request may have expired. Polling resumes on its own, and a
  // 404 lands the user back on the Connect button.

  // ---------- app ----------

  async function startApp() {
    $("login").hidden = true;
    $("app").hidden = false;
    if (!info) {
      try { info = await plugin("api/info"); }
      catch (e) { if (!apiKey) return; toast("Couldn't load printer info"); }
    }
    if (info) {
      $("printer-name").textContent = info.printer;
      document.title = info.printer;
      $("about").textContent = `Signed in as ${info.user}. PrintPocket ${info.version}.`;
      setupCamera();
      renderPresets();
    }
    switchTab(activeTab);
    refresh();
    setupPushUI();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
    stopCamera();
  }

  function schedule() {
    clearTimeout(pollTimer);
    if (document.hidden || !apiKey) return;
    const busy = printer && printer.state && printer.state.flags && (printer.state.flags.printing || printer.state.flags.paused);
    pollTimer = setTimeout(refresh, busy ? 2000 : 5000);
  }

  async function refresh() {
    if (!apiKey) return;
    try {
      const [c, j] = await Promise.all([api("api/connection"), api("api/job")]);
      connection = c;
      job = j;
      try {
        printer = await api("api/printer?exclude=sd");
      } catch (e) {
        if (e.status === 409) printer = null; else throw e;
      }
      render();
    } catch (e) {
      if (!apiKey) return;
      setState("Unreachable", "offline");
    }
    schedule();
  }

  function setState(text, cls) {
    const s = $("state");
    s.textContent = text;
    s.className = "pill " + (cls || "");
  }

  function render() {
    const flags = (printer && printer.state && printer.state.flags) || {};
    const stateText = (printer && printer.state && printer.state.text) || (connection && connection.current && connection.current.state) || "Offline";

    let cls = "";
    if (flags.error || flags.closedOrError) cls = "error";
    else if (flags.paused || flags.pausing) cls = "paused";
    else if (flags.printing || flags.cancelling) cls = "printing";
    else if (flags.ready || flags.operational) cls = "ready";
    else cls = "offline";
    setState(stateText.replace(/^Offline after error.*$/i, "Error"), cls);

    // offline card
    const operational = !!printer && flags.operational;
    $("offline").hidden = operational;
    $("temps-card").hidden = !operational;
    if (!operational) {
      const err = connection && connection.current && connection.current.state;
      $("offline-msg").textContent = err && /error/i.test(err) ? err : "The printer is not connected to OctoPrint.";
      $("connect").hidden = !(info && info.permissions.connection);
    }

    // job
    const p = (job && job.progress) || {};
    const f = (job && job.job && job.job.file) || {};
    const active = flags.printing || flags.paused || flags.pausing || flags.cancelling;
    const pct = p.completion != null ? Math.floor(p.completion) : 0;
    $("file").textContent = f.display ? stripExt(f.display) : "No file selected";
    $("pct").textContent = (active || pct >= 100) ? `${pct}%` : (f.display ? "Ready" : "Idle");
    $("pct").style.fontSize = (active || pct >= 100) ? "" : "18px";
    setRing(active || pct >= 100 ? pct : 0);
    $("elapsed").textContent = active ? fmtDur(p.printTime) : "-";
    $("left").textContent = active ? fmtDur(p.printTimeLeft) : (f.display && job.job.estimatedPrintTime ? "~" + fmtDur(job.job.estimatedPrintTime) : "-");
    $("eta").textContent = active && p.printTimeLeft != null ? fmtClock(p.printTimeLeft) : "-";

    const canControl = info && info.permissions.control;
    const pause = $("pause");
    pause.disabled = !canControl || !(flags.printing || flags.paused);
    pause.textContent = flags.paused ? "Resume" : "Pause";
    pause.className = "btn" + (flags.paused ? " primary" : "");
    $("cancel").disabled = !canControl || !(flags.printing || flags.paused);

    renderTemps();
  }

  function setRing(pct) {
    const r = 52;
    const c = 2 * Math.PI * r;
    const bar = $("ring-bar");
    bar.style.strokeDasharray = `${c}`;
    bar.style.strokeDashoffset = `${c * (1 - Math.max(0, Math.min(100, pct)) / 100)}`;
  }

  function renderTemps() {
    const box = $("temps");
    const t = (printer && printer.temperature) || {};
    const keys = Object.keys(t).filter((k) => /^(tool\d+|bed|chamber)$/.test(k)).sort((a, b) => {
      const order = (k) => (k.startsWith("tool") ? 0 : k === "bed" ? 1 : 2);
      return order(a) - order(b) || a.localeCompare(b);
    });
    const tools = keys.filter((k) => k.startsWith("tool")).length;
    box.innerHTML = "";
    for (const k of keys) {
      const v = t[k] || {};
      const label = k === "bed" ? "Bed" : k === "chamber" ? "Chamber" : tools > 1 ? `Hotend ${+k.slice(4) + 1}` : "Hotend";
      const btn = document.createElement("button");
      btn.className = "temp" + (v.target > 0 && Math.abs(v.actual - v.target) > 2 ? " heating" : "");
      btn.innerHTML = `<div class="k"></div><div class="v"></div><div class="t"></div>`;
      btn.children[0].textContent = label;
      btn.children[1].textContent = v.actual != null ? `${Math.round(v.actual)}°` : "-";
      btn.children[2].textContent = v.target > 0 ? `→ ${Math.round(v.target)}°` : "off";
      btn.onclick = () => setTemp(k, label, v.target);
      box.appendChild(btn);
    }
  }

  function renderPresets() {
    const box = $("presets");
    box.innerHTML = "";
    if (!info || !info.permissions.control) return;
    for (const p of info.temperatureProfiles || []) {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = p.name;
      b.onclick = () => applyPreset(p.extruder, p.bed, p.name);
      box.appendChild(b);
    }
    const off = document.createElement("button");
    off.className = "chip";
    off.textContent = "Cool down";
    off.onclick = () => applyPreset(0, 0, "Cool down");
    box.appendChild(off);
  }

  async function setTemp(key, label, current) {
    if (!info || !info.permissions.control) return;
    const v = await tempDlg(`${label} target`, current);
    if (v == null) return;
    try {
      if (key === "bed") await api("api/printer/bed", { method: "POST", body: { command: "target", target: v } });
      else if (key === "chamber") await api("api/printer/chamber", { method: "POST", body: { command: "target", target: v } });
      else await api("api/printer/tool", { method: "POST", body: { command: "target", targets: { [key]: v } } });
      toast(v ? `${label} → ${v}°` : `${label} off`);
      refresh();
    } catch (e) { toast(e.message || "Couldn't set temperature"); }
  }

  async function applyPreset(extruder, bed, name) {
    const t = (printer && printer.temperature) || {};
    const targets = {};
    Object.keys(t).filter((k) => k.startsWith("tool")).forEach((k) => { targets[k] = extruder; });
    try {
      if (Object.keys(targets).length) await api("api/printer/tool", { method: "POST", body: { command: "target", targets } });
      if (t.bed) await api("api/printer/bed", { method: "POST", body: { command: "target", target: bed } });
      toast(name === "Cool down" ? "Heaters off" : `Preheating ${name}`);
      refresh();
    } catch (e) { toast(e.message || "Couldn't set temperatures"); }
  }

  // ---------- job controls ----------

  $("pause").onclick = async () => {
    const paused = printer && printer.state.flags.paused;
    try {
      await api("api/job", { method: "POST", body: { command: "pause", action: paused ? "resume" : "pause" } });
      toast(paused ? "Resuming" : "Pausing");
      setTimeout(refresh, 600);
    } catch (e) { toast(e.message || "Couldn't do that"); }
  };

  $("cancel").onclick = async () => {
    const name = stripExt(job && job.job && job.job.file && job.job.file.display) || "this print";
    if (!(await confirmDlg("Cancel print?", `${name} will stop and can't be resumed.`, "Cancel print", true))) return;
    try {
      await api("api/job", { method: "POST", body: { command: "cancel" } });
      toast("Cancelling");
      setTimeout(refresh, 600);
    } catch (e) { toast(e.message || "Couldn't cancel"); }
  };

  $("connect").onclick = async () => {
    try {
      await api("api/connection", { method: "POST", body: { command: "connect" } });
      toast("Connecting…");
      setTimeout(refresh, 2500);
    } catch (e) { toast(e.message || "Couldn't connect"); }
  };

  // ---------- camera ----------

  let cam = null;
  let live = store.get(LIVE_STORE) !== "0";

  function setupCamera() {
    const usable = (info.webcams || []).filter((w) => w.stream || w.canSnapshot);
    cam = usable.find((w) => w.stream) || usable[0] || null;
    $("cam").hidden = !cam;
    if (!cam) return;
    $("cam").classList.toggle("r43", cam.ratio === "4:3");
    const tf = [];
    if (cam.rotate90) tf.push("rotate(-90deg)");
    if (cam.flipH) tf.push("scaleX(-1)");
    if (cam.flipV) tf.push("scaleY(-1)");
    $("cam-img").style.transform = tf.join(" ");
    $("cam-live").hidden = !(cam.stream && cam.canSnapshot);
    startCamera();
  }

  function streamUrl() {
    const s = cam.stream;
    if (/^https?:\/\//i.test(s)) return s;
    return ROOT + s.replace(/^\//, "");
  }

  function snapshotUrl() {
    return `${ROOT}plugin/printpocket/api/snapshot?webcam=${encodeURIComponent(cam.name)}&apikey=${encodeURIComponent(apiKey)}&t=${Date.now()}`;
  }

  function startCamera() {
    stopCamera();
    if (!cam || activeTab !== "printer" || document.hidden) return;
    const img = $("cam-img");
    $("cam-msg").hidden = true;
    const useLive = cam.stream && (live || !cam.canSnapshot);
    $("cam-live").classList.toggle("on", !!useLive);
    $("cam-live").textContent = useLive ? "Live" : "Snapshot";
    if (useLive) {
      img.onerror = () => {
        if (cam.canSnapshot) { live = false; startCamera(); }
        else $("cam-msg").hidden = false;
      };
      img.src = streamUrl();
    } else if (cam.canSnapshot) {
      img.onerror = () => { $("cam-msg").hidden = false; };
      img.onload = () => { $("cam-msg").hidden = true; };
      const tick = () => {
        img.src = snapshotUrl();
        snapTimer = setTimeout(tick, 5000);
      };
      tick();
    } else {
      $("cam-msg").hidden = false;
    }
  }

  function stopCamera() {
    clearTimeout(snapTimer);
    snapTimer = null;
    const img = $("cam-img");
    img.onerror = null;
    // Dropping src closes the MJPEG connection.
    img.removeAttribute("src");
  }

  $("cam-live").onclick = (ev) => {
    ev.stopPropagation();
    live = !live;
    store.set(LIVE_STORE, live ? "1" : "0");
    startCamera();
  };

  $("cam-full").onclick = (ev) => {
    ev.stopPropagation();
    const el = $("cam");
    if (document.fullscreenElement) return document.exitFullscreen();
    if (el.requestFullscreen) el.requestFullscreen().catch(() => el.classList.toggle("full"));
    else el.classList.toggle("full"); // iOS Safari has no element fullscreen
  };
  $("cam").onclick = () => { if ($("cam").classList.contains("full")) $("cam").classList.remove("full"); };

  // ---------- files ----------

  async function loadFiles() {
    const list = $("files");
    list.innerHTML = '<li class="empty">Loading…</li>';
    try {
      const r = await api("api/files?recursive=true");
      const out = [];
      const walk = (items) => {
        for (const it of items || []) {
          if (it.type === "folder") walk(it.children);
          else if (it.type === "machinecode") out.push(it);
        }
      };
      walk(r.files);
      out.sort((a, b) => (b.date || 0) - (a.date || 0));
      files = out;
      renderFiles();
    } catch (e) {
      list.innerHTML = '<li class="empty">Couldn\'t load files.</li>';
    }
  }

  function renderFiles() {
    const q = $("search").value.trim().toLowerCase();
    const list = $("files");
    list.innerHTML = "";
    const shown = files.filter((f) => !q || (f.path || f.name).toLowerCase().includes(q));
    if (!shown.length) {
      list.innerHTML = `<li class="empty">${files.length ? "No matches." : "No files on OctoPrint yet."}</li>`;
      return;
    }
    for (const f of shown.slice(0, 200)) {
      const li = document.createElement("li");
      const est = f.gcodeAnalysis && f.gcodeAnalysis.estimatedPrintTime;
      const last = f.prints && f.prints.last;
      li.innerHTML = '<div class="name"></div><div class="meta"></div>';
      li.children[0].textContent = stripExt(f.display || f.name);
      const meta = li.children[1];
      const parts = [];
      if (f.path && f.path.includes("/")) parts.push(f.path.slice(0, f.path.lastIndexOf("/")));
      if (est) parts.push("~" + fmtDur(est));
      if (f.size) parts.push(fmtSize(f.size));
      if (f.date) parts.push(fmtAgo(f.date));
      meta.textContent = parts.join(" · ");
      if (last) {
        const s = document.createElement("span");
        s.className = last.success ? "ok" : "bad";
        s.textContent = last.success ? "  ✓ printed" : "  ✕ last failed";
        meta.appendChild(s);
      }
      li.onclick = () => printFile(f);
      list.appendChild(li);
    }
  }

  async function printFile(f) {
    const flags = (printer && printer.state && printer.state.flags) || {};
    if (!info || !info.permissions.print) return toast("Your OctoPrint user can't start prints");
    if (!printer || !flags.ready) {
      return toast(flags.printing || flags.paused ? "A print is already running" : "Printer isn't ready");
    }
    const est = f.gcodeAnalysis && f.gcodeAnalysis.estimatedPrintTime;
    const body = `Make sure the bed is clear.${est ? ` Estimated ${fmtDur(est)}.` : ""}`;
    if (!(await confirmDlg(`Print ${stripExt(f.display || f.name)}?`, body, "Start print"))) return;
    try {
      const path = f.path.split("/").map(encodeURIComponent).join("/");
      await api(`api/files/${f.origin || "local"}/${path}`, { method: "POST", body: { command: "select", print: true } });
      toast("Print started");
      switchTab("printer");
      setTimeout(refresh, 800);
    } catch (e) { toast(e.message || "Couldn't start the print"); }
  }

  $("search").addEventListener("input", renderFiles);

  // ---------- push ----------

  const EVENT_LABELS = {
    started: ["Print started", ""],
    done: ["Print finished", ""],
    failed: ["Print failed", ""],
    cancelled: ["Print cancelled", ""],
    paused: ["Print paused", ""],
    filament: ["Filament change", "When the printer asks for new filament (M600)"],
    progress: ["Progress updates", ""],
    error: ["Printer errors", "Errors and unexpected disconnects"],
  };

  let swReg = null;
  let pushSub = null;

  async function registerSW() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return null;
    try {
      swReg = await navigator.serviceWorker.register("sw.js", { scope: "./" });
      return swReg;
    } catch (e) {
      return null;
    }
  }

  function pushSupported() {
    return window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  async function setupPushUI() {
    const note = $("push-note");
    const enable = $("push-enable");
    enable.hidden = true;
    $("push-events").hidden = true;
    $("push-actions").hidden = true;
    note.className = "note";

    if (!window.isSecureContext) {
      note.className = "note warn";
      note.textContent = "Notifications need an https:// address. Set up Tailscale HTTPS on the Pi (see the README) and open PrintPocket from that address.";
      return;
    }
    if (isIOS() && !isStandalone()) {
      note.className = "note warn";
      note.textContent = "On iPhone, add PrintPocket to your Home Screen first (Share, then Add to Home Screen), then open it from the icon to turn on notifications.";
      return;
    }
    if (!pushSupported() || !info || !info.vapidPublicKey) {
      note.textContent = "This browser can't receive push notifications.";
      return;
    }
    const reg = swReg || (await registerSW());
    if (!reg) { note.textContent = "Couldn't start the background worker."; return; }
    await navigator.serviceWorker.ready;
    pushSub = await reg.pushManager.getSubscription();

    let state = { subscribed: false, events: info.events.defaults };
    if (pushSub) {
      try { state = await plugin("api/push?endpoint=" + encodeURIComponent(pushSub.endpoint)); } catch (e) { /* keep defaults */ }
      if (!state.subscribed) {
        // Browser has a subscription the Pi forgot about. Re-register it.
        try { state = await savePush(info.events.defaults); } catch (e) { /* ignore */ }
      }
    }

    if (Notification.permission === "denied") {
      note.className = "note warn";
      note.textContent = "Notifications are blocked for this app. Allow them in your phone's settings.";
      return;
    }

    if (!pushSub || !state.subscribed) {
      note.textContent = `Get a notification on this ${isIOS() ? "iPhone" : "device"} when a print finishes, fails or needs you.`;
      enable.hidden = false;
      return;
    }

    note.textContent = "This device gets notifications for:";
    renderEventToggles(state.events);
    $("push-events").hidden = false;
    $("push-actions").hidden = false;
  }

  function renderEventToggles(selected) {
    const box = $("push-events");
    box.innerHTML = "";
    for (const ev of info.events.all) {
      const [label, sub] = EVENT_LABELS[ev] || [ev, ""];
      const row = document.createElement("label");
      row.className = "toggle";
      const text = document.createElement("div");
      text.textContent = label;
      const extra = ev === "progress" ? (info.progressStep > 0 ? `Every ${info.progressStep}%` : "Turned off in OctoPrint settings") : sub;
      if (extra) { const s = document.createElement("small"); s.textContent = extra; text.appendChild(s); }
      const sw = document.createElement("span");
      sw.className = "switch";
      sw.innerHTML = '<input type="checkbox"><span></span>';
      const input = sw.firstChild;
      input.checked = selected.includes(ev);
      input.disabled = ev === "progress" && !(info.progressStep > 0);
      input.dataset.ev = ev;
      input.onchange = onEventToggle;
      row.append(text, sw);
      box.appendChild(row);
    }
  }

  async function onEventToggle() {
    const events = [...$("push-events").querySelectorAll("input:checked")].map((i) => i.dataset.ev);
    try { await savePush(events); } catch (e) { toast("Couldn't save"); }
  }

  function deviceName() {
    const ua = navigator.userAgent;
    if (/iphone/i.test(ua)) return "iPhone";
    if (/ipad/i.test(ua) || isIOS()) return "iPad";
    if (/android/i.test(ua)) return "Android";
    if (/mac/i.test(ua)) return "Mac";
    if (/windows/i.test(ua)) return "Windows";
    return "Browser";
  }

  function savePush(events) {
    return plugin("api/push", {
      method: "POST",
      body: { subscription: pushSub.toJSON(), events, device: deviceName() },
    });
  }

  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  $("push-enable").onclick = async () => {
    try {
      // Must be called straight from the tap on iOS.
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("Notifications not allowed"); return setupPushUI(); }
      const reg = swReg || (await registerSW());
      await navigator.serviceWorker.ready;
      pushSub = (await reg.pushManager.getSubscription()) ||
        (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(info.vapidPublicKey) }));
      await savePush(info.events.defaults);
      toast("Notifications on");
    } catch (e) {
      toast("Couldn't turn on notifications");
    }
    setupPushUI();
  };

  $("push-test").onclick = async () => {
    try {
      await plugin("api/push/test", { method: "POST", body: { endpoint: pushSub.endpoint } });
      toast("Test sent. It should arrive in a few seconds.");
    } catch (e) { toast(e.message || "Couldn't send a test"); }
  };

  $("push-disable").onclick = async () => {
    try {
      if (pushSub) {
        await plugin("api/push/unsubscribe", { method: "POST", body: { endpoint: pushSub.endpoint } }).catch(() => {});
        await pushSub.unsubscribe();
        pushSub = null;
      }
      toast("Notifications off");
    } catch (e) { toast("Couldn't turn off"); }
    setupPushUI();
  };

  // ---------- tabs & lifecycle ----------

  function switchTab(tab) {
    activeTab = tab;
    for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
    for (const t of ["printer", "files", "alerts"]) $("tab-" + t).hidden = t !== tab;
    if (tab === "printer") startCamera(); else stopCamera();
    if (tab === "files") loadFiles();
    if (tab === "alerts") setupPushUI();
    window.scrollTo(0, 0);
  }

  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => switchTab(b.dataset.tab);

  $("logout").onclick = async () => {
    if (!(await confirmDlg("Sign out?", "This device will need to be approved again. Notifications stay on until you turn them off.", "Sign out", true))) return;
    signOut();
  };

  $("login-start").onclick = startLogin;
  $("login-cancel").onclick = () => { stopLoginPoll(); showLogin(); };
  $("login-key-save").onclick = () => useKey($("login-key").value);

  document.addEventListener("visibilitychange", () => {
    if (!apiKey || $("app").hidden) return;
    if (document.hidden) { stopPolling(); }
    else { refresh(); if (activeTab === "printer") startCamera(); }
  });

  // ---------- boot ----------

  registerSW();
  if (apiKey) startApp();
  else showLogin();
})();
