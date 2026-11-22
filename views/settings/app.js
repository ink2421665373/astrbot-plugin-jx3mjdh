const bridge = window.AstrBotPluginView;
const form = document.getElementById("settings");
const fields = document.getElementById("fields");
const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
const rowTemplate = document.getElementById("mapping");
const add = document.getElementById("add");
const save = document.getElementById("save");
const reload = document.getElementById("reload");
const status = document.getElementById("status");
const enabled = document.getElementById("enabled");
const provider = document.getElementById("provider");
const timeout = document.getElementById("timeout");
const reviewEnabled = document.getElementById("review-enabled");
const persona = document.getElementById("persona");
const tabs = document.querySelectorAll("[data-tab]");
const roleSearch = document.getElementById("role-search");
const roleRows = document.getElementById("role-rows");
const roleEmpty = document.getElementById("role-empty");
const roleGroupTemplate = document.getElementById("role-group");
const roleLinkTemplate = document.getElementById("role-link");
const addRole = document.getElementById("add-role");

const labels = {
  heart_aliases: { label: "心法全名", target: "花间游", aliases: "花花，万花DPS", empty: "还没有心法别名。" },
  server_aliases: { label: "服务器名", target: "梦江南", aliases: "梦服，江南", empty: "还没有服务器别名。" },
};

let tab = "heart_aliases";
let settings;
let token;
let snapshot;
let busy = false;

function announce(text, kind = "") {
  status.textContent = text;
  status.dataset.kind = kind;
}

function dirty() {
  return settings && JSON.stringify(settings) !== snapshot;
}

function updateButtons() {
  save.disabled = busy || !dirty();
  reload.disabled = busy || !bridge;
  fields.disabled = busy || !settings;
}

function changed() {
  updateButtons();
  announce(dirty() ? "有未保存的修改。" : "");
}

function showAliases() {
  if (!settings) return;
  document.getElementById("heart-count").textContent = settings.heart_aliases.length;
  document.getElementById("server-count").textContent = settings.server_aliases.length;
  for (const button of tabs) button.setAttribute("aria-pressed", String(button.dataset.tab === tab));
  const current = settings[tab];
  const text = labels[tab];
  rows.replaceChildren();
  empty.hidden = current.length > 0;
  empty.textContent = text.empty;
  for (const [index, entry] of current.entries()) {
    const fragment = rowTemplate.content.cloneNode(true);
    const target = fragment.querySelector(".target");
    const aliases = fragment.querySelector(".aliases");
    const remove = fragment.querySelector(".remove");
    fragment.querySelector(".target-label").textContent = text.label;
    target.value = entry.target;
    target.placeholder = text.target;
    aliases.value = entry.aliases;
    aliases.placeholder = text.aliases;
    remove.setAttribute("aria-label", `移除第 ${index + 1} 条映射`);
    target.addEventListener("input", () => { entry.target = target.value; changed(); });
    aliases.addEventListener("input", () => { entry.aliases = aliases.value; changed(); });
    remove.addEventListener("click", () => {
      current.splice(index, 1);
      showAliases();
      changed();
      (rows.querySelectorAll(".remove")[Math.min(index, current.length - 1)] || add).focus();
    });
    rows.append(fragment);
  }
}

function searchKey(value) {
  return value.normalize("NFKC").trim().toLowerCase();
}

function showRoleLinks(group, list, addLink) {
  list.replaceChildren();
  for (const [index, role] of group.roles.entries()) {
    const fragment = roleLinkTemplate.content.cloneNode(true);
    for (const input of fragment.querySelectorAll("input")) {
      input.value = role[input.name];
      input.addEventListener("input", () => { role[input.name] = input.value; changed(); });
    }
    const remove = fragment.querySelector(".remove-role-link");
    remove.setAttribute("aria-label", `移除第 ${index + 1} 个角色`);
    remove.addEventListener("click", () => {
      group.roles.splice(index, 1);
      showRoleLinks(group, list, addLink);
      changed();
      addLink.focus();
    });
    list.append(fragment);
  }
}

function showRoleNicknames() {
  const entries = settings.role_nicknames;
  const term = searchKey(roleSearch.value);
  roleRows.replaceChildren();
  for (const [index, entry] of entries.entries()) {
    if (term && !searchKey(entry.nicknames).includes(term)
        && !entry.roles.some(role => [role.name, role.server].some(value => searchKey(value).includes(term)))) continue;
    const fragment = roleGroupTemplate.content.cloneNode(true);
    fragment.querySelector("h3").textContent = `第 ${index + 1} 组`;
    const nicknames = fragment.querySelector("textarea");
    nicknames.value = entry.nicknames;
    nicknames.addEventListener("input", () => { entry.nicknames = nicknames.value; changed(); });
    const links = fragment.querySelector(".role-link-rows");
    const addLink = fragment.querySelector(".add-role-link");
    showRoleLinks(entry, links, addLink);
    addLink.addEventListener("click", () => {
      entry.roles.push({ server: "", name: "" });
      showRoleLinks(entry, links, addLink);
      changed();
      links.lastElementChild.querySelector("input").focus();
    });
    const remove = fragment.querySelector(".remove-role-group");
    remove.setAttribute("aria-label", `移除第 ${index + 1} 个关联组`);
    remove.addEventListener("click", () => {
      entries.splice(index, 1);
      showRoleNicknames();
      changed();
      addRole.focus();
    });
    roleRows.append(fragment);
  }
  roleEmpty.hidden = roleRows.childElementCount !== 0;
  roleEmpty.textContent = term ? "没有匹配的关联组。" : "还没有角色昵称关联组。";
}

function setSettings(data) {
  settings = data.settings;
  token = data.revision;
  snapshot = JSON.stringify(settings);
  enabled.checked = settings.natural_language_enabled;
  timeout.value = settings.natural_language_timeout;
  provider.replaceChildren(new Option("使用当前会话的模型", ""));
  for (const { id, model } of data.providers) {
    provider.add(new Option(model && model !== id ? `${id} · ${model}` : id, id));
  }
  const selected = settings.natural_language_provider;
  if (selected && !data.providers.some(({ id }) => id === selected)) {
    provider.add(new Option(`${selected}（当前未加载）`, selected));
  }
  provider.value = selected;
  reviewEnabled.checked = settings.review_enabled;
  persona.required = reviewEnabled.checked;
  const personas = data.personas;
  persona.replaceChildren(new Option(personas.length ? "请选择人格" : "暂无可用人格", ""));
  for (const name of personas) persona.add(new Option(name, name));
  const selectedPersona = settings.review_persona;
  if (selectedPersona && !personas.includes(selectedPersona)) {
    persona.add(new Option(`${selectedPersona}（当前不可用）`, selectedPersona));
  }
  persona.value = selectedPersona;
  showAliases();
  showRoleNicknames();
}

async function load() {
  if (!bridge) {
    announce("请从 AstrBot 的扩展功能打开此页。", "error");
    updateButtons();
    return;
  }
  busy = true;
  updateButtons();
  announce("正在读取设置…");
  try {
    await bridge.ready();
    setSettings(await bridge.apiGet("settings"));
    announce("已载入设置。");
  } catch (error) {
    announce(error.message || "读取设置失败，请重新载入。", "error");
  } finally {
    busy = false;
    updateButtons();
  }
}

reload.addEventListener("click", load);
for (const button of tabs) {
  button.addEventListener("click", () => { tab = button.dataset.tab; showAliases(); });
}
add.addEventListener("click", () => {
  settings[tab].push({ target: "", aliases: "" });
  showAliases();
  changed();
  rows.lastElementChild.querySelector(".target").focus();
});
enabled.addEventListener("change", () => { settings.natural_language_enabled = enabled.checked; changed(); });
roleSearch.addEventListener("input", showRoleNicknames);
roleSearch.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.isComposing) event.preventDefault();
});
addRole.addEventListener("click", () => {
  roleSearch.value = "";
  settings.role_nicknames.push({ nicknames: "", roles: [{ server: "", name: "" }] });
  showRoleNicknames();
  changed();
  roleRows.lastElementChild.querySelector("textarea").focus();
});
provider.addEventListener("change", () => { settings.natural_language_provider = provider.value; changed(); });
timeout.addEventListener("input", () => { settings.natural_language_timeout = timeout.valueAsNumber; changed(); });
reviewEnabled.addEventListener("change", () => {
  settings.review_enabled = reviewEnabled.checked;
  persona.required = reviewEnabled.checked;
  changed();
});
persona.addEventListener("change", () => { settings.review_persona = persona.value; changed(); });

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !dirty() || !form.reportValidity()) return;
  busy = true;
  updateButtons();
  announce("正在保存设置…");
  try {
    setSettings(await bridge.apiPost("settings", { revision: token, settings }));
    announce("已保存，新查询会使用这些设置。", "success");
  } catch (error) {
    announce(error.message || "保存失败，请重试。", "error");
  } finally {
    busy = false;
    updateButtons();
  }
});

load();
