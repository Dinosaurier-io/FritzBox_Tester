/* Dashboard-Logik.
 *
 * Zwei Aktualisierungstakte, weil die Daten aus zwei Quellen stammen:
 *   - alle 2 s: Live-Zustand aus dem Arbeitsspeicher der Module (Latenz,
 *     Modulzustand, Restzeit) - das ist billig und soll fluessig wirken.
 *   - alle 15 s: Ereignisse aus der Datenbank - teurer, aendert sich aber
 *     ohnehin nur, wenn tatsaechlich etwas passiert.
 *
 * Kurvenverlaeufe zeigt bewusst nur der Bericht: Waehrend eines Laufs sind sie
 * mangels Daten kaum lesbar und lenken vom aktuellen Zustand ab.
 */

const POLL_LIVE_MS = 2000;
const POLL_EVENTS_MS = 15000;

let currentRunId = null;
let selectedRuns = [];

// ------------------------------------------------------------------ Helfer

const $ = (id) => document.getElementById(id);

/* Fehlerfalle. Ein Fehler im JavaScript bricht die laufende Funktion ab -
 * und wenn das mitten im Aufbau der Seite passiert, bleibt eine leere Flaeche
 * zurueck, ohne jeden Hinweis worauf. Lieber eine haessliche rote Leiste als
 * ein weisser Bildschirm, bei dem niemand weiss, was los ist. */
function reportFailure(what, detail) {
  const banner = $("fatal");
  if (!banner) return;
  banner.classList.remove("hidden");
  banner.innerHTML =
    `<strong>Fehler in der Oberfläche:</strong> ${what}` +
    `<div class="hint">${detail || ""}<br>Die Messung selbst läuft davon unbeeinträchtigt ` +
    `weiter. Seite neu laden mit F5.</div>`;
}

window.addEventListener("error", (event) =>
  reportFailure(event.message, `${event.filename}:${event.lineno}`));
window.addEventListener("unhandledrejection", (event) =>
  reportFailure(event.reason?.message || String(event.reason), ""));

/* Sitzungs-Token. Der Server setzt es beim Ausliefern der Seite ein; jede
 * API-Anfrage muss es mitschicken. Damit kann keine fremde Webseite im selben
 * Browser einen laufenden Testlauf abbrechen oder die Konfiguration auslesen -
 * sie kommt wegen der Same-Origin-Policy nicht an diesen Wert heran. */
const SESSION_TOKEN =
  document.querySelector('meta[name="fbtest-token"]')?.content || "";

/**
 * Oeffnet eine erzeugte Datei im Betriebssystem.
 *
 * Bewusst nicht als Verweis mit ``target="_blank"``: Im Programmfenster gibt es
 * keine Adressleiste und damit auch keinen Zurueck-Knopf. Ein dort geoeffneter
 * Bericht ersetzt die Oberflaeche, und man kommt nicht mehr zurueck.
 *
 * @param {string} path Vollstaendiger Pfad.
 * @param {boolean} reveal True, um den Ordner statt der Datei zu oeffnen.
 */
async function openInSystem(path, reveal = false) {
  await api("/api/open", { method: "POST", body: JSON.stringify({ path, reveal }) });
}

/** Baut die beiden Knoepfe, mit denen sich ein Ergebnis oeffnen laesst. */
function fileActions(path, label = "Öffnen") {
  const escaped = path.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  return `<button class="btn small" onclick="openInSystem('${escaped}')">${label}</button>
          <button class="btn small" onclick="openInSystem('${escaped}', true)">Ordner</button>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Fbtest-Token": SESSION_TOKEN,
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // Fehler der Konfigurationspruefung kommen als Objekt mit Feldliste.
    const detail = data?.detail;
    const message = typeof detail === "string"
      ? detail : detail?.message || `HTTP ${response.status}`;
    const error = new Error(message);
    error.fieldErrors = detail?.errors || [];
    error.status = response.status;
    throw error;
  }
  return data;
}

function showMessage(element, kind, text, hint = "") {
  element.innerHTML = `<div class="msg ${kind}">${text}${
    hint ? `<div class="hint">${hint}</div>` : ""
  }</div>`;
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let index = 0;
  let value = bytes;
  while (value >= 1000 && index < units.length - 1) { value /= 1000; index++; }
  return `${value.toFixed(index ? 2 : 0)} ${units[index]}`;
}

function formatClock(timestamp) {
  return new Date(timestamp * 1000).toLocaleTimeString("de-CH");
}

function formatDate(timestamp) {
  return new Date(timestamp * 1000).toLocaleString("de-CH",
    { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ------------------------------------------------------------------ Reiter

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "runs") loadRuns();
    if (tab.dataset.tab === "settings") { loadSettings(); loadDiagnostics(); }
  });
});

// ------------------------------------------------------------- Live-Zustand

async function pollStatus() {
  let data;
  try {
    data = await api("/api/status");
    $("conn-pill").classList.remove("error");
  } catch (err) {
    $("conn-pill").textContent = "keine Verbindung";
    $("conn-pill").className = "pill error";
    $("headline").textContent = "Dashboard nicht erreichbar - läuft der Server noch?";
    return;
  }

  $("version").textContent = `fbtest ${data.version}`;

  if (!data.running) {
    currentRunId = null;
    $("conn-pill").textContent = "bereit";
    $("conn-pill").className = "pill idle";
    $("start-form").classList.remove("hidden");
    $("stop-form").classList.add("hidden");
    $("progress-panel").classList.add("hidden");
    $("headline").textContent = `Kein Testlauf aktiv · Router ${data.router_host}`;
    renderIdleKpis(data.last_result);
    renderTargets({});
    renderModules([]);
    // Auch leeren, sonst bliebe der Stand des beendeten Laufs stehen und
    // saehe aus, als liefe noch etwas.
    renderTraffic({ traffic: {}, traffic_paused: false, traffic_pause_reason: "" },
                  data.server_time);
    renderSpeedtest(null);
    return;
  }

  const run = data.run;
  // Wechselt der beobachtete Testlauf, sofort die Ereignisse nachladen -
  // sonst bliebe der Ticker bis zum naechsten 15-s-Takt leer.
  const runChanged = currentRunId !== run.run_id;
  currentRunId = run.run_id;
  if (runChanged) pollEvents();

  $("conn-pill").textContent = "Testlauf läuft";
  $("conn-pill").className = "pill live";
  $("start-form").classList.add("hidden");
  $("stop-form").classList.remove("hidden");
  $("progress-panel").classList.remove("hidden");
  // Der Benutzer soll sehen, ob der Rechner ueber Nacht wach bleibt - und
  // nicht erst am naechsten Morgen an einer Messluecke merken, dass nicht.
  $("standby-note").textContent = run.standby_text || "";
  $("standby-note").className = run.standby_prevented ? "muted" : "warn-text";
  $("headline").textContent =
    `Testlauf #${run.run_id}${run.run_name ? ` · ${run.run_name}` : ""}` +
    ` · ${run.router_model || "Router unbekannt"}` +
    ` · Firmware ${run.firmware_version || "unbekannt"}`;

  $("elapsed").textContent = run.elapsed_text;
  $("remaining").textContent = run.remaining_text;
  $("progress-bar").style.width = `${run.progress_pct}%`;

  renderKpis(data);
  renderTargets(run.ping);
  renderModules(run.modules);
  renderTraffic(run, data.server_time);
  renderSpeedtest(data.speedtest);
}

/** Merkt sich den letzten Zaehlerstand je Profil, um die Rate zu bilden. */
const trafficMarks = {};

/**
 * Zeigt je Traffic-Profil das uebertragene Volumen und die aktuelle Rate.
 *
 * Die Rate steht nicht in der Statusmeldung - der Server liefert nur den
 * Zaehlerstand. Sie ergibt sich aus der Differenz zweier Abfragen, was hier
 * genau richtig ist: Wer zusieht, will die *momentane* Last sehen, nicht den
 * Durchschnitt seit Beginn.
 */
function renderTraffic(run, serverTime) {
  const entries = Object.entries(run.traffic);
  const rows = entries.map(([name, bytes]) => {
    const previous = trafficMarks[name];
    trafficMarks[name] = { bytes, at: serverTime };
    const seconds = previous ? serverTime - previous.at : 0;
    const rate = seconds > 0.5 ? ((bytes - previous.bytes) * 8) / seconds / 1e6 : null;
    return `<tr>
      <td>${name}</td>
      <td class="num">${formatBytes(bytes)}</td>
      <td class="num">${rate === null ? "—" : `${rate.toFixed(1)} Mbit/s`}</td>
    </tr>`;
  });
  $("traffic").querySelector("tbody").innerHTML =
    rows.join("") || `<tr><td colspan="3" class="muted">Kein Traffic-Profil aktiv.</td></tr>`;
  $("traffic-note").textContent = run.traffic_paused
    ? `pausiert: ${run.traffic_pause_reason}` : "";
  $("traffic-note").className = run.traffic_paused ? "warn-text" : "muted";
}

/** Zeigt die letzte Bandbreitenmessung im Detail. */
function renderSpeedtest(speedtest) {
  const body = $("speedtest").querySelector("tbody");
  if (!speedtest) {
    body.innerHTML =
      `<tr><td class="muted">Noch keine Messung — die erste folgt nach dem
       konfigurierten Intervall.</td></tr>`;
    return;
  }
  const bloat = (speedtest.latency_loaded_ms !== null && speedtest.latency_idle_ms !== null)
    ? speedtest.latency_loaded_ms - speedtest.latency_idle_ms : null;
  const rows = [
    ["Download", speedtest.down_mbps !== null
      ? `${speedtest.down_mbps.toFixed(1)} Mbit/s` : "—"],
    ["Upload", speedtest.up_mbps !== null
      ? `${speedtest.up_mbps.toFixed(1)} Mbit/s` : "nicht gemessen"],
    ["Latenz im Leerlauf", speedtest.latency_idle_ms !== null
      ? `${speedtest.latency_idle_ms.toFixed(1)} ms` : "—"],
    ["Latenz unter Last", speedtest.latency_loaded_ms !== null
      ? `${speedtest.latency_loaded_ms.toFixed(1)} ms` : "—"],
    ["Bufferbloat", bloat === null ? "—"
      : `<span class="${bloat > 100 ? "warn-text" : ""}">${bloat.toFixed(1)} ms</span>`],
    ["gemessen um", formatClock(speedtest.ts)],
  ];
  body.innerHTML = rows.map(([name, value]) =>
    `<tr><td class="muted">${name}</td><td class="num">${value}</td></tr>`).join("");
}

function renderIdleKpis(lastResult) {
  const container = $("kpis");
  if (!lastResult) {
    container.innerHTML =
      `<div class="card"><div class="label">Status</div>
       <div class="value">bereit</div>
       <div class="note">Oben Name und Laufzeit eintragen und starten.</div></div>`;
    return;
  }
  container.innerHTML = `
    <div class="card ${lastResult.error ? "bad" : "ok"}">
      <div class="label">Letzter Testlauf</div>
      <div class="value">#${lastResult.run_id}</div>
      <div class="note">${lastResult.error
        ? "Fehler: " + lastResult.error
        : (lastResult.interrupted ? "vorzeitig beendet" : "regulär beendet")}</div>
    </div>
    <div class="card">
      <div class="label">Geschriebene Zeilen</div>
      <div class="value">${lastResult.rows_written.toLocaleString("de-CH")}</div>
    </div>`;
}

function renderKpis(data) {
  const run = data.run;
  const cards = [];

  // Ausfall hat immer Vorrang - das ist die wichtigste Information.
  if (data.outage) {
    const seconds = Math.round(data.server_time - data.outage.started_at);
    cards.push(card("bad", "AUSFALL AKTIV", `${seconds} s`,
      `Art: ${data.outage.scope} — seit ${formatClock(data.outage.started_at)}`));
  } else {
    cards.push(card("ok", "Verbindung", "stabil", "kein laufender Ausfall"));
  }

  const targets = Object.entries(run.ping);
  const alive = targets.filter(([, t]) => t.ok).length;
  cards.push(card(alive === targets.length ? "ok" : "bad", "Ping-Ziele erreichbar",
    `${alive}/${targets.length}`));

  const internet = targets.filter(([, t]) => t.scope === "internet");
  if (internet.length) {
    const values = internet.map(([, t]) => t.rtt_ms).filter((v) => v !== null);
    const avg = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
    cards.push(card(avg === null ? "bad" : (avg > 100 ? "warn" : ""), "Latenz Internet",
      avg === null ? "—" : avg.toFixed(1), "", "ms"));
  }

  const totalLoss = targets.reduce((sum, [, t]) => sum + t.lost, 0);
  const totalSent = targets.reduce((sum, [, t]) => sum + t.sent, 0);
  const lossPct = totalSent ? (100 * totalLoss / totalSent) : 0;
  cards.push(card(lossPct > 1 ? "warn" : "", "Paketverlust gesamt",
    lossPct.toFixed(2), `${totalLoss} von ${totalSent}`, "%"));

  if (data.speedtest) {
    const bloat = (data.speedtest.latency_loaded_ms !== null &&
                   data.speedtest.latency_idle_ms !== null)
      ? data.speedtest.latency_loaded_ms - data.speedtest.latency_idle_ms : null;
    cards.push(card("", "Letzter Speedtest",
      data.speedtest.down_mbps ? data.speedtest.down_mbps.toFixed(1) : "—",
      `${formatClock(data.speedtest.ts)}${bloat !== null
        ? ` · Bufferbloat ${bloat.toFixed(1)} ms` : ""}`, "Mbit/s"));
  }

  if (data.router && data.router.router_uptime_s !== null) {
    const hours = data.router.router_uptime_s / 3600;
    cards.push(card("", "Router-Laufzeit", hours.toFixed(1),
      `WAN: ${data.router.connection_status || "unbekannt"}`, "h"));
  } else if (!run.tr064_available) {
    cards.push(card("warn", "TR-064", "inaktiv",
      "Keine Router-Telemetrie — Neustart-Erkennung ist deaktiviert."));
  }

  const trafficBytes = Object.values(run.traffic).reduce((a, b) => a + b, 0);
  cards.push(card(run.traffic_paused ? "warn" : "", "Erzeugte Last",
    formatBytes(trafficBytes),
    run.traffic_paused ? `pausiert: ${run.traffic_pause_reason}` : ""));

  cards.push(card(run.dropped > 0 ? "warn" : "", "Gespeicherte Zeilen",
    run.rows_written.toLocaleString("de-CH"),
    run.dropped > 0 ? `${run.dropped} verworfen!` : `${run.buffered} im Puffer`));

  if (run.wlan.is_wireless) {
    cards.push(card(run.wlan.connected ? "ok" : "bad", "WLAN des Messgeräts",
      run.wlan.connected ? "verbunden" : "getrennt", run.wlan.ssid || ""));
  }

  $("kpis").innerHTML = cards.join("");
}

function card(kind, label, value, note = "", unit = "") {
  return `<div class="card ${kind}">
    <div class="label">${label}</div>
    <div class="value">${value}${unit ? `<small>${unit}</small>` : ""}</div>
    ${note ? `<div class="note">${note}</div>` : ""}
  </div>`;
}

function renderTargets(ping) {
  const rows = Object.entries(ping).map(([name, target]) => {
    const failing = target.consecutive_failures;
    const dot = target.ok ? "ok" : (failing >= 3 ? "bad" : "warn");
    return `<tr>
      <td><span class="dot ${dot}"></span><strong>${name}</strong>
          <div class="muted">${target.host} · ${target.scope}</div></td>
      <td class="num">${target.rtt_ms !== null ? target.rtt_ms.toFixed(1) + " ms" : "—"}</td>
      <td class="num">${target.loss_pct.toFixed(2)} %
          <div class="muted">${target.lost}/${target.sent}</div></td>
      <td class="num">${failing > 0 ? `<span style="color:var(--bad)">${failing}× Fehler</span>` : ""}</td>
    </tr>`;
  });
  $("targets").querySelector("tbody").innerHTML =
    rows.join("") || `<tr><td class="muted">Keine Ping-Ziele aktiv.</td></tr>`;
}

function renderModules(modules) {
  const rows = modules.map((module) => `
    <tr>
      <td><span class="dot ${module.running ? "ok" : "warn"}"></span>${module.name}</td>
      <td class="num">${Math.round(module.uptime_s)} s</td>
      <td class="num">${module.restarts > 0
        ? `<span style="color:var(--warn)">${module.restarts}× neu gestartet</span>` : "stabil"}</td>
      <td class="muted">${module.last_error || ""}</td>
    </tr>`);
  $("modules").querySelector("tbody").innerHTML =
    rows.join("") || `<tr><td class="muted">Keine Module aktiv.</td></tr>`;
}

// ---------------------------------------------------------------- Ereignisse

async function pollEvents() {
  if (currentRunId === null) return;
  try {
    renderEvents(await api(`/api/events?run_id=${currentRunId}&limit=40`));
  } catch (err) {
    console.warn("Ereignisse nicht verfügbar:", err.message);
  }
}

function renderEvents(events) {
  $("events").innerHTML = events.map((event) => `
    <div class="event ${event.severity}">
      <time>${formatClock(event.ts)}</time>
      <code>${event.type}</code>
      <span>${event.message}</span>
    </div>`).join("") || `<div class="muted">Noch keine Ereignisse.</div>`;
}

// ------------------------------------------------------------- Steuerung

function openDialog(id) { $(id).classList.remove("hidden"); }
function closeDialog(id) { $(id).classList.add("hidden"); }

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => button.closest(".overlay").classList.add("hidden"));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll(".overlay:not(.hidden)").forEach((o) => o.classList.add("hidden"));
  }
});

$("duration-presets").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  document.querySelectorAll("#duration-presets .chip").forEach((c) =>
    c.classList.toggle("active", c === chip));
  $("run-duration").value = chip.dataset.value;
});
$("run-duration").addEventListener("input", () => {
  document.querySelectorAll("#duration-presets .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.value === $("run-duration").value.trim()));
});

/** Zeigt vor dem Start, welche Module laufen werden - und warum eines fehlt. */
async function renderModulePreview() {
  const box = $("module-preview");
  try {
    const config = (await api("/api/config")).values;
    const secret = await api("/api/secret/password");
    const parts = [];
    if (config.ping.enabled) parts.push(`Ping (${config.ping.targets.length} Ziele)`);
    if (config.traffic.enabled && config.traffic.profiles.length) {
      parts.push(`Datenverkehr (${config.traffic.profiles.filter((p) => p.enabled).length} Profile)`);
    }
    if (config.speedtest.enabled) parts.push("Bandbreite");
    if (config.wlan.enabled) parts.push("WLAN");
    box.innerHTML = parts.map((p) => `<span class="chip static">${p}</span>`).join(" ");

    if (!secret.present) {
      box.innerHTML += `<div class="hint">Ohne FRITZ!Box-Passwort fehlen Router-Laufzeit
        und <strong>Neustart-Erkennung</strong> — unter Einstellungen nachtragen.</div>`;
    }
  } catch (err) {
    box.textContent = err.message;
  }
}

/** Fuehrt die Vorabpruefung aus und laesst bei Warnungen weitermachen. */
async function preflight() {
  const box = $("start-check");
  box.innerHTML = `<div class="msg">Vorabprüfung läuft …</div>`;
  try {
    const data = await api("/api/check", { method: "POST" });
    const bad = data.results.filter((item) => item.status !== "ok");
    if (!bad.length) {
      box.innerHTML = `<div class="msg ok">Vorabprüfung bestanden.</div>`;
      return true;
    }
    // Warnungen blockieren bewusst nicht - ein Lauf ohne TR-064 ist immer noch
    // ein brauchbarer Lauf. Fehler dagegen schon.
    const kinds = { warn: "warn", fail: "bad" };
    box.innerHTML = bad.map((item) => `
      <div class="msg ${kinds[item.status]}"><strong>${item.name}</strong>: ${item.message}
      ${item.hint ? `<div class="hint">${item.hint}</div>` : ""}</div>`).join("");
    return data.worst !== "fail";
  } catch (err) {
    box.innerHTML = `<div class="msg warn">Vorabprüfung nicht möglich: ${err.message}</div>`;
    return true;
  }
}

$("btn-start").addEventListener("click", async () => {
  $("start-check").innerHTML = "";
  openDialog("dialog-start");
  await renderModulePreview();
  await preflight();
});

$("btn-start-confirm").addEventListener("click", async () => {
  const button = $("btn-start-confirm");
  button.disabled = true;
  try {
    const result = await api("/api/run/start", {
      method: "POST",
      body: JSON.stringify({
        name: $("run-name").value.trim(),
        duration: $("run-duration").value.trim() || "24h",
        resume: false,
      }),
    });
    closeDialog("dialog-start");
    showMessage($("control-msg"), "ok", `Testlauf #${result.run_id} gestartet.`);
    await pollStatus();
    await pollEvents();
  } catch (err) {
    showMessage($("start-check"), "bad", `Start fehlgeschlagen: ${err.message}`);
  } finally {
    button.disabled = false;
  }
});

$("btn-stop").addEventListener("click", async () => {
  const button = $("btn-stop");
  button.disabled = true;
  button.textContent = "beende …";
  try {
    await api("/api/run/stop", { method: "POST" });
    showMessage($("control-msg"), "ok",
      "Testlauf beendet — alle gepufferten Messwerte wurden gespeichert.");
    await pollStatus();
    loadRuns();
  } catch (err) {
    showMessage($("control-msg"), "bad", `Beenden fehlgeschlagen: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "■ Testlauf beenden";
  }
});

// ------------------------------------------------------------- Testläufe

async function loadRuns() {
  let runs;
  try {
    runs = await api("/api/runs");
  } catch (err) {
    showMessage($("runs-msg"), "bad", err.message);
    return;
  }

  $("runs").querySelector("tbody").innerHTML = runs.map((run) => `
    <tr data-id="${run.id}">
      <td><input type="checkbox" class="run-select" value="${run.id}"></td>
      <td>#${run.id}</td>
      <td>${run.name || "—"}</td>
      <td>${run.firmware_version || "—"}</td>
      <td>${formatDate(run.started_at)}</td>
      <td class="num">${run.duration_text}</td>
      <td class="num">${run.measurements.toLocaleString("de-CH")}</td>
      <td class="num">${run.outages}</td>
      <td>${run.open
        ? '<span class="dot warn"></span>offen'
        : '<span class="dot ok"></span>beendet'}</td>
      <td>
        <button class="btn small" data-action="report" data-id="${run.id}">Bericht</button>
        <button class="btn small" data-action="export" data-id="${run.id}">Tabelle</button>
        <button class="btn small" data-action="export-raw" data-id="${run.id}"
                title="Rohdaten als CSV und JSON">Rohdaten</button>
        ${run.open
          ? `<button class="btn small" data-action="close" data-id="${run.id}">Abschliessen</button>`
          : ""}
        <button class="btn small danger" data-action="delete" data-id="${run.id}">Löschen</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="10" class="muted">Noch keine Testläufe.</td></tr>`;

  document.querySelectorAll(".run-select").forEach((box) => {
    box.addEventListener("change", () => {
      selectedRuns = [...document.querySelectorAll(".run-select:checked")]
        .map((b) => parseInt(b.value, 10));
      document.querySelectorAll("#runs tbody tr").forEach((tr) => {
        tr.classList.toggle("selected", selectedRuns.includes(parseInt(tr.dataset.id, 10)));
      });
      $("btn-compare").disabled = selectedRuns.length !== 2;
    });
  });

  document.querySelectorAll("#runs button[data-action]").forEach((button) => {
    button.addEventListener("click", () => handleRunAction(button));
  });
}

async function handleRunAction(button) {
  const runId = button.dataset.id;
  const action = button.dataset.action;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "…";
  try {
    if (action === "report") {
      const result = await api(`/api/report/${runId}`, { method: "POST" });
      showMessage($("runs-msg"), "ok",
        `Bericht erzeugt. ${fileActions(result.path, "Bericht öffnen")}`, result.path);
    } else if (action === "export") {
      // Standard ist die Arbeitsmappe: eine Datei, benannt nach dem Testlauf.
      // Die Rohformate stehen weiterhin über die zweite Schaltfläche bereit.
      const result = await api(`/api/export/${runId}`, {
        method: "POST", body: JSON.stringify({ format: "xlsx" }),
      });
      showMessage($("runs-msg"), "ok",
        `Tabelle erstellt. ${fileActions(result.files[0], "Tabelle öffnen")}`,
        result.files[0]);
    } else if (action === "export-raw") {
      const result = await api(`/api/export/${runId}`, {
        method: "POST", body: JSON.stringify({ format: "both" }),
      });
      showMessage($("runs-msg"), "ok",
        `${result.files.length} Dateien exportiert. ${fileActions(result.files[0], "Ordner öffnen")}`,
        result.directory);
    } else if (action === "close") {
      await api(`/api/run/${runId}/close`, { method: "POST" });
      showMessage($("runs-msg"), "ok",
        `Testlauf #${runId} als beendet markiert — die Messdaten bleiben erhalten.`);
      loadRuns();
    } else if (action === "delete") {
      // Bewusst mit Rueckfrage: Messdaten aus mehreren Tagen sind nicht
      // wiederherstellbar.
      if (!confirm(`Testlauf #${runId} mit allen Messdaten unwiderruflich löschen?`)) return;
      const result = await api(`/api/run/${runId}`, { method: "DELETE" });
      showMessage($("runs-msg"), "ok",
        `Testlauf #${runId} gelöscht (${result.rows.toLocaleString("de-CH")} Datenzeilen).`);
      loadRuns();
    }
  } catch (err) {
    showMessage($("runs-msg"), "bad", err.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

$("btn-compare").addEventListener("click", async () => {
  const [a, b] = selectedRuns.sort((x, y) => x - y);
  try {
    const result = await api(`/api/compare/${a}/${b}`, { method: "POST" });
    showMessage($("runs-msg"), "ok",
      `Vergleich #${a} gegen #${b} erzeugt. ${fileActions(result.path, "Vergleich öffnen")}`,
      result.path);
  } catch (err) {
    showMessage($("runs-msg"), "bad", err.message);
  }
});

// ------------------------------------------------------------- Vorabprüfung

$("btn-check").addEventListener("click", async () => {
  const button = $("btn-check");
  button.disabled = true;
  button.textContent = "prüfe …";
  $("check-results").innerHTML = "";
  try {
    const data = await api("/api/check", { method: "POST" });
    const kinds = { ok: "ok", warn: "warn", fail: "bad" };
    $("check-results").innerHTML = data.results.map((item) => `
      <div class="msg ${kinds[item.status]}">
        <strong>${item.name}</strong>: ${item.message}
        ${item.hint ? `<div class="hint">${item.hint}</div>` : ""}
      </div>`).join("") +
      `<div class="msg ${kinds[data.worst]}"><strong>${
        data.worst === "ok" ? "Alles bereit — der Testlauf kann gestartet werden."
        : data.worst === "warn" ? "Der Testlauf ist möglich, aber eingeschränkt."
        : "So ist kein sinnvoller Testlauf möglich."
      }</strong></div>`;
  } catch (err) {
    showMessage($("check-results"), "bad", err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Prüfung ausführen";
  }
});

// ------------------------------------------- Abgebrochener Testlauf

/**
 * Prueft beim Laden, ob ein Testlauf offen liegengeblieben ist.
 *
 * Frueher wurde ein solcher Lauf beim naechsten Start stillschweigend
 * fortgesetzt. Das ist falsch, wenn zwischen Absturz und Neustart Tage liegen -
 * dann klebt man zwei Messreihen zusammen, die nichts miteinander zu tun haben.
 * Deshalb entscheidet der Benutzer.
 */
async function checkOpenRun() {
  // Der Einrichtungsassistent hat Vorrang: Zwei Dialoge uebereinander waeren
  // beim allerersten Start genau die falsche Begruessung.
  if (await checkSetupNeeded()) return;

  let data;
  try {
    data = await api("/api/run/open");
  } catch {
    return;
  }
  if (!data.open) return;

  const run = data.run;
  $("resume-text").textContent =
    `Testlauf #${run.id}${run.name ? ` „${run.name}"` : ""} wurde nicht ordentlich beendet.`;
  $("resume-details").querySelector("tbody").innerHTML = `
    <tr><td>Begonnen</td><td>${formatDate(run.started_at)}</td></tr>
    <tr><td>Letzter Datenpunkt</td><td>${
      run.last_activity ? formatDate(run.last_activity) : "keiner"}</td></tr>
    <tr><td>Lücke seither</td><td>${
      run.gap_s ? formatGap(run.gap_s) : "—"}</td></tr>
    <tr><td>Messwerte</td><td>${run.measurements.toLocaleString("de-CH")}</td></tr>
    <tr><td>Ausfälle</td><td>${run.outages}</td></tr>`;
  openDialog("dialog-resume");

  $("btn-resume-continue").onclick = async () => {
    closeDialog("dialog-resume");
    try {
      await api("/api/run/start", {
        method: "POST",
        body: JSON.stringify({ name: run.name, duration: "24h", resume: true }),
      });
      await pollStatus();
    } catch (err) {
      showMessage($("control-msg"), "bad", err.message);
    }
  };
  $("btn-resume-close").onclick = async () => {
    await api(`/api/run/${run.id}/close`, { method: "POST" });
    closeDialog("dialog-resume");
    showMessage($("control-msg"), "ok",
      `Testlauf #${run.id} als beendet markiert — die Messdaten bleiben auswertbar.`);
    loadRuns();
  };
  $("btn-resume-discard").onclick = async () => {
    if (!confirm(`Testlauf #${run.id} mit allen Messdaten unwiderruflich löschen?`)) return;
    const result = await api(`/api/run/${run.id}`, { method: "DELETE" });
    closeDialog("dialog-resume");
    showMessage($("control-msg"), "ok",
      `Testlauf #${run.id} verworfen (${result.rows} Datenzeilen).`);
    loadRuns();
  };
}

/** Formatiert eine Zeitspanne grob - fuer die Einschaetzung reicht das. */
function formatGap(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} Sekunden`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} Minuten`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} Stunden`;
  return `${(seconds / 86400).toFixed(1)} Tage`;
}

// ------------------------------------------------------------- Start

pollStatus();
pollEvents();
loadRuns();
// Erst wenn alle Skripte geladen sind: checkOpenRun() greift auf
// checkSetupNeeded() aus setup.js zu, das nach dieser Datei eingebunden wird.
document.addEventListener("DOMContentLoaded", checkOpenRun);
setInterval(pollStatus, POLL_LIVE_MS);
setInterval(pollEvents, POLL_EVENTS_MS);
