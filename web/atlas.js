'use strict';

// Prezi-style zoom atlas. The camera is {x, y, s, r}: the world point at the centre of the free screen area,
// the scale (screen px per world px) and the rotation in degrees that makes the focused node upright.
// Every target box comes from the build (atlas.py), so nothing is measured from the DOM.
(() => {
  const data = JSON.parse(document.getElementById('atlas-data').textContent);
  const stage = document.getElementById('stage');
  const world = document.getElementById('world');
  const backdrop = document.getElementById('backdrop');
  const root = document.documentElement;
  const body = document.body;
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const nodes = Object.assign(Object.create(null), data.nodes);
  data.frames = Object.assign(Object.create(null), data.frames);
  const steps = new Map(data.steps.map((step) => [step.id, step]));
  const params = new URLSearchParams(location.search);
  const presenter = params.has('presenter');
  const opener = params.get('from');  // the deck window that pressed P
  const tabId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const DETAIL = new Set(['diagram', 'code', 'data']);
  const CONTAINERS = ['hub', 'p1', 'p2', 'p3', 'p4', 'p5', 'p6'].filter((id) => nodes[id]);
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ } },
  };
  const elementOf = (id) => (id === 'overview' || id === 'closing' ? null : document.getElementById(`n-${id.replace('.', '-')}`));
  const hasPath = (name) => typeof name === 'string' && Object.hasOwn(data.paths, name);
  const narrowQuery = matchMedia('(max-width: 760px)');
  const FRAMES = Object.keys(data.frames);   // story order
  // A frame step fits its text column and windows rather than the whole circle: about 40% larger type.
  const FRAME_BOX = { w: 3500, h: 2900 };
  // A detail step fits the whole window (card), so its label and edges clear the top bar and the dock.
  const PORTAL_BOX = { w: 960, h: 540 };
  const phonePortrait = () => innerWidth < 600 && innerHeight > innerWidth;

  let pathName = hasPath(storage.get('fde-path')) ? storage.get('fde-path') : 'interview';
  let index = 0;
  let cam = null;
  let flight = 0;
  let pending = null;
  let detour = false;
  let focusId = 'overview';  // the level currently revealed
  let aim = 'overview';      // where the camera is going (equals focusId at rest)
  let notesOpen = presenter || storage.get('fde-notes') === 'open';
  let tab = 'speaker';
  let notesKey = '';
  let timerStart = null;
  let remote = false;
  let lastNavigated = 0;
  let settleTimer = 0;

  const order = () => data.paths[pathName].steps;
  const session = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* storage may be blocked */ } },
  };
  // The talk clock survives a reload of this tab, so a reloaded projector never restarts everyone's timer.
  function setTimer(value) {
    timerStart = Number.isFinite(value) ? value : null;
    session.set('fde-timer', timerStart === null ? '' : String(timerStart));
  }
  timerStart = Number(session.get('fde-timer')) || null;
  const current = () => steps.get(order()[index]);

  // ------------------------------------------------------------ geometry
  function insets() {
    const narrow = narrowQuery.matches;
    if (presenter) return { top: 64, right: Math.round(innerWidth * 0.36) + 24, bottom: 78, left: 16 };
    const right = notesOpen && !narrow ? Math.min(460, innerWidth * 0.38) + 28 : 16;
    // On a phone the lower 40% holds the readable sheet (topics, frame text); notes take 46% instead.
    const sheet = phonePortrait() ? Math.round(innerHeight * 0.4) + 78 : 72;
    const bottom = narrow ? (notesOpen ? Math.round(innerHeight * 0.46) + 78 : sheet) : 72;
    return { top: narrow ? 56 : 60, right, bottom, left: 16 };
  }

  function area() {
    const inset = insets();
    const w = Math.max(80, innerWidth - inset.left - inset.right);
    const h = Math.max(80, innerHeight - inset.top - inset.bottom);
    return { w, h, cx: inset.left + w / 2, cy: inset.top + h / 2 };
  }

  const PAD = { overview: 1.0, closing: 1.0, hub: 1.03, planet: 1.03, frame: 1.0, diagram: 1.035, code: 1.035, data: 1.035 };

  function fitNode(id) {
    const node = nodes[id];
    const free = area();
    const pad = PAD[node.kind] || 1.03;
    const box = node.kind === 'frame' ? FRAME_BOX : DETAIL.has(node.kind) ? PORTAL_BOX : node;
    const s = Math.min(free.w / (box.w * pad), free.h / (box.h * pad));
    return { x: node.x, y: node.y, s, r: node.rot || 0 };
  }

  function apply(next) {
    cam = next;
    const free = area();
    world.style.transform =
      `translate(${free.cx}px,${free.cy}px) rotate(${-cam.r}deg) scale(${cam.s}) translate(${-cam.x}px,${-cam.y}px)`;
    // A dotted plane far behind the canvas: it scales and slides at a fraction of the camera (parallax).
    const base = fitNode('overview').s;
    const depth = Math.pow(cam.s / base, 0.3);
    const grid = 38 * depth;
    const shift = 0.55 * base * depth;
    backdrop.style.backgroundSize = `${grid}px ${grid}px`;
    backdrop.style.backgroundPosition = `${(-cam.x * shift) % grid}px ${(-cam.y * shift) % grid}px`;
    backdrop.style.transform = `rotate(${-cam.r}deg)`;
  }

  // Screen point → world point under the current camera.
  function toWorld(px, py) {
    const free = area();
    const a = (cam.r * Math.PI) / 180;
    const dx = (px - free.cx) / cam.s;
    const dy = (py - free.cy) / cam.s;
    return { x: cam.x + dx * Math.cos(a) - dy * Math.sin(a), y: cam.y + dx * Math.sin(a) + dy * Math.cos(a) };
  }

  // Smooth zoom-and-pan path (van Wijk & Nuij): long moves zoom out, travel, and zoom back in.
  function zoomPath(from, to) {
    const rho = 1.42;
    const [x0, y0, w0] = from;
    const [x1, y1, w1] = to;
    const dx = x1 - x0;
    const dy = y1 - y0;
    const d1 = Math.hypot(dx, dy);
    const d2 = d1 * d1;
    const rho2 = rho * rho;
    if (d1 < 1e-6 * Math.max(w0, w1)) {
      const span = Math.log(w1 / w0) / rho;
      return { length: Math.abs(span), at: (t) => [x0 + t * dx, y0 + t * dy, w0 * Math.exp(rho * t * span)] };
    }
    const b0 = (w1 * w1 - w0 * w0 + rho2 * rho2 * d2) / (2 * w0 * rho2 * d1);
    const b1 = (w1 * w1 - w0 * w0 - rho2 * rho2 * d2) / (2 * w1 * rho2 * d1);
    const r0 = -Math.asinh(b0);   // = log(sqrt(b² + 1) − b) without cancellation
    const r1 = -Math.asinh(b1);
    const span = (r1 - r0) / rho;
    const coshR0 = Math.cosh(r0);
    return {
      length: Math.abs(span),
      at(t) {
        const s = t * span;
        const u = (w0 / (rho2 * d1)) * (coshR0 * Math.tanh(rho * s + r0) - Math.sinh(r0));
        return [x0 + u * dx, y0 + u * dy, (w0 * coshR0) / Math.cosh(rho * s + r0)];
      },
    };
  }

  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

  // ``arrive`` runs once the flight is ``at`` (0-1) of the way there; zoom-outs reveal the shallower level late.
  function fly(target, instant = false, arrive = null, at = 0) {
    cancelAnimationFrame(flight);
    pending?.();
    pending = arrive;
    if (!cam || instant || reducedMotion.matches) {
      world.classList.remove('moving');
      apply(target);
      if (pending) [pending, arrive] = [null, pending];
      arrive?.();
      return;
    }
    const from = cam;
    const free = area();
    const path = zoomPath([from.x, from.y, free.w / from.s], [target.x, target.y, free.w / target.s]);
    if (!Number.isFinite(path.length) || !path.at(0.5).every(Number.isFinite)) {
      fly(target, true);
      return;
    }
    const turn = ((((target.r - from.r) % 360) + 540) % 360) - 180;
    const duration = Math.min(2600, Math.max(720, path.length * 820 + Math.abs(turn) * 9));
    const started = performance.now();
    world.classList.add('moving');
    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      const e = ease(t);
      const [x, y, w] = path.at(e);
      apply({ x, y, s: free.w / w, r: from.r + turn * e });
      if (pending && t >= at) {
        const run = pending;
        pending = null;
        run();
      }
      if (t < 1) flight = requestAnimationFrame(tick);
      else {
        apply(target);
        world.classList.remove('moving');  // re-rasterise text sharply at rest
      }
    };
    flight = requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------ zoom reveal (level of detail)
  function chain(id) {
    // [container, frame, portal] for a node id.
    const node = nodes[id];
    if (!node || node.kind === 'overview' || node.kind === 'closing') return [null, null, null];
    if (node.kind === 'hub' || node.kind === 'planet') return [id, null, null];
    if (node.kind === 'frame') return [node.parent, id, null];
    return [nodes[node.parent].parent, node.parent, id];
  }

  function setFocus(id) {
    focusId = id;
    const [container, frame, portal] = chain(id);
    $$('#world .is-open, #world .is-current, #world .is-focus')
      .forEach((element) => element.classList.remove('is-open', 'is-current', 'is-focus'));
    if (container) {
      elementOf(container).classList.add('is-open');
      document.getElementById(`cover-${container}`)?.classList.add('is-open');
    }
    if (frame) elementOf(frame).classList.add('is-current');
    if (portal) elementOf(portal).classList.add('is-focus');
    body.dataset.depth = portal ? 'detail' : frame ? 'frame' : container === 'hub' ? 'hub' : container ? 'planet' : 'overview';
    $$('#world .portal a[href]').forEach((link) => {
      link.tabIndex = link.closest('.portal.is-focus') ? 0 : -1;
    });
    showSheet(portal || frame || container || 'overview');
  }

  // Phones in portrait cannot read a 16:9 window scaled to their width: show its content as a sheet.
  function showSheet(id) {
    const sheet = $('#sheet');
    if (!id || !phonePortrait() || (notesOpen && !presenter)) {
      sheet.hidden = true;
      return;
    }
    const list = (items) => '<ul class="sheet-list">' + items.join('') + '</ul>';
    if (id === 'overview' && !detour && current()?.id === 'closing') {
      $('.sheet-title').textContent = '네 영역의 결과';
      $('.sheet-body').innerHTML = `<p class="sheet-message">${escapeHtml($('.hub-closing').textContent)}</p>`
        + '<ol class="sheet-points">' + [1, 2, 3, 4].map((n) =>
          `<li><strong>${escapeHtml(data.planets[n].outcome)}</strong> · ${escapeHtml(data.planets[n].proof)}</li>`).join('') + '</ol>';
      sheet.dataset.kind = 'frame';
      sheet.hidden = false;
      return;
    }
    if (id === 'overview') {
      // The overview on a phone: the six topics and the hub as a list under the canvas.
      $('.sheet-title').textContent = 'FDE 캔버스 · 주제';
      $('.sheet-body').innerHTML = list(['hub', ...CONTAINERS.filter((c) => c !== 'hub')].map((c) => {
        const number = c === 'hub' ? 0 : Number(c.slice(1));
        const name = number ? data.planets[number].name : '표지 · 0-1 · 0-2';
        const count = FRAMES.filter((key) => nodes[key]?.parent === c).length;
        return `<li><button type="button" data-goto="${c}" class="c${number}"><b>${String(number).padStart(2, '0')}</b>`
          + `<span>${escapeHtml(name)}</span><em>${count}개</em></button></li>`;
      }));
      sheet.dataset.kind = 'list';
      sheet.hidden = false;
      return;
    }
    if (nodes[id].kind === 'frame') {
      const frame = data.frames[id];
      $('.sheet-title').textContent = `${id} ${frame.label}`;
      $('.sheet-body').innerHTML = `<p class="sheet-kicker">${escapeHtml(frame.kicker)}</p><h3>${escapeHtml(frame.title)}</h3>`
        + `<p class="sheet-message">${escapeHtml(frame.message)}</p><ol class="sheet-points">`
        + frame.points.map((point) => `<li>${escapeHtml(point)}</li>`).join('') + '</ol>'
        + '<div class="sheet-links">' + ['diagram', 'code', 'data'].map((kind) =>
          `<button type="button" data-goto="${id}.${kind}">${{ diagram: '도식', code: '코드', data: '데이터' }[kind]}</button>`).join('') + '</div>';
      sheet.dataset.kind = 'frame';
      sheet.hidden = false;
      return;
    }
    if (!DETAIL.has(nodes[id].kind)) {
      // A topic on a phone: list its frames under the canvas so each one is one tap away.
      const keys = FRAMES.filter((key) => nodes[key]?.parent === id);
      $('.sheet-title').textContent = crumbs(id).slice(-1)[0].label;
      const spaced = (element) => element.innerText.replace(/\s+/g, ' ').trim();
      const caseQuote = spaced($('.hub-case')).replace(/^CASE\s*/, '');
      const intro = id === 'hub'
        ? `<p class="sheet-message">${escapeHtml(spaced($('.hub-question')))}</p>`
          + `<p class="sheet-case"><b>CASE</b> ${escapeHtml(caseQuote)}</p>`
        : '';
      $('.sheet-body').innerHTML = intro + list(keys.map((key) => {
        const frame = data.frames[key];
        return `<li><button type="button" data-goto="${key}" class="c${frame.cluster}"><b>${key}</b>`
          + `<span>${escapeHtml(frame.label)}</span><em>${formatMinutes(frame.minutes * data.paths[pathName].factor)}</em></button></li>`;
      }));
      sheet.dataset.kind = 'list';
      sheet.hidden = false;
      return;
    }
    const portal = id;
    const source = elementOf(portal).querySelector('.portal-inner');
    const content = source.cloneNode(true);
    content.querySelectorAll('[style]').forEach((element) => element.removeAttribute('style'));
    content.querySelectorAll('.portal-tag').forEach((element) => element.remove());
    content.querySelectorAll('svg').forEach((svg) => {
      const box = svg.getAttribute('viewBox')?.split(/\s+/).map(Number);
      if (box) svg.style.width = `${Math.max(320, box[2] * 0.9)}px`;
    });
    $('.sheet-title').textContent = crumbs(portal).slice(-2).map((item) => item.label).join(' · ');
    $('.sheet-body').replaceChildren(...content.childNodes);
    sheet.dataset.kind = nodes[portal].kind;
    sheet.hidden = false;
  }

  // During free exploration the deepest node that holds the screen centre and fills most of it is "open".
  function focusFromCamera() {
    const free = area();
    const short = Math.min(free.w, free.h);
    const inside = (node) => Math.hypot(node.x - cam.x, node.y - cam.y) < node.w / 2;
    const container = CONTAINERS.find((id) => inside(nodes[id]) && nodes[id].w * cam.s >= 0.55 * short);
    if (!container) return 'overview';
    const frame = Object.keys(nodes).find((id) => nodes[id].kind === 'frame' && nodes[id].parent === container
      && inside(nodes[id]) && nodes[id].w * cam.s >= 0.6 * short);
    if (!frame) return container;
    const portal = Object.keys(nodes).find((id) => nodes[id].parent === frame && DETAIL.has(nodes[id].kind)
      && Math.abs(nodes[id].x - cam.x) < nodes[id].w / 2 && Math.abs(nodes[id].y - cam.y) < nodes[id].h / 2
      && nodes[id].w * cam.s >= 0.45 * free.w);
    return portal || frame;
  }

  // ------------------------------------------------------------ navigation
  const RANK = { overview: 0, hub: 1, planet: 1, frame: 2, detail: 3 };
  const rankOf = (id) => {
    const [container, frame, portal] = chain(id);
    return portal ? 3 : frame ? 2 : container ? 1 : 0;
  };

  // Fly to a node and reveal its level: at once when zooming in or across, late when zooming out,
  // so the content being left stays visible while it shrinks into its parent.
  function travel(id, instant) {
    clearTimeout(settleTimer);
    if (pending) {
      const run = pending;
      pending = null;
      run();
    }
    aim = id;
    const outward = rankOf(id) < rankOf(focusId);
    if (!outward) setFocus(id);
    fly(fitNode(id), instant, outward ? () => setFocus(id) : null, 0.62);
  }

  function go(position, { instant = false, silent = false } = {}) {
    const list = order();
    index = Math.max(0, Math.min(list.length - 1, position));
    detour = false;
    const step = current();
    body.dataset.step = step.id;
    travel(step.node, instant);
    if (index > 0 && timerStart === null && !silent) setTimer(Date.now());
    render();
    history.replaceState(null, '', hashFor(index));
    $('.sr-status').textContent = `${index + 1}/${list.length} ${step.label}`;
    if (!silent) movedSinceHello = true;
    if (!silent && !remote) {
      lastNavigated = Date.now();
      channel?.postMessage({ type: 'nav', path: pathName, index, timerStart });
    }
  }

  // '#path~step', plus '~position' for steps a path visits more than once (the overview returns).
  function hashFor(position) {
    const list = order();
    const id = list[position];
    return `#${pathName}~${id}${list.indexOf(id) !== list.lastIndexOf(id) ? `~${position}` : ''}`;
  }

  function flyFree(id) {
    detour = true;
    travel(id, false);
    render();
    $('.sr-status').textContent = `자유 탐색 · ${crumbs(id).slice(-1)[0].label}`;
    if (!remote) channel?.postMessage({ type: 'free', aim: id });
  }

  // Click, breadcrumb and number keys: use the path when it visits the node, otherwise explore freely.
  function goToNode(id) {
    if (!nodes[id]) return;
    const list = order();
    let best = -1;
    list.forEach((stepId, position) => {
      if (stepId === id && (best < 0 || Math.abs(position - index) < Math.abs(best - index))) best = position;
    });
    if (best >= 0) go(best);
    else flyFree(id);
  }

  function parentOf(id) {
    const node = nodes[id];
    if (!node || id === 'overview') return null;
    return node.parent || 'overview';
  }

  // Is ``ancestor`` the node itself or one of its parents?
  const contains = (ancestor, id) => ancestor === 'overview' || chain(id).includes(ancestor);

  // Zooming out (Esc, empty space, breadcrumb, clicking an enclosing topic) shows context without rewinding
  // the path. Only a detail returning to its own frame stays a path step, because that frame is the step before.
  function ascend(id) {
    const list = order();
    if (!detour) {
      const own = list.lastIndexOf(id, index);
      if (own >= 0 && nodes[id]?.kind === 'frame' && list.slice(own + 1, index + 1).every((stepId) => chain(steps.get(stepId).node)[1] === id)) {
        go(own);
        return;
      }
    }
    flyFree(id);
  }

  function zoomOut() {
    const parent = parentOf(aim);
    if (parent) ascend(parent);
  }

  // Where to land in another path. The same non-overview step or its frame if the target has it; otherwise the
  // first target frame at or after the anchor, searched along the story and then along the rest of the source
  // path. The anchor is the step's frame, a topic's first frame in the source path, or for an overview/closing
  // step the next frame the source path would show. Only if the target's material lies wholly behind does it land
  // on the target's end (closing); otherwise it starts from the beginning.
  // Step ids of the previous deck still found in shared links.
  const LEGACY = new Map([['title', 'hub'], ['canvas', 'overview'],
    ...[1, 2, 3, 4, 5, 6].map((n) => [`c${n}`, `p${n}`])]);

  function locate(list, stepId, source = null, sourceIndex = -1) {
    stepId = LEGACY.get(stepId) || stepId;
    if (!steps.has(stepId) && !nodes[stepId]) return 0;  // unknown or mistyped ids start at the top
    const node = steps.get(stepId)?.node || stepId;
    const [container, frame] = chain(node);
    const whole = !container;  // overview or closing: use the position, not the (repeated) step id
    let found = whole && source ? -1 : list.indexOf(stepId);
    if (found >= 0) return found;
    if (frame && (found = list.indexOf(frame)) >= 0) return found;
    if (container && (found = list.findIndex((id) => chain(steps.get(id).node)[0] === container)) >= 0) return found;
    const sourceSteps = source && hasPath(source) ? data.paths[source].steps : [];
    const sourceFrames = source && hasPath(source) ? data.paths[source].frames : FRAMES;
    let anchor = frame || sourceFrames.find((key) => nodes[key]?.parent === container)
      || FRAMES.find((key) => nodes[key]?.parent === container);
    let afterEnd = false;
    // Before the source path has shown any frame (its opening overview), the target also starts at the top.
    if (whole && source && !sourceSteps.slice(0, Math.max(0, sourceIndex)).some((id) => steps.get(id).frame)) return 0;
    if (whole) {
      anchor = sourceSteps.slice(Math.max(0, sourceIndex)).map((id) => steps.get(id).frame).find(Boolean);
      if (!anchor && sourceFrames.length) {
        anchor = sourceFrames[sourceFrames.length - 1];
        afterEnd = true;
      }
    }
    if (!anchor) return 0;
    const pick = (order) => {
      const from = order.indexOf(anchor);
      return from < 0 ? null : order.slice(from + (afterEnd ? 1 : 0)).find((key) => list.includes(key));
    };
    // Story order first (so 5-1 comes before 5-2); the source path's own order covers paths that loop back to 0-2.
    const next = pick(FRAMES) || pick(sourceFrames);
    if (next) return list.indexOf(next);
    const at = FRAMES.indexOf(anchor);
    const behind = FRAMES.slice(0, at + 1).some((key) => list.includes(key))
      || sourceFrames.slice(0, Math.max(0, sourceFrames.indexOf(anchor))).some((key) => list.includes(key));
    if (!behind) return 0;
    if (list.includes('closing')) return list.lastIndexOf('closing');
    // A part without a closing step (2부, 3부) starts over at its first frame rather than at a dead end.
    const first = list.findIndex((id) => steps.get(id).kind === 'frame');
    return first >= 0 ? first : 0;
  }

  function setPath(name) {
    if (!hasPath(name)) return;
    // Exploring a node keeps that node; looking at the overview keeps the talk's position instead.
    const stepId = detour && aim !== 'overview' ? aim : current()?.id;
    const source = pathName;
    const sourceIndex = index;
    pathName = name;
    storage.set('fde-path', name);
    drawRoute();
    buildProgress();
    go(locate(order(), stepId, source, sourceIndex));
  }

  const isMajor = (id) => !DETAIL.has(steps.get(id).kind);

  function jumpFrame(direction) {
    const list = order();
    let position = index + direction;
    if (direction < 0) while (position > 0 && !isMajor(list[position])) position -= 1;
    else while (position < list.length - 1 && !isMajor(list[position])) position += 1;
    go(position);
  }

  function toggleOverview() {
    if (detour && aim === 'overview') go(index);
    else if (current().node === 'overview' && !detour) return;
    else flyFree('overview');
  }

  // ------------------------------------------------------------ rendering of chrome
  const escapeHtml = (text) => String(text).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const planetOf = (id) => {
    const [container] = chain(id);
    return container && container !== 'hub' ? Number(container.slice(1)) : 0;
  };
  const formatMinutes = (value) => `${Number.isInteger(value) ? value : value.toFixed(1)}분`;

  function crumbs(id) {
    const items = [{ id: 'overview', label: 'FDE 캔버스', planet: null }];
    const [container, frame, portal] = chain(id);
    if (container === 'hub') items.push({ id: 'hub', label: '표지', planet: 0 });
    else if (container) {
      const number = Number(container.slice(1));
      items.push({ id: container, label: `${number}. ${data.planets[number].name}`, planet: number });
    }
    if (frame) items.push({ id: frame, label: `${frame} ${data.frames[frame].label}`, planet: planetOf(frame) });
    if (portal) items.push({ id: portal, label: { diagram: '도식', code: '코드', data: '데이터' }[nodes[portal].kind], planet: planetOf(portal) });
    return items;
  }

  function render() {
    const list = order();
    const step = current();
    const shown = detour ? aim : step.node;
    body.dataset.step = detour ? '' : step.id;
    body.classList.toggle('detour', detour);
    $('.free-pill').hidden = !detour;
    $('.crumbs ol').innerHTML = crumbs(detour ? aim : step.kind === 'closing' ? 'overview' : step.node).map((item) =>
      `<li><button type="button" data-goto="${item.id}" class="${item.planet !== null ? `c${item.planet}` : ''}">`
      + `${item.planet !== null ? '<span class="dot"></span>' : ''}${escapeHtml(item.label)}</button></li>`).join('');
    $('.dock-count').textContent = detour ? '자유 탐색' : `${index + 1} / ${list.length}`;
    $('.dock-label').textContent = detour ? crumbs(shown).slice(-1)[0].label : step.label;
    const path = data.paths[pathName];
    $('.path-name').textContent = path.label;
    $('.path-minutes').textContent = `${path.minutes}분`;
    $$('.progress span').forEach((segment, position) => {
      segment.classList.toggle('done', position < index);
      segment.classList.toggle('now', position === index && !detour);
    });
    $$('.minutes[data-frame]').forEach((badge) => {
      badge.textContent = formatMinutes(data.frames[badge.dataset.frame].minutes * path.factor);
    });
    $$('.path-menu [data-path]').forEach((button) => button.setAttribute('aria-checked', String(button.dataset.path === pathName)));
    $('[data-action="notes"]').setAttribute('aria-pressed', String(notesOpen));
    renderNotes(detour ? { id: aim, kind: nodes[aim]?.kind, frame: chain(aim)[1] } : step);
  }

  function outline(keys, factor) {
    return '<ol class="notes-outline">' + keys.map((key) => {
      const frame = data.frames[key];
      return `<li><span>${key}</span>${escapeHtml(frame.title)}<em>${formatMinutes(frame.minutes * factor)}</em></li>`;
    }).join('') + '</ol>';
  }

  function renderNotes(step) {
    const panel = $('#notes');
    panel.hidden = !notesOpen;
    body.classList.toggle('notes-open', notesOpen);
    if (!notesOpen) return;
    const path = data.paths[pathName];
    const frameKey = step.frame || null;
    const frame = frameKey ? data.frames[frameKey] : null;
    const next = detour ? null : steps.get(order()[index + 1]);
    $('.notes-kicker').textContent = frame ? `FRAME ${frameKey} · ${frame.kicker}` : `${path.label} · ${path.minutes}분`;
    $('.notes-title').textContent = frame ? frame.title : (detour ? crumbs(aim).slice(-1)[0].label : step.label);
    $('.notes-next').textContent = detour ? '자유 탐색 중 · → 키로 경로에 복귀합니다' : next ? `다음 → ${next.label}` : '경로의 마지막 단계입니다';
    $('.clock-budget').textContent = frame ? `프레임 ${formatMinutes(frame.minutes * path.factor)} · 전체 ${path.minutes}분` : `전체 ${path.minutes}분`;
    $$('[data-tab]').forEach((button) => {
      const selected = button.dataset.tab === tab;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    $('.notes-body').setAttribute('aria-labelledby', `tab-${tab}`);
    const key = `${tab}|${frameKey || step.id}|${pathName}`;
    if (key === notesKey) return;
    notesKey = key;
    const target = $('.notes-body');
    target.scrollTop = 0;
    if (step.id === 'closing') {
      target.innerHTML = '<p class="notes-message">네 영역이 하나의 서비스로 남기는 결과</p><ol>'
        + [1, 2, 3, 4].map((n) => `<li><strong>${escapeHtml(data.planets[n].outcome)}</strong> · ${escapeHtml(data.planets[n].proof)}</li>`).join('')
        + '</ol><p>2부 현장 플레이북과 3부 규모 확장은 이 결과를 고객 현장에서 합의하고, 전 공장으로 넓히는 방법입니다.</p>';
    } else if (frame) {
      const points = frame.points.map((point) => `<li>${escapeHtml(point)}</li>`).join('');
      const header = tab === 'speaker' ? `<p class="notes-message">${escapeHtml(frame.message)}</p><ol>${points}</ol>` : '';
      const content = frame[tab] || '<p>이 프레임에 연결된 면접 질문이나 롤플레이가 없습니다.</p>';
      target.innerHTML = header + content;
    } else {
      const number = step.kind === 'planet' ? Number(step.id.slice(1)) : null;
      if (number) {
        const planet = data.planets[number];
        const keys = Object.keys(data.frames).filter((key) => data.frames[key].cluster === number);
        target.innerHTML = `<p class="notes-message">${escapeHtml(planet.tagline)}</p>${outline(keys, path.factor)}`;
      } else {
        target.innerHTML = `<p class="notes-message">${escapeHtml(path.description)}</p>${outline(path.frames, path.factor)}`;
      }
    }
  }

  function tickClock() {
    if (!notesOpen) return;
    const seconds = timerStart === null ? 0 : Math.floor((Date.now() - timerStart) / 1000);
    const element = $('.clock-elapsed');
    element.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    element.classList.toggle('over', seconds > data.paths[pathName].minutes * 60);
  }

  function buildProgress() {
    $('.progress').innerHTML = order().map((id) => {
      const step = steps.get(id);
      return `<span class="c${planetOf(step.node)}" title="${escapeHtml(step.label)}"></span>`;
    }).join('');
  }

  function buildMenu() {
    $('.path-menu ul').innerHTML = Object.entries(data.paths).map(([name, path]) =>
      `<li><button type="button" role="menuitemradio" data-path="${name}" aria-checked="false">`
      + `<strong>${escapeHtml(path.label)}</strong><span class="meta">${path.minutes}분 · ${path.frames.length}프레임</span>`
      + `<span class="desc">${escapeHtml(path.description)} · ${path.steps.length}단계</span></button></li>`).join('');
  }

  // The current path drawn over the overview, like Prezi's path view: one numbered stop per frame.
  function drawRoute() {
    const route = document.getElementById('route');
    if (!route) return;
    const keys = data.paths[pathName].frames;
    // Each stop sits on its frame's outer rim, away from the topic title in the planet's centre.
    const stops = keys.map((key) => {
      const frame = nodes[key];
      const parent = nodes[frame.parent];
      const dx = frame.x - parent.x;
      const dy = frame.y - parent.y;
      const length = Math.hypot(dx, dy) || 1;
      const push = frame.parent === 'hub' ? 0 : 1500;
      return { key, x: Math.round(frame.x + (dx / length) * push), y: Math.round(frame.y + (dy / length) * push) };
    });
    // Stops carry frame keys (not 1…N) so they never read as topic numbers; the line gives the order.
    const lines = document.getElementById('route-lines');
    if (lines) lines.innerHTML = `<polyline points="${stops.map((stop) => `${stop.x},${stop.y}`).join(' ')}"/>`;
    route.innerHTML = stops.map((stop) =>
      `<rect x="${stop.x - 1050}" y="${stop.y - 480}" width="2100" height="960" rx="480"/><text x="${stop.x}" y="${stop.y}">${stop.key}</text>`).join('');
  }

  function refit() {
    if (detour) apply(cam);
    else fly(fitNode(current().node), true);
    const [container, frame, portal] = chain(focusId);
    showSheet(portal || frame || container || 'overview');
  }

  // ------------------------------------------------------------ actions
  const menu = $('.path-menu');
  let helpOpener = null;
  const actions = {
    prev: () => (detour ? go(index) : go(index - 1)),
    next: () => go(index + 1),
    overview: toggleOverview,
    paths() {
      menu.hidden = !menu.hidden;
      $('[data-action="paths"]').setAttribute('aria-expanded', String(!menu.hidden));
      if (!menu.hidden) $('[aria-checked="true"]', menu)?.focus();
    },
    notes() {
      if (presenter) return;
      notesOpen = !notesOpen;
      storage.set('fde-notes', notesOpen ? 'open' : 'closed');
      notesKey = '';
      render();
      refit();
      tickClock();
    },
    presenter() {
      window.open(`${location.pathname}?presenter&from=${encodeURIComponent(tabId)}${location.hash}`,
        'fde-presenter', 'popup,width=1280,height=800');
    },
    fullscreen() {
      if (document.fullscreenElement) document.exitFullscreen?.();
      else root.requestFullscreen?.().catch(() => {});
    },
    theme() {
      const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      root.dataset.theme = dark ? 'light' : 'dark';
      storage.set('fde-theme', root.dataset.theme);
      channel?.postMessage({ type: 'theme', theme: root.dataset.theme });
    },
    help() {
      const help = $('#help');
      help.hidden = !help.hidden;
      $$('#stage, .bar, .dock, .progress, #notes, .path-menu, #sheet').forEach((element) => { element.inert = !help.hidden; });
      if (!help.hidden) {
        helpOpener = document.activeElement;
        $('button', help).focus();
      } else (helpOpener && helpOpener !== document.body ? helpOpener : stage).focus({ preventScroll: true });
    },
    'sheet-close': () => zoomOut(),
    'timer-reset'() {
      setTimer(Date.now());
      tickClock();
      channel?.postMessage({ type: 'timer', timerStart });
    },
  };

  function closeMenu() {
    if (menu.hidden) return;
    menu.hidden = true;
    $('[data-action="paths"]').setAttribute('aria-expanded', 'false');
  }

  document.addEventListener('click', (event) => {
    const control = event.target.closest('button[data-action], button[data-path], button[data-tab], button[data-goto], .progress span');
    if (!control) {
      if (!event.target.closest('.path-menu')) closeMenu();
      return;
    }
    if (event.detail > 0 && control.matches('button')) control.blur();
    if (control.dataset.action !== 'paths') closeMenu();
    if (control.dataset.action) actions[control.dataset.action]?.();
    else if (control.dataset.path) {
      setPath(control.dataset.path);
      if (event.detail === 0) $('[data-action="paths"]').focus();  // keyboard users return to the menu button
    }
    else if (control.dataset.tab) {
      tab = control.dataset.tab;
      render();
    } else if (control.dataset.goto) {
      const id = control.dataset.goto;
      if (id !== aim && contains(id, aim)) ascend(id);
      else if (id !== aim) goToNode(id);
    }
    else go([...control.parentNode.children].indexOf(control));
  });

  // Roving arrow keys inside a widget: the path menu (vertical) and the notes tabs (horizontal).
  function rove(event, items, keys) {
    const position = items.indexOf(event.target.closest('button'));
    const moves = { [keys[0]]: position - 1, [keys[1]]: position + 1, Home: 0, End: items.length - 1 };
    if (!(event.key in moves)) return false;
    const next = items[(moves[event.key] + items.length) % items.length];
    next.focus();
    event.preventDefault();
    return next;
  }

  document.addEventListener('keydown', (event) => {
    if (event.altKey || event.metaKey || event.ctrlKey) return;
    const help = $('#help');
    if (!help.hidden) {
      if (event.key === 'Escape' || event.key === '?') actions.help();
      if (event.key === 'Tab') {
        const items = $$('button', help);
        event.preventDefault();
        items[0].focus();
      }
      return;
    }
    if (!menu.hidden && (event.key === 'Escape' || event.key === 'Tab')) {
      closeMenu();
      if (event.key === 'Escape') $('[data-action="paths"]').focus();
      return;
    }
    if (event.target.closest('.path-menu')) {
      rove(event, $$('.path-menu [data-path]'), ['ArrowUp', 'ArrowDown']);
      return;
    }
    if (event.target.closest('[role="tablist"]')) {
      const tabButton = rove(event, $$('[data-tab]'), ['ArrowLeft', 'ArrowRight']);
      if (tabButton) {
        tab = tabButton.dataset.tab;
        render();
      }
      if (tabButton || event.key === 'ArrowLeft' || event.key === 'ArrowRight') return;
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
    else if (key === 'Escape') zoomOut();
    else if (key === 'o' || key === 'O') toggleOverview();
    else if (key === 'n' || key === 'N') actions.notes();
    else if (key === 'p' || key === 'P') actions.presenter();
    else if (key === 'f' || key === 'F') actions.fullscreen();
    else if (key === 't' || key === 'T') actions.theme();
    else if (key === '?') actions.help();
    else if (/^[0-6]$/.test(key)) goToNode(key === '0' ? 'hub' : `p${key}`);
    else handled = false;
    if (handled) event.preventDefault();
  });

  // ------------------------------------------------------------ pointer: click to zoom in, drag, wheel, pinch
  const pointers = new Map();
  let gesture = null;

  function settleFree() {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      if (!detour || world.classList.contains('moving')) return;
      const id = focusFromCamera();
      aim = id;
      if (id !== focusId) setFocus(id);
      render();
      const free = area();
      channel?.postMessage({ type: 'cam', aim, x: cam.x, y: cam.y, r: cam.r, span: free.w / cam.s });
    }, 140);
  }

  function enterFree() {
    cancelAnimationFrame(flight);
    world.classList.remove('moving');
    if (!detour) {
      detour = true;
      render();
    }
  }

  stage.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 1) {
      gesture = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, moved: false,
        type: event.pointerType, time: performance.now() };
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      gesture = { pinch: Math.hypot(a.x - b.x, a.y - b.y), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, moved: true };
      enterFree();
    }
  });

  stage.addEventListener('pointermove', (event) => {
    if (!pointers.has(event.pointerId) || !gesture) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (gesture.pinch) {
      if (pointers.size !== 2) return;  // after a pinch the remaining finger does nothing until lifted
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const anchor = toWorld(gesture.mid.x, gesture.mid.y);
      const s = clampScale(cam.s * (distance / gesture.pinch));
      const free = area();
      const ar = (cam.r * Math.PI) / 180;
      const dx = (mid.x - free.cx) / s;
      const dy = (mid.y - free.cy) / s;
      apply({ x: anchor.x - (dx * Math.cos(ar) - dy * Math.sin(ar)), y: anchor.y - (dx * Math.sin(ar) + dy * Math.cos(ar)), s, r: cam.r });
      gesture.pinch = distance;
      gesture.mid = mid;
      settleFree();
      return;
    }
    if (gesture.type === 'touch') return;  // one finger swipes between steps
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (!gesture.moved && Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) < 6) return;
    if (!gesture.moved) {
      gesture.moved = true;
      stage.setPointerCapture(event.pointerId);
      stage.classList.add('dragging');
      enterFree();
    }
    const a = (cam.r * Math.PI) / 180;
    const wx = dx / cam.s;
    const wy = dy / cam.s;
    apply({ x: cam.x - (wx * Math.cos(a) - wy * Math.sin(a)), y: cam.y - (wx * Math.sin(a) + wy * Math.cos(a)), s: cam.s, r: cam.r });
    gesture.x = event.clientX;
    gesture.y = event.clientY;
    settleFree();
  });

  function endPointer(event) {
    if (!pointers.has(event.pointerId)) return;
    pointers.delete(event.pointerId);
    stage.classList.remove('dragging');
    const start = gesture;
    if (pointers.size > 0) return;
    gesture = null;
    if (!start || start.pinch || event.type === 'pointercancel') return;
    if (start.type === 'touch') {
      const dx = event.clientX - start.startX;
      const dy = event.clientY - start.startY;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        if (dx < 0) actions.next();
        else actions.prev();
        return;
      }
      if (Math.hypot(dx, dy) > 10) return;
    }
    if (start.moved) return;
    if (event.target.closest('.portal.is-focus a[href]')) return;  // evidence links open normally
    const hit = event.target.closest('[data-node]');
    if (!hit) {
      zoomOut();
      return;
    }
    const id = hit.dataset.node;
    if (id === aim && DETAIL.has(nodes[id].kind)) zoomOut();
    else if (id !== aim && contains(id, aim)) ascend(id);
    else if (id !== aim) goToNode(id);
  }
  stage.addEventListener('pointerup', endPointer);
  stage.addEventListener('pointercancel', endPointer);

  function clampScale(s) {
    const min = fitNode('overview').s * 0.55;
    return Math.min(6, Math.max(min, s));
  }

  stage.addEventListener('wheel', (event) => {
    event.preventDefault();
    enterFree();
    const unit = event.deltaMode === 1 ? 16 : 1;
    if (!event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
      const a = (cam.r * Math.PI) / 180;
      const wx = (event.deltaX * unit) / cam.s;
      apply({ x: cam.x + wx * Math.cos(a), y: cam.y + wx * Math.sin(a), s: cam.s, r: cam.r });
      settleFree();
      return;
    }
    const factor = Math.exp(-event.deltaY * unit * (event.ctrlKey ? 0.012 : 0.0016));
    const anchor = toWorld(event.clientX, event.clientY);
    const s = clampScale(cam.s * factor);
    const free = area();
    const a = (cam.r * Math.PI) / 180;
    const dx = (event.clientX - free.cx) / s;
    const dy = (event.clientY - free.cy) / s;
    apply({ x: anchor.x - (dx * Math.cos(a) - dy * Math.sin(a)), y: anchor.y - (dx * Math.sin(a) + dy * Math.cos(a)), s, r: cam.r });
    settleFree();
  }, { passive: false });

  // Hide the chrome while presenting full screen and the pointer rests.
  let idleTimer = 0;
  const wake = () => {
    body.classList.remove('idle');
    clearTimeout(idleTimer);
    if (document.fullscreenElement && !presenter) idleTimer = setTimeout(() => body.classList.add('idle'), 2600);
  };
  ['pointermove', 'keydown'].forEach((type) => document.addEventListener(type, wake, { passive: true }));
  document.addEventListener('fullscreenchange', () => {
    wake();
    setTimeout(refit, 80);
  });

  // ------------------------------------------------------------ presenter window sync
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('fde-atlas') : null;
  if (!channel) $('[data-action="presenter"]')?.setAttribute('hidden', '');
  // A presenter asks once ('hello'), collects the replies for a moment and adopts ONE window's step and clock:
  // the window that opened it (from=…), else the most recently driven one (ties: the first reply).
  const helloId = presenter ? tabId : null;
  const replies = [];
  let movedSinceHello = false;
  function adoptReply() {
    if (movedSinceHello) return;  // someone already drove the talk; their 'nav' is newer than any reply
    const valid = replies.filter((reply) => hasPath(reply.path) && Number.isInteger(reply.index));
    const own = location.hash.match(/^#(\w+)~([\w.-]+)/);
    const sameStep = (reply) => Boolean(own) && reply.path === own[1] && data.paths[reply.path].steps[reply.index] === own[2];
    // Rank: the opener, then a window on the presenter's own step, then the most recently driven window.
    const rank = (reply) => [opener && reply.from === opener ? 1 : 0, sameStep(reply) ? 1 : 0, reply.lastNavigated || 0];
    const better = (a, b) => {
      const [ra, rb] = [rank(a), rank(b)];
      for (let i = 0; i < ra.length; i += 1) if (ra[i] !== rb[i]) return ra[i] > rb[i];
      return String(a.from) < String(b.from);  // stable tie-break, independent of arrival order
    };
    const chosen = valid.reduce((best, reply) => (!best || better(reply, best) ? reply : best), null);
    if (!chosen) return;
    if (chosen.timerStart) setTimer(chosen.timerStart);
    remote = true;
    if (chosen.path !== pathName) {
      pathName = chosen.path;
      drawRoute();
      buildProgress();
    }
    go(chosen.index);
    remote = false;
    tickClock();
  }
  channel?.addEventListener('message', ({ data: message }) => {
    if (message.type === 'hello' && !presenter) {
      // Reply only to the presenter that asked; other audience windows ignore 'state'.
      channel.postMessage({ type: 'state', to: message.id, from: tabId, path: pathName, index, timerStart, lastNavigated });
    } else if (message.type === 'state') {
      if (message.to === helloId) replies.push(message);
    } else if (message.type === 'free' && nodes[message.aim]) {
      remote = true;
      flyFree(message.aim);
      remote = false;
    } else if (message.type === 'cam' && nodes[message.aim] && [message.x, message.y, message.r, message.span].every(Number.isFinite)) {
      remote = true;
      enterFree();
      apply({ x: message.x, y: message.y, r: message.r, s: area().w / message.span });
      aim = message.aim;
      if (aim !== focusId) setFocus(aim);
      render();
      remote = false;
    } else if (message.type === 'nav' && hasPath(message.path) && Number.isInteger(message.index)) {
      remote = true;
      if (message.path !== pathName) {
        pathName = message.path;
        drawRoute();
        buildProgress();
      }
      // The window that drives the talk owns its clock; followers take it as is.
      if (message.timerStart) setTimer(message.timerStart);
      go(message.index);
      remote = false;
    } else if (message.type === 'theme' && (message.theme === 'dark' || message.theme === 'light')) {
      root.dataset.theme = message.theme;
    } else if (message.type === 'timer') {
      setTimer(message.timerStart);
      tickClock();
    }
  });

  // ------------------------------------------------------------ start
  function fromHash() {
    const match = location.hash.match(/^#(\w+)~([\w.-]+)(?:~(\d+))?$/) || location.hash.match(/^#\/(\w+)\/([\w.-]+)$/);
    if (!match || !hasPath(match[1])) return false;
    pathName = match[1];
    drawRoute();
    buildProgress();
    const list = order();
    const position = match[3] !== undefined && list[Number(match[3])] === match[2] ? Number(match[3]) : locate(list, match[2]);
    go(position, { instant: true, silent: true });
    return true;
  }

  if (presenter) body.classList.add('presenter');
  buildMenu();
  drawRoute();
  buildProgress();
  const fromLink = fromHash();
  if (!fromLink) {
    // Opening shot: start far out and slightly turned, then fly in to the overview like a Prezi intro.
    const overview = fitNode('overview');
    if (!reducedMotion.matches) apply({ ...overview, s: overview.s * 0.3, r: -14 });
    go(0, { silent: true });
  }
  addEventListener('hashchange', () => {
    if (location.hash !== hashFor(index)) fromHash();
  });
  addEventListener('resize', () => requestAnimationFrame(refit));
  narrowQuery.addEventListener?.('change', () => requestAnimationFrame(refit));
  // Fonts never move camera targets (they come from the build); only refit if nothing is flying.
  document.fonts?.ready.then(() => { if (!world.classList.contains('moving')) refit(); });
  if (presenter && channel) {
    channel.postMessage({ type: 'hello', id: helloId });
    setTimeout(adoptReply, 600);
  }
  setInterval(tickClock, 1000);
  tickClock();
  body.classList.add('ready');
  stage.focus({ preventScroll: true });
})();
