const rowsEl = document.getElementById("hall-rows");
const emptyEl = document.getElementById("hall-empty");
const summaryEl = document.getElementById("hall-summary");
const searchEl = document.getElementById("hall-search");
const statusEl = document.getElementById("hall-status");
const errorEl = document.getElementById("hall-error");

const state = {
  halls: [],
  sourceFilter: "all",
  statusFilter: "all",
};

function setStatus(message) {
  statusEl.textContent = message || "";
}

function setError(message) {
  errorEl.textContent = message || "";
  errorEl.classList.toggle("hidden", !message);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatStamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function visibleHalls() {
  const query = searchEl.value.trim().toLowerCase();
  return state.halls.filter((hall) => {
    if (state.sourceFilter !== "all" && hall.source !== state.sourceFilter) return false;
    if (state.statusFilter !== "all" && hall.status !== state.statusFilter) return false;
    if (!query) return true;
    return `${hall.name} ${hall.prefecture_label} ${hall.group_label} ${hall.id}`.toLowerCase().includes(query);
  });
}

function render() {
  const rows = visibleHalls();
  rowsEl.innerHTML = rows
    .map((hall) => {
      const checked = hall.enabled ? "checked" : "";
      const label = hall.enabled ? "有効" : "無効";
      return `<tr data-id="${escapeHtml(hall.id)}">
        <td>
          <div class="admin-name">
            <strong>${escapeHtml(hall.name)}</strong>
            <span>${escapeHtml(hall.group_label || "")}</span>
          </div>
        </td>
        <td>${escapeHtml(hall.prefecture_label || "—")}</td>
        <td><span class="badge source-${escapeHtml(hall.source)}">${escapeHtml(hall.source_label || "—")}</span>${hall.data_public ? "" : ' <span class="badge badge-off">未公開</span>'}</td>
        <td>${escapeHtml(formatStamp(hall.fetched_at))}</td>
        <td><span class="status-pill status-${escapeHtml(hall.status)}">${escapeHtml(hall.status_label || "—")}</span></td>
        <td>
          <label class="switch">
            <input type="checkbox" data-action="toggle" ${checked} aria-label="${escapeHtml(hall.name)}の取得を${label}" />
            <span>${label}</span>
          </label>
        </td>
      </tr>`;
    })
    .join("");
  emptyEl.classList.toggle("hidden", rows.length > 0);
  const enabled = state.halls.filter((item) => item.enabled).length;
  summaryEl.textContent = `登録 ${state.halls.length} 店 / 有効 ${enabled} 店 / 表示 ${rows.length} 店。成功 ${countStatus("ok")}・エラー ${countStatus("error")}・未取得 ${countStatus("pending")}・アーカイブ ${countStatus("archived")}。`;
}

function countStatus(status) {
  return state.halls.filter((item) => item.status === status).length;
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `通信に失敗しました (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : "保存に失敗しました");
  }
  return payload;
}

async function loadHalls() {
  setError("");
  const payload = await readJson(await fetch("/master-panel-x7k3q9/hall-rows"));
  state.halls = payload.halls || [];
  render();
}

async function toggleHall(id, enabled, input) {
  setError("");
  setStatus(enabled ? "有効にしています…" : "無効にしています…");
  input.disabled = true;
  try {
    const saved = await readJson(
      await fetch(`/master-panel-x7k3q9/hall-rows/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }),
    );
    const index = state.halls.findIndex((item) => item.id === id);
    if (index >= 0) state.halls[index] = { ...state.halls[index], ...saved };
    setStatus(`${saved.name} を${saved.enabled ? "有効" : "無効"}にしました`);
    render();
  } catch (error) {
    input.checked = !enabled;
    input.disabled = false;
    setStatus("");
    setError(error.message || "切り替えに失敗しました");
  }
}

searchEl.addEventListener("input", render);
document.querySelectorAll("[data-source-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.sourceFilter = button.dataset.sourceFilter;
    document.querySelectorAll("[data-source-filter]").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    render();
  });
});
document.querySelectorAll("[data-status-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.statusFilter = button.dataset.statusFilter;
    document.querySelectorAll("[data-status-filter]").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    render();
  });
});
rowsEl.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.dataset.action !== "toggle") return;
  const row = input.closest("tr");
  if (!row) return;
  toggleHall(row.dataset.id, input.checked, input);
});

loadHalls().catch((error) => {
  setError(error.message || "ホール一覧を読み込めませんでした");
});
