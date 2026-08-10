/* Ersteinrichtung.
 *
 * Laeuft beim allerersten Start automatisch. Fragt genau das ab, was das
 * Programm nicht selbst herausfinden kann - alles Uebrige kommt aus der
 * mitgelieferten Vorlage und laesst sich spaeter unter "Einstellungen" aendern.
 *
 * Bewusst kurz gehalten: Vier Schritte, drei Eingabefelder. Ein Assistent, der
 * sechzig Konfigurationswerte abfragt, wird abgebrochen - und dann ist gar
 * nichts eingerichtet.
 */

const SETUP_STEPS = ["Willkommen", "FRITZ!Box", "Ping-Ziele", "Prüfung"];
let setupStep = 0;
let setupValues = null;

function showSetupStep(index) {
  setupStep = index;
  document.querySelectorAll(".wizard-step").forEach((section) => {
    section.classList.toggle("hidden", Number(section.dataset.step) !== index);
  });
  $("setup-steps").innerHTML = SETUP_STEPS.map((name, position) => {
    const state = position < index ? "done" : position === index ? "active" : "";
    return `<span class="step ${state}">${position + 1}. ${name}</span>`;
  }).join("");
  $("setup-back").disabled = index === 0;
  $("setup-next").textContent =
    index === SETUP_STEPS.length - 2 ? "Speichern und prüfen"
      : index === SETUP_STEPS.length - 1 ? "Fertig" : "Weiter";
}

/** Startet den Assistenten, wenn noch keine Konfiguration existiert. */
async function checkSetupNeeded() {
  let state;
  try {
    state = await api("/api/setup/state");
  } catch {
    return false;
  }
  if (state.configured) return false;

  setupValues = (await api("/api/config")).values;
  $("setup-location").innerHTML =
    `Die Konfiguration wird abgelegt unter:<div class="hint"><code>${state.config_path}</code>
     — ${state.origin_text}</div>`;
  $("setup-targets").value = setupValues.ping.targets
    .filter((target) => target.scope === "internet")
    .map((target) => target.host).join("\n");

  showSetupStep(0);
  openDialog("dialog-setup");
  detectGateway();
  return true;
}

/** Schlaegt die Adresse der FRITZ!Box vor, statt sie zu raten. */
async function detectGateway() {
  const field = $("setup-host");
  const hint = $("setup-host-hint");
  try {
    const data = await api("/api/setup/gateway");
    field.value = data.gateway || data.fallback;
    hint.textContent = data.detected
      ? `Automatisch erkannt als Standard-Gateway dieses Rechners (${data.gateway}).`
      : `Nicht ermittelbar — die Werksadresse ist eingetragen. Bitte prüfen.`;
  } catch {
    field.value = "192.168.178.1";
    hint.textContent = "Nicht ermittelbar — die Werksadresse ist eingetragen. Bitte prüfen.";
  }
}

/** Baut aus den Eingaben eine vollstaendige Konfiguration. */
function buildSetupConfig() {
  const host = $("setup-host").value.trim() || "192.168.178.1";
  const values = JSON.parse(JSON.stringify(setupValues));
  values.router.host = host;
  values.router.username = $("setup-user").value.trim();
  // Das Passwort geht in den Schluesselspeicher, nicht in die Datei.
  values.router.password = "";

  const internet = $("setup-targets").value.split("\n")
    .map((line) => line.trim()).filter(Boolean);
  values.ping.targets = [
    { name: "fritzbox", host, scope: "gateway" },
    ...internet.map((address, index) => ({
      name: address.replace(/^www\./, "").split(".")[0] || `ziel${index + 1}`,
      host: address,
      scope: "internet",
    })),
  ];
  return values;
}

/** Speichert Konfiguration und Passwort und fuehrt die Vorabpruefung aus. */
async function finishSetup() {
  const box = $("setup-result");
  box.innerHTML = `<div class="msg">Wird gespeichert …</div>`;
  try {
    await api("/api/config", {
      method: "PUT", body: JSON.stringify({ values: buildSetupConfig() }),
    });
  } catch (err) {
    const details = (err.fieldErrors || []).map((e) => `${e.path}: ${e.message}`).join("\n");
    box.innerHTML = `<div class="msg bad">Speichern fehlgeschlagen: ${err.message}
      ${details ? `<div class="hint">${details}</div>` : ""}</div>`;
    return false;
  }

  const password = $("setup-password").value;
  if (password) {
    try {
      await api("/api/secret/password", {
        method: "PUT", body: JSON.stringify({ password }),
      });
      $("setup-password").value = "";
    } catch (err) {
      box.innerHTML = `<div class="msg warn">Konfiguration gespeichert, aber das Passwort
        konnte nicht abgelegt werden: ${err.message}</div>`;
    }
  }

  box.innerHTML += `<div class="msg">Vorabprüfung läuft …</div>`;
  try {
    const data = await api("/api/check", { method: "POST" });
    const kinds = { ok: "ok", warn: "warn", fail: "bad" };
    const summary = data.worst === "ok"
      ? "Alles bereit — der erste Testlauf kann starten."
      : data.worst === "warn"
        ? "Einsatzbereit, aber eingeschränkt. Die Hinweise unten erklären, was fehlt."
        : "So ist noch kein sinnvoller Testlauf möglich.";
    box.innerHTML =
      `<div class="msg ${kinds[data.worst]}"><strong>${summary}</strong></div>` +
      data.results.map((item) => `
        <div class="msg ${kinds[item.status]}"><strong>${item.name}</strong>: ${item.message}
        ${item.hint ? `<div class="hint">${item.hint}</div>` : ""}</div>`).join("");
  } catch (err) {
    box.innerHTML = `<div class="msg warn">Prüfung nicht möglich: ${err.message}</div>`;
  }
  return true;
}

$("setup-back").addEventListener("click", () => showSetupStep(Math.max(0, setupStep - 1)));

$("setup-next").addEventListener("click", async () => {
  const button = $("setup-next");
  if (setupStep === SETUP_STEPS.length - 1) {
    closeDialog("dialog-setup");
    // Die Oberflaeche kennt jetzt eine andere Konfiguration als beim Laden.
    await pollStatus();
    loadRuns();
    return;
  }
  button.disabled = true;
  try {
    if (setupStep === SETUP_STEPS.length - 2) {
      if (!(await finishSetup())) return;
    }
    showSetupStep(setupStep + 1);
  } finally {
    button.disabled = false;
  }
});
