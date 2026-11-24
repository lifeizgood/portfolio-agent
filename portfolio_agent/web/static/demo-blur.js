// Demo build only (branch blurred-for-demo): blurs dollar amounts on the page for screen
// recordings. Other numbers (dates, counts, quantities, account digits) stay readable.
// The data itself is unchanged; only the display is.
(() => {
  const MASK = "$•••••";
  // Chart: y-axis labels and tooltips never show amounts.
  const RealChart = window.Chart;
  if (RealChart) {
    window.Chart = class extends RealChart {
      constructor(el, cfg) {
        const o = cfg.options = cfg.options || {};
        o.scales = o.scales || {}; o.scales.y = o.scales.y || {};
        o.scales.y.ticks = { ...(o.scales.y.ticks || {}), callback: () => MASK };
        o.plugins = o.plugins || {}; o.plugins.tooltip = o.plugins.tooltip || {};
        o.plugins.tooltip.callbacks = { ...(o.plugins.tooltip.callbacks || {}),
          label: (ctx) => `${ctx.dataset.label}: ${MASK}` };
        super(el, cfg);
      }
    };
  }

  // A dollar amount: $1,234.56, -$300.00, −$5, $ 12
  const DOLLARS = /[-−]?\$\s?\d[\d,]*(?:\.\d+)?/g;
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "INPUT", "SELECT", "OPTION", "CANVAS", "TITLE"]);
  const keepEl = (el) => !el || SKIP.has(el.tagName) || el.closest(".demo-blur");

  function blurText(node) {
    const t = node.nodeValue;
    if (!t || !t.includes("$")) return;
    const frag = document.createDocumentFragment();
    let last = 0, found = false;
    for (const m of t.matchAll(DOLLARS)) {
      found = true;
      if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
      const s = document.createElement("span");
      s.className = "demo-blur"; s.textContent = m[0];
      frag.appendChild(s);
      last = m.index + m[0].length;
    }
    if (!found) return;
    if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
  function scan(root) {
    if (root.nodeType === Node.TEXT_NODE) { if (!keepEl(root.parentElement)) blurText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE || keepEl(root)) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => keepEl(n.parentElement) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    const nodes = []; while (w.nextNode()) nodes.push(w.currentNode);
    nodes.forEach(blurText);
  }
  const start = () => {
    scan(document.body);
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "characterData") scan(m.target);
        else m.addedNodes.forEach(scan);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  };
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();
