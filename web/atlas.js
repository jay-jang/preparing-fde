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
  let flight = null;         // the camera's current flight, if any (see fly)
  let wheelTo = null;        // the scale a wheel or pinch gesture is easing to, around a screen point
  let frameRequest = 0;
  let lastTick = 0;
  let closing = 0;           // 0-1: how far the closing step's message has replaced the title
  let titleAway = 0;         // 0-1: how far the hub title has left ahead of the message (kept across interruptions)
  let lastHeld = null;       // the subject as last drawn (reveal), where the next flight's hand-over starts
  let letGo = null;          // a flight stopped by free input: its subject eases back to the camera's (reveal)
  let stopDir = 0;           // zoom direction of the flight free input stopped (1 in, −1 out)
  let pin = null;            // at rest after free input: topics eased fully open or shut (reveal)
  let detour = false;
  let focusId = 'overview';  // the node the focus classes (links, depth, sheet) describe
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

  const PAD = { overview: 1.0, closing: 1.07, hub: 1.03, planet: 1.03, frame: 1.0, diagram: 1.035, code: 1.035, data: 1.035 };
  const fitScale = (box, pad, free) => Math.min(free.w / (box.w * pad), free.h / (box.h * pad));

  function fitNode(id) {
    const node = nodes[id];
    const box = node.kind === 'frame' ? FRAME_BOX : DETAIL.has(node.kind) ? PORTAL_BOX : node;
    return { x: node.x, y: node.y, s: fitScale(box, PAD[node.kind] || 1.03, area()), r: node.rot || 0 };
  }

  // #world is never promoted to its own compositor layer. Promoting it for each flight (will-change) made
  // Chrome re-layer and re-raster the whole 60,000px canvas at take-off and landing, so the screen froze,
  // jumped and turned from blurry to sharp on arrival. Unpromoted, every frame is drawn sharp at its own scale.
  function apply(next) {
    cam = next;
    const free = area();
    world.style.transform =
      `translate(${free.cx}px,${free.cy}px) rotate(${-cam.r}deg) scale(${cam.s}) translate(${-cam.x}px,${-cam.y}px)`;
    // A dotted plane far behind the canvas: it scales and slides at a fraction of the camera (parallax),
    // spreading out from the centre of the free area. The plane starts 30% of the stage above and left of it.
    const base = fitScale(nodes.overview, PAD.overview, free);
    const depth = Math.pow(cam.s / base, 0.3);
    const grid = 38 * depth;
    const shift = 0.55 * base * depth;
    const wrap = (value) => ((value % grid) + grid) % grid;
    backdrop.style.backgroundSize = `${grid}px ${grid}px`;
    backdrop.style.backgroundPosition = `${wrap(free.cx + 0.3 * innerWidth - cam.x * shift)}px `
      + `${wrap(free.cy + 0.3 * innerHeight - cam.y * shift)}px`;
    backdrop.style.transform = `rotate(${-cam.r}deg)`;
    reveal(free);
  }

  // The camera that keeps the world point under (px, py) in place while the scale becomes s.
  function zoomAround(px, py, s) {
    const anchor = toWorld(px, py);
    const free = area();
    const a = (cam.r * Math.PI) / 180;
    const dx = (px - free.cx) / s;
    const dy = (py - free.cy) / s;
    return { x: anchor.x - (dx * Math.cos(a) - dy * Math.sin(a)), y: anchor.y - (dx * Math.sin(a) + dy * Math.cos(a)), s, r: cam.r };
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

  // Progress along a flight with start speed v0 (in path lengths per flight time) and a soft landing:
  // e(t) = 1 − (1 − t)³(1 + (3 − v0)t). Motion starts at once, peaks at about a third of the time at 1.7 times
  // the mean speed (the old cubic in-out sat still for the first tenth and peaked at 3 times) and settles gently.
  const ease = (t, v0) => 1 - (1 - t) ** 3 * (1 + (3 - v0) * t);
  const REST_SPEED = 0.35;
  const KICK_TIME = 200;  // ms over which the velocity taken over from an interrupted flight fades out

  // Where flight f has the camera at `time`: the eased point on its path, plus the part of the velocity taken
  // over from an interrupted flight that its own path does not cover (kick), fading out long before landing.
  // w is the width of world in view.
  function flightAt(f, time) {
    const t = Math.min(1, Math.max(0, (time - f.started) / f.duration));
    const e = ease(t, f.v0);
    let [x, y, w] = f.path.at(e);
    let r = f.from.r + f.turn * e;
    if (f.kick) {
      const since = Math.max(0, time - f.started);
      const k = since * Math.exp(-since / KICK_TIME) * (1 - t) ** 2;
      x += f.kick[0] * k * w;
      y += f.kick[1] * k * w;
      w *= Math.exp(f.kick[2] * k);
      r += f.kick[3] * k;
    }
    return { t, e, x, y, w, r };
  }

  // Velocity of a flight per ms: pan in widths of view, zoom in e-folds, turn in degrees.
  function velocity(f, time) {
    const a = flightAt(f, time - 1);
    const b = flightAt(f, time + 1);
    return [(b.x - a.x) / (2 * a.w), (b.y - a.y) / (2 * a.w), Math.log(b.w / a.w) / 2, (b.r - a.r) / 2];
  }

  // An interrupted flight hands its velocity on, so quick key presses chain into one motion instead of stopping
  // and starting. The part along the new path becomes its start speed (negative when turned back: the camera
  // swings round instead of reversing within one frame); the rest is the kick that fades out (flightAt).
  function carry(previous, now, path, turn, duration) {
    if (!previous || now >= previous.started + previous.duration) return { v0: REST_SPEED, kick: null };
    const v = velocity(previous, now);
    const [x0, y0, w0] = path.at(0);
    const [x1, y1, w1] = path.at(1e-3);
    const tangent = [(x1 - x0) / w0 / 1e-3, (y1 - y0) / w0 / 1e-3, Math.log(w1 / w0) / 1e-3];
    const norm = tangent.reduce((sum, value) => sum + value * value, 0);
    const along = tangent.reduce((sum, value, i) => sum + value * v[i], 0) / norm;
    const v0 = Number.isFinite(along) ? Math.min(2.4, Math.max(-2.4, along * duration)) : REST_SPEED;
    const kick = [...tangent, turn].map((value, i) => v[i] - (value * v0) / duration);
    return { v0, kick: kick.every(Number.isFinite) ? kick : null };
  }

  // Fly the camera to ``target``. Short hops take about 0.65 s and the longest moves 1.4 s (before: 0.72-2.6 s);
  // what each level shows follows the camera on every frame (reveal), so nothing is left to fade in afterwards.
  function fly(target, instant = false) {
    const now = performance.now();
    const previous = flight;
    // What the flight or hand-back this one takes over from still holds down eases out (capsAt), never at once.
    const hand = capsAt(now);
    const holding = Boolean(hand.dimCap || hand.coverCap || hand.titleCap != null);
    flight = null;
    wheelTo = null;
    letGo = null;
    pin = null;
    stopDir = 0;
    clearTimeout(quietTimer);  // a pinch's settle under reduced motion no longer applies
    quietFrom = 0;
    if (!cam || instant || reducedMotion.matches) {
      closing = closingGoal();
      titleAway = 0;
      apply(target);
      world.classList.remove('moving');
      return;
    }
    const from = cam;
    const free = area();
    const path = zoomPath([from.x, from.y, free.w / from.s], [target.x, target.y, free.w / target.s]);
    const turn = ((((target.r - from.r) % 360) + 540) % 360) - 180;
    if (!Number.isFinite(path.length) || !path.at(0.5).every(Number.isFinite)) {
      fly(target, true);
      return;
    }
    if (path.length < 1e-3 && Math.abs(turn) < 0.05) {
      // Already there (a step back after a one-pixel pan): the closing blend still runs on wall time (tick).
      apply(target);
      world.classList.remove('moving');
      if (holding) letGo = { from: lastHeld, at: now, ...hand };
      schedule();
      return;
    }
    const duration = Math.min(1400, Math.max(600, 240 + 600 * Math.sqrt(path.length) + 3 * Math.abs(turn)));
    const heldTo = subjectAt(target, free);
    // A flight cut short goes on from the subject as it was drawn; one from rest takes off from what the camera
    // gives any effect (subjectAt), so the hub the overview is centred on does not open on the way to a frame.
    const chained = previous && now < previous.started + previous.duration;
    flight = { path, from, target, turn, duration, started: now, w: free.w, ...carry(previous, now, path, turn, duration),
      closingFrom: closing, closingTo: closingGoal(), awayFrom: titleAway, heldTo,
      heldFrom: chained ? lastHeld : subjectAt(cam, free, lastHeld || holdsAt(cam), true),
      // Leaving the closing view for somewhere outside the hub, the message stays until the camera is on its way.
      leaving: heldTo.topics[layers.containers.indexOf(layers.hub)] < 0.5, hand: holding ? { at: now, ...hand } : null };
    const atTarget = topicDims(heldTo.topics, Math.log2(target.s / fitScale(nodes.overview, PAD.overview, free)), flight.closingTo);
    if (flight.closingTo !== flight.closingFrom) {
      // Into or out of the closing view a topic is never drawn brighter than at both ends: the subject letting go
      // and the later parts dimming run at different times and would otherwise lift p5 and p6 in between.
      flight.dimCap = atTarget.map((v, i) => Math.max(v, shown.get(layers.containers[i].element) ?? 1));
    }
    if ((titleAway > 0 && flight.closingTo <= flight.closingFrom) || hand.titleCap != null) {
      // A flight that cancels the way into the closing view brings the hub title back at once, but never brighter
      // than it is drawn at take-off or at the target (the hub stepped back), so it neither relights nor goes blank.
      // So does a flight that takes over from one that held the title down.
      const hub = layers.containers.indexOf(layers.hub);
      const inner = 1 - 0.78 * smooth(Math.log2(target.s / fitScale(nodes.hub, PAD.hub, free)), 1, 2);
      flight.titleCap = Math.max(atTarget[hub] * inner * (1 - smooth(flight.closingTo, 0, 0.65)),
        (shown.get(layers.title) ?? 1) * (shown.get(layers.hub.element) ?? 1));
      // Where the hub steps back at the target, its frame labels come back with the title (reveal); a flight that
      // takes over from one that held them otherwise lets them go from where they are.
      const label = layers.frames.find((frame) => frame.container.hub)?.cover;
      flight.labelHold = atTarget[hub] < 0.5 ? true : chained && previous.labelHold != null ? shown.get(label) ?? 0 : null;
      // Nor is the cover of the topic it heads for lit up on the way before it opens.
      flight.coverCap = layers.containers.map((container, i) => {
        if (!container.cover || heldTo.topics[i] < 0.5) return 1;
        const open = heldTo.topics[i] * smooth(Math.log2(target.s / fitScale(container.node, PAD[container.node.kind], free)), -0.9, -0.3);
        return Math.max(shown.get(container.cover) ?? 1, (1 - smooth(open, 0, 0.5)) * atTarget[i]);
      });
    }
    world.classList.add('moving');  // the landing signal for tools and tests; no style depends on it
    schedule();
  }

  function schedule() {
    if (!frameRequest) frameRequest = requestAnimationFrame(tick);
  }

  // The ceilings that hold topics, covers and the hub title down at `time`: a flight's own (fly), and those of
  // what it or free input took over from (hand, letGo), which ease up to nothing over 450 ms. Whatever takes over
  // next starts from them, so a second key, a wheel notch or a drag does not bring anything back in one frame.
  function capsAt(time) {
    const from = flight ? flight.hand : letGo;
    const k = from ? smooth(time - from.at, 0, 450) : 1;
    const hand = k < 1 ? from : null;
    const up = (v) => (v == null ? null : mix(v, 1, k));
    const low = (a, b) => (a == null ? b : b == null ? a : Math.min(a, b));
    const each = (own, held) => (own || held ? layers.containers.map((_, i) => low(own?.[i], up(held?.[i])) ?? 1) : undefined);
    return { dimCap: each(flight?.dimCap, hand?.dimCap), coverCap: each(flight?.coverCap, hand?.coverCap),
      titleCap: low(flight?.titleCap, up(hand?.titleCap)) ?? undefined };
  }

  // The stiffness (per ms) of a glide over `gap` (log scale) that starts at speed v. A critically damped spring
  // speeds up only while w > 2v / gap, so it takes that, within 1/120 (about 0.6 s to arrive from rest) and
  // 1/70: a zoom that had almost stopped is not thrown into a second, faster one.
  const glideRate = (gap, v) => Math.min(1 / 70, Math.max(1 / 120, (2 * Math.max(0, v * Math.sign(gap))) / Math.abs(gap || 1)));

  // One animation loop drives flights, eased wheel zoom and the closing blend.
  function tick(now) {
    frameRequest = 0;
    // Easing below runs on wall time even when frames are slow; the cap only guards against a backgrounded tab.
    const dt = lastTick ? Math.min(250, Math.max(0, now - lastTick)) : 16;
    lastTick = now;
    if (flight) {
      const f = flight;
      const { t, e, x, y, w, r } = flightAt(f, now);
      f.e = e;
      // The closing message arrives over the second half of the flight that zooms out to it. It leaves over the
      // first half of a flight back into the hub, so it never covers the frames the camera closes in on, and over
      // the second half of a flight elsewhere, so the title and frames do not come back only to step back again.
      // A blend already under way when the flight starts goes on from where it is.
      closing = mix(f.closingFrom, f.closingTo, f.closingTo > f.closingFrom ? smooth(e, 0.5 * (1 - f.closingFrom), 1)
        : f.leaving ? smooth(e, 0.5, 1) : smooth(e, 0, 0.45));
      // The hub title leaves over the first half of a flight into the closing view. A flight cut short or
      // restarted takes it on from where it is, so it never jumps back.
      titleAway = f.closingTo > f.closingFrom ? mix(f.awayFrom, 1, smooth(e, 0, 0.5)) : f.awayFrom * (1 - smooth(e, 0, 0.45));
      if (t < 1) {
        apply({ x, y, s: f.w / w, r });
      } else {
        flight = null;
        closing = f.closingTo;
        titleAway = 0;  // at rest the closing blend alone decides
        apply(f.target);
        world.classList.remove('moving');
      }
    } else {
      // Outside a flight the closing blend and the title run on wall time, under a wheel zoom too, so they are
      // done by the time the camera stops.
      const step = reducedMotion.matches ? 1 : dt / 350;
      const goal = closingGoal();
      if (closing !== goal || titleAway > 0 || letGo || (pin && !pin.done)) {
        closing = goal > closing ? Math.min(goal, closing + step) : Math.max(goal, closing - step);
        titleAway = Math.max(0, titleAway - step);
        if (!wheelTo) reveal(area());
      }
      if (wheelTo) {
        // Each notch moves the goal; the camera follows exponentially, so a burst of notches is one smooth zoom.
        // Once the wheel is quiet, a goal halfway through a swap of detail moves on to where the swap is done:
        // the way the gesture went, or to the nearer end after a nudge (a slow scroll is not taken further).
        // A pinch streams a step every frame, so 48 ms without one means the fingers have stopped.
        if (!wheelTo.settled && now - wheelTo.at > (wheelTo.pinch ? 48 : 120)) {
          wheelTo.settled = true;
          settleWheel();
        }
        // The glide on to the end of a swap is a critically damped spring that starts at the zoom's own speed,
        // so it reads as the zoom running out, never as a second, faster zoom. A notch or pinch step during the
        // glide rides on top of it (boost) at the pace it has at rest.
        const gap = Math.log(wheelTo.s / cam.s);
        let move;
        if (wheelTo.glide) {
          const w = wheelTo.w;
          const b = (wheelTo.boost || 0) + (wheelTo.boostPinch || 0);
          const x = -(gap - b);
          const v = wheelTo.v;
          const k = Math.exp(-w * dt);
          move = (x + (v + w * x) * dt) * k - x;
          wheelTo.v = (v - w * (v + w * x) * dt) * k;
          // Notches and pinch steps each drain at their own pace, whichever came last.
          const notch = (wheelTo.boost || 0) * (1 - Math.exp(-dt / 80));
          const pinch = (wheelTo.boostPinch || 0) * (1 - Math.exp(-dt / 40));
          wheelTo.boost = (wheelTo.boost || 0) - notch;
          wheelTo.boostPinch = (wheelTo.boostPinch || 0) - pinch;
          move += notch + pinch;
        } else {
          move = gap * (1 - Math.exp(-dt / wheelTo.tau));
          // A pinch headed for the end of a swap does not slow down in the quiet before its settle (end), so the
          // glide after it starts at the fingers' speed instead of dipping and speeding up again. While steps come,
          // the camera follows the fingers, slowing as they slow.
          if (wheelTo.end && !wheelTo.settled && now - wheelTo.at > 20) {
            const room = Math.log(wheelTo.end / cam.s);
            if (room * wheelTo.dir > 0) {
              const hold = Math.min(Math.max(0, wheelTo.v * wheelTo.dir) * dt, Math.abs(room) * (1 - Math.exp(-dt / wheelTo.tau)));
              if (move * wheelTo.dir < hold) move = wheelTo.dir * hold;
            }
          }
          wheelTo.v = dt ? move / dt : 0;
        }
        const done = wheelTo.settled && Math.abs(gap) < 0.0007 && Math.abs(wheelTo.v * 16) < 0.0007;
        pinOn(wheelTo, now);
        apply(zoomAround(wheelTo.x, wheelTo.y, done ? wheelTo.s : cam.s * Math.exp(move)));
        // At rest the pin (settleFree) carries the zoom's easing on from where it is.
        if (done && wheelTo.pinTo) pin = { from: wheelTo.pinFrom, to: wheelTo.pinTo, at: wheelTo.pinAt, done: false, cam };
        if (done) {
          wheelTo = null;
          reveal(area());  // bodies dissolved on the way are hidden now that the camera rests (show)
          world.classList.remove('moving');
          settleFree();
        }
      }
    }
    if (flight || wheelTo || closing !== closingGoal() || titleAway > 0 || letGo || (pin && !pin.done)) schedule();
    else lastTick = 0;
  }

  // ------------------------------------------------------------ level of detail
  // What the canvas shows is a function of the camera alone. Each layer fades with its zoom level (log2 of the
  // scale over the scale that frames a node of its kind: 0 at that framing, −1 at half its size) and dims by how
  // far the screen centre is from it. Keys, the wheel and a pinch therefore reveal the same detail at the same
  // size, nothing fades on a timer after the camera stops, and an interrupted flight leaves nothing half done.
  // Only the closing step is a state of its own, blended over the flight into or out of it (tick).
  const smooth = (x, a, b) => {
    const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  };
  const mix = (a, b, t) => a + (b - a) * t;
  const closingGoal = () => (!detour && current()?.id === 'closing' ? 1 : 0);
  const shown = new WeakMap();

  // The swaps of detail that a free zoom must not come to rest in (zoom levels as in reveal): a frame's label
  // and body dissolve over −1.6…−1 of the frame framing, a topic's cover and disc over −0.9…−0.3 of its own.
  const SWAPS = { frame: [-1.6, -1], container: [-0.9, -0.3] };

  // How much a camera centred on (x, y) is on a node: a circle lets go over the outer 40% of its radius, a
  // window just past its edges.
  const inCircle = (node, x, y) => 1 - smooth(Math.hypot(node.x - x, node.y - y) / (node.w / 2), 0.6, 1);
  function inBox(node, x, y) {
    const a = (-(node.rot || 0) * Math.PI) / 180;
    const dx = x - node.x;
    const dy = y - node.y;
    const u = Math.abs(dx * Math.cos(a) - dy * Math.sin(a)) / (node.w / 2);
    const v = Math.abs(dx * Math.sin(a) + dy * Math.cos(a)) / (node.h / 2);
    return 1 - smooth(Math.max(u, v), 0.7, 1.15);
  }

  // The subject of a camera centred on `point`: how much it holds each frame and each topic. A node holds the
  // camera when the centre is on it or on anything inside it, so the chain topic > frame > window under the
  // camera is always at full strength: frames sit at 0.64 of a topic's radius and windows reach 0.83, where
  // the topic's own circle has already let go.
  function holdsAt({ x, y }) {
    const frames = layers.frames.map((frame) =>
      Math.max(inCircle(frame.node, x, y), ...frame.portals.map((portal) => inBox(portal.node, x, y))));
    const topics = layers.containers.map((container) => Math.max(inCircle(container.node, x, y),
      ...layers.frames.map((frame, j) => (frame.container === container ? frames[j] : 0))));
    return { frames, topics };
  }

  // The subject a flight lands on: the holds of its target counted only as far as the target's zoom opens each
  // topic, so the overview and closing views (centred on the hub but too far out to open it) hold nothing. A
  // flight takes off (from) with every hold the camera already draws: once past the overview the others step
  // back by them (reveal), so there they all count.
  function subjectAt(camera, free, held = holdsAt(camera), from = false) {
    const { frames, topics } = held;
    const drawn = from ? smooth(Math.log2(camera.s / fitScale(nodes.overview, PAD.overview, free)), 0.1, 0.35) * (1 - closing) : 0;
    const open = layers.containers.map((container) => Math.max(drawn,
      smooth(Math.log2(camera.s / fitScale(container.node, PAD[container.node.kind], free)), -0.9, -0.3)));
    return { frames: frames.map((v, j) => v * open[layers.containers.indexOf(layers.frames[j].container)]),
      topics: topics.map((v, i) => v * open[i]) };
  }
  // How strongly each topic is drawn (reveal) for a subject, a zoom level over the overview and a closing blend.
  function topicDims(focus, zOverview, closingV) {
    const backOff = 0.72 * smooth(zOverview, 0.35, 1) * (1 - closingV);
    const [lead, runnerUp] = [...focus].sort((a, b) => b - a);
    return layers.containers.map((container, i) => (1 - backOff * Math.max(0, (focus[i] >= lead ? runnerUp ?? 0 : lead) - focus[i]))
      * (container.later ? 1 - 0.75 * closingV : 1));
  }
  // A wheel zoom, pinch glide or settle glide knows where it will come to rest: its topics ease from how they
  // are drawn to fully open or shut there (as the rest pin does, settleFree) over 200 ms, rather than following
  // the moving screen centre, which can cross a frame's window in a frame or two and open a topic for that long.
  // While steps still come, a topic is taken up only once the goal has stayed on it for 100 ms and, zooming in,
  // only if it also holds the point under the pointer that the centre closes in on (pinOn): a burst whose goal
  // only passes over a topic does not start to open it. Zooming out, a topic is let go only once the camera
  // itself has left it or the zoom has settled, so notches out and back in do not shut the topic it never left.
  function aimPin(next, previous = null) {
    const aimTo = holdsAt(zoomAround(next.x, next.y, next.rest || next.s)).topics.map((v) => (v >= 0.5 ? 1 : 0));
    const now = performance.now();
    if (previous?.pinTo) Object.assign(next, { pinTo: previous.pinTo, pinFrom: previous.pinFrom, pinAt: previous.pinAt });
    else {
      const drawn = (lastHeld || holdsAt(cam)).topics;
      Object.assign(next, { pinTo: drawn.map((v) => (v >= 0.5 ? 1 : 0)), pinFrom: drawn.slice(), pinAt: now });
    }
    next.aimTo = aimTo;
    next.toward = next.dir > 0 ? holdsAt(toWorld(next.x, next.y)).topics : null;
    next.aimSince = aimTo.map((v, i) => (v && previous?.aimTo?.[i] ? previous.aimSince[i] : now));
    pinOn(next, now);
    return next;
  }
  function pinOn(wheel, now) {
    const here = holdsAt(cam).topics;
    const to = wheel.aimTo.map((v, i) => (v ? wheel.pinTo[i] || wheel.settled
      || (now - wheel.aimSince[i] >= 100 && (!wheel.toward || wheel.toward[i] >= 0.5))
      : wheel.pinTo[i] && !wheel.settled && wheel.dir < 0 && here[i] >= 0.5) ? 1 : 0);
    if (to.some((v, i) => v !== wheel.pinTo[i])) {
      // A topic let go while its hold is not drawn yet (it is not open and, this far out, steps nobody back:
      // the overview centred on the hub) goes at once, so its 200 ms ease does not dim the others as the zoom
      // reaches the band where they step back.
      const free = area();
      const quiet = smooth(Math.log2(cam.s / fitScale(nodes.overview, PAD.overview, free)), 0.35, 1) < 0.02;
      const pinFrom = (lastHeld || holdsAt(cam)).topics.map((v, i) => (quiet && !to[i] && wheel.pinTo[i]
        && v * smooth(Math.log2(cam.s / fitScale(layers.containers[i].node, PAD[layers.containers[i].node.kind], free)), -0.9, -0.3) < 0.02 ? 0 : v));
      Object.assign(wheel, { pinTo: to, pinFrom, pinAt: now });
    }
  }
  // From subject a to subject b at progress k: each hold rises over `rise` and falls over `fall` (both 0-1 of k).
  function blend(a, b, k, rise = [0, 1], fall = rise) {
    const step = (from, to) => mix(from, to, to > from ? smooth(k, ...rise) : smooth(k, ...fall));
    return { frames: a.frames.map((v, j) => step(v, b.frames[j])), topics: a.topics.map((v, i) => step(v, b.topics[i])) };
  }

  // Where a quiet wheel zoom rests (tick): its goal or, halfway through a swap, the end of the swap the gesture
  // went to (the nearer one after a nudge). A sideways scroll that moves the centre works it out again.
  function settleWheel() {
    const net = Math.log2(wheelTo.raw / wheelTo.from);
    const s = settleScale(wheelTo.raw, wheelTo.x, wheelTo.y, Math.abs(net) < 0.15 ? 0 : wheelTo.dir);
    if (s !== wheelTo.s) {
      wheelTo.s = s;
      // A spring that would overshoot (the camera already fast for the gap left) is not started: the follower
      // eases the rest of the way.
      const gap = Math.log(s / cam.s);
      const w = glideRate(gap, wheelTo.v);
      if (w * Math.abs(gap) >= Math.abs(wheelTo.v) || wheelTo.v * gap <= 0) {
        wheelTo.glide = true;
        wheelTo.w = w;
      }
    }
    wheelTo.rest = wheelTo.s;
    aimPin(wheelTo, wheelTo);  // where it rests is settled now: its topics are taken up at once
  }

  // The scale a free zoom settles on: s itself or, when s would rest halfway through a swap, the end of the swap
  // the zoom was heading for (dir 1 in, −1 out, 0 the nearer end), so the next notch always passes through it.
  function settleScale(s, px, py, dir) {
    const free = area();
    const through = (fit, [lo, hi]) => {
      const z = Math.log2(s / fit);
      if (z <= lo || z >= hi) return null;
      return fit * 2 ** (dir < 0 ? lo : dir > 0 ? hi : z < (lo + hi) / 2 ? lo : hi);
    };
    let settled = through(fitScale(FRAME_BOX, PAD.frame, free), SWAPS.frame);
    if (settled === null) {
      const at = zoomAround(px, py, s);
      const id = CONTAINERS.find((c) => Math.hypot(nodes[c].x - at.x, nodes[c].y - at.y) < nodes[c].w / 2);
      if (id) settled = through(fitScale(nodes[id], PAD[nodes[id].kind], free), SWAPS.container);
    }
    return settled === null ? s : clampScale(settled);
  }

  // Opacity is written only when it changes; at 0 the element also leaves painting and hit testing. Visibility
  // is inherited, so hiding a frame body restyles its whole subtree: with `lazy` (the camera is moving) a body
  // that dissolves keeps it and is hidden once the camera rests, rather than several at once mid-flight, and a
  // hidden body that the camera only grazes (under 5%) is not shown.
  const hidden = new WeakMap();
  function show(element, value, lazy = false) {
    if (!element) return;
    const v = value < 0.004 ? 0 : value > 0.996 ? 1 : Math.round(value * 1000) / 1000;
    const hide = v === 0;
    if ((hidden.get(element) ?? false) !== hide && !(lazy && (hide || v < 0.05))) {
      hidden.set(element, hide);
      element.style.visibility = hide ? 'hidden' : '';
    }
    if (shown.get(element) === v) return;
    shown.set(element, v);
    element.style.opacity = String(v);
  }

  const layers = (() => {
    const containers = CONTAINERS.map((id) => {
      const element = elementOf(id);
      const cover = document.getElementById(`cover-${id}`);
      return { id, node: nodes[id], element, hub: id === 'hub', later: element.matches('.part-2, .part-3'),
        cover, disc: $('.planet-disc', element), en: cover && $('.pc-en', cover), outcome: cover && $('.outcome', cover),
        open: 0, lift: null };
    });
    const byId = Object.fromEntries(containers.map((container) => [container.id, container]));
    const frames = FRAMES.filter((key) => nodes[key] && elementOf(key)).map((key) => {
      const element = elementOf(key);
      return { node: nodes[key], element, container: byId[nodes[key].parent], cover: $('.frame-cover', element),
        body: $('.frame-body', element), text: $$('.frame-body > :not(.portal)', element), focus: 0,
        portals: [...DETAIL].map((kind) => `${key}.${kind}`).filter((id) => nodes[id] && elementOf(id)).map((id) =>
          ({ node: nodes[id], element: elementOf(id), caption: $('figcaption', elementOf(id)) })) };
    });
    return { containers, frames, hub: byId.hub, title: $('.hub-title'), detail: $('.hub-detail'), message: $('.hub-closing'),
      eyebrow: $('.hub-eyebrow'), spine: $$('#world .spine-text'), routes: $$('#world .route'), orbits: $('#world .orbits') };
  })();

  function reveal(free) {
    const s = cam.s;
    const level = (box, pad) => Math.log2(s / fitScale(box, pad, free));
    const zOverview = level(nodes.overview, PAD.overview);
    const zFrame = level(FRAME_BOX, PAD.frame);
    const zPortal = level(PORTAL_BOX, PAD.diagram);
    const zHub = level(nodes.hub, PAD.hub);
    const zPlanet = level(nodes.p1 || nodes.hub, PAD.planet);  // every planet has the same size
    // World distance from the centre of the free area to the farthest corner of the screen.
    const reach = Math.hypot(Math.max(free.cx, innerWidth - free.cx), Math.max(free.cy, innerHeight - free.cy)) / s;
    const shut = 1 - closing;
    // Past the overview the labels of the inner rings come in. The closing view is tighter than the overview
    // on a narrow screen, so it rules them out itself.
    const deep = smooth(zOverview, 0.3, 0.9) * shut;
    // The title and the closing message share one place in the hub: one leaves as the other arrives, and the
    // hub's two frames leave with the title. On the way into the closing view the title leaves from take-off
    // (titleAway), so it does not come back as the camera passes the hub.
    const framesOut = 1 - smooth(closing, 0, 0.65);
    const titleOut = Math.min(framesOut, 1 - titleAway);
    const messageIn = smooth(closing, 0.35, 1);

    // The subject. Under free input it is what the screen centre is on. In a flight it is held at the flight's
    // two ends (fly) and handed over once, one way, so topics and frames the path only passes over neither open
    // nor dim the others (at full pan speed that blinked). The destination takes hold early and the origin lets
    // go late, as the camera closes in on one and leaves the other behind, so the origin does not shut while it
    // still fills the screen. A flight stopped by free input hands back to the camera's subject over 450 ms, and
    // once free input has come to rest each topic eases fully open or shut (pin): a centre left on a topic's rim
    // would otherwise show it halfway, which the fade-through draws as nothing.
    let held;
    const time = performance.now();
    if (flight) {
      held = blend(flight.heldFrom, flight.heldTo, flight.e || 0, [0.2, 0.6], [0.5, 0.9]);
    } else {
      held = holdsAt(cam);
      if (pin && (pin.cam.x !== cam.x || pin.cam.y !== cam.y || pin.cam.s !== cam.s || pin.cam.r !== cam.r)) {
        letGo = { from: lastHeld, at: time, ...capsAt(time) };  // the camera moves on: hand back from the pinned topics
        pin = null;
        schedule();
      }
      if (letGo) {
        const k = smooth(time - letGo.at, 0, 450);
        if (k < 1) held = blend(letGo.from, held, k);
        else letGo = null;
      }
      if (wheelTo?.pinTo) {
        const k = smooth(time - wheelTo.pinAt, 0, 200);
        held = { frames: held.frames, topics: wheelTo.pinFrom.map((v, i) => mix(v, wheelTo.pinTo[i], k)) };
      }
      if (pin) {
        const k = smooth(time - pin.at, 0, 200);
        pin.done = k >= 1;
        held = { frames: held.frames, topics: held.topics.map((v, i) => mix(pin.from[i], pin.to[i], k)) };
      }
    }
    lastHeld = held;
    layers.frames.forEach((frame, j) => { frame.focus = held.frames[j]; });
    // Topics: the subject opens as it fills the screen, and the others step back to 28% by how far the subject
    // leads them, so in a hand-over neither of the two dims itself.
    const focus = held.topics;
    const dims = topicDims(focus, zOverview, closing);
    const caps = capsAt(time);
    let hubTitle = 0;
    layers.containers.forEach((container, i) => {
      const z = container.hub ? zHub : zPlanet;
      container.open = focus[i] * smooth(z, -0.9, -0.3);
      const dim = Math.min(caps.dimCap?.[i] ?? 1, dims[i]);
      const inner = 1 - 0.78 * smooth(z, 1, 2);  // a frame inside has become the subject
      show(container.element, dim);
      if (container.hub) {
        const title = Math.min(titleOut * inner, (caps.titleCap ?? 1) / Math.max(dim, 1e-3));
        show(layers.title, title);
        hubTitle = title;
        show(layers.detail, title * container.open);
        show(layers.message, messageIn);
        return;
      }
      // A topic's cover leaves before its frames' labels and its description arrive (a fade through, never two
      // texts over each other).
      show(container.cover, Math.min(caps.coverCap?.[i] ?? 1, (1 - smooth(container.open, 0, 0.5)) * dim));
      show(container.disc, smooth(container.open, 0.5, 1) * inner);
      show(container.en, deep);
      if (container.outcome) {
        show(container.outcome, closing);
        const lift = Math.round(400 * shut);
        if (lift !== container.lift) {
          container.lift = lift;
          container.outcome.style.transform = `translate(-50%,${lift}px)`;
        }
      }
    });
    layers.routes.forEach((route) => show(route, 0.95 * (1 - smooth(zOverview, 0.25, 0.8)) * shut));
    show(layers.orbits, 1 - 0.25 * deep);
    show(layers.eyebrow, deep);
    layers.spine.forEach((text) => show(text, deep));

    // Frames: a label (cover) while small, the readable body once large, swapped in one short dissolve that the
    // camera crosses in about a tenth of a flight (SWAPS). Close up, neighbouring frames step back.
    const body = smooth(zFrame, -1.5, -1);
    const label = 1 - smooth(zFrame, -1.6, -1.3);
    const frameBack = 0.62 * smooth(zFrame, -1.2, -0.4);
    const portalBack = 0.72 * smooth(zPortal, -1.3, -0.5);
    const captionGone = smooth(zPortal, -1, -0.4);
    const textBack = 0.6 * smooth(zFrame, 1.2, 2);
    const moving = Boolean(flight || wheelTo || gesture?.pinch);
    const strongest = new Map();  // per topic, the two strongest frame holds
    // Frames off screen hide their body, so no large subtree is left to switch on in one frame of a flight,
    // and catch up on the first frame they are near the screen again.
    const near = layers.frames.filter((frame) => {
      if (Math.hypot(frame.node.x - cam.x, frame.node.y - cam.y) - frame.node.w / 2 > reach) {
        show(frame.body, 0, moving);
        return false;
      }
      const [first, second] = strongest.get(frame.container) || [0, 0];
      strongest.set(frame.container, frame.focus >= first ? [frame.focus, first] : [first, Math.max(second, frame.focus)]);
      return true;
    });
    for (const frame of near) {
      const container = frame.container;
      // The closing message spans the hub, so its two frames make room for it. A frame steps back by how far
      // another frame of its topic leads it, so the leading frame stays at full strength in a hand-over too.
      const base = mix(container.hub ? 0.6 : 0.5, 1, container.open) * (container.hub ? framesOut : 1);
      const [first, second] = strongest.get(container);
      const other = frame.focus >= first ? second : first;
      // On the way into the closing view the hub's frames keep stepping back until they leave with the title.
      const back = container.hub ? Math.max(frameBack, 0.62 * titleAway) : frameBack;
      show(frame.element, base * (1 - back * Math.max(0, other - frame.focus)));
      // A flight that cancels the way into the closing view brings the hub's frame labels back with its title
      // (titleCap), so the two circles are not left empty while the hub is still the subject; they go as it hands
      // over, or from where they are when another flight takes over (fly).
      const hold = !container.hub || flight?.labelHold == null ? 0 : flight.labelHold === true
        ? Math.min(hubTitle, 1 - smooth(flight.e || 0, 0.5, 0.9)) : flight.labelHold * (1 - smooth(flight.e || 0, 0, 0.3));
      show(frame.cover, Math.max(smooth(container.open, 0.5, 1), hold) * label);
      show(frame.body, body, moving);
      if (body < 0.004) continue;
      const portalFocus = frame.portals.map((portal) => inBox(portal.node, cam.x, cam.y));
      const zoomedIn = Math.max(0, ...portalFocus);
      frame.text.forEach((element) => show(element, 1 - textBack * zoomedIn));
      frame.portals.forEach((portal, i) => {
        show(portal.element, 1 - portalBack * zoomedIn * (1 - portalFocus[i]));
        show(portal.caption, 1 - captionGone * portalFocus[i]);
      });
    }
  }

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
  // Fly to a node. The focus classes (links, depth, phone sheet) change at once; what is visible changes with
  // the camera on the way (reveal), so the content being left stays in view while it shrinks into its parent.
  function travel(id, instant) {
    clearTimeout(settleTimer);
    aim = id;
    setFocus(id);
    fly(fitNode(id), instant);
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
      // Rewriting 68 unchanged badges inside the canvas would re-lay it out at the start of every flight.
      const text = formatMinutes(data.frames[badge.dataset.frame].minutes * path.factor);
      if (badge.textContent !== text) badge.textContent = text;
    });
    $$('.path-menu [data-path]').forEach((button) => button.setAttribute('aria-checked', String(button.dataset.path === pathName)));
    $('[data-action="notes"]').setAttribute('aria-pressed', String(notesOpen));
    renderNotes(detour ? { id: aim, kind: nodes[aim]?.kind, frame: chain(aim)[1] } : step);
    if (cam && closing !== closingGoal()) schedule();  // e.g. dragging away from the closing step fades it out
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
    else fly(fitNode(current().node), !flight);  // a flight under way turns to the new framing and keeps its speed
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

  // At rest a topic is open or shut, never left halfway because the centre stopped on its rim: holds ease to 1
  // inside 0.8 of the radius and to 0 outside it (reveal).
  function pinHere() {
    const spatial = holdsAt(cam).topics;
    const to = spatial.map((v) => (v >= 0.5 ? 1 : 0));
    if (to.some((v, i) => v !== spatial[i])) {
      pin = { from: (lastHeld || holdsAt(cam)).topics.slice(), to, at: performance.now(), done: false, cam };
      schedule();
    }
  }

  function settleFree() {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      if (!detour || flight || wheelTo) return;
      pinHere();
      const id = focusFromCamera();
      aim = id;
      if (id !== focusId) setFocus(id);
      render();
      // Other windows match the level of detail, not the width of view, so a presenter area of another shape
      // shows the same: near the overview (how far the others step back) its level, closer in a topic's.
      const free = area();
      channel?.postMessage({ type: 'cam', aim, x: cam.x, y: cam.y, r: cam.r, span: free.w / cam.s,
        level: Math.log2(cam.s / fitScale(nodes.p1 || nodes.hub, PAD.planet, free)),
        hub: Math.log2(cam.s / fitScale(nodes.hub, PAD.hub, free)),
        overview: Math.log2(cam.s / fitScale(nodes.overview, PAD.overview, free)) });
    }, 140);
  }

  // Free input takes the camera where it is: a flight stops, and a drag or pinch also ends an eased wheel zoom.
  function enterFree({ keepWheel = false } = {}) {
    const now = performance.now();
    if (flight) {
      stopDir = Math.sign(Math.log(flight.target.s / cam.s));
      letGo = { from: lastHeld, at: now, ...capsAt(now) };
      schedule();
    }
    if (wheelTo?.pinTo && !keepWheel) {
      // A drag or pinch that ends a wheel zoom hands back from the topics as the zoom eased them, and goes on
      // with what an earlier hand-back still holds down.
      letGo = { from: lastHeld, at: now, ...capsAt(now) };
      schedule();
    }
    if (flight || (wheelTo && !keepWheel)) {
      flight = null;
      if (!keepWheel) wheelTo = null;
      if (!wheelTo) world.classList.remove('moving');
    }
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
      gesture = { pinch: Math.hypot(a.x - b.x, a.y - b.y), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, moved: true,
        from: cam.s };
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

  // A glide around the screen point (x, y) to the scale s, as the end of a settling wheel zoom; at once under
  // reduced motion. It goes the way of the glide, so a step that way goes on past s rather than starting over.
  function glideTo(s, x, y) {
    if (reducedMotion.matches) {
      apply(zoomAround(x, y, s));
      settleFree();
      return;
    }
    wheelTo = aimPin({ s, raw: s, from: cam.s, x, y, tau: 80, at: -Infinity, dir: Math.sign(Math.log(s / cam.s)), settled: true, glide: true, v: 0,
      w: glideRate(Math.log(s / cam.s), 0) }, wheelTo);
    world.classList.add('moving');
    schedule();
  }

  // A flight stopped by a drag or a sideways scroll may have left the camera halfway through a swap of detail:
  // it glides on, around the screen centre, to where the swap is done (as after a wheel zoom or a pinch).
  function settleHere() {
    const free = area();
    const s = settleScale(cam.s, free.cx, free.cy, stopDir);
    stopDir = 0;
    if (s === cam.s) return false;
    glideTo(s, free.cx, free.cy);
    return true;
  }

  function endPointer(event) {
    if (!pointers.has(event.pointerId)) return;
    pointers.delete(event.pointerId);
    stage.classList.remove('dragging');
    const start = gesture;
    if (pointers.size > 0) return;
    gesture = null;
    if (start?.pinch) {
      stopDir = 0;
      reveal(area());  // bodies dissolved during the pinch are hidden now that it has stopped (show)
      if (event.type === 'pointercancel') return;
      // A pinch let go halfway through a swap of detail glides on to where the swap is done, as the wheel does
      // (to the nearer end after a small one).
      const net = Math.log2(cam.s / start.from);
      const s = settleScale(cam.s, start.mid.x, start.mid.y, Math.abs(net) < 0.15 ? 0 : Math.sign(net));
      if (s !== cam.s) glideTo(s, start.mid.x, start.mid.y);
      return;
    }
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
    if (start.moved) {
      settleHere();
      return;
    }
    if (event.target.closest('.portal.is-focus a[href]')) return;  // evidence links open normally
    // A body still mostly dissolved does not take clicks meant for the frame under its label.
    let hit = event.target.closest('[data-node]');
    const dissolved = hit?.closest('.frame-body');
    if (dissolved && (shown.get(dissolved) ?? 1) < 0.5) hit = dissolved.closest('[data-node]');
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

  let quietTimer = 0;
  let quietFrom = 0;
  stage.addEventListener('wheel', (event) => {
    event.preventDefault();
    enterFree({ keepWheel: true });
    const unit = event.deltaMode === 1 ? 16 : 1;
    if (!event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
      const a = (cam.r * Math.PI) / 180;
      const wx = (event.deltaX * unit) / cam.s;
      apply({ x: cam.x + wx * Math.cos(a), y: cam.y + wx * Math.sin(a), s: cam.s, r: cam.r });
      // The zoom now comes to rest somewhere else.
      if (wheelTo?.settled) settleWheel();
      else if (wheelTo) aimPin(wheelTo, wheelTo);
      else if (!settleHere()) settleFree();
      return;
    }
    stopDir = 0;
    // A notch (or a pinch step, with ctrlKey) moves the goal; tick eases the camera there around the pointer,
    // so a mouse wheel zooms smoothly instead of in 15% jumps. A notch the other way turns round from the goal
    // as the notches set it (so in and out again returns exactly) or, if that is still behind the camera, from
    // the camera: the zoom never runs on the old way. During the glide to the end of a swap the goal is that
    // end, so it turns round from the camera; a pinch that only paused there goes on from the fingers' goal.
    const factor = Math.exp(-event.deltaY * unit * (event.ctrlKey ? 0.012 : 0.0016));
    const dir = Math.sign(-event.deltaY);
    const keep = wheelTo && wheelTo.dir === dir;
    const resumed = Boolean(keep && event.ctrlKey && wheelTo.settled && wheelTo.glide && performance.now() - wheelTo.at <= 120);
    let s = clampScale((!wheelTo ? cam.s : keep && !resumed ? wheelTo.s : !keep && wheelTo.glide ? cam.s : wheelTo.raw) * factor);
    if (Math.log(s / cam.s) * dir < 0) s = clampScale(cam.s * factor);
    const { clientX: x, clientY: y } = event;
    clearTimeout(settleTimer);
    if (reducedMotion.matches) {
      // Without motion a notch lands at once, settled; the small steps of a pinch or a trackpad scroll settle
      // once they stop, so the gesture is not taken through a swap step by step.
      clearTimeout(quietTimer);
      if (Math.abs(Math.log2(factor)) >= 0.1) {
        quietFrom = 0;
        apply(zoomAround(x, y, settleScale(s, x, y, dir)));
        settleFree();
        return;
      }
      quietFrom = quietFrom || cam.s;
      apply(zoomAround(x, y, s));
      quietTimer = setTimeout(() => {
        const net = Math.log2(cam.s / quietFrom);
        quietFrom = 0;
        apply(zoomAround(x, y, settleScale(cam.s, x, y, Math.abs(net) < 0.15 ? 0 : Math.sign(net))));
        settleFree();
      }, 120);
      return;
    }
    // A step during the glide out of a swap keeps that glide's spring rather than lunging at the new goal: a step
    // the same way rides on it (boost, tick), and a step back turns the spring round.
    const glide = Boolean(wheelTo && wheelTo.glide && !resumed);
    // A notch back during the glide: the spring only brings the old motion to rest where the camera is, and the
    // notch rides on it at an ordinary notch's pace. A nudge or a pinch step turns the spring itself round.
    const turn = glide && !keep && !event.ctrlKey && Math.abs(Math.log2(factor)) >= 0.05;
    const previous = wheelTo;
    const from = keep ? wheelTo.from : cam.s;
    // Where the zoom will rest if no step follows (tick's settle): the end of a swap it is in, the way it went or,
    // after a nudge, the nearer one. A pinch past a nudge does not slow down before that end (tick).
    const nudge = Math.abs(Math.log2(s / from)) < 0.15;
    const rest = settleScale(s, x, y, nudge ? 0 : dir);
    wheelTo = { s, raw: s, from, x, y, tau: event.ctrlKey ? 40 : 80, at: performance.now(), dir, settled: false, glide,
      w: glide ? (keep ? wheelTo.w : turn ? 1 / 70 : glideRate(Math.log(s / cam.s), wheelTo.v)) : 0, v: wheelTo ? wheelTo.v : 0,
      boost: glide && keep ? (wheelTo.boost || 0) + (event.ctrlKey ? 0 : Math.log(s / wheelTo.s)) : turn ? Math.log(s / cam.s) : 0,
      boostPinch: glide && keep ? (wheelTo.boostPinch || 0) + (event.ctrlKey ? Math.log(s / wheelTo.s) : 0) : 0,
      rest, end: event.ctrlKey && !nudge && Math.log(rest / s) * dir > 0 ? rest : 0, pinch: event.ctrlKey };
    aimPin(wheelTo, previous);
    world.classList.add('moving');
    schedule();
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
      const free = area();
      const fit = fitScale(nodes.p1 || nodes.hub, PAD.planet, free);
      let s = Number.isFinite(message.level) ? fit * 2 ** message.level : free.w / message.span;
      if (Number.isFinite(message.overview) && message.overview < 1 && Number.isFinite(message.level)) {
        // Near the overview, its level (how far the others step back), as far as that leaves the topic or hub
        // under the centre shut, as it is in the sending window: a window of another shape has the overview
        // level elsewhere on them. Once one is opening there, the planet level.
        const held = holdsAt(message).topics;
        const [shut] = SWAPS.container;
        let most = fitScale(nodes.overview, PAD.overview, free) * 2 ** message.overview;
        layers.containers.forEach((container, i) => {
          const z = container.hub ? message.hub : message.level;
          if (held[i] < 0.5 || !Number.isFinite(most)) return;
          const top = (container.hub ? fitScale(nodes.hub, PAD.hub, free) : fit) * 2 ** shut;
          most = Number.isFinite(z) && z <= shut + 1e-3 ? Math.min(most, top) : NaN;
        });
        if (Number.isFinite(most)) s = most;
      }
      apply({ x: message.x, y: message.y, r: message.r, s: clampScale(s) });
      const settled = settleScale(cam.s, free.cx, free.cy, 0);  // a window of another shape can land in a swap
      if (settled !== cam.s) apply({ x: cam.x, y: cam.y, r: cam.r, s: settled });
      pinHere();  // as the window that sent it does; not posted back, so the windows do not echo each other
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
  document.fonts?.ready.then(() => { if (!flight && !wheelTo) refit(); });
  if (presenter && channel) {
    channel.postMessage({ type: 'hello', id: helloId });
    setTimeout(adoptReply, 600);
  }
  setInterval(tickClock, 1000);
  tickClock();
  body.classList.add('ready');
  stage.focus({ preventScroll: true });
})();
