const ACCOUNTS = [
  "research_sim_100k",
  "live_manual_10k",
  "shadow_ctrl_sim",
  "shadow_ta_sim",
];
// 四线对比页：研究 / 实盘 / 对照影子 / TA 影子
const COMPARE_ACCOUNTS = [
  "research_sim_100k",
  "live_manual_10k",
  "shadow_ctrl_sim",
  "shadow_ta_sim",
];
const ACCOUNT_SHORT = {
  research_sim_100k: "研究模拟线",
  live_manual_10k: "实盘线",
  shadow_ctrl_sim: "对照影子线",
  shadow_ta_sim: "TA影子线",
};
const charts = {};
const fmt = (v, d = 2) => (v == null || isNaN(v)) ? "-" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 2) => (v == null || isNaN(v)) ? "-" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%";
const cls = v => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const fmtYi = (v, d = 2) => {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(d) + "亿";
};

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

/** 简易 markdown → HTML（行内 + 段落 + 列表），不依赖外部库 */
function mdToHtml(text) {
  if (!text) return "";
  let s = String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    // headings ### / ## / #
    .replace(/^### (.+)$/gm, "<h5>$1</h5>")
    .replace(/^## (.+)$/gm, "<h4>$1</h4>")
    .replace(/^# (.+)$/gm, "<h3>$1</h3>")
    // bold / italic / code
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
  // 分段：双换行 → <p>，单换行 → <br>
  const blocks = s.split(/\n{2,}/);
  return blocks.map(b => {
    b = b.trim();
    if (!b) return "";
    if (b.startsWith("<h") || b.startsWith("<ul") || b.startsWith("<ol")) return b;
    // 无序列表：- item 或 * item
    if (/^[-*] /.test(b)) {
      const items = b.split(/\n/).map(l => `<li>${l.replace(/^[-*] /, "")}</li>`).join("");
      return `<ul>${items}</ul>`;
    }
    // 有序列表：1. item
    if (/^\d+\.\s/.test(b)) {
      const items = b.split(/\n/).map(l => `<li>${l.replace(/^\d+\.\s/, "")}</li>`).join("");
      return `<ol>${items}</ol>`;
    }
    // 普通段落
    return `<p>${b.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

/** 格式化秒数 → 可读时长 */
function fmtDur(sec) {
  if (sec == null) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

function mkChart(canvas, cfg) {
  if (!canvas) return;
  const key = canvas.id || canvas.dataset.k;
  if (charts[key]) charts[key].destroy();
  charts[key] = new Chart(canvas, cfg);
}

/* Glance · 暗底 Wire：灰阶承载结构，红/绿只编码涨跌，橙只给一个主角。 */
const GLANCE = {
  ink: "#ebe8e0",
  muted: "#8f8e88",
  faint: "#6a6963",
  grid: "rgba(235,232,224,0.08)",
  hero: "#f5572f",
  paper: "#12110f",
  font: 'Inter, "PingFang SC", "Microsoft YaHei", ui-sans-serif, sans-serif',
};
const COLORS = {
  research: GLANCE.ink,
  live: GLANCE.hero,
  bench: GLANCE.faint,
  green: "#2ea043",
  red: "#e5534b",
  shadow_ctrl: "#b0afa9",
  shadow_ta: "#8f8e88",
  amber: "#d4a017",
};
const ACCOUNT_COLORS = {
  research_sim_100k: COLORS.research,
  live_manual_10k: COLORS.live,
  shadow_ctrl_sim: COLORS.shadow_ctrl,
  shadow_ta_sim: COLORS.shadow_ta,
};
const ACCOUNT_LEGEND = "实盘橙 · 研究白 · 对照浅灰 · TA 中灰 · 基准虚线";

function hexAlpha(color, a) {
  if (!color || color[0] !== "#" || color.length < 7) return color;
  const n = Math.round(Math.min(1, Math.max(0, a)) * 255);
  return color.slice(0, 7) + n.toString(16).padStart(2, "0");
}

const lineDS = (label, data, color, fill = false, extra = {}) => ({
  label,
  data,
  borderColor: color,
  backgroundColor: fill ? hexAlpha(color, 0.14) : "transparent",
  borderWidth: extra.borderWidth ?? (fill ? 2.4 : 2.2),
  pointRadius: 0,
  pointHoverRadius: 4,
  pointHitRadius: 10,
  tension: 0,
  fill,
  borderDash: extra.dash || [],
});

function barDS(label, data, { signed = false, color = GLANCE.ink } = {}) {
  const n = (data || []).length;
  const fat = n <= 14;
  return {
    label,
    data,
    backgroundColor: signed
      ? data.map(v => (v >= 0 ? COLORS.red : COLORS.green))
      : color,
    borderRadius: signed || fat ? 99 : 3,
    borderSkipped: false,
    barPercentage: fat ? 0.52 : 0.78,
    categoryPercentage: 0.86,
  };
}

function lastNonNull(arr) {
  if (!arr) return null;
  for (let i = arr.length - 1; i >= 0; i--) {
    const v = arr[i];
    if (v != null && v !== "" && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}

function setChartCopy(canvas, { title, sub, src } = {}) {
  const el = typeof canvas === "string" ? document.getElementById(canvas) : canvas;
  const box = el?.closest?.(".chart-box");
  if (!box) return;
  const h = box.querySelector("h2, h3");
  const subEl = box.querySelector(".chart-sub");
  const srcEl = box.querySelector(".chart-src");
  if (title && h) h.textContent = title;
  if (sub != null && subEl) subEl.textContent = sub;
  if (src != null && srcEl) srcEl.textContent = src;
}

const tickFont = { size: 9.5, weight: 600, family: GLANCE.font };
const glanceScales = {
  x: {
    grid: { display: false },
    border: { display: false },
    ticks: { color: GLANCE.muted, maxTicksLimit: 7, padding: 6, font: tickFont },
  },
  y: {
    grid: { color: GLANCE.grid, lineWidth: 1 },
    border: { display: false },
    ticks: { color: GLANCE.muted, maxTicksLimit: 5, padding: 8, font: tickFont },
  },
};
const baseOpts = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  animation: { duration: 800, easing: "easeOutQuart" },
  plugins: {
    legend: { display: false },
    tooltip: {
      backgroundColor: GLANCE.ink,
      titleColor: GLANCE.paper,
      bodyColor: "#5c5b56",
      padding: 12,
      cornerRadius: 12,
      displayColors: false,
      titleFont: { family: GLANCE.font, weight: 700, size: 12 },
      bodyFont: { family: GLANCE.font, size: 12 },
    },
    glanceLastValue: true,
  },
  scales: glanceScales,
};

function withLegend(opts = baseOpts) {
  return {
    ...opts,
    plugins: {
      ...opts.plugins,
      legend: {
        display: true,
        position: "top",
        align: "start",
        labels: {
          color: GLANCE.muted,
          boxWidth: 12,
          boxHeight: 3,
          padding: 14,
          font: { size: 11, weight: 600, family: GLANCE.font },
        },
      },
      glanceLastValue: false,
    },
  };
}

const glanceLastValue = {
  id: "glanceLastValue",
  afterDatasetsDraw(chart) {
    if (chart.options.plugins?.glanceLastValue === false) return;
    const type = chart.config.type;
    if (type === "bar" || type === "bubble" || type === "scatter") return;
    const visible = chart.data.datasets.filter((_, i) => chart.isDatasetVisible(i));
    if (visible.length !== 1) return;
    const ds = chart.data.datasets[0];
    const meta = chart.getDatasetMeta(0);
    if (!meta?.data) return;
    let pt = null, val = null;
    for (let i = ds.data.length - 1; i >= 0; i--) {
      if (ds.data[i] != null && meta.data[i]) { pt = meta.data[i]; val = ds.data[i]; break; }
    }
    if (!pt || val == null || typeof val !== "number") return;
    const ctx = chart.ctx;
    const label = Math.abs(val) >= 100 ? val.toFixed(0) : (Math.abs(val) >= 10 ? val.toFixed(1) : val.toFixed(2));
    ctx.save();
    ctx.font = `700 11px ${GLANCE.font}`;
    ctx.fillStyle = typeof ds.borderColor === "string" ? ds.borderColor : GLANCE.ink;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const w = ctx.measureText(label).width;
    const x = Math.min(pt.x + 8, chart.chartArea.right - w - 2);
    ctx.fillText(label, x, pt.y);
    ctx.restore();
  },
};

(function applyGlanceDefaults() {
  if (typeof Chart === "undefined") return;
  Chart.register(glanceLastValue);
  Chart.defaults.color = GLANCE.muted;
  Chart.defaults.font.family = GLANCE.font;
  Chart.defaults.font.size = 10.5;
  Chart.defaults.borderColor = GLANCE.grid;
  Chart.defaults.elements.line.borderWidth = 2.2;
  Chart.defaults.elements.line.tension = 0;
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.plugins.tooltip.backgroundColor = GLANCE.ink;
  Chart.defaults.plugins.tooltip.titleColor = GLANCE.paper;
  Chart.defaults.plugins.tooltip.bodyColor = "#5c5b56";
  Chart.defaults.plugins.tooltip.padding = 12;
  Chart.defaults.plugins.tooltip.cornerRadius = 12;
  Chart.defaults.plugins.tooltip.displayColors = false;
  Chart.defaults.animation.duration = 800;
  Chart.defaults.animation.easing = "easeOutQuart";
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    Chart.defaults.animation = false;
    baseOpts.animation = false;
  }
})();

let fullAccess = false;

async function loadAccess() {
  try {
    const a = await getJSON("/api/access");
    fullAccess = !!a.full_access;
    applyAccessUI(a);
  } catch (_) {
    fullAccess = false;
    applyAccessUI({ demo_mode: true });
  }
}

function applyAccessUI(access = {}) {
  const demo = !fullAccess;
  const badge = document.getElementById("demo-badge");
  if (badge) {
    badge.classList.toggle("hidden", !demo);
    if (demo && access.client_ip) {
      badge.title = `当前 IP ${access.client_ip}，可加入 configs/webapp.local.yaml`;
    }
  }
  const writeBtns = ["sent-run", "sent-analyze-one", "sent-rerun-one", "swing-run",
                     "rs-run", "rs-analyze-one", "rs-rerun-one", "trk-run", "trk-analyze"];
  writeBtns.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", demo);
  });
  // 刷新按钮演示模式也可用（只读）
  ["refresh", "sent-refresh", "swing-refresh", "rs-refresh"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("hidden");
  });
  const codeInput = document.getElementById("sent-code");
  if (codeInput) {
    codeInput.disabled = demo;
    codeInput.placeholder = demo ? "演示模式：仅可浏览已有报告" : "代码 600519 / SH600519";
  }
  const rsCodeInput = document.getElementById("rs-code");
  if (rsCodeInput) {
    rsCodeInput.disabled = demo;
    rsCodeInput.placeholder = demo ? "演示模式：仅可浏览已有报告" : "代码 600519 / SH600519";
  }
  const visitTitle = document.getElementById("visit-stats-title");
  const visitBox = document.getElementById("visit-stats");
  if (visitTitle) visitTitle.classList.toggle("hidden", demo);
  if (visitBox) visitBox.classList.toggle("hidden", demo);
}

// ----------------- 总览 -----------------
async function loadOverview() {
  loadIndices();  // 异步加载大盘，行情源慢/被限不阻塞主面板
  const ov = await getJSON("/api/overview");
  document.getElementById("data-day").textContent = "数据日: " + (ov.data_day || "-");
  document.getElementById("now").textContent = ov.now;

  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  for (const a of ov.accounts) {
    const days = a.days || 0;
    cards.appendChild(card(
      a.label || a.account,
      days ? fmt(a.nav) + " 元" : "未初始化",
      days ? `${pct((a.cum_ret || 0) * 100)} 收益 · 超额 ${pct((a.cum_excess || 0) * 100)} · ${days}天` : "—",
      days ? (a.cum_ret >= 0 ? "pos" : "neg") : ""
    ));
  }

  // 累计收益对比：两条线交易日不同（研究线开线更早），必须按日期并集对齐坐标轴，
  // 否则短序列会被按下标错位画到最早的几天上（实盘线曾因此"停留在 6 月初"）。
  const series = await Promise.all(ACCOUNTS.map(a => getJSON(`/api/account/${a}/daily`)));
  const labels = [...new Set(series.flatMap(s => s.dates))].sort();
  const alignBy = (dates, values) => {
    const m = new Map(dates.map((d, i) => [d, values[i]]));
    return labels.map(d => (m.has(d) ? m.get(d) : null));
  };
  const ds = [];
  ACCOUNTS.forEach((a, i) => {
    if (!series[i].dates.length) return;
    ds.push(lineDS(
      ACCOUNT_SHORT[a] || a,
      alignBy(series[i].dates, series[i].series.cum_ret),
      ACCOUNT_COLORS[a] || COLORS.research,
    ));
  });
  // 基准仍取研究线（与历史口径一致）
  if (series[0].dates.length) {
    ds.push(lineDS("基准", alignBy(series[0].dates, series[0].series.cum_bench), COLORS.bench, false, { dash: [5, 4], borderWidth: 1.4 }));
  }
  const liveLast = lastNonNull(ds.find(d => d.label === "实盘线")?.data);
  const resLast = lastNonNull(ds.find(d => d.label === "研究模拟线")?.data);
  let ovTitle = "四线累计收益";
  if (liveLast != null && resLast != null) {
    const gap = liveLast - resLast;
    ovTitle = gap >= 0
      ? `实盘领先研究 ${pct(gap)}`
      : `实盘落后研究 ${pct(gap)}`;
  }
  setChartCopy("ovChart", {
    title: ovTitle,
    sub: ACCOUNT_LEGEND,
    src: "CUM RETURN · UNION OF TRADE DAYS · %",
  });
  mkChart(document.getElementById("ovChart"), {
    type: "line",
    data: { labels, datasets: ds },
    options: { ...withLegend(baseOpts), spanGaps: true },
  });
}

function card(label, value, sub, valueCls = "") {
  const d = document.createElement("div");
  d.className = "card";
  d.innerHTML = `<div class="label">${label}</div><div class="value ${valueCls}">${value}</div><div class="sub">${sub || ""}</div>`;
  return d;
}

function sideCls(side) {
  return String(side).toUpperCase() === "BUY" ? "side-buy" : "side-sell";
}
function sideLabel(side) {
  return String(side).toUpperCase() === "BUY" ? "买入" : "卖出";
}
function modeBadge(mode) {
  return mode === "simulated"
    ? '<span class="badge sim">自动模拟</span>'
    : '<span class="badge manual">人工回填</span>';
}
function statusBadge(status, label) {
  return `<span class="badge ${status}">${label}</span>`;
}

function renderDailyOpsPanel(plan, container) {
  if (!plan) {
    container.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const el = document.createElement("div");
  el.className = "ops-panel";
  const execDay = plan.execute_day || "—";
  const orderDay = plan.order_day || "—";
  el.innerHTML = `
    <div class="ops-head">
      <h4>${plan.label || plan.account}</h4>
      ${statusBadge(plan.status, plan.status_label)}
      ${modeBadge(plan.mode)}
    </div>
    <div class="ops-meta">
      订单日 <b>${orderDay}</b> → 执行日 <b>${execDay}</b>
    </div>
    <div class="ops-summary">${plan.summary || ""}</div>
    <div class="ops-grid">
      <div><h5>调仓指令</h5><div class="ops-orders table-wrap"></div></div>
      <div><h5>目标持仓（${plan.target_positions?.length || 0} 只）</h5><div class="ops-target table-wrap"></div></div>
    </div>`;
  container.innerHTML = "";
  container.appendChild(el);
  const ordersBox = el.querySelector(".ops-orders");
  const targetBox = el.querySelector(".ops-target");
  if (!plan.orders?.length) {
    ordersBox.innerHTML = '<div class="empty">无需调仓</div>';
  } else {
    renderTable(ordersBox, plan.orders,
      [["side", "方向"], ["instrument", "标的"], ["shares", "股数"], ["ref_price", "参考价"]],
      "无指令", "instrument");
    ordersBox.querySelectorAll("td").forEach(td => {
      if (td.textContent === "BUY" || td.textContent === "SELL") {
        const s = td.textContent;
        td.textContent = sideLabel(s);
        td.className = sideCls(s);
      }
    });
  }
  renderTable(targetBox, plan.target_positions,
    [["instrument", "标的"], ["shares", "股数"], ["last_price", "参考价"], ["entry_date", "建仓日"]],
    "暂无目标", "instrument");
}

async function loadDailyOps() {
  const data = await getJSON("/api/daily-ops");
  const box = document.getElementById("daily-ops-list");
  box.innerHTML = "";
  for (const plan of data.plans) {
    const wrap = document.createElement("div");
    renderDailyOpsPanel(plan, wrap);
    box.appendChild(wrap.firstElementChild);
  }
}

// ----------------- 账户页 -----------------
async function loadAccount(account) {
  const panel = document.getElementById(account);
  if (!panel.dataset.init) {
    panel.appendChild(document.getElementById("account-tpl").content.cloneNode(true));
    panel.dataset.init = "1";
  }
  const [daily, hold, fills, reports] = await Promise.all([
    getJSON(`/api/account/${account}/daily`),
    getJSON(`/api/account/${account}/holdings`),
    getJSON(`/api/account/${account}/fills`),
    getJSON(`/api/account/${account}/reports`),
  ]);

  const cc = panel.querySelector(".acct-cards");
  cc.innerHTML = "";
  if (!daily.dates.length) { cc.innerHTML = '<div class="empty">暂无净值数据，等待回填/收盘流水线。</div>'; }
  else {
    const s = daily.series, n = daily.dates.length - 1;
    // 净值序列末日未必是日历「今天」；收益标签用实际交易日，避免「当日」误导
    const retDay = String(daily.dates[n]).slice(5); // YYYY-MM-DD → MM-DD
    cc.appendChild(card("最新净值", fmt(s.nav[n]) + " 元", daily.dates[n]));
    cc.appendChild(card("累计收益", pct(s.cum_ret[n]), `超额 ${pct(s.cum_excess[n])}`, cls(s.cum_ret[n])));
    cc.appendChild(card(`${retDay} 收益`, pct(s.daily_ret[n]), "", cls(s.daily_ret[n])));
    cc.appendChild(card("持仓 / 现金", `${s.n_pos[n]} 只`, `现金 ${fmt(s.cash[n])} (${fmt(s.cash[n] / s.nav[n] * 100, 1)}%)`));
    cc.appendChild(card("交易天数", daily.dates.length, `平均换手 ${fmt(avg(s.turnover), 1)}%`));
  }

  if (daily.dates.length) {
    const L = daily.dates, s = daily.series;
    const navLast = lastNonNull(s.nav);
    const retLast = lastNonNull(s.cum_ret);
    const exLast = lastNonNull(s.cum_excess);
    setChartCopy(panel.querySelector(".navChart"), {
      title: navLast != null ? `净值 ${fmt(navLast)}` : "净值",
      sub: `${L[0] || ""} → ${L[L.length - 1] || ""} · 元`,
      src: "NAV · DAILY",
    });
    setChartCopy(panel.querySelector(".retChart"), {
      title: retLast != null ? `累计 ${pct(retLast)} · 超额 ${pct(exLast || 0)}` : "累计收益",
      sub: "累计白 · 超额橙 · 基准虚线 · 小数",
      src: "CUM RETURN VS BENCH",
    });
    setChartCopy(panel.querySelector(".posChart"), {
      title: "持仓只数与换手",
      sub: "持仓白 · 换手橙 · %",
      src: "POSITION COUNT · TURNOVER",
    });
    setChartCopy(panel.querySelector(".cashChart"), {
      title: "现金 vs 持仓市值",
      sub: "堆叠面积 · 元",
      src: "CASH + POSITION VALUE",
    });
    mkChartC(panel, ".navChart", "nav_" + account, {
      type: "line",
      data: { labels: L, datasets: [lineDS("净值", s.nav, GLANCE.ink, true)] },
      options: baseOpts,
    });
    mkChartC(panel, ".retChart", "ret_" + account, {
      type: "line",
      data: {
        labels: L,
        datasets: [
          lineDS("累计收益", s.cum_ret, GLANCE.ink),
          lineDS("基准", s.cum_bench, COLORS.bench, false, { dash: [5, 4], borderWidth: 1.4 }),
          lineDS("超额", s.cum_excess, GLANCE.hero),
        ],
      },
      options: withLegend(baseOpts),
    });
    mkChartC(panel, ".posChart", "pos_" + account, {
      type: "line",
      data: { labels: L, datasets: [lineDS("持仓数", s.n_pos, GLANCE.ink), lineDS("换手%", s.turnover, GLANCE.hero)] },
      options: withLegend(baseOpts),
    });
    mkChartC(panel, ".cashChart", "cash_" + account, {
      type: "line",
      data: {
        labels: L,
        datasets: [
          lineDS("现金", s.cash, GLANCE.faint, true),
          lineDS("持仓市值", s.position_value, GLANCE.ink, true),
        ],
      },
      options: {
        ...withLegend(baseOpts),
        scales: { ...baseOpts.scales, y: { ...baseOpts.scales.y, stacked: true } },
      },
    });
  }

  // 个股每日收盘独立加载：即使失败也不影响本页其它模块
  getJSON(`/api/account/${account}/positions-daily`)
    .then(posDaily => renderPosDaily(panel, "pos_daily_" + account, posDaily))
    .catch(e => { console.error(e); renderPosDaily(panel, "pos_daily_" + account, null); });

  renderTable(panel.querySelector(".holdings"), hold.holdings,
    [["instrument", "标的"], ["shares", "股数"], ["last_price", "现价"], ["market_value", "市值"], ["weight_pct", "权重%"], ["entry_date", "建仓日"]],
    "暂无持仓", "instrument");
  renderTable(panel.querySelector(".fills"), fills.fills,
    [["date", "日期"], ["instrument", "标的"], ["side", "方向"], ["shares", "股数"], ["price", "成交价"], ["amount", "金额"], ["fee", "费用"]],
    "暂无成交", "instrument");

  const rb = panel.querySelector(".reports"), rv = panel.querySelector(".report-view");
  rb.innerHTML = ""; rv.innerHTML = "";
  if (!reports.reports.length) rb.innerHTML = '<span class="empty">暂无报告</span>';
  reports.reports.slice(0, 12).forEach(name => {
    const b = document.createElement("button");
    b.textContent = name;
    b.onclick = async () => { rv.innerHTML = await (await fetch(`/api/account/${account}/report/${name}`)).text(); };
    rb.appendChild(b);
  });
}

// 个股每日收盘（近一月）：累计涨幅多线图 + 汇总表
const POS_LINE_COLORS = [
  GLANCE.ink, GLANCE.hero, "#c6c5bf", "#8f8e88", "#d4a574",
  "#9aa4b2", "#b0afa9", "#7a9e8a", "#a89b8c", "#6a6963",
];

function renderPosDaily(panel, chartKey, data) {
  const box = panel.querySelector(".pos-daily");
  const canvas = panel.querySelector(".posDailyChart");
  if (!data || !data.instruments || !data.instruments.length) {
    box.innerHTML = '<div class="empty">暂无个股收盘快照，运行 snapshot_positions.py --backfill 生成。</div>';
    if (charts[chartKey]) { charts[chartKey].destroy(); delete charts[chartKey]; }
    return;
  }
  const L = data.dates;
  const ds = data.instruments.map((it, i) => lineDS(
    it.instrument, it.cum, POS_LINE_COLORS[i % POS_LINE_COLORS.length]));
  canvas.dataset.k = chartKey;
  setChartCopy(canvas, {
    title: `近一月各股累计涨幅 · ${data.instruments.length} 只`,
    sub: "窗口起点归零 · % · 持仓与近一月买卖过的标的",
    src: "POSITION DAILY · LOCAL CLOSE",
  });
  mkChart(canvas, {
    type: "line",
    data: { labels: L, datasets: ds },
    options: { ...withLegend({ ...baseOpts, plugins: { ...baseOpts.plugins, glanceLastValue: false } }), spanGaps: true },
  });

  let h = "<table><thead><tr><th>标的</th><th>状态</th><th>最新收盘</th>"
    + "<th>当日涨跌幅</th><th>近一月</th></tr></thead><tbody>";
  for (const it of data.instruments) {
    let status;
    if (it.held) status = `<span class="badge done">持仓</span> ${it.shares}股`;
    else if (it.pending_buy) status = `<span class="badge pending">待买入</span> ${it.shares}股`;
    else status = '<span class="badge no_trade">已清仓</span>';
    h += "<tr>"
      + `<td class="clickable" data-inst="${it.instrument}">${it.instrument}</td>`
      + `<td>${status}</td>`
      + `<td>${fmt(it.last_close)}</td>`
      + `<td class="${cls(it.last_chg)}">${pct(it.last_chg)}</td>`
      + `<td class="${cls(it.month_ret)}">${pct(it.month_ret)}</td>`
      + "</tr>";
  }
  box.innerHTML = h + "</tbody></table>";
  box.querySelectorAll("td.clickable").forEach(td =>
    td.onclick = () => openStock(td.dataset.inst));
}

function mkChartC(panel, sel, key, cfg) {
  const c = panel.querySelector(sel); c.dataset.k = key; mkChart(c, cfg);
}
const avg = arr => arr && arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;

function renderTable(container, rows, cols, empty, instKey) {
  if (!rows || !rows.length) { container.innerHTML = `<div class="empty">${empty}</div>`; return; }
  let h = "<table><thead><tr>" + cols.map(c => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    h += "<tr>" + cols.map(c => {
      let v = r[c[0]];
      if (typeof v === "number") v = fmt(v, c[0] === "weight_pct" ? 2 : (Number.isInteger(v) ? 0 : 2));
      const click = (instKey && c[0] === instKey) ? ` class="clickable" data-inst="${r[instKey]}"` : "";
      return `<td${click}>${v ?? "-"}</td>`;
    }).join("") + "</tr>";
  }
  container.innerHTML = h + "</tbody></table>";
  if (instKey) container.querySelectorAll("td.clickable").forEach(td =>
    td.onclick = () => openStock(td.dataset.inst));
}

// ----------------- 大盘指数 -----------------
async function loadIndices() {
  const box = document.getElementById("indices");
  try {
    const r = await getJSON("/api/indices");
    box.innerHTML = "";
    for (const ix of r.indices) {
      const c = card(ix.display_name, fmt(ix.latest), `${pct(ix.chg_pct)} · ${ix.date}`, cls(ix.chg_pct));
      c.dataset.inst = ix.instrument;
      c.onclick = () => openStock(ix.instrument);
      box.appendChild(c);
    }
  } catch (e) { box.innerHTML = ""; }
}

// ----------------- 个股行情弹窗 -----------------
let smInst = null, smKlt = 101;
async function openStock(instrument) {
  smInst = instrument;
  document.getElementById("stock-modal").classList.remove("hidden");
  document.getElementById("sm-title").textContent = instrument;
  await loadStock();
}
async function loadStock() {
  const meta = document.getElementById("sm-meta");
  meta.innerHTML = '<span class="empty">加载中…</span>';
  let q;
  try { q = await getJSON(`/api/quote/${smInst}?klt=${smKlt}&n=${smKlt >= 100 ? 120 : 240}&_=${Date.now()}`); }
  catch (e) { meta.innerHTML = '<span class="empty">行情获取失败</span>'; return; }
  if (!q.ok || !q.klines.length) { meta.innerHTML = '<span class="empty">无行情数据</span>'; return; }

  document.getElementById("sm-title").textContent = `${q.name}（${smInst}）`;
  meta.innerHTML = `<span class="big ${cls(q.chg_pct)}">${fmt(q.latest)}</span>`
    + `<span class="${cls(q.chg_pct)}">${pct(q.chg_pct)}</span>`
    + `<span>开 ${fmt(q.open)} 高 ${fmt(q.high)} 低 ${fmt(q.low)}</span>`
    + `<span>${q.date}</span><span>来源: ${q.source}</span>`;

  const L = q.klines.map(k => k.date);
  const up = q.klines.map(k => k.close >= k.open);
  const RED = COLORS.red, GREEN = COLORS.green;
  const colors = up.map(u => u ? RED : GREEN);
  // 蜡烛：影线([low,high]) + 实体([open,close]) 两个浮动柱叠加（grouped:false 同位重叠）
  mkChart(document.getElementById("smK"), {
    type: "bar",
    data: {
      labels: L,
      datasets: [
        { label: "影线", data: q.klines.map(k => [k.low, k.high]), backgroundColor: colors,
          barThickness: 2, grouped: false, order: 2, borderRadius: 0, borderSkipped: false },
        { label: "实体", data: q.klines.map(k => [k.open, k.close]), backgroundColor: colors,
          barThickness: 8, grouped: false, order: 1, borderRadius: 1, borderSkipped: false },
      ],
    },
    options: {
      ...baseOpts,
      plugins: {
        ...baseOpts.plugins,
        legend: { display: false },
        glanceLastValue: false,
        tooltip: {
          ...baseOpts.plugins.tooltip,
          callbacks: {
            label: (c) => {
              const k = q.klines[c.dataIndex];
              return `开${k.open} 高${k.high} 低${k.low} 收${k.close}`;
            },
          },
        },
      },
      scales: {
        x: { ...baseOpts.scales.x, stacked: false },
        y: { ...baseOpts.scales.y, beginAtZero: false },
      },
    },
  });
  setChartCopy("smK", { title: "日K · 红涨绿跌", sub: `${q.name} · ${L[0] || ""} → ${L[L.length - 1] || ""}`, src: "OHLC · EMPTY UP / SOLID DOWN" });
  setChartCopy("smVol", { title: "成交量", sub: "柱色跟随当日涨跌", src: "VOLUME" });
  mkChart(document.getElementById("smVol"), {
    type: "bar",
    data: { labels: L, datasets: [{ label: "成交量", data: q.klines.map(k => k.volume), backgroundColor: colors, borderRadius: 2, borderSkipped: false, barPercentage: 0.78 }] },
    options: { ...baseOpts, plugins: { ...baseOpts.plugins, legend: { display: false }, glanceLastValue: false } },
  });

  renderTable(document.getElementById("sm-table"), q.klines.slice(-12).reverse(),
    [["date", "日期"], ["open", "开"], ["high", "高"], ["low", "低"], ["close", "收"], ["volume", "成交量"]],
    "无数据");
}
document.getElementById("sm-close").onclick = () =>
  document.getElementById("stock-modal").classList.add("hidden");
document.getElementById("stock-modal").onclick = (e) => {
  if (e.target.id === "stock-modal") e.target.classList.add("hidden");
};
document.querySelectorAll(".klt-btn").forEach(b => b.onclick = () => {
  document.querySelectorAll(".klt-btn").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); smKlt = +b.dataset.klt; loadStock();
});

// ----------------- 对比（四线） -----------------
async function loadCompare() {
  const c = await getJSON("/api/compare");
  const accounts = c.accounts?.length ? c.accounts : COMPARE_ACCOUNTS;
  const sum = c.summary || {};
  const box = document.getElementById("cmp-summary");
  box.className = "cards"; box.innerHTML = "";
  for (const a of accounts) {
    const s = sum[a];
    const label = (c.labels && c.labels[a]) || ACCOUNT_SHORT[a] || a;
    if (!s || !s.days) { box.appendChild(card(label, "未初始化", "")); continue; }
    box.appendChild(card(label, pct((s.cum_ret || 0) * 100),
      `净值 ${fmt(s.nav)} · ${s.days}天 · 超额 ${pct((s.cum_excess || 0) * 100)} · 现金 ${pct((s.cash_ratio || 0) * 100)}`,
      s.cum_ret >= 0 ? "pos" : "neg"));
  }

  // 累计收益 / 超额：按日期并集对齐（各线开线日不同）
  const seriesMap = c.series || {};
  const labels = [...new Set(accounts.flatMap(a => seriesMap[a]?.dates || []))].sort();
  const alignBy = (dates, values) => {
    const m = new Map((dates || []).map((d, i) => [d, values[i]]));
    return labels.map(d => (m.has(d) ? m.get(d) : null));
  };
  const cumDs = [];
  const excessDs = [];
  for (const a of accounts) {
    const ser = seriesMap[a];
    if (!ser?.dates?.length) continue;
    const color = ACCOUNT_COLORS[a] || COLORS.research;
    const name = ACCOUNT_SHORT[a] || a;
    cumDs.push(lineDS(name, alignBy(ser.dates, ser.series.cum_ret), color));
    excessDs.push(lineDS(name, alignBy(ser.dates, ser.series.cum_excess), color));
  }
  // 基准取研究线（与总览一致）
  const research = seriesMap.research_sim_100k;
  if (research?.dates?.length) {
    cumDs.push(lineDS("基准", alignBy(research.dates, research.series.cum_bench), COLORS.bench, false, { dash: [5, 4], borderWidth: 1.4 }));
  }
  const liveCum = lastNonNull(cumDs.find(d => d.label === "实盘线")?.data);
  const resCum = lastNonNull(cumDs.find(d => d.label === "研究模拟线")?.data);
  let cmpTitle = "四线累计收益";
  if (liveCum != null && resCum != null) {
    const gap = liveCum - resCum;
    cmpTitle = gap >= 0 ? `实盘领先研究 ${pct(gap)}` : `实盘落后研究 ${pct(gap)}`;
  }
  setChartCopy("cmpChart", { title: cmpTitle, sub: ACCOUNT_LEGEND, src: "CUM RETURN · UNION OF TRADE DAYS · %" });
  mkChart(document.getElementById("cmpChart"), {
    type: "line",
    data: { labels, datasets: cumDs },
    options: { ...withLegend(baseOpts), spanGaps: true },
  });
  setChartCopy("cmpExcessChart", { title: "累计超额", sub: ACCOUNT_LEGEND, src: "CUM EXCESS · %" });
  mkChart(document.getElementById("cmpExcessChart"), {
    type: "line",
    data: { labels, datasets: excessDs },
    options: { ...withLegend(baseOpts), spanGaps: true },
  });

  const gapDays = c.common_days || [];
  const gapVals = gapDays.map(d => d.gap);
  const gapLast = lastNonNull(gapVals);
  setChartCopy("cmpGapChart", {
    title: gapLast == null ? "日收益差（实盘−研究）" : `末日差 ${pct(gapLast)} · 实盘相对研究`,
    sub: "正=实盘当日更好 · 红正绿负 · %",
    src: "COMMON DAYS · LIVE − RESEARCH",
  });
  mkChart(document.getElementById("cmpGapChart"), {
    type: "bar",
    data: {
      labels: gapDays.map(d => d.date),
      datasets: [barDS("日收益差%", gapVals, { signed: true })],
    },
    options: {
      ...baseOpts,
      plugins: { ...baseOpts.plugins, glanceLastValue: false },
      scales: {
        x: { ...baseOpts.scales.x, ticks: { ...baseOpts.scales.x.ticks, maxTicksLimit: 6 } },
        y: {
          ...baseOpts.scales.y,
          grid: {
            color: (ctx) => ctx.tick.value === 0 ? "rgba(235,232,224,0.28)" : GLANCE.grid,
            lineWidth: (ctx) => ctx.tick.value === 0 ? 1.2 : 1,
          },
        },
      },
    },
  });

  const taGap = c.ta_gap_days || [];
  const taVals = taGap.map(d => d.gap);
  const taLast = lastNonNull(taVals);
  setChartCopy("cmpTaGapChart", {
    title: taLast == null ? "超额差（TA−对照）" : `末日超额差 ${pct(taLast)} · TA 相对对照`,
    sub: "正=TA 当日超额更好 · 红正绿负 · %",
    src: "COMMON DAYS · TA − CONTROL",
  });
  mkChart(document.getElementById("cmpTaGapChart"), {
    type: "bar",
    data: {
      labels: taGap.map(d => d.date),
      datasets: [barDS("超额差%", taVals, { signed: true })],
    },
    options: {
      ...baseOpts,
      plugins: { ...baseOpts.plugins, glanceLastValue: false },
      scales: {
        x: { ...baseOpts.scales.x, ticks: { ...baseOpts.scales.x.ticks, maxTicksLimit: 6 } },
        y: {
          ...baseOpts.scales.y,
          grid: {
            color: (ctx) => ctx.tick.value === 0 ? "rgba(235,232,224,0.28)" : GLANCE.grid,
            lineWidth: (ctx) => ctx.tick.value === 0 ? 1.2 : 1,
          },
        },
      },
    },
  });

  renderTable(document.getElementById("cmp-fills"), c.fill_diff,
    [["date", "日期"], ["instrument", "标的"], ["side", "方向"], ["research_price", "研究价"], ["live_price", "实盘价"], ["adverse_slip_pct", "不利滑点%"]],
    "暂无可匹配成交", "instrument");
}

// ----------------- 告警/调度 -----------------
async function loadAlerts() {
  const ov = await getJSON("/api/overview");
  let h = "<table><thead><tr><th>任务</th><th>下次运行</th></tr></thead><tbody>";
  for (const j of ov.jobs) h += `<tr><td>${j.id}</td><td>${j.next_run || "-"}</td></tr>`;
  document.getElementById("jobs").innerHTML = h + "</tbody></table>";

  const visitBox = document.getElementById("visit-stats");
  if (fullAccess && visitBox) {
    try {
      const v = await getJSON("/api/visits?limit=50");
      let vh = `<p class="panel-desc">累计 ${v.total} 次 · 独立 IP ${v.unique_ips} 个（打开首页即记一条）</p>`;
      if (v.by_ip?.length) {
        vh += "<table><thead><tr><th>IP</th><th>次数</th></tr></thead><tbody>";
        for (const row of v.by_ip) {
          vh += `<tr><td>${row.ip}</td><td>${row.count}</td></tr>`;
        }
        vh += "</tbody></table><h4 style='margin:16px 0 8px'>最近访问</h4>";
      }
      if (v.recent?.length) {
        vh += "<table><thead><tr><th>时间</th><th>IP</th><th>模式</th></tr></thead><tbody>";
        for (const row of v.recent) {
          const mode = row.full_access ? "全功能" : "演示";
          vh += `<tr><td>${row.time || "-"}</td><td>${row.ip || "-"}</td><td>${mode}</td></tr>`;
        }
        vh += "</tbody></table>";
      } else if (!v.total) {
        vh += '<div class="empty">尚无访问记录</div>';
      }
      visitBox.innerHTML = vh;
    } catch (e) {
      visitBox.innerHTML = `<div class="empty">访问统计加载失败</div>`;
    }
  } else if (visitBox) {
    visitBox.innerHTML = "";
  }

  const al = await getJSON("/api/alerts");
  if (!al.alerts.length) { document.getElementById("alert-list").innerHTML = '<div class="empty">暂无告警</div>'; return; }
  let a = "<table><tbody>";
  for (const x of al.alerts) {
    const lvl = (x.raw.match(/\b(CRIT|WARN|INFO)\b/) || [])[0] || "";
    a += `<tr><td style="text-align:left" class="lvl-${lvl}">${x.raw}</td></tr>`;
  }
  document.getElementById("alert-list").innerHTML = a + "</tbody></table>";
}

// ----------------- 舆情跟踪 -----------------
let sentInst = null;

function sentPolarity(s) {
  s = (s || "neutral").toLowerCase();
  if (s === "positive") return "pos";
  if (s === "negative") return "neg";
  return "";
}
function sentLabel(s) {
  return ({ positive: "偏多", negative: "偏空", mixed: "分化", neutral: "中性" })[s] || (s || "中性");
}

async function loadSentiment(keepSelection = true) {
  await resumeSentJobIfAny();
  const prev = keepSelection ? sentInst : null;
  const cat = await getJSON("/api/sentiment/catalog");
  const peakEl = document.getElementById("sent-peak");
  peakEl.textContent = cat.peak_hour ? "高峰 · 自部署" : "闲时 · DeepSeek";
  peakEl.className = "badge " + (cat.peak_hour ? "peak" : "offpeak");
  document.getElementById("sent-updated").textContent =
    cat.updated_at ? ("更新 " + cat.updated_at) : "尚无报告";

  const list = document.getElementById("sent-list");
  list.innerHTML = "";
  const items = cat.instruments || [];
  if (!items.length) {
    list.innerHTML = '<div class="empty" style="padding:12px">暂无跟踪标的</div>';
    document.getElementById("sent-empty").classList.remove("hidden");
    document.getElementById("sent-detail").classList.add("hidden");
    return;
  }
  items.forEach(it => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sent-item" + (prev === it.instrument ? " active" : "");
    const codeOnly = String(it.instrument || "").replace(/^(SH|SZ)/, "");
    b.innerHTML = `<div><span class="code">${codeOnly}</span>`
      + `<span class="name">${it.name || ""}</span></div>`
      + `<div class="snip">${it.headline || "—"}</div>`
      + `<span class="pill ${it.sentiment || ""}">${sentLabel(it.sentiment)} `
      + `${it.score == null ? "" : Number(it.score).toFixed(2)}</span>`;
    b.dataset.instrument = it.instrument;
    b.onclick = () => selectSentiment(it.instrument);
    list.appendChild(b);
  });
  const pick = (prev && items.some(x => x.instrument === prev))
    ? prev : items[0].instrument;
  await selectSentiment(pick);
}

async function selectSentiment(instrument) {
  sentInst = instrument;
  document.querySelectorAll(".sent-item").forEach(el => {
    el.classList.toggle("active", el.dataset.instrument === instrument);
  });
  document.getElementById("sent-empty").classList.add("hidden");
  document.getElementById("sent-detail").classList.remove("hidden");

  const data = await getJSON(`/api/sentiment/${instrument}`);
  const r = data.report;
  if (!r) {
    document.getElementById("sent-empty").classList.remove("hidden");
    document.getElementById("sent-empty").textContent = `${instrument} 尚无分析报告，请点击「重新分析」。`;
    document.getElementById("sent-detail").classList.add("hidden");
    return;
  }

  const codeOnly = String(instrument).replace(/^(SH|SZ)/, "");
  document.getElementById("sent-name").textContent = r.name || instrument;
  const codePill = document.getElementById("sent-code-pill");
  codePill.textContent = codeOnly || instrument;
  codePill.className = "sent-pill " + (r.sentiment || "neutral");
  document.getElementById("sent-date-pill").textContent =
    r.date ? (`报告日 ${r.date}`) : "报告日 —";
  document.getElementById("sent-sub").textContent =
    `${instrument} · 舆情长期记忆 · 研究口径`;

  const headline = r.headline || "—";
  document.getElementById("sent-headline").textContent = headline;
  const callout = document.getElementById("sent-callout");
  const sent = (r.sentiment || "neutral").toLowerCase();
  callout.className = "sent-callout"
    + (sent === "positive" ? " ok" : sent === "negative" ? " danger" : sent === "mixed" ? " warn" : "");

  const scoreEl = document.getElementById("sent-score");
  scoreEl.textContent = r.score == null ? "—" : Number(r.score).toFixed(2);
  scoreEl.className = "value " + sentPolarity(r.sentiment);
  document.getElementById("sent-score-sub").textContent = sentLabel(r.sentiment);

  document.getElementById("sent-stance").textContent = r.stance || "—";
  document.getElementById("sent-stance").className = "value stance " + sentPolarity(r.sentiment);

  const ann = r.announcement_count ?? 0;
  const pol = r.policy_count ?? 0;
  const newsN = r.news_count ?? 0;
  const media = Math.max(0, Number(newsN || 0) - Number(ann || 0) - Number(pol || 0));
  document.getElementById("sent-coverage").textContent =
    newsN == null ? "—" : String(newsN);
  document.getElementById("sent-coverage-sub").textContent =
    `公告 ${ann} · 政策 ${pol} · 媒体 ${media}`;

  const tags = document.getElementById("sent-tags");
  tags.innerHTML = (r.risk_tags || []).map(t => `<span class="tag">${t}</span>`).join("");
  document.getElementById("sent-summary").textContent = r.summary || "—";
  document.getElementById("sent-fundamentals").textContent = r.fundamentals || "—";
  document.getElementById("sent-policy").textContent = r.policy_impact || "—";
  const watch = document.getElementById("sent-watch");
  watch.innerHTML = (r.watchpoints || []).map(w => `<li>${w}</li>`).join("")
    || "<li class='empty'>暂无</li>";

  // 关键事件
  const evBox = document.getElementById("sent-events");
  if (!(r.key_events || []).length) evBox.innerHTML = '<div class="empty">暂无关键事件</div>';
  else {
    let h = "<table><thead><tr><th>日期</th><th>事件</th><th>影响</th></tr></thead><tbody>";
    for (const e of r.key_events) {
      h += `<tr><td>${e.date || "—"}</td><td style="text-align:left">${e.event || ""}</td>`
        + `<td class="impact-${e.impact || ""}">${e.impact || "—"}</td></tr>`;
    }
    evBox.innerHTML = h + "</tbody></table>";
  }

  // 舆情条目
  const newsBox = document.getElementById("sent-news");
  const news = r.news_preview || [];
  document.getElementById("sent-news-cap").textContent =
    news.length ? `最近 ${Math.min(news.length, 15)} 条` : "最近条目";
  if (!news.length) newsBox.innerHTML = '<div class="empty">暂无条目</div>';
  else {
    let h = "<table><thead><tr><th>时间</th><th>源</th><th>标题</th></tr></thead><tbody>";
    for (const n of news.slice(0, 15)) {
      const title = n.url
        ? `<a href="${n.url}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">${n.title || "—"}</a>`
        : (n.title || "—");
      h += `<tr><td>${(n.published || "").slice(0, 16)}</td><td>${n.source || ""}</td>`
        + `<td style="text-align:left;white-space:normal;max-width:360px">${title}</td></tr>`;
    }
    newsBox.innerHTML = h + "</tbody></table>";
  }

  // 历史报告表 + 情绪轨迹
  const hist = data.history || [];
  const histBox = document.getElementById("sent-hist");
  if (!hist.length) histBox.innerHTML = '<div class="empty">暂无历史</div>';
  else {
    let h = "<table><thead><tr><th>日期</th><th>情绪</th><th>分数</th><th>结论</th></tr></thead><tbody>";
    for (const x of hist.slice(0, 12)) {
      h += `<tr><td>${x.date}</td><td>${sentLabel(x.sentiment)}</td>`
        + `<td class="${sentPolarity(x.sentiment)}">${x.score == null ? "—" : Number(x.score).toFixed(2)}</td>`
        + `<td style="text-align:left;white-space:normal">${x.headline || ""}</td></tr>`;
    }
    histBox.innerHTML = h + "</tbody></table>";
  }
  const hChrono = [...hist].reverse();
  setChartCopy("sentHistChart", {
    title: "情绪轨迹",
    sub: "分数 −1 ~ 1 · 历史报告日",
    src: "SENTIMENT SCORE",
  });
  mkChart(document.getElementById("sentHistChart"), {
    type: "line",
    data: {
      labels: hChrono.map(x => x.date),
      datasets: [lineDS("情绪分", hChrono.map(x => x.score), GLANCE.hero)],
    },
    options: {
      ...baseOpts,
      plugins: { ...baseOpts.plugins, legend: { display: false } },
      scales: { ...baseOpts.scales, y: { ...baseOpts.scales.y, min: -1, max: 1 } },
    },
  });

  const meta = r.meta || {};
  document.getElementById("sent-meta").textContent =
    `模型 ${meta.model || "—"}（${meta.endpoint || "—"}） · 条目 ${r.news_count ?? "—"}`
    + `（公告 ${r.announcement_count ?? "—"} / 政策 ${r.policy_count ?? "—"}）`
    + ` · 向量记忆 ${data.vector?.count ?? r.vector_count ?? "—"} 条`
    + ` · 报告日 ${r.date || "—"} · 仅供研究参考，不构成投资建议`;

  // 近 90 天价格（约 65 个交易日≈三个月；取 70 根日 K 略留余量）
  const retEl = document.getElementById("sent-ret");
  const retSub = document.getElementById("sent-ret-sub");
  const priceCap = document.getElementById("sent-price-cap");
  try {
    const q = await getJSON(`/api/quote/${instrument}?klt=101&n=70&_=${Date.now()}`);
    const canvas = document.getElementById("sentPriceChart");
    if (!q.ok || !q.klines?.length) {
      if (charts.sentPriceChart) { charts.sentPriceChart.destroy(); delete charts.sentPriceChart; }
      retEl.textContent = "—";
      retEl.className = "value";
      retSub.textContent = "近 90 日累计";
      priceCap.textContent = "日线 · 暂无行情";
    } else {
      const L = q.klines.map(k => k.date);
      const closes = q.klines.map(k => k.close);
      const base = closes.find(c => c != null);
      const last = [...closes].reverse().find(c => c != null);
      const cum = closes.map(c => (base ? +((c / base - 1) * 100).toFixed(3) : null));
      const retPct = (base && last) ? ((last / base - 1) * 100) : null;
      if (retPct == null) {
        retEl.textContent = "—";
        retEl.className = "value";
      } else {
        const sign = retPct > 0 ? "+" : "";
        retEl.textContent = `${sign}${retPct.toFixed(2)}%`;
        retEl.className = "value " + (retPct > 0 ? "pos" : retPct < 0 ? "neg" : "");
      }
      retSub.textContent = last != null
        ? `最新 ${Number(last).toFixed(2)} · ${L[0] || ""}→${L[L.length - 1] || ""}`
        : "近 90 日累计";
      priceCap.textContent = `日线累计% · ${L[0] || "?"} 至 ${L[L.length - 1] || "?"} · ${closes.length} 根`;
      // 单轴：窗口累计收益（起点归零）。价格见上方「窗口涨跌」卡片，避免双 Y 轴量纲混淆。
      const lastCum = lastNonNull(cum);
      setChartCopy(canvas, {
        title: lastCum == null ? "近 90 天累计" : `窗口累计 ${pct(lastCum)}`,
        sub: `${L[0] || ""} → ${L[L.length - 1] || ""} · 起点归零`,
        src: "DAILY CLOSE · CUM %",
      });
      mkChart(canvas, {
        type: "line",
        data: {
          labels: L,
          datasets: [
            lineDS("窗口累计%", cum, lastCum != null && lastCum < 0 ? COLORS.green : COLORS.red, true),
          ],
        },
        options: {
          ...baseOpts,
          scales: {
            x: baseOpts.scales.x,
            y: { ...baseOpts.scales.y },
          },
        },
      });
    }
  } catch (e) {
    console.error(e);
    retEl.textContent = "—";
    retEl.className = "value";
    retSub.textContent = "近 90 日累计";
  }
}

document.getElementById("sent-refresh")?.addEventListener("click", () => loadSentiment(true));

function normalizeSentCode(raw) {
  let s = String(raw || "").trim().toUpperCase().replace(/\s+/g, "");
  if (!s) return null;
  s = s.replace(/\.SH$/, "").replace(/\.SZ$/, "").replace(/\./g, "");
  if (/^(SH|SZ)\d{6}$/.test(s)) return s;
  if (/^\d{6}$/.test(s)) return (s[0] === "0" || s[0] === "3") ? ("SZ" + s) : ("SH" + s);
  return null;
}

const SENT_JOB_KEY = "quant_sentiment_job";
let sentJobTimer = null;

function saveSentJobLocal(job) {
  try {
    if (!job || job.status === "idle") localStorage.removeItem(SENT_JOB_KEY);
    else localStorage.setItem(SENT_JOB_KEY, JSON.stringify(job));
  } catch (_) { /* ignore */ }
}

function renderSentProgress(job) {
  const box = document.getElementById("sent-progress");
  if (!box || !job) return;
  const st = job.status || "idle";
  if (st === "idle") {
    box.classList.add("hidden");
    box.classList.remove("done", "error");
    return;
  }
  box.classList.remove("hidden");
  box.classList.toggle("done", st === "done");
  box.classList.toggle("error", st === "error");
  const pct = Math.max(0, Math.min(100, Number(job.pct) || 0));
  const bar = document.getElementById("sent-progress-bar");
  const pctEl = document.getElementById("sent-progress-pct");
  const label = document.getElementById("sent-progress-label");
  const msg = document.getElementById("sent-progress-msg");
  if (bar) bar.style.setProperty("--p", (pct / 100).toFixed(4));
  if (pctEl) pctEl.textContent = pct + "%";
  const target = job.instrument || (job.account ? `账户 ${job.account}` : "跟踪标的");
  if (label) {
    if (st === "running") label.textContent = `分析进行中 · ${target}`;
    else if (st === "done") label.textContent = `分析完成 · ${target}`;
    else if (st === "error") label.textContent = `分析失败 · ${target}`;
    else label.textContent = "分析状态";
  }
  if (msg) {
    const extra = job.done_count != null && job.total
      ? `（${job.done_count}/${job.total}）` : "";
    msg.textContent = (job.message || job.last_line || "—") + extra;
  }
}

function stopSentJobPoll() {
  if (sentJobTimer) { clearInterval(sentJobTimer); sentJobTimer = null; }
}

async function pollSentJobOnce() {
  try {
    const job = await getJSON("/api/sentiment/job");
    renderSentProgress(job);
    saveSentJobLocal(job);
    if (job.status === "done" || job.status === "error") {
      stopSentJobPoll();
      // 完成后刷新列表/详情
      try {
        await loadSentiment(true);
        if (job.instrument) await selectSentiment(job.instrument);
      } catch (_) { /* ignore */ }
      // 完成态保留展示一会儿
      setTimeout(() => {
        const box = document.getElementById("sent-progress");
        if (box && !box.classList.contains("hidden") && job.status !== "running") {
          // 保留完成条，用户可手动刷新后仍能看到最近结果；不自动隐藏
        }
      }, 800);
      return job;
    }
    if (job.status !== "running") stopSentJobPoll();
    return job;
  } catch (e) {
    console.warn("job poll", e);
    return null;
  }
}

function startSentJobPoll() {
  stopSentJobPoll();
  pollSentJobOnce();
  sentJobTimer = setInterval(pollSentJobOnce, 2500);
}

async function resumeSentJobIfAny() {
  // 优先服务端状态（刷新后仍准确）
  try {
    const job = await getJSON("/api/sentiment/job");
    if (job && (job.status === "running" || job.status === "done" || job.status === "error")) {
      renderSentProgress(job);
      saveSentJobLocal(job);
      if (job.status === "running") startSentJobPoll();
      return;
    }
  } catch (_) { /* ignore */ }
  try {
    const raw = localStorage.getItem(SENT_JOB_KEY);
    if (!raw) return;
    const job = JSON.parse(raw);
    if (job?.status === "running") {
      renderSentProgress(job);
      startSentJobPoll();
    } else if (job) {
      renderSentProgress(job);
    }
  } catch (_) { /* ignore */ }
}

async function triggerSentimentRun({ instrument = null, btn = null, label = "重新分析" } = {}) {
  if (!fullAccess) {
    alert("演示模式：分析功能仅对白名单 IP 开放");
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
  let triggered = false;
  try {
    const q = new URLSearchParams();
    if (instrument) q.set("instrument", instrument);
    else q.set("account", "live_manual_10k");
    const r = await fetch("/api/sentiment/run?" + q.toString(), { method: "POST" });
    const raw = await r.text();
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${raw.slice(0, 120)}`);
    let body = {};
    try { body = JSON.parse(raw); } catch (_) { /* ignore */ }
    triggered = true;
    const job = body.job || {
      status: "running", pct: 5,
      instrument: body.instrument || instrument,
      account: body.account,
      message: body.busy ? "已有任务在跑，接入进度…" : "任务已启动…",
    };
    if (body.busy) {
      // 不重复排队，直接跟已有任务
    }
    const target = job.instrument || body.instrument || instrument;
    if (target) sentInst = target;
    renderSentProgress(job);
    saveSentJobLocal(job);
    startSentJobPoll();
    // 等待任务结束（最长约 8 分钟）
    const startedAt = Date.now();
    while (Date.now() - startedAt < 480000) {
      await new Promise(r => setTimeout(r, 2500));
      const cur = await pollSentJobOnce();
      if (!cur || cur.status === "done" || cur.status === "error" || cur.status === "idle") break;
    }
  } catch (e) {
    console.error(e);
    if (!triggered) alert("触发分析失败：" + (e.message || e));
    else alert("分析进度异常：" + (e.message || e));
    renderSentProgress({ status: "error", pct: 100, message: String(e.message || e), instrument });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

document.getElementById("sent-run")?.addEventListener("click", () => {
  const btn = document.getElementById("sent-run");
  triggerSentimentRun({ btn, label: "重新分析" });
});

document.getElementById("sent-analyze-one")?.addEventListener("click", () => {
  const input = document.getElementById("sent-code");
  const inst = normalizeSentCode(input?.value);
  if (!inst) {
    alert("请输入有效代码，例如 600519 或 SH600519");
    input?.focus();
    return;
  }
  input.value = inst;
  const btn = document.getElementById("sent-analyze-one");
  triggerSentimentRun({ instrument: inst, btn, label: "分析" });
});

document.getElementById("sent-code")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("sent-analyze-one")?.click();
});

document.getElementById("sent-rerun-one")?.addEventListener("click", () => {
  if (!sentInst) {
    alert("请先选择一只已分析的股票");
    return;
  }
  const btn = document.getElementById("sent-rerun-one");
  triggerSentimentRun({ instrument: sentInst, btn, label: "重新分析本股" });
});

// ----------------- 持仓追踪 -----------------
const TRACKING_JOB_KEY = "tracking_job_v1";
let trkData = null;
let trkJobTimer = null;
let trkModalInst = null;

function saveTrkJobLocal(job) {
  try {
    if (!job || job.status === "idle") localStorage.removeItem(TRACKING_JOB_KEY);
    else localStorage.setItem(TRACKING_JOB_KEY, JSON.stringify(job));
  } catch (e) { /* ignore */ }
}

function renderTrkProgress(job) {
  const box = document.getElementById("trk-progress");
  if (!job || job.status === "idle") { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const pct = Math.max(0, Math.min(100, Number(job.pct || 0)));
  document.getElementById("trk-progress-pct").textContent = pct + "%";
  document.getElementById("trk-progress-bar").style.width = pct + "%";
  const analyzing = job.kind === "analyze";
  const label = job.status === "running"
    ? (job.message || (analyzing ? "持仓分析中…" : "追踪快照构建中…"))
    : (job.status === "done" ? "完成" : "失败");
  document.getElementById("trk-progress-label").textContent = label;
  const cur = job.current
    ? `${job.current} ${job.current_name || ""}`.trim()
    : "";
  document.getElementById("trk-progress-msg").textContent = cur || (job.message || "—");
}

function stopTrkJobPoll() {
  if (trkJobTimer) { clearInterval(trkJobTimer); trkJobTimer = null; }
}

async function pollTrkJobOnce() {
  try {
    const a = await getJSON("/api/tracking/analyze/job");
    if (a && a.status === "running") {
      saveTrkJobLocal(a);
      renderTrkProgress(a);
      return a;
    }
    const j = await getJSON("/api/tracking/job");
    if (j && j.status === "running") {
      saveTrkJobLocal(j);
      renderTrkProgress(j);
      return j;
    }
    const shown = (a && a.status && a.status !== "idle") ? a : j;
    saveTrkJobLocal(shown);
    renderTrkProgress(shown);
    return shown;
  } catch (e) { return null; }
}

function startTrkJobPoll() {
  stopTrkJobPoll();
  trkJobTimer = setInterval(async () => {
    const j = await pollTrkJobOnce();
    if (j && j.status !== "running") {
      stopTrkJobPoll();
      loadTracking(true);
    }
  }, 2000);
}

async function resumeTrkJobIfAny() {
  try {
    const a = await getJSON("/api/tracking/analyze/job");
    if (a && a.status === "running") {
      renderTrkProgress(a);
      startTrkJobPoll();
      return true;
    }
    const j = await getJSON("/api/tracking/job");
    if (j && j.status === "running") {
      renderTrkProgress(j);
      startTrkJobPoll();
      return true;
    }
  } catch (e) { /* ignore */ }
  return false;
}

async function loadTrkAnalyzeHint() {
  const btn = document.getElementById("trk-analyze");
  if (!btn || !fullAccess) return;
  try {
    const d = await getJSON("/api/tracking/analyze/universe");
    const n = Number(d.total || 0);
    const labs = (d.account_labels || []).join(" + ");
    btn.textContent = n ? `跑分析 · ${n} 只` : "跑分析";
    btn.title = n
      ? `对${labs || "实盘线 + TA线"}当前持仓跑舆情 / 研究 / 短线`
      : "实盘线 + TA线当前无持仓";
    btn.dataset.total = String(n);
    btn.dataset.labels = labs;
  } catch (e) { /* ignore */ }
}

function trkSentBadge(s) {
  const map = { positive: ["多", "ok"], negative: ["空", "danger"],
                mixed: ["混", "warn"], neutral: ["中", ""] };
  const [lbl, cls] = map[s] || ["—", ""];
  return `<span class="badge ${cls}">${lbl}</span>`;
}

function trkSwingBadge(sw) {
  if (!sw) return '<span class="muted">—</span>';
  const st = String(sw.result || sw.state || "").toLowerCase();
  const map = {
    hit: ["达标", "ok"], stopped: ["止损", "danger"], expired: ["到期", ""],
    holding: ["跟踪", "ok"], triggered: ["待入场", "warn"],
    watch: ["观察", ""], invalid: ["无效", ""], reject: ["否决", ""],
  };
  const [lbl, bcls] = map[st] || [st || "—", ""];
  return `<span class="badge ${bcls}">${lbl}</span>`;
}

function trkResearchBadge(rs) {
  if (!rs || !rs.merged_direction) return '<span class="muted">—</span>';
  const map = { up: ["看涨", "ok"], down: ["看跌", "danger"], hold: ["观望", ""] };
  const [lbl, bcls] = map[rs.merged_direction] || [rs.merged_direction, ""];
  return `<span class="badge ${bcls}">${lbl}</span>`;
}

function trkFundflowCells(ff) {
  if (!ff) return '<td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td>';
  const main = ff.main_net_yi;
  const large = (ff.large || {}).net_yi;
  const li = (ff.large || {}).in_yi;
  const lo = (ff.large || {}).out_yi;
  const tip = `主力 流入 ${fmt(ff.main_in_yi, 2)}亿 / 流出 ${fmt(ff.main_out_yi, 2)}亿`
    + `\n超大+大 流入 ${fmt(li, 2)}亿 / 流出 ${fmt(lo, 2)}亿`;
  return `<td class="${cls(main)}" title="${tip}">${fmtYi(main)}`
    + `<div class="muted small">入 ${fmt(ff.main_in_yi, 2)} / 出 ${fmt(ff.main_out_yi, 2)}</div></td>`
    + `<td class="${cls(large)}" title="${tip}">${fmtYi(large)}`
    + `<div class="muted small">超大+大</div></td>`
    + `<td title="${tip}">${fmt(li, 2)}<div class="muted small">大量买入</div></td>`
    + `<td title="${tip}">${fmt(lo, 2)}<div class="muted small">大量卖出</div></td>`;
}

function renderTrkFundflow(box, ff) {
  if (!box) return;
  if (!ff) {
    box.innerHTML = '<div class="empty">暂无资金流向（重新构建快照后更新）</div>';
    return;
  }
  const card = (lbl, net, inn, out, extra) => {
    const netCls = cls(net);
    return `<div class="trk-ff-card">
      <div class="lbl">${lbl}</div>
      <div class="val ${netCls}">${fmtYi(net)}</div>
      <div class="sub">流入 ${fmtYi(inn)} · 流出 ${fmtYi(out)?.replace("+", "")}${extra || ""}</div>
    </div>`;
  };
  const pctTxt = ff.main_net_pct == null ? "" : ` · 净占比 ${pct(ff.main_net_pct)}`;
  let h = `<div class="trk-ff-grid">
    ${card("主力", ff.main_net_yi, ff.main_in_yi, ff.main_out_yi, pctTxt)}
    ${card("超大单", (ff.super || {}).net_yi, (ff.super || {}).in_yi, (ff.super || {}).out_yi)}
    ${card("大单", (ff.big || {}).net_yi, (ff.big || {}).in_yi, (ff.big || {}).out_yi)}
  </div>
  <div class="trk-ff-grid">
    ${card("超大+大合计", (ff.large || {}).net_yi, (ff.large || {}).in_yi, (ff.large || {}).out_yi, " · 大量买卖")}
    ${card("中单", (ff.medium || {}).net_yi, (ff.medium || {}).in_yi, (ff.medium || {}).out_yi)}
    ${card("小单", (ff.small || {}).net_yi, (ff.small || {}).in_yi, (ff.small || {}).out_yi)}
  </div>`;
  const hist = ff.history || [];
  if (hist.length) {
    h += `<div class="trk-ff-hist"><table><thead><tr>`
      + `<th>日期</th><th>主力净(亿)</th><th>超大净(亿)</th><th>大单净(亿)</th><th>超大+大净(亿)</th>`
      + `</tr></thead><tbody>`;
    for (const row of hist.slice().reverse()) {
      h += `<tr><td>${row.date}</td>`
        + `<td class="${cls(row.main_net_yi)}">${fmtYi(row.main_net_yi)}</td>`
        + `<td class="${cls(row.super_net_yi)}">${fmtYi(row.super_net_yi)}</td>`
        + `<td class="${cls(row.big_net_yi)}">${fmtYi(row.big_net_yi)}</td>`
        + `<td class="${cls(row.large_net_yi)}">${fmtYi(row.large_net_yi)}</td></tr>`;
    }
    h += "</tbody></table></div>";
  }
  box.innerHTML = h;
}

function trkAccountTags(accs) {
  return (accs || []).map(a => `<span class="tag">${ACCOUNT_SHORT[a] || a}</span>`).join("");
}

function trkFilterRows(rows) {
  const q = (document.getElementById("trk-filter").value || "").trim().toUpperCase();
  const onlyHeld = document.getElementById("trk-only-held").checked;
  const onlySent = document.getElementById("trk-only-sent").checked;
  return rows.filter(r => {
    if (onlyHeld && !r.still_held) return false;
    if (onlySent && !r.sentiment) return false;
    if (q) {
      const hay = `${r.instrument} ${r.name || ""}`.toUpperCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderTrkList() {
  const rows = trkFilterRows(trkData.instruments || []);
  document.getElementById("trk-count").textContent =
    `${rows.length} / ${trkData.total || 0} 只`;
  const box = document.getElementById("trk-list");
  if (!rows.length) {
    box.innerHTML = '<div class="empty">无符合条件的标的</div>';
    return;
  }
  let h = '<table class="trk-table"><thead><tr>'
    + '<th>标的</th><th>账户</th><th>首买日</th><th>首买价</th>'
    + '<th>最新价</th><th>累计涨跌</th><th>区间最低</th><th>区间最高</th>'
    + '<th>最大回撤</th><th>持仓</th><th>主力净</th><th>超大+大净</th><th>大量买</th><th>大量卖</th>'
    + '<th>舆情</th><th>短线</th><th>研究</th><th></th></tr></thead><tbody>';
  for (const r of rows) {
    const sent = r.sentiment || {};
    h += `<tr>`
      + `<td class="clickable" data-inst="${r.instrument}">${r.instrument}<div class="muted small">${r.name || ""}</div></td>`
      + `<td>${trkAccountTags(r.accounts)}</td>`
      + `<td>${r.first_buy_date || "—"}</td>`
      + `<td>${fmt(r.first_buy_price)}</td>`
      + `<td>${r.current_price == null ? "—" : fmt(r.current_price)}<div class="muted small">${r.current_date || ""}</div></td>`
      + `<td class="${cls(r.cum_ret)}">${r.cum_ret == null ? "—" : pct(r.cum_ret)}</td>`
      + `<td class="neg">${r.min_close == null ? "—" : fmt(r.min_close)}<div class="muted small">${r.min_date || ""}</div></td>`
      + `<td class="pos">${r.max_close == null ? "—" : fmt(r.max_close)}<div class="muted small">${r.max_date || ""}</div></td>`
      + `<td class="${cls(r.max_dd)}">${r.max_dd == null ? "—" : pct(r.max_dd)}</td>`
      + `<td>${r.still_held ? `<span class="badge done">持 ${r.net_shares}</span>` : '<span class="badge no_trade">清</span>'}</td>`
      + trkFundflowCells(r.fundflow)
      + `<td>${sent.sentiment ? trkSentBadge(sent.sentiment) : '<span class="muted">—</span>'}</td>`
      + `<td>${trkSwingBadge(r.swing)}</td>`
      + `<td>${trkResearchBadge(r.research)}</td>`
      + `<td><button class="mini-btn" data-open="${r.instrument}">详情</button></td>`
      + `</tr>`;
  }
  box.innerHTML = h + "</tbody></table>";
  box.querySelectorAll("td.clickable").forEach(td =>
    td.onclick = () => openTrkDetail(td.dataset.inst));
  box.querySelectorAll("button[data-open]").forEach(b =>
    b.onclick = () => openTrkDetail(b.dataset.open));
}

function renderTrkStats() {
  const d = trkData;
  if (!d || !d.total) {
    document.getElementById("trk-stats").innerHTML = "";
    return;
  }
  const rows = d.instruments || [];
  const held = rows.filter(r => r.still_held).length;
  const cov = d.coverage || {};
  const withSent = cov.sentiment ?? rows.filter(r => r.sentiment).length;
  const withSwing = cov.swing ?? rows.filter(r => r.swing).length;
  const withRs = cov.research ?? rows.filter(r => r.research).length;
  const withFf = cov.fundflow ?? rows.filter(r => r.fundflow).length;
  const rets = rows.map(r => r.cum_ret).filter(x => x != null);
  const avg = rets.length ? rets.reduce((a, b) => a + b, 0) / rets.length : null;
  const nUp = rets.filter(x => x > 0).length;
  const nDown = rets.filter(x => x < 0).length;
  const cards = [
    { l: "追踪标的", v: d.total, s: `行情 ${d.quote_ok_count} 成功` },
    { l: "当前持仓", v: held, s: `${d.total - held} 已清仓` },
    { l: "多 Agent 覆盖", v: `${withSent}/${withSwing}/${withRs}`, s: "舆情 / 短线 / 研究" },
    { l: "资金流向", v: withFf, s: `覆盖 ${withFf}/${d.total} 只 · 当日东财`, vCls: withFf ? "" : "muted" },
    { l: "平均涨跌", v: avg == null ? "—" : pct(avg), s: `红 ${nUp} / 绿 ${nDown}`, vCls: cls(avg) },
  ].map(x => card(x.l, x.v, x.s, x.vCls || ""));
  document.getElementById("trk-stats").innerHTML = cards.map(c => c.outerHTML).join("");
}

async function loadTracking(keepFilter = true) {
  if (!keepFilter) {
    document.getElementById("trk-filter").value = "";
    document.getElementById("trk-only-held").checked = false;
    document.getElementById("trk-only-sent").checked = false;
  }
  try {
    const d = await getJSON("/api/tracking");
    if (d.empty) {
      document.getElementById("trk-stats").innerHTML = "";
      document.getElementById("trk-list").innerHTML =
        `<div class="empty">${d.message || "尚无追踪快照"}</div>`;
      document.getElementById("trk-count").textContent = "—";
      document.getElementById("trk-updated").textContent = "—";
      return;
    }
    trkData = d;
    document.getElementById("trk-updated").textContent =
      `更新 ${d.updated_at || "—"} · 数据日 ${d.data_day || "—"}`;
    renderTrkStats();
    renderTrkList();
    loadTrkAnalyzeHint();
    if (await resumeTrkJobIfAny()) return;
  } catch (e) {
    console.error(e);
    document.getElementById("trk-list").innerHTML =
      '<div class="empty">追踪快照加载失败</div>';
  }
}

function renderTrkNodes(box, nodes) {
  if (!nodes || !nodes.length) { box.innerHTML = '<div class="empty">无</div>'; return; }
  let h = "<table><thead><tr><th>日期</th><th>账户</th><th>价</th><th>股</th><th>额</th></tr></thead><tbody>";
  for (const n of nodes) {
    h += `<tr><td>${n.date}</td><td>${ACCOUNT_SHORT[n.account] || n.account}</td>`
      + `<td>${fmt(n.price)}</td><td>${n.shares}</td><td>${fmt(n.amount)}</td></tr>`;
  }
  box.innerHTML = h + "</tbody></table>";
}

function renderTrkSentiment(box, sent) {
  if (!sent) { box.innerHTML = '<div class="empty">无舆情报告</div>'; return; }
  let h = "<table><thead><tr><th>报告日</th><th>情绪</th><th>分数</th><th>立场</th>"
    + "<th>条目</th><th>标题</th></tr></thead><tbody>";
  h += `<tr><td>${sent.latest_date || "—"}</td>`
    + `<td>${trkSentBadge(sent.sentiment)}</td>`
    + `<td>${sent.score == null ? "—" : Number(sent.score).toFixed(2)}</td>`
    + `<td>${sent.stance || "—"}</td>`
    + `<td>${sent.news_count ?? "—"}</td>`
    + `<td style="text-align:left;white-space:normal;max-width:380px">${escapeHtml(sent.headline || "—")}</td></tr>`;
  h += "</tbody></table>";
  if (sent.risk_tags && sent.risk_tags.length) {
    h += `<div class="sent-tags" style="margin-top:8px">${sent.risk_tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>`;
  }
  box.innerHTML = h;
}

const TRK_SWING_STATE = {
  hit: "达标", stopped: "止损", expired: "到期", holding: "跟踪中",
  triggered: "待入场", watch: "观察", invalid: "无效", reject: "否决",
};
const TRK_RS_DIR = { up: "看涨", down: "看跌", hold: "观望" };
const TRK_RS_CONS = { agree: "一致", partial: "部分一致", disagree: "分歧" };
const TRK_ANALYST = { market: "行情", news: "新闻", fundamentals: "基面", social: "舆情" };

function trkSideClass(kind) {
  if (kind === "up" || kind === "positive" || kind === "hit" || kind === "predict") return "pos";
  if (kind === "down" || kind === "negative" || kind === "stopped" || kind === "reject") return "neg";
  if (kind === "mixed") return "warn";
  return "";
}

function trkAlignHint(r) {
  const votes = [];
  const s = (r.sentiment || {}).sentiment;
  if (s === "positive") votes.push("多");
  else if (s === "negative") votes.push("空");
  else if (s === "mixed" || s === "neutral") votes.push("中");
  const st = String((r.swing || {}).result || (r.swing || {}).state || "");
  if (st === "hit" || st === "holding" || st === "triggered") votes.push("多");
  else if (st === "stopped" || st === "reject") votes.push("空");
  else if (r.swing) votes.push("中");
  const d = (r.research || {}).merged_direction;
  if (d === "up") votes.push("多");
  else if (d === "down") votes.push("空");
  else if (d === "hold") votes.push("中");
  if (votes.length < 2) return "覆盖不足：有数据的路可参考，其余尚未分析";
  const nBull = votes.filter(x => x === "多").length;
  const nBear = votes.filter(x => x === "空").length;
  if (nBull >= 2 && nBear === 0) return "三路偏多：舆情/短线/研究方向接近";
  if (nBear >= 2 && nBull === 0) return "三路偏空：舆情/短线/研究方向接近";
  if (nBull && nBear) return "三路分歧：多空并存，对照买卖节点看谁先对";
  return "三路偏中性：观望为主";
}

function renderTrkAgents(r) {
  const box = document.getElementById("trk-modal-agents");
  if (!box) return;
  const sent = r.sentiment;
  const sw = r.swing;
  const rs = r.research;
  const sentVal = sent ? (sent.sentiment === "positive" ? "偏多"
    : sent.sentiment === "negative" ? "偏空"
    : sent.sentiment === "mixed" ? "交织" : "中性") : "未分析";
  const sentSub = sent
    ? `分数 ${sent.score == null ? "—" : Number(sent.score).toFixed(2)} · ${sent.latest_date || ""}`
    : "catalog 无报告";
  const swKey = sw ? String(sw.result || sw.state || "") : "";
  const swVal = sw ? (TRK_SWING_STATE[swKey] || swKey) : "未分析";
  let swSub = "tracker 无记录";
  if (sw) {
    const bits = [];
    if (sw.confidence != null) bits.push(`置信 ${Number(sw.confidence).toFixed(2)}`);
    if (sw.result_return != null) bits.push(`兑现 ${pct(sw.result_return)}`);
    else if (sw.mfe != null) bits.push(`MFE ${pct(sw.mfe)}`);
    if (sw.pred_date) bits.push(sw.pred_date);
    swSub = bits.join(" · ");
  }
  const rsVal = rs ? (TRK_RS_DIR[rs.merged_direction] || rs.merged_direction || "—") : "未分析";
  const rsSub = rs
    ? `置信 ${rs.merged_confidence == null ? "—" : Number(rs.merged_confidence).toFixed(2)} · ${TRK_RS_CONS[rs.consensus] || rs.consensus || ""} · ${rs.date || ""}`
    : "尚无研究报告";

  let h = `<div class="trk-agent-grid">
    <div class="trk-agent-card ${sent ? trkSideClass(sent.sentiment) : "empty"}">
      <div class="lbl">舆情记忆</div>
      <div class="val">${sentVal}</div>
      <div class="sub">${sentSub}</div>
    </div>
    <div class="trk-agent-card ${sw ? trkSideClass(swKey) : "empty"}">
      <div class="lbl">短线猎手</div>
      <div class="val">${swVal}</div>
      <div class="sub">${swSub}</div>
    </div>
    <div class="trk-agent-card ${rs ? trkSideClass(rs.merged_direction) : "empty"}">
      <div class="lbl">研究分析</div>
      <div class="val">${rsVal}</div>
      <div class="sub">${rsSub}</div>
    </div>
  </div>
  <div class="trk-align">${trkAlignHint(r)}</div>`;

  if (sw) {
    const cats = (sw.catalysts || []).map(c => `<span class="trk-chip">${escapeHtml(c)}</span>`).join("");
    const reasons = (sw.reasons || []).map(x => `<li>${escapeHtml(x)}</li>`).join("")
      || "<li class='empty'>无理由</li>";
    const delta = sw.delta
      ? `<p>${escapeHtml(sw.delta.date || "")} · ${escapeHtml(sw.delta.headline || "")}</p>`
      : "";
    h += `<details class="trk-fold"><summary>短线猎手辩论</summary>
      <div class="trk-fold-body">
        <div>${cats || '<span class="muted">无催化标签</span>'}</div>
        <p class="kind">入场 ${sw.entry_date || "—"} @ ${sw.entry_price == null ? "—" : fmt(sw.entry_price)}
          · 持有 ${sw.days_held ?? "—"} 日
          · MAE ${sw.mae == null ? "—" : pct(sw.mae)}</p>
        <ul>${reasons}</ul>${delta}
      </div></details>`;
  }
  if (rs) {
    const reasons = (rs.reasons_cn || []).map(x => `<li>${escapeHtml(x)}</li>`).join("");
    const analysts = (rs.analysts || []).map(a => {
      const kind = TRK_ANALYST[a.kind] || a.kind || "分析师";
      return `<div class="kind">${kind}</div><p>${escapeHtml(a.brief || "—")}</p>`;
    }).join("");
    h += `<details class="trk-fold"><summary>研究分析裁决</summary>
      <div class="trk-fold-body">
        <p>${escapeHtml(rs.summary_cn || "—")}</p>
        ${reasons ? `<ul>${reasons}</ul>` : ""}
        ${rs.summary_en ? `<p class="kind">EN</p><p>${escapeHtml(rs.summary_en)}</p>` : ""}
        ${analysts}
      </div></details>`;
  }
  box.innerHTML = h;
}

async function openTrkDetail(instrument) {
  const r = (trkData?.instruments || []).find(x => x.instrument === instrument);
  if (!r) return;
  trkModalInst = instrument;
  const modal = document.getElementById("trk-modal");
  modal.classList.remove("hidden");
  document.getElementById("trk-modal-title").textContent =
    `${r.name || instrument} · ${instrument}`;
  document.getElementById("trk-modal-meta").textContent =
    `账户 ${r.accounts.map(a => ACCOUNT_SHORT[a] || a).join(" / ")} · `
    + `首买 ${r.first_buy_date} @ ${fmt(r.first_buy_price)} · `
    + `最新 ${r.current_price == null ? "—" : fmt(r.current_price)} (${r.current_date || "—"}) · `
    + `净仓 ${r.still_held ? r.net_shares + "股" : "已清仓"}`;

  const stats = document.getElementById("trk-modal-stats");
  stats.innerHTML = [
    { l: "累计涨跌", v: r.cum_ret == null ? "—" : pct(r.cum_ret), c: cls(r.cum_ret), s: `首买→最新` },
    { l: "区间最低", v: r.min_close == null ? "—" : fmt(r.min_close), c: "neg", s: r.min_date || "—" },
    { l: "区间最高", v: r.max_close == null ? "—" : fmt(r.max_close), c: "pos", s: r.max_date || "—" },
    { l: "最大回撤", v: r.max_dd == null ? "—" : pct(r.max_dd), c: cls(r.max_dd), s: "峰值→谷值" },
  ].map(x => `<div class="sent-stat"><div class="label">${x.l}</div>`
    + `<div class="value ${x.c}">${x.v}</div><div class="sub">${x.s}</div></div>`).join("");

  const tags = document.getElementById("trk-modal-tags");
  tags.innerHTML = trkAccountTags(r.accounts)
    + (r.still_held ? `<span class="badge done">持仓 ${r.net_shares}</span>` : '<span class="badge no_trade">已清仓</span>');

  renderTrkAgents(r);
  renderTrkFundflow(document.getElementById("trk-modal-fundflow"), r.fundflow);
  document.getElementById("trk-modal-ff-cap").textContent = r.fundflow
    ? `数据日 ${r.fundflow.date || "当日"} · 东财 push2 · 单位亿`
    : "暂无 · 重新构建后更新";
  renderTrkNodes(document.getElementById("trk-modal-buys"), r.buy_nodes);
  renderTrkNodes(document.getElementById("trk-modal-sells"), r.sell_nodes);
  renderTrkSentiment(document.getElementById("trk-modal-sent"), r.sentiment);
  document.getElementById("trk-modal-sent-cap").textContent =
    r.sentiment ? `报告日 ${r.sentiment.latest_date || "—"}` : "无报告";

  // 走势图 + 买卖节点标注
  const canvas = document.getElementById("trkModalChart");
  const series = r.series || [];
  document.getElementById("trk-modal-cap").textContent = series.length
    ? `日线累计% · ${series[0].date} → ${series[series.length - 1].date} · ${series.length} 根 · 标注买卖节点`
    : "暂无行情";
  if (!series.length) {
    if (charts.trkModalChart) { charts.trkModalChart.destroy(); delete charts.trkModalChart; }
    return;
  }
  const L = series.map(s => s.date);
  const cum = series.map(s => s.cum);
  const lastCum = lastNonNull(cum);
  // 买卖节点：在 cum 序列里找对应日期的 index
  const idxByDate = {};
  L.forEach((d, i) => { idxByDate[d] = i; });
  const buyPts = (r.buy_nodes || []).map(n => ({
    x: n.date, y: cum[idxByDate[n.date]] ?? null,
    label: `买 ${fmt(n.price)}`,
  })).filter(p => p.y != null);
  const sellPts = (r.sell_nodes || []).map(n => ({
    x: n.date, y: cum[idxByDate[n.date]] ?? null,
    label: `卖 ${fmt(n.price)}`,
  })).filter(p => p.y != null);
  setChartCopy(canvas, {
    title: lastCum == null ? "自首买日起累计" : `窗口累计 ${pct(lastCum)}`,
    sub: `${L[0]} → ${L[L.length - 1]} · 起点归零`,
    src: "DAILY CLOSE · CUM % · BUY/SELL MARKS",
  });
  mkChart(canvas, {
    type: "line",
    data: {
      labels: L,
      datasets: [
        lineDS("窗口累计%", cum, lastCum != null && lastCum < 0 ? COLORS.green : COLORS.red, true),
        {
          label: "买入",
          data: buyPts.map(p => p.y),
          borderColor: COLORS.green,
          backgroundColor: COLORS.green,
          pointRadius: 5, pointHoverRadius: 7,
          showLine: false, pointStyle: "triangle",
        },
        {
          label: "卖出",
          data: sellPts.map(p => p.y),
          borderColor: COLORS.red,
          backgroundColor: COLORS.red,
          pointRadius: 5, pointHoverRadius: 7,
          showLine: false, pointStyle: "rectRot",
        },
      ],
    },
    options: {
      ...baseOpts,
      scales: { x: baseOpts.scales.x, y: baseOpts.scales.y },
    },
  });
}

document.getElementById("trk-refresh")?.addEventListener("click", () => loadTracking(false));
document.getElementById("trk-filter")?.addEventListener("input", () => renderTrkList());
document.getElementById("trk-only-held")?.addEventListener("change", () => renderTrkList());
document.getElementById("trk-only-sent")?.addEventListener("change", () => renderTrkList());
document.getElementById("trk-run")?.addEventListener("click", async () => {
  if (!fullAccess) return;
  try {
    const r = await fetch("/api/tracking/run", { method: "POST" });
    const j = await r.json();
    if (j.busy) { renderTrkProgress(j.job || {}); startTrkJobPoll(); return; }
    renderTrkProgress(j.job || {});
    startTrkJobPoll();
  } catch (e) { console.error(e); }
});
document.getElementById("trk-analyze")?.addEventListener("click", async () => {
  if (!fullAccess) return;
  const btn = document.getElementById("trk-analyze");
  const n = Number(btn?.dataset.total || 0);
  const labs = btn?.dataset.labels || "实盘线 + TA线";
  const ok = window.confirm(
    n
      ? `将对「${labs}」当前持仓共 ${n} 只跑舆情、研究分析、短线猎手。\n研究较慢，大约每只 5–8 分钟。确定开始？`
      : "实盘线 + TA线当前无持仓，无法跑分析。"
  );
  if (!ok || !n) return;
  try {
    const r = await fetch("/api/tracking/analyze", { method: "POST" });
    const j = await r.json();
    if (!j.ok && j.message) { alert(j.message); return; }
    renderTrkProgress(j.job || {});
    startTrkJobPoll();
  } catch (e) { console.error(e); }
});
document.getElementById("trk-modal-close")?.addEventListener("click", () => {
  document.getElementById("trk-modal").classList.add("hidden");
  if (charts.trkModalChart) { charts.trkModalChart.destroy(); delete charts.trkModalChart; }
});

// ----------------- 研究分析 -----------------
const RESEARCH_JOB_KEY = "research_job_v1";
let rsInst = null;
let rsJobTimer = null;

const RS_DIR_LABEL = { up: "看涨", down: "看跌", hold: "观望" };
const RS_ACTION_LABEL = { buy: "买入", sell: "卖出", hold: "持有" };
const RS_CONSENSUS_LABEL = { agree: "一致", partial: "部分一致", disagree: "分歧" };

function saveRsJobLocal(job) {
  try {
    if (!job || job.status === "idle") localStorage.removeItem(RESEARCH_JOB_KEY);
    else localStorage.setItem(RESEARCH_JOB_KEY, JSON.stringify(job));
  } catch (_) { /* ignore */ }
}

function rsDirClass(d) {
  return d === "up" ? "ok" : d === "down" ? "danger" : "neutral";
}

function renderRsProgress(job) {
  const box = document.getElementById("rs-progress");
  if (!box || !job) return;
  const st = job.status || "idle";
  if (st === "idle") {
    box.classList.add("hidden");
    box.classList.remove("done", "error");
    return;
  }
  box.classList.remove("hidden");
  box.classList.toggle("done", st === "done");
  box.classList.toggle("error", st === "error");
  const pct = Math.max(0, Math.min(100, Number(job.pct) || 0));
  const bar = document.getElementById("rs-progress-bar");
  const pctEl = document.getElementById("rs-progress-pct");
  const label = document.getElementById("rs-progress-label");
  const msg = document.getElementById("rs-progress-msg");
  if (bar) bar.style.setProperty("--p", (pct / 100).toFixed(4));
  if (pctEl) pctEl.textContent = pct + "%";
  const target = job.current || (job.account ? `账户 ${job.account}` : "研究宇宙");
  if (label) {
    if (st === "running") label.textContent = `研究进行中 · ${target}`;
    else if (st === "done") label.textContent = `研究完成 · ${target}`;
    else if (st === "error") label.textContent = `研究失败 · ${target}`;
    else label.textContent = "研究状态";
  }
  if (msg) {
    const extra = job.done_count != null && job.total
      ? `（${job.done_count}/${job.total}）` : "";
    msg.textContent = (job.message || job.last_line || "—") + extra;
  }
}

function stopRsJobPoll() {
  if (rsJobTimer) { clearInterval(rsJobTimer); rsJobTimer = null; }
}

async function pollRsJobOnce() {
  try {
    const job = await getJSON("/api/research/job");
    renderRsProgress(job);
    saveRsJobLocal(job);
    if (job.status === "done" || job.status === "error") {
      stopRsJobPoll();
      try {
        await loadResearch(true);
        if (job.current) await selectResearch(job.current);
      } catch (_) { /* ignore */ }
      return job;
    }
    if (job.status !== "running") stopRsJobPoll();
    return job;
  } catch (e) {
    console.warn("rs job poll", e);
    return null;
  }
}

function startRsJobPoll() {
  stopRsJobPoll();
  pollRsJobOnce();
  rsJobTimer = setInterval(pollRsJobOnce, 2500);
}

async function resumeRsJobIfAny() {
  try {
    const job = await getJSON("/api/research/job");
    if (job && (job.status === "running" || job.status === "done" || job.status === "error")) {
      renderRsProgress(job);
      saveRsJobLocal(job);
      if (job.status === "running") startRsJobPoll();
      return;
    }
  } catch (_) { /* ignore */ }
  try {
    const raw = localStorage.getItem(RESEARCH_JOB_KEY);
    if (!raw) return;
    const job = JSON.parse(raw);
    if (job?.status === "running") { renderRsProgress(job); startRsJobPoll(); }
    else if (job) renderRsProgress(job);
  } catch (_) { /* ignore */ }
}

async function loadResearch(keepSelection = true) {
  await resumeRsJobIfAny();
  const prev = keepSelection ? rsInst : null;
  const cat = await getJSON("/api/research/catalog");
  const peakEl = document.getElementById("rs-peak");
  peakEl.textContent = cat.peak_hour ? "高峰 · 自部署" : "闲时 · DeepSeek";
  peakEl.className = "badge " + (cat.peak_hour ? "peak" : "offpeak");
  document.getElementById("rs-updated").textContent =
    cat.updated_at ? ("更新 " + cat.updated_at) : "尚无报告";

  const list = document.getElementById("rs-list");
  list.innerHTML = "";
  const items = cat.instruments || [];
  if (!items.length) {
    list.innerHTML = '<div class="empty" style="padding:12px">研究宇宙为空</div>';
    document.getElementById("rs-empty").classList.remove("hidden");
    document.getElementById("rs-detail").classList.add("hidden");
    return;
  }
  items.forEach(it => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sent-item" + (prev === it.instrument ? " active" : "");
    const codeOnly = String(it.instrument || "").replace(/^(SH|SZ)/, "");
    const dir = it.merged_direction;
    const dirLbl = RS_DIR_LABEL[dir] || "—";
    const conf = it.merged_confidence == null ? "" : Number(it.merged_confidence).toFixed(2);
    const srcs = (it.sources || []).join("/");
    b.innerHTML = `<div><span class="code">${codeOnly}</span>`
      + `<span class="name">${it.name || ""}</span></div>`
      + `<div class="snip">${srcs ? ("来源 " + srcs) : "—"}</div>`
      + `<span class="pill ${rsDirClass(dir)}">${dirLbl} ${conf}</span>`;
    b.dataset.instrument = it.instrument;
    b.onclick = () => selectResearch(it.instrument);
    list.appendChild(b);
  });
  const pick = (prev && items.some(x => x.instrument === prev)) ? prev : items[0].instrument;
  await selectResearch(pick);
}

function rsVerdictBlock(prefix, v) {
  const action = v?.action;
  const actLbl = RS_ACTION_LABEL[action] || (v?.parse_error ? "解析失败" : "—");
  const cap = document.getElementById(prefix + "-action");
  if (cap) {
    cap.textContent = `${actLbl} · 置信 ${v?.confidence == null ? "—" : Number(v.confidence).toFixed(2)}`;
    cap.className = "sent-pill " + rsDirClass(
      action === "buy" ? "up" : action === "sell" ? "down" : "hold");
  }
  const sumEl = document.getElementById(prefix + "-summary");
  if (sumEl) sumEl.innerHTML = mdToHtml(v?.summary || "—");
  const ul = document.getElementById(prefix + "-reasons");
  const reasons = v?.reasons || [];
  ul.innerHTML = reasons.length
    ? reasons.map(r => `<li>${mdToHtml(r)}</li>`).join("")
    : "<li class='empty'>暂无</li>";
  const meta = document.getElementById(prefix + "-meta");
  if (meta) {
    const parts = [];
    if (v?.target_price) parts.push("目标价 ¥" + v.target_price);
    if (v?.horizon_days) parts.push("窗口 " + v.horizon_days + " 日");
    if (v?.stop_pct != null) parts.push("止损 " + (v.stop_pct * 100).toFixed(1) + "%");
    if (v?.risk_tags?.length) parts.push("⚠ " + v.risk_tags.join("、"));
    if (v?.parse_error) parts.push("⚠ 裁决 JSON 解析异常");
    meta.textContent = parts.join(" · ") || "";
  }
}

async function selectResearch(instrument) {
  rsInst = instrument;
  document.querySelectorAll("#rs-list .sent-item").forEach(el => {
    el.classList.toggle("active", el.dataset.instrument === instrument);
  });
  document.getElementById("rs-empty").classList.add("hidden");
  document.getElementById("rs-detail").classList.remove("hidden");

  const data = await getJSON(`/api/research/${instrument}`);
  const r = data.report;
  if (!r) {
    document.getElementById("rs-empty").classList.remove("hidden");
    document.getElementById("rs-empty").textContent = `${instrument} 尚无研究报告，请点击「全量研究」或「重新研究本股」。`;
    document.getElementById("rs-detail").classList.add("hidden");
    return;
  }

  const codeOnly = String(instrument).replace(/^(SH|SZ)/, "");
  document.getElementById("rs-name").textContent = r.name || instrument;
  const codePill = document.getElementById("rs-code-pill");
  codePill.textContent = codeOnly || instrument;
  codePill.className = "sent-pill " + rsDirClass(r.merged_direction);
  document.getElementById("rs-date-pill").textContent =
    r.date ? (`报告日 ${r.date}`) : "报告日 —";
  document.getElementById("rs-sub").textContent = `${instrument} · 研究分析 · 双语裁决`;

  // 来源标签
  const tags = document.getElementById("rs-tags");
  const srcMap = { sentiment: "舆情", swing: "短线", orders: "订单", manual: "手动" };
  tags.innerHTML = (r.sources || []).map(s => `<span class="tag">${srcMap[s] || s}</span>`).join("");

  // 合并方向 / 一致性 / pred_id / 覆盖
  const dir = r.merged_direction;
  const dirEl = document.getElementById("rs-direction");
  dirEl.textContent = RS_DIR_LABEL[dir] || "—";
  dirEl.className = "value " + rsDirClass(dir);
  document.getElementById("rs-conf-sub").textContent =
    r.merged_confidence == null ? "置信 —" : ("置信 " + Number(r.merged_confidence).toFixed(2));

  const cons = r.consensus;
  const consEl = document.getElementById("rs-consensus");
  consEl.textContent = RS_CONSENSUS_LABEL[cons] || "—";
  consEl.className = "value " + (cons === "agree" ? "ok" : cons === "disagree" ? "danger" : "warn");
  document.getElementById("rs-consensus-sub").textContent =
    `CN ${RS_ACTION_LABEL[r.verdict_cn?.action] || "—"} / EN ${RS_ACTION_LABEL[r.verdict_en?.action] || "—"}`;

  const predEl = document.getElementById("rs-predid");
  predEl.textContent = r.pred_id || "未入账";
  predEl.className = "value " + (r.pred_id ? "ok" : "neutral");
  document.getElementById("rs-predid-sub").textContent =
    r.merged_direction === "hold" ? "hold 不入账" : "可结算 shadow";

  const bs = (r.meta?.bundle_stats) || {};
  document.getElementById("rs-coverage").textContent =
    bs.market_ok ? (bs.market_bars || "OK") : "无行情";
  document.getElementById("rs-coverage-sub").textContent =
    `新闻 ${bs.news_count ?? 0} · 基面 ${bs.has_fundamentals ? "有" : "无"} · 舆情 ${bs.has_sentiment ? "有" : "无"}`;

  // CN / EN 裁决块
  rsVerdictBlock("rs-cn", r.verdict_cn);
  rsVerdictBlock("rs-en", r.verdict_en);

  // 分析师报告（可折叠，默认展开第一条）
  const an = document.getElementById("rs-analysts");
  const kindMap = { market: "📈 行情", news: "📰 新闻", fundamentals: "📊 基面", social: "💬 舆情" };
  an.innerHTML = "";
  (r.analysts || []).forEach((a, idx) => {
    const wrap = document.createElement("details");
    wrap.className = "rs-analyst";
    if (idx === 0) wrap.setAttribute("open", "");
    const head = document.createElement("summary");
    const langTag = (a.lang || "cn").toUpperCase();
    head.innerHTML = `${kindMap[a.kind] || a.kind} 分析师 <span class="sent-pill muted">${langTag}</span>`;
    wrap.appendChild(head);
    const body = document.createElement("div");
    body.className = "rs-analyst-body";
    body.innerHTML = mdToHtml(a.content || "—");
    wrap.appendChild(body);
    an.appendChild(wrap);
  });
  if (!an.children.length) an.innerHTML = '<div class="empty">暂无分析师报告</div>';

  // 历史
  const hist = document.getElementById("rs-hist");
  const rows = data.history || [];
  if (!rows.length) hist.innerHTML = '<div class="empty">暂无历史</div>';
  else {
    let h = "<table><thead><tr><th>日期</th><th>合并</th><th>CN</th><th>一致</th><th>置信</th></tr></thead><tbody>";
    for (const x of rows) {
      h += `<tr><td>${x.date || "—"}</td>`
        + `<td class="${rsDirClass(x.merged_direction)}">${RS_DIR_LABEL[x.merged_direction] || "—"}</td>`
        + `<td>${RS_ACTION_LABEL[x.action_cn] || "—"}</td>`
        + `<td>${RS_CONSENSUS_LABEL[x.consensus] || "—"}</td>`
        + `<td>${x.merged_confidence == null ? "—" : Number(x.merged_confidence).toFixed(2)}</td></tr>`;
    }
    hist.innerHTML = h + "</tbody></table>";
  }

  const metaEl = document.getElementById("rs-meta");
  if (metaEl) {
    const m = r.meta || {};
    const bs = m.bundle_stats || {};
    const parts = [];
    if (r.created_at) parts.push(`生成 ${r.created_at}`);
    if (m.latency_sec != null) parts.push(`总耗时 ${fmtDur(m.latency_sec)}`);
    // trace 明细
    const trCn = m.trace_cn || [];
    const trEn = m.trace_en || [];
    if (trCn.length || trEn.length) {
      const stepNames = ["多头", "空头", "评判"];
      const cnSteps = trCn.map((t, i) => `${stepNames[i] || t.role}${fmtDur(t.latency_sec)}`).join(" → ");
      const enSteps = trEn.map((t, i) => `${stepNames[i] || t.role}${fmtDur(t.latency_sec)}`).join(" → ");
      if (cnSteps) parts.push(`CN ${cnSteps}`);
      if (enSteps) parts.push(`EN ${enSteps}`);
    }
    if (bs.market_ok) parts.push(`行情 ${bs.market_bars || "?"} 根`);
    if (bs.news_count) parts.push(`新闻 ${bs.news_count} 条`);
    if (m.force_llm) parts.push(`LLM ${m.force_llm}`);
    if (r.status) parts.push(`状态 ${r.status}`);
    metaEl.innerHTML = parts.map(p => `<span>${p}</span>`).join(" · ");
  }
}

async function triggerResearchRun({ instrument = null, btn = null, label = "全量研究" } = {}) {
  if (!fullAccess) {
    alert("演示模式：研究功能仅对白名单 IP 开放");
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = "研究中…"; }
  let triggered = false;
  try {
    const q = new URLSearchParams();
    if (instrument) q.set("instrument", instrument);
    else q.set("account", "live_manual_10k");
    q.set("force", "1");
    const r = await fetch("/api/research/run?" + q.toString(), { method: "POST" });
    const raw = await r.text();
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${raw.slice(0, 120)}`);
    let body = {};
    try { body = JSON.parse(raw); } catch (_) { /* ignore */ }
    triggered = true;
    const job = body.job || {
      status: "running", pct: 5,
      instrument: body.instrument || instrument,
      account: body.account,
      message: body.busy ? "已有任务在跑，接入进度…" : "任务已启动…",
    };
    const target = job.instrument || body.instrument || instrument;
    if (target) rsInst = target;
    renderRsProgress(job);
    saveRsJobLocal(job);
    startRsJobPoll();
    const startedAt = Date.now();
    while (Date.now() - startedAt < 600000) {
      await new Promise(r => setTimeout(r, 2500));
      const cur = await pollRsJobOnce();
      if (!cur || cur.status === "done" || cur.status === "error" || cur.status === "idle") break;
    }
  } catch (e) {
    console.error(e);
    if (!triggered) alert("触发研究失败：" + (e.message || e));
    else alert("研究进度异常：" + (e.message || e));
    renderRsProgress({ status: "error", pct: 100, message: String(e.message || e), current: instrument });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

document.getElementById("rs-run")?.addEventListener("click", () => {
  triggerResearchRun({ btn: document.getElementById("rs-run"), label: "全量研究" });
});
document.getElementById("rs-refresh")?.addEventListener("click", () => loadResearch(true));
document.getElementById("rs-analyze-one")?.addEventListener("click", () => {
  const input = document.getElementById("rs-code");
  const inst = normalizeSentCode(input?.value);
  if (!inst) {
    alert("请输入有效代码，例如 600519 或 SH600519");
    input?.focus();
    return;
  }
  input.value = inst;
  triggerResearchRun({ instrument: inst, btn: document.getElementById("rs-analyze-one"), label: "研究" });
});
document.getElementById("rs-code")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("rs-analyze-one")?.click();
});
document.getElementById("rs-rerun-one")?.addEventListener("click", () => {
  if (!rsInst) { alert("请先选择一只已研究的股票"); return; }
  triggerResearchRun({ instrument: rsInst, btn: document.getElementById("rs-rerun-one"), label: "重新研究本股" });
});

// ----------------- 短线猎手 -----------------
const SWING_ACTION_LABEL = { predict: "预测", watch: "观察", reject: "否决" };
const SWING_STANCE_LABEL = { hold: "持有", exit: "退出", watch: "观察" };
const SWING_JOB_KEY = "swing_job_v1";
let swingJobTimer = null;

function saveSwingJobLocal(job) {
  try {
    if (!job || job.status === "idle") localStorage.removeItem(SWING_JOB_KEY);
    else localStorage.setItem(SWING_JOB_KEY, JSON.stringify(job));
  } catch (_) { /* ignore */ }
}

function renderSwingProgress(job) {
  const box = document.getElementById("swing-progress");
  if (!box || !job) return;
  const st = job.status || "idle";
  if (st === "idle") {
    box.classList.add("hidden");
    box.classList.remove("done", "error");
    return;
  }
  box.classList.remove("hidden");
  box.classList.toggle("done", st === "done");
  box.classList.toggle("error", st === "error");
  const pct = Math.max(0, Math.min(100, Number(job.pct) || 0));
  const bar = document.getElementById("swing-progress-bar");
  const pctEl = document.getElementById("swing-progress-pct");
  const label = document.getElementById("swing-progress-label");
  const msg = document.getElementById("swing-progress-msg");
  if (bar) bar.style.setProperty("--p", (pct / 100).toFixed(4));
  if (pctEl) pctEl.textContent = pct + "%";
  const cur = job.current
    ? `${job.current}${job.current_name ? " " + job.current_name : ""}`
    : (job.account ? `账户 ${job.account}` : "候选池");
  if (label) {
    if (st === "running") label.textContent = `短线猎手进行中 · ${cur}`;
    else if (st === "done") label.textContent = "短线猎手完成";
    else if (st === "error") label.textContent = "短线猎手失败";
    else label.textContent = "短线猎手状态";
  }
  if (msg) {
    const counts = [];
    if (job.done_count != null && job.total)
      counts.push(`${job.done_count}/${job.total}`);
    if (job.n_predict != null)
      counts.push(`预测${job.n_predict}/观察${job.n_watch || 0}/否决${job.n_reject || 0}`);
    const tail = counts.length ? `（${counts.join(" · ")}）` : "";
    msg.textContent = (job.message || job.last_line || "—") + tail;
  }
}

function stopSwingJobPoll() {
  if (swingJobTimer) { clearInterval(swingJobTimer); swingJobTimer = null; }
}

async function pollSwingJobOnce() {
  try {
    const job = await getJSON("/api/swing/job");
    renderSwingProgress(job);
    saveSwingJobLocal(job);
    const btn = document.getElementById("swing-run");
    if (job.status === "running") {
      if (btn) { btn.disabled = true; btn.textContent = "运行中…"; }
    } else if (btn) {
      btn.disabled = false;
      btn.textContent = "运行短线猎手";
    }
    if (job.status === "done" || job.status === "error") {
      stopSwingJobPoll();
      try { await loadSwing(); } catch (_) { /* ignore */ }
      return job;
    }
    if (job.status !== "running") stopSwingJobPoll();
    return job;
  } catch (e) {
    console.warn("swing job poll", e);
    return null;
  }
}

function startSwingJobPoll() {
  stopSwingJobPoll();
  pollSwingJobOnce();
  swingJobTimer = setInterval(pollSwingJobOnce, 2000);
}

async function resumeSwingJobIfAny() {
  try {
    const job = await getJSON("/api/swing/job");
    if (job && (job.status === "running" || job.status === "done" || job.status === "error")) {
      renderSwingProgress(job);
      saveSwingJobLocal(job);
      if (job.status === "running") startSwingJobPoll();
      return;
    }
  } catch (_) { /* ignore */ }
  try {
    const raw = localStorage.getItem(SWING_JOB_KEY);
    if (!raw) return;
    const job = JSON.parse(raw);
    if (job?.status === "running") {
      renderSwingProgress(job);
      startSwingJobPoll();
    } else if (job) {
      renderSwingProgress(job);
    }
  } catch (_) { /* ignore */ }
}

function swingActionBadge(action) {
  const a = action || "watch";
  const cls = a === "predict" ? "done" : (a === "reject" ? "no_trade" : "pending");
  return `<span class="badge ${cls}">${SWING_ACTION_LABEL[a] || a}</span>`;
}

function swingStateBadge(state) {
  const m = {
    triggered: "待入场", holding: "跟踪中", hit: "达标", stopped: "止损",
    expired: "到期", invalid: "失效",
  };
  const cls = state === "hit" ? "done" : (state === "stopped" ? "no_trade" : "pending");
  return `<span class="badge ${cls}">${m[state] || state || "—"}</span>`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function mdInline(s) {
  return escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code class='sw-code'>$1</code>");
}

function parseMarkdownTable(text) {
  const lines = (text || "").split("\n").filter(l => l.trim().startsWith("|"));
  if (lines.length < 2) return null;
  const parseRow = line => line.split("|").slice(1, -1).map(c => c.trim());
  const headers = parseRow(lines[0]);
  const rows = lines.slice(2).map(parseRow).filter(r => r.some(c => c));
  return { headers, rows };
}

function renderStyledTable(tbl, opts = {}) {
  if (!tbl || !tbl.rows.length) return "";
  const { headers, rows } = tbl;
  let h = "<div class='table-wrap swing-table-wrap'><table><thead><tr>";
  headers.forEach(hdr => { h += `<th>${mdInline(hdr)}</th>`; });
  h += "</tr></thead><tbody>";
  rows.forEach(row => {
    h += "<tr>";
    row.forEach((cell, i) => {
      const hdr = (headers[i] || "").toLowerCase();
      let inner = mdInline(cell);
      const actOnly = cell.replace(/↘.*/, "").trim();
      if (hdr === "action" || hdr === "动作") {
        inner = swingActionBadge(actOnly);
      } else if (hdr.includes("action/conf") || hdr.includes("action")) {
        const slash = cell.indexOf("/");
        const act = slash >= 0 ? cell.slice(0, slash).trim() : actOnly;
        if (/^(predict|watch|reject)$/i.test(act)) {
          inner = swingActionBadge(act);
          if (slash >= 0) inner += `<span class="sent-meta">/${escapeHtml(cell.slice(slash + 1))}</span>`;
        }
      } else if (hdr.includes("标的") && /^[A-Z]{2}\d{6}/.test(cell)) {
        const inst = cell.split(/\s+/)[0];
        inner = `<span class="clickable" data-inst="${inst}">${mdInline(cell)}</span>`;
      } else if (hdr.includes("理由") || hdr.includes("headline")) {
        h += `<td class="sw-reason-cell">${inner}</td>`;
        return;
      }
      h += `<td>${inner}</td>`;
    });
    h += "</tr>";
  });
  h += "</tbody></table></div>";
  return h;
}

function parseStockBlocks(body) {
  const blocks = (body || "").split(/\n### /).filter(b => b.trim());
  return blocks.map(block => {
    const lines = block.split("\n");
    const head = lines[0].trim();
    const parts = head.split("·").map(s => s.trim());
    const instMatch = parts[0]?.match(/^(\S+)\s+(.*)$/);
    const inst = instMatch?.[1] || parts[0] || "";
    const name = instMatch?.[2] || "";
    const action = (parts.find(p => /^(predict|watch|reject)$/i.test(p)) || "watch").toLowerCase();
    const swingPart = parts.find(p => p.includes("swing"));
    const swing = swingPart ? swingPart.replace(/.*swing\s*/i, "") : "";
    const gatePart = parts.find(p => p.includes("门槛"));
    const gate = gatePart ? gatePart.replace(/.*门槛\s*/i, "") : "";
    const bullets = [];
    const materials = [];
    let inMaterials = false;
    for (const line of lines.slice(1)) {
      if (line.startsWith("- **近期材料**")) { inMaterials = true; continue; }
      if (line.startsWith("  - ")) {
        materials.push(line.replace(/^\s*-\s*/, ""));
        continue;
      }
      if (line.startsWith("- ")) {
        bullets.push(line.replace(/^-\s*/, ""));
        inMaterials = false;
      }
    }
    return { inst, name, action, swing, gate, bullets, materials };
  });
}

function renderStockCard(stock, compact = false) {
  const catalyst = stock.bullets.find(b => b.includes("催化"));
  const risk = stock.bullets.find(b => b.includes("风险"));
  const meta = stock.bullets.find(b => b.includes("置信度") || b.includes("目标档"));
  const reasons = stock.bullets.filter(b =>
    !b.includes("催化") && !b.includes("风险") && !b.includes("置信度") && !b.includes("目标档"));
  const mats = stock.materials.slice(0, compact ? 3 : 5);
  return `
    <div class="swing-pick-card action-${stock.action}">
      <div class="swing-pick-head">
        <div class="swing-pick-ident">
          <span class="swing-pick-code clickable" data-inst="${stock.inst}">${stock.inst}</span>
          <span class="swing-pick-name">${escapeHtml(stock.name)}</span>
        </div>
        <div class="swing-pick-badges">
          ${swingActionBadge(stock.action)}
          <span class="swing-pick-score">${stock.swing ? "swing " + stock.swing : ""}</span>
          ${stock.gate ? `<span class="badge peak">${escapeHtml(stock.gate)}</span>` : ""}
        </div>
      </div>
      ${meta ? `<div class="swing-pick-meta">${mdInline(meta)}</div>` : ""}
      ${catalyst ? `<div class="swing-pick-tagline pos-tag">${mdInline(catalyst)}</div>` : ""}
      ${risk ? `<div class="swing-pick-tagline risk-tag">${mdInline(risk)}</div>` : ""}
      ${reasons.length ? `<ul class="swing-pick-reasons">${reasons.map(r =>
        `<li>${mdInline(r)}</li>`).join("")}</ul>` : ""}
      ${mats.length ? `<div class="swing-pick-materials"><div class="swing-mat-label">近期材料</div>${mats.map(m =>
        `<div class="swing-mat-item">${mdInline(m)}</div>`).join("")}</div>` : ""}
    </div>`;
}

function renderSwingDailyDoc(md, meta) {
  if (!md) return '<div class="empty">暂无日报</div>';
  const sections = md.split(/\n## /);
  const header = sections[0] || "";
  const titleM = header.match(/^# (.+)/m);
  const quoteM = header.match(/^> (.+)/m);
  let html = '<div class="swing-doc">';
  html += `<div class="swing-doc-head">
    <div class="swing-doc-title">${mdInline(titleM?.[1] || "短线猎手日报")}</div>
    ${quoteM ? `<div class="swing-doc-sub">${mdInline(quoteM[1])}</div>` : ""}
  </div>`;

  if (meta?.gate) {
    const g = meta.gate;
    html += `<div class="swing-gate-panel">
      <div class="swing-gate-title">预测门槛</div>
      <div class="swing-gate-row">
        <span class="swing-chip">初始 <strong>${escapeHtml(g.initial_tier || "strict")}</strong></span>
        <span class="swing-chip applied">采用 <strong>${escapeHtml(g.applied_tier || "—")}</strong></span>
        ${g.fallback_used ? '<span class="swing-chip warn">已降档</span>' : ""}
        <span class="swing-chip muted">predict ${g.n_predict_initial ?? "—"} → ${g.n_predict_final ?? "—"}</span>
      </div>
      <div class="sent-meta">${escapeHtml(g.label_applied || "")}</div>
    </div>`;
  }

  for (let i = 1; i < sections.length; i++) {
    const chunk = sections[i];
    const nl = chunk.indexOf("\n");
    const title = chunk.slice(0, nl > 0 ? nl : chunk.length).trim();
    const body = nl > 0 ? chunk.slice(nl + 1).trim() : "";

    if (title.includes("汇总")) {
      const tbl = parseMarkdownTable(body);
      const gateLine = body.split("\n").find(l => l.includes("预测门槛"));
      html += `<div class="swing-section"><h4 class="swing-section-title">${mdInline(title)}</h4>`;
      if (tbl && tbl.rows[0]) {
        const [np, nw, nr] = tbl.rows[0];
        html += `<div class="swing-summary-chips">
          <div class="swing-sum-chip predict"><span class="n">${np}</span><span class="l">预测</span></div>
          <div class="swing-sum-chip watch"><span class="n">${nw}</span><span class="l">观察</span></div>
          <div class="swing-sum-chip reject"><span class="n">${nr}</span><span class="l">否决</span></div>
        </div>`;
      }
      if (gateLine) html += `<div class="sent-meta">${mdInline(gateLine)}</div>`;
      html += "</div>";
      continue;
    }

    const stocks = parseStockBlocks(body);
    const isWatch = title.includes("watch");
    const limit = isWatch ? 5 : 20;
    html += `<div class="swing-section">
      <h4 class="swing-section-title">${mdInline(title)}</h4>
      <div class="swing-pick-grid${isWatch ? " compact" : ""}">`;
    if (!stocks.length) {
      html += '<div class="empty">（无）</div>';
    } else {
      stocks.slice(0, limit).forEach(s => { html += renderStockCard(s, isWatch); });
      if (stocks.length > limit) {
        html += `<div class="sent-meta">另有 ${stocks.length - limit} 只未展示</div>`;
      }
    }
    html += "</div></div>";
  }
  html += "</div>";
  return html;
}

function renderSwingEvalDoc(md) {
  if (!md) return '<div class="empty">尚无评测报告</div>';
  const sections = md.split(/\n## /);
  const header = sections[0] || "";
  const titleM = header.match(/^# (.+)/m);
  const quoteM = header.match(/^> (.+)/m);
  let html = '<div class="swing-doc swing-eval-doc">';
  html += `<div class="swing-doc-head">
    <div class="swing-doc-title">${mdInline(titleM?.[1] || "LLM 评测")}</div>
    ${quoteM ? `<div class="swing-doc-sub">${mdInline(quoteM[1])}</div>` : ""}
  </div>`;

  for (let i = 1; i < sections.length; i++) {
    const chunk = sections[i];
    const nl = chunk.indexOf("\n");
    const title = chunk.slice(0, nl > 0 ? nl : chunk.length).trim();
    const body = nl > 0 ? chunk.slice(nl + 1).trim() : "";

    html += `<div class="swing-section"><h4 class="swing-section-title">${mdInline(title)}</h4>`;

    if (title.includes("预测门槛")) {
      const items = body.split("\n").filter(l => l.startsWith("- "));
      html += '<div class="swing-gate-panel flat">';
      items.forEach(item => {
        html += `<div class="swing-gate-line">${mdInline(item.replace(/^-\s*/, ""))}</div>`;
      });
      html += "</div>";
    } else if (title.includes("汇总")) {
      html += '<ul class="swing-eval-summary">';
      body.split("\n").filter(l => l.startsWith("- ")).forEach(l => {
        html += `<li>${mdInline(l.replace(/^-\s*/, ""))}</li>`;
      });
      html += "</ul>";
    } else if (body.includes("|")) {
      const tbl = parseMarkdownTable(body);
      html += renderStyledTable(tbl, { actionCol: tbl?.headers.findIndex(h =>
        h.toLowerCase().includes("action")) });
    } else {
      html += `<div class="sent-meta">${mdInline(body)}</div>`;
    }
    html += "</div>";
  }
  html += "</div>";
  return html;
}

function bindSwingDocClicks(root) {
  if (!root) return;
  root.querySelectorAll("[data-inst].clickable").forEach(el => {
    el.onclick = () => openSwingDetail(el.dataset.inst);
  });
}

function renderSwingReport(md, meta) {
  const box = document.getElementById("swing-report");
  const metaBox = document.getElementById("swing-report-meta");
  if (!box) return;
  box.innerHTML = renderSwingDailyDoc(md, meta);
  bindSwingDocClicks(box);
  if (metaBox && meta) {
    const sp = meta.sentiment_prep || {};
    const ds = meta.delta_summary || {};
    const chips = [];
    if (sp.collected != null || sp.skipped != null) {
      chips.push(`舆情补齐 <strong>${sp.collected ?? 0}</strong> · 跳过 ${sp.skipped ?? 0}`);
    }
    if (ds.updated != null) chips.push(`Delta ${ds.updated}`);
    if (meta.n_llm_ok != null) chips.push(`LLM ${meta.n_llm_ok}`);
    if (meta.source === "eval") chips.push("来源评测");
    metaBox.innerHTML = chips.length
      ? `<div class="swing-meta-chips">${chips.map(c => `<span class="swing-chip muted">${c}</span>`).join("")}</div>`
      : "";
  }
}

function renderSwingEval(data) {
  const box = document.getElementById("swing-eval");
  const sel = document.getElementById("swing-eval-day");
  if (!box) return;
  if (!data.days?.length) {
    box.innerHTML = '<div class="empty">尚无评测报告（run_swing_eval.py）</div>';
    if (sel) sel.innerHTML = "";
    return;
  }
  const paint = (markdown) => {
    box.innerHTML = renderSwingEvalDoc(markdown);
    bindSwingDocClicks(box);
  };
  if (sel) {
    sel.innerHTML = data.days.map(d =>
      `<option value="${d}"${d === data.day ? " selected" : ""}>${d}</option>`).join("");
    sel.onchange = async () => {
      const r = await safeJSON(`/api/swing/eval?day=${sel.value}`);
      paint(r?.markdown || "");
    };
  }
  paint(data.markdown);
}

function swingMdToHtml(md) {
  return renderSwingDailyDoc(md, null);
}

function renderSwingPatterns(data) {
  const box = document.getElementById("swing-patterns");
  const cnt = document.getElementById("swing-patterns-count");
  if (!box) return;
  const patterns = data.patterns || [];
  if (cnt) cnt.textContent = patterns.length ? `(${data.count ?? patterns.length} 条)` : "";
  if (!patterns.length) {
    box.innerHTML = '<div class="empty">尚无 hit 挖掘案例（达标后自动写入 swing_patterns.yaml）</div>';
    return;
  }
  let h = "<table><thead><tr><th>标的</th><th>预测日</th><th>收益</th>"
    + "<th>催化</th><th>理由</th></tr></thead><tbody>";
  for (const p of patterns.slice(0, 20)) {
    h += "<tr>"
      + `<td class="clickable" data-inst="${p.instrument}">${p.instrument} ${p.name || ""}</td>`
      + `<td>${p.pred_date || "—"}</td>`
      + `<td class="${cls(p.result_return)}">${p.result_return != null ? pct(p.result_return * 100) : "—"}</td>`
      + `<td style="text-align:left">${(p.catalysts || []).join("、") || "—"}</td>`
      + `<td class="truncate-cell" style="text-align:left">${(p.reasons || [])[0] || "—"}</td>`
      + "</tr>";
  }
  box.innerHTML = h + "</tbody></table>";
  box.querySelectorAll("td.clickable").forEach(td =>
    td.onclick = () => openSwingDetail(td.dataset.inst));
}

async function openSwingDetail(instrument) {
  const modal = document.getElementById("swing-detail-modal");
  if (!modal) { openStock(instrument); return; }
  modal.classList.remove("hidden");
  document.getElementById("sw-detail-title").textContent = instrument + " · 加载中…";
  try {
    const d = await getJSON(`/api/swing/detail/${instrument}`);
    const p = d.prediction;
    const ar = d.active_record;
    document.getElementById("sw-detail-title").textContent =
      `${instrument} ${p?.name || ar?.name || ""}`;
    const meta = document.getElementById("sw-detail-meta");
    if (ar) {
      meta.innerHTML = `${swingStateBadge(ar.state)} · 预测日 ${ar.pred_date}`
        + ` · 入场 ${ar.entry_price ?? "—"} · 持有 ${ar.days_held ?? 0} 日`
        + ` · MFE ${ar.mfe != null ? pct(ar.mfe * 100) : "—"}`;
    } else if (p) {
      meta.innerHTML = `${swingActionBadge(p.action)} · swing ${Number(p.swing_score).toFixed(2)}`
        + ` · conf ${Number(p.confidence).toFixed(2)}`;
    } else {
      meta.innerHTML = "暂无活跃跟踪或最新预测";
    }
    const predBox = document.getElementById("sw-detail-pred");
    if (p) {
      predBox.innerHTML = "<ul class='sent-ul'>"
        + (p.reasons || []).map(r => `<li>${r}</li>`).join("")
        + "</ul>"
        + (p.catalysts?.length ? `<p><strong>催化</strong> ${p.catalysts.join("、")}</p>` : "")
        + (p.risk_tags?.length ? `<p><strong>风险</strong> ${p.risk_tags.join("、")}</p>` : "")
        + ((p.news_brief || []).length
          ? "<p><strong>材料</strong></p><ul class='sent-ul'>"
            + p.news_brief.slice(0, 6).map(e =>
              `<li>[${e.source || ""}] ${e.published || ""} ${e.title || ""}</li>`).join("")
            + "</ul>" : "");
    } else {
      predBox.innerHTML = '<div class="empty">无最新预测条目</div>';
    }
    const deltaBox = document.getElementById("sw-detail-deltas");
    const deltas = d.deltas || [];
    if (!deltas.length) {
      deltaBox.innerHTML = '<div class="empty">尚无 delta（每日仅分析新增公告）</div>';
    } else {
      deltaBox.innerHTML = deltas.slice(0, 12).map(dl => `
        <div class="delta-card">
          <div class="delta-head">
            <span class="badge pending">${SWING_STANCE_LABEL[dl.stance] || dl.stance || "—"}</span>
            <span class="delta-date">${dl.date || ""}</span>
            ${dl.invalidate ? '<span class="badge no_trade">证伪</span>' : ""}
          </div>
          <div><strong>${dl.headline || ""}</strong></div>
          <div class="sent-meta">${dl.summary || ""}</div>
          ${(dl.new_items_preview || []).slice(0, 3).map(it =>
            `<div class="sent-meta">· [${it.source || ""}] ${it.title || ""}</div>`).join("")}
        </div>`).join("");
    }
    const dailyBox = document.getElementById("sw-detail-daily");
    const rec = ar || (d.tracker?.records || [])[0];
    const daily = rec?.daily || [];
    if (!daily.length) {
      dailyBox.innerHTML = '<div class="empty">尚无逐日收盘数据</div>';
    } else {
      let ht = "<table><thead><tr><th>日期</th><th>收盘</th><th>收益</th></tr></thead><tbody>";
      for (const row of daily) {
        ht += `<tr><td>${row.date}</td><td>${row.close}</td>`
          + `<td class="${cls(row.ret)}">${pct(row.ret * 100)}</td></tr>`;
      }
      dailyBox.innerHTML = ht + "</tbody></table>";
    }
  } catch (e) {
    document.getElementById("sw-detail-meta").textContent = "加载失败：" + (e.message || e);
  }
}

document.getElementById("sw-detail-close")?.addEventListener("click", () =>
  document.getElementById("swing-detail-modal")?.classList.add("hidden"));
document.getElementById("swing-detail-modal")?.addEventListener("click", (e) => {
  if (e.target.id === "swing-detail-modal") e.target.classList.add("hidden");
});

async function safeJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return { _error: url + " " + r.status };
    return await r.json();
  } catch (e) {
    return { _error: url + " " + (e.message || e) };
  }
}

async function loadSwing() {
  const errBox = document.getElementById("swing-load-err");
  const showErr = (msg) => {
    if (errBox) {
      errBox.textContent = msg;
      errBox.classList.remove("hidden");
    } else {
      console.error(msg);
    }
  };
  if (errBox) errBox.classList.add("hidden");

  const [cat, trk, report, evalData, patterns] = await Promise.all([
    safeJSON("/api/swing/catalog"),
    safeJSON("/api/swing/tracking?limit=80"),
    safeJSON("/api/swing/report"),
    safeJSON("/api/swing/eval"),
    safeJSON("/api/swing/patterns?limit=20"),
  ]);
  const errors = [cat, trk, report, evalData, patterns]
    .filter(x => x && x._error).map(x => x._error);
  if (errors.length) {
    showErr("部分数据加载失败（若刚更新代码请重启看板服务）：" + errors.join("；"));
  }
  if (cat?._error) {
    document.getElementById("swing-preds").innerHTML =
      '<div class="empty">无法连接短线猎手 API，请重启 webapp 后刷新</div>';
    return;
  }
  document.getElementById("swing-updated").textContent =
    cat.updated_at ? `更新 ${cat.updated_at}` : "—";
  const title = document.getElementById("swing-pred-title");
  if (title) title.textContent = `最新预测 · ${cat.prediction_day || "无"}`;

  resumeSwingJobIfAny();

  renderSwingReport(report?.markdown || "", report?.meta || cat.prediction_meta);
  renderSwingEval(evalData?._error ? { days: [], markdown: "" } : evalData);
  renderSwingPatterns(patterns?._error ? { patterns: [] } : patterns);

  const st = cat.stats || (trk && !trk._error ? trk.stats : {}) || {};
  const statsBox = document.getElementById("swing-stats");
  if (statsBox) {
    statsBox.innerHTML = "";
    const hr = st.hit_rate != null ? pct(st.hit_rate * 100, 1) : "—";
    const meta = cat.prediction_meta || report.meta || {};
    const sp = meta.sentiment_prep || {};
    statsBox.appendChild(card("活跃跟踪", String(st.active ?? cat.active?.length ?? 0), "只", ""));
    statsBox.appendChild(card("已结算", String(st.settled ?? 0), `hit率 ${hr}`, st.hit_rate > 0.25 ? "pos" : ""));
    statsBox.appendChild(card("达标 hit", String(st.hit ?? 0), `止损 ${st.stopped ?? 0} · 到期 ${st.expired ?? 0}`, ""));
    statsBox.appendChild(card("均收益(结算)", st.avg_return != null ? pct(st.avg_return * 100, 1) : "—",
      st.note || "收盘口径 · 样本不足", cls(st.avg_return)));
    if (sp.collected != null) {
      statsBox.appendChild(card("舆情补齐", String(sp.collected), `跳过 ${sp.skipped ?? 0}`, ""));
    }
  }

  const activeBox = document.getElementById("swing-active");
  const active = cat.active || [];
  if (!active.length) {
    activeBox.innerHTML = '<div class="empty">暂无活跃跟踪（predict 动作会入跟踪）</div>';
  } else {
    let h = "<table><thead><tr><th>标的</th><th>预测日</th><th>状态</th>"
      + "<th>置信度</th><th>入场</th><th>持有</th><th>MFE</th><th>最新 Delta</th></tr></thead><tbody>";
    for (const r of active) {
      const ld = r.latest_delta;
      const deltaTxt = ld
        ? `${SWING_STANCE_LABEL[ld.stance] || ld.stance || ""} ${(ld.headline || "").slice(0, 28)}`
        : "—";
      h += "<tr>"
        + `<td class="clickable" data-inst="${r.instrument}">${r.instrument} ${r.name || ""}</td>`
        + `<td>${r.pred_date || "—"}</td><td>${swingStateBadge(r.state)}</td>`
        + `<td>${r.confidence != null ? Number(r.confidence).toFixed(2) : "—"}</td>`
        + `<td>${r.entry_price != null ? fmt(r.entry_price) : "—"}</td>`
        + `<td>${r.days_held ?? 0}日</td>`
        + `<td class="${cls(r.mfe)}">${r.mfe != null ? pct(r.mfe * 100) : "—"}</td>`
        + `<td class="truncate-cell" style="text-align:left">${deltaTxt}</td>`
        + "</tr>";
    }
    activeBox.innerHTML = h + "</tbody></table>";
    activeBox.querySelectorAll("td.clickable").forEach(td =>
      td.onclick = () => openSwingDetail(td.dataset.inst));
  }

  const preds = cat.predictions || [];
  const predBox = document.getElementById("swing-preds");
  if (!preds.length) {
    predBox.innerHTML = '<div class="empty">尚无预测文件；evening 流水线或点击「运行短线猎手」</div>';
  } else {
    let h = "<table><thead><tr><th>标的</th><th>动作</th><th>置信度</th><th>融合分</th>"
      + "<th>门槛</th><th>目标档</th><th>催化</th><th>理由</th></tr></thead><tbody>";
    for (const p of preds.slice(0, 40)) {
      const tiers = (p.target_tiers || []).map(t =>
        `+${(t.pct * 100).toFixed(0)}%@${((t.prob || 0) * 100).toFixed(0)}%`).join(" · ");
      const gt = (p.meta || {}).gate_tier || "—";
      const gate = (p.meta || {}).gate_fallback ? `${gt} ↘` : gt;
      h += "<tr>"
        + `<td class="clickable" data-inst="${p.instrument}">${p.instrument} ${p.name || ""}</td>`
        + `<td>${swingActionBadge(p.action)}</td>`
        + `<td>${p.confidence != null ? Number(p.confidence).toFixed(2) : "—"}</td>`
        + `<td>${p.swing_score != null ? Number(p.swing_score).toFixed(2) : "—"}</td>`
        + `<td>${gate}</td>`
        + `<td style="text-align:left">${tiers || "—"}</td>`
        + `<td style="text-align:left">${(p.catalysts || []).join("、") || "—"}</td>`
        + `<td class="truncate-cell" style="text-align:left;white-space:normal">${(p.reasons || [])[0] || "—"}</td>`
        + "</tr>";
    }
    predBox.innerHTML = h + "</tbody></table>";
    predBox.querySelectorAll("td.clickable").forEach(td =>
      td.onclick = () => openSwingDetail(td.dataset.inst));
  }

  const settled = ((trk && !trk._error ? trk.records : []) || []).filter(r =>
    r.result === "hit" || r.result === "stopped" || r.result === "expired");
  const setBox = document.getElementById("swing-settled");
  if (!settled.length) {
    setBox.innerHTML = '<div class="empty">尚无结算记录（需 predict 入跟踪并经历 ≥1 交易日）</div>';
  } else {
    let h = "<table><thead><tr><th>标的</th><th>预测日</th><th>结果</th>"
      + "<th>收益</th><th>持有</th><th>催化</th></tr></thead><tbody>";
    for (const r of settled.slice(0, 30)) {
      h += "<tr>"
        + `<td class="clickable" data-inst="${r.instrument}">${r.instrument} ${r.name || ""}</td>`
        + `<td>${r.pred_date}</td><td>${swingStateBadge(r.result)}</td>`
        + `<td class="${cls(r.result_return)}">${r.result_return != null ? pct(r.result_return * 100) : "—"}</td>`
        + `<td>${r.days_held ?? 0}日</td>`
        + `<td style="text-align:left">${(r.catalysts || []).join("、") || "—"}</td>`
        + "</tr>";
    }
    setBox.innerHTML = h + "</tbody></table>";
    setBox.querySelectorAll("td.clickable").forEach(td =>
      td.onclick = () => openSwingDetail(td.dataset.inst));
  }
}

document.getElementById("swing-refresh")?.addEventListener("click", () => loadSwing());

document.getElementById("swing-run")?.addEventListener("click", async () => {
  if (!fullAccess) {
    alert("演示模式：仅对白名单 IP 开放");
    return;
  }
  const btn = document.getElementById("swing-run");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    const r = await fetch("/api/swing/run?account=live_manual_10k&force=true", { method: "POST" });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || r.status);
    const job = body.job || { status: "running", pct: 3, message: body.busy ? "已有任务进行中…" : "已启动…" };
    renderSwingProgress(job);
    saveSwingJobLocal(job);
    startSwingJobPoll();
    if (body.busy) {
      // 不弹窗，进度条已展示当前任务
    }
  } catch (e) {
    alert("启动失败：" + (e.message || e));
    btn.disabled = false;
    btn.textContent = "运行短线猎手";
  }
});

// ----------------- 路由 -----------------
// ----------------- 大盘看板（market_board，池内统计，只读） -----------------
let boardDay = null;       // 选中的快照日（null=最新）
let boardDaysLoaded = false;

function boardGradeBadge(g) {
  const color = { S: "#e5534b", A: "#d29922", B: "#3b82d6", C: "#8b98a9" }[g] || "#8b98a9";
  return `<span class="board-grade" style="background:${color}">${g}</span>`;
}

async function loadBoard() {
  // 1) 快照日期下拉（首次或最新日变化时刷新）
  try {
    const dd = await getJSON("/api/board/days");
    const sel = document.getElementById("board-day");
    const latest = dd.days[0] || null;
    if (!boardDaysLoaded || sel.dataset.latest !== latest) {
      sel.innerHTML = dd.days.map(d => `<option value="${d}">${d}</option>`).join("");
      if (latest) {
        sel.value = latest;
        boardDay = latest;
        sel.dataset.latest = latest;
      }
      sel.onchange = () => { boardDay = sel.value; loadBoardData(); };
      boardDaysLoaded = true;
    }
  } catch (e) { console.error(e); }
  await loadBoardData();
}

async function loadBoardData() {
  const qs = boardDay ? `?day=${boardDay}` : "";
  const [ov, lad, stg, thm, rot, flow, nw] = await Promise.all([
    getJSON("/api/board/overview" + qs),
    getJSON("/api/board/ladder" + qs),
    getJSON("/api/board/strong" + qs),
    getJSON("/api/board/themes" + qs),
    getJSON("/api/board/rotation" + qs),
    getJSON("/api/board/fundflow" + qs),
    getJSON("/api/board/news" + qs),
  ]);
  const meta = document.getElementById("board-meta");
  const jobEl = document.getElementById("board-job");
  if (!ov.available) {
    meta.textContent = "尚无快照";
    jobEl.textContent = ov.disclaimer || "";
    if (ov.job && ov.job.status === "running") jobEl.textContent = "快照生成中… " + (ov.job.message || "");
    return;
  }
  jobEl.textContent = ov.job && ov.job.status === "running" ? "后台生成中…" : "";
  meta.textContent = `盘后 ${ov.day} | 池内 ${ov.pool?.size ?? "-"} 只 | 生成于 ${ov.generated || "-"}` +
    (ov.status === "partial" ? ` | ⚠ 部分模块失败 ${ov.errors.length}` : "");

  renderBoardPanorama(ov);
  renderBoardLadder(lad);
  renderBoardStrong(stg, flow);
  renderBoardLimit(lad);
  renderBoardThemes(thm, rot);
  renderBoardNews(nw);

  await applyBoardIntraday();
}

function tempTone(temp) {
  if (temp >= 60) return GLANCE.hero;
  if (temp >= 40) return COLORS.amber;
  return GLANCE.muted;
}

function renderThermoBlock(el, { temp, label, emotion, reasons, t = {}, live = false } = {}) {
  if (!el) return;
  const tone = tempTone(temp);
  const up = t.up ?? 0, down = t.down ?? 0;
  const tot = Math.max(1, up + down);
  const upW = ((up / tot) * 100).toFixed(1);
  const tClamp = Math.max(0, Math.min(100, temp ?? 50));
  const broken = t.broken_rate != null ? `（${fmt(t.broken_rate * 100, 0)}%）` : "";
  el.innerHTML = `
    <div class="thermo-head">
      <div class="thermo-value" style="color:${tone}">${fmt(temp, 0)}°</div>
      <div class="thermo-copy">
        <div class="thermo-label" style="color:${tone}">${label || "—"}</div>
        <div class="thermo-emotion">${live ? "盘中口径（手动刷新）" : `情绪 ${emotion || "—"}`}</div>
      </div>
    </div>
    <div class="temp-track" aria-hidden="true">
      <span class="temp-fill" style="width:${tClamp}%;background:${tone}"></span>
      <span class="temp-thumb" style="left:${tClamp}%;background:${tone}"></span>
    </div>
    <div class="breadth" role="img" aria-label="上涨 ${up} 下跌 ${down}">
      <div class="breadth-track">
        <span class="breadth-up" style="width:${upW}%"></span>
        <span class="breadth-dn" style="flex:1"></span>
      </div>
      <div class="breadth-legend">
        <span class="pos">上涨 ${up}</span>
        <span class="neg">下跌 ${down}</span>
      </div>
    </div>
    <div class="thermo-facts">
      <span class="pos">涨停 ${t.limit_up ?? 0}</span>
      <span class="neg">跌停 ${t.limit_down ?? 0}</span>
      <span>炸板 ${t.broken ?? 0}${broken}</span>
      <span>最高连板 ${t.max_streak ?? 0}</span>
      <span class="pos">&gt;5% ${t.up_gt5 ?? 0}</span>
      <span class="neg">跌超5% ${t.down_lt5 ?? 0}</span>
    </div>
    ${reasons?.length ? `<p class="thermo-why">${reasons.join("；")}</p>` : ""}`;
}

// ---- 全景 ----
function renderBoardPanorama(ov) {
  const t = ov.thermometer || {};
  const temp = ov.temperature ?? 50;
  renderThermoBlock(document.getElementById("board-thermo"), {
    temp,
    label: ov.temperature_label || "—",
    emotion: ov.emotion,
    reasons: ov.emotion_reasons,
    t,
  });

  const env = ov.index_env || {};
  document.getElementById("board-index-env").innerHTML = env.available ? `
    <div class="env-compact">
      <div class="env-score">${fmt(env.env_score, 0)}</div>
      <div>
        <div class="env-label">${env.env_label}</div>
        <div class="env-sub">${env.benchmark || ""} · ${env.trend || ""}</div>
      </div>
    </div>
    <div class="env-facts">
      <div>20日线 <b class="${env.above_ma20 ? "pos" : "neg"}">${env.above_ma20 ? "上方" : "下方"}</b></div>
      <div>20日 <span class="${cls(env.ret20 * 100)}">${pct(env.ret20 * 100)}</span></div>
      <div>量能 5/20 ${fmt(env.vol_ratio)}</div>
      <div>收盘 ${fmt(env.close)}</div>
    </div>` : `<div class="empty">指数环境不可用${env.error ? "：" + env.error : ""}</div>`;

  setChartCopy("boardEmotionChart", {
    title: `${ov.emotion || "情绪周期"} · 近15日`,
    sub: "温度线 · 涨停红柱 / 跌停绿柱（家数，跌停向下）",
    src: "POOL · 15D · TEMPERATURE + LIMITS",
  });
  setChartCopy("boardPeriodChart", {
    title: `${ov.period30 || "30日行情"} · 池内等权`,
    sub: (ov.period30_reasons || []).join("；") || "净值相对窗口起点 · %",
    src: "POOL EQUAL WEIGHT · 30D",
  });
  const emoLabel = document.getElementById("board-emotion-label");
  const perLabel = document.getElementById("board-period-label");
  if (emoLabel) emoLabel.textContent = "";
  if (perLabel) perLabel.textContent = "";

  const cols = [
    ["instrument", "代码"], ["name", "名称"], ["industry", "行业"],
    ["ret", "涨幅%"], ["streak", "连板"], ["limit_type", "涨停类型"],
  ];
  const mapRows = rows => (rows || []).map(r => ({
    ...r, ret: r.ret != null ? +(r.ret * 100).toFixed(2) : null,
    limit_type: r.limit_type || "-", streak: r.streak || "",
  }));
  renderTable(document.getElementById("board-gainers"), mapRows(ov.gainers), cols, "无数据", "instrument");
  renderTable(document.getElementById("board-losers"), mapRows(ov.losers), cols, "无数据", "instrument");

  // 曲线图
  loadBoardCharts();
}

async function loadBoardCharts() {
  const qs = boardDay ? `?day=${boardDay}` : "";
  const cyc = await getJSON("/api/board/cycle" + qs);
  if (!cyc.available) return;

  const s15 = cyc.series15 || [];
  const n15 = s15.length;
  mkChart(document.getElementById("boardEmotionChart"), {
    type: "bar",
    data: {
      labels: s15.map(x => x.day.slice(5)),
      datasets: [
        {
          type: "line",
          ...lineDS("温度", s15.map(x => x.temperature), GLANCE.ink),
          yAxisID: "y",
          order: 2,
        },
        {
          type: "bar",
          label: "涨停家数",
          data: s15.map(x => x.limit_up),
          backgroundColor: hexAlpha(COLORS.red, 0.72),
          borderRadius: 99,
          borderSkipped: false,
          barPercentage: 0.7,
          yAxisID: "y1",
          order: 1,
        },
        {
          type: "bar",
          label: "跌停家数",
          data: s15.map(x => -(x.limit_down || 0)),
          backgroundColor: hexAlpha(COLORS.green, 0.72),
          borderRadius: 99,
          borderSkipped: false,
          barPercentage: 0.7,
          yAxisID: "y1",
          order: 1,
        },
      ],
    },
    options: {
      ...baseOpts,
      plugins: { ...baseOpts.plugins, glanceLastValue: false },
      animation: {
        duration: 800,
        easing: "easeOutQuart",
        delay: ctx => (ctx.type === "data" && n15 <= 20 ? ctx.dataIndex * 40 : 0),
      },
      scales: {
        ...baseOpts.scales,
        y: { ...baseOpts.scales.y, min: 0, max: 100 },
        y1: {
          ...baseOpts.scales.y,
          position: "right",
          grid: { drawOnChartArea: false },
        },
      },
    },
  });

  const s30 = cyc.series30 || [];
  const nav = s30.map(x => +((x.eq_nav - 1) * 100).toFixed(2));
  mkChart(document.getElementById("boardPeriodChart"), {
    type: "line",
    data: {
      labels: s30.map(x => x.day.slice(5)),
      datasets: [lineDS("池内等权", nav, lastNonNull(nav) < 0 ? COLORS.green : COLORS.red, true)],
    },
    options: baseOpts,
  });
}

// ---- 连板梯队 ----
function renderBoardLadder(lad) {
  if (!lad.available) return;
  const ladder = lad.ladder || {};
  const tiersEl = document.getElementById("board-tiers");
  if (!ladder.tiers || !ladder.tiers.length) {
    tiersEl.innerHTML = `<div class="empty">当日无 ≥2 连板（情绪低位）</div>`;
  } else {
    let h = "";
    for (const t of ladder.tiers) {
      const width = Math.min(100, t.streak * 18 + 20);
      h += `<div class="tier-row"><div class="tier-head" style="width:${width}%">
              <b>${t.streak}板</b> × ${t.count}</div>
            <div class="tier-stocks">` +
        t.stocks.map(s => {
          const ua = s.unlock_alert;
          const uaHtml = ua ? `<i class="unlock-warn" title="${ua.days_left}日后解禁 ${ua.ratio_pct}% ${ua.market_value_yi ?? "-"}亿">⚠解禁</i>` : "";
          return `<span class="tier-stock clickable" data-inst="${s.instrument}">${s.name || s.instrument}
             <i class="muted">${s.limit_type || ""}</i>${uaHtml}</span>`;
        }).join("") +
        `</div></div>`;
    }
    tiersEl.innerHTML = h;
    tiersEl.querySelectorAll(".tier-stock.clickable").forEach(el =>
      el.onclick = () => openStock(el.dataset.inst));
  }

  renderTable(document.getElementById("board-survival"),
    (ladder.survival || []).map(s => ({ ...s, rate: +(s.rate * 100).toFixed(1) })),
    [["streak", "板位"], ["rate", "晋级率%"], ["samples", "样本日"]],
    "样本不足");

  renderTable(document.getElementById("board-industry"), lad.industry_dist || [],
    [["industry", "行业"], ["count", "涨停家数"]], "当日无涨停");

  renderTable(document.getElementById("board-first"),
    (ladder.first_boards || []).map(r => ({ ...r, ret: r.ret != null ? +(r.ret * 100).toFixed(2) : null })),
    [["instrument", "代码"], ["name", "名称"], ["industry", "行业"],
     ["ret", "涨幅%"], ["limit_type", "类型"]],
    "当日无首板", "instrument");
}

// ---- 涨停复盘：封单散点图 + 预警 + 明细 ----
async function renderBoardLimit(lad) {
  const qs = boardDay ? `?day=${boardDay}` : "";
  const scatter = await getJSON("/api/board/limit-scatter" + qs);
  if (!scatter.available) {
    document.getElementById("board-limit-detail").innerHTML =
      `<div class="empty">${scatter.disclaimer || "无数据"}</div>`;
    return;
  }
  const modeEl = document.getElementById("board-scatter-mode");
  modeEl.classList.remove("hidden");
  modeEl.textContent = scatter.mode === "intraday"
    ? `盘中实时 ${scatter.generated || ""}`
    : `盘后口径 ${scatter.generated || ""} · 点「刷新行情」看封单`;

  const pts = scatter.scatter || [];
  const canvas = document.getElementById("boardScatterChart");
  const colorFor = p => (p.y >= 3 ? GLANCE.hero : (p.y === 2 ? GLANCE.ink : GLANCE.faint));
  const hi = pts.filter(p => (p.y || 0) >= 3).length;
  setChartCopy(canvas, {
    title: hi ? `${hi} 只 ≥3 板 · 封单气泡` : "封单强度 · 连板气泡",
    sub: scatter.mode === "intraday"
      ? "x=封单额/流通市值% · y=连板 · 面积=√成交额 · 橙=3板以上"
      : "盘后无盘口 · 面积=√成交额 · 点击看评分卡",
    src: scatter.mode === "intraday" ? "INTRADAY SEALED / FLOAT MV" : "POST-CLOSE · CLICK FOR CARD",
  });
  mkChart(canvas, {
    type: "bubble",
    data: {
      datasets: [{
        label: "涨停票",
        data: pts.map(p => ({
          x: p.x, y: p.y,
          r: Math.max(4, Math.min(22, Math.sqrt(Math.max(0, p.r_amt || 0)) * 6)),
          _p: p,
        })),
        backgroundColor: pts.map(p => hexAlpha(colorFor(p), 0.72)),
        borderColor: pts.map(p => colorFor(p)),
        borderWidth: 1.25,
      }],
    },
    options: {
      ...baseOpts,
      onClick: (evt, els) => {
        if (els.length) {
          const p = pts[els[0].index];
          openBoardCard(p.instrument);
        }
      },
      onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
      plugins: {
        ...baseOpts.plugins,
        legend: { display: false },
        glanceLastValue: false,
        tooltip: {
          ...baseOpts.plugins.tooltip,
          callbacks: {
            label: ctx => {
              const p = ctx.raw._p;
              const lines = [`${p.name || p.instrument}  ${p.y}板`];
              if (scatter.mode === "intraday") {
                lines.push(`封单 ${p.sealed_yi}亿 (${p.x}%)`, `成交额 ${p.r_amt}亿`,
                           `换手 ${p.turnover_pct}%  流通 ${p.float_mv_yi}亿`);
              } else {
                lines.push(`成交额 ${p.r_amt}亿`, `类型 ${p.limit_type || "-"}`);
              }
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          ...baseOpts.scales.x,
          title: {
            display: true,
            text: scatter.mode === "intraday" ? "封单额 / 流通市值 %" : "封单强度（盘后无盘口）",
            color: GLANCE.muted,
            font: { size: 11, weight: 600, family: GLANCE.font },
          },
        },
        y: {
          ...baseOpts.scales.y,
          min: 0,
          suggestedMax: 5,
          ticks: { ...baseOpts.scales.y.ticks, stepSize: 1 },
          title: { display: true, text: "连板数", color: GLANCE.muted, font: { size: 11, weight: 600, family: GLANCE.font } },
        },
      },
    },
  });

  // 涨停预警（仅盘中模式有）
  renderTable(document.getElementById("board-warn"),
    (scatter.warn_near_limit || []).map(r => ({ ...r })),
    [["instrument", "代码"], ["name", "名称"], ["industry", "行业"],
     ["chg_pct", "涨幅%"], ["vol_ratio", "量比"], ["turnover_pct", "换手%"]],
    scatter.mode === "intraday" ? "当前无逼近涨停（≥8%/15%）个股" : "盘后模式无预警，点「刷新行情」获取盘中预警",
    "instrument");

  // 明细表：盘中用实时 limit_ups，盘后用梯队
  if (scatter.mode === "intraday") {
    const intra = await getJSON("/api/board/intraday");
    renderTable(document.getElementById("board-limit-detail"),
      ((intra.limit_ups) || []).map(r => ({
        ...r,
        sealed_yi: r.sealed_amt != null ? +(r.sealed_amt / 1e8).toFixed(2) : r.sealed_yi,
        sealed_ratio_pct: r.sealed_ratio != null ? +(r.sealed_ratio * 100).toFixed(2) : null,
      })),
      [["instrument", "代码"], ["name", "名称"], ["industry", "行业"], ["streak", "连板"],
       ["chg_pct", "涨幅%"], ["sealed_yi", "封单(亿)"], ["sealed_ratio_pct", "封单/流通%"],
       ["vol_ratio", "量比"], ["turnover_pct", "换手%"]],
      "当前无涨停", "instrument");
  } else {
    const ladder = lad.ladder || {};
    const all = [];
    for (const t of ladder.tiers || []) for (const s of t.stocks) all.push(s);
    all.push(...(ladder.first_boards || []));
    all.sort((a, b) => b.streak - a.streak || (b.amount || 0) - (a.amount || 0));
    renderTable(document.getElementById("board-limit-detail"),
      all.map(r => ({ ...r, ret: r.ret != null ? +(r.ret * 100).toFixed(2) : null,
                     amount: r.amount ? +(r.amount / 1e8).toFixed(2) : null })),
      [["instrument", "代码"], ["name", "名称"], ["industry", "行业"], ["streak", "连板"],
       ["limit_type", "类型"], ["ret", "涨幅%"], ["amount", "成交额(亿)"]],
      "当日池内无涨停", "instrument");
  }
}

// ---- 评分卡抽屉 ----
async function openBoardCard(instrument) {
  const drawer = document.getElementById("board-drawer");
  const mask = document.getElementById("board-drawer-mask");
  const body = document.getElementById("board-drawer-body");
  const title = document.getElementById("board-drawer-title");
  drawer.classList.remove("hidden");
  mask.classList.remove("hidden");
  title.textContent = instrument;
  body.innerHTML = `<div class="empty">加载中…</div>`;
  mask.onclick = closeBoardCard;
  try {
    const c = await getJSON(`/api/board/stock/${instrument}`);
    title.innerHTML = `${c.name || instrument} <span class="muted">${instrument}</span> ${boardGradeBadge(c.grade)} <b style="color:#e6edf3">${fmt(c.score, 1)}分</b>`;
    const rt = c.realtime || {};
    const dims = Object.entries(c.dimensions || {});
    body.innerHTML = `
      ${rt.price ? `<div class="card-quote">
        <span class="card-price ${cls(rt.chg_pct)}">${fmt(rt.price)}</span>
        <span class="${cls(rt.chg_pct)}">${pct(rt.chg_pct)}</span>
        ${rt.at_limit_up ? `<span class="limit-badge">涨停${c.streak > 1 ? c.streak + "板" : ""}</span>` : ""}
        ${rt.sealed_amt ? `<span class="muted">封单 ${(rt.sealed_amt / 1e8).toFixed(2)}亿（${(rt.sealed_ratio * 100).toFixed(2)}%）</span>` : ""}
      </div>
      <div class="card-sub muted">量比 ${fmt(rt.vol_ratio)} · 换手 ${fmt(rt.turnover_pct)}% · 成交 ${fmt(rt.amount_yi, 1)}亿 · 流通 ${fmt(rt.float_mv_yi, 0)}亿</div>` : ""}
      <h4>五维评分</h4>
      <div class="dims">${dims.map(([k, v]) => `
        <div class="dim-row"><span class="dim-name">${k}</span>
          <div class="dim-bar"><div class="dim-fill" style="width:${v}%;background:${v >= 65 ? "#e5534b" : v >= 50 ? "#d29922" : "#3b82d6"}"></div></div>
          <span class="dim-val">${fmt(v, 0)}</span></div>`).join("")}
      </div>
      <h4>盘后横截面（${c.snapshot_day || "-"}）</h4>
      <div class="card-sub">${c.strong ?
        `盘后评分 <b>${fmt(c.strong.score, 1)}</b>（${c.strong.grade}） · 20日动量 ${pct((c.strong.momentum_20d || 0) * 100, 1)} · 相对强度 ${pct((c.strong.rs_20d || 0) * 100, 1)} · 量比 ${fmt(c.strong.vol_ratio)}` :
        "未进盘后强势榜 TOP50"}</div>
      <div class="card-sub muted">所属行业当日涨停 ${c.industry_limit_ups} 家 · 当前 ${c.streak || 0} 连板</div>
      ${(c.notes || []).length ? `<div class="muted small">${c.notes.join("；")}</div>` : ""}
      <div class="card-actions">
        <button class="mini-btn" onclick="closeBoardCard();openStock('${instrument}')">看 K 线</button>
      </div>
      <p class="muted small">${c.disclaimer || ""}</p>`;
  } catch (e) {
    body.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
  }
}
function closeBoardCard() {
  document.getElementById("board-drawer").classList.add("hidden");
  document.getElementById("board-drawer-mask").classList.add("hidden");
}
document.getElementById("board-drawer-close")?.addEventListener("click", closeBoardCard);

function viewingLatestBoardDay() {
  const sel = document.getElementById("board-day");
  return !boardDay || !sel?.dataset.latest || boardDay === sel.dataset.latest;
}

async function applyBoardIntraday() {
  const badgeEl = document.getElementById("board-intraday-badge");
  try {
    const intra = await getJSON("/api/board/intraday");
    if (!intra.available || !viewingLatestBoardDay()) {
      badgeEl.classList.add("hidden");
      return;
    }
    showIntradayBadge(intra);
    renderBoardPanoramaIntraday(intra);
  } catch (e) {
    badgeEl.classList.add("hidden");
  }
}

// ---- 刷新行情（盘中实时） ----
document.getElementById("board-refresh")?.addEventListener("click", async () => {
  try {
    const r = await fetch("/api/board/refresh", { method: "POST" });
    const j = await r.json();
    const jobEl = document.getElementById("board-job");
    if (j.busy) { jobEl.textContent = "刷新中…"; return; }
    jobEl.textContent = "正在拉取实时报价（约10秒）…";
    const timer = setInterval(async () => {
      const d = await getJSON("/api/board/intraday");
      if (d.job && d.job.status !== "running") {
        clearInterval(timer);
        jobEl.textContent = d.job.message || "完成";
        if (d.available && viewingLatestBoardDay()) {
          showIntradayBadge(d);
          const qs = boardDay ? `?day=${boardDay}` : "";
          renderBoardLimit(await getJSON("/api/board/ladder" + qs));
          renderBoardPanoramaIntraday(d);
        }
      }
    }, 2000);
  } catch (e) { console.error(e); }
});

function showIntradayBadge(d) {
  const el = document.getElementById("board-intraday-badge");
  el.classList.remove("hidden");
  const ts = d.generated || d.session_day || "";
  el.textContent = `实时行情 ${ts} · 报价 ${d.quotes_ok}/${d.quotes_total} 只 · 盘后快照 ${d.snapshot_day || "-"}`;
  const meta = document.getElementById("board-meta");
  if (meta && d.session_day && !meta.textContent.includes("盘中")) {
    meta.textContent += ` | 盘中 ${d.generated || d.session_day}`;
  }
}

// 盘中刷新后同步全景温度计/涨跌榜（不覆盖盘后历史曲线）
function renderBoardPanoramaIntraday(d) {
  const t = d.thermometer || {};
  renderThermoBlock(document.getElementById("board-thermo"), {
    temp: t.temperature ?? 50,
    label: "实时",
    t,
    live: true,
  });
  const cols = [["instrument", "代码"], ["name", "名称"], ["industry", "行业"],
                ["chg_pct", "涨幅%"], ["vol_ratio", "量比"], ["turnover_pct", "换手%"]];
  renderTable(document.getElementById("board-gainers"), d.gainers || [], cols, "无数据", "instrument");
  renderTable(document.getElementById("board-losers"), d.losers || [], cols, "无数据", "instrument");
}

// ---- 题材轮动 ----
function renderBoardThemes(thm, rot) {
  if (thm.available) {
    renderTable(document.getElementById("board-concepts"),
      (thm.concepts || []).map(t => ({
        ...t, pool_limit_list: (t.pool_limit_list || []).join(" "),
      })),
      [["name", "概念"], ["chg_pct", "涨幅%"], ["heat", "热度"],
       ["up_count", "上涨家数"], ["pool_limit_hits", "池内涨停"],
       ["pool_limit_list", "命中代码"]],
      "无题材数据");
    renderTable(document.getElementById("board-ind-list"), thm.industries || [],
      [["name", "行业"], ["chg_pct", "涨幅%"], ["up_count", "上涨"],
       ["down_count", "下跌"], ["turnover_pct", "换手%"]],
      "无行业数据");
  }
  // 轮动矩阵：热力着色的表格
  const rows = rot.rows || [];
  const el = document.getElementById("board-rotation");
  if (!rows.length) { el.innerHTML = `<div class="empty">无轮动数据</div>`; return; }
  const heat = v => {
    if (v == null) return "";
    const p = Math.max(-5, Math.min(5, v * 100));
    const a = Math.min(0.55, Math.abs(p) / 5 * 0.55);
    return p >= 0 ? `background:rgba(229,83,75,${a})` : `background:rgba(46,160,67,${a})`;
  };
  let h = `<table><thead><tr><th>行业</th><th>池内家数</th><th>近5日%</th><th>近10日%</th><th>近20日%</th><th>切换</th></tr></thead><tbody>`;
  for (const r of rows) {
    h += `<tr><td>${r.industry}</td><td>${r.n_stocks}</td>
      <td style="${heat(r.ret_5d)}">${r.ret_5d != null ? pct(r.ret_5d * 100, 1) : "-"}</td>
      <td style="${heat(r.ret_10d)}">${r.ret_10d != null ? pct(r.ret_10d * 100, 1) : "-"}</td>
      <td style="${heat(r.ret_20d)}">${r.ret_20d != null ? pct(r.ret_20d * 100, 1) : "-"}</td>
      <td>${r.shift || ""}</td></tr>`;
  }
  el.innerHTML = h + "</tbody></table>";
}

// ---- 快讯 ----
function renderBoardNews(nw) {
  const feedEl = document.getElementById("board-news-feed");
  const st = nw.stats || {};
  document.getElementById("board-news-stats").textContent = nw.available
    ? `（近${nw.lookback_days ?? 3}日 全局快讯 ${st.global_n ?? 0} 条 · 池内动态 ${st.pool_news_n ?? 0} 条 · 池内命中 ${st.global_hit_n ?? 0}）`
    : `（${nw.error || "无数据"}）`;
  if (!nw.available) { feedEl.innerHTML = `<div class="empty">${nw.error || "无数据"}</div>`; return; }

  window._boardNewsFeed = nw.global_feed || [];
  const renderFeed = filter => {
    let items = window._boardNewsFeed;
    if (filter === "hits") items = items.filter(x => (x.pool_hits || []).length);
    if (filter === "policy") items = items.filter(x => x.is_policy);
    if (!items.length) { feedEl.innerHTML = `<div class="empty">无匹配快讯</div>`; return; }
    feedEl.innerHTML = items.map(x => {
      const hits = (x.pool_hits || []).map(hh =>
        `<span class="news-hit clickable" data-inst="${hh.instrument}">${hh.name || hh.instrument}${hh.via === "code" ? "🔗" : ""}</span>`).join(" ");
      return `<div class="news-item ${x.pool_hits?.length ? "news-hit-row" : ""}">
        <span class="news-time muted">${(x.published || "").slice(5, 16)}</span>
        <span class="news-src">${x.is_policy ? "📌" : ""}${x.source || ""}</span>
        <span class="news-title">${x.title || ""}</span> ${hits}</div>`;
    }).join("");
    feedEl.querySelectorAll(".news-hit.clickable").forEach(el =>
      el.onclick = () => openBoardCard(el.dataset.inst));
  };
  renderFeed("all");
  document.querySelectorAll(".news-filter-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".news-filter-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      renderFeed(b.dataset.nf);
    };
  });

  renderTable(document.getElementById("board-pool-feed"),
    (nw.pool_feed || []).map(x => ({
      ...x,
      published: (x.published || "").slice(5, 16),
      kind: x.kind || "新闻",
    })),
    [["published", "时间"], ["name", "股票"], ["kind", "类型"], ["title", "标题"]],
    "近 3 日无池内个股动态", "instrument");
}

// ---- 强势资金 ----
function renderBoardStrong(stg, flow) {
  if (!stg.available) return;
  document.getElementById("board-strong-caliber").textContent =
    `口径：${stg.caliber || ""}；权重 动量${(stg.weights?.momentum ?? 0) * 100}%/量能${(stg.weights?.volume_ratio ?? 0) * 100}%/强度${(stg.weights?.relative_strength ?? 0) * 100}%`;

  // 资金流向榜
  if (flow && flow.available) {
    const fcols = [["instrument", "代码"], ["name", "名称"], ["main_net_yi", "主力净流入(亿)"],
                   ["main_net_pct", "净占比%"], ["chg_pct", "涨幅%"]];
    renderTable(document.getElementById("board-inflow"), flow.inflow || [], fcols, "无数据", "instrument");
    renderTable(document.getElementById("board-outflow"), flow.outflow || [], fcols, "无数据", "instrument");
  }
  const rows = (stg.stocks || []).map(s => ({
    ...s,
    grade_html: boardGradeBadge(s.grade),
    momentum_20d: +(s.momentum_20d * 100).toFixed(1),
    rs_20d: +(s.rs_20d * 100).toFixed(1),
    ret: +(s.ret * 100).toFixed(2),
  }));
  const container = document.getElementById("board-strong-list");
  if (!rows.length) { container.innerHTML = `<div class="empty">无数据</div>`; return; }
  let h = `<table><thead><tr><th>#</th><th>评级</th><th>代码</th><th>名称</th><th>行业</th>
    <th>评分</th><th>20日动量%</th><th>量比</th><th>相对强度%</th><th>当日%</th><th>连板</th></tr></thead><tbody>`;
  rows.forEach((s, i) => {
    const ua = s.unlock_alert;
    const uaHtml = ua ? ` <span class="unlock-warn" title="${ua.days_left}日后解禁 ${ua.ratio_pct}%">⚠</span>` : "";
    h += `<tr class="grade-${s.grade}">
      <td>${i + 1}</td><td>${s.grade_html}</td>
      <td class="clickable" data-inst="${s.instrument}">${s.instrument}</td>
      <td>${s.name || "-"}${uaHtml}</td><td>${s.industry || "-"}</td>
      <td><b>${fmt(s.score, 1)}</b></td>
      <td class="${cls(s.momentum_20d)}">${pct(s.momentum_20d, 1)}</td>
      <td>${fmt(s.vol_ratio)}</td>
      <td class="${cls(s.rs_20d)}">${pct(s.rs_20d, 1)}</td>
      <td class="${cls(s.ret)}">${pct(s.ret)}</td>
      <td>${s.streak || ""}${s.limit_up ? "🔥" : ""}</td></tr>`;
  });
  container.innerHTML = h + "</tbody></table>";
  container.querySelectorAll("td.clickable").forEach(td =>
    td.onclick = () => openStock(td.dataset.inst));
}

// 子页切换
document.querySelectorAll("#board-subtabs .subtab").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll("#board-subtabs .subtab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll("#board .board-sub").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("board-" + btn.dataset.sub).classList.add("active");
  };
});

// 生成快照按钮 + job 轮询
document.getElementById("board-run")?.addEventListener("click", async () => {
  try {
    const r = await fetch("/api/board/run", { method: "POST" });
    const j = await r.json();
    if (j.busy) return;
    const jobEl = document.getElementById("board-job");
    jobEl.textContent = "快照生成中…";
    const timer = setInterval(async () => {
      const dd = await getJSON("/api/board/overview");
      if (dd.job && dd.job.status !== "running") {
        clearInterval(timer);
        boardDaysLoaded = false;   // 刷新日期下拉
        jobEl.textContent = dd.job.message || "完成";
        loadBoard();
      } else {
        jobEl.textContent = "快照生成中… " + (dd.job?.message || "");
      }
    }, 2000);
  } catch (e) { console.error(e); }
});

async function loadTab(tab) {
  try {
    if (tab === "overview") await loadOverview();
    else if (tab === "daily-ops") await loadDailyOps();
    else if (tab === "tracking") await loadTracking();
    else if (tab === "board") await loadBoard();
    else if (tab === "sentiment") await loadSentiment(true);
    else if (tab === "swing") await loadSwing();
    else if (tab === "research") await loadResearch(true);
    else if (tab === "compare") await loadCompare();
    else if (tab === "alerts") await loadAlerts();
    else await loadAccount(tab);
  } catch (e) { console.error(e); }
}

document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById(t.dataset.tab).classList.add("active");
    loadTab(t.dataset.tab);
  };
});
document.getElementById("refresh")?.addEventListener("click", () => {
  if (!fullAccess) return;
  loadTab(document.querySelector(".tab.active").dataset.tab);
});

(async () => {
  await loadAccess();
  loadOverview();
})();
setInterval(() => {
  const tab = document.querySelector(".tab.active").dataset.tab;
  if (tab === "overview") loadOverview();
  else if (tab === "daily-ops") loadDailyOps();
  else if (tab === "board") applyBoardIntraday();
}, 60000);
