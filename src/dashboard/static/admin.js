const masterRowsEl = document.getElementById("master-rows");
const masterEmptyEl = document.getElementById("master-empty");
const masterSummaryEl = document.getElementById("master-summary");
const logicSummaryEl = document.getElementById("logic-summary");
const masterSearchEl = document.getElementById("master-search");
const inspectionRowsEl = document.getElementById("inspection-rows");
const inspectionEmptyEl = document.getElementById("inspection-empty");
const inspectionSummaryEl = document.getElementById("inspection-summary");
const inspectionSearchEl = document.getElementById("inspection-search");
const statusEl = document.getElementById("admin-status");
const errorEl = document.getElementById("admin-error");
const rejudgeBtn = document.getElementById("rejudge-btn");
const createForm = document.getElementById("create-master-form");
const newLevelsEl = document.getElementById("new-levels");
const newTypeEl = document.getElementById("new-type");
const newHitEl = document.getElementById("new-hit");

const HIT_LABELS = { bb: "BB", rb: "RB", combined: "合算" };
const TYPE_LABELS = { A: "Aタイプ", AT: "AT機" };
const LEVELS = [1, 2, 3, 4, 5, 6];

const state = {
  machines: [],
  inspection: [],
  judgedAt: null,
  logic: {},
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

function optionalNumber(raw) {
  const text = String(raw ?? "").trim();
  return text === "" ? null : Number(text);
}

function rowValues(row) {
  const valueOf = (name) => {
    const field = row.querySelector(`[data-field="${name}"]`);
    return field ? field.value : "";
  };
  const payload = {
    type: valueOf("type"),
    hit_target: valueOf("hit_target"),
    min_games: optionalNumber(valueOf("min_games")),
    early_min_games: optionalNumber(valueOf("early_min_games")),
    note: valueOf("note").trim(),
  };
  LEVELS.forEach((level) => {
    payload[`setting_${level}`] = optionalNumber(valueOf(`setting_${level}`));
    payload[`rb_${level}`] = optionalNumber(valueOf(`rb_${level}`));
  });
  const name = row.querySelector('[data-field="name"]');
  if (name) payload.name = name.value.trim();
  return payload;
}

function sameNumber(left, right) {
  if (left == null && right == null) return true;
  if (left == null || right == null) return false;
  return Math.round(Number(left) * 10000) === Math.round(Number(right) * 10000);
}

function isDirty(machine, values) {
  if (values.name != null && values.name !== machine.name) return true;
  if ((values.note || "") !== (machine.note || "")) return true;
  if (values.type !== machine.type || values.hit_target !== machine.hit_target) return true;
  if (!sameNumber(values.min_games, machine.min_games)) return true;
  if (!sameNumber(values.early_min_games, machine.early_min_games)) return true;
  return LEVELS.some(
    (level) =>
      !sameNumber(values[`setting_${level}`], machine[`setting_${level}`]) ||
      !sameNumber(values[`rb_${level}`], machine[`rb_${level}`]),
  );
}

function numberInput(field, value) {
  const shown = value == null ? "" : String(value);
  return `<input data-field="${field}" type="number" inputmode="decimal" min="0.1" max="100000" step="0.1" value="${shown}" />`;
}

function originMark(origin) {
  if (origin === "derived") return '<span class="origin-mark">算出</span>';
  if (origin === "override") return '<span class="origin-mark">補正</span>';
  return "";
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function historyHtml(history) {
  const items = (history || []).slice(-3).reverse();
  if (!items.length) return "";
  return `<ul class="edit-history">${items
    .map((item) => `<li>${escapeHtml(formatTime(item.at))} ${escapeHtml(item.summary || "")}</li>`)
    .join("")}</ul>`;
}

function levelCell(machine, level) {
  const primary = machine.type === "A" ? "合算" : "初当";
  const syn = `setting_${level}`;
  const rb = `rb_${level}`;
  const rbTitle = machine.type === "AT" ? "AT機の判定では初当りだけを使います" : "REG確率の分母";
  return `<td class="level-cell">
      <label class="mini-field">${primary}${numberInput(syn, machine[syn])}${originMark(machine.origins?.[syn])}</label>
      <label class="mini-field" title="${rbTitle}">REG${numberInput(rb, machine[rb])}${originMark(machine.origins?.[rb])}</label>
    </td>`;
}

function renderLogic() {
  const logic = state.logic || {};
  const points = logic.score_points || {};
  const tolerance = Math.round(Number(logic.tolerance || 0) * 1000) / 10;
  const weight = (value) => Math.round(Number(value || 0) * 100);
  logicSummaryEl.innerHTML = `<ul class="logic-list">
      <li>確率は 1/N の分母です。Aタイプは合算とREGの両方、AT機は初当り（RB回数）だけを判定に使います。</li>
      <li>設定5・4が無い機種は設定6から算出します（設定5 ×${logic.derive_setting5}、設定4の合算 ×${logic.derive_setting4_synthetic}、REG ×${logic.derive_setting4_rb}）。算出した欄には「算出」と出ます。</li>
      <li>サンプル ${logic.margin_min_games}G 以上では、設定6と設定5を理論値の悪い方向へ ${tolerance}% まで許容します。</li>
      <li>ジャグラー島のスコアは REG ${weight(logic.score_weight_rb)}%・合算 ${weight(logic.score_weight_synthetic)}%・回転 ${weight(logic.score_weight_games)}%。点数は設定6=${points["6"]}、設定5=${points["5"]}、設定4=${points["4"]}です。</li>
      <li>回転スコアは規定G×${logic.score_games_full_ratio} または ${logic.score_games_full}G で100点、規定Gで80点、${logic.score_games_forming}Gで45点、${logic.score_games_early}Gで20点です。朝一G未満は高設定欄に出しません。</li>
    </ul>`;
}

function renderMasters() {
  const query = masterSearchEl.value.trim().toLowerCase();
  const rows = state.machines.filter((machine) => {
    if (state.masterFilter === "override" && !machine.override_active) return false;
    if (state.masterFilter === "added" && !machine.added) return false;
    if ((state.masterFilter === "A" || state.masterFilter === "AT") && machine.type !== state.masterFilter) return false;
    if (!query) return true;
    return `${machine.name} ${machine.series || ""} ${machine.id} ${machine.note || ""}`.toLowerCase().includes(query);
  });
  masterRowsEl.innerHTML = rows
    .map((machine) => {
      const badges = [
        machine.added ? '<span class="badge">追加</span>' : "",
        machine.override_active ? '<span class="badge">補正</span>' : "",
        machine.enabled ? "" : '<span class="badge badge-off">対象外</span>',
      ].join("");
      const hitValue = machine.type === "AT" ? "rb" : machine.hit_target;
      const nameCell = machine.added
        ? `<input data-field="name" type="text" maxlength="80" value="${escapeHtml(machine.name)}" />`
        : `<strong>${escapeHtml(machine.name)}</strong>`;
      return `<tr data-id="${escapeHtml(machine.id)}">
        <td>
          <div class="admin-name">
            ${nameCell}
            <span>${badges}${escapeHtml(machine.series || machine.id)}</span>
          </div>
        </td>
        <td class="type-cell">
          <select data-field="type">
            <option value="A" ${machine.type === "A" ? "selected" : ""}>Aタイプ</option>
            <option value="AT" ${machine.type === "AT" ? "selected" : ""}>AT機</option>
          </select>
          <select data-field="hit_target" ${machine.type === "AT" ? "disabled" : ""} title="${machine.type === "AT" ? "AT機の初当りはRB固定" : "Aタイプの初当り対象"}">
            <option value="combined" ${hitValue === "combined" ? "selected" : ""}>合算</option>
            <option value="bb" ${hitValue === "bb" ? "selected" : ""}>BB</option>
            <option value="rb" ${hitValue === "rb" ? "selected" : ""}>RB</option>
          </select>
        </td>
        ${LEVELS.map((level) => levelCell(machine, level)).join("")}
        <td class="level-cell">
          <label class="mini-field">規定G<input data-field="min_games" type="number" min="1" max="100000" step="1" value="${machine.min_games ?? ""}" /></label>
          <label class="mini-field">朝一G<input data-field="early_min_games" type="number" min="1" max="100000" step="1" value="${machine.early_min_games ?? ""}" /></label>
        </td>
        <td class="note-cell">
          <textarea data-field="note" maxlength="400" placeholder="メモ・根拠">${escapeHtml(machine.note || "")}</textarea>
          ${historyHtml(machine.history)}
        </td>
        <td>
          <div class="row-actions">
            <button type="button" data-action="save">保存</button>
            <button type="button" data-action="reset" ${machine.added || machine.override_active ? "" : "disabled"}>${machine.added ? "削除" : "戻す"}</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
  masterEmptyEl.classList.toggle("hidden", rows.length > 0);
  const overrideCount = state.machines.filter((item) => item.override_active).length;
  const addedCount = state.machines.filter((item) => item.added).length;
  masterSummaryEl.textContent = `${state.machines.length} 機種。手動補正 ${overrideCount} 件、画面から追加 ${addedCount} 件。表示中の数値が、いまの判定が読んでいる値です。`;
}

function renderNewLevels() {
  newLevelsEl.innerHTML = [...LEVELS]
    .reverse()
    .map((level) => {
      const placeholder = level === 5 || level === 4 ? "空欄は算出" : "";
      return `<fieldset>
        <legend>設定${level}</legend>
        <label>初当り / 合算
          <input id="new-setting-${level}" type="number" inputmode="decimal" min="0.1" max="100000" step="0.1" placeholder="${placeholder}" ${level === 6 ? "required" : ""} />
        </label>
        <label>REG
          <input id="new-rb-${level}" type="number" inputmode="decimal" min="0.1" max="100000" step="0.1" />
        </label>
      </fieldset>`;
    })
    .join("");
}

function syncNewHitTarget() {
  if (newTypeEl.value === "AT") {
    newHitEl.value = "rb";
    newHitEl.disabled = true;
    newHitEl.title = "AT機の初当りはデータサイトのRB回数で固定です";
  } else {
    newHitEl.disabled = false;
    newHitEl.value = "combined";
    newHitEl.title = "Aタイプの初当り対象";
  }
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
  state.logic = payload.logic || {};
  renderLogic();
  renderMasters();
}

async function loadInspection() {
  const payload = await readJson(await fetch(`${panelBase}/inspection`));
  state.inspection = payload.rows || [];
  state.judgedAt = payload.judged_at;
  renderInspection();
}

function saveStatus(saved) {
  const fields = saved.saved_fields || [];
  if (fields.length === 1 && fields[0] === "note") {
    return `${saved.name} の備考を保存しました。`;
  }
  if (saved.override_active || saved.added || fields.length) {
    return `${saved.name} を保存しました。次の判定とジャグラー島のスコアがこの参照数値を使います。`;
  }
  return `${saved.name} はマスターの参照数値のままです。`;
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
  setStatus(saveStatus(saved));
}

async function resetRow(row) {
  const id = row.dataset.id;
  const machine = machineById(id);
  if (machine.added && !window.confirm(`${machine.name} をマスターから削除しますか？`)) return;
  setError("");
  setStatus(machine.added ? `${machine.name} を削除しています…` : `${machine.name} をマスター値に戻しています…`);
  const saved = await readJson(
    await fetch(`${panelBase}/masters/${encodeURIComponent(id)}`, { method: "DELETE" }),
  );
  if (saved.deleted) {
    state.machines = state.machines.filter((item) => item.id !== id);
    renderMasters();
    setStatus(`${machine.name} を削除しました。以降の判定では使いません。`);
    return;
  }
  const index = state.machines.findIndex((item) => item.id === id);
  if (index >= 0) state.machines[index] = saved;
  renderMasters();
  setStatus(`${saved.name} の参照数値をマスター値に戻しました。`);
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
    hit.title = "Aタイプの初当り対象";
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
  if (event.target.matches('[data-field="type"]')) syncHitTarget(row);
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

function newMachinePayload() {
  const payload = {
    name: document.getElementById("new-name").value.trim(),
    type: newTypeEl.value,
    hit_target: newHitEl.value,
    min_games: optionalNumber(document.getElementById("new-min-games").value),
    early_min_games: optionalNumber(document.getElementById("new-early-games").value),
    note: document.getElementById("new-note").value.trim(),
  };
  LEVELS.forEach((level) => {
    payload[`setting_${level}`] = optionalNumber(document.getElementById(`new-setting-${level}`).value);
    payload[`rb_${level}`] = optionalNumber(document.getElementById(`new-rb-${level}`).value);
  });
  return payload;
}

function resetCreateForm() {
  createForm.reset();
  document.getElementById("new-min-games").value = "3000";
  document.getElementById("new-early-games").value = "300";
  syncNewHitTarget();
}

newTypeEl.addEventListener("change", syncNewHitTarget);

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("create-master");
  button.disabled = true;
  setError("");
  setStatus("機種を追加しています…");
  try {
    const saved = await readJson(
      await fetch(`${panelBase}/masters`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(newMachinePayload()),
      }),
    );
    state.machines.unshift(saved);
    state.masterFilter = "added";
    document.querySelectorAll("[data-master-filter]").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.masterFilter === "added");
    });
    resetCreateForm();
    renderMasters();
    setStatus(`${saved.name} を追加しました。次の判定からこの参照数値を使います。`);
  } catch (error) {
    setError(error.message);
    setStatus("");
  } finally {
    button.disabled = false;
  }
});

renderNewLevels();
syncNewHitTarget();

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
