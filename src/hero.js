import { FACE, PORTRAIT } from "./config.js";

const mq = (q) => window.matchMedia(q);
const reduceMotion = mq("(prefers-reduced-motion: reduce)");
const canHover = mq("(hover: hover) and (pointer: fine)");
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const lerp = (a, b, f) => a + (b - a) * f;

export function playIntro() {
  const hero = document.querySelector(".hero");
  if (reduceMotion.matches) { hero.dataset.intro = "done"; return; }
  hero.dataset.intro = "play";
  // Hand back to a static state so a later language switch does not replay it.
  window.setTimeout(() => { hero.dataset.intro = "done"; }, 2000);
}

/* ---------- Tilt + lamp light + sheen ---------- */
function initTilt() {
  const hero = document.querySelector(".hero");
  const arch = document.getElementById("arch");
  const sheen = arch.querySelector(".arch-sheen");
  const MAX = 8;
  const target = { rx: 0, ry: 0, mx: 70, my: 40 };
  const cur = { ...target };
  let raf = 0;

  const tick = () => {
    cur.rx = lerp(cur.rx, target.rx, 0.08);
    cur.ry = lerp(cur.ry, target.ry, 0.08);
    cur.mx = lerp(cur.mx, target.mx, 0.08);
    cur.my = lerp(cur.my, target.my, 0.08);
    arch.style.transform = `rotateX(${cur.rx.toFixed(3)}deg) rotateY(${cur.ry.toFixed(3)}deg)`;
    hero.style.setProperty("--mx", `${cur.mx.toFixed(2)}%`);
    hero.style.setProperty("--my", `${cur.my.toFixed(2)}%`);
    sheen.style.setProperty("--sx", `${(cur.ry * 2.2).toFixed(2)}%`);
    sheen.style.setProperty("--sy", `${(-cur.rx * 1.6).toFixed(2)}%`);
    const settled = Math.abs(cur.rx - target.rx) < 0.01 && Math.abs(cur.ry - target.ry) < 0.01 &&
      Math.abs(cur.mx - target.mx) < 0.05 && Math.abs(cur.my - target.my) < 0.05;
    raf = settled ? 0 : requestAnimationFrame(tick);
  };
  const kick = () => { if (!raf) raf = requestAnimationFrame(tick); };

  hero.addEventListener("pointermove", (e) => {
    if (reduceMotion.matches || !canHover.matches || e.pointerType !== "mouse") return;
    const r = hero.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    target.ry = clamp((x - 0.5) * 2, -1, 1) * MAX;
    target.rx = clamp((0.5 - y) * 2, -1, 1) * MAX;
    target.mx = x * 100;
    target.my = y * 100;
    kick();
  });
  hero.addEventListener("pointerleave", () => {
    target.rx = 0; target.ry = 0; target.mx = 70; target.my = 40;
    kick();
  });
}

/* ---------- Seal turns with the page, not on its own ---------- */
function initSeal() {
  const seal = document.querySelector(".seal-text");
  let queued = false;
  const update = () => {
    queued = false;
    const deg = reduceMotion.matches ? 0 : window.scrollY * 0.15;
    seal.style.setProperty("--seal-rot", `${deg.toFixed(2)}deg`);
  };
  window.addEventListener("scroll", () => {
    if (!queued) { queued = true; requestAnimationFrame(update); }
  }, { passive: true });
  update();
}

/* ---------- Face follows cursor (activates when frames exist in config.js) ---------- */
function preload(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(src);
    img.onerror = reject;
    img.src = src;
  });
}

async function initFace() {
  const img = document.getElementById("portrait");
  const spriteEl = document.getElementById("portrait-sprite");
  img.src = PORTRAIT;

  const { cols, rows, frames, sprite } = FACE;
  const useFrames = Array.isArray(frames) && frames.length === cols * rows && cols > 0 && rows > 0;
  if (!useFrames && !sprite) return; // static portrait, tilt only

  try {
    await Promise.all(useFrames ? frames.map(preload) : [preload(sprite)]);
  } catch (e) {
    return; // keep the static portrait if anything fails to load
  }

  const centre = { col: Math.floor(cols / 2), row: Math.floor(rows / 2) };
  let shown = { col: -1, row: -1 };
  let pending = null;

  const show = ({ col, row }) => {
    if (col === shown.col && row === shown.row) return;
    shown = { col, row };
    if (useFrames) {
      img.src = frames[row * cols + col];
    } else {
      spriteEl.style.backgroundPosition =
        `${cols > 1 ? (col / (cols - 1)) * 100 : 0}% ${rows > 1 ? (row / (rows - 1)) * 100 : 0}%`;
    }
  };

  if (!useFrames) {
    spriteEl.style.backgroundImage = `url("${sprite}")`;
    spriteEl.style.backgroundSize = `${cols * 100}% ${rows * 100}%`;
    spriteEl.hidden = false;
    img.style.visibility = "hidden";
  }
  show(centre);

  const cellFor = (x, y) => {
    const r = img.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dx = clamp((x - cx) / (window.innerWidth / 2), -1, 1);
    const dy = clamp((y - cy) / (window.innerHeight / 2), -1, 1);
    return {
      col: Math.round(((dx + 1) / 2) * (cols - 1)),
      row: Math.round(((dy + 1) / 2) * (rows - 1)),
    };
  };

  const onMove = (e) => {
    if (reduceMotion.matches) return;
    const touch = e.pointerType !== "mouse";
    if (touch && e.type === "pointermove") return; // touch: follow the last tap only
    const wasIdle = pending === null;
    pending = cellFor(e.clientX, e.clientY);
    if (wasIdle) requestAnimationFrame(() => { show(pending); pending = null; });
  };

  window.addEventListener("pointermove", onMove, { passive: true });
  window.addEventListener("pointerdown", onMove, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => show(centre));
  window.addEventListener("blur", () => show(centre));
  reduceMotion.addEventListener?.("change", () => { if (reduceMotion.matches) show(centre); });
}

export function initHero() {
  initTilt();
  initSeal();
  initFace();
}
