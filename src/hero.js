import { animate, stagger, springValue, styleEffect } from "motion";
import { FACE, PORTRAIT } from "./config.js";

const mq = (q) => window.matchMedia(q);
const reduceMotion = mq("(prefers-reduced-motion: reduce)");
const canHover = mq("(hover: hover) and (pointer: fine)");
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const EASE_OUT = [0.2, 0.7, 0.2, 1];

// One orchestrated intro after the gate opens. Motion drives it: tween for the line draw,
// springs for everything that moves, so it settles naturally rather than on a fixed curve.
export function playIntro() {
  const hero = document.querySelector(".hero");
  if (reduceMotion.matches) { hero.dataset.intro = "done"; return; }
  const q = (sel) => hero.querySelectorAll(sel);

  animate(q(".arch-line path"), { strokeDasharray: [1, 1], strokeDashoffset: [1, 0] }, { duration: 0.6, ease: EASE_OUT });
  animate(q(".arch-photo"), { opacity: [0, 1], scale: [1.04, 1] }, { delay: 0.2, duration: 0.7, ease: EASE_OUT });
  animate(q(".intro-fade"), { opacity: [0, 1] }, { delay: 0.25, duration: 0.5 });
  // Split by word, never by letter (Devanagari conjuncts).
  animate(q(".hero-name .word"), { opacity: [0, 1], y: [24, 0] },
    { delay: stagger(0.08, { startDelay: 0.3 }), type: "spring", visualDuration: 0.6, bounce: 0 });
  animate(q(".intro-late"), { opacity: [0, 1], y: [8, 0] }, { delay: 0.7, duration: 0.4, ease: EASE_OUT });
  // z keeps the seal's translateZ(60px) layer depth while Motion owns its transform.
  const seal = animate(q(".seal"), { opacity: [0, 1], scale: [0.8, 1], z: [60, 60] },
    { delay: 0.9, type: "spring", visualDuration: 0.5, bounce: 0.35 });

  // Hand back to a static state so a later language switch does not replay anything.
  seal.then(() => { hero.dataset.intro = "done"; });
}

/* ---------- Tilt + lamp light + sheen (Motion springs) ---------- */
function initTilt() {
  const hero = document.querySelector(".hero");
  const arch = document.getElementById("arch");
  const sheen = arch.querySelector(".arch-sheen");
  const MAX = 8;
  const spring = { stiffness: 120, damping: 20, mass: 0.8 };

  const rotateX = springValue(0, spring);
  const rotateY = springValue(0, spring);
  const mx = springValue(70, spring);
  const my = springValue(40, spring);
  styleEffect(arch, { rotateX, rotateY });

  // The lamp and the glint on the glass follow the same springs through CSS variables.
  const paint = () => {
    hero.style.setProperty("--mx", `${mx.get().toFixed(2)}%`);
    hero.style.setProperty("--my", `${my.get().toFixed(2)}%`);
    sheen.style.setProperty("--sx", `${(rotateY.get() * 2.2).toFixed(2)}%`);
    sheen.style.setProperty("--sy", `${(-rotateX.get() * 1.6).toFixed(2)}%`);
  };
  [rotateX, rotateY, mx, my].forEach((v) => v.on("change", paint));

  hero.addEventListener("pointermove", (e) => {
    if (reduceMotion.matches || !canHover.matches || e.pointerType !== "mouse") return;
    const r = hero.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    rotateY.set(clamp((x - 0.5) * 2, -1, 1) * MAX);
    rotateX.set(clamp((0.5 - y) * 2, -1, 1) * MAX);
    mx.set(x * 100);
    my.set(y * 100);
  });
  hero.addEventListener("pointerleave", () => {
    rotateX.set(0); rotateY.set(0); mx.set(70); my.set(40);
  });
}

/* ---------- Seal ring turns with the page, not on its own ---------- */
function initSeal() {
  const ring = document.querySelector(".seal-text");
  const rot = springValue(0, { stiffness: 90, damping: 22 });
  rot.on("change", (v) => ring.style.setProperty("--seal-rot", `${v.toFixed(2)}deg`));
  const update = () => rot.set(reduceMotion.matches ? 0 : window.scrollY * 0.15);
  window.addEventListener("scroll", update, { passive: true });
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
