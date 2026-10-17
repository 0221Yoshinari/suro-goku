const highCardsEl = document.getElementById("high-cards");
const midCardsEl = document.getElementById("mid-cards");
const baseCardsEl = document.getElementById("base-cards");
const hallDigitBoardsEl = document.getElementById("hall-digit-boards");
const storeDigitBoardEl = document.getElementById("store-digit-board");
const hallSelectEl = document.getElementById("hall-select");
const digitHallSelectEl = document.getElementById("digit-hall-select");
const crossCardsEl = document.getElementById("cross-cards");
const crossEmptyEl = document.getElementById("cross-empty");
const crossSummaryEl = document.getElementById("cross-summary");
const machineSearchEl = document.getElementById("machine-search");
const hitDigitNavEl = document.getElementById("hit-digit-nav");
const cardsEmptyHighEl = document.getElementById("cards-empty-high");
const cardsEmptyMidEl = document.getElementById("cards-empty-mid");
const cardsEmptyBaseEl = document.getElementById("cards-empty-base");
const cardsEmptyDigitsEl = document.getElementById("cards-empty-digits");
const rowsEl = document.getElementById("rows");
const tableEmptyEl = document.getElementById("table-empty");
const tableNoneEl = document.getElementById("table-none");
const fetchedAtBannerEl = document.getElementById("fetched-at-banner");
const hallFetchedStampEl = document.getElementById("hall-fetched-stamp");
const judgedAtEl = document.getElementById("judged-at");
const freshnessEl = document.getElementById("freshness");
const statCountEl = document.getElementById("stat-count");
const statAllHighEl = document.getElementById("stat-all-high");
const statHigh56El = document.getElementById("stat-high-56");
const statHighBaseEl = document.getElementById("stat-high-base");
const errorEl = document.getElementById("error");
const reloadBtn = document.getElementById("reload-btn");
const judgeBtn = document.getElementById("judge-btn");
const watchBtn = document.getElementById("watch-btn");
const watchClose = document.getElementById("watch-close");
const watchModal = document.getElementById("watch-modal");
const watchScheduleEl = document.getElementById("watch-schedule");
const watchHallSelectEl = document.getElementById("watch-hall-select");
const watchHallMetaEl = document.getElementById("watch-hall-meta");
const watchRowsEl = document.getElementById("watch-rows");
const watchEmptyEl = document.getElementById("watch-empty");
const scheduleStripEl = document.getElementById("schedule-strip");
const searchEl = document.getElementById("search");

let payload = { machines: [], results: [] };
let watchConfig = null;
let watchHall = "";
let filter = "all";
let groupFilter = "tamaya";
let hallFilter = "";
const PREF_STORAGE_KEY = "slgoku-prefecture";
const AREA_STORAGE_KEY = "slgoku-area";
const DEFAULT_PREF = "fukuoka";
const DEFAULT_AREA = "fukuoka_central";
const PREF_LABELS = {
  hokkaido: "北海道",
  aomori: "青森県",
  iwate: "岩手県",
  miyagi: "宮城県",
  akita: "秋田県",
  yamagata: "山形県",
  fukushima: "福島県",
  ibaraki: "茨城県",
  tochigi: "栃木県",
  gunma: "群馬県",
  saitama: "埼玉県",
  chiba: "千葉県",
  tokyo: "東京都",
  kanagawa: "神奈川県",
  niigata: "新潟県",
  toyama: "富山県",
  ishikawa: "石川県",
  fukui: "福井県",
  yamanashi: "山梨県",
  nagano: "長野県",
  gifu: "岐阜県",
  shizuoka: "静岡県",
  aichi: "愛知県",
  mie: "三重県",
  shiga: "滋賀県",
  kyoto: "京都府",
  osaka: "大阪府",
  hyogo: "兵庫県",
  nara: "奈良県",
  wakayama: "和歌山県",
  tottori: "鳥取県",
  shimane: "島根県",
  okayama: "岡山県",
  hiroshima: "広島県",
  yamaguchi: "山口県",
  tokushima: "徳島県",
  kagawa: "香川県",
  ehime: "愛媛県",
  kochi: "高知県",
  fukuoka: "福岡県",
  saga: "佐賀県",
  nagasaki: "長崎県",
  kumamoto: "熊本県",
  oita: "大分県",
  miyazaki: "宮崎県",
  kagoshima: "鹿児島県",
  okinawa: "沖縄県",
};

function prefectureLabel(id, fallback) {
  if (PREF_LABELS[id]) return PREF_LABELS[id];
  if (fallback && fallback !== id) return fallback;
  return "未設定";
}

function readStored(key, fallback) {
  try {
    const saved = localStorage.getItem(key);
    if (saved) {
      return saved;
    }
  } catch (err) {
    /* 保存できない環境では初期値を使う */
  }
  return fallback;
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (err) {
    /* 保存できない環境では今回の表示だけ切り替える */
  }
}

let prefectureFilter = readStored(PREF_STORAGE_KEY, DEFAULT_PREF);
let areaFilter = readStored(AREA_STORAGE_KEY, DEFAULT_AREA);
let digitHall = "all";
let mainView = "stores";
let categoryFilter = "all";
let query = "";
let machineQuery = "";
let crossGroupFilter = "all";
let crossDataOnly = false;
let crossMajorOnly = false;
let crossHallSort = "status";
// 主要系列のみ展開にしたときの対象。初期は全系列を展開する。
const CROSS_MAJOR_GROUP_IDS = new Set(["tamaya", "plaza", "123", "wonderland"]);
const crossOpenOverrides = new Map();
const CROSS_DATA_STATUSES = new Set(["ALL_HIGH", "HIGH_56", "HIGH_BASE", "EARLY_ALERT", "NOT_HIGH"]);
const TO_TOP_THRESHOLD = 200;
let lastJudgedAt = null;
const expanded = new Set();

function showError(message) {
  errorEl.textContent = message;
  errorEl.classList.toggle("hidden", !message);
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return String(value);
}

function formatOdds(value) {
  return value == null ? "-" : `1/${value}`;
}

function normalGamesRows(item) {
  const rows = [["通常時G（合算）", formatNumber(item.real_normal_games ?? item.total_games)]];
  if (!item.includes_art_in_total_games) {
    return rows;
  }
  rows.push([
    "ART/AT消化G",
    item.art_consumed_games == null ? "記録なし" : formatNumber(item.art_consumed_games),
  ]);
  if (item.reported_total_games != null && item.reported_total_games !== (item.real_normal_games ?? item.total_games)) {
    rows.push(["総回転（補正前）", formatNumber(item.reported_total_games)]);
  }
  return rows;
}

function formatAvgSetting(value) {
  if (value == null || value === "") {
    return "-";
  }
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "-";
  }
  return `設定${n}`;
}

function formatAvgSettingValue(value) {
  if (value == null || value === "") {
    return "-";
  }
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "-";
  }
  return n.toFixed(2);
}

function avgSettingTier(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 4) {
    return "calm";
  }
  if (n >= 5.5) {
    return "s6";
  }
  return "high";
}

function settingTierClass(value) {
  return `setting-tier setting-tier-${avgSettingTier(value)}`;
}

function formatCompactCount(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "-";
  }
  return Math.round(n).toLocaleString("ja-JP");
}

function formatCompactOdds(value) {
  if (value == null || value === "") {
    return "-";
  }
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "-";
  }
  const rounded = Math.round(n * 10) / 10;
  const body = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `1/${body}`;
}

function hallDigitAnchor(hall, digit) {
  return `hall-digit-${hall}-${digit}`;
}

function activeGroup() {
  if (groupFilter === "all") {
    return null;
  }
  return (payload.hall_groups || []).find((group) => group.id === groupFilter) || null;
}

function prefectureList() {
  const source = payload.prefectures && payload.prefectures.length
    ? payload.prefectures
    : [{ id: prefectureFilter || DEFAULT_PREF, label: "", default_area: "all" }];
  return source.map((item) => ({
    ...item,
    label: prefectureLabel(item.id, item.label),
  }));
}

function activePrefecture() {
  return prefectureList().find((item) => item.id === prefectureFilter) || prefectureList()[0];
}

function areaPresets() {
  const source = payload.area_presets && payload.area_presets.length
    ? payload.area_presets
    : [
        { id: "fukuoka_central", prefecture: "fukuoka", label: "福岡中央（天神・博多・南区）", areas: ["fukuoka_central"] },
        { id: "fukuoka_west", prefecture: "fukuoka", label: "福岡西・糸島", areas: ["fukuoka_west"] },
        { id: "fukuoka_east", prefecture: "fukuoka", label: "福岡東・糟屋", areas: ["fukuoka_east"] },
        { id: "chikushi", prefecture: "fukuoka", label: "筑紫（春日・大野城・太宰府・筑紫野）", areas: ["chikushi"] },
        { id: "chikugo", prefecture: "fukuoka", label: "筑後・久留米", areas: ["chikugo"] },
        { id: "kitakyushu", prefecture: "fukuoka", label: "北九州", areas: ["kitakyushu"] },
        { id: "all", prefecture: "fukuoka", label: "全域（福岡県内すべて）", areas: [] },
      ];
  const pref = activePrefecture()?.id || DEFAULT_PREF;
  const scoped = source.filter((item) => item.prefecture === pref);
  if (scoped.length) {
    return scoped;
  }
  const label = prefectureLabel(pref, activePrefecture()?.label);
  return [{ id: "all", prefecture: pref, label: `全域（${label}内すべて）`, areas: [] }];
}

function activeArea() {
  return areaPresets().find((item) => item.id === areaFilter) || areaPresets()[0];
}

function hallLocale(hallName) {
  return (payload.hall_locales || {})[hallName || ""] || null;
}

function inArea(hallName) {
  const locale = hallLocale(hallName);
  const pref = activePrefecture()?.id || prefectureFilter;
  if (!locale || locale.prefecture !== pref) {
    return false;
  }
  const preset = activeArea();
  if (!preset || preset.id === "all" || !(preset.areas || []).length) {
    return true;
  }
  return preset.areas.includes(locale.area);
}

function groupHalls(group) {
  return (group?.halls || []).filter((hall) => inArea(hall));
}

function hallsInScope() {
  const group = activeGroup();
  if (!group) {
    return null;
  }
  return new Set(groupHalls(group));
}

function inScope(hallName) {
  if (!inArea(hallName)) {
    return false;
  }
  const names = hallsInScope();
  if (!names) {
    return true;
  }
  return names.has(hallName || "");
}

function hallBoardsForView(selected = hallFilter) {
  const boards = (payload.hall_digit_summaries || []).filter((board) => inArea(board.hall_name));
  if (!selected || selected === "all") {
    return boards;
  }
  return boards.filter((board) => board.hall_name === selected);
}

function hallHotDigits() {
  const set = new Set();
  hallBoardsForView().forEach((board) => {
    (board.hit_digits || []).forEach((row) => set.add(Number(row.digit)));
    (board.digits || []).forEach((row) => {
      if (row.is_hit) {
        set.add(Number(row.digit));
      }
    });
  });
  return set;
}

function scrollToHallDigitBoard() {
  const section = document.getElementById("section-hall-digits");
  if (!section) {
    return;
  }
  const dock = document.querySelector(".nav-dock")?.offsetHeight || 0;
  const top = section.getBoundingClientRect().top + window.scrollY - dock - 8;
  window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

function formatWhen(iso) {
  if (!iso) {
    return "判定結果なし";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return `判定時刻: ${date.toLocaleString("ja-JP")}`;
}

function formatFetchedStamp(iso) {
  if (!iso) {
    return "取得日時不明";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return `${iso} 取得データ`;
  }
  const stamp = date.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${stamp} 取得データ`;
}

function currentFetchedAt() {
  return payload.fetched_at || watchConfig?.fetched_at || null;
}

function formatHm(iso) {
  if (!iso) {
    return "--:--";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "--:--";
  }
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function updateFetchedStamps() {
  const stamp = formatFetchedStamp(currentFetchedAt());
  if (fetchedAtBannerEl) {
    fetchedAtBannerEl.textContent = `最終更新 ${formatHm(currentFetchedAt())}`;
  }
  if (hallFetchedStampEl) {
    const group = activeGroup();
    const scope = group ? group.label : "全系列";
    const hall = hallFilter === "all" ? scope : hallFilter;
    const status = hallFilter && hallFilter !== "all" ? hallDataStatus(hallFilter).label : "";
    hallFetchedStampEl.textContent = status ? `${hall}　${status}　${stamp}` : `${hall}　${stamp}`;
  }
}

function scheduleIntervalMin() {
  const n = Number(watchConfig?.schedule?.interval_min);
  return Number.isFinite(n) && n > 0 ? n : 40;
}

function formatRelative(iso) {
  if (!iso) {
    return { text: "最終更新: なし", tone: "stale", next: "次回目安: 取得後に表示" };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return { text: `最終更新: ${iso}`, tone: "stale", next: "" };
  }
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.max(0, Math.floor(diffMs / 60000));
  let ago;
  if (diffMin < 1) {
    ago = "たった今";
  } else if (diffMin < 60) {
    ago = `${diffMin}分前`;
  } else {
    const hours = Math.floor(diffMin / 60);
    const rest = diffMin % 60;
    ago = rest ? `${hours}時間${rest}分前` : `${hours}時間前`;
  }
  const interval = scheduleIntervalMin();
  const schedule = watchConfig?.schedule || {};
  const scheduledIso = schedule.next_run_at;
  const scheduled = scheduledIso ? new Date(scheduledIso) : null;
  let nextText;
  if (schedule.running) {
    nextText = "次回予定: 取得実行中";
  } else if (scheduled && !Number.isNaN(scheduled.getTime()) && scheduled.getTime() > Date.now() + 60000) {
    nextText = `次回予定: ${scheduled.toLocaleString("ja-JP", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })}`;
  } else if (schedule.in_business_hours && schedule.scheduler_alive === false) {
    nextText = "次回予定: 自動巡回が停止中";
  } else {
    const next = new Date(date.getTime() + interval * 60000);
    if (next.getTime() > Date.now()) {
      nextText = `次回予定: ${next.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" })}`;
    } else {
      nextText = "次回予定: まもなく";
    }
  }
  let tone = "fresh";
  if (diffMin >= interval + 5) {
    tone = "stale";
  } else if (diffMin >= Math.max(20, Math.floor(interval / 2))) {
    tone = "aging";
  }
  return { text: `最終更新: ${ago}`, tone, next: nextText };
}

function updateFreshness() {
  if (!freshnessEl) {
    return;
  }
  const info = formatRelative(payload.judged_at);
  freshnessEl.textContent = `${info.text} ／ ${info.next}`;
  freshnessEl.classList.remove("fresh", "aging", "stale");
  freshnessEl.classList.add(info.tone);
}

function parseDaiNo(value) {
  const n = Number.parseInt(String(value ?? "").replace(/[^\d]/g, ""), 10);
  return Number.isFinite(n) ? n : null;
}

function estimateUnitSetting(unit) {
  if (unit.estimated_setting == null) {
    return { level: null, label: "データ不足", className: "unit-unknown" };
  }
  const level = Number(unit.estimated_setting);
  if (level === 6) {
    return { level: 6, label: unit.estimated_setting_label || "設定6相当", className: "unit-s6" };
  }
  if (level === 5) {
    return { level: 5, label: unit.estimated_setting_label || "設定5相当", className: "unit-s5" };
  }
  if (level === 4) {
    return { level: 4, label: unit.estimated_setting_label || "設定4相当", className: "unit-s4" };
  }
  if (level === 2 || level === 1) {
    return { level, label: unit.estimated_setting_label || `設定${level}相当`, className: "unit-s0" };
  }
  return { level: 0, label: unit.estimated_setting_label || "基準未満", className: "unit-s0" };
}

function annotateUnits(item) {
  const units = [...(item.units || [])]
    .map((unit) => {
      const est = estimateUnitSetting(unit);
      return { ...unit, daiNum: parseDaiNo(unit.dai_no), estimate: est };
    })
    .sort((a, b) => {
      if (a.daiNum == null && b.daiNum == null) {
        return 0;
      }
      if (a.daiNum == null) {
        return 1;
      }
      if (b.daiNum == null) {
        return -1;
      }
      return a.daiNum - b.daiNum;
    });
  return units;
}

function setStatusFilter(next) {
  filter = next || "all";
  document.querySelectorAll("#status-filters .filter").forEach((el) => {
    el.classList.toggle("is-active", (el.dataset.filter || "all") === filter);
  });
}

function formatClock(iso) {
  if (!iso) {
    return "なし";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function metricCard(label, value) {
  const article = document.createElement("article");
  article.className = "watch-metric";
  const lab = document.createElement("p");
  lab.className = "label";
  lab.append(text(label));
  const val = document.createElement("p");
  val.className = "value";
  val.append(text(value));
  article.append(lab, val);
  return article;
}

function renderSchedule() {
  if (!watchConfig) {
    return;
  }
  const s = watchConfig.schedule || {};
  const windowText = `${s.start || "-"}〜${s.end || "-"}（${s.interval_min || 40}分周期 / 最終 ${s.last_slot_hint || "22:00台"}）`;
  let nextText = formatClock(s.next_run_at);
  if (s.running) {
    nextText = "取得実行中";
  } else if (!s.in_business_hours) {
    nextText = `${formatClock(s.next_run_at)}（営業開始）`;
  } else if (s.scheduler_alive === false && s.due_now) {
    nextText = "自動巡回が停止中";
  } else {
    nextText = formatClock(s.next_run_at);
  }
  if (!watchScheduleEl) {
    return;
  }
  watchScheduleEl.replaceChildren();
  watchScheduleEl.append(
    metricCard("自動実行時間", windowText),
    metricCard("当日の最終スロット", `${s.last_slot_hint || "22:00台"}（${s.hard_stop || "23:00"}以降は実行しない）`),
    metricCard("前回の実行完了", formatClock(s.last_completed_at)),
    metricCard("次回の自動実行", nextText),
    metricCard("直近の起動", formatClock(s.last_started_at)),
    metricCard("状態", s.running ? "取得中" : s.in_business_hours ? "営業時間内" : "営業時間外")
  );
  const note = document.createElement("p");
  note.className = "watch-note";
  note.append(text(s.note || ""));
  if (s.last_event?.message) {
    note.append(text(` 直近ログ: ${s.last_event.message}`));
  }
  watchScheduleEl.append(note);
}

function currentWatchHall() {
  const halls = (watchConfig?.halls || []).filter((item) => inScope(item.hall_name));
  return halls.find((item) => item.hall_name === watchHall) || halls[0];
}

function renderWatchHall() {
  const hall = currentWatchHall();
  if (!watchRowsEl) {
    return;
  }
  watchRowsEl.replaceChildren();
  if (!hall) {
    watchEmptyEl?.classList.remove("hidden");
    if (watchHallMetaEl) {
      watchHallMetaEl.textContent = "";
    }
    return;
  }
  watchHall = hall.hall_name;
  if (watchHallMetaEl) {
    watchHallMetaEl.textContent = `${hall.hall_name}: マーク ${hall.marked_count}機種 ／ 直近取得でヒット ${hall.found_count}機種 ／ ${formatFetchedStamp(currentFetchedAt())}`;
  }
  if (watchHallSelectEl && watchHallSelectEl.value !== watchHall) {
    watchHallSelectEl.value = watchHall;
  }
  const machines = hall.machines || [];
  watchEmptyEl?.classList.toggle("hidden", machines.length > 0);
  for (const item of machines) {
    const tr = document.createElement("tr");
    if (!item.found_in_last_fetch) {
      tr.className = "row-miss";
    }
    const cells = [
      item.marked ? "取得対象" : "-",
      item.display_name,
      item.type_label || item.type,
      item.site7_names?.length ? item.site7_names.join(" / ") : "直近データなし",
      item.found_in_last_fetch ? `${item.unit_count}台` : "-",
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 2) {
        const span = document.createElement("span");
        span.className = `badge badge-${item.type}`;
        span.append(text(value));
        td.append(span);
      } else {
        td.append(text(value));
      }
      tr.append(td);
    });
    watchRowsEl.append(tr);
  }
}

function renderWatchTabs() {
  if (!watchHallSelectEl) {
    return;
  }
  const watchHalls = (watchConfig?.halls || []).filter((hall) => inArea(hall.hall_name));
  watchHallSelectEl.replaceChildren();
  for (const hall of watchHalls) {
    const option = document.createElement("option");
    option.value = hall.hall_name;
    option.append(text(`${hall.hall_name}（${hall.found_count}/${hall.marked_count}）`));
    watchHallSelectEl.append(option);
  }
  if (watchHall && watchHalls.some((hall) => hall.hall_name === watchHall)) {
    watchHallSelectEl.value = watchHall;
  }
}

function renderWatch() {
  renderSchedule();
  renderWatchTabs();
  renderWatchHall();
}

function openWatchModal() {
  watchModal?.classList.remove("hidden");
  renderWatch();
}

function closeWatchModal() {
  watchModal?.classList.add("hidden");
}

function redirectIfUnauthorized(response) {
  if (response.status !== 401 && response.status !== 403) {
    return false;
  }
  const next = `${window.location.pathname}${window.location.search}`;
  const notices = {
    "利用権限がありません": "expired",
    "無料体験は終了しています": "trial",
    "利用期限が切れています": "lapsed",
    "別の端末で利用中です": "session",
  };
  response
    .clone()
    .json()
    .then((data) => {
      const notice = notices[data && data.detail] || "";
      const params = new URLSearchParams();
      params.set("next", next);
      if (notice) {
        params.set("notice", notice);
      }
      window.location.assign(`/login?${params.toString()}`);
    })
    .catch(() => {
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    });
  return true;
}

async function loadWatchConfig() {
  const response = await fetch("/api/watch-config");
  if (redirectIfUnauthorized(response)) {
    return;
  }
  if (!response.ok) {
    throw new Error(`監視設定の取得に失敗しました (${response.status})`);
  }
  watchConfig = await response.json();
  if (!watchHall && watchConfig.halls?.length) {
    watchHall = watchConfig.halls[0].hall_name;
  }
  renderSchedule();
  updateFreshness();
  updateFetchedStamps();
  render();
  if (watchModal && !watchModal.classList.contains("hidden")) {
    renderWatch();
  }
}

function jumpTo(id) {
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  const view = el.closest(".view");
  if (view?.classList.contains("hidden")) {
    setMainView(view.id.replace("view-", ""));
  }
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

let signalToastTimer = 0;
let signalFocusTimer = 0;
let signalScrollEnd = null;

function showSignalToast(message) {
  const toast = document.getElementById("signal-toast");
  if (!toast) {
    return;
  }
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(signalToastTimer);
  signalToastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2200);
}

function clearSignalFocus() {
  document.querySelectorAll(".is-signal-focus").forEach((el) => el.classList.remove("is-signal-focus"));
  window.clearTimeout(signalFocusTimer);
  if (signalScrollEnd) {
    window.removeEventListener("scrollend", signalScrollEnd);
    signalScrollEnd = null;
  }
}

function holdSignalFocus() {
  window.clearTimeout(signalFocusTimer);
  if (signalScrollEnd) {
    window.removeEventListener("scrollend", signalScrollEnd);
  }
  signalScrollEnd = () => {
    signalScrollEnd = null;
    window.clearTimeout(signalFocusTimer);
    signalFocusTimer = window.setTimeout(clearSignalFocus, 1600);
  };
  window.addEventListener("scrollend", signalScrollEnd, { once: true });
  signalFocusTimer = window.setTimeout(clearSignalFocus, 5000);
}

const SIGNAL_LABELS = {
  ALL_HIGH: "全6",
  HIGH_56: "56",
  HIGH_BASE: "456",
};

function syncSignalJumps() {
  const items = machineList();
  document.querySelectorAll(".signal-jump").forEach((button) => {
    const status = button.dataset.signal;
    const label = SIGNAL_LABELS[status] || "";
    const count = items.filter((item) => normalizedStatus(item) === status).length;
    const name = button.querySelector(".signal-jump-label");
    const num = button.querySelector(".signal-jump-count");
    if (name) {
      name.textContent = label;
    }
    if (num) {
      num.textContent = `${count}機種`;
    }
    button.setAttribute("aria-label", `${label} ${count}機種。タップして確認`);
    const empty = count === 0;
    button.classList.toggle("is-empty", empty);
    button.setAttribute("aria-disabled", empty ? "true" : "false");
  });
}

function scrollToSignalTarget(hall, chip) {
  const dock = document.querySelector(".nav-dock")?.offsetHeight || 0;
  const padding = dock + 10;
  const hallTop = hall.getBoundingClientRect().top + window.scrollY;
  let top = hallTop - padding;
  const chipTop = chip.getBoundingClientRect().top + window.scrollY;
  if (chipTop > top + window.innerHeight - 48) {
    top = chipTop - padding - 36;
  }
  window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

function jumpToSignal(status) {
  const hasMatch = machineList().some((item) => normalizedStatus(item) === status);
  if (!hasMatch) {
    showSignalToast("現在反応はありません");
    return;
  }
  if (mainView !== "stores") {
    setMainView("stores");
  }
  requestAnimationFrame(() => {
    const chip = document.querySelector(`#today-summary .today-chip[data-signal="${status}"]`);
    const hall = chip?.closest(".today-hall");
    if (!chip || !hall) {
      showSignalToast("現在反応はありません");
      return;
    }
    clearSignalFocus();
    hall.classList.add("is-signal-focus");
    hall.querySelectorAll(`.today-chip[data-signal="${status}"]`).forEach((el) => el.classList.add("is-signal-focus"));
    scrollToSignalTarget(hall, chip);
    holdSignalFocus();
  });
}

function text(value) {
  return document.createTextNode(value == null ? "" : String(value));
}

function isAtType(item) {
  return (item?.judgment_type || lookupJudgmentType(item)) === "AT";
}

function lookupJudgmentType(item) {
  if (!item) {
    return null;
  }
  if (item.judgment_type) {
    return item.judgment_type;
  }
  const match = machineList().find(
    (machine) =>
      machine.machine_name === item.machine_name && (machine.hall_name || "") === (item.hall_name || "")
  );
  return match?.judgment_type || null;
}

function appendMetric(dl, label, value, { emphasize = false } = {}) {
  const dt = document.createElement("dt");
  dt.append(text(label));
  const dd = document.createElement("dd");
  if (emphasize) {
    dd.className = "metric-hit";
  }
  dd.append(text(value));
  dl.append(dt, dd);
}

function groupKey(item) {
  return `${item.hall_name || ""}|${item.machine_name || ""}`;
}

function machineList() {
  const items = payload.machines && payload.machines.length ? payload.machines : [];
  return items.filter((item) => item.status !== "EXCLUDED" && !item.judgment_excluded && inArea(item.hall_name));
}

function searched(items) {
  const q = query.trim();
  if (!q) {
    return items;
  }
  return items.filter((item) => {
    const hay = `${item.machine_name || ""} ${item.series || ""} ${item.hall_name || ""}`;
    return hay.includes(q);
  });
}

function countKindLabel(item) {
  return isAtType(item) ? "初当り" : "REG";
}

function settingBars(item) {
  const settings = item.criteria?.settings || {};
  const at = isAtType(item);
  const rbName = countKindLabel(item);
  const fmt = (level) => {
    const bar = settings[String(level)];
    const primary = bar?.first_hit || bar?.synthetic;
    if (!primary) {
      return "-";
    }
    if (at || !bar.rb) {
      return `1/${primary}`;
    }
    return `1/${bar.synthetic} (${rbName} 1/${bar.rb})`;
  };
  return {
    six: fmt(6),
    five: fmt(5),
    four: fmt(4),
    compact: [6, 5, 4]
      .map((level) => {
        const bar = settings[String(level)];
        if (!bar?.synthetic) {
          return `${level}:-`;
        }
        return `${level}:1/${bar.synthetic}`;
      })
      .join(" / "),
  };
}

function makeUnitTable(item, { inspect = false } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "unit-table-wrap";
  const units = annotateUnits(item);
  const at = isAtType(item);
  const table = document.createElement("table");
  table.className = "unit-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headers = inspect
    ? ["台番号", "通常時G", "BB", "RB", "初当り", "初当り確率", "推定設定"]
    : at
      ? ["台番号", "通常時G", "初当り", "初当り確率", "推定設定"]
      : ["台番号", "通常時G", "BB / RB", "ART", "合成", "BIG", "REG", "推定設定"];
  headers.forEach((label) => {
    const th = document.createElement("th");
    th.append(text(label));
    headRow.append(th);
  });
  thead.append(headRow);
  const tbody = document.createElement("tbody");
  for (const unit of units) {
    const tr = document.createElement("tr");
    tr.className = ["unit-row", unit.estimate.className].join(" ");
    const badge = document.createElement("span");
    badge.className = `badge ${unit.estimate.className}`;
    badge.append(text(unit.estimate.label));
    const values = inspect
      ? [
          formatNumber(unit.dai_no),
          formatNumber(unit.real_normal_games ?? unit.total_games),
          formatNumber(unit.bb_count),
          formatNumber(unit.rb_count),
          formatNumber(unit.first_hit_count),
          formatOdds(unit.synthetic_probability),
        ]
      : at
      ? [
          formatNumber(unit.dai_no),
          formatNumber(unit.total_games),
          formatNumber(unit.first_hit_count),
          formatOdds(unit.synthetic_probability),
        ]
      : [
          formatNumber(unit.dai_no),
          formatNumber(unit.total_games),
          `${formatNumber(unit.bb_count)} / ${formatNumber(unit.rb_count)}`,
          formatNumber(unit.art_count),
          formatOdds(unit.synthetic_probability),
          formatOdds(unit.bb_probability),
          formatOdds(unit.rb_probability),
        ];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if ((inspect && index === 5) || (at && !inspect && index === 3)) {
        td.className = "metric-hit";
      }
      td.append(text(value));
      tr.append(td);
    });
    const tdBadge = document.createElement("td");
    tdBadge.append(badge);
    tr.append(tdBadge);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

function digitBadgeClass(row) {
  if (row.is_all_high || row.status === "ALL_HIGH") {
    return "badge-ALL_HIGH";
  }
  if (row.is_high_56 || row.status === "HIGH_56") {
    return "badge-HIGH_56";
  }
  if (row.is_high_base || row.status === "HIGH_BASE") {
    return "badge-HIGH_BASE";
  }
  if (row.status === "NORMAL") {
    return "badge-NORMAL";
  }
  return `badge-${row.status || "NOT_HIGH"}`;
}

function makeDigitTable(item) {
  const rows = item.digit_judgments || [];
  if (!rows.length) {
    return null;
  }
  const at = isAtType(item);
  const wrap = document.createElement("div");
  wrap.className = "digit-table-wrap";
  const table = document.createElement("table");
  table.className = at ? "digit-table digit-table-at" : "digit-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headers = at
    ? ["末尾", "台数", "通常時G", "初当り確率", "判定"]
    : ["末尾", "台数", "通常時G", "合成", "BIG", "REG", "判定"];
  headers.forEach((label) => {
    const th = document.createElement("th");
    th.append(text(label));
    headRow.append(th);
  });
  thead.append(headRow);
  const tbody = document.createElement("tbody");
  const hot = hallHotDigits();
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const classes = [];
    if (row.is_all_high) {
      classes.push("row-ALL_HIGH");
    } else if (row.is_high_56) {
      classes.push("row-HIGH_56");
    } else if (row.is_high_base) {
      classes.push("row-HIGH_BASE");
    }
    if (hot.has(Number(row.digit)) && row.unit_count) {
      classes.push("digit-hall-hot");
    }
    tr.className = classes.join(" ");
    const badge = document.createElement("span");
    badge.className = `badge ${digitBadgeClass(row)}`;
    badge.append(text(visibleStatusLabel(row.status_label)));
    const values = at
      ? [String(row.digit), formatNumber(row.unit_count), formatNumber(row.total_games), formatOdds(row.synthetic_probability)]
      : [
          String(row.digit),
          formatNumber(row.unit_count),
          formatNumber(row.total_games),
          formatOdds(row.synthetic_probability),
          formatOdds(row.bb_probability),
          formatOdds(row.rb_probability),
        ];
    values.forEach((value) => {
      const td = document.createElement("td");
      td.append(text(value));
      tr.append(td);
    });
    const tdBadge = document.createElement("td");
    tdBadge.append(badge);
    tr.append(tdBadge);
    tbody.append(tr);
  });
  table.append(thead, tbody);
  const details = document.createElement("details");
  details.className = "digit-aux";
  const summary = document.createElement("summary");
  summary.append(text("機種内末尾（参考・全館判定は上部）"));
  details.append(summary, table);
  wrap.append(details);
  return wrap;
}

function makeCard(item, { inspect = false } = {}) {
  const card = document.createElement("article");
  card.className = `card card-${item.status || "UNMATCHED"}`;
  const key = groupKey(item);
  const title = document.createElement("h3");
  title.append(text(item.machine_name || "(機種不明)"));
  const series = document.createElement("p");
  series.className = "series";
  const unitLabel = `${item.unit_count || 0}台`;
  series.append(text(`${item.series || item.criteria?.series || ""}　${unitLabel}`));
  const badge = document.createElement("span");
  badge.className = `badge badge-${normalizedStatus(item) || "UNMATCHED"}`;
  badge.append(text(cardBadgeLabel(item)));
  const dl = document.createElement("dl");
  const typeLabel = item.judgment_type === "A" ? "Aタイプ" : item.judgment_type === "AT" ? "AT/ART" : "-";
  const rows = inspect
    ? [
        ["店舗", item.hall_name || "-"],
        ["通常時G", formatNumber(item.real_normal_games ?? item.total_games)],
        ["BB", formatNumber(item.bb_count)],
        ["RB", formatNumber(item.rb_count)],
        ["初当り確率", formatOdds(item.synthetic_probability), true],
      ]
    : isAtType(item)
    ? [
        ["店舗", item.hall_name || "-"],
        ["台数", formatNumber(item.unit_count)],
        ["通常時G", formatNumber(item.real_normal_games ?? item.total_games)],
        ["平均G", formatNumber(item.avg_games)],
        ["RB回数", formatNumber(item.rb_count)],
        ["初当り確率", formatOdds(item.synthetic_probability), true],
      ]
    : [
        ["店舗", item.hall_name || "-"],
        ["タイプ", typeLabel],
        ["台数", formatNumber(item.unit_count)],
        ...normalGamesRows(item),
        ["平均G", formatNumber(item.avg_games)],
        ["BB / RB（合算）", `${formatNumber(item.bb_count)} / ${formatNumber(item.rb_count)}`],
        ["合成確率", formatOdds(item.synthetic_probability)],
        ["BIG確率", formatOdds(item.bb_probability)],
        ["REG確率", formatOdds(item.rb_probability)],
        ["ART（合算）", formatNumber(item.art_count)],
        ["規定G（平均）", formatNumber(item.criteria?.min_games)],
      ];
  for (const [label, value, emphasize] of rows) {
    appendMetric(dl, label, value, { emphasize: Boolean(emphasize) });
  }

  const details = document.createElement("details");
  details.className = "unit-details";
  if (expanded.has(key)) {
    details.open = true;
  }
  details.addEventListener("toggle", () => {
    if (details.open) {
      expanded.add(key);
    } else {
      expanded.delete(key);
    }
  });
  const summary = document.createElement("summary");
  summary.append(text(`台別内訳を表示（${item.unit_count || 0}台）`));
  details.append(summary, makeUnitTable(item, { inspect }));
  const digits = makeDigitTable(item);
  if (digits) {
    card.append(title, series, badge, dl, digits, details);
  } else {
    card.append(title, series, badge, dl, details);
  }
  return card;
}

function visibleStatusLabel(label) {
  if (label === "朝一チャンス") {
    return "全456数値";
  }
  if (label === "基準未達" || label === "規定数不足") {
    return "通常";
  }
  return label || "-";
}

function normalizedStatus(item) {
  if (item?.status === "EXCLUDED" || item?.judgment_excluded) {
    return item?.status || "EXCLUDED";
  }
  if (item?.checks?.early_enough === false) {
    return item?.status === "INCOMPLETE" ? "INCOMPLETE" : "INSUFFICIENT_GAMES";
  }
  if (item?.checks?.meets_setting_6) {
    return "ALL_HIGH";
  }
  if (item?.checks?.meets_setting_5) {
    return "HIGH_56";
  }
  if (item?.status === "EARLY_ALERT") {
    return "HIGH_BASE";
  }
  return item?.status;
}

function cardBadgeLabel(item) {
  const status = normalizedStatus(item);
  if (status === "ALL_HIGH") {
    return "全6数値";
  }
  if (status === "HIGH_56") {
    return "全56数値";
  }
  if (status === "HIGH_BASE") {
    return "全456数値";
  }
  if (status === "NOT_HIGH" || status === "INSUFFICIENT_GAMES") {
    return "その他（通常）";
  }
  const raw = item?.status_label || "-";
  if (raw === "朝一チャンス" || raw === "基準未達" || raw === "規定数不足") {
    return "その他（通常）";
  }
  return raw;
}

let otherOpen = false;

function isFeaturedStatus(item) {
  const status = normalizedStatus(item);
  return status === "ALL_HIGH" || status === "HIGH_56" || status === "HIGH_BASE";
}

function otherToggleLabel(count, open) {
  return `${open ? "▲" : "▼"} その他全機種データ・検品表示（全${count}機種）`;
}

function renderOtherMachines(items) {
  const toggle = document.getElementById("other-machines-toggle");
  const panel = document.getElementById("other-panel");
  const cards = document.getElementById("other-cards");
  const empty = document.getElementById("cards-empty-other");
  if (!toggle || !panel || !cards) {
    return;
  }
  const others = items.filter((item) => !isFeaturedStatus(item));
  toggle.textContent = otherToggleLabel(others.length, otherOpen);
  toggle.setAttribute("aria-expanded", otherOpen ? "true" : "false");
  cards.replaceChildren();
  if (!otherOpen) {
    panel.classList.remove("is-open");
    empty?.classList.add("hidden");
    return;
  }
  empty?.classList.toggle("hidden", others.length > 0);
  others.forEach((item) => cards.append(makeCard(item, { inspect: true })));
  panel.classList.add("is-open");
}

function renderCards(items) {
  highCardsEl.replaceChildren();
  midCardsEl?.replaceChildren();
  baseCardsEl.replaceChildren();
  const highs = items.filter((item) => normalizedStatus(item) === "ALL_HIGH");
  const mids = items.filter((item) => normalizedStatus(item) === "HIGH_56");
  const bases = items.filter((item) => normalizedStatus(item) === "HIGH_BASE");
  cardsEmptyHighEl.classList.toggle("hidden", highs.length > 0);
  cardsEmptyMidEl?.classList.toggle("hidden", mids.length > 0);
  cardsEmptyBaseEl.classList.toggle("hidden", bases.length > 0);
  highs.forEach((item) => highCardsEl.append(makeCard(item)));
  mids.forEach((item) => midCardsEl?.append(makeCard(item)));
  bases.forEach((item) => baseCardsEl.append(makeCard(item)));
  renderOtherMachines(items);
}

function renderHallDigitBoards(root = hallDigitBoardsEl, boards = hallBoardsForView()) {
  if (!root) {
    return;
  }
  root.replaceChildren();
  if (!boards.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.append(text("全館末尾データがありません。再判定してください。"));
    root.append(empty);
    return;
  }
  boards.forEach((board) => {
    const wrap = document.createElement("div");
    wrap.className = "hall-digit-board";
    const title = document.createElement("h3");
    title.className = "hall-digit-title";
    title.append(text(`${board.hall_name}　全${board.unit_count || 0}台`));
    const grid = document.createElement("div");
    grid.className = "hall-digit-grid";
    (board.digits || []).forEach((row) => {
      const card = document.createElement("article");
      card.id = hallDigitAnchor(board.hall_name, row.digit);
      card.className = `hall-digit-card card-${row.status || "NORMAL"}`;
      const head = document.createElement("div");
      head.className = "hall-digit-head";
      const num = document.createElement("p");
      num.className = "hall-digit-num";
      const numLabel = document.createElement("span");
      numLabel.append(text("末尾"));
      num.append(numLabel, text(String(row.digit)));
      const badge = document.createElement("span");
      badge.className = `badge ${digitBadgeClass(row)}`;
      badge.append(text(visibleStatusLabel(row.status_label || "通常")));
      head.append(num, badge);
      const setting = document.createElement("p");
      setting.className = "hall-digit-setting";
      const settingLabel = document.createElement("span");
      settingLabel.append(text("平均推測設定"));
      const settingValue = document.createElement("strong");
      settingValue.className = settingTierClass(row.avg_setting);
      settingValue.append(text(formatAvgSettingValue(row.avg_setting)));
      setting.append(settingLabel, settingValue);
      const dl = document.createElement("dl");
      [
        ["台数", formatCompactCount(row.unit_count)],
        ["通常時G", formatCompactCount(row.real_normal_games ?? row.total_games)],
        ["平均G", formatCompactCount(row.avg_games)],
      ].forEach(([label, value]) => appendMetric(dl, label, value));
      card.append(head, setting, dl);
      grid.append(card);
    });
    const table = document.createElement("table");
    table.className = "hall-digit-table";
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["末尾", "平均推測設定", "台数", "通常時G", "平均G", "判定"].forEach((label) => {
      const th = document.createElement("th");
      th.append(text(label));
      headRow.append(th);
    });
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    (board.digits || []).forEach((row) => {
      const tr = document.createElement("tr");
      tr.className = `row-${row.status || ""}`;
      [
        { value: String(row.digit) },
        { value: formatAvgSetting(row.avg_setting), tier: true },
        { value: formatNumber(row.unit_count) },
        { value: formatNumber(row.total_games) },
        { value: formatNumber(row.avg_games) },
        { value: visibleStatusLabel(row.status_label || "通常"), hit: true },
      ].forEach((cell) => {
        const td = document.createElement("td");
        if (cell.tier) {
          td.className = settingTierClass(row.avg_setting);
        } else if (cell.hit) {
          td.className = "metric-hit";
        }
        td.append(text(cell.value));
        tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(thead, tbody);
    const details = document.createElement("details");
    details.className = "digit-aux";
    const summary = document.createElement("summary");
    summary.append(text("表で見る"));
    details.append(summary, table);
    wrap.append(title, grid, details);
    root.append(wrap);
  });
}

const LIST_BUCKETS = [
  { id: "all6", label: "全6挙動", statuses: ["ALL_HIGH"] },
  { id: "high56", label: "全56挙動", statuses: ["HIGH_56"] },
  { id: "base", label: "全456挙動", statuses: ["HIGH_BASE"] },
  { id: "other", label: "その他（通常）", statuses: ["NOT_HIGH"] },
];

function listBucket(item) {
  const status = normalizedStatus(item);
  return LIST_BUCKETS.find((bucket) => bucket.statuses.includes(status)) || null;
}

function visibleRows(items) {
  const listed = items.filter((item) => listBucket(item));
  const filtered = filter === "all" ? listed : listed.filter((item) => listBucket(item)?.id === filter);
  const rank = { all6: 0, high56: 1, base: 2, other: 3 };
  return [...filtered].sort((a, b) => (rank[listBucket(a)?.id] ?? 9) - (rank[listBucket(b)?.id] ?? 9));
}

function renderTable(items) {
  rowsEl.replaceChildren();
  const rows = visibleRows(items);
  tableNoneEl.classList.toggle("hidden", items.length > 0 || query !== "");
  tableEmptyEl.classList.toggle("hidden", rows.length > 0);
  if (items.length === 0 && query === "") {
    tableEmptyEl.classList.add("hidden");
  }
  let previousBucket = "";
  for (const item of rows) {
    const bucket = listBucket(item);
    if (bucket && bucket.id !== previousBucket) {
      previousBucket = bucket.id;
      const groupRow = document.createElement("tr");
      groupRow.className = "list-group";
      const cell = document.createElement("td");
      cell.colSpan = 14;
      const count = rows.filter((row) => listBucket(row)?.id === bucket.id).length;
      cell.append(text(`${bucket.label}（${count}機種）`));
      groupRow.append(cell);
      rowsEl.append(groupRow);
    }
    const tr = document.createElement("tr");
    tr.className = `row-${normalizedStatus(item) || ""}`;
    const bars = settingBars(item);
    const values = [
      item.hall_name || "-",
      item.machine_name || "(機種不明)",
      item.series || item.criteria?.series || "-",
      item.judgment_type === "A" ? "A" : item.judgment_type === "AT" ? "AT" : "-",
      bucket?.label || item.status_label || "-",
      formatNumber(item.unit_count),
      formatNumber(item.total_games),
      formatNumber(item.avg_games),
      isAtType(item) ? "-" : `${formatNumber(item.bb_count)} / ${formatNumber(item.rb_count)}`,
      isAtType(item) ? "-" : formatOdds(item.synthetic_probability),
      isAtType(item) ? "-" : formatOdds(item.bb_probability),
      formatOdds(item.rb_probability),
      formatNumber(item.criteria?.min_games),
      bars.compact,
    ];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 4) {
        const badge = document.createElement("span");
        badge.className = `badge badge-${normalizedStatus(item) || "UNMATCHED"}`;
        badge.append(text(value));
        td.append(badge);
      } else {
        td.append(text(value));
      }
      tr.append(td);
    });
    rowsEl.append(tr);
  }
}

function hallItems(items) {
  const scoped = items.filter((item) => inScope(item.hall_name));
  if (hallFilter === "all") {
    return scoped;
  }
  return scoped.filter((item) => (item.hall_name || "") === hallFilter);
}

function counts(results) {
  const status = {};
  let units = 0;
  let judged = 0;
  for (const item of results) {
    const statusName = normalizedStatus(item);
    status[statusName] = (status[statusName] || 0) + 1;
    units += item.unit_count || 0;
    if (item.status !== "EXCLUDED") {
      judged += 1;
    }
  }
  return {
    machines: judged,
    units,
    high: status.ALL_HIGH || 0,
    high56: status.HIGH_56 || 0,
    highBase: status.HIGH_BASE || 0,
    early: status.EARLY_ALERT || 0,
    notHigh: status.NOT_HIGH || 0,
    short: status.INSUFFICIENT_GAMES || 0,
  };
}

function allHallNames() {
  const names = [];
  for (const group of payload.hall_groups || []) {
    for (const hall of group.halls || []) {
      if (hall && !names.includes(hall)) {
        names.push(hall);
      }
    }
  }
  for (const hall of payload.configured_halls || []) {
    if (hall && !names.includes(hall)) {
      names.push(hall);
    }
  }
  return names;
}

function hallTabNames() {
  const names = allHallNames();
  const extra = [
    ...(payload.halls || []),
    ...machineList().map((item) => item.hall_name),
    ...((watchConfig?.halls || []).map((item) => item.hall_name)),
  ];
  extra.forEach((name) => {
    if (name && !names.includes(name)) {
      names.push(name);
    }
  });
  return names.filter((name) => inArea(name));
}

function syncGroupToHall(hall) {
  if (!hall || hall === "all") {
    return;
  }
  const owner = (payload.hall_groups || []).find((group) => (group.halls || []).includes(hall));
  if (owner) {
    groupFilter = owner.id;
  }
}

function visibleGroups() {
  return (payload.hall_groups || []).filter((group) => groupHalls(group).length > 0);
}

function ensureSelection() {
  const payloadReady = Boolean(payload.prefectures && payload.prefectures.length);
  const prefs = prefectureList();
  if (payloadReady && !prefs.some((item) => item.id === prefectureFilter)) {
    prefectureFilter = prefs[0]?.id || DEFAULT_PREF;
  }
  const presets = areaPresets();
  if (payloadReady && !presets.some((item) => item.id === areaFilter)) {
    const preferred = activePrefecture()?.default_area || DEFAULT_AREA;
    areaFilter = presets.some((item) => item.id === preferred) ? preferred : (presets[0]?.id || "all");
  }
  if (payloadReady) {
    writeStored(PREF_STORAGE_KEY, prefectureFilter);
    writeStored(AREA_STORAGE_KEY, areaFilter);
  }
  const groups = visibleGroups();
  if (!groups.some((group) => group.id === groupFilter)) {
    const ranked = [...groups].sort((a, b) => groupHalls(b).length - groupHalls(a).length);
    groupFilter = ranked[0]?.id || "all";
  }
  const names = hallTabNames();
  if (hallFilter && hallFilter !== "all" && !names.includes(hallFilter)) {
    hallFilter = groupHalls(activeGroup())[0] || names[0] || "";
  }
  if (!hallFilter) {
    hallFilter = groupHalls(activeGroup())[0] || names[0] || "";
  }
  if (digitHall && digitHall !== "all" && !inArea(digitHall)) {
    digitHall = "all";
  }
}

function fillLocaleSelect(select, options, value, onChange) {
  if (!select) {
    return;
  }
  select.replaceChildren();
  for (const option of options) {
    const el = document.createElement("option");
    el.value = option.id;
    el.append(text(option.menuLabel || option.label));
    select.append(el);
  }
  if ([...select.options].some((option) => option.value === value)) {
    select.value = value;
  }
  select.onchange = () => onChange(select.value);
}

function prefectureMenuLabel(item) {
  const base = prefectureLabel(item.id, item.label);
  const count = Number(item.hall_count);
  if (!Number.isFinite(count)) {
    return base;
  }
  return `${base} ${count}店舗対応`;
}

function areaMenuLabel(item) {
  const count = Number(item.hall_count);
  if (!Number.isFinite(count)) {
    return item.label;
  }
  return `${item.label}（${count}店舗）`;
}

function hallDataStatus(name) {
  const row = (payload.hall_status || {})[name || ""];
  if (row && row.label) {
    return { state: row.state || "queued", label: row.label };
  }
  return { state: "queued", label: "順次データ反映中" };
}

function renderAreaFilters() {
  const prefs = prefectureList().map((item) => ({ ...item, menuLabel: prefectureMenuLabel(item) }));
  const areas = areaPresets().map((item) => ({ ...item, menuLabel: areaMenuLabel(item) }));
  fillLocaleSelect(document.getElementById("prefecture-select"), prefs, prefectureFilter, (id) => {
    if (prefectureFilter === id) {
      return;
    }
    prefectureFilter = id;
    const presets = areaPresets();
    if (!presets.some((item) => item.id === areaFilter)) {
      const preferred = activePrefecture()?.default_area || "all";
      areaFilter = presets.some((item) => item.id === preferred) ? preferred : (presets[0]?.id || "all");
    }
    writeStored(PREF_STORAGE_KEY, prefectureFilter);
    writeStored(AREA_STORAGE_KEY, areaFilter);
    hallFilter = "";
    digitHall = "all";
    loadJudgments(true).catch((err) => showError(err.message));
  });
  fillLocaleSelect(document.getElementById("area-select"), areas, areaFilter, (id) => {
    if (areaFilter === id) {
      return;
    }
    areaFilter = id;
    writeStored(AREA_STORAGE_KEY, areaFilter);
    hallFilter = "";
    digitHall = "all";
    render();
  });
}

function renderGroupFilters() {
  const groups = visibleGroups();
  const options = groups.map((group) => {
    const names = new Set(groupHalls(group));
    const high = machineList().filter((item) => names.has(item.hall_name) && normalizedStatus(item) === "ALL_HIGH").length;
    return { id: group.id, label: `${group.label}（${names.size}店・全6:${high}）` };
  });
  for (const el of [document.getElementById("group-filters"), document.getElementById("digit-group-filters")]) {
    if (!el) {
      continue;
    }
    el.replaceChildren();
    for (const option of options) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "filter";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", groupFilter === option.id ? "true" : "false");
      if (groupFilter === option.id) {
        btn.classList.add("is-active");
      }
      btn.append(text(option.label));
      btn.addEventListener("click", () => {
        groupFilter = option.id;
        hallFilter = "";
        ensureSelection();
        render();
      });
      el.append(btn);
    }
  }
}

function fillHallSelect(select, { includeAll, value, allLabel }) {
  if (!select) {
    return;
  }
  const names = hallTabNames();
  select.replaceChildren();
  if (includeAll) {
    const all = document.createElement("option");
    all.value = "all";
    all.append(text(allLabel || "すべてのホール"));
    select.append(all);
  }
  for (const hall of names) {
    const option = document.createElement("option");
    option.value = hall;
    option.append(text(hall));
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === value)) {
    select.value = value;
  }
}

function renderHallRoster(names) {
  const root = document.getElementById("hall-roster");
  const meta = document.getElementById("roster-meta");
  const title = document.getElementById("roster-title");
  if (!root) {
    return;
  }
  const area = activeArea();
  const pref = activePrefecture();
  if (title) {
    const place = area && area.id !== "all" ? area.label : (pref ? prefectureLabel(pref.id, pref.label) : "このエリア");
    title.textContent = `${place}の対応ホール`;
  }
  const order = { updated: 0, waiting: 1, queued: 2, preparing: 3 };
  const rows = names
    .map((name) => ({ name, ...hallDataStatus(name) }))
    .sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || a.name.localeCompare(b.name, "ja"));
  if (meta) {
    const updated = rows.filter((row) => row.state === "updated").length;
    meta.textContent = `${rows.length}店舗対応。データ更新済み ${updated}店舗。取得元が違っても、同じ画面で全台系・末尾・台データを見られます。`;
  }
  root.replaceChildren();
  rows.forEach((row) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = hallFilter === row.name ? "hall-roster-card is-current" : "hall-roster-card";
    const name = document.createElement("span");
    name.className = "hall-roster-name";
    name.append(text(row.name));
    const badge = document.createElement("span");
    badge.className = `data-badge data-${row.state}`;
    badge.append(text(row.label));
    button.append(name, badge);
    const events = eventsForHall(row.name);
    if (events.length) {
      button.append(renderEventBadges(events));
    }
    button.addEventListener("click", () => focusHall(row.name));
    root.append(button);
  });
}

function renderHallFilters() {
  renderGroupFilters();
  const names = hallTabNames();
  const label = document.getElementById("hall-count-label");
  if (label) {
    const total = Number(payload.enabled_hall_count);
    const nationwide = Number.isFinite(total) ? `全国${total}店舗対応` : "全国の対応店舗";
    label.textContent = `${nationwide} / このエリア${names.length}店舗`;
  }
  renderHallRoster(names);
  fillHallSelect(hallSelectEl, { includeAll: false, value: hallFilter });
  fillHallSelect(digitHallSelectEl, { includeAll: true, value: digitHall, allLabel: "ホールを選んでください" });
}

const STATUS_RANK = { ALL_HIGH: 0, HIGH_56: 1, HIGH_BASE: 2, EARLY_ALERT: 3, NOT_HIGH: 4, INSUFFICIENT_GAMES: 5, INCOMPLETE: 6, UNMATCHED: 7, EXCLUDED: 8 };

const MACHINE_ALIASES = {
  "ソードアート": "ソードアートオンライン2",
  "ウルトラミラクルジャグ": "ウルトラミラクルジャグラー",
  "モンハンライズ": "モンスターハンターライズ",
  "カバネリ海門決戦": "甲鉄城のカバネリ海門決戦",
  "ゴージャグ3": "ゴーゴージャグラー3",
  "ネオアイム": "ネオアイムジャグラー",
  "ファンキー2": "ファンキージャグラー2",
  "ハッピージャグ": "ハッピージャグラー",
  "甲鉄城カバネリ": "甲鉄城のカバネリ",
  "マギレコ": "マギアレコード",
  "北斗転生の章2": "北斗の拳転生の章2",
  "ジャグラーガールズk": "ジャグラーガールズ",
};

function machineKey(name) {
  let text = String(name || "").replaceAll("ＶⅢ", "V3").replaceAll("VⅢ", "V3");
  text = text.normalize("NFKC");
  text = text.replace(/[・･\s!！?？'’"“”\[\]【】（）()：:／/－—−\-]/g, "");
  text = text.toLowerCase().replaceAll("iii", "3").replaceAll("ii", "2");
  text = text.replace(/パチスロ|スマスロ/g, "");
  text = text.replace(/^(?:11|20|40|46)/, "");
  text = text.replace(/^[lsp]+(?=[\u3040-\u30ff\u4e00-\u9fff]|u[\u3040-\u30ff])/, "");
  if (text.startsWith("uミラクル")) {
    text = `ウルトラ${text.slice(1)}`;
  }
  const suffix = /(?:[a-z]{2}\d{0,2}|(?<=\d)[a-z]|[a-z]\d{1,2})$/;
  let prev = "";
  while (prev !== text) {
    prev = text;
    const next = text.replace(suffix, "");
    if (next.length < 3) {
      break;
    }
    text = next;
  }
  text = text.replace(/(?<=[\u3040-\u30ff\u4e00-\u9fff])v$/, "5");
  text = text.replace(/(?<=[\u3040-\u30ff\u4e00-\u9fff])[a-z]$/, "");
  return MACHINE_ALIASES[text] || text;
}

function machineCategory(item) {
  const series = item.series || item.criteria?.series || "";
  if (item.judgment_type === "A" || series === "ジャグラー") {
    return "juggler";
  }
  const name = String(item.machine_name || "").normalize("NFKC");
  const smart = series === "スマスロ"
    || /スマスロ|北斗|喰種|グール|ヴァルヴ|ヴヴヴ|モンキー|モンハン|モンスターハンター|化物語|番長|ミリオンゴッド/.test(name)
    || /(?:^|[^A-Za-z0-9])L(?=[\u3040-\u30ff\u4e00-\u9fff])/i.test(name);
  if (smart) {
    return "smaslo";
  }
  return "medal";
}

function categoryLabel(id) {
  if (id === "juggler") return "ジャグラー・Aタイプ";
  if (id === "smaslo") return "スマスロAT機";
  return "メダルAT・その他";
}

function crossGroups() {
  const groups = new Map();
  for (const item of machineList()) {
    const key = machineKey(item.machine_name) || item.machine_name || "(不明)";
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(item);
  }
  return [...groups.values()].map((rows) => {
    const byHall = new Map();
    for (const item of rows) {
      const hall = item.hall_name || "(店舗不明)";
      if (!byHall.has(hall)) {
        byHall.set(hall, []);
      }
      byHall.get(hall).push(item);
    }
    const halls = [...byHall.values()].map((items) => {
      const best = [...items].sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9))[0];
      if (items.length === 1) {
        return best;
      }
      const units = items.reduce((sum, item) => sum + (Number(item.unit_count) || 0), 0);
      const gamesKnown = items.every((item) => item.real_normal_games != null);
      const games = gamesKnown ? items.reduce((sum, item) => sum + Number(item.real_normal_games), 0) : null;
      const recover = (field) => {
        if (!gamesKnown) {
          return null;
        }
        let hits = 0;
        for (const item of items) {
          const probability = Number(item[field]);
          const played = Number(item.real_normal_games);
          if (!probability || !played) {
            return null;
          }
          hits += Math.round(played / probability);
        }
        return hits > 0 ? games / hits : null;
      };
      return {
        ...best,
        unit_count: units,
        real_normal_games: games,
        total_games: games,
        synthetic_probability: recover("synthetic_probability"),
        rb_probability: recover("rb_probability"),
      };
    });
    const ranked = [...halls].sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9));
    const names = halls.map((item) => String(item.machine_name || ""));
    const clean = names.filter((value) => !/^[\d⑪-⑳]/.test(value.normalize("NFKC")));
    const titleName = [...(clean.length ? clean : names)].sort((a, b) => a.length - b.length)[0] || "(機種不明)";
    const title = halls.find((item) => item.machine_name === titleName) || halls[0];
    return {
      key: machineKey(title.machine_name),
      name: title.machine_name || "(機種不明)",
      category: machineCategory(title),
      halls: ranked,
      best: ranked[0]?.status || "",
      highCount: halls.filter((item) => normalizedStatus(item) === "ALL_HIGH").length,
    };
  });
}

function renderCategoryFilters(groups) {
  const counts = { all: groups.length, juggler: 0, smaslo: 0, medal: 0 };
  groups.forEach((group) => {
    counts[group.category] = (counts[group.category] || 0) + 1;
  });
  document.querySelectorAll("#category-filters .filter").forEach((button) => {
    const id = button.dataset.category || "all";
    const base = id === "all" ? "すべて" : categoryLabel(id);
    button.replaceChildren(text(`${base}（${counts[id] || 0}）`));
    button.classList.toggle("is-active", categoryFilter === id);
  });
}

function crossStatusRank(item) {
  return STATUS_RANK[normalizedStatus(item)] ?? 9;
}

function hallHasCrossData(hall) {
  return CROSS_DATA_STATUSES.has(normalizedStatus(hall));
}

function buildHallGroupIndex() {
  const index = new Map();
  for (const group of payload.hall_groups || []) {
    for (const name of group.halls || []) {
      index.set(name, { id: group.id, label: group.label || group.id });
    }
  }
  return index;
}

function crossGroupStartsOpen(groupId) {
  if (!crossMajorOnly) {
    return true;
  }
  return CROSS_MAJOR_GROUP_IDS.has(groupId);
}

function isCrossGroupOpen(machineKey, groupId) {
  const key = `${machineKey}::${groupId}`;
  if (crossOpenOverrides.has(key)) {
    return crossOpenOverrides.get(key);
  }
  return crossGroupStartsOpen(groupId);
}

function sortCrossHalls(halls) {
  const copy = [...halls];
  if (crossHallSort === "name") {
    copy.sort((a, b) => String(a.hall_name || "").localeCompare(String(b.hall_name || ""), "ja"));
    return copy;
  }
  copy.sort((a, b) => crossStatusRank(a) - crossStatusRank(b) || String(a.hall_name || "").localeCompare(String(b.hall_name || ""), "ja"));
  return copy;
}

function bucketCrossHalls(halls, index) {
  const buckets = new Map();
  for (const hall of halls) {
    const owner = index.get(hall.hall_name) || { id: "other", label: "その他店舗" };
    if (!buckets.has(owner.id)) {
      buckets.set(owner.id, { id: owner.id, label: owner.label, halls: [] });
    }
    buckets.get(owner.id).halls.push(hall);
  }
  const rank = new Map((payload.hall_groups || []).map((group, order) => [group.id, order]));
  rank.set("other", 999);
  return [...buckets.values()]
    .sort((a, b) => (rank.get(a.id) ?? 500) - (rank.get(b.id) ?? 500))
    .map((bucket) => ({ ...bucket, halls: sortCrossHalls(bucket.halls) }));
}

function syncCrossToggles() {
  const dataBtn = document.getElementById("cross-data-only");
  const majorBtn = document.getElementById("cross-major-only");
  if (dataBtn) {
    dataBtn.classList.toggle("is-active", crossDataOnly);
    dataBtn.setAttribute("aria-pressed", crossDataOnly ? "true" : "false");
  }
  if (majorBtn) {
    majorBtn.classList.toggle("is-active", crossMajorOnly);
    majorBtn.setAttribute("aria-pressed", crossMajorOnly ? "true" : "false");
    majorBtn.replaceChildren(text(crossMajorOnly ? "すべて展開" : "主要系列のみ展開"));
  }
}

function renderCrossGroupFilters() {
  const root = document.getElementById("cross-group-filters");
  if (!root) {
    return;
  }
  const options = [{ id: "all", label: "全系列" }];
  for (const group of visibleGroups()) {
    options.push({ id: group.id, label: group.label });
  }
  if (crossGroupFilter !== "all" && !options.some((option) => option.id === crossGroupFilter)) {
    crossGroupFilter = "all";
  }
  root.replaceChildren();
  const caption = document.createElement("span");
  caption.className = "cross-filter-label";
  caption.append(text("系列"));
  root.append(caption);
  for (const option of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "filter";
    const active = crossGroupFilter === option.id;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.append(text(option.label));
    btn.addEventListener("click", () => {
      if (crossGroupFilter === option.id) {
        return;
      }
      crossGroupFilter = option.id;
      render();
    });
    root.append(btn);
  }
}

function makeCrossHallCell(hall) {
  const status = normalizedStatus(hall) || "";
  const cell = document.createElement("div");
  cell.className = `cross-hall card-${status}`;
  const name = document.createElement("strong");
  name.append(text(hall.hall_name || "-"));
  const line = document.createElement("div");
  line.className = "cross-hall-line";
  const badge = document.createElement("span");
  badge.className = `badge badge-${status || "UNMATCHED"}`;
  badge.append(text(cardBadgeLabel(hall)));
  const games = document.createElement("span");
  games.className = "cross-hall-meta";
  games.append(text(`通常 ${formatNumber(hall.real_normal_games ?? hall.total_games)}G · ${formatNumber(hall.unit_count)}台`));
  line.append(badge, games);
  const odds = document.createElement("span");
  odds.className = "odds";
  odds.append(text(isAtType(hall) ? `初当り ${formatOdds(hall.synthetic_probability)}` : `合成 ${formatOdds(hall.synthetic_probability)} / REG ${formatOdds(hall.rb_probability)}`));
  cell.append(name, line, odds);
  return cell;
}

function appendCrossGroup(card, machineKey, bucket, index) {
  const open = isCrossGroupOpen(machineKey, bucket.id);
  const section = document.createElement("section");
  section.className = `cross-group${open ? "" : " is-collapsed"}`;
  section.dataset.groupId = bucket.id;
  const panelId = `cross-group-${index}-${bucket.id}`;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "cross-group-toggle";
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.setAttribute("aria-controls", panelId);
  const chevron = document.createElement("span");
  chevron.className = "cross-chevron";
  chevron.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "cross-group-name";
  label.append(text(bucket.label));
  const count = document.createElement("span");
  count.className = "cross-group-count";
  const highCount = bucket.halls.filter((item) => normalizedStatus(item) === "ALL_HIGH").length;
  count.append(text(`${bucket.halls.length}店`));
  toggle.append(chevron, label, count);
  if (highCount) {
    const mark = document.createElement("span");
    mark.className = "badge badge-ALL_HIGH";
    mark.append(text(`全6 ${highCount}`));
    toggle.append(mark);
  }
  const grid = document.createElement("div");
  grid.className = "cross-halls";
  grid.id = panelId;
  grid.hidden = !open;
  grid.setAttribute("role", "region");
  grid.setAttribute("aria-label", bucket.label);
  for (const hall of bucket.halls) {
    grid.append(makeCrossHallCell(hall));
  }
  toggle.addEventListener("click", () => {
    const next = grid.hidden;
    crossOpenOverrides.set(`${machineKey}::${bucket.id}`, next);
    section.classList.toggle("is-collapsed", !next);
    toggle.setAttribute("aria-expanded", next ? "true" : "false");
    grid.hidden = !next;
  });
  section.append(toggle, grid);
  card.append(section);
}

function renderCross() {
  const groups = crossGroups();
  renderCategoryFilters(groups);
  renderCrossGroupFilters();
  syncCrossToggles();
  const hallGroups = buildHallGroupIndex();
  const needle = machineQuery.trim().toLowerCase();
  const visible = groups
    .filter((group) => categoryFilter === "all" || group.category === categoryFilter)
    .filter((group) => !needle || `${group.name} ${group.halls.map((item) => item.machine_name).join(" ")}`.toLowerCase().includes(needle))
    .map((group) => {
      const halls = group.halls.filter((hall) => {
        if (crossGroupFilter !== "all") {
          const owner = hallGroups.get(hall.hall_name) || { id: "other" };
          if (owner.id !== crossGroupFilter) {
            return false;
          }
        }
        if (crossDataOnly && !hallHasCrossData(hall)) {
          return false;
        }
        return true;
      });
      const ranked = [...halls].sort((a, b) => crossStatusRank(a) - crossStatusRank(b));
      return {
        ...group,
        halls,
        best: normalizedStatus(ranked[0]) || "",
        highCount: halls.filter((item) => normalizedStatus(item) === "ALL_HIGH").length,
      };
    })
    .filter((group) => group.halls.length > 0)
    .sort((a, b) => (STATUS_RANK[a.best] ?? 9) - (STATUS_RANK[b.best] ?? 9) || b.highCount - a.highCount || a.name.localeCompare(b.name, "ja"));
  if (crossSummaryEl) {
    const hallCount = new Set(machineList().map((item) => item.hall_name).filter(Boolean)).size;
    const notes = ["系列ごとに折りたためます"];
    if (crossDataOnly) {
      notes.push("データ不足の店舗は非表示");
    }
    if (crossGroupFilter !== "all") {
      const selected = (payload.hall_groups || []).find((group) => group.id === crossGroupFilter);
      notes.push(`${selected?.label || "選択系列"}のみ`);
    }
    crossSummaryEl.textContent = `全${hallCount}店舗を機種単位で比較。${visible.length}機種を表示中。${notes.join("。")}。`;
  }
  if (!crossCardsEl) {
    return;
  }
  crossCardsEl.replaceChildren();
  crossEmptyEl?.classList.toggle("hidden", visible.length > 0);
  visible.forEach((group, cardIndex) => {
    const card = document.createElement("article");
    card.className = `cross-card card-${group.best || "UNMATCHED"}`;
    const head = document.createElement("div");
    head.className = "cross-head";
    const title = document.createElement("h3");
    title.append(text(group.name));
    const cat = document.createElement("span");
    cat.className = group.category === "juggler" ? "badge badge-A" : "badge badge-AT";
    cat.append(text(categoryLabel(group.category)));
    const best = document.createElement("span");
    best.className = `badge badge-${group.best || "UNMATCHED"}`;
    const lead = [...group.halls].sort((a, b) => crossStatusRank(a) - crossStatusRank(b))[0] || {};
    best.append(text(group.highCount ? `全6が${group.highCount}店舗` : cardBadgeLabel(lead)));
    head.append(title, cat, best);
    const meta = document.createElement("p");
    meta.className = "cross-meta";
    const units = group.halls.reduce((sum, item) => sum + (item.unit_count || 0), 0);
    meta.append(text(`${group.halls.length}店舗 / ${units}台`));
    card.append(head, meta);
    const buckets = bucketCrossHalls(group.halls, hallGroups);
    buckets.forEach((bucket) => appendCrossGroup(card, group.key, bucket, cardIndex));
    crossCardsEl.append(card);
  });
}

function syncToTopButton() {
  const button = document.getElementById("to-top");
  if (!button) {
    return;
  }
  const show = window.scrollY >= TO_TOP_THRESHOLD;
  button.classList.toggle("is-visible", show);
  button.setAttribute("aria-hidden", show ? "false" : "true");
  button.tabIndex = show ? 0 : -1;
}

function alignLocaleToHall(hall) {
  const locale = hallLocale(hall);
  if (!locale) {
    return;
  }
  if (locale.prefecture && prefectureList().some((item) => item.id === locale.prefecture)) {
    prefectureFilter = locale.prefecture;
  }
  const presets = areaPresets();
  if (locale.area && presets.some((item) => item.id === locale.area)) {
    areaFilter = locale.area;
  } else if (presets.some((item) => item.id === "all")) {
    areaFilter = "all";
  }
}

function focusHall(hall) {
  alignLocaleToHall(hall);
  hallFilter = hall;
  syncGroupToHall(hall);
  if (mainView !== "stores") {
    setMainView("stores");
  } else {
    render();
  }
  requestAnimationFrame(() => {
    document.getElementById("section-all-high")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function eventsToday() {
  return payload.events || [];
}

function eventsForHall(hall) {
  return eventsToday().filter((event) => (event.hall_names || []).includes(hall));
}

function hallHasFeatured(machines) {
  return machines.some((item) => isFeaturedStatus(item));
}

function renderEventBadges(events) {
  const row = document.createElement("div");
  row.className = "event-badges";
  events.forEach((event) => {
    const badge = document.createElement("span");
    badge.className = `event-badge event-badge-${event.badge_kind || "day"}`;
    badge.append(text(event.badge || event.event_title || ""));
    row.append(badge);
  });
  return row;
}

function renderJointPicks() {
  const picks = payload.group_joint_picks || [];
  if (!picks.length) {
    return null;
  }
  const box = document.createElement("section");
  box.className = "today-joint";
  const title = document.createElement("h3");
  title.append(text("🎯 本日のグループ合同有力機種"));
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.append(text("合同イベントの系列全体で、同じ機種が全6または全56の店舗を2店以上数えます。"));
  box.append(title, hint);
  const grouped = new Map();
  picks.forEach((pick) => {
    const key = pick.event_id || pick.group || pick.event_title;
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(pick);
  });
  grouped.forEach((rows) => {
    const list = document.createElement("div");
    list.className = "today-joint-list";
    rows.forEach((pick) => {
      const scopedHalls = (pick.halls || []).filter((hall) => inArea(hall.hall_name));
      if (scopedHalls.length < 2) {
        return;
      }
      pick = {
        ...pick,
        halls: scopedHalls,
        hall_count: scopedHalls.length,
        all_high_count: scopedHalls.filter((hall) => hall.status === "ALL_HIGH").length,
        high_56_count: scopedHalls.filter((hall) => hall.status === "HIGH_56").length,
      };
      const row = document.createElement("div");
      row.className = "today-joint-machine";
      const name = document.createElement("strong");
      name.append(text(pick.machine_name || "(機種不明)"));
      row.append(name);
      if (pick.all_high_count) {
        const high = document.createElement("span");
        high.className = "badge badge-ALL_HIGH";
        high.append(text(`全6: ${pick.all_high_count}店`));
        row.append(high);
      }
      if (pick.high_56_count) {
        const mid = document.createElement("span");
        mid.className = "badge badge-HIGH_56";
        mid.append(text(`全56: ${pick.high_56_count}店`));
        row.append(mid);
      }
      const halls = document.createElement("span");
      halls.className = "today-joint-halls";
      (pick.halls || []).forEach((hall) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "today-joint-hall";
        button.append(text(`${hall.hall_name}（${hall.status_label || ""}）`));
        button.addEventListener("click", () => focusHall(hall.hall_name));
        halls.append(button);
      });
      row.append(halls);
      list.append(row);
    });
    if (!list.childElementCount) {
      return;
    }
    const head = document.createElement("p");
    head.className = "today-joint-event";
    head.append(text(rows[0].badge || rows[0].event_title || "合同"));
    box.append(head, list);
  });
  return box;
}

function renderTodaySummary() {
  const root = document.getElementById("today-summary");
  const empty = document.getElementById("today-empty");
  const meta = document.getElementById("today-summary-meta");
  if (!root) {
    return;
  }
  const featured = machineList().filter((item) => isFeaturedStatus(item));
  const byHall = new Map();
  featured.forEach((item) => {
    const hall = item.hall_name || "(店舗不明)";
    if (!byHall.has(hall)) {
      byHall.set(hall, []);
    }
    byHall.get(hall).push(item);
  });
  eventsToday().forEach((event) => {
    (event.direct_hall_names || []).forEach((hall) => {
      if (!hall || !inArea(hall) || byHall.has(hall)) {
        return;
      }
      byHall.set(hall, []);
    });
  });
  const rank = { ALL_HIGH: 0, HIGH_56: 1, HIGH_BASE: 2 };
  const scoreOf = (list) => list.reduce((sum, item) => sum + (100 - (rank[normalizedStatus(item)] ?? 9) * 30), 0);
  const boostOf = (hall, list) => (hallHasFeatured(list) && eventsForHall(hall).length ? 1 : 0);
  const halls = [...byHall.entries()].sort((a, b) => {
    return boostOf(b[0], b[1]) - boostOf(a[0], a[1]) || scoreOf(b[1]) - scoreOf(a[1]) || a[0].localeCompare(b[0], "ja");
  });
  const high = featured.filter((item) => normalizedStatus(item) === "ALL_HIGH").length;
  const mid = featured.filter((item) => normalizedStatus(item) === "HIGH_56").length;
  const base = featured.filter((item) => normalizedStatus(item) === "HIGH_BASE").length;
  const reacting = halls.filter(([, machines]) => machines.length).length;
  const eventHalls = halls.filter(([hall]) => eventsForHall(hall).length).length;
  const area = activeArea();
  const title = document.getElementById("today-summary-title");
  if (title) {
    const scope = !area || area.id === "all" ? "全店舗サマリー" : area.label;
    title.textContent = `🔥 本日の全台系・高設定挙動ホール一覧（${scope}）`;
  }
  if (meta) {
    const scopeCount = hallTabNames().length;
    const scopeLabel = !area || area.id === "all" ? `全${scopeCount}店舗` : `${area.label}の${scopeCount}店舗`;
    const eventNote = eventHalls ? ` ／ 本日イベント ${eventHalls}店舗` : "";
    meta.textContent = `${scopeLabel}中 ${reacting}店舗が反応 ／ 全6 ${high}機種・全56 ${mid}機種・全456 ${base}機種${eventNote}`;
  }
  root.replaceChildren();
  const joint = renderJointPicks();
  if (joint) {
    root.append(joint);
  }
  empty?.classList.toggle("hidden", halls.length > 0 || Boolean(joint));
  halls.forEach(([hall, machines]) => {
    const events = eventsForHall(hall);
    const boosted = hallHasFeatured(machines) && events.length > 0;
    const button = document.createElement("button");
    button.type = "button";
    button.className = boosted ? "today-hall is-boosted" : "today-hall";
    if (events.length) {
      button.append(renderEventBadges(events));
    }
    const head = document.createElement("div");
    head.className = "today-hall-head";
    const name = document.createElement("span");
    name.className = "today-hall-name";
    name.append(text(hall));
    const counts = document.createElement("span");
    counts.className = "today-counts";
    [
      ["ALL_HIGH", "全6", machines.filter((item) => normalizedStatus(item) === "ALL_HIGH").length],
      ["HIGH_56", "全56", machines.filter((item) => normalizedStatus(item) === "HIGH_56").length],
      ["HIGH_BASE", "全456", machines.filter((item) => normalizedStatus(item) === "HIGH_BASE").length],
    ].forEach(([status, label, count]) => {
      if (!count) {
        return;
      }
      const pill = document.createElement("span");
      pill.className = `badge badge-${status}`;
      pill.append(text(`${label}: ${count}機種`));
      counts.append(pill);
    });
    const dataBadge = document.createElement("span");
    const dataStatus = hallDataStatus(hall);
    dataBadge.className = `data-badge data-${dataStatus.state}`;
    dataBadge.append(text(dataStatus.label));
    head.append(name, dataBadge, counts);
    const chips = document.createElement("div");
    chips.className = "today-machines";
    machines
      .slice()
      .sort((a, b) => (rank[normalizedStatus(a)] ?? 9) - (rank[normalizedStatus(b)] ?? 9))
      .forEach((item) => {
        const chip = document.createElement("span");
        const status = normalizedStatus(item);
        chip.className = `today-chip today-chip-${status}`;
        chip.dataset.signal = status;
        const machine = document.createElement("span");
        machine.className = "today-chip-name";
        machine.append(text(item.machine_name || "(機種不明)"));
        const odds = document.createElement("span");
        odds.className = "today-chip-odds";
        odds.append(text(formatOdds(item.synthetic_probability)));
        chip.append(machine, odds);
        chips.append(chip);
      });
    button.append(head, chips);
    if (!machines.length) {
      const note = document.createElement("p");
      note.className = "today-hall-note";
      note.append(text("全台系の反応はまだありません"));
      button.append(note);
    }
    button.addEventListener("click", () => focusHall(hall));
    root.append(button);
  });
  syncSignalJumps();
}

function renderSelectedHallEvents() {
  const root = document.getElementById("hall-event-badges");
  if (!root) {
    return;
  }
  const hall = hallFilter && hallFilter !== "all" ? hallFilter : "";
  const events = hall ? eventsForHall(hall) : [];
  root.replaceChildren();
  root.classList.toggle("hidden", events.length === 0);
  if (events.length) {
    root.append(renderEventBadges(events));
  }
}

function renderStore() {
  const scoped = hallItems(machineList());
  const results = searched(scoped);
  const summary = counts(scoped);
  const judged = scoped.filter((item) => listBucket(item));
  const judgedUnits = judged.reduce((sum, item) => sum + (item.unit_count || 0), 0);
  statCountEl.textContent = `全${judged.length}機種 / 全${judgedUnits}台`;
  statAllHighEl.textContent = `${summary.high}機種`;
  if (statHigh56El) {
    statHigh56El.textContent = `${summary.high56}機種`;
  }
  statHighBaseEl.textContent = `${summary.highBase}機種`;
  renderCards(results);
  renderHallDigitBoards(storeDigitBoardEl, hallBoardsForView(hallFilter));
  renderTable(results);
}

function inPrefecture(hall) {
  const locale = hallLocale(hall);
  const pref = activePrefecture()?.id || prefectureFilter;
  if (!locale || !pref) {
    return true;
  }
  return locale.prefecture === pref;
}

const EXTRA_AREA_LABELS = {
  munakata: "宗像",
  iizuka: "飯塚",
  karatsu: "唐津",
  kumamoto: "熊本",
};

function areaLabelForHall(hall) {
  const locale = hallLocale(hall);
  if (!locale) {
    return "—";
  }
  const source = payload.area_presets || [];
  const preset = source.find((item) => item.id !== "all" && (item.id === locale.area || (item.areas || []).includes(locale.area)));
  if (preset) {
    return preset.label;
  }
  return EXTRA_AREA_LABELS[locale.area] || locale.area || "—";
}

function formatEventDate(iso) {
  const parts = String(iso || "").split("-").map(Number);
  if (parts.length !== 3 || parts.some((value) => !value)) {
    return iso || "";
  }
  const stamp = new Date(parts[0], parts[1] - 1, parts[2]);
  const weeks = "日月火水木金土";
  const prefix = iso === payload.events_date ? "本日 " : iso === payload.tomorrow_date ? "明日 " : "";
  return `${prefix}${parts[1]}月${parts[2]}日（${weeks[stamp.getDay()]}）`;
}

function coverageRows() {
  const events = payload.upcoming_events || [];
  const rows = [];
  const seen = new Set();
  events.forEach((event) => {
    const names = event.hall_names || [];
    names.forEach((hall) => {
      if (!hall || !inArea(hall)) {
        return;
      }
      const key = `${event.date}|${hall}|${event.event_title}|${event.event_type}`;
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      rows.push({
        date: event.date,
        hall,
        area: areaLabelForHall(hall),
        title: event.event_title || "",
        badge: event.badge || event.event_type || "",
        badgeKind: event.badge_kind || "day",
      });
    });
  });
  rows.sort((a, b) => a.date.localeCompare(b.date) || a.area.localeCompare(b.area, "ja") || a.hall.localeCompare(b.hall, "ja"));
  return rows;
}

function renderCoverage() {
  const root = document.getElementById("coverage-list");
  const empty = document.getElementById("coverage-empty");
  const meta = document.getElementById("coverage-meta");
  if (!root) {
    return;
  }
  const rows = coverageRows();
  const todayCount = rows.filter((row) => row.date === payload.events_date).length;
  const tomorrowCount = rows.filter((row) => row.date === payload.tomorrow_date).length;
  if (meta) {
    meta.textContent = `本日 ${todayCount}件 / 明日 ${tomorrowCount}件。店舗名を押すとその店舗の判定へ移動します。`;
  }
  empty?.classList.toggle("hidden", rows.length > 0);
  root.replaceChildren();
  rows.forEach((row) => {
    const line = document.createElement("article");
    line.className = "coverage-row";
    const date = document.createElement("span");
    date.className = "coverage-date";
    date.append(text(formatEventDate(row.date)));
    const hall = document.createElement("button");
    hall.type = "button";
    hall.className = "coverage-hall";
    hall.append(text(row.hall));
    hall.addEventListener("click", () => focusHall(row.hall));
    const title = document.createElement("span");
    title.className = "coverage-title";
    title.append(text(row.title));
    const badge = document.createElement("span");
    badge.className = `event-badge event-badge-${row.badgeKind}`;
    badge.append(text(row.badge));
    line.append(date, hall, title, badge);
    root.append(line);
  });
}

function renderDigitsView() {
  if (!hallDigitBoardsEl) {
    return;
  }
  if (!digitHall || digitHall === "all") {
    hallDigitBoardsEl.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.append(text("ホールを選ぶと、その店の末尾判定を表示します。"));
    hallDigitBoardsEl.append(empty);
    return;
  }
  renderHallDigitBoards(hallDigitBoardsEl, hallBoardsForView(digitHall));
}

function setMainView(view) {
  mainView = view;
  document.querySelectorAll(".main-tab").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("hidden", el.id !== `view-${view}`);
  });
  render();
}

function jointHeadline(event) {
  const group = (payload.hall_groups || []).find((item) => item.id === event.group);
  const groupLabel = group?.label || "";
  const title = String(event.event_title || "").trim();
  const named = title.includes("合同") || (groupLabel && title.includes(groupLabel));
  const headline = named ? title : [groupLabel, title].filter(Boolean).join(" ");
  return `🔥 【${headline || groupLabel || "合同イベント"}】本日開催中！`;
}

function eventTouchesScope(event) {
  const names = event.hall_names || event.direct_hall_names || [];
  return names.some((hall) => inArea(hall));
}

function jointEventsToday() {
  const events = eventsToday().filter((event) => event.event_type === "合同" && event.group && eventTouchesScope(event));
  const preview = new URLSearchParams(window.location.search).has("jointPreview");
  if (preview && !events.some((event) => event.event_title === "ワンダーランド全店合同")) {
    events.unshift({
      event_type: "合同",
      group: "wonderland",
      event_title: "ワンダーランド全店合同",
    });
  }
  return events;
}

function renderJointAnnounce() {
  const root = document.getElementById("joint-announce");
  if (!root) {
    return;
  }
  const seen = new Set();
  const events = jointEventsToday();
  root.replaceChildren();
  root.classList.remove("hidden");
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "joint-announce-empty";
    empty.append(text("📢 本日の合同取材: 本日開催予定の合同取材はありません"));
    root.append(empty);
    return;
  }
  events.forEach((event) => {
    const headline = jointHeadline(event);
    if (seen.has(headline)) {
      return;
    }
    seen.add(headline);
    const card = document.createElement("button");
    card.type = "button";
    card.className = "joint-announce-card";
    card.append(text(headline));
    card.addEventListener("click", () => setMainView("coverage"));
    root.append(card);
  });
}

function render() {
  ensureSelection();
  renderAreaFilters();
  renderHallFilters();
  renderSelectedHallEvents();
  renderJointAnnounce();
  renderTodaySummary();
  judgedAtEl.textContent = formatWhen(payload.judged_at);
  updateFetchedStamps();
  updateFreshness();
  if (mainView === "coverage") {
    renderCoverage();
    return;
  }
  if (mainView === "machines") {
    renderCross();
    return;
  }
  if (mainView === "digits") {
    renderDigitsView();
    return;
  }
  renderStore();
}

const HALL_CATALOG_KEYS = [
  "halls",
  "configured_halls",
  "hall_groups",
  "hall_areas",
  "hall_locales",
  "prefectures",
  "area_presets",
  "enabled_hall_count",
  "hall_status",
];

function applyHallCatalog(catalog) {
  if (!catalog || typeof catalog !== "object") {
    return;
  }
  HALL_CATALOG_KEYS.forEach((key) => {
    if (catalog[key] != null) {
      payload[key] = catalog[key];
    }
  });
}

async function loadHallCatalog() {
  const response = await fetch("/api/hall-catalog");
  if (redirectIfUnauthorized(response)) {
    return;
  }
  if (!response.ok) {
    throw new Error(`店舗一覧の取得に失敗しました (${response.status})`);
  }
  applyHallCatalog(await response.json());
  render();
}

let judgmentsInFlight = null;

async function loadJudgments(force) {
  if (judgmentsInFlight) {
    return judgmentsInFlight;
  }
  judgmentsInFlight = loadJudgmentsNow(force).finally(() => {
    judgmentsInFlight = null;
  });
  return judgmentsInFlight;
}

async function loadJudgmentsNow(force) {
  showError("");
  const pref = prefectureFilter || "fukuoka";
  const response = await fetch(`/api/judgments?prefecture=${encodeURIComponent(pref)}`);
  if (redirectIfUnauthorized(response)) {
    return;
  }
  if (!response.ok) {
    throw new Error(`判定結果の取得に失敗しました (${response.status})`);
  }
  const next = await response.json();
  const stamp = `${next.judged_at || ""}|${next.events_updated_at || ""}`;
  if (!force && stamp === lastJudgedAt) {
    updateFreshness();
    return;
  }
  payload = next;
  lastJudgedAt = stamp;
  render();
}

function setButtonBusy(button, busy) {
  if (!button) {
    return;
  }
  if (busy) {
    button.disabled = true;
    button.classList.add("is-busy");
    button.setAttribute("aria-busy", "true");
    return;
  }
  button.disabled = false;
  button.classList.remove("is-busy");
  button.removeAttribute("aria-busy");
}

async function rerunJudgment() {
  showError("");
  setButtonBusy(judgeBtn, true);
  try {
    const response = await fetch("/api/judge", { method: "POST" });
    if (redirectIfUnauthorized(response)) {
      return;
    }
    if (!response.ok) {
      throw new Error(`再判定に失敗しました (${response.status})`);
    }
    payload = await response.json();
    lastJudgedAt = `${payload.judged_at || ""}|${payload.events_updated_at || ""}`;
    render();
  } finally {
    setButtonBusy(judgeBtn, false);
  }
}

reloadBtn.addEventListener("click", () => {
  setButtonBusy(reloadBtn, true);
  Promise.all([loadJudgments(true), loadWatchConfig()])
    .catch((err) => showError(err.message))
    .finally(() => setButtonBusy(reloadBtn, false));
});
judgeBtn.addEventListener("click", () => {
  rerunJudgment().catch((err) => showError(err.message));
});
watchBtn?.addEventListener("click", () => {
  loadWatchConfig()
    .then(() => openWatchModal())
    .catch((err) => showError(err.message));
});
watchClose?.addEventListener("click", closeWatchModal);
watchModal?.querySelector("[data-close-watch]")?.addEventListener("click", closeWatchModal);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && watchModal && !watchModal.classList.contains("hidden")) {
    closeWatchModal();
  }
});
document.getElementById("other-machines-toggle")?.addEventListener("click", () => {
  otherOpen = !otherOpen;
  renderOtherMachines(searched(hallItems(machineList())));
});
searchEl.addEventListener("input", () => {
  query = searchEl.value || "";
  render();
});
machineSearchEl?.addEventListener("input", () => {
  machineQuery = machineSearchEl.value || "";
  if (mainView !== "machines") {
    setMainView("machines");
    return;
  }
  render();
});
document.querySelectorAll(".main-tab").forEach((button) => {
  button.addEventListener("click", () => setMainView(button.dataset.view || "stores"));
});
hallSelectEl?.addEventListener("change", () => {
  hallFilter = hallSelectEl.value;
  syncGroupToHall(hallFilter);
  render();
});
digitHallSelectEl?.addEventListener("change", () => {
  digitHall = digitHallSelectEl.value || "all";
  syncGroupToHall(digitHall);
  render();
  if (digitHall !== "all") {
    requestAnimationFrame(() => scrollToHallDigitBoard());
  }
});
watchHallSelectEl?.addEventListener("change", () => {
  watchHall = watchHallSelectEl.value;
  renderWatchHall();
});
document.querySelectorAll("#category-filters .filter").forEach((button) => {
  button.addEventListener("click", () => {
    categoryFilter = button.dataset.category || "all";
    render();
  });
});
document.getElementById("cross-data-only")?.addEventListener("click", () => {
  crossDataOnly = !crossDataOnly;
  render();
});
document.getElementById("cross-major-only")?.addEventListener("click", () => {
  crossMajorOnly = !crossMajorOnly;
  crossOpenOverrides.clear();
  render();
});
document.getElementById("cross-hall-sort")?.addEventListener("change", (event) => {
  crossHallSort = event.target.value || "status";
  render();
});
document.getElementById("to-top")?.addEventListener("click", () => {
  window.scrollTo({ top: 0, behavior: "smooth" });
});
window.addEventListener("scroll", syncToTopButton, { passive: true });
syncToTopButton();

document.querySelectorAll("#status-filters .filter").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter || "all";
    document.querySelectorAll("#status-filters .filter").forEach((el) => {
      el.classList.toggle("is-active", el === button);
    });
    render();
  });
});

document.querySelectorAll(".signal-jump").forEach((button) => {
  button.addEventListener("click", () => jumpToSignal(button.dataset.signal || ""));
});

document.querySelectorAll(".stat-jump").forEach((card) => {
  const activate = () => {
    const nextFilter = card.dataset.filter;
    if (nextFilter) {
      setStatusFilter(nextFilter);
      render();
    }
    jumpTo(card.dataset.jump || "section-list");
  };
  card.addEventListener("click", activate);
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  });
});

renderAreaFilters();
Promise.all([
  loadHallCatalog().catch((err) => showError(err.message)),
  loadWatchConfig().catch((err) => showError(err.message)),
]).finally(() => {
  loadJudgments(true).catch((err) => showError(err.message));
});
setInterval(() => {
  updateFreshness();
  loadJudgments(false).catch((err) => showError(err.message));
  loadWatchConfig().catch(() => {});
}, 15000);
