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
 *
 * Alle Abschnitte werden einmal gebaut und danach nur noch ein- und
 * ausgeblendet. Ein Neuaufbau beim Abschnittswechsel wuerde begonnene, noch
 * nicht gespeicherte Eingaben verwerfen - und zwar unbemerkt.
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
  desktop: "Desktop-Fenster",
};

/**
 * Gliederung des Abschnittsmenues.
 *
 * Neun gleichrangige Eintraege sind eine Liste, in der man sucht statt findet.
 * Die Gruppen trennen nach der Frage, die man gerade hat: Was wird gemessen,
 * wie laeuft der Test ab, wo landen die Daten. Die Reihenfolge innerhalb einer
 * Gruppe ist die der Wichtigkeit, nicht die der Konfigurationsdatei.
 */
const SECTION_GROUPS = [
  { title: "Messung", sections: ["ping", "traffic", "speedtest", "wlan"] },
  { title: "Testlauf", sections: ["router", "run"] },
  { title: "System", sections: ["storage", "logging", "dashboard", "desktop"] },
];

/** Kurzbeschreibung je Abschnitt - als Tooltip, nicht mehr im Menue selbst. */
const SECTION_HINTS = {
  router: "Zugang, Abfragetakt",
  ping: "Ziele, Takt, Ausfallschwelle",
  traffic: "Kuenstlich erzeugte Last",
  speedtest: "Periodische Bandbreitenmessung",
  wlan: "Router- und Clientsicht",
  run: "Dauer und Abbruchverhalten",
  storage: "Datenbank, Berichte, Exporte",
  logging: "Umfang der Protokolldateien",
  dashboard: "Adresse und Port",
  desktop: "Fenstergroesse, Infobereich, Meldungen",
};

/** Felder, die das Formular nicht selbst anzeigt. */
const HIDDEN_FIELDS = new Set(["router.password"]);

/**
 * Einheiten fuer Dauerfelder, absteigend sortiert.
 *
 * In der Konfigurationsdatei stehen Dauern als Sekunden. `1800` ist beim Lesen
 * aber eine Rechenaufgabe, und beim Schreiben verrutscht schnell eine Null.
 * Die Maske zeigt deshalb Zahl und Einheit getrennt und rechnet beim Speichern
 * zurueck - die Datei bleibt unveraendert im gewohnten Format.
 */
const DURATION_UNITS = [
  { suffix: "d", factor: 86400, label: "Tage" },
  { suffix: "h", factor: 3600, label: "Stunden" },
  { suffix: "m", factor: 60, label: "Minuten" },
  { suffix: "s", factor: 1, label: "Sekunden" },
];

let settingsSchema = null;
let settingsValues = null;
let rawMode = false;
let activeSection = null;

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

/** Liest einen Wert an einem Punktpfad aus (undefined, wenn nicht vorhanden). */
function getPath(source, parts) {
  let node = source;
  for (const part of parts) {
    if (node === null || node === undefined) return undefined;
    node = node[part];
  }
  return node;
}

/** Vergleicht zwei Werte strukturell. */
function sameValue(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

// -------------------------------------------------------------------- Dauern

/** True, wenn ein Feld eine Dauer in Sekunden enthaelt. */
function isDuration(key, value) {
  return typeof value === "number" && /_s$/.test(key);
}

/** Waehlt die groesste Einheit, in der die Dauer ohne Rest aufgeht. */
function pickUnit(seconds) {
  for (const unit of DURATION_UNITS) {
    if (seconds >= unit.factor && seconds % unit.factor === 0) return unit;
  }
  return DURATION_UNITS.at(-1);
}

// ------------------------------------------------------------------ Rendering

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Uebernimmt Wertebereiche aus dem Schema, umgerechnet auf die Einheit. */
function applyBounds(input, meta, factor) {
  const scale = (bound) => (bound === undefined ? undefined : bound / factor);
  const min = scale(meta.minimum ?? meta.exclusiveMinimum);
  const max = scale(meta.maximum);
  if (min !== undefined) input.min = min; else input.removeAttribute("min");
  if (max !== undefined) input.max = max; else input.removeAttribute("max");
}

/** Baut ein Zahlenfeld mit Einheitenauswahl fuer eine Dauer. */
function renderDuration(input, value, meta) {
  const unit = pickUnit(value);
  input.type = "number";
  input.step = "any";
  input.value = Number((value / unit.factor).toFixed(6));
  input.dataset.duration = String(unit.factor);
  applyBounds(input, meta, unit.factor);

  const select = el("select", "duration-unit");
  for (const option of DURATION_UNITS) {
    const item = el("option", null, option.label);
    item.value = String(option.factor);
    select.appendChild(item);
  }
  select.value = String(unit.factor);
  select.addEventListener("change", () => {
    // Die Dauer selbst bleibt gleich, nur ihre Darstellung wechselt.
    const seconds = Number(input.value || 0) * Number(input.dataset.duration);
    const factor = Number(select.value);
    input.dataset.duration = String(factor);
    input.value = Number((seconds / factor).toFixed(6));
    applyBounds(input, meta, factor);
  });

  const box = el("div", "duration-input");
  box.appendChild(input);
  box.appendChild(select);
  return box;
}

/** Baut ein einzelnes Eingabefeld passend zum Wert. */
function renderField(path, key, value, meta) {
  const id = `cfg-${path.join("-")}`;
  const wrapper = el("div", "field");
  wrapper.dataset.path = path.join(".");
  const label = el("label");
  label.setAttribute("for", id);
  label.appendChild(el("span", "field-name", fieldLabel(key, meta)));

  let input = el("input");
  let control = input;
  const enumValues = meta.enum || meta.const;

  if (typeof value === "boolean") {
    input.type = "checkbox";
    input.checked = value;
    wrapper.classList.add("field-check");
  } else if (Array.isArray(enumValues)) {
    input = el("select");
    control = input;
    for (const option of enumValues) {
      const item = el("option", null, String(option));
      item.value = String(option);
      input.appendChild(item);
    }
    input.value = String(value);
  } else if (isDuration(key, value)) {
    control = renderDuration(input, value, meta);
  } else if (typeof value === "number") {
    input.type = "number";
    input.step = "any";
    applyBounds(input, meta, 1);
    input.value = value;
  } else if (Array.isArray(value)) {
    // Reine Textlisten (URLs, Hostnamen): eine Zeile je Eintrag.
    input = el("textarea");
    control = input;
    input.rows = Math.max(2, value.length);
    input.value = value.join("\n");
    input.dataset.list = "true";
  } else {
    input.type = key === "password" ? "password" : "text";
    input.value = value === null ? "" : String(value);
    if (value === null) input.dataset.nullable = "true";
  }

  input.id = id;
  input.dataset.path = path.join(".");
  input.addEventListener("input", refreshState);
  input.addEventListener("change", refreshState);
  label.appendChild(control);
  wrapper.appendChild(label);

  if (meta.description) wrapper.appendChild(el("div", "hint", meta.description));

  // Zuruecksetzen nur anbieten, wo das Schema einen Standard kennt. Bei
  // Feldern mit default_factory (Listen) gibt pydantic keinen aus.
  if (meta.default !== undefined) {
    wrapper.dataset.default = JSON.stringify(meta.default);
    const reset = el("button", "field-reset", "auf Standard");
    reset.type = "button";
    reset.title = `Standard: ${JSON.stringify(meta.default)}`;
    reset.addEventListener("click", () => {
      writeInput(input, meta.default);
      refreshState();
    });
    wrapper.appendChild(reset);
  }

  wrapper.appendChild(el("div", "field-error"));
  wrapper.dataset.search = [
    fieldLabel(key, meta), meta.description || "", path.join("."),
  ].join(" ").toLowerCase();
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

/** Baut eine Karte innerhalb einer Objektliste. */
function renderListItem(path, index, item) {
  const card = el("div", "list-item");
  const head = el("div", "list-item-head");
  head.appendChild(el("span", "muted", `#${index + 1}${item.type ? ` · ${item.type}` : ""}`));
  const remove = el("button", "btn small danger", "Entfernen");
  remove.type = "button";
  remove.addEventListener("click", () => {
    card.remove();
    refreshState();
  });
  head.appendChild(remove);
  card.appendChild(head);
  card.appendChild(renderGroup([...path, String(index)], item, null));
  return card;
}

/** Baut die Bearbeitung einer Liste von Objekten (Ping-Ziele, Profile). */
function renderObjectList(path, key, items, meta) {
  const block = el("div", "group list-group");
  block.appendChild(el("h3", null, fieldLabel(key, meta)));
  if (meta.description) block.appendChild(el("div", "hint", meta.description));

  const container = el("div");
  container.dataset.list = path.join(".");
  items.forEach((item, index) => container.appendChild(renderListItem(path, index, item)));
  block.appendChild(container);

  // Hinzufuegen nur dort, wo die Feldstruktur eindeutig ist. Traffic-Profile
  // haengen von ihrem Typ ab - ein neues Profil entsteht in der Rohansicht.
  if (path.join(".") === "ping.targets") {
    const add = el("button", "btn small", "Ziel hinzufügen");
    add.type = "button";
    add.addEventListener("click", () => {
      container.appendChild(renderListItem(
        path, container.children.length, { name: "", host: "", scope: "internet" }));
      refreshState();
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

/** Liest ein einzelnes Bedienelement als Konfigurationswert. */
function readInput(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.dataset.list === "true") {
    return input.value.split("\n").map((line) => line.trim()).filter(Boolean);
  }
  if (input.dataset.duration !== undefined) {
    return input.value === "" ? null : Number(input.value) * Number(input.dataset.duration);
  }
  if (input.type === "number") return input.value === "" ? null : Number(input.value);
  if (input.value === "" && input.dataset.nullable === "true") return null;
  return input.value;
}

/** Schreibt einen Konfigurationswert zurueck in sein Bedienelement. */
function writeInput(input, value) {
  if (input.type === "checkbox") {
    input.checked = Boolean(value);
  } else if (input.dataset.list === "true") {
    input.value = (value || []).join("\n");
  } else if (input.dataset.duration !== undefined) {
    const unit = pickUnit(Number(value));
    input.dataset.duration = String(unit.factor);
    input.value = Number((Number(value) / unit.factor).toFixed(6));
    const select = input.parentElement.querySelector(".duration-unit");
    if (select) select.value = String(unit.factor);
  } else {
    input.value = value === null ? "" : String(value);
  }
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
    if (input.tagName === "DIV") continue;
    setPath(result, input.dataset.path.split("."), readInput(input));
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

// ------------------------------------------------------------------ Zustand

/**
 * Bewertet jedes Feld neu und richtet Menue, Filter und Knopfleiste danach.
 *
 * Unterschieden werden zwei Dinge, die leicht verwechselt werden: `is-dirty`
 * heisst "seit dem Laden geaendert, noch nicht gespeichert", `is-custom`
 * dagegen "weicht vom Auslieferungsstandard ab". Ein gespeicherter Wert kann
 * dauerhaft vom Standard abweichen, ohne dass etwas offen waere.
 */
function refreshState() {
  const term = $("settings-search").value.trim().toLowerCase();
  const onlyChanged = $("settings-only-changed").checked;
  const counts = {};
  const dirtyPerSection = {};
  let dirty = 0;

  for (const field of document.querySelectorAll("#settings-form .field")) {
    const input = field.querySelector("[data-path]");
    if (!input) continue;
    const parts = field.dataset.path.split(".");
    const current = readInput(input);

    const saved = getPath(settingsValues, parts);
    const isDirty = saved === undefined || !sameValue(current, saved);
    field.classList.toggle("is-dirty", isDirty);
    if (isDirty) {
      dirty += 1;
      dirtyPerSection[parts[0]] = (dirtyPerSection[parts[0]] || 0) + 1;
    }

    const hasDefault = field.dataset.default !== undefined;
    const isCustom = hasDefault && !sameValue(current, JSON.parse(field.dataset.default));
    field.classList.toggle("is-custom", isCustom);

    const matches = !term || field.dataset.search.includes(term);
    const visible = matches && (!onlyChanged || isCustom || isDirty);
    field.classList.toggle("filtered", !visible);
    if (visible) counts[parts[0]] = (counts[parts[0]] || 0) + 1;
  }

  // Gruppen und Listenkarten ohne sichtbares Feld sind nur noch Ueberschrift.
  for (const box of document.querySelectorAll("#settings-form .group, #settings-form .list-item")) {
    box.classList.toggle("filtered", !box.querySelector(".field:not(.filtered)"));
  }

  // Beim Filtern verblassen Abschnitte ohne Treffer, statt eine Trefferzahl
  // anzuzeigen: Wo nichts steht, muss man auch nicht nachzaehlen.
  const filtering = Boolean(term) || onlyChanged;
  for (const item of document.querySelectorAll("#settings-nav .nav-item")) {
    const section = item.dataset.section;
    item.classList.toggle("empty", filtering && !counts[section]);
    item.classList.toggle("has-dirty", Boolean(dirtyPerSection[section]));
  }
  for (const box of document.querySelectorAll("#settings-nav .nav-group")) {
    box.classList.toggle("empty", !box.querySelector(".nav-item:not(.empty)"));
  }
  showSection(filtering ? null : activeSection);

  $("settings-dirty").textContent = dirty
    ? `${dirty} Änderung${dirty > 1 ? "en" : ""} nicht gespeichert`
    : "";
  $("btn-settings-save").classList.toggle("primary", dirty > 0);
}

/**
 * Springt zu einem Abschnitt - aufgerufen aus dem Dialog «Testlauf starten».
 *
 * Setzt Suche und Filter zurueck, weil sonst ein Abschnitt angesteuert wuerde,
 * dessen Felder gerade alle ausgeblendet sind.
 */
function showSettingsSection(section) {
  activeSection = section;
  $("settings-search").value = "";
  $("settings-only-changed").checked = false;
  refreshState();
  $("settings-nav").scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Zeigt einen Abschnitt allein; ``null`` zeigt alle (Suchmodus). */
function showSection(section) {
  for (const panel of document.querySelectorAll("#settings-form .section")) {
    const own = panel.dataset.section;
    const empty = !panel.querySelector(".field:not(.filtered)");
    panel.classList.toggle("hidden", section ? own !== section : empty);
    panel.classList.toggle("search-mode", section === null);
  }
  for (const item of document.querySelectorAll("#settings-nav .nav-item")) {
    item.classList.toggle("active", section !== null && item.dataset.section === section);
  }
}

// ------------------------------------------------------------------- Aktionen

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
      // Ein Fehler in einem ausgeblendeten Abschnitt bliebe sonst unsichtbar,
      // und die Meldung "1 Feld fehlerhaft" haette keinen erkennbaren Bezug.
      const section = field.closest(".section");
      if (section) {
        activeSection = section.dataset.section;
        showSection(activeSection);
      }
    } else {
      orphans.push(`${error.path}: ${error.message}`);
    }
  }
  const first = document.querySelector("#settings-form .has-error");
  if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  return orphans;
}

/** Baut einen einzelnen Menueeintrag. */
function navItem(section) {
  const item = el("button", "nav-item");
  item.type = "button";
  item.dataset.section = section;
  // Die Kurzbeschreibung bleibt als Tooltip erhalten. Als zweite Zeile unter
  // jedem der neun Eintraege war sie mehr Unruhe als Hilfe.
  item.title = SECTION_HINTS[section] || "";
  item.appendChild(el("span", "nav-title", SECTION_TITLES[section] || section));
  item.appendChild(el("span", "nav-dot"));
  item.addEventListener("click", () => {
    activeSection = section;
    $("settings-search").value = "";
    $("settings-only-changed").checked = false;
    refreshState();
  });
  return item;
}

/**
 * Baut das Abschnittsmenue neben dem Formular, nach Gruppen gegliedert.
 *
 * Args:
 *   sections: Die Abschnitte, die das Schema tatsaechlich hergibt.
 */
function renderNav(sections) {
  const nav = $("settings-nav");
  nav.innerHTML = "";

  const offen = new Set(sections);
  const groups = SECTION_GROUPS.map((group) => ({
    title: group.title,
    sections: group.sections.filter((section) => offen.delete(section)),
  }));
  // Ein neuer Abschnitt in der Konfiguration darf nicht aus dem Menue fallen,
  // nur weil ihn hier niemand eingeordnet hat - er waere dann unerreichbar.
  if (offen.size) groups.push({ title: "Weitere", sections: [...offen] });

  for (const group of groups) {
    if (!group.sections.length) continue;
    const box = el("div", "nav-group");
    box.appendChild(el("div", "nav-group-title", group.title));
    for (const section of group.sections) box.appendChild(navItem(section));
    nav.appendChild(box);
  }
}

/**
 * Laedt Schema und Werte und baut das Formular neu auf.
 *
 * Beim Wechsel auf den Reiter wird diese Funktion erneut aufgerufen. Gibt es
 * ungespeicherte Aenderungen, bleibt der Aufbau stehen - ein Neuaufbau haette
 * sie kommentarlos verworfen. Ueber "Verwerfen" ist das weiterhin moeglich,
 * dann aber absichtlich.
 */
async function loadSettings(force = false) {
  if (!force && settingsValues && document.querySelector("#settings-form .field.is-dirty")) {
    await loadSecretState();
    return;
  }
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

    const sections = Object.keys(settingsValues);
    if (!sections.includes(activeSection)) activeSection = sections[0];
    renderNav(sections);

    const form = $("settings-form");
    form.innerHTML = "";
    for (const [section, values] of Object.entries(settingsValues)) {
      const panel = el("div", "section");
      panel.dataset.section = section;
      panel.appendChild(el("h3", "section-title", SECTION_TITLES[section] || section));
      panel.appendChild(renderGroup([section], values, null));
      form.appendChild(panel);
    }
    refreshState();
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
    await loadSettings(true);
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
  await loadSettings(true);
  if (rawMode) await loadRaw();
  showMessage($("settings-msg"), "ok", "Änderungen verworfen, Stand neu geladen.");
});
$("btn-raw-toggle").addEventListener("click", async () => {
  rawMode = !rawMode;
  $("btn-raw-toggle").textContent = rawMode ? "Formular" : "Rohansicht";
  $("settings-layout").classList.toggle("hidden", rawMode);
  $("settings-tools").classList.toggle("hidden", rawMode);
  $("settings-raw").classList.toggle("hidden", !rawMode);
  if (rawMode) await loadRaw();
});
$("settings-search").addEventListener("input", refreshState);
$("settings-only-changed").addEventListener("change", refreshState);
$("btn-secret-save").addEventListener("click", saveSecret);
$("btn-secret-delete").addEventListener("click", async () => {
  await api("/api/secret/password", { method: "DELETE" });
  await loadSecretState();
});
$("secret-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveSecret();
});
