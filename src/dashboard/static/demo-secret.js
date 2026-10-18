(() => {
  const DEMO = JSON.parse(document.getElementById("demo-data").textContent);
  const MODES = ["digits", "lineups", "all", "joint"];
  const STATUS = {
    ALL_HIGH: { label: "全6数値", badge: "badge-ALL_HIGH", card: "card-ALL_HIGH", tier: "s6" },
    HIGH_56: { label: "全56数値", badge: "badge-HIGH_56", card: "card-HIGH_56", tier: "s5" },
    HIGH_BASE: { label: "全456数値", badge: "badge-HIGH_BASE", card: "card-HIGH_BASE", tier: "s4" },
    NORMAL: { label: "通常", badge: "badge-NORMAL", card: "card-NOT_HIGH", tier: "s0" },
  };
  const MODE_COPY = {
    digits: "末尾0〜9ごとに、台数と平均Gを一覧しています。厚い末尾は全56・全456で色が付きます。",
    lineups: "店舗を選んで並び反応を押すと、その店で良さそうな台番号の区間が出ます。ジャグラー、北斗の拳、モンキーターン、カバネリです。",
    all: "白金ステージ天神のマイジャグラーVです。20台が推定1、推定5、推定2、推定6の順で、当たりとハズレが1台ずつ交互です。回転は16:30時点です。",
    joint: "系列を選んでボタンを押すと、2店舗以上に入っている機種だけを比べ、いちばん強い共通機種を出します。回転は昼時点の2,000〜3,000G台です。",
  };

  const state = {
    mode: "all",
    group: "face",
    shown: "face",
    digitHall: DEMO.digits[0].hall_name,
    allHall: "all",
    cityStatus: "all",
    lineupHall: DEMO.lineup_halls[0].hall_name,
    lineupShown: "",
  };

  const leadEl = document.getElementById("mode-lead");
  const statsEl = document.getElementById("mode-stats");
  const pendingEl = document.getElementById("joint-pending");
  const pickEl = document.getElementById("joint-pick");
  const resultEl = document.getElementById("joint-result");
  const lineupPendingEl = document.getElementById("lineup-pending");
  const lineupReactEl = document.getElementById("lineup-react");

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    node.replaceChildren();
  }

  function fmt(value) {
    return new Intl.NumberFormat("ja-JP").format(value);
  }

  function groupById(id) {
    return DEMO.groups.find((group) => group.id === id);
  }

  function readUrl() {
    const params = new URLSearchParams(location.search);
    const groupId = params.get("group");
    if (DEMO.groups.some((group) => group.id === groupId)) {
      state.group = groupId;
      state.shown = groupId;
    }
    const hash = location.hash.replace("#", "");
    if (MODES.includes(hash)) state.mode = hash;
  }

  function syncUrl() {
    const url = new URL(location.href);
    url.searchParams.set("group", state.shown);
    url.hash = state.mode;
    history.replaceState(null, "", url);
  }

  let jumpTimer = 0;

  function jumpTo(selector) {
    const targets = [...document.querySelectorAll(selector)].filter((node) => !node.closest(".view.hidden"));
    if (!targets.length) return;
    targets.forEach((node) => node.classList.add("is-jump-focus"));
    targets[0].scrollIntoView({ behavior: "auto", block: "start" });
    window.clearTimeout(jumpTimer);
    jumpTimer = window.setTimeout(() => {
      targets.forEach((node) => node.classList.remove("is-jump-focus"));
    }, 2400);
  }

  function stat(label, value, selector) {
    const card = el("article", selector ? "stat stat-jump" : "stat");
    card.append(el("p", "stat-label", label), el("p", "stat-value", value));
    if (!selector) return card;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", `${label} ${value}。タップして機種と店舗を見る`);
    card.append(el("p", "signal-jump-hint", "タップして確認"));
    const activate = () => {
      if (typeof selector === "function") selector();
      else jumpTo(selector);
    };
    card.addEventListener("click", activate);
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      activate();
    });
    return card;
  }

  function renderStats() {
    clear(statsEl);
    if (state.mode === "digits") {
      const hall = DEMO.digits.find((item) => item.hall_name === state.digitHall);
      const hot56 = hall.digits.filter((row) => row.status === "HIGH_56").length;
      const hot456 = hall.digits.filter((row) => row.status === "HIGH_BASE").length;
      const best = hall.digits.reduce((top, row) => (row.avg_setting > top.avg_setting ? row : top), hall.digits[0]);
      statsEl.append(
        stat("対象ホール", hall.hall_name),
        stat("全56の末尾", `${hot56}`, "#digit-board .hall-digit-card[data-status='HIGH_56']"),
        stat("全456の末尾", `${hot456}`, "#digit-board .hall-digit-card[data-status='HIGH_BASE']"),
        stat("厚い末尾", `末尾${best.digit}`, `#digit-board .hall-digit-card[data-digit='${best.digit}']`),
      );
      return;
    }
    if (state.mode === "lineups") {
      const hall = lineupHallByName(state.lineupHall);
      const shown = state.lineupShown === state.lineupHall;
      if (!shown) {
        statsEl.append(
          stat("選択中の店舗", hall.hall_name),
          stat("並び反応", "未表示", () => showLineupReaction()),
        );
        return;
      }
      const targets = hall.islands.flatMap((island) => island.units.filter((unit) => unit.target));
      const avgGames = Math.round(targets.reduce((sum, unit) => sum + unit.games, 0) / targets.length);
      statsEl.append(
        stat("並び反応", `${hall.islands.length}件`, "#lineup-list .lineup-event"),
        stat("良さそうな箇所", `${hall.islands.length}区間`, ".unit-row.is-target"),
        stat("対象台", `${targets.length}台`, ".unit-row.is-target"),
        stat("対象の平均G", `${fmt(avgGames)}G`),
      );
      return;
    }
    if (state.mode === "all") {
      const units = DEMO.juggler.units;
      const allHigh = units.filter((row) => row.est === 6).length;
      const high56 = units.filter((row) => row.est === 5).length;
      const normal = units.filter((row) => row.est <= 2).length;
      const tiles = [
        stat("台数", `${units.length}台`, () => showCity("all")),
        stat("全6数値", `${allHigh}台`, () => showCity("ALL_HIGH")),
        stat("全56数値", `${high56}台`, () => showCity("HIGH_56")),
        stat("通常", `${normal}台`, () => showCity("NORMAL")),
      ];
      const activeIndex = { all: 0, ALL_HIGH: 1, HIGH_56: 2, NORMAL: 3 }[state.cityStatus] ?? 0;
      tiles[activeIndex].classList.add("is-active");
      statsEl.append(...tiles);
      return;
    }
    const group = groupById(state.shown);
    const top = group.ranked[0];
    const strong = top.rows.filter((row) => row.status === "HIGH_56" || row.status === "ALL_HIGH").length;
    statsEl.append(
      stat("対象店舗", String(group.halls.length)),
      stat("共通機種", String(group.ranked.length)),
      stat("平均G", `${fmt(top.avg_games)}G`, "#joint-winner-name"),
      stat("設定56の店", `${strong}店`, "#joint-result tr.row-HIGH_56, #joint-result tr.row-ALL_HIGH"),
    );
  }

  function showMode(mode) {
    state.mode = mode;
    MODES.forEach((id) => {
      document.getElementById(`view-${id}`).classList.toggle("hidden", id !== mode);
      const tab = document.getElementById(`tab-${id}`);
      const on = id === mode;
      tab.classList.toggle("is-active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    leadEl.textContent = MODE_COPY[mode];
    renderStats();
  }

  function chip(label, active, onClick) {
    const button = el("button", `filter${active ? " is-active" : ""}`, label);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.addEventListener("click", onClick);
    return button;
  }

  function metricList(pairs) {
    const list = document.createElement("dl");
    pairs.forEach(([label, value]) => {
      list.append(el("dt", "", label), el("dd", "", value));
    });
    return list;
  }

  function badge(text, className) {
    return el("span", `badge ${className}`, text);
  }

  function machineMetrics(row) {
    const pairs = [
      ["台数", `${fmt(row.units)}台`],
      ["通常時G", `${fmt(row.normal_games)}G`],
      ["平均G", `${fmt(row.games)}G`],
    ];
    if (row.kind === "A") {
      pairs.push(["BB", row.bb], ["REG", row.reg]);
    } else {
      pairs.push(["RB回数", `${fmt(row.rb_count)}回`], ["初当り", row.first_hit]);
    }
    return pairs;
  }

  function renderDigits() {
    const root = document.getElementById("digit-halls");
    clear(root);
    DEMO.digits.forEach((hall) => {
      root.append(chip(hall.hall_name, hall.hall_name === state.digitHall, () => {
        state.digitHall = hall.hall_name;
        renderDigits();
        renderStats();
      }));
    });
    const hall = DEMO.digits.find((item) => item.hall_name === state.digitHall);
    document.getElementById("digit-title").textContent = `${hall.hall_name}　末尾0〜9`;
    const hot = hall.digits.filter((row) => row.status !== "NORMAL");
    document.getElementById("digit-caption").textContent = hot.map((row) => (
      `末尾${row.digit}は平均${fmt(row.games)}G（${STATUS[row.status].label}）`
    )).join("。");
    const board = document.getElementById("digit-board");
    clear(board);
    const peak = Math.max(...hall.digits.map((row) => row.avg_setting));
    hall.digits.forEach((row) => {
      const peaked = row.avg_setting === peak;
      const card = el("article", `hall-digit-card ${STATUS[row.status].card}${peaked ? " is-setting-peak" : ""}`);
      card.dataset.status = row.status;
      card.dataset.digit = String(row.digit);
      const head = el("div", "hall-digit-head");
      const num = el("p", "hall-digit-num");
      num.append(el("span", "", "末尾"), document.createTextNode(String(row.digit)));
      head.append(num, badge(STATUS[row.status].label, STATUS[row.status].badge));
      const setting = el("p", "hall-digit-setting");
      const settingValue = el("strong", `setting-tier setting-tier-${row.avg_setting >= 5.5 ? "s6" : row.avg_setting >= 4 ? "high" : "calm"}${peaked ? " setting-peak" : ""}`, row.avg_setting.toFixed(2));
      setting.append(el("span", "", "平均推測設定"), settingValue);
      card.append(head, setting, metricList([
        ["台数", `${fmt(row.units)}台`],
        ["通常時G", `${fmt(row.normal_games)}G`],
        ["平均G", `${fmt(row.games)}G`],
      ]));
      board.append(card);
    });
  }

  function estClass(est) {
    if (est >= 6) return "unit-s6";
    if (est >= 5) return "unit-s5";
    if (est >= 4) return "unit-s4";
    return "unit-s0";
  }

  function lineupHallByName(name) {
    return DEMO.lineup_halls.find((hall) => hall.hall_name === name);
  }

  function showLineupReaction() {
    state.lineupShown = state.lineupHall;
    renderLineups();
    paintLineupPending();
    if (state.mode === "lineups") renderStats();
  }

  function paintLineupHalls() {
    const root = document.getElementById("lineup-halls");
    clear(root);
    DEMO.lineup_halls.forEach((hall) => {
      root.append(chip(hall.hall_name, hall.hall_name === state.lineupHall, () => {
        state.lineupHall = hall.hall_name;
        paintLineupHalls();
        paintLineupPending();
        renderLineups();
        if (state.mode === "lineups") renderStats();
      }));
    });
  }

  function paintLineupPending() {
    const pending = state.lineupShown !== state.lineupHall;
    lineupPendingEl.classList.toggle("hidden", !pending);
    lineupReactEl.classList.toggle("is-pending", pending);
    if (pending) {
      lineupPendingEl.textContent = `${state.lineupHall}を選択中です。並び反応を押すと、この店で良さそうな区間が出ます。`;
    }
  }

  function renderLineups() {
    const root = document.getElementById("lineup-list");
    clear(root);
    if (state.lineupShown !== state.lineupHall) return;
    const hall = lineupHallByName(state.lineupShown);
    const intro = el("section", "hall-block");
    const spots = hall.islands.map((island) => `${island.series} ${island.target_from}〜${island.target_to}`).join("、");
    intro.append(
      el("h2", "", `${hall.hall_name}の並び反応`),
      el("p", "hint", `良さそうなのは ${spots}。色の付いた行が対象で、前後の色なしは通常台です。`),
    );
    root.append(intro);
    hall.islands.forEach((island) => {
      const panel = el("section", "panel lineup-event");
      const head = el("div", "panel-head");
      const titleWrap = el("div");
      titleWrap.append(
        el("p", "series", `${hall.area}　${island.series}`),
        el("h2", "", island.machine_name),
      );
      const badges = el("div", "badge-row");
      badges.append(badge("並び反応", "promo-hot"));
      badges.append(badge(`${island.target_from}〜${island.target_to}`, "promo-gold"));
      titleWrap.append(badges);
      head.append(titleWrap, el("p", "score-pill", `全${island.floor_units}台`));
      const call = el("p", "line-call", island.finding);
      const wrap = el("div", "unit-table-wrap");
      const table = el("table", "unit-table");
      const thead = el("thead");
      const headRow = el("tr");
      const headers = island.kind === "A"
        ? ["台番号", "通常時G", "BB", "REG", "推定"]
        : ["台番号", "通常時G", "初当り", "推定"];
      headers.forEach((label) => headRow.append(el("th", "", label)));
      thead.append(headRow);
      const tbody = el("tbody");
      island.units.forEach((unit) => {
        const tr = el("tr", `unit-row ${estClass(unit.est)}${unit.target ? " is-target" : ""}`);
        if (unit.target) tr.dataset.target = "1";
        const values = island.kind === "A"
          ? [String(unit.no), `${fmt(unit.games)}G`, unit.bb, unit.reg]
          : [String(unit.no), `${fmt(unit.games)}G`, unit.first_hit];
        values.forEach((value) => tr.append(el("td", "", value)));
        const estCell = el("td");
        estCell.append(badge(unit.target ? `推定${unit.est}　対象` : `推定${unit.est}`, estClass(unit.est)));
        tr.append(estCell);
        tbody.append(tr);
      });
      table.append(thead, tbody);
      wrap.append(table);
      panel.append(head, call, wrap);
      root.append(panel);
    });
  }

  function localBadges(row) {
    const items = [badge(row.status_label, STATUS[row.status].badge)];
    if (row.status === "HIGH_BASE") items.push(badge("📊 全456挙動", "promo-base"));
    if (row.status === "ALL_HIGH") items.push(badge("設定6近似", "badge-ALL_HIGH"));
    return items;
  }

  function showCity(status) {
    state.cityStatus = status;
    renderAll();
    renderStats();
    jumpTo("#all-list .unit-row");
  }

  function renderAll() {
    const floor = DEMO.juggler;
    const chips = document.getElementById("all-halls");
    clear(chips);
    chips.append(chip(floor.hall_name, true, () => showCity("all")));
    const root = document.getElementById("all-list");
    clear(root);
    const shown = state.cityStatus === "all"
      ? floor.units
      : floor.units.filter((unit) => unit.status === state.cityStatus);
    const caption = state.cityStatus === "ALL_HIGH"
      ? "設定6近似の台"
      : state.cityStatus === "HIGH_56"
        ? "設定56近似の台"
        : state.cityStatus === "NORMAL"
          ? "設定1・2付近の台"
          : "当たりとハズレが1台ずつ交互です。推定1、推定5、推定2、推定6の順です。";
    const panel = el("section", "panel lineup-event");
    const head = el("div", "panel-head");
    const titleWrap = el("div");
    titleWrap.append(
      el("p", "series", `${floor.hall_name}　${floor.area}`),
      el("h2", "", floor.machine_name),
    );
    const badges = el("div", "badge-row");
    badges.append(badge("全20台", "promo-gold"));
    titleWrap.append(badges);
    head.append(titleWrap, el("p", "score-pill", "16:30"));
    const tableWrap = el("div", "unit-table-wrap");
    const table = el("table", "unit-table");
    const thead = el("thead");
    const headRow = el("tr");
    ["台番号", "通常時G", "BB", "REG", "初当り", "初当り確率", "推定"].forEach((label) => {
      headRow.append(el("th", "", label));
    });
    thead.append(headRow);
    const tbody = el("tbody");
    shown.forEach((unit) => {
      const tr = el("tr", `unit-row ${estClass(unit.est)}`);
      tr.dataset.status = unit.status;
      const values = [
        String(unit.no),
        `${fmt(unit.games)}G`,
        `${fmt(unit.bb_count)}回`,
        `${fmt(unit.reg_count)}回`,
        `${fmt(unit.hit_count)}回`,
        unit.first_hit,
      ];
      values.forEach((value, index) => {
        const cell = el("td", index === 5 ? "metric-hit" : "", value);
        tr.append(cell);
      });
      const estCell = el("td");
      estCell.append(badge(`推定${unit.est}`, STATUS[unit.status].badge));
      tr.append(estCell);
      tbody.append(tr);
    });
    table.append(thead, tbody);
    tableWrap.append(table);
    panel.append(head, el("p", "line-call", caption), tableWrap);
    root.append(panel);
  }

  function paintGroups() {
    const root = document.getElementById("joint-groups");
    if (!root.childElementCount) {
      DEMO.groups.forEach((group) => {
        const button = el("button", "filter", group.name);
        button.type = "button";
        button.dataset.group = group.id;
        button.setAttribute("role", "tab");
        button.addEventListener("click", () => {
          state.group = group.id;
          paintGroups();
          paintPending();
        });
        root.append(button);
      });
    }
    root.querySelectorAll("button").forEach((button) => {
      const on = button.dataset.group === state.group;
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function paintPending() {
    const pending = state.group !== state.shown;
    pendingEl.classList.toggle("hidden", !pending);
    pickEl.classList.toggle("is-pending", pending);
    if (pending) {
      pendingEl.textContent = `${groupById(state.group).name}を選択中です。ボタンを押すと、共通の合同機種を洗い出します。`;
    }
  }

  function compareTable(item) {
    const wrap = el("div", "table-wrap");
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    const columns = item.kind === "A"
      ? ["店舗", "台数", "通常時G", "平均G", "BB", "REG", "挙動"]
      : ["店舗", "台数", "通常時G", "平均G", "RB回数", "初当り", "挙動"];
    columns.forEach((label) => headRow.append(el("th", "", label)));
    head.append(headRow);
    const body = document.createElement("tbody");
    item.rows.forEach((row) => {
      const tr = el("tr", `row-${row.status}`);
      const hallCell = document.createElement("td");
      hallCell.append(document.createTextNode(row.hall_name), el("div", "area-note", row.area));
      const values = [
        hallCell,
        el("td", "", `${fmt(row.units)}台`),
        el("td", "", `${fmt(row.normal_games)}G`),
        el("td", "", `${fmt(row.games)}G`),
      ];
      if (item.kind === "A") {
        values.push(el("td", "", row.bb), el("td", "", row.reg));
      } else {
        values.push(el("td", "", `${fmt(row.rb_count)}回`), el("td", "", row.first_hit));
      }
      values.forEach((cell) => tr.append(cell));
      const judge = document.createElement("td");
      judge.append(badge(row.status_label, STATUS[row.status].badge));
      tr.append(judge);
      body.append(tr);
    });
    table.append(head, body);
    wrap.append(table);
    return wrap;
  }

  function renderJointResult(flash) {
    const group = groupById(state.shown);
    const ranked = group.ranked;
    const top = ranked[0];
    clear(resultEl);
    const panelClass = top.rows.some((row) => row.status === "HIGH_56" || row.status === "ALL_HIGH")
      ? "panel panel-high-56 joint-winner"
      : "panel panel-high-base joint-winner";
    const panel = el("section", panelClass);
    const head = el("div", "panel-head");
    const titleWrap = el("div");
    titleWrap.append(el("p", "series", `${group.name}　${group.lead}`));
    const heading = el("h2", "", top.machine_name);
    heading.id = "joint-winner-name";
    titleWrap.append(heading);
    const tags = el("div", "badge-row");
    top.badges.forEach((item) => tags.append(badge(item.text, item.class_name)));
    titleWrap.append(tags);
    head.append(titleWrap);
    const summary = el("p", "hint", group.summary);
    summary.id = "joint-summary";
    const inline = el("section", "stats stats-inline");
    inline.append(
      stat("平均回転", `${fmt(top.avg_games)}G`),
      stat("導入店舗", `${top.rows.length}店`),
      stat("全56", `${top.rows.filter((row) => row.status === "HIGH_56").length}店`, "#joint-result tr.row-HIGH_56"),
      stat("全456", `${top.rows.filter((row) => row.status === "HIGH_BASE").length}店`, "#joint-result tr.row-HIGH_BASE"),
    );
    panel.append(head, summary, inline, compareTable(top));
    resultEl.append(panel);

    const rest = ranked.slice(1);
    if (rest.length) {
      const others = el("section", "panel");
      const othersHead = el("div", "panel-head");
      othersHead.append(el("h2", "", "ほかの共通機種"), el("p", "hint", "2店舗以上に入り、どこか1店でも全456以上の機種です。"));
      const cards = el("div", "cards");
      rest.forEach((item) => {
        const card = el("article", "card card-HIGH_BASE");
        card.append(el("p", "series", `${item.series}　${item.rows.length}店舗`));
        card.append(el("h3", "", item.machine_name));
        const cardTags = el("div", "badge-row");
        item.badges.forEach((badgeItem) => cardTags.append(badge(badgeItem.text, badgeItem.class_name)));
        card.append(cardTags, metricList([
          ["平均G", `${fmt(item.avg_games)}G`],
          ["導入", item.rows.map((row) => row.short_name).join(" / ")],
        ]));
        cards.append(card);
      });
      others.append(othersHead, cards);
      resultEl.append(others);
    }
    if (flash) {
      resultEl.classList.remove("is-flash");
      void resultEl.offsetWidth;
      resultEl.classList.add("is-flash");
    }
    if (state.mode === "joint") renderStats();
  }

  function init() {
    readUrl();
    document.querySelectorAll(".main-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        showMode(tab.dataset.mode);
        syncUrl();
      });
    });
    pickEl.addEventListener("click", () => {
      state.shown = state.group;
      renderJointResult(true);
      paintPending();
      syncUrl();
    });
    lineupReactEl.addEventListener("click", showLineupReaction);
    renderDigits();
    paintLineupHalls();
    paintLineupPending();
    renderLineups();
    renderAll();
    paintGroups();
    renderJointResult(false);
    paintPending();
    showMode(state.mode);
  }

  init();
})();
