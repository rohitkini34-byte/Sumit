import { animate, springValue, styleEffect } from "motion";

// Sanad flip card: a real <button> with aria-pressed. Click, Enter and Space all flip it
// (native button behaviour). The flip and the desktop hover tilt are Motion springs.

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const canHover = window.matchMedia("(hover: hover) and (pointer: fine)");

export function initSanadCard() {
  const card = document.getElementById("sanad");
  const inner = card.querySelector(".sanad-inner");
  const MAX = 4;

  const tilt = { stiffness: 160, damping: 18 };
  const rotateX = springValue(0, tilt);
  const rotateY = springValue(0, tilt);
  styleEffect(card, { rotateX, rotateY });

  card.addEventListener("click", () => {
    const flipped = card.getAttribute("aria-pressed") !== "true";
    card.setAttribute("aria-pressed", String(flipped));
    rotateX.set(0);
    rotateY.set(0);
    if (reduceMotion.matches) { inner.style.transform = ""; return; } // CSS crossfade handles it
    animate(inner, { rotateY: flipped ? 180 : 0 }, { type: "spring", visualDuration: 0.7, bounce: 0.18 });
  });

  card.addEventListener("pointermove", (e) => {
    if (reduceMotion.matches || !canHover.matches || e.pointerType !== "mouse") return;
    if (card.getAttribute("aria-pressed") === "true") return;
    const r = card.getBoundingClientRect();
    rotateY.set(((e.clientX - r.left) / r.width - 0.5) * 2 * MAX);
    rotateX.set(-((e.clientY - r.top) / r.height - 0.5) * 2 * MAX);
  });

  card.addEventListener("pointerleave", () => { rotateX.set(0); rotateY.set(0); });
}
