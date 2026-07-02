const ACCOUNTS = ["research_sim_100k", "live_manual_10k"];
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

const COLORS = { research: "#4f9cf9", live: "#d29922", bench: "#8b98a9", green: "#3fb950", red: "#f85149" };
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
  if (series[0].dates.length) ds.push(lineDS("研究模拟线", alignBy(series[0].dates, series[0].series.cum_ret), COLORS.research));
  if (series[1].dates.length) ds.push(lineDS("实盘线", alignBy(series[1].dates, series[1].series.cum_ret), COLORS.live));
  if (series[0].dates.length) ds.push(lineDS("基准", alignBy(series[0].dates, series[0].series.cum_bench), COLORS.bench));
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
  for (const a of ACCOUNTS) {
    const s = sum[a];
    if (!s.days) { box.appendChild(card(a, "未初始化", "")); continue; }
    box.appendChild(card(a, pct((s.cum_ret || 0) * 100),
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

  const al = await getJSON("/api/alerts");
  if (!al.alerts.length) { document.getElementById("alert-list").innerHTML = '<div class="empty">暂无告警</div>'; return; }
  let a = "<table><tbody>";
  for (const x of al.alerts) {
    const lvl = (x.raw.match(/\b(CRIT|WARN|INFO)\b/) || [])[0] || "";
    a += `<tr><td style="text-align:left" class="lvl-${lvl}">${x.raw}</td></tr>`;
  }
  document.getElementById("alert-list").innerHTML = a + "</tbody></table>";
}

// ----------------- 路由 -----------------
async function loadTab(tab) {
  try {
    if (tab === "overview") await loadOverview();
    else if (tab === "daily-ops") await loadDailyOps();
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
document.getElementById("refresh").onclick = () => {
  loadTab(document.querySelector(".tab.active").dataset.tab);
};
loadOverview();
setInterval(() => {
  const tab = document.querySelector(".tab.active").dataset.tab;
  if (tab === "overview") loadOverview();
  else if (tab === "daily-ops") loadDailyOps();
}, 60000);
