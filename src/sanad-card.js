// Sanad flip card: a real <button> with aria-pressed. Click, Enter and Space all flip it
// (native button behaviour). On desktop, the unflipped card also tilts slightly with the cursor.

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const canHover = window.matchMedia("(hover: hover) and (pointer: fine)");

export function initSanadCard() {
  const card = document.getElementById("sanad");
  const MAX = 4;
  let raf = 0;

  card.addEventListener("click", () => {
    const flipped = card.getAttribute("aria-pressed") === "true";
    card.setAttribute("aria-pressed", String(!flipped));
    card.style.transform = "";
  });

  card.addEventListener("pointermove", (e) => {
    if (reduceMotion.matches || !canHover.matches || e.pointerType !== "mouse") return;
    if (card.getAttribute("aria-pressed") === "true") return;
    const r = card.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width - 0.5;
    const py = (e.clientY - r.top) / r.height - 0.5;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      card.style.transition = "transform .15s ease-out";
      card.style.transform = `rotateY(${(px * 2 * MAX).toFixed(2)}deg) rotateX(${(-py * 2 * MAX).toFixed(2)}deg)`;
    });
  });

  card.addEventListener("pointerleave", () => {
    cancelAnimationFrame(raf);
    card.style.transition = "transform .5s cubic-bezier(.2,.7,.2,1)";
    card.style.transform = "";
  });
}
