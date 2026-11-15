/* Settings page: the form is generated from /api/settings metadata.
 *
 * Secret fields are write-only: the browser never receives their value, only
 * "set" / "not set". Saving sends every visible value plus only the secrets the
 * user actually typed; an empty secret input means "leave it alone".
 */

import { api, byId, clear, make, setStatus, show } from "./dom.js";

const state = {
  data: null,
  entries: new Map(),
  loaded: false,
  dirty: false,
};

function fieldId(key) {
  return "f-" + key;
}

function setDirty(value) {
  state.dirty = value;
}

function clearErrors() {
  state.entries.forEach(function (entry) {
    entry.container.classList.remove("has-error");
    entry.error.textContent = "";
  });
}

function showFieldError(key, message) {
  const entry = state.entries.get(key);
  if (!entry) {
    return false;
  }
  entry.container.classList.add("has-error");
  entry.error.textContent = message;
  return true;
}

function helpNode(field) {
  const help = make("p", "help", field.help);
  help.id = "h-" + field.key;
  return help;
}

function errorNode(field) {
  const error = make("p", "field-error");
  error.id = "e-" + field.key;
  error.setAttribute("aria-live", "polite");
  return error;
}

function labelled(field, control) {
  const label = make("label", null, field.label);
  label.htmlFor = control.id;
  return label;
}

function numberInput(field) {
  const input = document.createElement("input");
  input.type = "number";
  input.id = fieldId(field.key);
  input.step = field.type === "int" ? "1" : "any";
  if (field.min !== null && field.min !== undefined) {
    input.min = String(field.min);
  }
  if (field.max !== null && field.max !== undefined) {
    input.max = String(field.max);
  }
  input.value = field.value === null || field.value === undefined ? "" : String(field.value);
  return input;
}

function textInput(field) {
  const input = document.createElement("input");
  input.type = "text";
  input.id = fieldId(field.key);
  input.value = Array.isArray(field.value) ? field.value.join(", ") : (field.value || "");
  if (field.type === "list") {
    input.placeholder = "NSE:RELIANCE, NFO:NIFTY-FUT";
  }
  return input;
}

function secretField(field) {
  const wrap = make("div", "field");
  const input = document.createElement("input");
  input.type = "password";
  input.id = fieldId(field.key);
  input.autocomplete = "new-password";
  input.placeholder = "Leave blank to keep the saved value";
  const label = labelled(field, input);

  const row = make("div", "secret-row");
  row.appendChild(input);

  const pill = make("span", "pill");
  pill.id = "pill-" + field.key;
  pill.dataset.state = field.is_set ? "set" : "not-set";
  pill.textContent = field.is_set ? "Set" : "Not set";
  row.appendChild(pill);

  const clearButton = make("button", "button button-quiet", "Clear");
  clearButton.type = "button";
  clearButton.addEventListener("click", function () {
    clearSecret(field.key, pill, clearButton);
  });
  row.appendChild(clearButton);

  wrap.appendChild(label);
  wrap.appendChild(row);
  wrap.appendChild(helpNode(field));
  const error = errorNode(field);
  wrap.appendChild(error);

  state.entries.set(field.key, {
    field: field,
    container: wrap,
    error: error,
    get: function () {
      return input.value ? input.value : null;
    },
    set: function (value) {
      input.value = value || "";
    },
  });
  input.addEventListener("input", function () {
    setDirty(true);
    wrap.classList.remove("has-error");
    error.textContent = "";
  });
  return wrap;
}

function boolField(field) {
  const wrap = make("div", "field checkbox");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = fieldId(field.key);
  input.checked = Boolean(field.value);
  const label = labelled(field, input);
  wrap.appendChild(input);
  wrap.appendChild(label);
  wrap.appendChild(helpNode(field));
  const error = errorNode(field);
  wrap.appendChild(error);

  state.entries.set(field.key, {
    field: field,
    container: wrap,
    error: error,
    get: function () {
      return input.checked;
    },
    set: function (value) {
      input.checked = Boolean(value);
    },
  });
  input.addEventListener("change", function () {
    setDirty(true);
  });
  return wrap;
}

function choiceField(field) {
  const wrap = make("div", "field");
  const select = document.createElement("select");
  select.id = fieldId(field.key);
  (field.choices || []).forEach(function (choice) {
    const option = document.createElement("option");
    option.value = choice;
    option.textContent = choice;
    select.appendChild(option);
  });
  select.value = field.value;
  const label = labelled(field, select);
  wrap.appendChild(label);
  wrap.appendChild(select);
  wrap.appendChild(helpNode(field));
  const error = errorNode(field);
  wrap.appendChild(error);

  state.entries.set(field.key, {
    field: field,
    container: wrap,
    error: error,
    get: function () {
      return select.value;
    },
    set: function (value) {
      select.value = value;
    },
  });
  select.addEventListener("change", function () {
    setDirty(true);
  });
  return wrap;
}

function multiField(field) {
  const wrap = make("div", "field");
  wrap.id = fieldId(field.key);
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", field.label);
  const legend = make("span", "field-label", field.label);
  wrap.appendChild(legend);

  const row = make("div", "channel-row");
  const boxes = new Map();
  (field.choices || []).forEach(function (choice) {
    const option = make("label", "channel-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = choice;
    const isConsole = choice === "console";
    input.checked = isConsole || (field.value || []).indexOf(choice) >= 0;
    input.disabled = isConsole;
    option.appendChild(input);
    option.appendChild(make("span", null, choice));
    row.appendChild(option);
    boxes.set(choice, input);
  });
  wrap.appendChild(row);
  wrap.appendChild(helpNode(field));
  const error = errorNode(field);
  wrap.appendChild(error);

  state.entries.set(field.key, {
    field: field,
    container: wrap,
    error: error,
    get: function () {
      const values = [];
      boxes.forEach(function (input, name) {
        if (input.checked) {
          values.push(name);
        }
      });
      if (values.indexOf("console") < 0) {
        values.unshift("console");
      }
      return values;
    },
    set: function (value) {
      const selected = value || [];
      boxes.forEach(function (input, name) {
        input.checked = name === "console" || selected.indexOf(name) >= 0;
      });
    },
  });
  boxes.forEach(function (input) {
    input.addEventListener("change", function () {
      setDirty(true);
    });
  });
  return wrap;
}

function renderField(field) {
  if (field.type === "secret") {
    return secretField(field);
  }
  if (field.type === "bool") {
    return boolField(field);
  }
  if (field.type === "choice") {
    return choiceField(field);
  }
  if (field.type === "multi") {
    return multiField(field);
  }

  const wrap = make("div", "field");
  const input = field.type === "int" || field.type === "float"
    ? numberInput(field)
    : textInput(field);
  const label = labelled(field, input);
  wrap.appendChild(label);
  wrap.appendChild(input);
  wrap.appendChild(helpNode(field));
  const error = errorNode(field);
  wrap.appendChild(error);

  state.entries.set(field.key, {
    field: field,
    container: wrap,
    error: error,
    get: function () {
      if (field.type === "int") {
        if (input.value === "") {
          return null;
        }
        const number = Number.parseInt(input.value, 10);
        return Number.isNaN(number) ? input.value : number;
      }
      if (field.type === "float") {
        if (input.value === "") {
          return null;
        }
        const number = Number.parseFloat(input.value);
        return Number.isNaN(number) ? input.value : number;
      }
      return input.value;
    },
    set: function (value) {
      input.value = Array.isArray(value) ? value.join(", ") : String(value === null ? "" : value);
    },
  });
  input.addEventListener("input", function () {
    setDirty(true);
    wrap.classList.remove("has-error");
    error.textContent = "";
  });
  return wrap;
}

function renderAdvanced(fields) {
  const box = document.createElement("details");
  box.className = "advanced-box";
  const summary = document.createElement("summary");
  summary.textContent = "Advanced options (" + fields.length + ")";
  box.appendChild(summary);
  const grid = make("div", "field-grid");
  fields.forEach(function (field) {
    grid.appendChild(renderField(field));
  });
  box.appendChild(grid);
  return box;
}

function renderGroups(data) {
  const container = byId("settings-groups");
  clear(container);
  state.entries.clear();

  data.groups.forEach(function (group) {
    const box = document.createElement("fieldset");
    box.className = "group";
    const legend = document.createElement("legend");
    legend.textContent = group.title;
    box.appendChild(legend);
    box.appendChild(make("p", "group-note", group.description));

    const grid = make("div", "field-grid");
    const advanced = [];
    group.fields.forEach(function (field) {
      if (field.advanced) {
        advanced.push(field);
      } else {
        grid.appendChild(renderField(field));
      }
    });
    box.appendChild(grid);
    if (advanced.length) {
      box.appendChild(renderAdvanced(advanced));
    }
    container.appendChild(box);
  });
}

function collectValues() {
  const values = {};
  state.entries.forEach(function (entry, key) {
    const value = entry.get();
    if (value === null) {
      return;
    }
    values[key] = value;
  });
  return values;
}

async function saveValues(values, options) {
  const opts = options || {};
  const status = byId(opts.statusId || "settings-status");
  setStatus(status, "Saving…", "");
  try {
    const data = await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: values }),
    });
    setStatus(status, data.message || "Saved.", "ok");
    setDirty(false);
    show(byId("settings-applies"), true);
    return data;
  } catch (error) {
    if (error.status === 422 && error.data && error.data.errors) {
      clearErrors();
      let first = null;
      error.data.errors.forEach(function (item) {
        if (item.key && showFieldError(item.key, item.message)) {
          if (!first) {
            first = state.entries.get(item.key);
          }
        } else {
          setStatus(status, item.message, "error");
        }
      });
      if (first) {
        setStatus(status, "Some values need attention — nothing was saved.", "error");
        const input = first.container.querySelector("input, select");
        if (input) {
          input.focus();
        }
      }
    } else if (error.status === 429) {
      setStatus(status, error.data && error.data.message
        ? error.data.message
        : "Please wait a moment before saving again.", "warning");
    } else {
      setStatus(status, error.message, "error");
    }
    throw error;
  }
}

async function submitSettings(event) {
  event.preventDefault();
  const button = byId("settings-save");
  button.disabled = true;
  clearErrors();
  const values = collectValues();
  try {
    await saveValues(values);
    state.entries.forEach(function (entry) {
      if (entry.field.type !== "secret") {
        return;
      }
      const typed = Object.prototype.hasOwnProperty.call(values, entry.field.key);
      if (typed || entry.field.is_set) {
        const pill = byId("pill-" + entry.field.key);
        if (pill) {
          pill.dataset.state = "set";
          pill.textContent = "Set";
        }
      }
      entry.set("");
      entry.field.is_set = typed || entry.field.is_set;
    });
  } catch (error) {
    // saveValues already explained the problem in the form.
  } finally {
    button.disabled = false;
  }
}

async function clearSecret(key, pill, button) {
  button.disabled = true;
  try {
    await saveValues({ [key]: "" }, { statusId: "settings-status" });
    pill.dataset.state = "not-set";
    pill.textContent = "Not set";
    const entry = state.entries.get(key);
    if (entry) {
      entry.set("");
    }
  } catch (error) {
    // The status line already carries the reason.
  } finally {
    button.disabled = false;
  }
}

async function checkSettings() {
  const button = byId("settings-check");
  const panel = byId("settings-check-result");
  button.disabled = true;
  clear(panel);
  show(panel, false);
  try {
    const data = await api("/api/settings/check", { method: "POST" });
    panel.appendChild(make("p", data.ok ? "alerts-only" : "warning", data.message));
    if ((data.warnings || []).length) {
      const list = make("ul");
      data.warnings.forEach(function (warning) {
        list.appendChild(make("li", null, warning));
      });
      panel.appendChild(list);
    }
    if (data.summary && Object.keys(data.summary).length) {
      const pre = document.createElement("pre");
      pre.className = "small";
      pre.textContent = JSON.stringify(data.summary, null, 2);
      panel.appendChild(pre);
    }
    show(panel, true);
  } catch (error) {
    panel.appendChild(make("p", "warning", error.message));
    show(panel, true);
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------- notifications */

async function findChatId() {
  const button = byId("telegram-find");
  const status = byId("telegram-status");
  button.disabled = true;
  setStatus(status, "Asking Telegram for the latest message…", "");
  try {
    const data = await api("/api/notifications/telegram/chat-id", { method: "POST" });
    if (data.ok) {
      const entry = state.entries.get("TELEGRAM_CHAT_ID");
      if (entry) {
        entry.set(data.chat_id);
        setDirty(true);
      }
      setStatus(status, data.message, "ok");
    } else {
      setStatus(status, data.message, "error");
    }
  } catch (error) {
    setStatus(status, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ test alert */

let cooldownTimer = null;

function renderTestAlert(data) {
  setStatus(
    byId("test-alert-status"),
    data.label + " — " + data.detail,
    data.ok ? "ok" : "warning",
  );
  const results = byId("test-alert-results");
  clear(results);
  (data.results || []).forEach(function (result) {
    const item = make("li", result.ok ? "result-ok" : "result-fail");
    const mark = make("span", "result-mark", result.ok ? "✓" : "✕");
    mark.setAttribute("aria-hidden", "true");
    item.appendChild(mark);
    item.appendChild(make("span", null, result.message));
    results.appendChild(item);
  });
  const setup = byId("test-alert-setup");
  clear(setup);
  (data.setup || []).forEach(function (hint) {
    setup.appendChild(make("li", null, hint));
  });
}

function startCooldown(seconds) {
  const button = byId("test-alert-button");
  const status = byId("test-alert-status");
  let remaining = Math.max(1, Math.ceil(Number(seconds) || 10));
  button.disabled = true;
  if (cooldownTimer !== null) {
    window.clearTimeout(cooldownTimer);
  }
  const step = function () {
    if (remaining <= 0) {
      button.disabled = false;
      setStatus(status, "You can send another test now.", "");
      cooldownTimer = null;
      return;
    }
    setStatus(status, "Please wait " + remaining + " more second(s) before another test.", "warning");
    remaining -= 1;
    cooldownTimer = window.setTimeout(step, 1000);
  };
  step();
}

async function sendTestAlert() {
  const button = byId("test-alert-button");
  button.disabled = true;
  setStatus(byId("test-alert-status"), "Sending the test message…", "");
  clear(byId("test-alert-results"));
  clear(byId("test-alert-setup"));
  try {
    renderTestAlert(await api("/api/test-alert", { method: "POST" }));
    button.disabled = false;
  } catch (error) {
    if (error.status === 429 && error.data) {
      renderTestAlert(error.data);
      startCooldown(error.data.retry_after);
    } else {
      setStatus(byId("test-alert-status"), error.message, "error");
      button.disabled = false;
    }
  }
}

/* --------------------------------------------------------------------- load */

async function load() {
  const data = await api("/api/settings");
  state.data = data;
  renderGroups(data);
  state.loaded = true;
  setDirty(false);

  const warning = byId("settings-warning");
  if (data.settings_error) {
    warning.textContent =
      "Your settings need attention: " + data.settings_error +
      " The practice page still works; fix the value below and save again.";
    show(warning, true);
  } else {
    show(warning, false);
  }

  const applies = byId("settings-applies");
  applies.textContent = data.applies_notice;
  show(applies, true);

  const warnings = byId("settings-warnings");
  clear(warnings);
  (data.warnings || []).forEach(function (text) {
    warnings.appendChild(make("li", null, text));
  });
  show(warnings, (data.warnings || []).length > 0);
}

export function initSettings() {
  byId("settings-form").addEventListener("submit", submitSettings);
  byId("settings-check").addEventListener("click", checkSettings);
  byId("telegram-find").addEventListener("click", findChatId);
  byId("test-alert-button").addEventListener("click", sendTestAlert);
}

export async function onShowSettings() {
  if (!state.loaded || !state.dirty) {
    try {
      await load();
    } catch (error) {
      setStatus(byId("settings-status"), error.message, "error");
    }
  }
}
