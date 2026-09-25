import { animate } from "motion";

// Disclaimer gate: a real modal (focus trapped, background inert, Esc does not close it),
// opened on first visit per session and re-openable from the footer.

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const FOCUSABLE = 'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

export function initGate({ onEnter }) {
  const root = document.documentElement;
  const gate = document.getElementById("gate");
  const app = document.getElementById("app");
  const agree = document.getElementById("gate-agree");
  const reopen = document.getElementById("reopen-gate");
  let returnFocus = null;
  let firstVisit = root.classList.contains("gate-open");

  function trap(e) {
    if (e.key === "Escape") { e.preventDefault(); return; }
    if (e.key !== "Tab") return;
    const items = [...gate.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!gate.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  function open({ reopened = false } = {}) {
    returnFocus = reopened ? document.activeElement : null;
    gate.hidden = false;
    gate.classList.remove("is-leaving", "is-opening");
    gate.classList.toggle("is-reopened", reopened);
    root.classList.add("gate-open");
    app.inert = true;
    document.addEventListener("keydown", trap);
    requestAnimationFrame(() => agree.focus({ preventScroll: true }));
  }

  function close() {
    try { sessionStorage.setItem("disclaimer", "accepted"); } catch (e) { /* storage unavailable */ }
    document.removeEventListener("keydown", trap);
    const finish = () => {
      gate.hidden = true;
      gate.querySelectorAll(".gate-door, .gate-content").forEach((el) => { el.style.transform = ""; el.style.opacity = ""; });
      gate.classList.remove("is-leaving", "is-opening", "is-reopened");
      root.classList.remove("gate-open");
      root.classList.add("gate-accepted");
      app.inert = false;
      if (returnFocus) returnFocus.focus({ preventScroll: true });
      if (firstVisit) { firstVisit = false; onEnter?.(); }
    };

    if (reduceMotion()) {
      gate.classList.add("is-opening");
      window.setTimeout(finish, 200);
      return;
    }
    if (gate.classList.contains("is-reopened")) {
      gate.classList.add("is-leaving");
      window.setTimeout(finish, 250);
      return;
    }
    // The court doors open: content fades, then the panels slide apart (Motion).
    const DOOR_EASE = [0.77, 0, 0.18, 1];
    animate(gate.querySelector(".gate-content"), { opacity: 0, y: -8 }, { duration: 0.25, ease: "easeIn" })
      .then(() => {
        const doors = Promise.all([
          animate(gate.querySelector(".gate-door--left"), { x: "-100%" }, { duration: 0.9, ease: DOOR_EASE }),
          animate(gate.querySelector(".gate-door--right"), { x: "100%" }, { duration: 0.9, ease: DOOR_EASE }),
        ]);
        // Start the hero intro as the doors part, so it is revealed mid-motion.
        window.setTimeout(() => { if (firstVisit) { firstVisit = false; onEnter?.(); } }, 250);
        return doors;
      })
      .then(finish);
  }

  agree.addEventListener("click", close);
  reopen.addEventListener("click", () => open({ reopened: true }));

  if (firstVisit) open();
  else { gate.hidden = true; onEnter?.({ immediate: true }); firstVisit = false; }
}
