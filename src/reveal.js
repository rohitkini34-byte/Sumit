// Timeline + SGPI chart: both draw once when scrolled into view.
import { t, onLanguageChange } from "./i18n.js";

const SGPI = [6.40, 7.80, 7.80, 8.60, 7.60, 7.60];
const ROMAN = ["I", "II", "III", "IV", "V", "VI"];
const SVGNS = "http://www.w3.org/2000/svg";

// The chart is drawn at its real pixel width so labels stay at a readable 14px+ on phones.
let W = 600;
const H = 260;
const PAD = { l: 36, r: 26, t: 34, b: 44 };
const Y_MIN = 6, Y_MAX = 9;
const x = (i) => PAD.l + (i * (W - PAD.l - PAD.r)) / (SGPI.length - 1);
const y = (v) => PAD.t + ((Y_MAX - v) / (Y_MAX - Y_MIN)) * (H - PAD.t - PAD.b);

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function buildChart() {
  const svg = document.querySelector(".chart-svg");
  const tbody = document.getElementById("chart-table");
  W = Math.max(300, Math.round(svg.getBoundingClientRect().width) || 600);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  tbody.replaceChildren();
  const sem = t("edu.sem");
  const narrow = W < 460;

  const grid = el("g", { class: "chart-grid", "aria-hidden": "true" }, svg);
  const axis = el("g", { class: "chart-axis", "aria-hidden": "true" }, svg);
  for (let v = Y_MIN; v <= Y_MAX; v++) {
    el("line", { x1: PAD.l, x2: W - PAD.r + 6, y1: y(v), y2: y(v) }, grid);
    el("text", { x: PAD.l - 14, y: y(v) + 5, "text-anchor": "end" }, axis).textContent = v;
  }

  const pts = SGPI.map((v, i) => [x(i), y(v)]);
  const line = pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const base = y(Y_MIN);
  el("path", { class: "chart-area", d: `${line} L${x(SGPI.length - 1)},${base} L${x(0)},${base} Z`, "aria-hidden": "true" }, svg);
  el("path", { class: "chart-line", d: line, pathLength: 1, "aria-hidden": "true" }, svg);

  const labels = el("g", { class: "chart-x", "aria-hidden": "true" }, svg);
  SGPI.forEach((v, i) => {
    const g = el("g", { class: "chart-pt", style: `--i:${i}`, "aria-hidden": "true" }, svg);
    el("circle", { class: "chart-dot", cx: pts[i][0], cy: pts[i][1], r: 5 }, g);
    el("text", { class: "chart-val", x: pts[i][0], y: pts[i][1] - 14 }, g).textContent = v.toFixed(2);

    // On narrow screens the axis shows just the numeral; the caption names the unit.
    el("text", { x: pts[i][0], y: H - 12, "text-anchor": "middle" }, labels).textContent =
      narrow ? ROMAN[i] : `${sem} ${ROMAN[i]}`;

    const tr = document.createElement("tr");
    const th = document.createElement("td");
    th.textContent = `${sem} ${ROMAN[i]}`;
    const td = document.createElement("td");
    td.textContent = v.toFixed(2);
    tr.append(th, td);
    tbody.append(tr);
  });
}

function onceVisible(node, cb, threshold = 0.35) {
  if (!("IntersectionObserver" in window)) { cb(); return; }
  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) { cb(); io.disconnect(); }
  }, { threshold });
  io.observe(node);
}

export function initReveal() {
  buildChart();
  onLanguageChange(buildChart);
  let lastW = W;
  let timer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const w = Math.round(document.querySelector(".chart-svg").getBoundingClientRect().width);
      if (Math.abs(w - lastW) > 4) { buildChart(); lastW = W; }
    }, 150);
  });

  const chart = document.getElementById("chart");
  const timeline = document.getElementById("timeline");
  chart.classList.add("will-draw");
  onceVisible(timeline, () => timeline.classList.add("is-drawn"), 0.3);
  onceVisible(chart, () => chart.classList.add("is-drawn"), 0.4);
}
