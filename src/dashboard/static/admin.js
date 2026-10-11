const masterRowsEl = document.getElementById("master-rows");
const masterEmptyEl = document.getElementById("master-empty");
const masterSummaryEl = document.getElementById("master-summary");
const masterSearchEl = document.getElementById("master-search");
const inspectionRowsEl = document.getElementById("inspection-rows");
const inspectionEmptyEl = document.getElementById("inspection-empty");
const inspectionSummaryEl = document.getElementById("inspection-summary");
const inspectionSearchEl = document.getElementById("inspection-search");
const statusEl = document.getElementById("admin-status");
const errorEl = document.getElementById("admin-error");
const rejudgeBtn = document.getElementById("rejudge-btn");

const HIT_LABELS = { bb: "BB", rb: "RB", combined: "合算" };
const TYPE_LABELS = { A: "Aタイプ", AT: "AT機" };

const state = {
  machines: [],
  inspection: [],
  judgedAt: null,
  masterFilter: "all",
  inspectFilter: "all",
};

function setStatus(message) {
  statusEl.textContent = message || "";
}

function setError(message) {
  errorEl.textContent = message || "";
  errorEl.classList.toggle("hidden", !message);
}

function probText(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const rounded = Math.round(number * 10) / 10;
  const text = Number.isInteger(rounded) ? String(rounded) : String(rounded);
  return `1/${text}`;
}

function countText(value) {
  if (value == null || value === "") return "-";
  return String(value);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `通信に失敗しました (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : "保存に失敗しました");
  }
  return payload;
}

function machineById(id) {
  return state.machines.find((item) => item.id === id);
}

function rowValues(row) {
  const valueOf = (name) => row.querySelector(`[data-field="${name}"]`).value;
  const optional = (name) => {
    const raw = valueOf(name).trim();
    return raw === "" ? null : Number(raw);
  };
  return {
    type: valueOf("type"),
    hit_target: valueOf("hit_target"),
    setting_6: Number(valueOf("setting_6")),
    setting_5: Number(valueOf("setting_5")),
    setting_4: Number(valueOf("setting_4")),
    rb_6: optional("rb_6"),
    rb_5: optional("rb_5"),
    rb_4: optional("rb_4"),
  };
}

function sameNumber(left, right) {
  if (left == null && right == null) return true;
  if (left == null || right == null) return false;
  return Math.round(Number(left) * 10000) === Math.round(Number(right) * 10000);
}

function isDirty(machine, values) {
  return (
    values.type !== machine.type ||
    values.hit_target !== machine.hit_target ||
    !sameNumber(values.setting_6, machine.setting_6) ||
    !sameNumber(values.setting_5, machine.setting_5) ||
    !sameNumber(values.setting_4, machine.setting_4) ||
    !sameNumber(values.rb_6, machine.rb_6) ||
    !sameNumber(values.rb_5, machine.rb_5) ||
    !sameNumber(values.rb_4, machine.rb_4)
  );
}

function numberInput(field, value) {
  const shown = value == null ? "" : String(value);
  return `<input data-field="${field}" type="number" inputmode="decimal" min="0.1" max="100000" step="0.1" value="${shown}" />`;
}

function hint(machine, field) {
  const base = machine.base || {};
  if (sameNumber(machine[field], base[field]) && machine[field] != null) return "";
  if (base[field] == null) return "";
  return `<span class="base-hint">マスター ${probText(base[field])}</span>`;
}

function renderMasters() {
  const query = masterSearchEl.value.trim().toLowerCase();
  const rows = state.machines.filter((machine) => {
    if (state.masterFilter === "override" && !machine.override_active) return false;
    if ((state.masterFilter === "A" || state.masterFilter === "AT") && machine.type !== state.masterFilter) return false;
    if (!query) return true;
    return `${machine.name} ${machine.series || ""} ${machine.id}`.toLowerCase().includes(query);
  });
  masterRowsEl.innerHTML = rows
    .map((machine) => {
      const badges = [
        machine.override_active ? '<span class="badge">補正</span>' : "",
        machine.enabled ? "" : '<span class="badge badge-off">対象外</span>',
      ].join("");
      const hitValue = machine.type === "AT" ? "rb" : machine.hit_target;
      return `<tr data-id="${machine.id}">
        <td>
          <div class="admin-name">
            <strong>${escapeHtml(machine.name)}</strong>
            <span>${badges}${escapeHtml(machine.series || machine.id)}</span>
          </div>
        </td>
        <td>
          <select data-field="type">
            <option value="A" ${machine.type === "A" ? "selected" : ""}>Aタイプ</option>
            <option value="AT" ${machine.type === "AT" ? "selected" : ""}>AT機</option>
          </select>
        </td>
        <td>
          <select data-field="hit_target" ${machine.type === "AT" ? "disabled" : ""} title="${machine.type === "AT" ? "AT機の初当りはRB固定" : ""}">
            <option value="combined" ${hitValue === "combined" ? "selected" : ""}>合算</option>
            <option value="bb" ${hitValue === "bb" ? "selected" : ""}>BB</option>
            <option value="rb" ${hitValue === "rb" ? "selected" : ""}>RB</option>
          </select>
        </td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("setting_6", machine.setting_6)}${hint(machine, "setting_6")}</td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("setting_5", machine.setting_5)}${hint(machine, "setting_5")}</td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("setting_4", machine.setting_4)}${hint(machine, "setting_4")}</td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("rb_6", machine.rb_6)}${hint(machine, "rb_6")}</td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("rb_5", machine.rb_5)}${hint(machine, "rb_5")}</td>
        <td class="prob-cell"><span class="prob-prefix">1/</span>${numberInput("rb_4", machine.rb_4)}${hint(machine, "rb_4")}</td>
        <td>
          <div class="row-actions">
            <button type="button" data-action="save">保存</button>
            <button type="button" data-action="reset" ${machine.override_active ? "" : "disabled"}>戻す</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
  masterEmptyEl.classList.toggle("hidden", rows.length > 0);
  const overrideCount = state.machines.filter((item) => item.override_active).length;
  masterSummaryEl.textContent = `${state.machines.length} 機種を登録中。手動補正 ${overrideCount} 件。理論値は 1/N の分母です。`;
}

function renderInspection() {
  const query = inspectionSearchEl.value.trim().toLowerCase();
  const rows = state.inspection.filter((item) => {
    if (state.inspectFilter !== "all" && item.status !== state.inspectFilter) return false;
    if (!query) return true;
    return `${item.hall_name || ""} ${item.machine_name || ""}`.toLowerCase().includes(query);
  });
  inspectionRowsEl.innerHTML = rows
    .map((item) => {
      const actual = item.judgment_type === "A" && item.hit_target === "combined"
        ? `合算 ${probText(item.synthetic_probability)} / REG ${probText(item.rb_probability)}`
        : probText(item.synthetic_probability);
      return `<tr class="row-${item.status}">
        <td>${escapeHtml(item.hall_name || "-")}</td>
        <td>${escapeHtml(item.machine_name || "-")}${item.override_active ? ' <span class="badge">補正</span>' : ""}</td>
        <td>${escapeHtml(item.status_label || "-")}</td>
        <td>${escapeHtml(TYPE_LABELS[item.judgment_type] || item.judgment_type || "-")}</td>
        <td>${escapeHtml(HIT_LABELS[item.hit_target] || item.hit_target || "-")}</td>
        <td>${countText(item.unit_count)}</td>
        <td>${countText(item.total_games)}</td>
        <td>${countText(item.bb_count)}</td>
        <td>${countText(item.rb_count)}</td>
        <td>${actual}</td>
        <td>${probText(item.setting_6)}</td>
        <td>${probText(item.setting_5)}</td>
        <td>${probText(item.setting_4)}</td>
      </tr>`;
    })
    .join("");
  inspectionEmptyEl.classList.toggle("hidden", rows.length > 0);
  const stamp = state.judgedAt ? `判定日時 ${state.judgedAt}` : "判定結果はまだありません";
  inspectionSummaryEl.textContent = `${stamp}。全6 / 全56 / 全456が ${state.inspection.length} 件。実数と、その判定に使った理論値です。`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

const panelBase = window.location.pathname.replace(/\/$/, "");

async function loadMasters() {
  const payload = await readJson(await fetch(`${panelBase}/masters`));
  state.machines = payload.machines || [];
  renderMasters();
}

async function loadInspection() {
  const payload = await readJson(await fetch(`${panelBase}/inspection`));
  state.inspection = payload.rows || [];
  state.judgedAt = payload.judged_at;
  renderInspection();
}

async function saveRow(row) {
  const id = row.dataset.id;
  const machine = machineById(id);
  const values = rowValues(row);
  setError("");
  setStatus(`${machine.name} を保存しています…`);
  const saved = await readJson(
    await fetch(`${panelBase}/masters/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    }),
  );
  const index = state.machines.findIndex((item) => item.id === id);
  if (index >= 0) state.machines[index] = saved;
  renderMasters();
  setStatus(
    saved.override_active
      ? `${saved.name} の補正を保存しました。次の40分巡回と再判定でこの値が優先されます。`
      : `${saved.name} はマスター値のままです。`,
  );
}

async function resetRow(row) {
  const id = row.dataset.id;
  const machine = machineById(id);
  setError("");
  setStatus(`${machine.name} をマスター値に戻しています…`);
  const saved = await readJson(
    await fetch(`${panelBase}/masters/${encodeURIComponent(id)}`, { method: "DELETE" }),
  );
  const index = state.machines.findIndex((item) => item.id === id);
  if (index >= 0) state.machines[index] = saved;
  renderMasters();
  setStatus(`${saved.name} の手動補正を外しました。`);
}

masterRowsEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const row = button.closest("tr");
  button.disabled = true;
  try {
    if (button.dataset.action === "save") await saveRow(row);
    if (button.dataset.action === "reset") await resetRow(row);
  } catch (error) {
    setError(error.message);
    setStatus("");
    button.disabled = false;
  }
});

function syncHitTarget(row) {
  const type = row.querySelector('[data-field="type"]');
  const hit = row.querySelector('[data-field="hit_target"]');
  if (!type || !hit) return;
  if (type.value === "AT") {
    hit.value = "rb";
    hit.disabled = true;
    hit.title = "AT機の初当りはデータサイトのRB回数で固定です";
  } else {
    hit.disabled = false;
    hit.title = "";
  }
}

masterRowsEl.addEventListener("input", (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  const machine = machineById(row.dataset.id);
  if (!machine) return;
  row.classList.toggle("is-dirty", isDirty(machine, rowValues(row)));
});

masterRowsEl.addEventListener("change", (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  if (event.target.matches('[data-field="type"]')) {
    syncHitTarget(row);
  }
  const machine = machineById(row.dataset.id);
  if (!machine) return;
  row.classList.toggle("is-dirty", isDirty(machine, rowValues(row)));
});

masterSearchEl.addEventListener("input", renderMasters);
inspectionSearchEl.addEventListener("input", renderInspection);

document.querySelectorAll("[data-master-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.masterFilter = button.dataset.masterFilter;
    document.querySelectorAll("[data-master-filter]").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    renderMasters();
  });
});

document.querySelectorAll("[data-inspect-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.inspectFilter = button.dataset.inspectFilter;
    document.querySelectorAll("[data-inspect-filter]").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    renderInspection();
  });
});

rejudgeBtn.addEventListener("click", async () => {
  const dirty = masterRowsEl.querySelector("tr.is-dirty");
  if (dirty && !window.confirm("保存していない修正があります。いまの補正で再判定しますか？")) return;
  rejudgeBtn.disabled = true;
  setError("");
  setStatus("取得済みデータで再判定しています…");
  try {
    const result = await readJson(await fetch(`${panelBase}/rejudge`, { method: "POST" }));
    await loadInspection();
    setStatus(
      `再判定しました。全6 ${result.all_high_count} / 全56 ${result.high_56_count} / 全456 ${result.high_base_count}。ダッシュボードにも反映されています。`,
    );
  } catch (error) {
    setError(error.message);
    setStatus("");
  } finally {
    rejudgeBtn.disabled = false;
  }
});

const eventForm = document.getElementById("event-form");
const eventRowsEl = document.getElementById("event-rows");
const eventEmptyEl = document.getElementById("event-empty");
const eventSummaryEl = document.getElementById("event-summary");
const eventDateEl = document.getElementById("event-date");
const eventTargetEl = document.getElementById("event-target");
const eventTypeEl = document.getElementById("event-type");
const eventTitleEl = document.getElementById("event-title");
const eventIdEl = document.getElementById("event-id");
const eventSaveEl = document.getElementById("event-save");
const eventCancelEl = document.getElementById("event-cancel");

const eventState = {
  events: [],
  today: "",
  monitor: null,
};

function fillEventTargets(targets) {
  const groups = targets.groups || [];
  const halls = targets.halls || [];
  const groupOptions = groups
    .map((group) => `<option value="group:${escapeHtml(group.id)}">${escapeHtml(group.label)}（系列・${group.hall_count}店）</option>`)
    .join("");
  const hallOptions = halls
    .map((hall) => `<option value="hall:${escapeHtml(hall.id)}">${escapeHtml(hall.name)}（${escapeHtml(hall.group_label || "店舗")}）</option>`)
    .join("");
  eventTargetEl.innerHTML = `<optgroup label="系列">${groupOptions}</optgroup><optgroup label="店舗">${hallOptions}</optgroup>`;
}

function resetEventForm() {
  eventIdEl.value = "";
  eventDateEl.value = eventState.today || "";
  eventTypeEl.value = "合同";
  eventTitleEl.value = "";
  eventSaveEl.textContent = "登録";
  eventCancelEl.classList.add("hidden");
}

function eventTargetValue(event) {
  if (event.group && !event.hall_id) {
    return `group:${event.group}`;
  }
  if (event.hall_id) {
    return `hall:${event.hall_id}`;
  }
  return event.group ? `group:${event.group}` : "";
}

function renderEvents() {
  eventRowsEl.innerHTML = eventState.events
    .map((event) => `<tr data-id="${escapeHtml(event.id)}">
        <td>${escapeHtml(event.date)}</td>
        <td>${escapeHtml(event.event_type)}</td>
        <td>${escapeHtml(event.target_label || "-")}</td>
        <td>${escapeHtml(event.badge || event.event_title)}</td>
        <td>
          <div class="row-actions">
            <button type="button" data-event-action="edit">編集</button>
            <button type="button" data-event-action="delete">削除</button>
          </div>
        </td>
      </tr>`)
    .join("");
  eventEmptyEl.classList.toggle("hidden", eventState.events.length > 0);
  const todayCount = eventState.events.filter((event) => event.date === eventState.today).length;
  const autoCount = eventState.events.filter((event) => String(event.source || "").startsWith("x:")).length;
  const synced = eventState.monitor && eventState.monitor.last_synced_at
    ? ` 最終自動取得 ${eventState.monitor.last_synced_at}（追加 ${eventState.monitor.added || 0} / 更新 ${eventState.monitor.updated || 0}）。`
    : "";
  eventSummaryEl.textContent = `登録 ${eventState.events.length} 件（自動 ${autoCount} 件）。本日（${eventState.today || "-"}）は ${todayCount} 件がサマリーに出ます。${synced}`;
}

async function loadEvents() {
  const payload = await readJson(await fetch(`${panelBase}/events`));
  eventState.events = payload.events || [];
  eventState.today = payload.today || "";
  eventState.monitor = payload.x_monitor || null;
  fillEventTargets(payload.targets || {});
  if (!eventIdEl.value) {
    eventDateEl.value = eventState.today;
  }
  renderEvents();
}

function editingEventPayload() {
  return {
    date: eventDateEl.value,
    target: eventTargetEl.value,
    event_type: eventTypeEl.value,
    event_title: eventTitleEl.value.trim(),
  };
}

eventForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  eventSaveEl.disabled = true;
  const id = eventIdEl.value;
  const body = JSON.stringify(editingEventPayload());
  try {
    if (id) {
      setStatus("イベントを更新しています…");
      await readJson(
        await fetch(`${panelBase}/events/${encodeURIComponent(id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body,
        }),
      );
      setStatus("イベントを更新しました。ダッシュボードの本日分に反映されます。");
    } else {
      setStatus("イベントを登録しています…");
      await readJson(
        await fetch(`${panelBase}/events`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        }),
      );
      setStatus("イベントを登録しました。ダッシュボードの本日分に反映されます。");
    }
    resetEventForm();
    await loadEvents();
  } catch (error) {
    setError(error.message);
    setStatus("");
  } finally {
    eventSaveEl.disabled = false;
  }
});

eventCancelEl?.addEventListener("click", () => {
  resetEventForm();
  setStatus("");
});

eventRowsEl?.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const row = button.closest("tr");
  const id = row?.dataset.id;
  const current = eventState.events.find((item) => item.id === id);
  if (!current) return;
  if (button.dataset.eventAction === "edit") {
    eventIdEl.value = current.id;
    eventDateEl.value = current.date;
    eventTypeEl.value = current.event_type;
    eventTitleEl.value = current.event_title;
    eventTargetEl.value = eventTargetValue(current);
    eventSaveEl.textContent = "更新";
    eventCancelEl.classList.remove("hidden");
    eventForm?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (button.dataset.eventAction !== "delete") return;
  if (!window.confirm(`${current.date} ${current.event_title} を削除しますか？`)) return;
  setError("");
  setStatus("イベントを削除しています…");
  try {
    await readJson(await fetch(`${panelBase}/events/${encodeURIComponent(id)}`, { method: "DELETE" }));
    if (eventIdEl.value === id) resetEventForm();
    await loadEvents();
    setStatus("イベントを削除しました。");
  } catch (error) {
    setError(error.message);
    setStatus("");
  }
});

Promise.all([loadMasters(), loadInspection(), loadEvents()]).catch((error) => {
  setError(error.message || "管理データを読み込めませんでした");
});
