// 起動処理とページ切り替え
import { gsap } from "gsap";
import { Stage } from "./core/stage.js";
import { UI } from "./core/ui.js";
import { detectQuality, prefersReducedMotion } from "./core/quality.js";
import { Home } from "./home/home.js";
import { MENU } from "./config/menu.js";
import { PAGES } from "./pages/index.js";

const pageEl = document.getElementById("page");
const pageInner = document.getElementById("page-inner");
const backBtn = document.getElementById("back");
let currentPage = null;

function routeToItem(pathname) {
  return MENU.find((m) => m.route === pathname.replace(/\/+$/, "")) || null;
}

function openPage(item, { push }) {
  const page = PAGES[item.id];
  if (!page) return;
  if (push) history.pushState({ id: item.id }, "", item.route);
  document.title = `${page.title} — RSGY`;
  currentPage = page;
  page.mount(pageInner);
  pageEl.scrollTop = 0;
  document.body.classList.add("page-open");
  gsap.fromTo(pageEl, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.7, ease: "power2.out" });
  gsap.fromTo(pageInner.querySelectorAll(".eyebrow, .page-title, .page-lead, .page-body"),
    { autoAlpha: 0, y: 24 }, { autoAlpha: 1, y: 0, duration: 1.1, stagger: 0.09, ease: "expo.out", delay: 0.15 });
}

function closePage({ push }, home) {
  if (!currentPage) return;
  if (push) history.pushState({}, "", "/");
  document.title = "RSGY";
  const page = currentPage;
  currentPage = null;
  gsap.to(pageEl, {
    autoAlpha: 0, duration: 0.5, ease: "power2.in",
    onComplete: () => { page.unmount(); pageInner.innerHTML = ""; document.body.classList.remove("page-open"); },
  });
  home.returnToMenu();
}

async function boot() {
  const quality = detectQuality();
  document.documentElement.dataset.quality = quality.name;
  const stage = new Stage(document.getElementById("gl"), quality);
  const ui = new UI();
  const home = new Home(stage, ui, { onSelect: (item) => openPage(item, { push: true }) });
  await home.build();
  stage.start();
  window.__rsgy = { home, stage }; // デバッグ・動作確認用

  const initial = routeToItem(location.pathname);
  if (initial) {
    // ページを直接開いた場合はオープニングを省略
    home.showMenuImmediately();
    home.orbs.forEach((o) => o.scale.setScalar(0.0001));
    home.state = "leaving";
    openPage(initial, { push: false });
  } else if (prefersReducedMotion()) {
    home.playOpening();
    home.skipOpening();
  } else {
    home.playOpening();
  }

  backBtn.addEventListener("click", () => closePage({ push: true }, home));
  window.addEventListener("popstate", () => {
    const item = routeToItem(location.pathname);
    if (item && !currentPage) { home.showMenuImmediately(); openPage(item, { push: false }); }
    else if (!item && currentPage) closePage({ push: false }, home);
  });
}

boot().catch((err) => {
  console.error(err);
  document.getElementById("fallback").hidden = false;
});
