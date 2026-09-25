import en from "./i18n/en.json";
import hi from "./i18n/hi.json";
import mr from "./i18n/mr.json";

const DICTS = { en, hi, mr };
const LANGS = Object.keys(DICTS);
const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const listeners = new Set();

export let currentLang = LANGS.includes(document.documentElement.lang) ? document.documentElement.lang : "en";

export function t(key, lang = currentLang) {
  return DICTS[lang][key] ?? DICTS.en[key] ?? "";
}

export function onLanguageChange(fn) { listeners.add(fn); }

// Split by word, never by letter: letter-splitting breaks Devanagari conjuncts.
function splitWords(el, text) {
  el.textContent = "";
  text.split(/\s+/).filter(Boolean).forEach((word, i) => {
    const span = document.createElement("span");
    span.className = "word";
    span.style.setProperty("--i", i);
    span.textContent = word;
    el.append(span, " ");
  });
}

function apply(lang) {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const value = t(el.dataset.i18n, lang);
    if (el.hasAttribute("data-i18n-split")) {
      splitWords(el, value);
    } else {
      el.textContent = value;
    }
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria, lang));
  });
  document.querySelectorAll("[data-i18n-alt]").forEach((el) => {
    el.setAttribute("alt", t(el.dataset.i18nAlt, lang));
  });
  document.querySelectorAll("[data-lang]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.lang === lang));
  });
  document.querySelectorAll("[data-lang-select]").forEach((sel) => { sel.value = lang; });
  currentLang = lang;
  listeners.forEach((fn) => fn(lang));
}

export function setLanguage(lang, { animate = true } = {}) {
  if (!LANGS.includes(lang)) return;
  try { localStorage.setItem("lang", lang); } catch (e) { /* storage unavailable */ }
  if (!animate || reduceMotion() || lang === currentLang) {
    apply(lang);
    return;
  }
  const root = document.documentElement;
  root.classList.add("is-switching");
  window.setTimeout(() => {
    apply(lang);
    requestAnimationFrame(() => root.classList.remove("is-switching"));
  }, 180);
}

export function initI18n() {
  apply(currentLang);
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-lang]");
    if (btn) setLanguage(btn.dataset.lang);
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-lang-select]")) setLanguage(e.target.value);
  });
}
