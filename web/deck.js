'use strict';

// Zoomable canvas presentation: a camera (centre x, y and visible world width w) flies between DOM targets.
(() => {
  const data = JSON.parse(document.getElementById('deck-data').textContent);
  const viewport = document.getElementById('viewport');
  const canvas = document.getElementById('canvas');
  const root = document.documentElement;
  const body = document.body;
  const steps = new Map(data.steps.map((step) => [step.id, step]));
  const presenter = new URLSearchParams(location.search).has('presenter');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const subSteps = new Set(['diagram', 'code']);
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ } },
  };
  const $ = (selector, scope = document) => scope.querySelector(selector);

  let pathName = data.paths[storage.get('fde-path')] ? storage.get('fde-path') : 'onboarding';
  let index = 0;
  let camera = null;
  let frameRequest = 0;
  let detour = false;
  let notesOpen = presenter || storage.get('fde-notes') === 'open';
  let tab = 'notes';
  let notesKey = '';
  let timerStart = null;
  let remote = false;
  let clockSynced = false;

  const order = () => data.paths[pathName].steps;
  const current = () => steps.get(order()[index]);

  // ---------- geometry ----------
  function rectOf(id) {
    const element = document.getElementById(id);
    let x = 0;
    let y = 0;
    for (let node = element; node && node !== canvas; node = node.offsetParent) {
      x += node.offsetLeft;
      y += node.offsetTop;
    }
    return { x, y, w: element.offsetWidth, h: element.offsetHeight };
  }

  function insets() {
    const narrow = innerWidth < 760;
    const drawer = notesOpen && !presenter;
    const side = drawer && !narrow ? Math.min(470, innerWidth * 0.4) + 20 : 20;
    // On narrow screens the notes become a bottom sheet (46vh above the HUD).
    const bottom = narrow ? (drawer ? Math.round(innerHeight * 0.46) + 76 : 76) : 88;
    return { top: narrow ? 58 : 70, bottom, left: 20, right: side };
  }

  function fit(rect, pad) {
    const width = viewport.clientWidth;
    const height = viewport.clientHeight;
    const inset = insets();
    const usableW = Math.max(60, width - inset.left - inset.right);
    const usableH = Math.max(60, height - inset.top - inset.bottom);
    const scale = Math.min(usableW / (rect.w * pad), usableH / (rect.h * pad));
    return {
      x: rect.x + rect.w / 2 - (inset.left - inset.right) / 2 / scale,
      y: rect.y + rect.h / 2 - (inset.top - inset.bottom) / 2 / scale,
      w: width / scale,
    };
  }

  function apply(next) {
    camera = next;
    const width = viewport.clientWidth;
    const scale = width / next.w;
    const x = width / 2 - next.x * scale;
    const y = viewport.clientHeight / 2 - next.y * scale;
    canvas.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  // Smooth zoom-and-pan path (van Wijk & Nuij): zoom out on long moves, then back in.
  function zoomPath(from, to) {
    const rho = 1.36;
    const [x0, y0, w0] = from;
    const [x1, y1, w1] = to;
    const dx = x1 - x0;
    const dy = y1 - y0;
    const d2 = dx * dx + dy * dy;
    const rho2 = rho * rho;
    if (d2 < 1e-6) {
      const span = Math.log(w1 / w0) / rho;
      return { length: Math.abs(span), at: (t) => [x0 + t * dx, y0 + t * dy, w0 * Math.exp(rho * t * span)] };
    }
    const d1 = Math.sqrt(d2);
    const b0 = (w1 * w1 - w0 * w0 + rho2 * rho2 * d2) / (2 * w0 * rho2 * d1);
    const b1 = (w1 * w1 - w0 * w0 - rho2 * rho2 * d2) / (2 * w1 * rho2 * d1);
    const r0 = Math.log(Math.sqrt(b0 * b0 + 1) - b0);
    const r1 = Math.log(Math.sqrt(b1 * b1 + 1) - b1);
    const span = (r1 - r0) / rho;
    return {
      length: span,
      at(t) {
        const s = t * span;
        const coshR0 = Math.cosh(r0);
        const u = (w0 / (rho2 * d1)) * (coshR0 * Math.tanh(rho * s + r0) - Math.sinh(r0));
        return [x0 + u * dx, y0 + u * dy, (w0 * coshR0) / Math.cosh(rho * s + r0)];
      },
    };
  }

  function fly(target, instant) {
    cancelAnimationFrame(frameRequest);
    if (!camera || instant || reducedMotion.matches) {
      canvas.classList.remove('moving');
      apply(target);
      return;
    }
    const path = zoomPath([camera.x, camera.y, camera.w], [target.x, target.y, target.w]);
    const duration = Math.min(2100, Math.max(520, path.length * 780));
    const started = performance.now();
    canvas.classList.add('moving');
    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      const eased = t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
      const [x, y, w] = path.at(eased);
      apply({ x, y, w });
      if (t < 1) frameRequest = requestAnimationFrame(tick);
      else canvas.classList.remove('moving');  // re-rasterise text sharply at rest
    };
    frameRequest = requestAnimationFrame(tick);
  }

  const padding = (step) => ({ code: 1.04, diagram: 1.06, card: 1.035, title: 1.08 }[step.kind] || 1.015);

  // A code step shows the panel top-aligned: short snippets keep the panel filling the screen
  // instead of floating mid-screen with the card title cut off above it.
  function rectFor(step) {
    if (step.kind !== 'code') return rectOf(step.target);
    const focus = document.getElementById(step.target);
    const panel = rectOf(`panel-${step.frame}`);
    const inset = insets();
    const screen = (viewport.clientHeight - inset.top - inset.bottom) / (viewport.clientWidth - inset.left - inset.right);
    const content = focus.offsetTop + focus.offsetHeight + 44;
    return { ...panel, h: Math.min(panel.h, Math.max(content, panel.w * screen)) };
  }

  // ---------- navigation ----------
  function go(position, { instant = false, silent = false } = {}) {
    const list = order();
    index = Math.max(0, Math.min(list.length - 1, position));
    detour = false;
    const step = current();
    fly(fit(rectFor(step), padding(step)), instant);
    if (index > 0 && timerStart === null) timerStart = Date.now();
    render();
    history.replaceState(null, '', `#/${pathName}/${step.id}`);
    if (!silent && !remote) channel?.postMessage({ type: 'nav', path: pathName, index });
  }

  function flyFree(rect, label) {
    detour = true;
    fly(fit(rect, 1.03));
    render(label);
  }

  function setPath(name, keepStep = true) {
    if (!data.paths[name]) return;
    const stepId = current()?.id;
    pathName = name;
    storage.set('fde-path', name);
    const list = order();
    const found = keepStep ? list.indexOf(stepId) : -1;
    go(found >= 0 ? found : 0);
  }

  const isMain = (id) => !subSteps.has(steps.get(id).kind);

  function jumpFrame(direction) {
    const list = order();
    let position = index + direction;
    if (direction < 0) {
      // From a sub-step, first return to its own card.
      while (position > 0 && !isMain(list[position])) position -= 1;
    } else {
      while (position < list.length - 1 && !isMain(list[position])) position += 1;
    }
    go(position);
  }

  function goToStepId(id) {
    const list = order();
    // Prefer the occurrence nearest the current position (overview steps repeat).
    let best = -1;
    list.forEach((stepId, position) => {
      if (stepId === id && (best < 0 || Math.abs(position - index) < Math.abs(best - index))) best = position;
    });
    if (best >= 0) go(best);
    else if (steps.has(id)) flyFree(rectOf(steps.get(id).target), `${steps.get(id).label} · 현재 경로 밖`);
  }

  function toggleOverview() {
    if (detour) go(index);
    else flyFree(rectOf('canvas-bounds'), '전체 캔버스 · Esc로 복귀');
  }

  // ---------- rendering of HUD, notes and state ----------
  function frameOf(step) {
    return step && step.frame ? data.frames[step.frame] : null;
  }

  function render(freeLabel) {
    const list = order();
    const step = current();
    body.dataset.step = step.id;
    body.dataset.kind = step.kind;
    body.dataset.frame = step.frame || '';
    body.classList.toggle('detour', detour);
    document.querySelectorAll('.is-current, .is-focus').forEach((node) => node.classList.remove('is-current', 'is-focus'));
    const target = document.getElementById(step.target);
    target?.closest('.card, .cluster, .title-band')?.classList.add('is-current');
    target?.closest('.cluster, .overview-cards, .title-band')?.classList.add('is-focus');
    const factor = data.paths[pathName].factor;
    document.querySelectorAll('.card').forEach((card) => {
      const minutes = data.frames[card.dataset.frame].minutes * factor;
      card.querySelector('.card-minutes').textContent = `${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)}분`;
    });
    $('.hud-count').textContent = `${index + 1} / ${list.length}`;
    $('.hud-label').textContent = detour && freeLabel ? freeLabel : step.label;
    $('.progress span').style.width = `${((index + 1) / list.length) * 100}%`;
    document.querySelectorAll('[data-path]').forEach((button) => {
      button.setAttribute('aria-checked', String(button.dataset.path === pathName));
    });
    renderNotes(step);
  }

  function renderNotes(step) {
    const panel = $('#notes');
    panel.hidden = !notesOpen;
    $('[data-action="notes"]').setAttribute('aria-pressed', String(notesOpen));
    body.classList.toggle('notes-open', notesOpen);
    if (!notesOpen) return;
    const frame = frameOf(step);
    const path = data.paths[pathName];
    const list = order();
    const next = steps.get(list[index + 1]);
    $('.notes-kicker').textContent = frame ? `FRAME ${step.frame} · ${frame.kicker}` : path.label;
    $('.notes-title').textContent = frame ? frame.title : step.label;
    $('.notes-next').textContent = next ? `다음 → ${next.label}` : '마지막 단계입니다';
    const budget = frame ? frame.minutes * path.factor : null;
    $('.clock-budget').textContent = budget
      ? `프레임 예산 ${budget.toFixed(1)}분 · 경로 ${path.minutes}분`
      : `경로 전체 ${path.minutes}분`;
    document.querySelectorAll('[data-tab]').forEach((button) => {
      button.setAttribute('aria-selected', String(button.dataset.tab === tab));
    });
    const target = $('.notes-body');
    // Rebuild only when the content changes, so the presenter's scroll position survives sub-steps.
    const contentKey = `${tab}|${tab === 'appendix' ? '' : step.frame || step.id}|${pathName}`;
    if (contentKey === notesKey) return;
    notesKey = contentKey;
    target.scrollTop = 0;
    if (tab === 'appendix') target.innerHTML = data.appendix;
    else if (frame) {
      const points = frame.points.map((point) => `<li>${escapeHtml(point)}</li>`).join('');
      target.innerHTML = (tab === 'notes'
        ? `<p class="notes-message">${escapeHtml(frame.message)}</p><ol class="notes-points">${points}</ol>`
        : '') + frame[tab];
    } else {
      // Title, cluster and overview stops list the frames the presenter is about to cover.
      const frames = [...new Set(list.map((id) => steps.get(id).frame).filter(Boolean))];
      const wanted = step.kind === 'cluster' ? frames.filter((key) => key[0] === step.id.slice(1)) : frames;
      target.innerHTML = '<ol class="notes-outline">' + wanted.map((key) => {
        const item = data.frames[key];
        return `<li><span>${key}</span>${escapeHtml(item.title)}<em>${(item.minutes * path.factor).toFixed(1)}분</em></li>`;
      }).join('') + '</ol>';
    }
    target.querySelectorAll('a[href^="http"]').forEach((link) => {
      link.target = '_blank';
      link.rel = 'noopener';
    });
  }

  function escapeHtml(text) {
    return text.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function tickClock() {
    if (!notesOpen) return;
    const seconds = timerStart === null ? 0 : Math.floor((Date.now() - timerStart) / 1000);
    $('.clock-elapsed').textContent =
      `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }

  // Inline SVG diagrams carry no intrinsic size; fit each box to its panel keeping the aspect ratio.
  function sizeDiagrams() {
    document.querySelectorAll('.diagram').forEach((figure) => {
      const box = figure.firstElementChild;
      const ratio = Number(figure.dataset.ratio);
      const style = getComputedStyle(figure);
      const width = figure.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const height = figure.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
      const fitted = Math.min(width, height * ratio);
      box.style.width = `${fitted}px`;
      box.style.height = `${fitted / ratio}px`;
    });
  }

  function refit() {
    sizeDiagrams();
    if (!detour) fly(fit(rectFor(current()), padding(current())), true);
  }

  // ---------- input ----------
  const actions = {
    prev: () => (detour ? go(index) : go(index - 1)),
    next: () => go(index + 1),
    overview: toggleOverview,
    notes() {
      notesOpen = !notesOpen;
      if (!presenter) storage.set('fde-notes', notesOpen ? 'open' : 'closed');
      render();
      refit();
    },
    presenter() {
      const url = `${location.pathname}?presenter${location.hash}`;
      window.open(url, 'fde-presenter', 'popup,width=1280,height=800');
    },
    fullscreen() {
      if (document.fullscreenElement) document.exitFullscreen?.();
      else root.requestFullscreen?.().catch(() => {});
    },
    theme() {
      const dark = root.dataset.theme
        ? root.dataset.theme === 'dark'
        : matchMedia('(prefers-color-scheme: dark)').matches;
      root.dataset.theme = dark ? 'light' : 'dark';
      storage.set('fde-theme', root.dataset.theme);
    },
    help() {
      const help = $('#help');
      help.hidden = !help.hidden;
      if (!help.hidden) $('button', help).focus();
      else viewport.focus({ preventScroll: true });
    },
    'timer-reset'() {
      timerStart = Date.now();
      tickClock();
      channel?.postMessage({ type: 'timer', timerStart });
    },
  };

  document.addEventListener('click', (event) => {
    // <body> carries data-step too, so only real controls may match here.
    const control = event.target.closest('button[data-action], button[data-path], button[data-tab], a[data-step]');
    if (!control) return;
    // A mouse click should not leave focus on a button, or Space would press it again instead of advancing.
    if (event.detail > 0 && control.matches('button')) control.blur();
    if (control.dataset.action) actions[control.dataset.action]?.();
    else if (control.dataset.path) setPath(control.dataset.path);
    else if (control.dataset.tab) {
      tab = control.dataset.tab;
      render();
    } else if (control.dataset.step) {
      event.preventDefault();
      goToStepId(control.dataset.step);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.altKey || event.metaKey || event.ctrlKey) return;
    const help = $('#help');
    if (!help.hidden) {
      if (event.key === 'Escape' || event.key === '?') actions.help();
      return;
    }
    if (event.target.closest('button, a') && (event.key === ' ' || event.key === 'Enter')) return;
    const key = event.key;
    let handled = true;
    if (key === 'ArrowRight' || key === 'PageDown' || key === ' ') {
      if (event.shiftKey && key === 'ArrowRight') jumpFrame(1);
      else actions.next();
    } else if (key === 'ArrowLeft' || key === 'PageUp') {
      if (event.shiftKey) jumpFrame(-1);
      else actions.prev();
    } else if (key === 'ArrowDown') jumpFrame(1);
    else if (key === 'ArrowUp') jumpFrame(-1);
    else if (key === 'Home') go(0);
    else if (key === 'End') go(order().length - 1);
    else if (key === 'Escape') {
      if (detour) go(index);
      else toggleOverview();
    } else if (key === 'o' || key === 'O') toggleOverview();
    else if (key === 'n' || key === 'N') actions.notes();
    else if (key === 'p' || key === 'P') actions.presenter();
    else if (key === 'f' || key === 'F') actions.fullscreen();
    else if (key === 't' || key === 'T') actions.theme();
    else if (key === '?') actions.help();
    else if (/^[1-5]$/.test(key)) goToStepId(`c${key}`);
    else handled = false;
    if (handled) event.preventDefault();
  });

  // Click a card, panel or chapter to zoom there; drag to pan; wheel or pinch to zoom.
  function targetFor(element) {
    const step = current();
    const card = element.closest('.card');
    if (card) {
      const key = card.dataset.frame;
      const panel = element.closest('.diagram, .code');
      if (panel && step.frame === key) return panel.classList.contains('code') ? `${key}.code` : `${key}.diagram`;
      return key;
    }
    const cluster = element.closest('.cluster');
    if (cluster) return `c${cluster.id.split('-')[1]}`;
    if (element.closest('.title-band')) return 'title';
    return null;
  }

  let pointer = null;
  viewport.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false, type: event.pointerType };
  });
  viewport.addEventListener('pointermove', (event) => {
    if (!pointer || pointer.id !== event.pointerId || pointer.type === 'touch') return;
    const dx = event.clientX - pointer.x;
    const dy = event.clientY - pointer.y;
    if (!pointer.moved && Math.hypot(dx, dy) < 5) return;
    if (!pointer.moved) {
      pointer.moved = true;
      viewport.setPointerCapture(event.pointerId);
      cancelAnimationFrame(frameRequest);
      canvas.classList.remove('moving');
    }
    const scale = viewport.clientWidth / camera.w;
    apply({ x: camera.x - dx / scale, y: camera.y - dy / scale, w: camera.w });
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    if (!detour) {
      detour = true;
      render('자유 탐색 · → 키로 경로 복귀');
    }
  });
  viewport.addEventListener('pointerup', (event) => {
    if (!pointer || pointer.id !== event.pointerId) return;
    const start = pointer;
    pointer = null;
    if (start.type === 'touch') {
      const dx = event.clientX - start.x;
      const dy = event.clientY - start.y;
      if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        if (dx < 0) actions.next();
        else actions.prev();
        return;
      }
    }
    if (start.moved) return;
    const id = targetFor(event.target);
    if (id && id !== current().id) goToStepId(id);
  });
  viewport.addEventListener('pointercancel', () => { pointer = null; });

  viewport.addEventListener('wheel', (event) => {
    event.preventDefault();
    cancelAnimationFrame(frameRequest);
    canvas.classList.remove('moving');
    const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
    const factor = Math.exp(delta * (event.ctrlKey ? 0.01 : 0.0015));
    const bounds = viewport.getBoundingClientRect();
    const scale = viewport.clientWidth / camera.w;
    const sx = event.clientX - bounds.left - viewport.clientWidth / 2;
    const sy = event.clientY - bounds.top - viewport.clientHeight / 2;
    const wx = camera.x + sx / scale;
    const wy = camera.y + sy / scale;
    const limit = rectOf('canvas-bounds').w * 1.6;
    const width = Math.min(limit, Math.max(260, camera.w * factor));
    const next = viewport.clientWidth / width;
    apply({ x: wx - sx / next, y: wy - sy / next, w: width });
    if (!detour) {
      detour = true;
      render('자유 탐색 · → 키로 경로 복귀');
    }
  }, { passive: false });

  // Hide the controls while presenting full screen and the pointer is idle.
  let idleTimer = 0;
  const wake = () => {
    body.classList.remove('idle');
    clearTimeout(idleTimer);
    if (document.fullscreenElement && !presenter) idleTimer = setTimeout(() => body.classList.add('idle'), 2600);
  };
  ['pointermove', 'keydown'].forEach((type) => document.addEventListener(type, wake, { passive: true }));
  document.addEventListener('fullscreenchange', () => {
    wake();
    setTimeout(refit, 60);
  });

  // ---------- presenter window sync ----------
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('fde-deck') : null;
  const presenterButton = $('[data-action="presenter"]');
  if (!channel && presenterButton) presenterButton.hidden = true;
  channel?.addEventListener('message', ({ data: message }) => {
    if (message.type === 'hello' && !presenter) {
      channel.postMessage({ type: 'nav', path: pathName, index, timerStart });
    } else if (message.type === 'nav') {
      remote = true;
      if (message.path !== pathName && data.paths[message.path]) pathName = message.path;
      // Every open deck window answers a presenter's hello; the earliest start is when the talk began.
      if (message.timerStart) {
        timerStart = clockSynced ? Math.min(timerStart, message.timerStart) : message.timerStart;
        clockSynced = true;
      }
      go(message.index);
      remote = false;
    } else if (message.type === 'timer') {
      timerStart = message.timerStart;
      tickClock();
    }
  });

  // ---------- start ----------
  const savedTheme = storage.get('fde-theme');
  if (savedTheme === 'dark' || savedTheme === 'light') root.dataset.theme = savedTheme;
  if (presenter) body.classList.add('presenter');

  function fromHash() {
    const match = location.hash.match(/^#\/(\w+)\/([\w.-]+)$/);
    if (!match || !data.paths[match[1]]) return false;
    pathName = match[1];
    const position = order().indexOf(match[2]);
    go(position >= 0 ? position : 0, { instant: true, silent: true });
    return true;
  }

  sizeDiagrams();
  if (!fromHash()) go(0, { instant: true, silent: true });
  addEventListener('hashchange', () => {
    const expected = `#/${pathName}/${current().id}`;
    if (location.hash !== expected) fromHash();
  });
  addEventListener('resize', () => requestAnimationFrame(refit));
  document.fonts?.ready.then(refit);
  if (presenter) channel?.postMessage({ type: 'hello' });
  setInterval(tickClock, 1000);
  body.classList.add('ready');
  viewport.focus({ preventScroll: true });
})();
