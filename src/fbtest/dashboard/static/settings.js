/* Einstellungen: Formular, Rohansicht, Passwort, Diagnose.
 *
 * Das Formular wird nicht von Hand gepflegt, sondern aus dem JSON-Schema der
 * pydantic-Modelle erzeugt. Der Grund ist Wartbarkeit: Kommt in der
 * Konfiguration ein Feld dazu, erscheint es hier automatisch mit Beschriftung,
 * Beschreibung und Wertebereich. Eine handgeschriebene Maske waere spaetestens
 * beim dritten neuen Feld veraltet - und niemand merkt es.
 *
 * Aufbau folgt den *Werten*, nicht dem Schema: Bei den Traffic-Profilen
 * entscheidet ein Unterscheidungsmerkmal (`type`) darueber, welche Felder es
 * gibt. Aus dem Schema allein liesse sich das nicht aufloesen, aus dem
 * vorhandenen Wert dagegen zweifelsfrei.
 */

const SECTION_TITLES = {
  router: "FRITZ!Box",
  ping: "Erreichbarkeit (Ping)",
  traffic: "Datenverkehr",
  speedtest: "Bandbreite",
  wlan: "WLAN",
  run: "Testlauf",
  storage: "Speicherorte",
  logging: "Protokollierung",
  dashboard: "Dashboard",
};

/** Felder, die das Formular nicht selbst anzeigt. */
const HIDDEN_FIELDS = new Set(["router.password"]);

let settingsSchema = null;
let settingsValues = null;
let rawMode = false;

// -------------------------------------------------------------- Schema-Zugriff

/** Loest eine ``$ref``-Angabe im Schema auf. */
function deref(node) {
  if (node && node.$ref) {
    const name = node.$ref.split("/").pop();
    return settingsSchema.$defs?.[name] || {};
  }
  return node || {};
}

/**
 * Sucht die Beschreibung eines Feldes anhand seines Punktpfads.
 *
 * @param {string[]} path Pfadbestandteile, z. B. ["ping", "targets", "0", "host"].
 * @returns {object} Schema-Ausschnitt (leer, wenn nicht auffindbar).
 */
function fieldMeta(path) {
  let node = settingsSchema;
  for (const part of path) {
    node = deref(node);
    if (node.type === "array" || node.items) {
      // Listenindex ueberspringen und in den Elementtyp absteigen.
      node = deref(node.items);
      if (/^\d+$/.test(part)) continue;
    }
    if (node.anyOf) {
      // Optionale Felder erscheinen als anyOf[Typ, null]; die Variante mit
      // Inhalt ist die interessante.
      node = deref(node.anyOf.find((entry) => entry.$ref || entry.type !== "null") || {});
    }
    const next = node.properties?.[part];
    if (!next) return {};
    node = next;
  }
  node = deref(node);
  if (node.anyOf) {
    const inner = node.anyOf.find((entry) => entry.type && entry.type !== "null");
    return { ...node, ...(inner || {}) };
  }
  return node;
}

/** Beschriftung eines Feldes: Schema-Titel, sonst der Schlüsselname. */
function fieldLabel(key, meta) {
  return meta.title || key;
}

// ------------------------------------------------------------------ Rendering

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Baut ein einzelnes Eingabefeld passend zum Wert. */
function renderField(path, key, value, meta) {
  const id = `cfg-${path.join("-")}`;
  const wrapper = el("div", "field");
  const label = el("label");
  label.setAttribute("for", id);
  label.appendChild(el("span", "field-name", fieldLabel(key, meta)));

  let input;
  const enumValues = meta.enum || meta.const;

  if (typeof value === "boolean") {
    input = el("input");
    input.type = "checkbox";
    input.checked = value;
    wrapper.classList.add("field-check");
  } else if (Array.isArray(enumValues)) {
    input = el("select");
    for (const option of enumValues) {
      const item = el("option", null, String(option));
      item.value = String(option);
      input.appendChild(item);
    }
    input.value = String(value);
  } else if (typeof value === "number") {
    input = el("input");
    input.type = "number";
    input.step = "any";
    if (meta.minimum !== undefined) input.min = meta.minimum;
    if (meta.exclusiveMinimum !== undefined) input.min = meta.exclusiveMinimum;
    if (meta.maximum !== undefined) input.max = meta.maximum;
    input.value = value;
  } else if (Array.isArray(value)) {
    // Reine Textlisten (URLs, Hostnamen): eine Zeile je Eintrag.
    input = el("textarea");
    input.rows = Math.max(2, value.length);
    input.value = value.join("\n");
    input.dataset.list = "true";
  } else {
    input = el("input");
    input.type = key === "password" ? "password" : "text";
    input.value = value === null ? "" : String(value);
    if (value === null) input.dataset.nullable = "true";
  }

  input.id = id;
  input.dataset.path = path.join(".");
  input.dataset.kind = typeof value;
  label.appendChild(input);
  wrapper.appendChild(label);

  if (meta.description) wrapper.appendChild(el("div", "hint", meta.description));
  wrapper.appendChild(el("div", "field-error"));
  return wrapper;
}

/** Baut eine Gruppe (verschachteltes Objekt oder Listeneintrag). */
function renderGroup(path, values, title) {
  const group = el("div", "group");
  if (title) group.appendChild(el("h3", null, title));

  for (const [key, value] of Object.entries(values)) {
    const childPath = [...path, key];
    if (HIDDEN_FIELDS.has(childPath.join("."))) continue;
    const meta = fieldMeta(childPath);

    if (Array.isArray(value) && value.length && typeof value[0] === "object") {
      group.appendChild(renderObjectList(childPath, key, value, meta));
    } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      group.appendChild(renderGroup(childPath, value, fieldLabel(key, meta)));
    } else {
      group.appendChild(renderField(childPath, key, value, meta));
    }
  }
  return group;
}

/** Baut die Bearbeitung einer Liste von Objekten (Ping-Ziele, Profile). */
function renderObjectList(path, key, items, meta) {
  const block = el("div", "group list-group");
  block.appendChild(el("h3", null, fieldLabel(key, meta)));
  if (meta.description) block.appendChild(el("div", "hint", meta.description));

  const container = el("div");
  container.dataset.list = path.join(".");
  items.forEach((item, index) => {
    const card = el("div", "list-item");
    const head = el("div", "list-item-head");
    head.appendChild(el("span", "muted", `#${index + 1}${item.type ? ` · ${item.type}` : ""}`));
    const remove = el("button", "btn small danger", "Entfernen");
    remove.type = "button";
    remove.addEventListener("click", () => {
      card.remove();
      markDirty();
    });
    head.appendChild(remove);
    card.appendChild(head);
    card.appendChild(renderGroup([...path, String(index)], item, null));
    container.appendChild(card);
  });
  block.appendChild(container);

  // Hinzufuegen nur dort, wo die Feldstruktur eindeutig ist. Traffic-Profile
  // haengen von ihrem Typ ab - ein neues Profil entsteht in der Rohansicht.
  if (path.join(".") === "ping.targets") {
    const add = el("button", "btn small", "Ziel hinzufügen");
    add.type = "button";
    add.addEventListener("click", () => {
      const index = container.children.length;
      const card = el("div", "list-item");
      const head = el("div", "list-item-head");
      head.appendChild(el("span", "muted", `#${index + 1}`));
      const remove = el("button", "btn small danger", "Entfernen");
      remove.type = "button";
      remove.addEventListener("click", () => { card.remove(); markDirty(); });
      head.appendChild(remove);
      card.appendChild(head);
      card.appendChild(renderGroup([...path, String(index)],
        { name: "", host: "", scope: "internet" }, null));
      container.appendChild(card);
    });
    block.appendChild(add);
  } else {
    block.appendChild(el("div", "hint",
      "Neue Einträge über die Rohansicht anlegen – die Felder hängen vom Typ ab."));
  }
  return block;
}

// --------------------------------------------------------------- Einsammeln

/** Setzt einen Wert an einem Punktpfad in ein verschachteltes Objekt. */
function setPath(target, parts, value) {
  let node = target;
  parts.slice(0, -1).forEach((part, index) => {
    const nextIsIndex = /^\d+$/.test(parts[index + 1]);
    if (node[part] === undefined) node[part] = nextIsIndex ? [] : {};
    node = node[part];
  });
  node[parts.at(-1)] = value;
}

/**
 * Liest das Formular aus.
 *
 * Grundlage sind die geladenen Werte, damit ausgeblendete Felder (Passwort)
 * unveraendert erhalten bleiben. Listen werden komplett neu aufgebaut, weil
 * Eintraege entfernt worden sein koennen.
 */
function collectValues() {
  const result = JSON.parse(JSON.stringify(settingsValues));
  for (const listNode of document.querySelectorAll("#settings-form [data-list]")) {
    if (!listNode.dataset.list.includes(".")) continue;
    setPath(result, listNode.dataset.list.split("."), []);
  }

  for (const input of document.querySelectorAll("#settings-form [data-path]")) {
    const parts = input.dataset.path.split(".");
    let value;
    if (input.type === "checkbox") {
      value = input.checked;
    } else if (input.dataset.list === "true") {
      value = input.value.split("\n").map((line) => line.trim()).filter(Boolean);
    } else if (input.type === "number") {
      value = input.value === "" ? null : Number(input.value);
    } else if (input.value === "" && input.dataset.nullable === "true") {
      value = null;
    } else {
      value = input.value;
    }
    setPath(result, parts, value);
  }

  // Nach dem Entfernen von Listeneintraegen bleiben Luecken - sie wuerden als
  // null in der YAML-Datei landen.
  compactArrays(result);
  return result;
}

/** Entfernt Luecken aus Listen, die durch geloeschte Eintraege entstehen. */
function compactArrays(node) {
  if (Array.isArray(node)) {
    const cleaned = node.filter((item) => item !== undefined && item !== null);
    node.length = 0;
    node.push(...cleaned);
    node.forEach(compactArrays);
  } else if (node && typeof node === "object") {
    Object.values(node).forEach(compactArrays);
  }
}

// ------------------------------------------------------------------- Aktionen

function markDirty() {
  $("btn-settings-save").classList.add("primary");
}

function clearFieldErrors() {
  document.querySelectorAll("#settings-form .field-error").forEach((node) => {
    node.textContent = "";
    node.parentElement.classList.remove("has-error");
  });
}

/** Zeigt Validierungsfehler direkt am verursachenden Feld. */
function showFieldErrors(errors) {
  clearFieldErrors();
  const orphans = [];
  for (const error of errors) {
    const input = document.querySelector(
      `#settings-form [data-path="${CSS.escape(error.path)}"]`);
    if (input) {
      const field = input.closest(".field");
      field.classList.add("has-error");
      field.querySelector(".field-error").textContent = error.message;
    } else {
      orphans.push(`${error.path}: ${error.message}`);
    }
  }
  const first = document.querySelector("#settings-form .has-error");
  if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  return orphans;
}

async function loadSettings() {
  try {
    if (!settingsSchema) settingsSchema = await api("/api/config/schema");
    const data = await api("/api/config");
    settingsValues = data.values;

    $("settings-path").innerHTML =
      `Datei: <code>${data.path}</code> — ${data.origin_text}` +
      (data.running
        ? ` · <span class="warn-text">Testlauf läuft: Speicherorte sind gesperrt,
             übrige Änderungen gelten ab dem nächsten Lauf.</span>`
        : "");

    const form = $("settings-form");
    form.innerHTML = "";
    for (const [section, values] of Object.entries(settingsValues)) {
      const panel = el("details", "section");
      if (["router", "ping"].includes(section)) panel.open = true;
      const summary = el("summary", null, SECTION_TITLES[section] || section);
      panel.appendChild(summary);
      panel.appendChild(renderGroup([section], values, null));
      form.appendChild(panel);
    }
    await loadSecretState();
  } catch (err) {
    showMessage($("settings-msg"), "bad", err.message);
  }
}

async function saveSettings() {
  const button = $("btn-settings-save");
  button.disabled = true;
  try {
    if (rawMode) {
      await api("/api/config/raw", {
        method: "PUT", body: JSON.stringify({ text: $("raw-text").value }),
      });
      showMessage($("settings-msg"), "ok", "Rohansicht gespeichert.");
    } else {
      const result = await api("/api/config", {
        method: "PUT", body: JSON.stringify({ values: collectValues() }),
      });
      clearFieldErrors();
      showMessage($("settings-msg"), "ok", "Gespeichert.",
        [result.backup ? `Sicherung: ${result.backup}` : "", ...(result.warnings || [])]
          .filter(Boolean).join("\n"));
    }
    await loadSettings();
    if (rawMode) await loadRaw();
  } catch (err) {
    const errors = err.fieldErrors || [];
    if (errors.length && !rawMode) {
      const orphans = showFieldErrors(errors);
      showMessage($("settings-msg"), "bad",
        `${errors.length} Feld${errors.length > 1 ? "er" : ""} fehlerhaft — nichts gespeichert.`,
        orphans.join("\n"));
    } else {
      showMessage($("settings-msg"), "bad", err.message);
    }
  } finally {
    button.disabled = false;
  }
}

async function loadRaw() {
  try {
    $("raw-text").value = (await api("/api/config/raw")).text;
  } catch (err) {
    showMessage($("settings-msg"), "bad", err.message);
  }
}

// ------------------------------------------------------------------- Passwort

async function loadSecretState() {
  try {
    const data = await api("/api/secret/password");
    $("secret-account").textContent = `Konto: ${data.account}`;
    const kind = data.present ? (data.source === "config" ? "warn" : "ok") : "warn";
    let text = data.present
      ? `Passwort hinterlegt — Quelle: ${data.source_text}.`
      : "Kein Passwort hinterlegt. Ohne Passwort fehlen Router-Laufzeit und Neustart-Erkennung.";
    let hint = "";
    if (!data.keyring_available) {
      hint = `Schlüsselspeicher nicht nutzbar: ${data.keyring_error}\n` +
             `Alternative: Umgebungsvariable ${data.env_variable}.`;
    } else if (data.source === "environment") {
      hint = `Die Umgebungsvariable ${data.env_variable} hat Vorrang. ` +
             "Ein hier gespeichertes Passwort greift erst, wenn sie nicht gesetzt ist.";
    } else if (data.source === "config") {
      hint = "Das Passwort steht im Klartext in der config.yaml. " +
             "Besser hier neu setzen und den Eintrag in der Datei leeren.";
    }
    showMessage($("secret-msg"), kind, text, hint);
  } catch (err) {
    showMessage($("secret-msg"), "bad", err.message);
  }
}

async function saveSecret() {
  const input = $("secret-input");
  if (!input.value) {
    showMessage($("secret-msg"), "bad", "Bitte ein Passwort eingeben.");
    return;
  }
  try {
    await api("/api/secret/password", {
      method: "PUT", body: JSON.stringify({ password: input.value }),
    });
    input.value = "";
    await loadSecretState();
  } catch (err) {
    showMessage($("secret-msg"), "bad", err.message);
  }
}

// ------------------------------------------------------------------- Diagnose

async function loadDiagnostics() {
  const body = $("diagnostics").querySelector("tbody");
  try {
    const data = await api("/api/system");
    const rows = [
      ["Version", `fbtest ${data.version} · Python ${data.python}`, ""],
      ["System", data.platform + (data.frozen ? " (gebündelt)" : ""), ""],
      ["Konfiguration", data.paths.config, data.paths.origin],
      ["Datenbank", data.paths.database, ""],
      ["Berichte", data.paths.reports, ""],
      ["Exporte", data.paths.exports, ""],
      ["Protokolle", data.paths.logs, ""],
      ["Ping-Verfahren", data.ping_backend.name +
        (data.ping_backend.precise ? " (präzise)" : " (Systembefehl)"),
        data.ping_backend.hint],
      ["Schlüsselspeicher", data.keyring.available ? data.keyring.name : "nicht verfügbar",
        data.keyring.error],
      ["Energiesparmodus", data.standby.enabled
        ? `wird unterdrückt (${data.standby.backend})` : "wird nicht unterdrückt", ""],
    ];
    body.innerHTML = rows.map(([name, value, hint]) => `
      <tr><td class="diag-name">${name}</td>
      <td><code>${value}</code>${hint ? `<div class="hint">${hint}</div>` : ""}</td></tr>`).join("");
  } catch (err) {
    body.innerHTML = `<tr><td colspan="2" class="muted">${err.message}</td></tr>`;
  }
}

// ------------------------------------------------------------------ Verdrahten

$("btn-settings-save").addEventListener("click", saveSettings);
$("btn-settings-reload").addEventListener("click", async () => {
  await loadSettings();
  if (rawMode) await loadRaw();
  showMessage($("settings-msg"), "ok", "Änderungen verworfen, Stand neu geladen.");
});
$("btn-raw-toggle").addEventListener("click", async () => {
  rawMode = !rawMode;
  $("btn-raw-toggle").textContent = rawMode ? "Formular" : "Rohansicht";
  $("settings-form").classList.toggle("hidden", rawMode);
  $("settings-raw").classList.toggle("hidden", !rawMode);
  if (rawMode) await loadRaw();
});
$("btn-secret-save").addEventListener("click", saveSecret);
$("btn-secret-delete").addEventListener("click", async () => {
  await api("/api/secret/password", { method: "DELETE" });
  await loadSecretState();
});
$("secret-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveSecret();
});
