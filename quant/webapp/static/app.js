const ACCOUNTS = [
  "research_sim_100k",
  "live_manual_10k",
  "shadow_ctrl_sim",
  "shadow_ta_sim",
];
// 双线对比页仍只比研究 vs 实盘（影子 A/B 用 review_ta_overlay）
const COMPARE_ACCOUNTS = ["research_sim_100k", "live_manual_10k"];
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

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

function mkChart(canvas, cfg) {
  const key = canvas.id || canvas.dataset.k;
  if (charts[key]) charts[key].destroy();
  charts[key] = new Chart(canvas, cfg);
}

const COLORS = {
  research: "#4f9cf9", live: "#d29922", bench: "#8b98a9",
  green: "#3fb950", red: "#f85149",
  shadow_ctrl: "#a371f7", shadow_ta: "#3fb950",
};
const ACCOUNT_COLORS = {
  research_sim_100k: COLORS.research,
  live_manual_10k: COLORS.live,
  shadow_ctrl_sim: COLORS.shadow_ctrl,
  shadow_ta_sim: COLORS.shadow_ta,
};
const lineDS = (label, data, color, fill = false) => ({
  label, data, borderColor: color, backgroundColor: color + "33",
  borderWidth: 2, pointRadius: 0, tension: .2, fill,
});
const baseOpts = {
  responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
  plugins: { legend: { labels: { color: "#8b98a9", boxWidth: 12 } } },
  scales: { x: { ticks: { color: "#8b98a9", maxTicksLimit: 8 }, grid: { color: "#2a344133" } },
            y: { ticks: { color: "#8b98a9" }, grid: { color: "#2a344133" } } },
};

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
  const writeBtns = ["sent-run", "sent-analyze-one", "sent-rerun-one", "swing-run"];
  writeBtns.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", demo);
  });
  // 刷新按钮演示模式也可用（只读）
  ["refresh", "sent-refresh", "swing-refresh"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("hidden");
  });
  const codeInput = document.getElementById("sent-code");
  if (codeInput) {
    codeInput.disabled = demo;
    codeInput.placeholder = demo ? "演示模式：仅可浏览已有报告" : "代码 600519 / SH600519";
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
    ds.push(lineDS("基准", alignBy(series[0].dates, series[0].series.cum_bench), COLORS.bench));
  }
  mkChart(document.getElementById("ovChart"), { type: "line", data: { labels, datasets: ds }, options: { ...baseOpts, spanGaps: true } });
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
    cc.appendChild(card("最新净值", fmt(s.nav[n]) + " 元", daily.dates[n]));
    cc.appendChild(card("累计收益", pct(s.cum_ret[n]), `超额 ${pct(s.cum_excess[n])}`, cls(s.cum_ret[n])));
    cc.appendChild(card("当日收益", pct(s.daily_ret[n]), "", cls(s.daily_ret[n])));
    cc.appendChild(card("持仓 / 现金", `${s.n_pos[n]} 只`, `现金 ${fmt(s.cash[n])} (${fmt(s.cash[n] / s.nav[n] * 100, 1)}%)`));
    cc.appendChild(card("交易天数", daily.dates.length, `平均换手 ${fmt(avg(s.turnover), 1)}%`));
  }

  if (daily.dates.length) {
    const L = daily.dates, s = daily.series;
    mkChartC(panel, ".navChart", "nav_" + account, { type: "line", data: { labels: L, datasets: [lineDS("净值", s.nav, COLORS.research, true)] }, options: baseOpts });
    mkChartC(panel, ".retChart", "ret_" + account, { type: "line", data: { labels: L, datasets: [lineDS("累计收益", s.cum_ret, COLORS.green), lineDS("基准", s.cum_bench, COLORS.bench), lineDS("超额", s.cum_excess, COLORS.live)] }, options: baseOpts });
    mkChartC(panel, ".posChart", "pos_" + account, { type: "line", data: { labels: L, datasets: [lineDS("持仓数", s.n_pos, COLORS.research), lineDS("换手%", s.turnover, COLORS.live)] }, options: baseOpts });
    mkChartC(panel, ".cashChart", "cash_" + account, { type: "line", data: { labels: L, datasets: [lineDS("现金", s.cash, COLORS.bench, true), lineDS("持仓市值", s.position_value, COLORS.research, true)] }, options: { ...baseOpts, scales: { ...baseOpts.scales, y: { ...baseOpts.scales.y, stacked: true } } } });
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
const POS_LINE_COLORS = ["#4f9cf9", "#d29922", "#3fb950", "#a371f7", "#f85149",
  "#56d4dd", "#e3b341", "#db61a2", "#8b98a9", "#2ea043", "#f0883e", "#6cb6ff"];

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
  mkChart(canvas, { type: "line", data: { labels: L, datasets: ds },
    options: { ...baseOpts, spanGaps: true } });

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
  const RED = "#f85149", GREEN = "#3fb950";
  const colors = up.map(u => u ? RED : GREEN);
  // 蜡烛：影线([low,high]) + 实体([open,close]) 两个浮动柱叠加（grouped:false 同位重叠）
  mkChart(document.getElementById("smK"), {
    type: "bar",
    data: {
      labels: L,
      datasets: [
        { label: "影线", data: q.klines.map(k => [k.low, k.high]), backgroundColor: colors,
          barThickness: 2, grouped: false, order: 2 },
        { label: "实体", data: q.klines.map(k => [k.open, k.close]), backgroundColor: colors,
          barThickness: 7, grouped: false, order: 1 },
      ],
    },
    options: { ...baseOpts, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: (c) => {
        const k = q.klines[c.dataIndex];
        return `开${k.open} 高${k.high} 低${k.low} 收${k.close}`; } } } },
      scales: { x: { ...baseOpts.scales.x, stacked: false }, y: { ...baseOpts.scales.y, beginAtZero: false } } },
  });
  mkChart(document.getElementById("smVol"), {
    type: "bar",
    data: { labels: L, datasets: [{ label: "成交量", data: q.klines.map(k => k.volume), backgroundColor: colors }] },
    options: { ...baseOpts, plugins: { legend: { display: false } } },
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

// ----------------- 对比 -----------------
async function loadCompare() {
  const c = await getJSON("/api/compare");
  const sum = c.summary;
  const box = document.getElementById("cmp-summary");
  box.className = "cards"; box.innerHTML = "";
  for (const a of COMPARE_ACCOUNTS) {
    const s = sum[a];
    const label = ACCOUNT_SHORT[a] || a;
    if (!s || !s.days) { box.appendChild(card(label, "未初始化", "")); continue; }
    box.appendChild(card(label, pct((s.cum_ret || 0) * 100),
      `净值 ${fmt(s.nav)} · 费用 ${fmt(s.fee)} (${pct((s.fee_ratio || 0) * 100)}) · 现金 ${pct((s.cash_ratio || 0) * 100)}`,
      s.cum_ret >= 0 ? "pos" : "neg"));
  }
  const L = c.common_days.map(d => d.date);
  mkChart(document.getElementById("cmpChart"), {
    type: "bar",
    data: { labels: L, datasets: [{ label: "日收益差(实盘-研究)%", data: c.common_days.map(d => d.gap), backgroundColor: c.common_days.map(d => d.gap >= 0 ? "#3fb95099" : "#f8514999") }] },
    options: baseOpts,
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
    b.innerHTML = `<div><span class="code">${it.instrument}</span>`
      + `<span class="name">${it.name || ""}</span></div>`
      + `<div class="snip">${it.headline || "—"}</div>`
      + `<span class="pill ${it.sentiment || ""}">${sentLabel(it.sentiment)} `
      + `${it.score == null ? "" : Number(it.score).toFixed(2)}</span>`;
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
    el.classList.toggle("active", el.querySelector(".code")?.textContent === instrument);
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
  document.getElementById("sent-name").textContent =
    `${r.name ? r.name + " · " : ""}${instrument}`;
  document.getElementById("sent-headline").textContent = r.headline || "—";
  const scoreEl = document.getElementById("sent-score");
  scoreEl.textContent = r.score == null ? "—" : Number(r.score).toFixed(2);
  scoreEl.className = "value " + sentPolarity(r.sentiment);
  document.getElementById("sent-stance").textContent =
    `${sentLabel(r.sentiment)} · ${r.stance || "—"}`;

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
  mkChart(document.getElementById("sentHistChart"), {
    type: "line",
    data: {
      labels: hChrono.map(x => x.date),
      datasets: [lineDS("情绪分", hChrono.map(x => x.score), COLORS.live)],
    },
    options: { ...baseOpts, plugins: { legend: { display: false } },
      scales: { ...baseOpts.scales, y: { ...baseOpts.scales.y, min: -1, max: 1 } } },
  });

  const meta = r.meta || {};
  document.getElementById("sent-meta").textContent =
    `模型 ${meta.model || "—"}（${meta.endpoint || "—"}） · 条目 ${r.news_count ?? "—"}`
    + `（公告 ${r.announcement_count ?? "—"} / 政策 ${r.policy_count ?? "—"}）`
    + ` · 向量记忆 ${data.vector?.count ?? r.vector_count ?? "—"} 条`
    + ` · 报告日 ${r.date || "—"}`;

  // 近 90 天价格（约 65 个交易日≈三个月；取 70 根日 K 略留余量）
  try {
    const q = await getJSON(`/api/quote/${instrument}?klt=101&n=70&_=${Date.now()}`);
    const canvas = document.getElementById("sentPriceChart");
    if (!q.ok || !q.klines?.length) {
      if (charts.sentPriceChart) { charts.sentPriceChart.destroy(); delete charts.sentPriceChart; }
    } else {
      const L = q.klines.map(k => k.date);
      const closes = q.klines.map(k => k.close);
      const base = closes.find(c => c != null);
      const cum = closes.map(c => (base ? +((c / base - 1) * 100).toFixed(3) : null));
      mkChart(canvas, {
        type: "line",
        data: {
          labels: L,
          datasets: [
            { ...lineDS("收盘价", closes, COLORS.research), yAxisID: "y" },
            { ...lineDS("窗口累计%", cum, COLORS.live), yAxisID: "y1" },
          ],
        },
        options: {
          ...baseOpts,
          scales: {
            x: baseOpts.scales.x,
            y: { ...baseOpts.scales.y, position: "left", title: { display: true, text: "价格", color: "#8b98a9" } },
            y1: {
              position: "right", ticks: { color: "#8b98a9" },
              grid: { drawOnChartArea: false },
              title: { display: true, text: "累计%", color: "#8b98a9" },
            },
          },
        },
      });
    }
  } catch (e) { console.error(e); }
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
  if (bar) bar.style.width = pct + "%";
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
  if (bar) bar.style.width = pct + "%";
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
async function loadTab(tab) {
  try {
    if (tab === "overview") await loadOverview();
    else if (tab === "daily-ops") await loadDailyOps();
    else if (tab === "sentiment") await loadSentiment(true);
    else if (tab === "swing") await loadSwing();
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
}, 60000);
