(() => {
  const rowsEl = document.getElementById("contract-rows");
  const emptyEl = document.getElementById("contract-empty");
  const errorEl = document.getElementById("contract-error");
  const statusEl = document.getElementById("contract-status");
  const summaryEl = document.getElementById("contract-summary");
  const queryEl = document.getElementById("contract-query");
  const state = { members: [], filter: "all" };

  const kpi = {
    total: document.getElementById("kpi-total"),
    trialing: document.getElementById("kpi-trialing"),
    active: document.getElementById("kpi-active"),
    ending: document.getElementById("kpi-ending"),
    week: document.getElementById("kpi-week"),
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

  function matches(member, needle) {
    if (state.filter === "ending_this_week") {
      if (!member.ending_this_week) return false;
    } else if (state.filter !== "all" && member.status !== state.filter) {
      return false;
    }
    if (!needle) return true;
    const haystack = [member.email, member.name, member.customer_id, member.user_id, member.subscription_id]
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  }

  function stripeLink(member) {
    const url = String(member.stripe_url || "");
    if (!url.startsWith("https://dashboard.stripe.com/")) return text("span", "contract-sub", "");
    const link = document.createElement("a");
    link.className = "stripe-link";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Stripeで開く";
    return link;
  }

  function render() {
    const needle = queryEl.value.trim().toLowerCase();
    const visible = state.members.filter((member) => matches(member, needle));
    rowsEl.replaceChildren();
    visible.forEach((member) => {
      const row = document.createElement("tr");
      const statusCell = document.createElement("td");
      const badge = text("span", `contract-badge contract-badge-${member.badge || "muted"}`, member.status_label);
      statusCell.append(badge);
      const person = document.createElement("td");
      const block = document.createElement("div");
      block.className = "contract-person";
      block.append(text("strong", "", member.name || member.email || "名前未設定"));
      if (member.name && member.email) block.append(text("span", "", member.email));
      if (member.customer_id) block.append(text("span", "", member.customer_id));
      if (member.plan_name) block.append(text("span", "", member.plan_name));
      person.append(block);
      const trial = document.createElement("td");
      const trialBlock = document.createElement("div");
      trialBlock.className = "contract-trial";
      trialBlock.append(text("span", "", member.trial_range));
      if (member.trial_remaining_label) {
        const soon = member.trial_remaining_label === "終了" ? "remain-ended" : "remain-soon";
        trialBlock.append(text("span", soon, member.trial_remaining_label));
      }
      trial.append(trialBlock);
      row.append(
        statusCell,
        person,
        text("td", "", member.created_at_jst),
        trial,
        text("td", "", member.phase),
        text("td", "", member.next_billing_jst),
        document.createElement("td"),
      );
      row.lastChild.append(stripeLink(member));
      rowsEl.append(row);
    });
    emptyEl.classList.toggle("hidden", visible.length > 0);
    summaryEl.textContent = `${visible.length} 件を表示`;
  }

  function setFilter(next) {
    state.filter = next;
    document.querySelectorAll("[data-filter]").forEach((button) => {
      const on = button.getAttribute("data-filter") === next;
      button.classList.toggle("is-on", on);
      if (button.getAttribute("role") === "tab") button.setAttribute("aria-selected", on ? "true" : "false");
    });
    render();
  }

  function shortDate(value) {
    const match = String(value || "").match(/^\d{4}-(\d{2})-(\d{2})/);
    return match ? `${Number(match[1])}/${Number(match[2])}` : "";
  }

  function fillSummary(summary, generatedAt, mode) {
    kpi.total.textContent = String(summary.total ?? 0);
    kpi.trialing.textContent = String(summary.trialing ?? 0);
    kpi.active.textContent = String(summary.active ?? 0);
    kpi.ending.textContent = String(summary.ending_this_week ?? 0);
    const start = shortDate(summary.week_start_jst);
    const end = shortDate(summary.week_end_jst);
    kpi.week.textContent = start && end ? `${start}〜${end}` : "";
    const place = mode === "test" ? "Stripeテスト" : "Stripe本番";
    statusEl.textContent = generatedAt ? `${generatedAt} 時点 · ${place}` : "";
  }

  async function load() {
    clearError();
    statusEl.textContent = "読み込み中";
    const response = await fetch("/master-panel-x7k3q9/contracts/rows", {
      headers: { accept: "application/json" },
      credentials: "same-origin",
    });
    if (!response.ok) {
      statusEl.textContent = "";
      showError(response.status === 401 ? "管理者認証が必要です。" : "契約一覧を読み込めませんでした。");
      return;
    }
    const payload = await response.json();
    state.members = Array.isArray(payload.members) ? payload.members : [];
    fillSummary(payload.summary || {}, payload.generated_at || "", payload.stripe_mode || "");
    render();
  }

  queryEl.addEventListener("input", render);
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => setFilter(button.getAttribute("data-filter") || "all"));
  });
  load().catch(() => {
    statusEl.textContent = "";
    showError("契約一覧を読み込めませんでした。");
  });
})();
