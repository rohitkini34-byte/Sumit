import { CONTACT, COUNCIL_LOGO } from "./config.js";
import { initI18n } from "./i18n.js";
import { initGate } from "./gate.js";
import { initHero, playIntro } from "./hero.js";
import { initReveal } from "./reveal.js";
import { initSanadCard } from "./sanad-card.js";
import { initTheme } from "./theme.js";

function applyContact() {
  const set = (sel, fn) => document.querySelectorAll(`[data-contact="${sel}"]`).forEach(fn);
  set("email", (el) => { el.textContent = CONTACT.email; });
  set("phone", (el) => { el.textContent = CONTACT.phoneDisplay; });
  set("email-link", (el) => { el.href = `mailto:${CONTACT.email}`; });
  set("tel-link", (el) => { el.href = `tel:${CONTACT.phoneTel}`; });
  set("wa-link", (el) => { el.href = `https://wa.me/${CONTACT.whatsapp.replace(/\D/g, "")}`; });
}

// Use the Bar Council emblem wherever it is wanted, but only once it has actually loaded.
function applyCouncilLogo() {
  const probe = new Image();
  probe.onload = () => {
    document.querySelectorAll(".council-logo").forEach((img) => {
      img.src = COUNCIL_LOGO;
      img.hidden = false;
    });
    document.documentElement.classList.add("has-council-logo");
  };
  probe.src = COUNCIL_LOGO;
}

function initNav() {
  const header = document.getElementById("site-header");
  const menuBtn = header.querySelector(".menu-btn");
  const sheet = document.getElementById("menu-sheet");
  const root = document.documentElement;

  // Navy background once the page scrolls past 40px
  let queued = false;
  const onScroll = () => {
    queued = false;
    header.classList.toggle("is-scrolled", window.scrollY > 40);
  };
  window.addEventListener("scroll", () => {
    if (!queued) { queued = true; requestAnimationFrame(onScroll); }
  }, { passive: true });
  onScroll();

  // Mobile menu sheet
  const setMenu = (open) => {
    menuBtn.setAttribute("aria-expanded", String(open));
    sheet.hidden = !open;
    root.classList.toggle("menu-open", open);
    document.body.style.overflow = open ? "hidden" : "";
    document.querySelector("main").inert = open;
    document.querySelector(".site-footer").inert = open;
  };
  menuBtn.addEventListener("click", () => setMenu(menuBtn.getAttribute("aria-expanded") !== "true"));
  sheet.addEventListener("click", (e) => { if (e.target.closest("a")) setMenu(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !sheet.hidden) { setMenu(false); menuBtn.focus(); }
  });
  window.matchMedia("(min-width: 900px)").addEventListener("change", (e) => { if (e.matches) setMenu(false); });

  // Scroll-spy: brass underline on the section in view
  const links = [...document.querySelectorAll('.site-nav a[href^="#"]')];
  const sections = links.map((a) => document.querySelector(a.getAttribute("href"))).filter(Boolean);
  if ("IntersectionObserver" in window) {
    const visible = new Map();
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => visible.set(e.target.id, e.isIntersecting));
      const active = sections.find((s) => visible.get(s.id));
      links.forEach((a) => {
        if (active && a.getAttribute("href") === `#${active.id}`) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    sections.forEach((s) => io.observe(s));
  }
}

applyContact();
applyCouncilLogo();
initReveal();
initI18n();
initTheme();
initNav();
initHero();
initSanadCard();
initGate({ onEnter: playIntro });
