"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const giB = (kib) => (Number(kib || 0) / 1048576).toFixed(2);
const memory = (bytes) =>
  bytes == null
    ? "—"
    : Number(bytes) >= 1073741824
      ? `${(bytes / 1073741824).toFixed(2)} GiB`
      : `${(bytes / 1048576).toFixed(0)} MiB`;
const num = (value) => Number(value || 0).toLocaleString("ko-KR");
const when = (stamp) =>
  stamp
    ? new Date(stamp * 1000).toLocaleString("ko-KR", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      })
    : "기록 없음";
const age = (seconds) =>
  seconds >= 86400
    ? `${Math.floor(seconds / 86400)}일 ${Math.floor((seconds % 86400) / 3600)}시간`
    : `${Math.floor(seconds / 3600)}시간 ${Math.floor((seconds % 3600) / 60)}분`;
// "1일 3시간" plus the absolute start time on hover/long-press.
function ageCell(seconds) {
  const base = monitorData?.collected_at || Date.now() / 1000;
  return `<span class="age" title="시작 ${esc(when(base - seconds))}">${age(seconds)}</span>`;
}
const titles = {
  disks: [
    "Disk",
    "저장 공간",
    "Windows 볼륨과 WSL 내부 공간을 함께 확인하세요.",
  ],
  overview: [
    "개요",
    "내 PC 한눈에 보기",
    "서비스와 자원 상태를 한곳에서 확인하세요.",
  ],
  services: [
    "서비스 관리",
    "내 PC의 서비스",
    "서비스를 등록하고 실행과 자동 시작을 관리하세요.",
  ],
  top: [
    "Top 사용량",
    "어디에 자원을 쓰고 있나요?",
    "프로젝트와 AI 도구별 메모리 사용량입니다.",
  ],
  sessions: [
    "LLM 세션",
    "실행 중인 LLM 세션",
    "세션별 자원 사용과 프로젝트 구성을 확인하세요.",
  ],
  mcp: [
    "MCP",
    "MCP 서버 상태",
    "LLM에 연결된 MCP 서버 트리와 실행 시간을 확인하세요.",
  ],
  history: [
    "자원 이력",
    "시간에 따른 자원 변화",
    "Resource Guard가 기록한 최근 자원 상태입니다.",
  ],
  alerts: [
    "경보 이력",
    "경보가 시작되고 끝난 구간",
    "지속 시간과 주요 원인을 구간별로 확인하세요.",
  ],
  settings: [
    "설정·진단",
    "설정과 진단 정보",
    "적용 중인 임계값과 알림 채널, 설치본 상태를 확인하세요.",
  ],
  audit: [
    "작업 기록",
    "서비스 관리 기록",
    "등록과 설정 변경의 성공·실패를 확인하세요.",
  ],
};
const actionNames = {
  enable: "사용",
  disable: "미사용",
  "autostart-on": "자동 실행만 켜기",
  "autostart-off": "자동 실행만 끄기",
  start: "지금만 시작",
  stop: "지금만 중지",
  restart: "지금 재시작",
  remove: "등록 삭제",
};
const actionDescriptions = {
  enable: "자동 실행을 켜고 지금 시작합니다.",
  disable: "자동 실행을 해제하고 지금 중지합니다.",
  "autostart-on":
    "다음 WSL 시작부터 자동 실행합니다. 현재 실행 상태는 유지합니다.",
  "autostart-off":
    "다음 WSL 시작 시 자동 실행하지 않습니다. 현재 실행 상태는 유지합니다.",
  start: "지금 시작합니다. 자동 실행 설정은 유지합니다.",
  stop: "지금 중지합니다. 자동 실행 설정은 유지합니다.",
  restart: "실행 중인 서비스를 재시작합니다. 잠시 연결이 끊길 수 있습니다.",
  remove:
    "자동 실행을 해제하고 중지한 뒤 관리 등록을 삭제합니다.\n프로그램·설정·DB·Docker 볼륨·로그는 보존합니다.",
};
let csrf = "",
  currentView = "overview",
  serviceData = { services: [] },
  monitorData = null,
  diskData = null,
  dockerDfData = null,
  overviewHistory = null,
  attributionData = null,
  sessionHistoryData = null,
  pushKeyData = null,
  serviceMemoryData = [],
  historyData = null,
  alertsData = null,
  settingsData = null,
  auditData = [],
  loading = false,
  pendingForce = false,
  timer = null,
  lastRefresh = 0,
  busyAction = false,
  // The history table folds past 20 rows on narrow screens; the user's
  // expanded choice survives refreshes until the range changes.
  historyExpanded = false,
  // Settings view: only rows differing from the factory default (C4).
  settingsChangedOnly = false;
const loaded = {
  monitor: false,
  disks: false,
  services: false,
  history: false,
  alerts: false,
  settings: false,
  audit: false,
};
const HISTORY_RANGE_KEYS = ["3h", "24h", "7d", "14d"];
function storedRange() {
  try {
    const v = localStorage.getItem("wrg-history-range");
    return HISTORY_RANGE_KEYS.includes(v) ? v : "3h";
  } catch {
    return "3h";
  }
}
let historyRange = storedRange();
// A zoomed time window inside the loaded range ({from, to} epoch seconds),
// set by dragging on a history chart or by an alert episode link.
let historyWindow = null;
function storedInterval() {
  try {
    const v = localStorage.getItem("wrg-refresh");
    return ["0", "30", "60", "120"].includes(v) ? v : "30";
  } catch {
    return "30";
  }
}
$("refresh-interval").value = storedInterval();
let noticeTimer = null;
function notify(message, success = false) {
  clearTimeout(noticeTimer);
  $("notice-text").textContent = message;
  $("notice").classList.remove("hidden");
  $("notice").classList.toggle("success", success);
  if (success)
    noticeTimer = setTimeout(
      () => $("notice").classList.add("hidden"),
      7000,
    );
}
$("notice-close").addEventListener("click", () =>
  $("notice").classList.add("hidden"),
);
async function fetchCsrf() {
  let bootstrap;
  try {
    bootstrap = await fetch("/api/bootstrap", { headers: { Accept: "application/json" } });
  } catch {
    throw new Error("본인 Tailscale 연결을 확인하세요.");
  }
  if (!bootstrap.ok) throw new Error("본인 Tailscale 연결을 확인하세요.");
  try {
    csrf = (await bootstrap.json()).csrf;
  } catch {
    throw new Error("서버 응답을 해석할 수 없습니다. 화면을 새로고침하세요.");
  }
}
// POSTs reuse the session's CSRF token; a 403 (expired session, new
// cookie) refetches it once and retries instead of a bootstrap per write.
async function api(path, body, retried = false) {
  const options = { headers: { Accept: "application/json" } };
  if (body) {
    if (!csrf) await fetchCsrf();
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrf;
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("서버에 연결할 수 없습니다. 잠시 후 다시 시도하세요.");
  }
  if (body && response.status === 403 && !retried) {
    csrf = "";
    return api(path, body, true);
  }
  const type = response.headers.get("Content-Type") || "";
  if (!type.includes("application/json"))
    throw new Error(`서버 응답 오류 (HTTP ${response.status}). 잠시 후 다시 시도하세요.`);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`서버 응답을 해석할 수 없습니다 (HTTP ${response.status}).`);
  }
  if (!response.ok)
    throw new Error(data.error || `요청에 실패했습니다 (HTTP ${response.status}).`);
  return data;
}
function pill(state) {
  const labels = {
    active: "실행 중",
    inactive: "중지됨",
    failed: "실패",
    degraded: "일부 이상",
    unknown: "확인 필요",
    missing: "컨테이너 없음",
    activating: "시작 중",
    deactivating: "중지 중",
    normal: "정상",
    warning: "주의",
    critical: "위험",
  };
  const cls = ["active", "normal"].includes(state)
    ? "good"
    : ["failed", "critical"].includes(state)
      ? "bad"
      : ["degraded", "warning", "unknown"].includes(state)
        ? "warn"
        : "";
  return `<span class="pill ${cls}">${esc(labels[state] || state)}</span>`;
}
const tableSorts = {};
// headers: label string, or {t, cls, key} where key(item) yields a sortable value.
function table(headers, rows, ready = true, opts = {}) {
  if (!ready) return '<div class="empty">불러오는 중…</div>';
  const defs = headers.map((h) => (typeof h === "string" ? { t: h } : h));
  const state = opts.id ? tableSorts[opts.id] || {} : {};
  const items = rows.map((r) => (Array.isArray(r) ? { item: r, cells: r } : r));
  if (state.col != null && defs[state.col] && defs[state.col].key) {
    const key = defs[state.col].key,
      dir = state.dir === "asc" ? 1 : -1;
    items.sort((a, b) => {
      const va = key(a.item),
        vb = key(b.item);
      if (typeof va === "string" || typeof vb === "string")
        return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
      return ((va ?? 0) - (vb ?? 0)) * dir;
    });
  }
  const head = defs
    .map((h, i) =>
      h.key && opts.id
        ? `<th class="${esc(h.cls || "")}" aria-sort="${state.col === i ? (state.dir === "asc" ? "ascending" : "descending") : "none"}"><button type="button" class="th-sort" data-table="${esc(opts.id)}" data-col="${i}" aria-label="${esc(h.t)} 정렬">${esc(h.t)}${state.col === i ? (state.dir === "asc" ? " ▴" : " ▾") : ""}</button></th>`
        : `<th class="${esc(h.cls || "")}">${esc(h.t)}</th>`,
    )
    .join("");
  const body = items
    .map(
      (r) =>
        `<tr>${r.cells
          .map((c, i) => {
            const d = defs[i] || {};
            const cls = [d.cls, d.t ? "" : "bare"].filter(Boolean).join(" ");
            return `<td${cls ? ` class="${esc(cls)}"` : ""} data-label="${esc(d.t || "")}">${c}</td>`;
          })
          .join("")}</tr>`,
    )
    .join("");
  if (!items.length) return '<div class="empty">표시할 항목이 없습니다.</div>';
  return `<table class="${esc(opts.cls || "")}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
// rows entries for sortable tables: {item: rawRow, cells: [...]}. For plain
// tables pass cell arrays directly — they are wrapped for uniformity.
function rowsOf(items, cellFn) {
  return items.map((item) => ({ item, cells: cellFn(item) }));
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".th-sort");
  if (!btn) return;
  const id = btn.dataset.table,
    col = Number(btn.dataset.col);
  const state = (tableSorts[id] ||= {});
  if (state.col === col) state.dir = state.dir === "asc" ? "desc" : "asc";
  else {
    state.col = col;
    state.dir = "asc";
  }
  render();
  syncHash();
});
function preserveViewState(root, render) {
  const openDetails = new Set(
    [...root.querySelectorAll("details[data-key]")]
      .filter((d) => d.open)
      .map((d) => d.dataset.key),
  );
  const scrollHosts = root.classList.contains("table-wrap") ? [root] : [];
  scrollHosts.push(...root.querySelectorAll(".table-wrap"));
  const scrolls = scrollHosts.map((el) => el.scrollLeft);
  render();
  root.querySelectorAll("details[data-key]").forEach((d) => {
    if (openDetails.has(d.dataset.key)) d.open = true;
  });
  scrollHosts.forEach((el, i) => (el.scrollLeft = scrolls[i]));
}
function bindServiceButtons(root) {
  root
    .querySelectorAll("[data-manage]")
    .forEach((b) =>
      b.addEventListener("click", () => manageService(b.dataset.manage)),
    );
  root
    .querySelectorAll("[data-navigate]")
    .forEach((b) =>
      b.addEventListener("click", () => navigate(b.dataset.navigate)),
    );
  root
    .querySelectorAll("[data-quick]")
    .forEach((b) =>
      b.addEventListener("click", () =>
        quickServiceAction(b.dataset.quick, b.dataset.quickAction, b),
      ),
    );
}
// Inline actions arm on the first tap and run on a second tap within 3 s,
// replacing a modal confirm for low-risk app start/restart (D3). The armed
// state is keyed by service+action, not the button element, so a refresh or
// resize re-render in between keeps it.
const quickArmed = new Map();
const QUICK_ARMED_LABEL = "한 번 더 눌러 확인";
const isArmed = (id, action) => (quickArmed.get(`${id}:${action}`) || 0) > Date.now();
async function quickServiceAction(id, action, btn) {
  const row = serviceData.services.find((s) => s.id === id);
  if (!row || busyAction) return;
  const key = `${id}:${action}`;
  if (!isArmed(id, action)) {
    quickArmed.set(key, Date.now() + 3000);
    btn.textContent = QUICK_ARMED_LABEL;
    btn.classList.add("armed");
    setTimeout(() => {
      if (quickArmed.get(key) > Date.now()) return;
      quickArmed.delete(key);
      if (currentView === "services") render();
    }, 3100);
    return;
  }
  quickArmed.delete(key);
  btn.classList.remove("armed");
  busyAction = true;
  try {
    const data = await api(
      `/api/services/${encodeURIComponent(id)}/action`,
      { action, confirmed: true },
    );
    notify(data.message, true);
  } catch (error) {
    notify(error.message);
  } finally {
    busyAction = false;
    await refresh(true);
    await settleService(id);
  }
}
function safeLink(url) {
  try {
    const u = new URL(url);
    return ["https:", "http:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? esc(u.href)
      : "";
  } catch {
    return "";
  }
}
const clampPercent = (v) => Math.max(0, Math.min(100, Number(v) || 0));
// The strict CSP forbids style= in markup; size bars and place ticks here.
function paintBars(root) {
  root.querySelectorAll("[data-percent]").forEach((el) => (el.style.width = el.dataset.percent + "%"));
  root.querySelectorAll("[data-mark]").forEach((el) => (el.style.left = el.dataset.mark + "%"));
}
// Meter fill for a value judged against a threshold: the threshold sits at
// THRESHOLD_MARK percent of the bar so small percentages (PSI 2%, swap-out
// MiB/min) are readable, and anything past the threshold visibly overshoots.
const THRESHOLD_MARK = 60;
const thresholdPercent = (value, threshold) =>
  threshold ? clampPercent((Number(value || 0) / threshold) * THRESHOLD_MARK) : null;
// Last rendered value per card label: a changed number flashes once
// (motion-safe only, see style.css) so updates are noticed.
const lastCardValues = {};
function metricCard(label, value, unit, caption, percent, kind = "", tone = "", mark = null, extra = {}) {
  // A null percent omits the meter entirely so empty values never look like 0%.
  const marker =
    mark == null ? "" : `<i class="meter-mark" data-mark="${clampPercent(mark)}" aria-hidden="true"></i>`;
  const meter =
    percent == null
      ? ""
      : `<div class="meter"><span data-percent="${clampPercent(percent)}"></span>${marker}</div>`;
  // Only judged cards carry a status dot; neutral cards show no icon (C2).
  const dot = ["warn", "bad"].includes(tone)
    ? `<span class="metric-dot ${tone}" role="img" aria-label="${tone === "bad" ? "위험" : "주의"}"></span>`
    : "";
  const changed = label in lastCardValues && lastCardValues[label] !== String(value);
  lastCardValues[label] = String(value);
  const inner = `<div class="metric-label">${label}${dot}</div><div class="metric-value${changed ? " flash" : ""}">${value} <small>${unit}</small></div>${extra.trend || ""}${meter}<div class="metric-caption">${caption}</div>`;
  // Cards with a detail view are links: the whole card is the target.
  return extra.href
    ? `<a class="metric link ${kind} ${tone}" href="#${esc(extra.href)}" aria-label="${esc(label)} 자세히 보기">${inner}</a>`
    : `<div class="metric ${kind} ${tone}">${inner}</div>`;
}
// 3-hour trend under an overview card. Scaled to its own min..max because
// the direction matters here; the tooltip carries the actual range.
function cardTrend(values, format) {
  const pts = values.map(finite);
  const real = pts.filter((v) => v != null);
  if (real.length < 2) return "";
  const w = 120,
    h = 22,
    lo = Math.min(...real),
    hi = Math.max(...real),
    span = hi - lo || 1;
  let d = "",
    open = false;
  pts.forEach((v, i) => {
    if (v == null) return void (open = false);
    d += `${open ? "L" : "M"}${((i / (pts.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - lo) / span) * (h - 4)).toFixed(1)} `;
    open = true;
  });
  const range = `최근 3시간 ${format(lo)} ~ ${format(hi)}`;
  return `<svg class="card-trend" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(range)}"><title>${esc(range)}</title><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}
const CHANNEL_LABELS = {
  push: "푸시",
  windows_toast: "Windows 토스트",
  gmail: "Gmail",
  discord: "Discord",
  webhook: "Webhook",
};
function channelChip(name, status, error) {
  const label = CHANNEL_LABELS[name] || name;
  if (error) return `<span class="pill bad">${esc(label)} 오류</span>`;
  if (status === "enabled" || status === "configured")
    return `<span class="pill good">${esc(label)} 사용</span>`;
  if (status === "disabled") return `<span class="pill">${esc(label)} 꺼짐</span>`;
  if (status === "unsubscribed") return `<span class="pill" title="설정·진단에서 이 기기를 등록하세요">${esc(label)} 미등록</span>`;
  return `<span class="pill warn">${esc(label)} 설정 안 됨</span>`;
}
function renderOverview() {
  if (!monitorData) {
    $("view-overview").innerHTML = loaded.monitor
      ? '<div class="empty">자원 상태를 받지 못했습니다. 잠시 후 자동으로 다시 시도합니다.</div>'
      : '<div class="empty">불러오는 중…</div>';
    return;
  }
  const d = monitorData,
    m = d.metrics,
    available = m.mem_available_kib,
    swap = m.swap_total_kib - m.swap_free_kib,
    stale = observationIsStale(d.daemon_updated_at);
  const staleMin =
    stale && d.daemon_updated_at
      ? Math.max(1, Math.floor((Date.now() / 1000 - d.daemon_updated_at) / 60))
      : 0;
  const staleNote = stale ? ` · ${staleMin || "?"}분 전 값` : "";
  const th = d.thresholds || {};
  const ramTone =
    th.critical_available_gib != null &&
    available <= th.critical_available_gib * 1048576
      ? "bad"
      : th.warning_available_gib != null &&
          available <= th.warning_available_gib * 1048576
        ? "warn"
        : "good";
  const psiTone =
    th.critical_psi_full_avg60 != null &&
    m.psi_full_avg60 != null && m.psi_full_avg60 >= th.critical_psi_full_avg60
      ? "bad"
      : th.warning_psi_some_avg60 != null &&
          m.psi_some_avg60 != null && m.psi_some_avg60 >= th.warning_psi_some_avg60
        ? "warn"
        : m.psi_some_avg60 == null || m.psi_full_avg60 == null ? "" : "good";
  const apps = serviceData.services.filter((s) => s.category === "app");
  const activeApps = apps.filter((s) => s.state === "active").length;
  // Only apps needing attention are listed; healthy apps stay out of the way.
  const serviceProblems = appProblems();
  const warnings = [...d.reasons, ...d.pending_reasons];
  const channelErrors = Object.entries(d.alerts.channel_errors || {});
  // Windows drives within 72 h of a threshold move to the alert banner (C2).
  const urgentDisks = (diskData ? diskData.disks : []).filter(
    (disk) =>
      disk.kind === "windows" &&
      disk.insight &&
      disk.insight.hours_to_critical != null &&
      disk.insight.hours_to_critical <= 72,
  );
  const alertItems = [
    ...d.reasons.map((r) => `<div class="alert-item">${esc(r)}</div>`),
    ...d.pending_reasons.map(
      (r) => `<div class="alert-item muted">판정 대기: ${esc(r)}</div>`,
    ),
    ...urgentDisks.map((disk) => {
      const info = disk.insight;
      const level = info.hours_to_warning != null ? "경고" : "위험";
      const hours = info.hours_to_warning ?? info.hours_to_critical;
      return `<div class="alert-item">${esc(disk.name)} ${level}까지 약 ${Number(hours).toFixed(0)}시간 · 최근 ${Number(info.recent_gib_per_hour).toFixed(1)} GiB/시간 소모<span class="alert-hint">PC 터미널에서 docker system df 로 원인을 확인하세요</span></div>`;
    }),
  ];
  const snoozeUntil = liveSummary?.snooze_until || d.snooze_until || 0;
  const snoozeText = snoozeUntil > Date.now() / 1000 ? `재알림 일시 정지 중 · ${shortWhen(snoozeUntil)}까지` : "";
  const alertActions = [
    d.severity !== "normal" || snoozeText
      ? `<button class="button secondary" data-snooze>${snoozeText ? "일시 정지 변경" : "재알림 일시 정지"}</button>`
      : "",
    urgentDisks.length
      ? '<button class="button secondary" data-navigate="disks">Disk 상세 →</button>'
      : "",
    warnings.some((r) => /RAM|압력|PSI/i.test(r))
      ? '<button class="button secondary" data-navigate="top">Top 보기 →</button>'
      : "",
  ]
    .filter(Boolean)
    .join("");
  const alertBanner =
    stale || d.severity !== "normal" || urgentDisks.length
      ? `<div class="alert-banner ${stale || d.severity !== "critical" ? "warn" : "bad"}" id="overview-alert"><div class="alert-body"><div class="alert-title">${stale ? "Guard의 최근 수집 상태를 확인해 주세요" : "지금 확인이 필요합니다"}</div>${alertItems.join("")}${snoozeText ? `<div class="alert-item snoozed">${esc(snoozeText)} — 새 경보와 상태 변화는 계속 알립니다</div>` : ""}</div>${alertActions ? `<div class="alert-actions">${alertActions}</div>` : ""}</div>`
      : "";
  const suggestions = [];
  if (diskData)
    for (const disk of diskData.disks) {
      const info = disk.insight || {};
      if (info.vhd_reclaim_gib)
        suggestions.push({
          html: `VHDX 정리로 약 ${Number(info.vhd_reclaim_gib).toFixed(0)} GiB 회수 가능 <button class="button compact secondary" data-vhd-guide>정리 방법</button>`,
        });
      else if (
        disk.kind === "windows" &&
        !urgentDisks.includes(disk)
      ) {
        const days = info.days_to_warning ?? info.days_to_critical;
        const limit = info.days_to_warning != null ? "경고" : "위험";
        if (days != null && days < 30)
          suggestions.push(
            `${disk.name} ${limit}까지 약 ${Number(days).toFixed(0)}일 · 소모 ${Number(info.gib_per_day).toFixed(1)} GiB/일`,
          );
      }
    }
  const staleSessions = (d.sessions || []).filter((s) => s.stale);
  if (staleSessions.length)
    suggestions.push({
      html: `오래된 LLM 세션 ${staleSessions.length}개 종료 시 약 ${giB(staleSessions.reduce((t, s) => t + s.rss_kib, 0))} GiB 회수 <a class="button compact secondary" href="#sessions?stale=1">세션 보기</a>`,
    });
  for (const leak of d.leaks || [])
    suggestions.push(
      `${leak.name} 메모리가 ${leak.hours}시간 동안 ${(leak.from_bytes / 2 ** 30).toFixed(1)}→${(leak.to_bytes / 2 ** 30).toFixed(1)} GiB로 지속 증가 — 재시작을 고려하세요`,
    );
  const channelErrorsMap = d.alerts.channel_errors || {};
  const channelChips = Object.entries(d.alerts.channels)
    .map(([k, v]) => channelChip(k, v, channelErrorsMap[k]))
    .join(" ");
  // Aggregate sessions by project so one project never appears twice (U12).
  const projectTotals = new Map();
  for (const r of d.top) {
    const total =
      projectTotals.get(r.project) ||
      { project: r.project, providers: new Set(), rss_kib: 0, swap_kib: 0, mcp_count: 0 };
    total.providers.add(r.provider);
    total.rss_kib += r.rss_kib;
    total.swap_kib += r.swap_kib;
    total.mcp_count += r.mcp_count;
    projectTotals.set(r.project, total);
  }
  const topProjects = [...projectTotals.values()]
    .sort((a, b) => b.rss_kib - a.rss_kib)
    .slice(0, 5);
  const recent = (overviewHistory || []).filter((r) => r.metrics);
  const series = (fn) => recent.map((r) => fn(r.metrics));
  const gib = (v) => `${Number(v).toFixed(1)} GiB`;
  const trends = {
    ram: cardTrend(series((x) => x.mem_available_kib / 1048576), gib),
    swap: cardTrend(series((x) => x.swap_out_mib_per_minute), (v) => `${Number(v).toFixed(0)} MiB/분`),
    psi: cardTrend(series((x) => x.psi_some_avg60), (v) => `${Number(v).toFixed(2)}%`),
    vmmem: cardTrend(series((x) => (x.vmmem_bytes == null ? null : x.vmmem_bytes / 2 ** 30)), gib),
  };
  $("view-overview").innerHTML =
    `${alertBanner}<div class="cards${stale ? " stale" : ""}">${metricCard("사용 가능한 RAM", giB(available), "GiB", `전체 ${giB(m.mem_total_kib)} GiB 중 사용 가능${staleNote}`, (available / m.mem_total_kib) * 100, "", ramTone, th.warning_available_gib != null ? ((th.warning_available_gib * 1048576) / m.mem_total_kib) * 100 : null, { href: "history", trend: trends.ram })}${metricCard("사용 중인 Swap", giB(swap), "GiB", `전체 ${giB(m.swap_total_kib)} GiB · in ${Number(m.swap_in_mib_per_minute || 0).toFixed(1)} / out ${Number(m.swap_out_mib_per_minute || 0).toFixed(1)} MiB/분 · 막대는 swap-out 속도${staleNote}`, thresholdPercent(m.swap_out_mib_per_minute, th.warning_swap_out_mib_per_minute), "swap", "", th.warning_swap_out_mib_per_minute ? THRESHOLD_MARK : null, { href: "history", trend: trends.swap })}${metricCard("메모리 대기 압력 · PSI", m.psi_some_avg60 == null ? "관측 불가" : Number(m.psi_some_avg60).toFixed(2), m.psi_some_avg60 == null ? "" : "%", `some / full ${psiValue(m.psi_full_avg60)} · ${psiStatus(m)} · 최근 60초 · 표시선은 경고 ${th.warning_psi_some_avg60 ?? "—"}%${staleNote}`, m.psi_some_avg60 == null ? null : thresholdPercent(m.psi_some_avg60, th.warning_psi_some_avg60), "", psiTone, th.warning_psi_some_avg60 ? THRESHOLD_MARK : null, { href: "history", trend: trends.psi })}${metricCard("호스트 WSL 메모리 (vmmem)", m.vmmem_bytes != null ? (m.vmmem_bytes / 2 ** 30).toFixed(2) : "—", m.vmmem_bytes != null ? "GiB" : "", m.vmmem_bytes != null ? `호스트 관측 ${when(m.vmmem_observed_at)}${staleNote}` : "호스트 관측 불가", m.vmmem_bytes != null ? (m.vmmem_bytes / Math.max(1, m.mem_total_kib * 1024)) * 100 : null, "", "", null, { href: "history", trend: trends.vmmem })}${metricCard("LLM 세션", num(d.sessions.length), "개", `MCP ${num(d.mcp_count)}개 · ${giB(d.mcp_rss_kib)} GiB${staleSessions.length ? ` · 오래된 세션 ${staleSessions.length}개` : ""}${staleNote}`, null, "", "", null, { href: "sessions" })}</div>
<div class="split"><div class="panel" id="overview-status"><div class="panel-heading"><h2>감시·알림 상태</h2>${pill(stale ? "unknown" : d.severity)}</div><div class="status-strip"><strong>${stale ? "Guard의 최근 수집 상태를 확인해 주세요." : d.severity === "normal" ? "자원 상태가 안정적입니다." : "자원 경보가 감지됐습니다."}</strong><span class="muted">마지막 수집 ${when(d.daemon_updated_at)}</span></div><div class="panel-body">${alertBanner ? '<p class="health-description">경보 원인은 위 배너에 표시됩니다.</p>' : warnings.length ? warnings.map((s) => `<div class="reason">${esc(s)}</div>`).join("") : '<p class="health-description">현재 지속 중인 경보 원인이 없습니다. RAM, swap 입출력, 메모리 대기 압력을 함께 감시합니다.</p>'}${d.observations.length ? `<div class="health-description">관찰 항목<br>${d.observations.map(esc).join("<br>")}</div>` : ""}<div class="health-description channel-chips">알림 ${channelChips}<br>정기 이메일 ${d.alerts.email_heartbeat_enabled ? "켜짐" : "꺼짐"} · 마지막 경보 ${when(d.alerts.last_alert)}${snoozeText ? ` · ${esc(snoozeText)}` : ""}${channelErrors.length ? `<br><span class="row-error">알림 채널 실패: ${channelErrors.map(([k, v]) => `${esc(CHANNEL_LABELS[k] || k)} — ${esc(v)}`).join(" · ")}</span>` : ""}</div></div></div>
<div class="panel" id="overview-services"><div class="panel-heading"><h2>서비스 요약 <span class="badge-count">${activeApps} / ${apps.length} 실행</span></h2><button class="button secondary" data-navigate="services">모두 보기 →</button></div><div class="mini-list">${
      serviceProblems
        .map(
          (s) =>
            `<div class="mini-row"><div>${esc(s.name)}<small>자동 실행 ${s.autostart ? "켜짐" : "꺼짐"}</small>${s.error ? `<div class="row-error">${esc(s.error)}</div>` : ""}${s.state === "active" && s.health && !s.health.ok ? `<div class="row-error">웹 응답 없음 · ${esc(s.health.error || "")}</div>` : ""}</div>${pill(s.state)}</div>`,
        )
        .join("") || '<div class="empty">모든 앱이 설정대로 동작 중입니다</div>'
    }</div></div></div>
<div class="panel" id="overview-top"><div class="panel-heading"><h2>메모리를 많이 사용하는 프로젝트</h2><button class="button secondary" data-navigate="top">Top 보기 →</button></div><div class="table-wrap">${table(
      [{ t: "프로젝트" }, { t: "도구" }, { t: "RAM", cls: "num" }, { t: "Swap", cls: "num" }, { t: "MCP", cls: "num" }],
      topProjects.map((r) => [
        `<strong>${esc(r.project)}</strong>`,
        esc([...r.providers].join(", ")),
        `${giB(r.rss_kib)} GiB`,
        `${giB(r.swap_kib)} GiB`,
        num(r.mcp_count),
      ]),
    )}</div></div>${suggestions.length ? `<div class="panel" id="overview-suggestions"><div class="panel-heading"><h2>정리 제안</h2><span class="muted">자동으로 변경하지 않습니다</span></div><div class="panel-body">${suggestions.map((s) => `<div class="reason">${typeof s === "string" ? esc(s) : s.html}</div>`).join("")}</div></div>` : ""}`;
  $("view-overview").querySelector(".cards").insertAdjacentHTML("afterend", diskOverview());
  $("view-overview")
    .querySelector(".disk-more")
    ?.addEventListener("click", () => {
      diskOverviewExpanded = !diskOverviewExpanded;
      renderOverview();
    });
  paintBars($("view-overview"));
  bindServiceButtons($("view-overview"));
}
function sparkline(records, id) {
  const pts = records.filter((r) => r.services[id] != null);
  if (pts.length < 2) return "";
  const w = 80,
    h = 26;
  const t0 = pts[0].timestamp,
    span = Math.max(1, pts[pts.length - 1].timestamp - t0);
  const values = pts.map((r) => r.services[id]);
  const max = Math.max(1, ...values);
  const d = pts
    .map(
      (r, i) =>
        `${i ? "L" : "M"}${(((r.timestamp - t0) / span) * w).toFixed(1)},${(h - 3 - (r.services[id] / max) * (h - 6)).toFixed(1)}`,
    )
    .join(" ");
  // The line is drawn from a zero baseline; the tooltip gives the real range
  // so a small service's few-MiB wobble is not read as a crash.
  const range = `24시간 최소 ${memory(Math.min(...values))} · 최대 ${memory(max)}`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(range)}"><title>${esc(range)}</title><line class="spark-base" x1="0" x2="${w}" y1="${h - 3}" y2="${h - 3}"/><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.5"/></svg>`;
}
// Memory against the unit's systemd MemoryHigh/MemoryMax: past High the
// kernel throttles and reclaims, so the gauge turns amber there.
function memoryLimit(s) {
  const limit = s.memory_max ?? s.memory_high;
  if (!limit || s.memory_bytes == null) return "";
  const used = s.memory_bytes,
    tone = s.memory_max && used >= s.memory_max * 0.95 ? "bad" : s.memory_high && used >= s.memory_high * 0.95 ? "warn" : "";
  const parts = [
    s.memory_high ? `High ${memory(s.memory_high)}` : "",
    s.memory_max ? `Max ${memory(s.memory_max)}` : "",
  ].filter(Boolean);
  const mark = s.memory_high && s.memory_max ? `<i class="meter-mark" data-mark="${clampPercent((s.memory_high / s.memory_max) * 100)}"></i>` : "";
  return `<div class="limit ${tone}" title="systemd 메모리 한도"><div class="meter"><span data-percent="${clampPercent((used / limit) * 100)}"></span>${mark}</div><span class="subline">${esc(parts.join(" · "))}</span></div>`;
}
function healthPill(h) {
  if (!h) return "";
  const when_ = `확인 ${when(h.checked_at)}`;
  return h.ok
    ? `<span class="pill good health" title="${esc(`HTTP ${h.status} · ${h.latency_ms ?? "?"}ms · ${when_}`)}">응답 정상</span>`
    : `<span class="pill bad health" title="${esc(`${h.error || "응답 없음"} · ${when_}`)}">응답 없음</span>`;
}
function renderServices() {
  if (!loaded.services) {
    $("services-list").innerHTML = '<div class="empty">불러오는 중…</div>';
    return;
  }
  let html = "";
  const memRecords = (serviceMemoryData || []).filter(
    (r) => r.timestamp && r.services,
  );
  // Column widths come from .services-table th:nth-child rules in style.css.
  const serviceColumns = [
    { t: "서비스", key: (s) => String(s.name) },
    { t: "자동 실행", key: (s) => (s.autostart ? 1 : 0) },
    { t: "현재 상태", key: (s) => String(s.state) },
    { t: "메모리", cls: "num", key: (s) => s.memory_bytes ?? -1 },
    { t: "" },
  ];
  for (const [category, label] of [
    ["app", "앱 서비스"],
    ["foundation", "기반 서비스"],
  ]) {
    const rows = serviceData.services.filter((s) => s.category === category);
    html += `<div class="section-label">${label}<span>${rows.length}</span></div><div class="panel table-wrap">${table(
      serviceColumns,
      rowsOf(rows, (s) => {
        const link = safeLink(s.url);
        const quick =
          s.category === "app"
            ? s.state === "active"
              ? `<button class="button compact secondary${isArmed(s.id, "restart") ? " armed" : ""}" data-quick="${esc(s.id)}" data-quick-action="restart" title="3초 안에 두 번 눌러 재시작">${isArmed(s.id, "restart") ? QUICK_ARMED_LABEL : "↻ 재시작"}</button>`
              : `<button class="button compact secondary${isArmed(s.id, "start") ? " armed" : ""}" data-quick="${esc(s.id)}" data-quick-action="start" title="3초 안에 두 번 눌러 시작">${isArmed(s.id, "start") ? QUICK_ARMED_LABEL : "▶ 시작"}</button>`
            : "";
        return [
          `<strong>${link ? `<a href="${link}" target="_blank" rel="noopener noreferrer">${esc(s.name)} ↗</a>` : esc(s.name)}</strong><span class="subline">${esc(s.kind === "compose" ? "Docker Compose" : s.target)}</span>`,
          `<span class="pill ${s.autostart ? "good" : ""}">${s.autostart ? "켜짐" : "꺼짐"}</span>${s.drift ? ' <span class="pill warn">설정 불일치</span>' : ""}`,
          `${pill(s.state)} ${s.state === "active" ? healthPill(s.health) : ""}<span class="subline">${esc(s.detail)}</span>${s.error ? `<div class="row-error">${esc(s.error)}</div>` : ""}${s.state === "active" && s.health && !s.health.ok ? `<div class="row-error">웹 응답 확인 실패: ${esc(s.health.error || "")}</div>` : ""}`,
          `${memory(s.memory_bytes)}${sparkline(memRecords, s.id)}${memoryLimit(s)}`,
          `<div class="row-actions">${quick}<button class="button secondary" data-manage="${esc(s.id)}">관리 ⋯</button></div>`,
        ];
      }),
      true,
      { id: `services-${category}`, cls: "services-table" },
    )}</div>`;
  }
  $("services-list").innerHTML = html;
  paintBars($("services-list"));
  bindServiceButtons($("services-list"));
  const chartEl = $("service-memory-chart");
  if (chartEl) {
    const rows = memRecords.map((r) => ({ timestamp: r.timestamp, metrics: r.services }));
    // Rank services by their latest sample; only the top 4 start visible (U10).
    const latest = {};
    memRecords.forEach((r) => Object.assign(latest, r.services));
    const ids = serviceData.services
      .map((s) => s.id)
      .filter((id) => memRecords.some((r) => id in r.services))
      .sort((a, b) => (latest[b] || 0) - (latest[a] || 0));
    chartEl.innerHTML =
      memRecords.length >= 2 && ids.length
        ? lineChart(
            rows,
            "서비스 메모리 추세",
            "GiB",
            ids.map((id, i) => ({
              name: (serviceData.services.find((s) => s.id === id) || {}).name || id,
              color: `var(--chart-${(i % 8) + 1})`,
              hidden: i >= 4,
              value: (metrics) => (metrics[id] == null ? null : metrics[id] / 1073741824),
            })),
            [],
            { scaleToggle: true },
          )
        : '<div class="panel"><div class="panel-body"><p class="muted">등록된 서비스의 메모리 사용량을 1분마다 수집합니다. 데이터가 쌓이면 추세가 표시됩니다.</p></div></div>';
    bindCharts(chartEl);
  }
}
function searchable(row, fields) {
  // Leaf values only: field names like "pid" or "rss" must not match every row.
  // When `fields` is given, only those visible columns participate (U11).
  const parts = [];
  const walk = (v) => {
    if (v == null) return;
    if (Array.isArray(v)) v.forEach(walk);
    else if (typeof v === "object") Object.values(v).forEach(walk);
    else parts.push(String(v));
  };
  if (fields) fields.forEach((f) => walk(row[f]));
  else walk(row);
  return parts.join(" ").toLowerCase();
}
function matches(row, term, fields) {
  return searchable(row, fields).includes(term.trim().toLowerCase());
}
function renderTop() {
  if (!monitorData) {
    $("top-table").innerHTML = `<div class="empty">${loaded.monitor ? "자원 상태를 받지 못했습니다." : "불러오는 중…"}</div>`;
    return;
  }
  const rows = monitorData.top.filter((r) =>
    matches(r, $("top-search").value, ["project", "provider", "root_pid"]),
  );
  $("top-table").innerHTML = table(
    [
      { t: "프로젝트", key: (r) => String(r.project) },
      { t: "도구", key: (r) => String(r.provider) },
      { t: "PID", cls: "num", key: (r) => r.root_pid },
      { t: "RAM", cls: "num", key: (r) => r.rss_kib },
      { t: "Swap", cls: "num", key: (r) => r.swap_kib },
      { t: "CPU", cls: "num", key: (r) => r.cpu_percent || 0 },
      { t: "프로세스", cls: "num", key: (r) => r.process_count },
      { t: "MCP", cls: "num", key: (r) => r.mcp_count },
    ],
    rowsOf(rows, (r) => [
      `<strong>${esc(r.project)}</strong>`,
      esc(r.provider),
      num(r.root_pid),
      `${giB(r.rss_kib)} GiB`,
      `${giB(r.swap_kib)} GiB`,
      `${Number(r.cpu_percent || 0).toFixed(1)}%`,
      num(r.process_count),
      num(r.mcp_count),
    ]),
    true,
    { id: "top" },
  );
  const other = $("top-other");
  if (other)
    other.innerHTML = table(
      ["프로세스", { t: "PID", cls: "num" }, { t: "RAM", cls: "num" }, { t: "Swap", cls: "num" }, "실행 시간"],
      (monitorData.top_other || []).map((p) => [
        `<strong>${esc(p.name)}</strong>`,
        num(p.pid),
        `${giB(p.rss_kib)} GiB`,
        `${giB(p.swap_kib)} GiB`,
        ageCell(p.age_seconds),
      ]),
    );
}
function renderSessions() {
  if (!monitorData) {
    $("sessions-table").innerHTML = `<div class="empty">${loaded.monitor ? "자원 상태를 받지 못했습니다." : "불러오는 중…"}</div>`;
    return;
  }
  const rows = monitorData.sessions.filter(
    (r) =>
      matches(r, $("session-search").value, [
        "project",
        "provider",
        "root_pid",
        "cgroup",
      ]) &&
      (!$("session-stale").checked || r.stale),
  );
  const stale = monitorData.sessions.filter((r) => r.stale && r.killable !== false);
  const bulk = $("kill-stale");
  bulk.classList.toggle("hidden", !stale.length);
  bulk.textContent = `오래된 세션 ${stale.length}개 종료`;
  preserveViewState($("sessions-table"), () => {
    $("sessions-table").innerHTML = table(
    [
      { t: "세션 / 프로젝트", key: (r) => String(r.project) },
      { t: "도구", key: (r) => String(r.provider) },
      { t: "실행 시간", cls: "num", key: (r) => r.age_seconds },
      { t: "최근 자식 실행 시간", cls: "num", key: (r) => r.youngest_process_age_seconds },
      { t: "RAM", cls: "num", key: (r) => r.rss_kib },
      { t: "Swap", cls: "num", key: (r) => r.swap_kib },
      { t: "CPU", cls: "num", key: (r) => r.cpu_percent || 0 },
      { t: "프로세스 / MCP", cls: "num", key: (r) => r.process_count },
      "",
    ],
    rowsOf(rows, (r) => {
      const limits = r.limits && Object.keys(r.limits).length ? r.limits : null;
      const limitText = limits
        ? Object.entries({
            memory_high: "high",
            memory_max: "max",
            swap_max: "swap",
            tasks_max: "tasks",
          })
            .filter(([k]) => k in limits)
            .map(
              ([k, label]) =>
                `${label}=${limits[k] == null ? "max" : k === "tasks_max" ? limits[k] : (limits[k] / 1073741824).toFixed(0) + "G"}`,
            )
            .join(" ")
        : "";
      return [
        `<strong>${esc(r.project)}</strong>${limits ? ' <span class="pill warn" title="' + esc(limitText) + '">제한</span>' : ""}<span class="subline">PID ${num(r.root_pid)}</span><details class="details" data-key="${Number(r.root_pid)}"><summary>프로젝트 구성</summary><div>${r.projects.map((p) => `${esc(p.project)} · ${giB(p.rss_kib)} GiB · CPU ${Number(p.cpu_percent || 0).toFixed(1)}% · MCP ${num(p.mcp_count)}`).join("<br>")}<br>${esc(r.cgroup)}${limits ? "<br>제한: " + esc(limitText) : ""}</div></details>`,
        esc(r.provider),
        ageCell(r.age_seconds),
        ageCell(r.youngest_process_age_seconds),
        `${giB(r.rss_kib)} GiB`,
        `${giB(r.swap_kib)} GiB`,
        `${Number(r.cpu_percent || 0).toFixed(1)}%`,
        `${num(r.process_count)} / ${num(r.mcp_count)}`,
        r.killable === false
          ? `<button class="button compact danger" disabled title="${esc(r.kill_block_reason || "종료할 수 없습니다.")}">종료</button>`
          : `<button class="button compact danger" data-kill="${Number(r.root_pid)}" data-label="${esc(r.project)} 세션">종료</button>`,
      ];
    }),
    true,
    { id: "sessions" },
    );
  });
  bindKillButtons($("sessions-table"));
  renderSessionHistory();
}
function renderSessionHistory() {
  const host = $("session-history");
  if (!sessionHistoryData) {
    host.innerHTML = '<div class="empty">불러오는 중…</div>';
    return;
  }
  const term = $("session-search").value;
  const rows = sessionHistoryData.filter((r) => matches(r, term, ["project", "provider", "root_pid", "root_name"]));
  host.innerHTML = table(
    [
      { t: "상태", key: (r) => (r.ended ? 1 : 0) },
      { t: "세션 / 프로젝트", key: (r) => String(r.project) },
      { t: "도구", key: (r) => String(r.provider) },
      { t: "시작", key: (r) => r.started_at },
      { t: "마지막 관측", key: (r) => r.last_seen },
      { t: "실행 시간", cls: "num", key: (r) => r.duration_seconds },
      { t: "최대 RAM", cls: "num", key: (r) => r.peak_rss_kib },
      { t: "최대 MCP", cls: "num", key: (r) => r.peak_mcp_count },
    ],
    rowsOf(rows, (r) => [
      r.ended ? '<span class="pill">종료</span>' : '<span class="pill good">실행 중</span>',
      `<strong>${esc(r.project)}</strong><span class="subline">PID ${num(r.root_pid)}${r.persistent ? " · 상주 서버" : ""}</span>`,
      esc(r.provider),
      when(r.started_at),
      r.ended ? when(r.last_seen) : "지금",
      durationText(r.duration_seconds),
      `${giB(r.peak_rss_kib)} GiB`,
      num(r.peak_mcp_count),
    ]),
    true,
    { id: "session-history" },
  );
}
function renderMcp() {
  if (!monitorData) {
    $("mcp-table").innerHTML = `<div class="empty">${loaded.monitor ? "자원 상태를 받지 못했습니다." : "불러오는 중…"}</div>`;
    return;
  }
  const rows = monitorData.mcp.filter(
    (r) =>
      matches(r, $("mcp-search").value, [
        "root_name",
        "project",
        "provider",
        "root_pid",
        "session_root_pid",
      ]) &&
      r.age_seconds >= Math.max(0, Number($("mcp-hours").value) || 0) * 3600,
  );
  preserveViewState($("mcp-table"), () => {
    $("mcp-table").innerHTML = table(
      [
        { t: "MCP / 프로젝트", key: (r) => String(r.project) },
        { t: "도구", key: (r) => String(r.provider) },
        { t: "세션 PID", cls: "num", key: (r) => r.session_root_pid },
        { t: "실행 시간", cls: "num", key: (r) => r.age_seconds },
        { t: "RAM", cls: "num", key: (r) => r.rss_kib },
        { t: "Swap", cls: "num", key: (r) => r.swap_kib },
        { t: "CPU", cls: "num", key: (r) => r.cpu_percent || 0 },
        { t: "프로세스", cls: "num", key: (r) => r.process_count },
        "",
      ],
      rowsOf(rows, (r) => [
        `<strong>${esc(r.root_name)}</strong><span class="subline">${esc(r.project)} · PID ${num(r.root_pid)}</span>`,
        esc(r.provider),
        num(r.session_root_pid),
        ageCell(r.age_seconds),
        `${giB(r.rss_kib)} GiB`,
        `${giB(r.swap_kib)} GiB`,
        `${Number(r.cpu_percent || 0).toFixed(1)}%`,
        num(r.process_count),
        r.killable === false
          ? `<button class="button compact danger" disabled title="${esc(r.kill_block_reason || "종료할 수 없습니다.")}">종료</button>`
          : `<button class="button compact danger" data-kill="${Number(r.root_pid)}" data-label="${esc(r.root_name)}">종료</button>`,
      ]),
      true,
      { id: "mcp" },
    );
  });
  bindKillButtons($("mcp-table"));
}
let chartSeq = 0;
const chartDataPending = {};
// Legend on/off choices survive automatic refreshes; keyed "title|series".
const legendPrefs = {};
// Missing samples stay missing: a gap in the line, "—" in the tooltip, never 0.
const finite = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
function chartVal(v) {
  return v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
}
function chartMove(e) {
  const svg = e.currentTarget,
    info = svg._chart;
  if (!info || !info.rows.length) return;
  const { left, right, top, bottom, w, h, max } = info.geo,
    n = info.rows.length,
    ctm = svg.getScreenCTM();
  if (!ctm) return;
  const px = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse()).x;
  let i, gx;
  if (info.xAt) {
    // Index-based axis (e.g. daily disk history): pick the nearest point.
    i = 0;
    for (let k = 1; k < n; k++)
      if (Math.abs(info.xAt(k) - px) < Math.abs(info.xAt(i) - px)) i = k;
    gx = info.xAt(i);
  } else {
    const t = info.geo.t0 + ((px - left) / (w - left - right)) * info.geo.span;
    let lo = 0,
      hi = n - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (info.rows[mid].timestamp < t) lo = mid + 1;
      else hi = mid;
    }
    i = lo;
    if (i > 0 && t - info.rows[i - 1].timestamp < info.rows[i].timestamp - t) i = i - 1;
    gx = left + ((info.rows[i].timestamp - info.geo.t0) / info.geo.span) * (w - left - right);
  }
  const gy = info.yOf || ((v) => h - bottom - (Math.max(0, v) / max) * (h - top - bottom)),
    row = info.rows[i],
    cursor = svg.querySelector(".chart-cursor"),
    dots = svg.querySelectorAll(".chart-dot");
  cursor.setAttribute("x1", gx);
  cursor.setAttribute("x2", gx);
  cursor.setAttribute("visibility", "visible");
  const lines = info.series.map((s, k) => {
    const v = finite(s.value(row.metrics, i));
    if (dots[k]) {
      if (v != null) {
        dots[k].setAttribute("cx", gx);
        dots[k].setAttribute("cy", gy(v));
        dots[k].setAttribute("visibility", "visible");
      } else dots[k].setAttribute("visibility", "hidden");
    }
    const shown = s.display ? finite(s.display(row.metrics, i)) : v;
    return `<div><i data-color="${esc(s.color)}"></i>${esc(s.name)} <strong>${shown != null ? chartVal(shown) : info.psi ? "관측 불가" : "—"}</strong> ${shown == null && info.psi ? "" : esc(info.unit)}</div>`;
  });
  if (info.psi) lines.push(`<div>${esc(psiStatus(row.metrics))}</div>`);
  if (info.stacked) {
    const total = finite(info.series[info.series.length - 1].value(row.metrics, i));
    if (total != null) lines.push(`<div class="tip-total">합계 <strong>${chartVal(total)}</strong> ${esc(info.unit)}</div>`);
  }
  const tip = svg.parentElement.querySelector(".chart-tip"),
    host = svg.parentElement.getBoundingClientRect();
  const label = info.labelAt
    ? info.labelAt(i)
    : new Date(row.timestamp * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
  tip.innerHTML = `<div class="tip-time">${esc(label)}</div>${lines.join("")}`;
  // The strict CSP forbids style= attributes; paint the dots via CSSOM.
  tip.querySelectorAll("i[data-color]").forEach((el) => (el.style.background = el.dataset.color));
  tip.style.display = "block";
  const tw = tip.offsetWidth,
    th = tip.offsetHeight;
  let tx = e.clientX - host.left + 14,
    ty = e.clientY - host.top - th - 8;
  if (tx + tw > host.width - 6) tx = e.clientX - host.left - tw - 14;
  if (ty < 4) ty = e.clientY - host.top + 16;
  tip.style.left = tx + "px";
  tip.style.top = ty + "px";
}
function hideChartHover(svg) {
  const tip = svg.parentElement.querySelector(".chart-tip");
  if (tip) tip.style.display = "none";
  svg
    .querySelectorAll(".chart-cursor,.chart-dot")
    .forEach((el) => el.setAttribute("visibility", "hidden"));
}
function chartLeave(e) {
  // A touch fires pointerleave right after the tap; keep the tooltip until
  // the user taps elsewhere (see the document listener below).
  if (e.pointerType === "touch") return;
  hideChartHover(e.currentTarget);
}
// Touch screens have no hover: tapping outside a chart closes its tooltip.
document.addEventListener("pointerdown", (e) => {
  document.querySelectorAll("svg.chart").forEach((svg) => {
    if (!svg.contains(e.target)) hideChartHover(svg);
  });
});
function bindCharts(root) {
  root.querySelectorAll("svg.chart").forEach((svg) => {
    const info = chartDataPending[svg.dataset.chart];
    if (info) {
      svg._chart = info;
      delete chartDataPending[svg.dataset.chart];
    }
    svg.addEventListener("pointermove", chartMove);
    // A tap has no pointermove; show the values at the tapped point.
    svg.addEventListener("pointerdown", chartMove);
    svg.addEventListener("pointerleave", chartLeave);
    if (svg.classList.contains("zoomable")) svg.addEventListener("pointerdown", zoomStart);
  });
  // The strict CSP forbids style= in markup; paint the legend dots here.
  root.querySelectorAll(".legend-key i[data-color]").forEach(
    (el) => (el.style.background = el.dataset.color),
  );
  // Legend entries toggle their series on/off (U10).
  root.querySelectorAll(".legend-key[data-series]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const panel = btn.closest(".panel");
      const path = panel
        ? panel.querySelectorAll("svg.chart path.series-line")[Number(btn.dataset.series)]
        : null;
      if (!path) return;
      const off = path.getAttribute("visibility") === "hidden";
      path.setAttribute("visibility", off ? "visible" : "hidden");
      btn.classList.toggle("off", !off);
      const legend = btn.closest(".legend");
      // Keep the choice so the next refresh re-renders the same state.
      if (legend && btn.dataset.name != null)
        legendPrefs[`${legend.dataset.chart}|${btn.dataset.name}`] = off;
      // Keep the "표시 N/M" counter in sync when the legend has one.
      const counter = legend ? legend.querySelector(".legend-count") : null;
      if (counter) {
        const keys = [...legend.querySelectorAll(".legend-key")];
        counter.textContent = `표시 ${keys.filter((k) => !k.classList.contains("off")).length}/${keys.length}`;
      }
    }),
  );
}
// Per-chart axis choice ("log" | "linear"), remembered per browser.
const chartScales = (() => {
  try {
    return JSON.parse(localStorage.getItem("wrg-chart-scales") || "{}") || {};
  } catch {
    return {};
  }
})();
// Log axes floor at 1 MiB (in GiB) so idle services stay on the chart.
const LOG_FLOOR = 1 / 1024;
const logTick = (v) => (v >= 1 ? `${v}G` : `${Math.round(v * 1024)}M`);
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-chart-scale]");
  if (!btn) return;
  const title = btn.dataset.chartScale;
  chartScales[title] = chartScales[title] === "log" ? "linear" : "log";
  try {
    localStorage.setItem("wrg-chart-scales", JSON.stringify(chartScales));
  } catch {}
  render();
});
const psiValue = (value) => value == null ? "관측 불가" : `${Number(value).toFixed(2)}%`;
const psiStatus = (m) => ({ normal: "수집 정상", missing: "미제공", error: "수집 오류",
  partial: "일부 관측", legacy: "과거 수집 상태 미확인" })[m.psi_status || "legacy"] || "수집 상태 미확인";

function lineChart(rows, title, unit, series, bands = [], opts = {}) {
  const log = opts.scaleToggle && chartScales[title] === "log";
  // Keep the viewBox close to the rendered width so text stays readable on
  // narrow screens instead of shrinking with the scale factor (U2).
  const host = document.querySelector(".view:not(.hidden)");
  const w = Math.max(360, Math.min(900, Math.floor((host ? host.clientWidth : 948) - 48) || 900)),
    h = w < 560 ? 240 : 190,
    left = 48,
    right = 18,
    top = 16,
    bottom = 34;
  const seq = ++chartSeq;
  chartDataPending[seq] = {
    rows,
    series,
    unit: opts.stacked ? "GiB" : unit,
    stacked: !!opts.stacked,
    psi: !!opts.psi,
    geo: { left, right, top, bottom, w, h, max: 0 },
  };
  const max = Math.max(
    0.01,
    ...rows.flatMap((r, i) => series.map((s) => finite(s.value(r.metrics, i)) ?? 0)),
  );
  const t0 = rows[0].timestamp,
    t1 = rows[rows.length - 1].timestamp,
    span = Math.max(1, t1 - t0);
  chartDataPending[seq].geo.max = max;
  chartDataPending[seq].geo.t0 = t0;
  chartDataPending[seq].geo.span = span;
  const intervals = rows
    .slice(1)
    .map((r, i) => r.timestamp - rows[i].timestamp)
    .filter((d) => d > 0)
    .sort((a, b) => a - b);
  const step = intervals.length ? intervals[Math.floor(intervals.length / 2)] : 1;
  const gapAfter = (i) =>
    i + 1 < rows.length && rows[i + 1].timestamp - rows[i].timestamp > step * 2;
  const logLow = Math.log10(LOG_FLOOR),
    logHigh = Math.max(logLow + 1, Math.ceil(Math.log10(Math.max(max, LOG_FLOOR * 10))));
  const x = (t) => left + ((t - t0) / span) * (w - left - right),
    y = log
      ? (v) =>
          h - bottom - ((Math.log10(Math.max(v, LOG_FLOOR)) - logLow) / (logHigh - logLow)) * (h - top - bottom)
      : (v) => h - bottom - (v / max) * (h - top - bottom);
  chartDataPending[seq].yOf = y;
  const path = (fn) => {
    const parts = [];
    let open = false;
    rows.forEach((r, i) => {
      const v = finite(fn(r.metrics, i));
      if (v == null) {
        open = false;
        return;
      }
      parts.push(`${open ? "L" : "M"}${x(r.timestamp).toFixed(1)},${y(Math.max(0, v)).toFixed(1)}`);
      open = !gapAfter(i);
    });
    return parts.join(" ");
  };
  let grid = "";
  if (log)
    for (let e = logLow; e <= logHigh + 1e-9; e++) {
      const v = 10 ** e,
        yy = y(v);
      grid += `<line x1="${left}" x2="${w - right}" y1="${yy}" y2="${yy}" stroke="var(--line)"/><text x="${left - 9}" y="${yy + 4}" text-anchor="end">${logTick(Number(v.toPrecision(3)))}</text>`;
    }
  else
    for (let i = 0; i < 5; i++) {
      const v = (max * i) / 4,
        yy = y(v);
      grid += `<line x1="${left}" x2="${w - right}" y1="${yy}" y2="${yy}" stroke="var(--line)"/><text x="${left - 9}" y="${yy + 4}" text-anchor="end">${v < 10 ? v.toFixed(1) : v.toFixed(0)}</text>`;
    }
  for (let i = 0; i < 5; i++) {
    const t = t0 + (span * i) / 4;
    grid += `<text x="${x(t)}" y="${h - 9}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${esc(new Date(t * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false }))}</text>`;
  }
  // Alert bands shade the time ranges where severity was not normal (N1).
  const bandSvg = bands
    .map(
      (b) =>
        `<rect class="band-${b.sev === "critical" ? "critical" : "warning"}" x="${Math.max(left, x(b.start)).toFixed(1)}" y="${top}" width="${Math.max(1.5, Math.min(x(b.end), w - right) - Math.max(left, x(b.start))).toFixed(1)}" height="${h - top - bottom}"/>`,
    )
    .join("");
  // Restore legend on/off choices made before an automatic refresh.
  series.forEach((s) => {
    const key = `${title}|${s.name}`;
    if (key in legendPrefs) s.hidden = !legendPrefs[key];
  });
  const scaleButton = opts.scaleToggle
    ? `<button type="button" class="button compact secondary" data-chart-scale="${esc(title)}" aria-pressed="${log}" title="큰 값 하나가 축을 차지할 때 작은 서비스도 보이게 합니다">${log ? "선형 축" : "로그 축"}</button>`
    : "";
  // Stacked layers fill down to the layer below; toggling one off would
  // break the stack, so their legend is a plain key.
  const areas = opts.stacked
    ? series
        .map((s, k) => {
          const pts = rows.map((r, i) => [x(r.timestamp), y(Math.max(0, finite(s.value(r.metrics, i)) ?? 0))]);
          const below = k
            ? rows.map((r, i) => [x(r.timestamp), y(Math.max(0, finite(series[k - 1].value(r.metrics, i)) ?? 0))])
            : rows.map((r) => [x(r.timestamp), y(0)]);
          const d = [...pts, ...below.reverse()].map(([a, b], i) => `${i ? "L" : "M"}${a.toFixed(1)},${b.toFixed(1)}`).join(" ");
          return `<path class="stack-area" d="${d} Z" fill="${s.color}"/>`;
        })
        .join("")
    : "";
  const legend = opts.stacked
    ? series.map((s) => `<span class="legend-key"><i data-color="${esc(s.color)}"></i>${esc(s.name)}</span>`).join("")
    : `${series.length > 4 ? `<span class="legend-count">표시 ${series.filter((s) => !s.hidden).length}/${series.length}</span>` : ""}${series.map((s, i) => `<button type="button" class="legend-key series-${i % 8}${s.hidden ? " off" : ""}" data-series="${i}" data-name="${esc(s.name)}" title="표시 전환"><i data-color="${esc(s.color)}"></i>${esc(s.name)}</button>`).join("")}`;
  const zoomRect = opts.zoom ? `<rect class="zoom-sel" x="0" y="${top}" width="0" height="${h - top - bottom}" visibility="hidden"/>` : "";
  return `<div class="panel"><div class="panel-heading"><h2>${esc(title)} <span class="badge-count">${esc(unit)}${log ? " · 로그" : ""}</span>${scaleButton}</h2><div class="legend" data-chart="${esc(title)}">${legend}</div></div><div class="panel-body"><svg class="chart${opts.zoom ? " zoomable" : ""}" data-chart="${seq}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}">${grid}${bandSvg}${areas}${zoomRect}<line class="chart-cursor" x1="0" x2="0" y1="${top}" y2="${h - bottom}" visibility="hidden"/>${series.map((s) => `<path class="series-line" d="${path(s.value)}" stroke="${s.color}" stroke-width="${opts.stacked ? 1.2 : 2.5}" fill="none"${s.hidden ? ' visibility="hidden"' : ""}/>`).join("")}${series.map((s) => `<circle class="chart-dot" r="4" stroke="${s.color}" visibility="hidden"/>`).join("")}</svg><div class="chart-tip"></div></div></div>`;
}
function renderHistory() {
  if (!loaded.history) {
    $("history-chart").innerHTML = '<div class="panel empty">불러오는 중…</div>';
    $("history-table").innerHTML = '<div class="empty">불러오는 중…</div>';
    return;
  }
  const inWindow = (t) => !historyWindow || (t >= historyWindow.from && t <= historyWindow.to);
  const rows = (historyData || []).filter((r) => r.metrics && r.timestamp && inWindow(r.timestamp));
  const chip = $("history-window");
  chip.classList.toggle("hidden", !historyWindow);
  if (historyWindow)
    chip.innerHTML = `확대 중 · ${esc(shortWhen(historyWindow.from))} ~ ${esc(shortWhen(historyWindow.to))} <button class="button compact secondary" id="history-unzoom" type="button">전체 기간 보기</button>`;
  $("history-unzoom")?.addEventListener("click", () => {
    historyWindow = null;
    syncHash();
    renderHistory();
  });
  // Consecutive non-normal samples become shaded bands on the charts (N1).
  const bands = [];
  let open = null;
  rows.forEach((r) => {
    if (r.severity && r.severity !== "normal") {
      if (!open) open = { start: r.timestamp, end: r.timestamp, sev: r.severity };
      open.end = r.timestamp;
      if (r.severity === "critical") open.sev = "critical";
    } else if (open) {
      bands.push(open);
      open = null;
    }
  });
  if (open) bands.push(open);
  // Drop vmmem values the daemon merely carried forward: when the last host
  // observation is more than 10 minutes before the row's own timestamp the
  // number is stale and must render as a gap, not a flat line (F5).
  const vmmemRaw = rows.map((r) =>
    r.metrics.vmmem_observed_at != null &&
    r.timestamp - r.metrics.vmmem_observed_at > 600
      ? null
      : r.metrics.vmmem_bytes,
  );
  const vmmemSeries = [];
  if (vmmemRaw.some((v) => v != null)) {
    // The daemon already carries vmmem forward per row, so the nulls left in
    // vmmemRaw are stale or missing samples: keep them as gaps (F5).
    vmmemSeries.push({
      name: "호스트 vmmem",
      color: "var(--chart-3)",
      value: (m, i) => (vmmemRaw[i] == null ? null : vmmemRaw[i] / 2 ** 30),
    });
  }
  if (rows.length) {
    $("history-chart").innerHTML =
      lineChart(rows, "메모리 변화", "GiB", [
        {
          name: "사용 가능한 RAM",
          color: "var(--chart-1)",
          value: (m) => m.mem_available_kib / 1048576,
        },
        {
          name: "사용 중인 Swap",
          color: "var(--chart-2)",
          value: (m) => (m.swap_total_kib - m.swap_free_kib) / 1048576,
        },
        ...vmmemSeries,
      ], bands, { zoom: true }) +
      attributionChart(inWindow) +
      lineChart(rows, "메모리 대기 압력 · PSI", "%", [
        { name: "some", color: "var(--chart-3)", value: (m) => m.psi_some_avg60 },
        { name: "full", color: "var(--chart-4)", value: (m) => m.psi_full_avg60 },
      ], bands, { zoom: true, psi: true }) +
      lineChart(rows, "Swap 입출력", "MiB/분", [
        {
          name: "swap-in",
          color: "var(--chart-2)",
          value: (m) => m.swap_in_mib_per_minute,
        },
        {
          name: "swap-out",
          color: "var(--chart-1)",
          value: (m) => m.swap_out_mib_per_minute,
        },
      ], bands, { zoom: true });
  } else
    $("history-chart").innerHTML =
      '<div class="panel empty">저장된 자원 이력이 없습니다.</div>';
  bindCharts($("history-chart"));
  const historyWrap = $("history-table");
  historyWrap.innerHTML = table(
    [
      { t: "수집 시각", key: (r) => r.timestamp },
      { t: "상태", key: (r) => String(r.severity) },
      { t: "가용 RAM", cls: "num", key: (r) => r.metrics.mem_available_kib },
      { t: "Swap 사용", cls: "num", key: (r) => r.metrics.swap_total_kib - r.metrics.swap_free_kib },
      { t: "PSI some / full", cls: "num", key: (r) => r.metrics.psi_some_avg60 ?? -1 },
      { t: "Swap in / out", cls: "num", key: (r) => r.metrics.swap_out_mib_per_minute || 0 },
    ],
    rowsOf(
      [...rows].reverse(),
      (r) => [
        when(r.timestamp),
        pill(r.severity),
        `${giB(r.metrics.mem_available_kib)} GiB`,
        `${giB(r.metrics.swap_total_kib - r.metrics.swap_free_kib)} GiB`,
        `${psiValue(r.metrics.psi_some_avg60)} / ${psiValue(r.metrics.psi_full_avg60)} · ${psiStatus(r.metrics)}`,
        `${Number(r.metrics.swap_in_mib_per_minute || 0).toFixed(1)} / ${Number(r.metrics.swap_out_mib_per_minute || 0).toFixed(1)} MiB/분`,
      ],
    ),
    true,
    { id: "history" },
  );
  // Narrow screens fold long tables to 20 rows; historyExpanded keeps the
  // user's choice across refreshes and resets when the range changes.
  const collapsible = rows.length > 20;
  historyWrap.classList.toggle("collapsed", collapsible && !historyExpanded);
  if (collapsible) {
    historyWrap.insertAdjacentHTML(
      "beforeend",
      `<button class="button secondary table-toggle" id="history-toggle">${historyExpanded ? "접기" : `전체 ${rows.length}개 보기`}</button>`,
    );
    $("history-toggle").addEventListener("click", () => {
      historyExpanded = !historyExpanded;
      historyWrap.classList.toggle("collapsed", !historyExpanded);
      $("history-toggle").textContent = historyExpanded
        ? "접기"
        : `전체 ${rows.length}개 보기`;
    });
  }
  document
    .querySelectorAll(".range-picker [data-range]")
    .forEach((b) => {
      const selected = b.dataset.range === historyRange;
      b.classList.toggle("primary", selected);
      b.classList.toggle("secondary", !selected);
      b.setAttribute("aria-pressed", String(selected));
    });
}
// LLM-session memory stacked by project: which work held the RAM when
// available memory dropped. Same time axis and zoom as the charts above.
function attributionChart(inWindow) {
  if (!attributionData || !attributionData.rows?.length || !attributionData.keys?.length) return "";
  const rows = attributionData.rows
    .filter((r) => inWindow(r.timestamp))
    .map((r) => ({ timestamp: r.timestamp, metrics: r.projects }));
  if (rows.length < 2) return "";
  const keys = attributionData.keys;
  const series = keys.map((key, k) => ({
    name: key,
    color: `var(--chart-${(k % 8) + 1})`,
    // Cumulative height for drawing; the tooltip shows the project's own value.
    value: (m) => keys.slice(0, k + 1).reduce((t, name) => t + (m[name] || 0), 0) / 1048576,
    display: (m) => (m[key] || 0) / 1048576,
  }));
  return lineChart(rows, "LLM 세션 메모리 구성", "GiB · 프로젝트별 누적", series, [], { zoom: true, stacked: true });
}
function zoomStart(e) {
  const svg = e.currentTarget,
    info = svg._chart;
  // Touch drags scroll the page; phones use 기간 지정 instead.
  if (!info || e.pointerType === "touch" || e.button !== 0) return;
  const ctm = svg.getScreenCTM();
  if (!ctm) return;
  const px = (ev) => new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse()).x;
  const x0 = px(e),
    sel = svg.querySelector(".zoom-sel");
  const move = (ev) => {
    const x1 = px(ev);
    sel.setAttribute("x", Math.min(x0, x1));
    sel.setAttribute("width", Math.abs(x1 - x0));
    sel.setAttribute("visibility", "visible");
  };
  const up = (ev) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    sel.setAttribute("visibility", "hidden");
    const x1 = px(ev);
    if (Math.abs(x1 - x0) < 12) return;
    const { left, right, w, t0, span } = info.geo;
    const t = (x) => t0 + ((Math.min(Math.max(x, left), w - right) - left) / (w - left - right)) * span;
    historyWindow = { from: t(Math.min(x0, x1)), to: t(Math.max(x0, x1)) };
    syncHash();
    renderHistory();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
const RANGE_SECONDS = { "3h": 3 * 3600, "24h": 86400, "7d": 7 * 86400, "14d": 14 * 86400 };
// The smallest loaded range that still reaches back to `from`.
const rangeCovering = (from) =>
  HISTORY_RANGE_KEYS.find((k) => Date.now() / 1000 - RANGE_SECONDS[k] <= from) || "14d";
function showHistoryWindow(from, to) {
  const range = rangeCovering(from);
  navigate("history", false, { range, from: String(Math.round(from)), to: String(Math.round(to)) });
}
function localInput(t) {
  const d = new Date(t * 1000),
    pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function customRangeDialog() {
  const now = Date.now() / 1000,
    win = historyWindow || { from: now - 6 * 3600, to: now };
  dialog(
    "기간 지정",
    `<p class="dialog-text">보관 기간(최대 14일) 안에서 시작과 끝을 고릅니다.</p><div class="range-form"><label>시작 <input id="range-from" type="datetime-local" class="search" value="${localInput(win.from)}" min="${localInput(now - 14 * 86400)}" max="${localInput(now)}"></label><label>끝 <input id="range-to" type="datetime-local" class="search" value="${localInput(win.to)}" min="${localInput(now - 14 * 86400)}" max="${localInput(now)}"></label></div><div class="dialog-actions"><button id="range-cancel" class="button secondary">취소</button><button id="range-apply" class="button primary">적용</button></div>`,
  );
  $("range-cancel").addEventListener("click", () => $("dialog").close());
  $("range-apply").addEventListener("click", () => {
    const from = new Date($("range-from").value).getTime() / 1000,
      to = new Date($("range-to").value).getTime() / 1000;
    if (!(from > 0 && to > from)) return notify("끝 시각이 시작보다 뒤여야 합니다.");
    $("dialog").close();
    showHistoryWindow(Math.max(from, now - 14 * 86400), Math.min(to, now));
  });
}
const durationText = (seconds) =>
  seconds >= 3600
    ? `${Math.floor(seconds / 3600)}시간 ${Math.floor((seconds % 3600) / 60)}분`
    : `${Math.floor(seconds / 60)}분`;
const severityText = (severity) =>
  ({ warning: "주의", critical: "위험" })[severity] || severity;
// Compact episode start: bare time for today, date + time otherwise.
const shortWhen = (stamp) => {
  const d = new Date(stamp * 1000),
    pad = (n) => String(n).padStart(2, "0"),
    time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === new Date().toDateString()
    ? `${time}:${pad(d.getSeconds())}`
    : `${pad(d.getMonth() + 1)}. ${pad(d.getDate())}. ${time}`;
};
function renderAlerts() {
  if (!loaded.alerts) {
    $("alerts-summary").innerHTML = '<div class="panel empty">불러오는 중…</div>';
    return;
  }
  if (!alertsData) {
    $("alerts-summary").innerHTML =
      '<div class="panel empty">경보 이력을 받지 못했습니다.</div>';
    return;
  }
  const episodes = [...(alertsData.episodes || [])].reverse();
  const weekAgo = Date.now() / 1000 - 7 * 86400;
  const recent = episodes.filter((e) => e.start >= weekAgo);
  const ongoing = episodes.find((e) => e.ongoing);
  const totalSeconds = recent.reduce((t, e) => t + e.duration_seconds, 0);
  $("alerts-summary").innerHTML = `<div class="cards">${metricCard(
    "지난 7일 경보",
    num(recent.length),
    "회",
    `누적 ${durationText(totalSeconds)}`,
    null,
  )}${metricCard(
    "진행 중",
    ongoing ? shortWhen(ongoing.start) : "—",
    "",
    ongoing
      ? `${durationText(Date.now() / 1000 - ongoing.start)}째 ${esc(severityText(ongoing.worst_severity))}`
      : "지금 진행 중인 경보가 없습니다",
    null,
    "",
    ongoing ? (ongoing.worst_severity === "critical" ? "bad" : "warn") : "",
  )}</div>`;
  // 7-day cause rollup: one count per episode per label (C4). Bar widths use
  // data-percent + JS style assignment, which the strict CSP allows.
  const reasons = alertsData.reasons_7d || [];
  const maxEpisodes = Math.max(1, ...reasons.map((r) => r.episodes));
  $("alerts-reasons").innerHTML = reasons.length
    ? `<div class="panel"><div class="panel-heading"><h2>지난 7일 원인별</h2><span class="muted">에피소드마다 원인을 한 번씩 집계</span></div><div class="panel-body">${reasons
        .map(
          (r) =>
            `<div class="reason-bar"><span class="reason-label" title="${esc(r.label)}">${esc(r.label)}</span><span class="bar"><span data-percent="${(r.episodes / maxEpisodes) * 100}"></span></span><span class="reason-count">${num(r.episodes)}회 · 누적 ${durationText(r.total_seconds)}</span></div>`,
        )
        .join("")}</div></div>`
    : "";
  $("alerts-reasons")
    .querySelectorAll("[data-percent]")
    .forEach((el) => (el.style.width = el.dataset.percent + "%"));
  $("alerts-table").innerHTML = table(
    [
      { t: "시작", key: (e) => e.start },
      { t: "끝", key: (e) => (e.ongoing ? Infinity : e.end) },
      { t: "지속", cls: "num", key: (e) => e.duration_seconds },
      { t: "최고 상태", key: (e) => String(e.worst_severity) },
      { t: "주요 원인" },
      { t: "" },
    ],
    rowsOf(episodes, (e) => [
      when(e.start),
      e.ongoing ? '<span class="pill warn">진행 중</span>' : when(e.end),
      durationText(e.duration_seconds),
      pill(e.worst_severity),
      (e.reason_summary || [])
        .slice(0, 5)
        .map(
          (r) =>
            `<div>${esc(r.label)} <span class="pill">${num(r.samples)}회 관측</span><span class="subline">${esc(r.last)}</span></div>`,
        )
        .join("") ||
        (e.reasons_top5 || []).map(esc).join("<br>") ||
        "—",
      e.start >= Date.now() / 1000 - 14 * 86400
        ? `<button class="button compact secondary" data-episode="${Number(e.start)}:${Number(e.ongoing ? Date.now() / 1000 : e.end)}">이력 보기</button>`
        : "",
    ]),
    true,
    { id: "alerts" },
  );
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-episode]");
  if (!btn) return;
  const [start, end] = btn.dataset.episode.split(":").map(Number);
  // Pad the episode so the lead-up and recovery are visible too.
  const pad = Math.max(600, (end - start) * 0.25);
  showHistoryWindow(start - pad, Math.min(Date.now() / 1000, end + pad));
});
function snoozeDialog() {
  const until = liveSummary?.snooze_until || monitorData?.snooze_until || 0;
  const choices = [
    [60, "1시간"],
    [240, "4시간"],
    [480, "8시간"],
    [1440, "24시간"],
  ];
  dialog(
    "재알림 일시 정지",
    `<p class="dialog-text">같은 상태가 이어질 때 보내는 재알림만 멈춥니다.\n새 경보, 주의→위험 전환, 새 디스크 경보, 회복 알림은 계속 보냅니다.${until > Date.now() / 1000 ? `\n\n현재 ${shortWhen(until)}까지 일시 정지 중입니다.` : ""}</p><div class="action-grid">${choices.map(([m, label]) => `<button class="button secondary" data-snooze-minutes="${m}">${label}</button>`).join("")}${until > Date.now() / 1000 ? '<button class="button danger" data-snooze-minutes="0">일시 정지 해제</button>' : ""}</div>`,
  );
  $("dialog-content")
    .querySelectorAll("[data-snooze-minutes]")
    .forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const data = await api("/api/alerts/snooze", { minutes: Number(b.dataset.snoozeMinutes) });
          if (liveSummary) liveSummary.snooze_until = data.until;
          if (monitorData) monitorData.snooze_until = data.until;
          $("dialog").close();
          notify(data.message, true);
          render();
        } catch (error) {
          notify(error.message);
          b.disabled = false;
        }
      }),
    );
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-snooze]")) snoozeDialog();
  const edit = e.target.closest("[data-config-key]");
  if (edit) configDialog(edit.dataset.configKey);
});
function configDialog(key) {
  const r = (settingsData?.rules || []).find((rule) => rule.key === key);
  if (!r || !r.editable) return;
  const step = r.kind === "int" ? "1" : "any";
  dialog(
    "설정 변경",
    `<p class="dialog-text"><strong>${esc(r.description)}</strong>\n<code>${esc(r.key)}</code> · 허용 범위 ${r.min} ~ ${r.max} · 기본값 ${esc(String(r.default))}</p><label class="range-form">새 값 <input id="config-value" class="search" type="number" inputmode="decimal" step="${step}" min="${r.min}" max="${r.max}" value="${esc(String(r.value))}"></label><p class="health-description">Guard가 다음 측정(약 15초 안)에 검증하고 config.toml에 저장합니다. 경고·위험 임계값의 대소 관계를 깨는 값은 거부됩니다.</p><div class="dialog-actions"><button id="cancel-action" class="button secondary">취소</button><button id="confirm-action" class="button primary">변경 요청</button></div>`,
  );
  $("cancel-action").addEventListener("click", () => $("dialog").close());
  $("confirm-action").addEventListener("click", async () => {
    const raw = $("config-value").value.trim();
    const value = r.kind === "int" ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
    if (!Number.isFinite(value) || value < r.min || value > r.max || (r.kind === "int" && String(value) !== raw))
      return notify(`${r.min} ~ ${r.max} 범위의 ${r.kind === "int" ? "정수" : "숫자"}를 입력하세요.`);
    $("confirm-action").disabled = true;
    try {
      const data = await api("/api/settings/change", { key, value });
      $("dialog").close();
      notify(data.message, true);
      await settleConfigRequest();
    } catch (error) {
      notify(error.message);
      $("confirm-action").disabled = false;
    }
  });
}
// Re-read settings until the guard has handled the request (max ~45 s).
async function settleConfigRequest() {
  for (let i = 0; i < 15; i++) {
    await refresh(true);
    const state = settingsData?.config_request?.state;
    if (state && state !== "pending") {
      if (state === "failed") notify(`설정 변경 실패: ${settingsData.config_request.message}`);
      else notify(`${settingsData.config_request.key} 변경을 적용했습니다.`, true);
      return;
    }
    await sleep(3000);
  }
}
function renderSettings() {
  if (!loaded.settings) {
    $("settings-meta").innerHTML = '<div class="empty">불러오는 중…</div>';
    return;
  }
  if (!settingsData) {
    $("settings-meta").innerHTML =
      '<div class="empty">설정 정보를 받지 못했습니다.</div>';
    return;
  }
  const d = settingsData;
  $("settings-meta").innerHTML = `<div class="status-strip"><strong>${esc(d.config_path)}</strong></div>`;
  $("settings-warnings").innerHTML = d.load_warnings.length
    ? d.load_warnings
        .map((w) => `<div class="reason">${esc(w)}</div>`)
        .join("")
    : '<p class="health-description">설정 파일 경고가 없습니다.</p>';
  const channelErrs = d.channel_errors || {};
  $("settings-channels").innerHTML =
    `<div class="channel-chips">${Object.entries(d.channels)
      .map(([k, v]) => channelChip(k, v, channelErrs[k]))
      .join(" ")}</div>` +
    (Object.entries(channelErrs).length
      ? `<div class="health-description"><span class="row-error">최근 실패: ${Object.entries(channelErrs)
          .map(([k, v]) => `${esc(CHANNEL_LABELS[k] || k)} — ${esc(v)}`)
          .join(" · ")}</span></div>`
      : '<div class="health-description">최근 알림 채널 실패가 없습니다.</div>');
  const weekly = d.weekly_report || {};
  const weekdays = ["월", "화", "수", "목", "금", "토", "일"];
  const rule = (key) => (d.rules || []).find((r) => r.key === key)?.value;
  $("settings-channels").insertAdjacentHTML(
    "beforeend",
    `<div class="health-description">주간 리포트 ${rule("weekly_report_enabled") ? `매주 ${weekdays[rule("weekly_report_weekday")] ?? "?"}요일 ${rule("weekly_report_hour")}시 이후 첫 측정에 발송` : "꺼짐"} · 마지막 발송 ${weekly.sent_at ? `${when(weekly.sent_at)} (${esc(weekly.slot)})` : "기록 없음"} · 미리보기: <code>wrg weekly-report</code></div>`,
  );
  const req = d.config_request || {};
  const reqBox = $("config-request");
  reqBox.classList.toggle("hidden", !req.key);
  if (req.key)
    reqBox.innerHTML = `<div class="status-strip config-request ${req.state}">${pill(req.state === "applied" ? "normal" : req.state === "failed" ? "failed" : "unknown")}<span><code>${esc(req.key)}</code> = ${esc(String(req.value))} · ${req.state === "pending" ? "Guard 적용 대기 중" : req.state === "applied" ? "적용됨" : `실패: ${esc(req.message)}`} · 요청 ${when(req.requested_at)}</span></div>`;
  renderPushDevice();
  const c = d.code_copies;
  const commitText = (commit, dirty) =>
    commit ? esc(commit.slice(0, 12)) + (dirty ? " (미커밋 포함)" : "") : "없음";
  $("settings-copies").innerHTML = `<div class="status-strip">${pill(
    c.level === "OK" ? "normal" : c.level === "WARN" ? "warning" : "critical",
  )}<strong>${esc(c.detail)}</strong></div><div class="health-description">터미널 설치본 ${commitText(c.cli_commit, c.cli_dirty)} · 서비스 설치본 ${commitText(c.web_commit, c.web_dirty)}</div>`;
  const formatValue = (kind, value) =>
    kind === "bool"
      ? value
        ? "켜짐"
        : "꺼짐"
      : value == null || value === ""
        ? "—"
        : esc(String(value));
  const changedOnly = $("settings-changed-only");
  if (changedOnly) changedOnly.checked = settingsChangedOnly;
  // Rules arrive in CONFIG_RULES order; group them by title in the
  // server-provided CONFIG_GROUPS order (C4).
  const groups = new Map((d.groups || []).map((title) => [title, []]));
  for (const r of d.rules) {
    if (!groups.has(r.group)) groups.set(r.group, []);
    groups.get(r.group).push(r);
  }
  $("settings-groups").innerHTML = [...groups.entries()]
    .map(([title, rules], index) => {
      const visible = rules.filter(
        (r) => !settingsChangedOnly || r.value !== r.default,
      );
      if (!visible.length) return "";
      return `<div class="panel"><div class="panel-heading"><h2>${esc(title)} <span class="badge-count">${visible.length}개</span></h2></div><div class="table-wrap">${table(
        [
          { t: "설정", key: (r) => String(r.key) },
          { t: "현재 값", cls: "num", key: (r) => String(r.value ?? "") },
          { t: "기본값", cls: "num", key: (r) => String(r.default ?? "") },
          { t: "허용 범위", cls: "num" },
          { t: "" },
        ],
        rowsOf(visible, (r) => {
          const changed = r.value !== r.default;
          return [
            `<strong>${esc(r.description.replace(/^(Warning|Critical): /, ""))}</strong><span class="subline"><code>${esc(r.key)}</code></span>`,
            formatValue(r.kind, r.value),
            `${formatValue(r.kind, r.default)}${changed ? ' <span class="changed">·변경됨</span>' : ""}`,
            r.min != null || r.max != null
              ? `${r.min != null ? r.min : ""} ~ ${r.max != null ? r.max : ""}`
              : "—",
            r.editable
              ? `<button class="button compact secondary" data-config-key="${esc(r.key)}">변경</button>`
              : '<span class="muted" title="PC 터미널의 wrg config set에서 바꿉니다">터미널</span>',
          ];
        }),
        true,
        { id: `settings-${index}` },
      )}</div></div>`;
    })
    .join("") ||
    '<div class="panel empty">모든 설정이 기본값입니다.</div>';
}
function auditDetail(d) {
  const parts = [];
  if (d.minutes != null) parts.push(d.minutes ? `${d.minutes / 60}시간` : "해제");
  if (d.value != null) parts.push(`값 ${d.value}`);
  if (d.pids) parts.push(`요청 PID ${d.pids.join(", ")}`);
  if (d.killed) parts.push(`종료 ${d.killed.length}개`);
  return parts.join(" · ");
}
// --- Browser push on this device. iPhone/iPad need the dashboard added to
// the home screen (Safari 16.4+); other browsers work from a normal tab.
const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
let swRegistration = null;
async function registerServiceWorker() {
  if (!pushSupported()) return null;
  try {
    swRegistration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    swRegistration = null;
  }
  return swRegistration;
}
function keyBytes(base64) {
  const raw = atob((base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}
let pushRenderVersion = 0;
async function renderPushDevice() {
  const version = ++pushRenderVersion;
  const host = $("push-device");
  if (!host) return;
  if (!pushSupported()) {
    host.innerHTML = '<p class="health-description">이 브라우저는 푸시 알림을 지원하지 않습니다. iPhone·iPad는 Safari에서 공유 → 홈 화면에 추가한 뒤 그 앱에서 등록하세요.</p>';
    return;
  }
  if (!pushKeyData) {
    host.innerHTML = '<p class="health-description">푸시 설정을 확인하는 중…</p>';
    return;
  }
  if (!pushKeyData.available) {
    host.innerHTML = `<p class="health-description">푸시를 사용할 수 없습니다: ${esc(pushKeyData.reason || "")}</p>`;
    return;
  }
  const reg = swRegistration || (await registerServiceWorker());
  let sub = null, registered = false, replaceSubscription = false, statusError = "";
  try {
    sub = reg ? await reg.pushManager.getSubscription() : null;
    if (sub) {
      const localKey = sub.options?.applicationServerKey;
      const expected = keyBytes(pushKeyData.public_key);
      if (localKey) {
        const actual = new Uint8Array(localKey);
        replaceSubscription = actual.length !== expected.length || actual.some((b, i) => b !== expected[i]);
      }
      if (!replaceSubscription) {
        const status = await api("/api/push/status", { subscription: sub.toJSON() });
        registered = status.registered === true;
        replaceSubscription = status.expired === true;
      }
    }
  } catch {
    statusError = "서버 등록 여부를 확인하지 못했습니다. 다시 등록할 수 있습니다.";
  }
  if (version !== pushRenderVersion || $("push-device") !== host) return;
  const denied = Notification.permission === "denied";
  const ready = registered && !denied && !replaceSubscription && !statusError;
  const description = denied ? "브라우저 설정에서 이 사이트의 알림이 차단되어 있습니다"
    : !reg ? "브라우저 알림 서비스를 준비하지 못했습니다. 새로고침 후 다시 시도하세요"
    : replaceSubscription ? "푸시 키가 변경됐거나 구독이 만료됐습니다. 다시 등록하세요"
    : statusError || (ready ? "서버 등록 완료 · 실제 수신은 테스트 알림으로 확인하세요"
      : sub ? "서버 등록 미완료 · 기존 브라우저 구독으로 다시 등록할 수 있습니다"
      : "경보·회복 알림을 이 기기로 받습니다");
  const on = `<button class="button compact primary" id="push-on" ${denied || !reg ? "disabled" : ""}>${sub ? "다시 등록" : "이 기기에서 받기"}</button>`;
  host.innerHTML = `<div class="push-row"><div><strong>이 기기 푸시 알림</strong><span class="subline">${esc(description)} · 등록된 기기 ${num(pushKeyData.subscriptions)}대</span></div><div class="row-actions">${ready ? '<button class="button compact secondary" id="push-test">테스트 보내기</button>' : on}${sub ? '<button class="button compact secondary" id="push-off">해제</button>' : ""}</div></div>`;
  $("push-on")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      if (!reg) throw new Error("브라우저 알림 서비스를 준비하지 못했습니다.");
      if ((await Notification.requestPermission()) !== "granted") {
        notify("알림 권한이 허용되지 않았습니다.");
        return;
      }
      let subscription = sub;
      if (subscription && replaceSubscription) {
        await api("/api/push/unsubscribe", { endpoint: subscription.endpoint });
        if (!(await subscription.unsubscribe())) throw new Error("이전 구독을 해제하지 못했습니다.");
        subscription = null;
      }
      subscription ||= await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyBytes(pushKeyData.public_key),
      });
      const label = (navigator.userAgentData?.platform || navigator.platform || "").slice(0, 40);
      const data = await api("/api/push/subscribe", { subscription: subscription.toJSON(), label });
      notify(data.message, true);
      await refresh(true);
    } catch (error) {
      notify(error.message || "푸시 등록에 실패했습니다.");
    } finally {
      await renderPushDevice();
    }
  });
  $("push-off")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/push/unsubscribe", { endpoint: sub.endpoint });
      if (!(await sub.unsubscribe())) throw new Error("서버 등록은 해제됐지만 브라우저 구독을 해제하지 못했습니다.");
      notify(data.message, true);
      await refresh(true);
    } catch (error) {
      notify(error.message);
    } finally {
      await renderPushDevice();
    }
  });
  $("push-test")?.addEventListener("click", async () => {
    try {
      notify((await api("/api/push/test", {})).message, true);
    } catch (error) {
      notify(error.message);
    }
  });
}

function renderAudit() {
  if (!loaded.audit) {
    $("audit-table").innerHTML = '<div class="empty">불러오는 중…</div>';
    return;
  }
  $("audit-table").innerHTML = table(
    [
      { t: "시각", key: (r) => String(r.time) },
      { t: "서비스", key: (r) => String(r.id) },
      { t: "작업", key: (r) => String(r.action || r.op) },
      "결과",
    ],
    rowsOf(
      [...auditData].reverse(),
      (r) => [
        esc(new Date(r.time).toLocaleString("ko-KR", { hour12: false })),
        esc(r.id),
        `${esc(actionNames[r.action] || { register: "등록", kill: "세션 종료", snooze: "재알림 일시 정지", "kill-stale": "오래된 세션 일괄 종료", "config-change": "설정 변경 요청", "push-subscribe": "푸시 알림 등록", "push-unsubscribe": "푸시 알림 해제", "push-test": "푸시 테스트" }[r.op] || r.op)}${r.detail ? `<span class="subline">${esc(auditDetail(r.detail))}</span>` : ""}`,
        `${pill(r.ok ? "normal" : "failed")}${r.error ? `<span class="subline">${esc(r.error)}</span>` : ""}`,
      ],
    ),
    true,
    { id: "audit" },
  );
}
function render() {
  switch (currentView) {
    case "services": return renderServices();
    case "disks": return renderDisks();
    case "top": return renderTop();
    case "sessions": return renderSessions();
    case "mcp": return renderMcp();
    case "history": return renderHistory();
    case "alerts": return renderAlerts();
    case "settings": return renderSettings();
    case "audit": return renderAudit();
    default: return renderOverview();
  }
}
// Menu badges: where attention is needed, without opening each view. Fed
// by whatever data is loaded plus the live summary stream (see liveSummary).
let liveSummary = null;
function observationIsStale(timestamp) {
  const stamp = Number(timestamp), now = Date.now() / 1000;
  return !Number.isFinite(stamp) || stamp <= 0 || stamp > now + 5 || now - stamp > 90;
}
function currentResourceSeverity() {
  const liveAt = Number(liveSummary?.updated_at) || 0;
  const pollAt = Number(monitorData?.daemon_updated_at) || 0;
  const source = liveAt > pollAt ? liveSummary : monitorData;
  if (!source || observationIsStale(Math.max(liveAt, pollAt))) return "unknown";
  return source.severity || "unknown";
}
function appProblems() {
  return serviceData.services.filter(
    (s) =>
      s.category === "app" &&
      (["failed", "degraded", "unknown", "missing"].includes(s.state) ||
        s.error ||
        s.drift ||
        (s.autostart && s.state !== "active") ||
        (s.state === "active" && s.health && !s.health.ok)),
  );
}
function setBadge(view, count, level = "") {
  document.querySelectorAll(`[data-badge="${view}"]`).forEach((el) => {
    el.classList.toggle("hidden", !count);
    el.classList.toggle("bad", level === "bad");
    el.classList.toggle("dot", count === true);
    el.textContent = count === true || !count ? "" : String(count);
    el.setAttribute("aria-label", count ? (count === true ? "확인 필요" : `확인 필요 ${count}건`) : "");
  });
}
function updateBadges() {
  const sum = liveSummary || {};
  const services = loaded.services ? appProblems() : null;
  const serviceCount = services ? services.length : sum.service_problems || 0;
  const serviceBad = services ? services.some((s) => s.state === "failed") : sum.service_failed;
  setBadge("services", serviceCount, serviceBad ? "bad" : "");
  const disks = diskData
    ? diskData.disks.filter((d) => d.kind === "windows" && ["warning", "critical"].includes(d.severity))
    : null;
  const diskCount = disks ? disks.length : (sum.disk_alerts || []).length;
  const diskBad = disks ? disks.some((d) => d.severity === "critical") : sum.disk_critical;
  setBadge("disks", diskCount, diskBad ? "bad" : "");
  const severity = currentResourceSeverity();
  setBadge("alerts", ["warning", "critical"].includes(severity) || false, severity === "critical" ? "bad" : "");
  const stale = monitorData ? (monitorData.sessions || []).filter((r) => r.stale).length : sum.stale_sessions || 0;
  setBadge("sessions", stale);
  // 더보기 carries a dot when a view hidden behind it needs attention.
  const hidden = document.querySelectorAll(
    ".sidebar .nav-item:not([data-view=overview]):not([data-view=services]):not([data-view=sessions]):not([data-view=history]) .nav-badge:not(.hidden)",
  );
  setBadge("more", hidden.length > 0 || false, [...hidden].some((b) => b.classList.contains("bad")) ? "bad" : "");
}
// Reflect the current severity in the tab title and favicon dot (U13).
const BASE_TITLE = "내 PC · WSL Resource Guard";
const SEVERITY_COLORS = {
  normal: "#4cbfa7",
  warning: "#d3a148",
  critical: "#c95661",
};
function updateAlertBadge() {
  const severity = currentResourceSeverity();
  const icon = { critical: "🔴", warning: "🟠" }[severity] || "";
  document.title = (icon ? `${icon} ` : severity === "unknown" ? "상태 미확인 · " : "") + BASE_TITLE;
  const favicon = $("favicon");
  if (!favicon) return;
  const color = SEVERITY_COLORS[severity] || "#9aa6b2";
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(16, 16, 13, 0, Math.PI * 2);
  ctx.fill();
  favicon.href = canvas.toDataURL("image/png");
}
async function refresh(force = false) {
  if (busyAction) return;
  if (loading) {
    pendingForce = pendingForce || force;
    return;
  }
  loading = true;
  $("refresh").disabled = true;
  $("refresh").textContent = "↻ 갱신 중";
  const view = currentView;
  try {
    const jobs = [];
    if (["overview", "disks"].includes(view))
      jobs.push({ key: "disks", run: () => api("/api/disks" + (force ? "?force=1" : "")) });
    if (view === "disks") jobs.push({ key: "dockerDf", run: () => api("/api/docker-df") });
    if (view === "overview") jobs.push({ key: "overviewHistory", run: () => api("/api/history?range=3h") });
    if (view === "sessions") jobs.push({ key: "sessionHistory", run: () => api("/api/session-history") });
    if (["overview", "top", "sessions", "mcp"].includes(view))
      jobs.push({ key: "monitor", run: () => api("/api/monitor" + (force ? "?force=1" : "")) });
    if (["overview", "services"].includes(view))
      jobs.push({ key: "services", run: () => api("/api/services") });
    if (view === "services")
      jobs.push({ key: "serviceMemory", run: () => api("/api/service-memory") });
    if (view === "history") {
      // Tag the request: if the user switches ranges mid-flight the stale
      // response must not be applied under the new selection.
      const range = historyRange;
      jobs.push({ key: "history", range, run: () => api(`/api/history?range=${range}`) });
      jobs.push({ key: "attribution", range, run: () => api(`/api/attribution?range=${range}`) });
    }
    if (view === "alerts") jobs.push({ key: "alerts", run: () => api("/api/alerts") });
    if (view === "settings") {
      jobs.push({ key: "settings", run: () => api("/api/settings") });
      jobs.push({ key: "pushKey", run: () => api("/api/push/key") });
    }
    if (view === "audit")
      jobs.push({ key: "audit", run: () => api("/api/audit") });
    const setters = {
      disks: (v) => (diskData = v),
      dockerDf: (v) => (dockerDfData = v),
      overviewHistory: (v) => (overviewHistory = v),
      attribution: (v) => (attributionData = v),
      sessionHistory: (v) => (sessionHistoryData = v),
      pushKey: (v) => (pushKeyData = v),
      monitor: (v) => {
        const stamp = Number(v.daemon_updated_at || 0);
        if (!monitorData || !stamp || stamp >= Number(monitorData.daemon_updated_at || 0)) monitorData = v;
      },
      services: (v) => (serviceData = v),
      serviceMemory: (v) => (serviceMemoryData = v),
      history: (v) => (historyData = v),
      alerts: (v) => (alertsData = v),
      settings: (v) => (settingsData = v),
      audit: (v) => (auditData = v),
    };
    const results = await Promise.allSettled(jobs.map((j) => j.run()));
    const failed = [];
    results.forEach((r, i) => {
      const { key } = jobs[i];
      if (r.status === "fulfilled") {
        if (["history", "attribution"].includes(key) && jobs[i].range !== historyRange) return;
        setters[key](r.value);
        if (key in loaded) loaded[key] = true;
      } else if (!["serviceMemory", "dockerDf", "overviewHistory", "attribution", "sessionHistory", "pushKey"].includes(key)) {
        failed.push(r.reason.message);
      }
    });
    lastRefresh = Date.now();
    $("loading").classList.add("hidden");
    render();
    updateAlertBadge();
    updateBadges();
    const stamp = new Date().toLocaleTimeString("ko-KR", { hour12: false });
    if (failed.length) {
      notify(`일부 갱신 실패: ${[...new Set(failed)].join(" · ")}`);
      $("last-updated").textContent = `일부 갱신 실패 · ${stamp}`;
    } else {
      $("last-updated").textContent = "마지막 갱신 " + stamp;
    }
  } catch (error) {
    notify(error.message);
  } finally {
    loading = false;
    $("refresh").disabled = false;
    $("refresh").textContent = "↻ 지금 새로고침";
    if (pendingForce) {
      pendingForce = false;
      refresh(true);
    } else if (view !== currentView) refresh();
  }
}
function schedule() {
  clearInterval(timer);
  const seconds = Number($("refresh-interval").value);
  if (seconds && !document.hidden)
    timer = setInterval(() => refresh(), seconds * 1000);
}
// Views reachable from the mobile tab bar; the rest live under 더보기.
const TAB_VIEWS = ["overview", "services", "sessions", "history"];
function navigate(view, initial = false, params = null) {
  if (!titles[view]) view = "overview";
  $("notice").classList.add("hidden");
  currentView = view;
  if (params) applyViewParams(view, params);
  const target = "#" + hashFor(view);
  if (location.hash !== target) {
    // The first load rewrites in place so Back does not bounce to a bare URL.
    if (initial) history.replaceState(null, "", target);
    else location.hash = target;
  }
  let selectedNav = null;
  document.querySelectorAll(".nav-item, .tab-item[data-view]").forEach((b) => {
    const selected = b.dataset.view === view;
    b.classList.toggle("selected", selected);
    if (selected) {
      b.setAttribute("aria-current", "page");
      if (b.classList.contains("nav-item")) selectedNav = b;
    } else b.removeAttribute("aria-current");
  });
  $("tab-more").classList.toggle("selected", !TAB_VIEWS.includes(view));
  document
    .querySelectorAll(".view")
    .forEach((el) => el.classList.toggle("hidden", el.id !== "view-" + view));
  [
    $("breadcrumb").textContent,
    $("page-title").textContent,
    $("page-description").textContent,
  ] = titles[view];
  // Keep the selected item visible inside the scrolling nav and land at the
  // top of the new view. The first load skips both.
  if (!initial) {
    selectedNav?.scrollIntoView({ block: "nearest" });
    window.scrollTo(0, 0);
  }
  render();
  refresh();
}
// --- URL state: filters, sort, and the history range live in the hash
// (#sessions?q=demo&stale=1&sort=4:desc) so reloads and bookmarks keep them.
const SORT_TABLE = { top: "top", sessions: "sessions", mcp: "mcp", history: "history", alerts: "alerts", audit: "audit" };
function parseHash(hash = location.hash) {
  const [view, query = ""] = hash.replace(/^#/, "").split("?");
  return { view: view || "overview", params: Object.fromEntries(new URLSearchParams(query)) };
}
function viewParams(view) {
  const p = {};
  if (view === "top" && $("top-search").value) p.q = $("top-search").value;
  if (view === "sessions") {
    if ($("session-search").value) p.q = $("session-search").value;
    if ($("session-stale").checked) p.stale = "1";
  }
  if (view === "mcp") {
    if ($("mcp-search").value) p.q = $("mcp-search").value;
    if (Number($("mcp-hours").value) > 0) p.hours = $("mcp-hours").value;
  }
  if (view === "history") {
    p.range = historyRange;
    if (historyWindow) p.from = String(Math.round(historyWindow.from)), (p.to = String(Math.round(historyWindow.to)));
  }
  const sort = tableSorts[SORT_TABLE[view]];
  if (sort && sort.col != null) p.sort = `${sort.col}:${sort.dir}`;
  return p;
}
function hashFor(view) {
  const query = new URLSearchParams(viewParams(view)).toString();
  return query ? `${view}?${query}` : view;
}
function applyViewParams(view, p) {
  if (view === "top") $("top-search").value = p.q || "";
  if (view === "sessions") {
    $("session-search").value = p.q || "";
    $("session-stale").checked = p.stale === "1";
  }
  if (view === "mcp") {
    $("mcp-search").value = p.q || "";
    $("mcp-hours").value = p.hours || "0";
  }
  if (view === "history") {
    if (HISTORY_RANGE_KEYS.includes(p.range) && p.range !== historyRange) {
      historyRange = p.range;
      loaded.history = false;
      historyData = null;
      attributionData = null;
    }
    const from = Number(p.from),
      to = Number(p.to);
    historyWindow = from > 0 && to > from ? { from, to } : null;
  }
  const id = SORT_TABLE[view];
  const m = /^(\d+):(asc|desc)$/.exec(p.sort || "");
  if (id) {
    if (m) tableSorts[id] = { col: Number(m[1]), dir: m[2] };
    else delete tableSorts[id];
  }
}
// Rewrites the hash for in-view changes without a new Back entry.
function syncHash() {
  const target = "#" + hashFor(currentView);
  if (location.hash !== target) history.replaceState(null, "", target);
}
function openMoreMenu() {
  const groups = [...document.querySelectorAll(".sidebar .nav-group")]
    .map((g) => g.outerHTML)
    .join("");
  dialog("메뉴", `<div class="more-menu">${groups}</div>`);
  $("dialog-content")
    .querySelectorAll("[data-view]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        $("dialog").close();
        navigate(b.dataset.view);
      }),
    );
}
function dialog(title, html) {
  $("dialog-title").textContent = title;
  $("dialog-content").innerHTML = html;
  if (!$("dialog").open) $("dialog").showModal();
}
// Dialog sections: what runs now, what happens at the next WSL start, and
// the destructive removal. Buttons that would not change anything are
// disabled with the reason as a tooltip.
const ACTION_GROUPS = [
  ["지금 실행", ["start", "stop", "restart"]],
  ["자동 실행 (다음 WSL 시작)", ["autostart-on", "autostart-off"]],
  ["한 번에 바꾸기", ["enable", "disable"]],
  ["등록", ["remove"]],
];
function actionBlock(row, action) {
  if (
    row.target === "tailscaled.service" &&
    ["disable", "stop", "restart", "remove", "autostart-off"].includes(action)
  )
    return "PC 터미널에서 실행하세요";
  const running = ["active", "degraded", "activating"].includes(row.state);
  if (action === "start" && running) return "이미 실행 중입니다";
  if (["stop", "restart"].includes(action) && !running) return "실행 중이 아닙니다";
  if (action === "autostart-on" && row.autostart && !row.drift) return "이미 자동 실행이 켜져 있습니다";
  if (action === "autostart-off" && !row.autostart && !row.drift) return "이미 자동 실행이 꺼져 있습니다";
  if (action === "enable" && row.autostart && running) return "이미 사용 중입니다";
  if (action === "disable" && !row.autostart && !running) return "이미 미사용입니다";
  return "";
}
function manageService(id) {
  const row = serviceData.services.find((s) => s.id === id);
  if (!row) return;
  const facts = [
    `${row.detail} · 자동 실행 ${row.autostart ? "켜짐" : "꺼짐"}`,
    row.memory_bytes != null ? `메모리 ${memory(row.memory_bytes)}${row.memory_high ? ` · High ${memory(row.memory_high)}` : ""}${row.memory_max ? ` · Max ${memory(row.memory_max)}` : ""}` : "",
    row.health ? `웹 응답 ${row.health.ok ? `정상 (HTTP ${row.health.status})` : `없음 — ${row.health.error || ""}`} · ${when(row.health.checked_at)}` : "",
    row.notice,
    row.directory,
  ].filter(Boolean);
  const groups = ACTION_GROUPS.map(
    ([label, actions]) =>
      `<div class="action-group"><div class="action-group-label">${esc(label)}</div><div class="action-grid">${actions
        .map((action) => {
          const blocked = actionBlock(row, action);
          return `<button class="button ${action === "remove" ? "danger" : "secondary"}" data-action="${action}" ${blocked ? `disabled title="${esc(blocked)}"` : `title="${esc(actionDescriptions[action])}"`}>${actionNames[action]}</button>`;
        })
        .join("")}</div></div>`,
  ).join("");
  dialog(
    row.name,
    `<p class="dialog-text">${facts.map(esc).join("\n")}</p>${row.containers ? `<div class="health-description">${row.containers.map((c) => `${esc(c.name)} · ${esc(c.state)}${c.health ? " / " + esc(c.health) : ""}`).join("<br>")}</div>` : ""}${groups}<div class="action-grid"><button id="show-logs" class="button secondary">최근 로그 보기</button></div>${row.target === "tailscaled.service" ? '<p class="health-description">연결을 끊는 작업은 PC의 터미널에서 실행하세요.</p>' : ""}`,
  );
  $("dialog-content")
    .querySelectorAll("[data-action]")
    .forEach((b) =>
      b.addEventListener("click", () => confirmAction(row, b.dataset.action)),
    );
  $("show-logs").addEventListener("click", () => showLogs(row));
}
// After start/stop/restart the unit can sit in activating/deactivating for a
// while; re-read until it settles (max ~30 s) instead of showing a frozen
// transitional state until the next automatic refresh.
const TRANSITIONAL = ["activating", "deactivating", "reloading"];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function settleService(id) {
  for (let i = 0; i < 12; i++) {
    const row = serviceData.services.find((s) => s.id === id);
    if (!row || !TRANSITIONAL.includes(row.state)) return row;
    $("last-updated").textContent = `${row.name} 상태 확인 중…`;
    await sleep(2500);
    await refresh(true);
  }
  return serviceData.services.find((s) => s.id === id);
}
function confirmAction(row, action) {
  dialog(
    `${row.name} · ${actionNames[action]}`,
    `<p class="dialog-text">${esc(actionDescriptions[action])}${row.notice ? "\n\n" + esc(row.notice) : ""}</p><div class="dialog-actions"><button id="cancel-action" class="button secondary">취소</button><button id="confirm-action" class="button ${action === "remove" || action === "disable" ? "danger" : "primary"}">${actionNames[action]}</button></div>`,
  );
  $("cancel-action").addEventListener("click", () => manageService(row.id));
  $("confirm-action").addEventListener("click", async () => {
    busyAction = true;
    $("confirm-action").disabled = true;
    $("cancel-action").disabled = true;
    $("dialog-close").disabled = true;
    $("confirm-action").textContent = "적용 중…";
    try {
      const data = await api(
        `/api/services/${encodeURIComponent(row.id)}/action`,
        { action, confirmed: true },
      );
      $("dialog").close();
      notify(data.message, true);
    } catch (error) {
      notify(error.message);
      $("dialog").close();
    } finally {
      busyAction = false;
      $("dialog-close").disabled = false;
      await refresh(true);
      await settleService(row.id);
    }
  });
}
function bindKillButtons(root) {
  root
    .querySelectorAll("[data-kill]")
    .forEach((b) =>
      b.addEventListener("click", () =>
        confirmKill(Number(b.dataset.kill), b.dataset.label || "대상"),
      ),
    );
}
// SIGTERM is a request: 10 s later, re-read and say which trees survived so
// the user knows to escalate from the terminal.
let killFollowUpMs = 10000;
function followUpKill(pids) {
  setTimeout(async () => {
    try {
      monitorData = await api("/api/monitor?force=1");
    } catch {
      return;
    }
    const alive = new Set([...monitorData.sessions, ...monitorData.mcp].map((r) => r.root_pid));
    const left = pids.filter((pid) => alive.has(pid));
    if (left.length)
      notify(`아직 실행 중: PID ${left.join(", ")}. 응답하지 않으면 PC 터미널에서 wrg stop ${left[0]} --confirm --kill 로 강제 종료하세요.`);
    else notify(`종료를 확인했습니다 (PID ${pids.join(", ")}).`, true);
    render();
    updateBadges();
  }, killFollowUpMs);
}
function confirmKillStale() {
  const stale = (monitorData?.sessions || []).filter((r) => r.stale && r.killable !== false);
  if (!stale.length) return;
  dialog(
    `오래된 세션 ${stale.length}개 종료`,
    `<p class="dialog-text">아래 세션 트리에 SIGTERM을 보냅니다. 실행 직전에 서버가 다시 확인해, 그사이 활동을 재개한 세션은 건너뜁니다.</p><div class="health-description">${stale.map((r) => `${esc(r.provider)} · ${esc(r.project)} · PID ${num(r.root_pid)} · ${giB(r.rss_kib)} GiB · ${age(r.age_seconds)}`).join("<br>")}</div><div class="dialog-actions"><button id="cancel-action" class="button secondary">취소</button><button id="confirm-action" class="button danger">모두 종료</button></div>`,
  );
  $("cancel-action").addEventListener("click", () => $("dialog").close());
  $("confirm-action").addEventListener("click", async () => {
    busyAction = true;
    $("confirm-action").disabled = true;
    $("cancel-action").disabled = true;
    try {
      const data = await api("/api/sessions/kill-stale", { pids: stale.map((r) => r.root_pid), confirmed: true });
      $("dialog").close();
      notify(data.message, true);
      if (data.killed?.length) followUpKill(data.killed);
    } catch (error) {
      notify(error.message);
      $("dialog").close();
    } finally {
      busyAction = false;
      await refresh(true);
    }
  });
}
function confirmKill(pid, label) {
  // data-kill carries the raw integer; a locale-formatted "12,345" would become NaN.
  if (!Number.isSafeInteger(pid) || pid <= 1) {
    notify("종료할 PID를 확인할 수 없습니다. 화면을 새로고침하세요.");
    return;
  }
  dialog(
    `${label} · 종료`,
    `<p class="dialog-text">PID ${num(pid)} 트리의 모든 프로세스에 SIGTERM을 보냅니다.\n강제 종료(SIGKILL)는 PC 터미널의 wrg stop에서만 가능하며, 종료된 세션은 자동으로 다시 시작되지 않습니다.</p><div class="dialog-actions"><button id="cancel-action" class="button secondary">취소</button><button id="confirm-action" class="button danger">종료</button></div>`,
  );
  $("cancel-action").addEventListener("click", () => $("dialog").close());
  $("confirm-action").addEventListener("click", async () => {
    busyAction = true;
    $("confirm-action").disabled = true;
    $("cancel-action").disabled = true;
    $("dialog-close").disabled = true;
    $("confirm-action").textContent = "적용 중…";
    try {
      const data = await api(`/api/sessions/${pid}/kill`, { confirmed: true });
      $("dialog").close();
      notify(data.message, true);
      followUpKill([pid]);
    } catch (error) {
      notify(error.message);
      $("dialog").close();
    } finally {
      busyAction = false;
      $("dialog-close").disabled = false;
      await refresh(true);
    }
  });
}
async function showLogs(row) {
  dialog(
    `${row.name} · 최근 로그`,
    '<div class="log-toolbar"><select id="log-lines" class="search" aria-label="로그 줄 수"><option value="50">최근 50줄</option><option value="200" selected>최근 200줄</option><option value="500">최근 500줄</option><option value="1000">최근 1000줄</option></select><input id="log-filter" class="search" placeholder="로그 필터" aria-label="로그 필터"><label class="log-follow"><input id="log-follow" type="checkbox" checked> 자동 스크롤</label><button id="refresh-logs" class="button compact secondary" title="로그 새로고침">↻</button><button id="copy-logs" class="button compact secondary">복사</button></div><pre id="log-content" class="log">불러오는 중…</pre>',
  );
  let rawText = "";
  const applyFilter = () => {
    const needle = ($("log-filter")?.value || "").trim().toLowerCase();
    const shown = needle
      ? rawText
          .split("\n")
          .filter((line) => line.toLowerCase().includes(needle))
          .join("\n")
      : rawText;
    const pre = $("log-content");
    pre.textContent = shown || "필터와 일치하는 로그가 없습니다.";
    if ($("log-follow")?.checked) pre.scrollTop = pre.scrollHeight;
  };
  const fetchLogs = async () => {
    try {
      const lines = $("log-lines")?.value || "200";
      const data = await api(
        `/api/services/${encodeURIComponent(row.id)}/logs?lines=${lines}`,
      );
      rawText = data.text || "기록된 로그가 없습니다.";
      applyFilter();
    } catch (error) {
      rawText = "";
      $("log-content").textContent = error.message;
    }
  };
  $("refresh-logs").addEventListener("click", fetchLogs);
  $("log-lines").addEventListener("change", fetchLogs);
  $("log-filter").addEventListener("input", applyFilter);
  $("copy-logs").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(rawText);
      notify("로그를 복사했습니다.", true);
    } catch {
      notify("클립보드 복사가 차단됐습니다.");
    }
  });
  await fetchLogs();
}
async function registerDialog() {
  dialog(
    "서비스 등록",
    '<p class="muted">이 PC의 서비스 목록을 검색하고 있습니다…</p>',
  );
  try {
    const candidates = await api("/api/discover");
    $("dialog-content").innerHTML =
      '<p class="dialog-text">현재 등록된 systemd 서비스나 기존 Docker Compose 프로젝트를 가져옵니다. 현재 실행 상태를 유지합니다.</p><input id="candidate-search" class="search full" placeholder="서비스 이름, 프로젝트 검색" aria-label="등록 대상 검색"><label>표시 이름 (선택)<input id="register-name" class="search full" maxlength="80" placeholder="기본 서비스 이름 사용"></label><label>접속 URL (선택)<input id="register-url" type="url" class="search full" placeholder="https://..."></label><div id="candidate-list"></div>';
    const renderCandidates = () => {
      const filtered = candidates.filter((c) =>
        matches(c, $("candidate-search").value, ["name", "kind", "target"]),
      );
      $("candidate-list").innerHTML =
        filtered
          .map(
            (c) =>
              `<div class="candidate"><div><strong>${esc(c.name)}</strong><small>${esc(c.kind)} · ${esc(c.target)}</small></div><button class="button secondary" data-key="${esc(c.key)}">등록</button></div>`,
          )
          .join("") ||
        '<div class="empty">등록할 항목이 없습니다. 새 앱은 먼저 PC에서 서비스나 Compose 프로젝트로 생성하세요.</div>';
      $("candidate-list")
        .querySelectorAll("[data-key]")
        .forEach((b) =>
          b.addEventListener("click", async () => {
            b.disabled = true;
            busyAction = true;
            try {
              const result = await api("/api/services/register", {
                key: b.dataset.key,
                ...($("register-name").value.trim()
                  ? { name: $("register-name").value.trim() }
                  : {}),
                ...($("register-url").value.trim()
                  ? { url: $("register-url").value.trim() }
                  : {}),
              });
              $("dialog").close();
              notify(result.message, true);
            } catch (error) {
              notify(error.message);
            } finally {
              busyAction = false;
              await refresh(true);
            }
          }),
        );
    };
    $("candidate-search").addEventListener("input", renderCandidates);
    renderCandidates();
  } catch (error) {
    $("dialog-content").textContent = error.message;
  }
}
document
  .querySelectorAll("[data-view]")
  .forEach((b) => b.addEventListener("click", () => navigate(b.dataset.view)));
$("refresh").addEventListener("click", () => refresh(true));
$("refresh-interval").addEventListener("change", () => {
  try {
    localStorage.setItem("wrg-refresh", $("refresh-interval").value);
  } catch {}
  schedule();
});
document.addEventListener("visibilitychange", () => {
  schedule();
  if (
    !document.hidden &&
    Number($("refresh-interval").value) &&
    Date.now() - lastRefresh > Number($("refresh-interval").value) * 1000
  )
    refresh();
});
$("dialog-close").addEventListener("click", () => {
  if (!busyAction) $("dialog").close();
});
$("dialog").addEventListener("cancel", (e) => {
  if (busyAction) e.preventDefault();
});
$("register").addEventListener("click", registerDialog);
for (const [id, event, fn] of [
  ["top-search", "input", () => renderTop()],
  ["session-search", "input", () => renderSessions()],
  ["session-stale", "change", () => renderSessions()],
  ["mcp-search", "input", () => renderMcp()],
  ["mcp-hours", "input", () => renderMcp()],
])
  $(id).addEventListener(event, () => {
    fn();
    syncHash();
  });
$("tab-more").addEventListener("click", openMoreMenu);
$("history-custom").addEventListener("click", customRangeDialog);
$("kill-stale").addEventListener("click", confirmKillStale);
$("settings-changed-only").addEventListener("change", (e) => {
  settingsChangedOnly = e.target.checked;
  renderSettings();
});
document.querySelectorAll(".range-picker [data-range]").forEach((b) =>
  b.addEventListener("click", () => {
    if (b.dataset.range === historyRange && !historyWindow) return;
    historyRange = b.dataset.range;
    historyWindow = null;
    historyExpanded = false;
    try {
      localStorage.setItem("wrg-history-range", historyRange);
    } catch {}
    loaded.history = false;
    historyData = null;
    attributionData = null;
    syncHash();
    renderHistory();
    refresh(true);
  }),
);
window.addEventListener("hashchange", () => {
  const { view, params } = parseHash();
  if (view !== currentView || location.hash !== "#" + hashFor(currentView))
    navigate(view, false, params);
});
// Live summary over SSE: badges, title, and favicon follow the guard within
// seconds; a severity or reason change also refreshes the open view. The
// regular polling stays as the fallback (the server caps live streams).
let stream = null,
  liveSignature = "";
function invalidateLiveSummary() {
  liveSummary = null;
  liveSignature = "";
  document.body.classList.remove("live");
  updateBadges();
  updateAlertBadge();
}
function connectStream() {
  if (stream || !window.EventSource || document.hidden) return;
  const source = stream = new EventSource("/api/stream");
  source.addEventListener("summary", (e) => {
    if (source !== stream) return;
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      invalidateLiveSummary();
      return;
    }
    if (!data || typeof data !== "object" || Array.isArray(data) || data.error) {
      invalidateLiveSummary();
      return;
    }
    if (liveSummary && Number(data.updated_at || 0) < Number(liveSummary.updated_at || 0)) return;
    liveSummary = data;
    document.body.classList.add("live");
    const signature = `${data.severity}|${(data.reasons || []).join("|")}|${data.service_problems}|${(data.disk_alerts || []).join()}`;
    const changed = liveSignature && signature !== liveSignature;
    liveSignature = signature;
    updateBadges();
    updateAlertBadge();
    if (changed && !busyAction) refresh();
  });
  source.addEventListener("error", () => {
    if (source !== stream) return;
    // Each 50 s window ends with an error event and an automatic reconnect;
    // only a refused stream (503) closes for good, and polling carries on.
    invalidateLiveSummary();
    if (source.readyState === EventSource.CLOSED) stream = null;
  });
}
function disconnectStream() {
  stream?.close();
  stream = null;
  invalidateLiveSummary();
}
document.addEventListener("visibilitychange", () => (document.hidden ? disconnectStream() : connectStream()));
// Keyboard: g+<key> jumps to a view, / focuses the view's search, r
// refreshes, ? lists everything. Ignored while typing or in a dialog.
const GO_KEYS = { o: "overview", s: "services", d: "disks", t: "top", l: "sessions", m: "mcp", h: "history", a: "alerts", c: "settings", w: "audit" };
const SEARCH_FOR = { top: "top-search", sessions: "session-search", mcp: "mcp-search" };
let goPending = 0;
function showShortcuts() {
  const rows = [
    ["?", "이 도움말"],
    ["r", "지금 새로고침"],
    ["/", "현재 화면 검색창으로 이동"],
    ...Object.entries(GO_KEYS).map(([k, v]) => [`g ${k}`, titles[v][0]]),
    ["Esc", "대화상자 닫기"],
  ];
  dialog("단축키", `<div class="shortcuts">${rows.map(([k, label]) => `<span>${k.split(" ").map((x) => `<kbd>${esc(x)}</kbd>`).join(" ")}</span><span>${esc(label)}</span>`).join("")}</div>`);
}
$("shortcut-help").addEventListener("click", showShortcuts);
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || $("dialog").open) return;
  if (e.target.closest("input, select, textarea, [contenteditable]")) return;
  if (goPending > Date.now() && GO_KEYS[e.key]) {
    goPending = 0;
    e.preventDefault();
    return navigate(GO_KEYS[e.key]);
  }
  goPending = 0;
  if (e.key === "g") goPending = Date.now() + 1500;
  else if (e.key === "r") refresh(true);
  else if (e.key === "?") showShortcuts();
  else if (e.key === "/" && SEARCH_FOR[currentView]) {
    e.preventDefault();
    $(SEARCH_FOR[currentView]).focus();
  }
});
// Re-render charts when the layout width changes so the viewBox tracks the
// container instead of scaling text (U2). Debounced; only chart views.
let resizeTimer = null;
const mainEl = document.querySelector("main");
if (window.ResizeObserver && mainEl)
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (["services", "history", "disks"].includes(currentView)) render();
    }, 250);
  }).observe(mainEl);
(async () => {
  try {
    const info = await api("/api/bootstrap");
    csrf = info.csrf;
    $("identity").textContent = info.login;
    const { view, params } = parseHash();
    navigate(view, true, params);
    schedule();
    connectStream();
    registerServiceWorker();
  } catch (error) {
    notify(error.message);
    $("loading").classList.add("hidden");
  }
})();

function diskMeter(disk) {
  if (disk.total_bytes == null)
    return '<div class="disk-meter empty-meter"></div>';
  const level = disk.status !== "ok" ? "unknown" : disk.severity;
  return `<div class="disk-meter ${esc(level)}" role="img" aria-label="사용 ${Number(disk.used_percent).toFixed(1)}%, 예약 ${(((disk.reserved_bytes || 0) / disk.total_bytes) * 100).toFixed(1)}%"><span class="disk-used" data-percent="${Number(disk.used_percent)}"></span><span class="disk-reserved" data-percent="${((disk.reserved_bytes || 0) / disk.total_bytes) * 100}"></span></div>`;
}
function insightText(info, monitored = true) {
  const parts = [];
  if (info.daily_delta_gib != null)
    parts.push(
      Math.abs(info.daily_delta_gib) < 0.05
        ? "하루 전과 여유 비슷함"
        : info.daily_delta_gib >= 0
          ? `하루 전보다 여유 ${Number(info.daily_delta_gib).toFixed(1)} GiB 늘어남`
          : `하루 전보다 여유 ${Number(-info.daily_delta_gib).toFixed(1)} GiB 줄어듦`,
    );
  if (info.gib_per_day > 0.05)
    parts.push(`소모 ${Number(info.gib_per_day).toFixed(1)} GiB/일`);
  if (monitored && info.recent_gib_per_hour) {
    parts.push(`최근 ${Number(info.recent_gib_per_hour).toFixed(1)} GiB/시간 소모`);
    // Past three days, hours read better as days ("약 23일", not "약 554시간").
    const span = (h) => (h > 72 ? `${(h / 24).toFixed(0)}일` : `${Number(h).toFixed(0)}시간`);
    if (info.hours_to_warning != null) parts.push(`경고까지 약 ${span(info.hours_to_warning)}`);
    else if (info.hours_to_critical != null) parts.push(`위험까지 약 ${span(info.hours_to_critical)}`);
  } else if (monitored) {
    if (info.days_to_warning != null)
      parts.push(`경고까지 약 ${Number(info.days_to_warning).toFixed(0)}일`);
    else if (info.days_to_critical != null)
      parts.push(`위험까지 약 ${Number(info.days_to_critical).toFixed(0)}일`);
  }
  if (info.vhd_reclaim_gib)
    parts.push(
      `VHDX 정리로 약 ${Number(info.vhd_reclaim_gib).toFixed(0)} GiB 회수 가능`,
    );
  return parts.join(" · ");
}
function diskRow(disk, healthy = false) {
  const status =
    disk.status !== "ok"
      ? pill("unknown")
      : disk.kind === "wsl"
        ? '<span class="pill">내부 파일시스템</span>'
        : pill(disk.severity);
  const numbers =
    disk.total_bytes == null
      ? '<span class="muted">용량 조회 불가</span>'
      : `<strong>${memory(disk.used_bytes)}</strong><span class="muted"> / ${memory(disk.total_bytes)} 사용</span>`;
  return `<div class="disk-row${healthy ? " disk-ok" : ""}"><div class="disk-row-title"><h3>${esc(disk.name)}</h3>${status}</div><div class="disk-row-value">${numbers}</div>${diskMeter(disk)}<div class="disk-row-footer"><span>${disk.available_bytes == null ? "—" : `여유 <b>${memory(disk.available_bytes)}</b> (${Number(disk.available_percent).toFixed(1)}%)`}</span><span>${disk.observed_at ? when(disk.observed_at) : "측정값 없음"}</span></div>${disk.insight ? `<p class="disk-note">${esc(insightText(disk.insight, disk.kind === "windows"))}</p>` : ""}${disk.status !== "ok" ? `<p class="row-error">${esc(disk.error)}${disk.observed_at ? " · 마지막 확인값 표시" : ""}</p>` : ""}</div>`;
}
// Phones show only drives needing attention; the rest fold behind a toggle.
let diskOverviewExpanded = false;
const diskNeedsAttention = (disk) =>
  disk.kind === "windows" && (disk.status !== "ok" || ["warning", "critical"].includes(disk.severity));
function diskOverview() {
  if (!diskData) return "";
  const healthy = diskData.disks.filter((disk) => !diskNeedsAttention(disk)).length;
  const toggle = healthy
    ? `<button class="button secondary disk-more" type="button" aria-expanded="${diskOverviewExpanded}">${diskOverviewExpanded ? "정상 볼륨 접기" : `정상 볼륨 ${healthy}개 더 보기`}</button>`
    : "";
  return `<div class="panel${diskOverviewExpanded ? " expanded" : ""}" id="disk-overview"><div class="panel-heading"><h2>저장 공간</h2><button class="button secondary" data-navigate="disks">Disk 상세 →</button></div><div class="disk-grid">${diskData.disks.map((disk) => diskRow(disk, !diskNeedsAttention(disk))).join("")}</div>${toggle}<div class="disk-footnote">WSL 저장 공간은 Windows 저장 드라이브 사용량에 포함됩니다. 위 수치들을 합산하지 않습니다.</div></div>`;
}
function diskChart(history, disks) {
  if (!history.length)
    return '<div class="empty">디스크 이력이 쌓이면 일별 변화가 표시됩니다.</div>';
  const colors = Array.from({ length: 8 }, (_, i) => `var(--chart-${i + 1})`);
  // Track the rendered width like lineChart so text stays readable on
  // narrow (mobile) screens instead of shrinking with the scale factor (U2).
  const host = document.querySelector(".view:not(.hidden)");
  const width = Math.max(360, Math.min(920, Math.floor((host ? host.clientWidth : 968) - 48) || 920)),
    narrow = width < 560,
    height = narrow ? 260 : 245,
    left = narrow ? 44 : 52,
    right = narrow ? 16 : 24,
    bottom = 40,
    top = 18;
  const max = Math.max(
    1,
    ...history.flatMap((h) =>
      h.disks.map((d) => Number(d.total_bytes || 0) / 2 ** 30),
    ),
  );
  const x = (index) =>
    left +
    (history.length === 1 ? 0.5 : index / (history.length - 1)) *
      (width - left - right);
  const y = (value) =>
    height - bottom - (value / max) * (height - top - bottom);
  // Missing or failed samples stay missing: a gap in the line, "—" in the tooltip.
  const usedGiB = (h, disk) => {
    const point = h.disks.find((d) => d.id === disk.id);
    return !point || point.status !== "ok" || point.used_bytes == null
      ? null
      : point.used_bytes / 2 ** 30;
  };
  const series = disks.map((disk, i) => ({
    name: disk.name,
    color: colors[i % colors.length],
    value: (h) => usedGiB(h, disk),
  }));
  const seq = ++chartSeq;
  chartDataPending[seq] = {
    rows: history.map((h) => ({ metrics: h })),
    series,
    unit: "GiB",
    geo: { left, right, top, bottom, w: width, h: height, max },
    xAt: x,
    labelAt: (i) => history[i].date,
  };
  let svg = "";
  for (let step = 0; step < 5; step++) {
    const value = (max * step) / 4,
      yy = y(value);
    svg += `<line x1="${left}" x2="${width - right}" y1="${yy}" y2="${yy}" stroke="var(--line)"/><text x="${left - 9}" y="${yy + 4}" text-anchor="end">${value.toFixed(0)}</text>`;
  }
  svg += `<line class="chart-cursor" x1="0" x2="0" y1="${top}" y2="${height - bottom}" visibility="hidden"/>`;
  series.forEach((s) => {
    let path = "",
      connected = false;
    history.forEach((h, index) => {
      const v = s.value(h);
      if (v == null) {
        connected = false;
        return;
      }
      const xx = x(index),
        yy = y(v);
      path += `${connected ? "L" : "M"}${xx.toFixed(1)},${yy.toFixed(1)} `;
      connected = true;
      svg += `<circle cx="${xx.toFixed(1)}" cy="${yy.toFixed(1)}" r="3" fill="${s.color}"/>`;
    });
    svg += `<path d="${path}" stroke="${s.color}" stroke-width="2" fill="none"/>`;
  });
  svg += series
    .map((s) => `<circle class="chart-dot" r="4" stroke="${s.color}" visibility="hidden"/>`)
    .join("");
  const every = Math.max(1, Math.ceil(history.length / (narrow ? 4 : 7)));
  const lastIndex = history.length - 1;
  const labeled = history.map((_, i) => i % every === 0);
  if (!labeled[lastIndex]) {
    // Always show the latest day; drop the stride label right before it if crowded.
    const prev = lastIndex - (lastIndex % every);
    if (prev > 0 && lastIndex - prev < every / 2) labeled[prev] = false;
    labeled[lastIndex] = true;
  }
  history.forEach((h, i) => {
    if (!labeled[i]) return;
    const anchor = lastIndex === 0 ? "middle" : i === 0 ? "start" : i === lastIndex ? "end" : "middle";
    svg += `<text x="${x(i)}" y="${height - 13}" text-anchor="${anchor}">${esc(h.date.slice(5))}</text>`;
  });
  return `<svg class="chart" data-chart="${seq}" viewBox="0 0 ${width} ${height}" role="img" aria-label="일별 마지막 디스크 사용량 GiB">${svg}</svg><div class="chart-tip"></div><div class="disk-legend">${disks.map((d, i) => `<span class="series-${i % 8}">● ${esc(d.name)}</span>`).join("")}</div>`;
}
function renderDisks() {
  if (!loaded.disks) {
    $("view-disks").innerHTML = '<div class="panel empty">불러오는 중…</div>';
    return;
  }
  if (!diskData) {
    $("view-disks").innerHTML =
      '<div class="panel empty">저장 공간 정보를 받지 못했습니다.</div>';
    return;
  }
  const windows = diskData.disks.filter((d) => d.kind === "windows");
  const wsl = diskData.disks.find((d) => d.kind === "wsl");
  const vhd = wsl?.vhd;
  const host = diskData.disks.find((d) => d.id === vhd?.host_drive);
  const thresholds = diskData.thresholds;
  const historyRows = [...diskData.history]
    .reverse()
    .flatMap((h) =>
      h.disks.map((d) => [
        esc(h.date),
        esc(d.name),
        d.status === "ok" ? memory(d.used_bytes) : "조회 불가",
        d.status === "ok" && d.available_percent != null
          ? `${memory(d.available_bytes)} (${Number(d.available_percent).toFixed(1)}%)`
          : "—",
        when(d.observed_at),
      ]),
    );
  $("view-disks").innerHTML =
    `<div class="panel"><div class="panel-heading"><h2>Windows 볼륨</h2><span class="muted">${diskData.sample_interval_seconds}초 간격 수집 · 수동 갱신 가능</span></div><div class="disk-grid windows-disks">${windows.map(diskRow).join("")}</div><div class="disk-footnote">남은 공간 ${thresholds.warning_free_percent}% 미만 Warning · ${thresholds.critical_free_percent}% 미만 Critical</div></div>
  ${wsl ? `<div class="panel"><div class="panel-heading"><h2>WSL 저장 공간</h2><span class="pill">${esc(vhd?.host_drive || "저장 위치 미설정")}에 저장</span></div><div class="wsl-storage"><div>${diskRow(wsl)}<p class="disk-note">파일시스템 예약 공간 ${memory(wsl.reserved_bytes)} · 회색 막대는 예약 공간입니다.<br>WSL 내부 여유 비율은 표시용이며, 현재 경보 대상은 C/D/E입니다.</p></div><div class="vhd-info"><h3>Windows의 VHDX 파일</h3><div class="metric-value">${vhd?.file_bytes != null ? memory(vhd.file_bytes) : "조회 불가"}</div><p class="disk-path">${esc(vhd?.path || "config.toml에서 wsl_vhd_path를 설정하세요.")}</p><p class="health-description">${vhd?.status === "error" ? esc(vhd.error) : "WSL 내부 파일 사용량과 VHDX 파일 크기는 다릅니다. 파일 크기 차이가 즉시 회수 가능한 공간을 뜻하지는 않습니다."}</p>${vhd?.path ? '<button class="button secondary" data-vhd-guide>VHDX 정리 방법 보기</button>' : ""}${host?.available_bytes != null ? `<div class="host-space">저장 드라이브 ${esc(host.id)}의 여유 <strong>${memory(host.available_bytes)}</strong><small>${host.status !== "ok" ? "마지막 확인값 · 현재 조회 불가" : `남은 공간 ${Number(host.available_percent).toFixed(1)}%`}</small></div>` : ""}</div></div></div>` : ""}
  ${dockerPanel()}
  <div class="panel"><div class="panel-heading"><h2>일별 사용량 변화</h2><span class="muted">GiB · 일별 마지막 측정값 · 합산하지 않음</span></div><div class="panel-body">${diskChart(diskData.history, diskData.disks)}</div></div>
  <div class="panel"><div class="panel-heading"><h2>일별 측정 기록</h2><span class="muted">설치 이후 수집된 기록</span></div><div class="table-wrap">${table(["날짜", "볼륨", "사용 중", "여유 공간", "측정 시각"], historyRows)}</div></div>`;
  paintBars($("view-disks"));
  bindCharts($("view-disks"));
}
// `docker system df`, read-only: shows where Docker's share of the disk
// went. Pruning stays a terminal action.
function dockerPanel() {
  if (!dockerDfData) return "";
  const body = !dockerDfData.available
    ? `<div class="empty">${esc(dockerDfData.error || "Docker 사용량을 확인할 수 없습니다.")}</div>`
    : table(
        ["종류", { t: "개수", cls: "num" }, { t: "사용 중", cls: "num" }, { t: "크기", cls: "num" }, { t: "회수 가능", cls: "num" }],
        dockerDfData.rows.map((r) => [
          `<strong>${esc({ Images: "이미지", Containers: "컨테이너", "Local Volumes": "볼륨", "Build Cache": "빌드 캐시" }[r.Type] || r.Type)}</strong>`,
          esc(r.TotalCount),
          esc(r.Active),
          esc(r.Size),
          esc(r.Reclaimable),
        ]),
      );
  return `<div class="panel" id="docker-df"><div class="panel-heading"><h2>Docker 사용량</h2><span class="muted">읽기 전용 · 정리는 PC 터미널에서 <code>docker system prune</code> 등으로 직접 실행 · ${dockerDfData.checked_at ? `확인 ${when(dockerDfData.checked_at)}` : ""}</span></div><div class="table-wrap">${body}</div></div>`;
}
// /mnt/e/WSL/x.vhdx -> E:\WSL\x.vhdx for the Windows-side commands.
function windowsPath(path) {
  const m = /^\/mnt\/([a-z])\/(.*)$/i.exec(path || "");
  return m ? `${m[1].toUpperCase()}:\\${m[2].replaceAll("/", "\\")}` : path || "";
}
// Copyable, ordered steps only — nothing here runs on the PC (D: no remote
// shutdown of WSL, which would also cut this dashboard).
function showVhdGuide() {
  const wsl = (diskData?.disks || []).find((d) => d.kind === "wsl");
  const path = windowsPath(wsl?.vhd?.path);
  const reclaim = wsl?.insight?.vhd_reclaim_gib;
  const step = (title, note, command) =>
    `<li><strong>${esc(title)}</strong>${note ? `<span class="subline">${esc(note)}</span>` : ""}${command ? `<div class="guide-cmd"><pre>${esc(command)}</pre><button class="button compact secondary" data-copy="${esc(command)}">복사</button></div>` : ""}</li>`;
  dialog(
    "VHDX 정리 방법",
    `<p class="dialog-text">${reclaim ? `현재 약 ${Number(reclaim).toFixed(0)} GiB를 회수할 수 있는 것으로 보입니다.\n` : ""}아래 단계는 이 화면에서 실행되지 않습니다. WSL을 끄면 이 대시보드와 모든 WSL 서비스도 함께 중단됩니다.</p><ol class="guide">${[
      step("WSL 안에서 빈 블록을 반환합니다", "삭제된 파일 자리를 VHDX가 알 수 있게 합니다. 이 단계를 건너뛰면 압축 효과가 작습니다.", "sudo fstrim -av"),
      step("작업을 저장하고 Windows PowerShell에서 WSL을 종료합니다", "실행 중인 세션·컨테이너·웹 서비스가 모두 멈춥니다.", "wsl --shutdown"),
      step("관리자 PowerShell에서 diskpart로 압축합니다", "한 줄씩 입력합니다. 수 분 이상 걸릴 수 있습니다.", `diskpart\nselect vdisk file="${path}"\nattach vdisk readonly\ncompact vdisk\ndetach vdisk\nexit`),
      step("WSL을 다시 시작합니다", "등록된 서비스는 부팅 복구가 자동 실행 설정대로 되살립니다.", "wsl"),
      step("다음부터 자동으로 줄이려면 (선택)", "WSL 2.0 이상의 sparse VHD 옵션입니다. 배포판 이름은 wsl -l -v로 확인합니다.", "wsl --manage <배포판> --set-sparse true"),
    ].join("")}</ol>`,
  );
}
document.addEventListener("click", async (e) => {
  if (e.target.closest("[data-vhd-guide]")) return showVhdGuide();
  const copy = e.target.closest("[data-copy]");
  if (!copy) return;
  try {
    await navigator.clipboard.writeText(copy.dataset.copy);
    copy.textContent = "복사됨";
  } catch {
    notify("클립보드 복사가 차단됐습니다. 명령을 직접 선택해 복사하세요.");
  }
});
