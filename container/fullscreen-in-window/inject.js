// remote-chrome: emulate the Fullscreen API inside the slot window.
// Real fullscreen would make the slot's Chromium cover the whole shared
// 2880x1800 desktop (all four slots). Instead the requested element is put
// into the top layer (popover) and stretched over the viewport, and the page
// is told it is fullscreen (fullscreenElement + fullscreenchange events).
(() => {
  if (window.__fsiw) return;
  window.__fsiw = true;

  let fsEl = null;
  const STYLE_ID = '__fsiw_style';
  const CSS = `[data-fsiw]{position:fixed!important;inset:0!important;
    width:100vw!important;height:100vh!important;max-width:none!important;
    max-height:none!important;margin:0!important;padding:0!important;
    border:0!important;transform:none!important;
    z-index:2147483647!important;background:#000!important;}
    [data-fsiw]::backdrop{background:#000;}`;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  function fire(target) {
    for (const type of ['fullscreenchange', 'webkitfullscreenchange']) {
      const t = target && target.isConnected ? target : document;
      t.dispatchEvent(new Event(type, { bubbles: true, composed: true }));
    }
  }

  function enter(el) {
    if (fsEl === el) return;
    if (fsEl) exit();
    fsEl = el;
    // whole-page fullscreen (YouTube's player button requests it on <html>):
    // the page already fills the slot window, so only report the state. A
    // root element in the top layer / with forced styles renders black.
    if (el === document.documentElement || el === document.body) {
      fire(el);
      return;
    }
    ensureStyle();
    el.setAttribute('data-fsiw', '');
    // top layer escapes ancestor transforms/overflow, like real fullscreen
    try {
      if (!el.hasAttribute('popover')) {
        el.setAttribute('popover', 'manual');
        el.__fsiwPopover = true;
      }
      el.showPopover();
    } catch (e) { /* CSS alone still covers the viewport */ }
    fire(el);
  }

  function exit() {
    const el = fsEl;
    if (!el) return;
    fsEl = null;
    if (!el.hasAttribute('data-fsiw')) {
      fire(el);
      return;
    }
    try { el.hidePopover(); } catch (e) { /* not shown */ }
    if (el.__fsiwPopover) {
      el.removeAttribute('popover');
      delete el.__fsiwPopover;
    }
    el.removeAttribute('data-fsiw');
    fire(el);
  }

  const request = function () { enter(this); return Promise.resolve(); };
  const leave = function () { exit(); return Promise.resolve(); };
  const current = { get() { return fsEl; }, configurable: true };
  const active = { get() { return !!fsEl; }, configurable: true };

  for (const name of ['requestFullscreen', 'webkitRequestFullscreen',
                      'webkitRequestFullScreen']) {
    Element.prototype[name] = request;
  }
  for (const name of ['exitFullscreen', 'webkitExitFullscreen',
                      'webkitCancelFullScreen']) {
    Document.prototype[name] = leave;
  }
  for (const name of ['fullscreenElement', 'webkitFullscreenElement',
                      'webkitCurrentFullScreenElement']) {
    Object.defineProperty(Document.prototype, name, current);
  }
  for (const name of ['fullscreen', 'webkitIsFullScreen']) {
    Object.defineProperty(Document.prototype, name, active);
  }

  // Esc leaves fullscreen, as it does natively
  window.addEventListener('keydown', (e) => {
    if (fsEl && e.key === 'Escape') {
      e.stopImmediatePropagation();
      exit();
    }
  }, true);
})();
