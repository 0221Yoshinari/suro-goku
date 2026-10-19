(() => {
  const panel = document.body.getAttribute("data-panel") || "";
  const rowsEl = document.getElementById("member-rows");
  const emptyEl = document.getElementById("member-empty");
  const errorEl = document.getElementById("member-error");
  const statusEl = document.getElementById("member-status");
  const summaryEl = document.getElementById("member-summary");
  const queryEl = document.getElementById("member-query");
  const state = { filter: "all", sort: "registered_at", order: "desc", loading: false };

  const kpi = {
    total: document.getElementById("kpi-total"),
    trialing: document.getElementById("kpi-trialing"),
    active: document.getElementById("kpi-active"),
    cvr: document.getElementById("kpi-cvr"),
    note: document.getElementById("kpi-cvr-note"),
  };

  const actionLabels = {
    extend_trial: "無料期間を延長",
    force_active: "強制有料化",
    cancel: "手動解約",
    suspend: "アカウント停止",
  };

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove("hidden");
  }

  function clearError() {
    errorEl.textContent = "";
    errorEl.classList.add("hidden");
  }

  function text(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null || value === "" ? "—" : String(value);
    return node;
  }

  function setFilter(next) {
    state.filter = next;
    document.querySelectorAll("[data-filter]").forEach((button) => {
      const on = button.getAttribute("data-filter") === next;
      button.classList.toggle("is-on", on);
      if (button.getAttribute("role") === "tab") button.setAttribute("aria-selected", on ? "true" : "false");
    });
    load();
  }

  function fillSummary(summary, generatedAt) {
    kpi.total.textContent = String(summary.total ?? 0);
    kpi.trialing.textContent = String(summary.trialing ?? 0);
    kpi.active.textContent = String(summary.active ?? 0);
    kpi.cvr.textContent = summary.cvr_label || "—";
    const converted = summary.converted ?? 0;
    const started = summary.started ?? 0;
    kpi.note.textContent = started ? `${converted} / ${started} 人が有料化` : "体験開始前は分母に入れません";
    statusEl.textContent = generatedAt ? `${generatedAt} 時点 · 15秒ごとに更新` : "";
  }

  function personCell(member) {
    const cell = document.createElement("td");
    const block = document.createElement("div");
    block.className = "contract-person";
    block.append(text("strong", "", member.line_name || "LINE名未取得"));
    block.append(text("span", "", member.line_user_id || member.id));
    cell.append(block);
    return cell;
  }

  function trialCell(member) {
    const cell = document.createElement("td");
    const block = document.createElement("div");
    block.className = "contract-trial";
    block.append(text("span", "", member.trial_end_jst));
    if (member.trial_remaining_label) {
      const soon = member.trial_remaining_label === "終了" ? "remain-ended" : "remain-soon";
      block.append(text("span", soon, member.trial_remaining_label));
    }
    cell.append(block);
    return cell;
  }

  function squareCell(member) {
    const cell = document.createElement("td");
    const block = document.createElement("div");
    block.className = "member-ids";
    block.append(text("span", "contract-sub", member.square_customer_id ? `顧客 ${member.square_customer_id}` : ""));
    block.append(text("span", "contract-sub", member.square_subscription_id ? `定期 ${member.square_subscription_id}` : ""));
    cell.append(block);
    return cell;
  }

  function actionCell(member) {
    const cell = document.createElement("td");
    const form = document.createElement("form");
    form.className = "member-actions";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "ステータス操作");
    Object.entries(actionLabels).forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    });
    const days = document.createElement("input");
    days.type = "number";
    days.min = "1";
    days.max = "90";
    days.value = "7";
    days.setAttribute("aria-label", "延長日数");
    const button = document.createElement("button");
    button.type = "submit";
    button.textContent = "実行";
    form.append(select, days, button);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const action = select.value;
      const label = actionLabels[action] || "この操作";
      const extra = action === "extend_trial" ? `（${days.value}日）` : "";
      const who = member.line_name || member.email || member.line_user_id || member.id;
      if (!window.confirm(`${who} を「${label}」に更新します${extra}。よろしいですか？`)) return;
      runAction(member.id, action, days.value);
    });
    cell.append(form);
    return cell;
  }

  function render(members) {
    rowsEl.replaceChildren();
    members.forEach((member) => {
      const row = document.createElement("tr");
      const statusCell = document.createElement("td");
      statusCell.append(text("span", `contract-badge contract-badge-${member.badge || "muted"}`, member.status_label));
      row.append(
        statusCell,
        personCell(member),
        text("td", "", member.email),
        text("td", "", member.registered_at_jst),
        trialCell(member),
        squareCell(member),
        actionCell(member),
      );
      rowsEl.append(row);
    });
    emptyEl.classList.toggle("hidden", members.length > 0);
    summaryEl.textContent = `${members.length} 件を表示`;
  }

  function queryString() {
    const params = new URLSearchParams();
    const needle = queryEl.value.trim();
    if (needle) params.set("q", needle);
    if (state.filter && state.filter !== "all") params.set("status", state.filter);
    params.set("sort", state.sort);
    params.set("order", state.order);
    return params.toString();
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    clearError();
    try {
      const response = await fetch(`${panel}/members/rows?${queryString()}`, {
        headers: { accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const fallback = response.status === 401 ? "管理者認証が必要です。" : "会員一覧を読み込めませんでした。";
        showError(payload.detail || fallback);
        return;
      }
      const payload = await response.json();
      fillSummary(payload.summary || {}, payload.generated_at || "");
      render(Array.isArray(payload.members) ? payload.members : []);
      document.querySelectorAll(".sort-button").forEach((button) => {
        button.classList.toggle("is-sort", button.getAttribute("data-sort") === state.sort);
      });
    } catch (_error) {
      showError("会員一覧を読み込めませんでした。");
    } finally {
      state.loading = false;
    }
  }

  async function runAction(memberId, action, days) {
    clearError();
    statusEl.textContent = "更新しています";
    try {
      const response = await fetch(`${panel}/members/${encodeURIComponent(memberId)}/action`, {
        method: "POST",
        credentials: "same-origin",
        headers: { accept: "application/json", "content-type": "application/json" },
        body: JSON.stringify({ action, days: Number(days) }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        showError(payload.detail || "会員を更新できませんでした。");
        statusEl.textContent = "";
        return;
      }
      await load();
    } catch (_error) {
      showError("会員を更新できませんでした。");
    }
  }

  queryEl.addEventListener("input", () => {
    window.clearTimeout(queryEl._timer);
    queryEl._timer = window.setTimeout(load, 200);
  });
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => setFilter(button.getAttribute("data-filter") || "all"));
  });
  document.querySelectorAll(".sort-button").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.getAttribute("data-sort") || "registered_at";
      if (state.sort === key) state.order = state.order === "desc" ? "asc" : "desc";
      else {
        state.sort = key;
        state.order = key === "email" || key === "line_name" || key === "status" ? "asc" : "desc";
      }
      load();
    });
  });
  load();
  window.setInterval(() => {
    if (document.hidden) return;
    const active = document.activeElement;
    if (active && (active.tagName === "SELECT" || active.tagName === "INPUT")) return;
    load();
  }, 15000);
})();
