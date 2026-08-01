/* Иконки из HH Agent Spec.dc.html — сетка 16×16, штрих 1.4, currentColor. */
const ICON = {
  // навигация (16×16, stroke 1.4)
  navWork: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.4"/><path d="M6.6 5.6 10.4 8l-3.8 2.4V5.6Z" fill="currentColor"/></svg>`,
  navStats: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M3.2 12.8V9.4M8 12.8V5.2M12.8 12.8V7.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  navResume: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><rect x="3.2" y="2.2" width="9.6" height="11.6" rx="2" stroke="currentColor" stroke-width="1.4"/><path d="M5.8 6h4.4M5.8 8.6h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  navFilters: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M2.4 5.4h11.2M2.4 10.6h11.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="6" cy="5.4" r="1.9" fill="var(--bg)" stroke="currentColor" stroke-width="1.4"/><circle cx="10.4" cy="10.6" r="1.9" fill="var(--bg)" stroke="currentColor" stroke-width="1.4"/></svg>`,
  navModel: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M8 2.2 13.2 5v6L8 13.8 2.8 11V5L8 2.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>`,
  navNotify: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M4.6 10.8c0-.7.5-1.1.5-1.9V7.2a2.9 2.9 0 0 1 5.8 0v1.7c0 .8.5 1.2.5 1.9H4.6Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M6.9 12.6h2.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  navChat: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M2.6 3.6h10.8v7.2H6.6L3.6 13.2V10.8H2.6V3.6Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>`,

  // действия (14×14)
  play12: `<svg width="12" height="12" viewBox="0 0 14 14"><path d="M4 2.8 11 7l-7 4.2V2.8Z" fill="currentColor"/></svg>`,
  stop11: `<svg width="11" height="11" viewBox="0 0 14 14"><rect x="3.2" y="3.2" width="7.6" height="7.6" rx="1.8" fill="currentColor"/></svg>`,
  refresh12: `<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M12 7a5 5 0 1 1-1.6-3.7M12 2.4V5h-2.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  download12: `<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M7 2v6.4M4.4 6l2.6 2.4L9.6 6M2.6 11.4h8.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  search12: `<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><circle cx="6.2" cy="6.2" r="3.9" stroke="currentColor" stroke-width="1.3"/><path d="M9.2 9.2 12 12" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  pin15: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M8 14s4.6-4.2 4.6-7.4A4.6 4.6 0 0 0 3.4 6.6C3.4 9.8 8 14 8 14Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><circle cx="8" cy="6.4" r="1.7" stroke="currentColor" stroke-width="1.4"/></svg>`,
  globe15: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.4"/><path d="M1.8 8h12.4M8 1.8c1.6 1.8 2.4 3.9 2.4 6.2S9.6 12.4 8 14.2C6.4 12.4 5.6 10.3 5.6 8s.8-4.4 2.4-6.2Z" stroke="currentColor" stroke-width="1.4"/></svg>`,

  // уровни журнала (14×14)
  logOk: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M3 7.4 5.6 10 11 4.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  logViewing: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="2.2" stroke="currentColor" stroke-width="1.3"/><path d="M1.6 7c1.6-2.6 3.4-3.9 5.4-3.9S10.8 4.4 12.4 7c-1.6 2.6-3.4 3.9-5.4 3.9S3.2 9.6 1.6 7Z" stroke="currentColor" stroke-width="1.3"/></svg>`,
  logSkip: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M4.6 3.4 9 7l-4.4 3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  logReject: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.4" stroke="currentColor" stroke-width="1.3"/><path d="M4.6 7h4.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  logWarn: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.4" stroke="currentColor" stroke-width="1.3"/><path d="M7 4.4v3M7 9.6v.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  logError: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M7 2.6 12.6 11.6H1.4L7 2.6Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M7 6v2.4M7 10.2v.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,

  // служебные
  spinner15: `<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.5" opacity=".25"/><path d="M8 1.8A6.2 6.2 0 0 1 14.2 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  timer12: `<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.4" stroke="currentColor" stroke-width="1.3"/><path d="M7 4.4V7l1.8 1.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  captcha18: `<svg width="18" height="18" viewBox="0 0 18 18" fill="none"><rect x="3.4" y="7.6" width="11.2" height="7.4" rx="2.2" stroke="currentColor" stroke-width="1.5"/><path d="M6.2 7.6V5.8a2.8 2.8 0 0 1 5.6 0v1.8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  errTriangle17: `<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><path d="M9 3.2 16.2 15H1.8L9 3.2Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M9 7.4v3.2M9 12.7v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  warnTriangle17: `<svg width="17" height="17" viewBox="0 0 18 18" fill="none"><circle cx="9" cy="9" r="7.2" stroke="currentColor" stroke-width="1.5"/><path d="M9 5.4v4.2M9 12.4v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  plus11: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M6 2.4v7.2M2.4 6h7.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  minus11: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M2.6 6h6.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  plus12: `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M6 2.4v7.2M2.4 6h7.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  remove11: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`,
  attach16: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M11.2 4.4 5.6 10a2.2 2.2 0 1 0 3.1 3.1l5.2-5.2a3.7 3.7 0 0 0-5.2-5.2L3.5 7.9a5.2 5.2 0 0 0 7.4 7.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  check12: `<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M3 7.4 5.6 10 11 4.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  checkTick11: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M2.6 6.2 4.6 8.2 9 3.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  circleOk16: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7.1" stroke="currentColor" stroke-width="1.3"/><path d="M5 8.2 7 10.2l4-4.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  circleWarn16: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7.1" stroke="currentColor" stroke-width="1.3"/><path d="M8 4.8v4M8 11.1v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  circleErr16: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7.1" stroke="currentColor" stroke-width="1.3"/><path d="M8 4.8v4M8 11.1v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  chevron10: `<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2.4 4 5 6.6 7.6 4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevronDown: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M2.6 4.4 6 8l3.4-3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevronRight11: `<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M4.4 2.6 8 6l-3.6 3.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  dot: `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--faint);margin-right:8px"></span>`,
};

// Заполняем статичные <i data-icon="..."> в разметке — используется и для
// динамически создаваемых элементов через ICON.xxx напрямую в app.js.
function paintIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach(el => {
    const svg = ICON[el.dataset.icon];
    if (svg) el.innerHTML = svg;
  });
}
document.addEventListener("DOMContentLoaded", () => paintIcons());
