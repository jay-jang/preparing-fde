"""Render the Prezi-style zoom atlas from parsed presentation frames.

One canvas holds a hub (title, 0-1, 0-2) and six topic planets; each planet holds its frames on a ring
around a title disc, and each frame holds three detail portals (diagram, code, evidence data). The camera
flies, zooms and rotates between these nested targets, and deeper content is revealed only when zoomed in.

Every camera target is a world-space box (centre, width, height, rotation in degrees) computed here and
shipped as JSON, so atlas.js never measures the DOM. The DOM nests the same transforms (hub/planet > frame >
portal) with ``transform-origin`` at each element's centre, so the browser draws exactly these boxes.
"""
from html import escape
import json
import math
import re
import unicodedata

FRAME_R, PLANET_R, HUB_R = 2000, 6900, 5800
RING_R, DISC_R = 4400, 2100
PORTAL_W, PORTAL_H, PORTAL_PAD = 960, 540, 24
PORTAL_Y = 855                       # portal centres sit this far below the frame centre
PORTAL_XS = (-1040, 0, 1040)
PORTAL_KINDS = ('diagram', 'code', 'data')
PORTAL_NAMES = {'diagram': '도식', 'code': '코드', 'data': '데이터'}
FRAME_TILT = (-4, 3, -2, 4, -3, 2)
HUB_FRAMES = {'0-1': ((-2250, 1900), -3), '0-2': ((2250, 1900), 3)}
OVERVIEW = {'x': 0, 'y': 0, 'w': 2 * (23000 + PLANET_R), 'h': 2 * (10000 + PLANET_R), 'rot': 0}

PLANETS = {
    1: {'name': '데이터 통합과 온톨로지', 'en': 'Ingestion & Ontology', 'part': 1,
        'tagline': '같은 설비를 같은 객체로, 재전송에도 같은 상태로', 'outcome': '재생 가능한 상태',
        'proof': '재전송·역순·삭제에도 같은 상태 (1-2, 1-3)',
        'at': (-12000, -10000), 'rot': -7},
    2: {'name': 'RAG와 에이전트', 'en': 'Retrieval, Grounding & Agents', 'part': 1,
        'tagline': '허용된 근거 안에서 찾고, 출처를 검증해 답한다', 'outcome': '허용된 근거',
        'proof': 'RLS 안 검색과 버전 맞는 인용만 (2-1~2-3)',
        'at': (12000, -10000), 'rot': 6},
    3: {'name': '폐쇄망과 거버넌스', 'en': 'Air-gap & Governance', 'part': 1,
        'tagline': '인터넷 없이 실행되고, 경계와 기록이 남는 배포', 'outcome': '검증된 실행물',
        'proof': '서명·digest 반입, 고정된 실행 경계 (3-1, 3-2)',
        'at': (-12000, 10000), 'rot': 5},
    4: {'name': '현장 MVP와 운영', 'en': 'Prototype to Production', 'part': 1,
        'tagline': '작게 증명하고, 게이트를 통과한 범위만 넓힌다', 'outcome': '운영 가능한 책임',
        'proof': '품질 게이트·burn rate 경보·복구 훈련 (4-2, 4-3)',
        'at': (12000, 10000), 'rot': -6},
    5: {'name': '현장 플레이북', 'en': 'FDE Field Playbook', 'part': 2,
        'tagline': '고객과 합의하고, 재현하고, 넘겨준다', 'outcome': None,
        'at': (-23000, 0), 'rot': -9},
    6: {'name': '규모 확장', 'en': 'Scale-out', 'part': 3,
        'tagline': '한 공장의 파일럿을 측정으로 전\u00a0공장까지 넓힌다', 'outcome': None,
        'at': (23000, 0), 'rot': 8},
}
PARTS = {1: '1부 기술 아키텍처', 2: '2부 현장 플레이북', 3: '3부 규모 확장'}
SPINE = [('IDENTITY', '누가 요청했나', -90), ('POLICY', '무엇이 허용되나', 0),
         ('LINEAGE', '어디서 왔나', 90), ('OBSERVABILITY', '무슨 일이 있었나', 180)]
# Part 1 flows run on lanes around the hub: (from planet, to planet, lane radius, label, kind).
FLOWS = [(1, 2, 7000, '데이터와 권한 상태', 'flow'), (2, 4, 7000, '근거가 붙은 응답', 'flow'),
         (3, 1, 7000, '검증된 실행 환경', 'platform'), (4, 1, 7900, '운영 피드백', 'feedback')]
INTERVIEW = ['0-1', '1-1', '1-2', '2-1', '2-2', '2-3', '3-1', '4-1', '0-2']
DEEP = ['0-1', '1-1', '1-2', '1-5', '2-1', '2-2', '2-3', '2-4', '2-5', '3-1', '3-4', '4-1', '4-2', '4-4',
        '5-2', '6-1', '0-2']
PATHS = [('interview', '면접 15분', 15, '핵심 9개 프레임, 프레임마다 도식이나 코드 하나'),
         ('deep', '면접 심화 30분', 30, '17개 프레임으로 설계 판단과 확장까지'),
         ('part1', '1부 기술 아키텍처', None, '개요와 네 기술 영역의 모든 도식·코드·데이터'),
         ('part2', '2부 현장 플레이북', None, '고객 합의, 재현, 장애 대응, 운영 이관'),
         ('part3', '3부 규모 확장', None, '전 공장 확장에서 먼저 깨지는 곳과 측정'),
         ('onboarding', '온보딩 전체', None, '34개 프레임 전체를 순서대로')]

KEYWORDS = {
    'python': r'\b(?:import|from|as|def|return|if|else|elif|for|in|with|not|and|or|raise|try|except|class|'
              r'lambda|None|True|False|assert|while|yield|is)\b',
    'sql': r'\b(?:INSERT|INTO|VALUES|CREATE|POLICY|ON|FOR|SELECT|TO|USING|AND|NOT|EXISTS|FROM|WHERE|UPDATE|SET|'
           r'CONFLICT|DO|ALTER|TABLE|ENABLE|FORCE|ROW|LEVEL|SECURITY|PARTITION|BY|LIST|OF|IN|ATTACH|DETACH|'
           r'EXPLAIN|GRANT|ROLE|AS|WITH|CHECK|true|false)\b',
    'yaml': r'[A-Za-z_][\w.\-]*(?=:(?:\s|$))',
    'text': None,
}
COMMENT = {'python': r'#[^\n]*', 'yaml': r'#[^\n]*', 'sql': r'--[^\n]*'}


# ---------------------------------------------------------------- geometry

def rotate(x, y, degrees):
    """CSS rotate(): positive angles turn clockwise on screen (y grows downwards)."""
    a = math.radians(degrees)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def place(parent, local, tilt):
    """World centre and rotation of a child at ``local`` (relative to the parent centre) tilted by ``tilt``."""
    dx, dy = rotate(*local, parent['rot'])
    return {'x': round(parent['x'] + dx, 2), 'y': round(parent['y'] + dy, 2), 'rot': parent['rot'] + tilt}


def ring(count):
    """Frame centres on a ring, first at the upper left, then clockwise."""
    start = -90 - 180 / count
    return [(RING_R * math.cos(math.radians(start + i * 360 / count)),
             RING_R * math.sin(math.radians(start + i * 360 / count))) for i in range(count)]


def diagram_box(ratio):
    inner_w, inner_h = PORTAL_W - 2 * PORTAL_PAD, PORTAL_H - 2 * PORTAL_PAD
    if ratio >= inner_w / inner_h:
        return inner_w, inner_w / ratio
    return inner_h * ratio, inner_h


# ---------------------------------------------------------------- markup helpers

def highlight(code, language):
    """Return escaped code with a few token classes; unknown languages are only escaped."""
    if not KEYWORDS.get(language):
        return escape(code)
    pattern = re.compile('|'.join([
        f'(?P<c>{COMMENT[language]})',
        r'''(?P<s>"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|'(?:''|\\.|[^'\\\n])*')''',
        f'(?P<k>{KEYWORDS[language]})',
        r'(?P<n>\b\d[\d_.]*\b)',
    ]))
    parts, position = [], 0
    for match in pattern.finditer(code):
        parts.append(escape(code[position:match.start()]))
        parts.append(f'<span class="t-{match.lastgroup}">{escape(match.group())}</span>')
        position = match.end()
    parts.append(escape(code[position:]))
    return mark_output(code, ''.join(parts))


def mark_output(code, html):
    """Give comments that show printed output (right after a print line) their own class, t-out."""
    raw, lines = code.split('\n'), html.split('\n')
    if len(raw) != len(lines):
        return html
    printing = False
    for i, line in enumerate(raw):
        stripped = line.strip()
        if stripped.startswith(('#', '--')) and printing:
            lines[i] = lines[i].replace('class="t-c"', 'class="t-c t-out"')
            continue
        printing = 'print(' in line
        if printing and ('#' in line or '--' in line):
            lines[i] = lines[i].replace('class="t-c"', 'class="t-c t-out"')
    return '\n'.join(lines)


def keep_together(text):
    """Escape for HTML and add word joiners so '업무·권한', 'SHA-256' and '(source)를' never break apart."""
    text = re.sub(r'\s*·\s*', lambda m: m.group().replace('·', '\u2060·\u2060'), text)
    text = re.sub(r'(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])', '-\u2060', text)  # SHA-256, P-101
    return escape(re.sub(r'\)(?=[가-힣])', ')\u2060', text))


def columns(line):
    return sum(1.0 if unicodedata.east_asian_width(c) in 'WF' else .6 for c in line)


def code_leading(code):
    return 1.22 if len(code.split('\n')) >= 20 else 1.42


def code_font(code, width, height):
    """Largest font (world px) at which every line fits the code portal without wrapping."""
    lines = code.split('\n')
    widest = max(columns(line) for line in lines) or 1
    return round(max(8.0, min(17.0, width / widest * .97, height / (len(lines) * code_leading(code)))), 2)


def text_width(text, font):
    """Rough rendered width: wide (Hangul, CJK) glyphs ~1 em, others ~0.56 em."""
    plain = re.sub(r'<[^>]+>', '', text)
    return sum(font if unicodedata.east_asian_width(c) in 'WF' else font * .56 for c in plain)


def table_font(rows):
    """Largest font (world px) at which the evidence table fills the data portal without overflowing."""
    inner_w, inner_h = PORTAL_W - 2 * PORTAL_PAD, PORTAL_H - 2 * PORTAL_PAD - 40
    columns = (inner_w * .25 - 16, inner_w * .47 - 16, inner_w * .28 - 16)
    for tenth in range(220, 139, -5):
        font = tenth / 10
        height = font * .75 * 1.35 + 12
        for row in rows:
            lines = max(math.ceil(text_width(row[name], font * scale) / width)
                        for name, scale, width in zip(('item', 'value', 'basis'), (1, 1.04, .82), columns))
            height += max(1, lines) * font * 1.35 + 17
        if height <= inner_h:
            return font
    return 14.0


def svg_markup(frame):
    """Inline the vector diagram (crisp at every zoom); fall back to the PNG."""
    diagram = frame['diagram']
    label = escape(f'{frame["title"]} 도식', quote=True)
    if diagram.get('svg'):
        svg = re.sub(r'^<\?xml[^>]*>\s*', '', diagram['svg'].strip())
        head = re.match(r'<svg\b[^>]*>', svg)
        attributes = dict(re.findall(r'([\w:-]+)="([^"]*)"', head.group()))
        kept = {name: value for name, value in attributes.items()
                if name in ('id', 'viewBox', 'xmlns', 'xmlns:xlink', 'class')}
        kept.update({'role': 'img', 'aria-label': label, 'preserveAspectRatio': 'xMidYMid meet'})
        return '<svg ' + ' '.join(f'{name}="{value}"' for name, value in kept.items()) + '>' + svg[head.end():]
    return f'<img src="{diagram["src"]}" alt="{label}" decoding="async">'


def box_style(cx, cy, w, h, tilt=0):
    """Absolute box whose centre is (cx, cy) in the parent's local pixels."""
    style = f'left:{cx - w / 2:g}px;top:{cy - h / 2:g}px;width:{w:g}px;height:{h:g}px'
    return style + (f';transform:rotate({tilt:g}deg)' if tilt else '')


def level_tag(level):
    return {1: 'L1 개요', 2: 'L2 시스템 계약', 3: 'L3 불변식과 코드'}[level]


# ---------------------------------------------------------------- nodes

def segments(value):
    """Let a value wrap only between its ' · ' parts; short parts never split ('Recall@5 1.0 → 0.5')."""
    parts = value.split(' · ')
    return ' · '.join(f'<span class="seg">{part}</span>' if len(re.sub(r'<[^>]+>', '', part)) <= 28 else part
                      for part in parts)


def basis_kind(basis):
    """code / calc (computed here), source (official link) or proposal (to agree with the customer)."""
    if basis.startswith('코드:'):
        return 'code'
    if basis.startswith('계산:'):
        return 'calc'
    if basis.startswith('제안값'):
        return 'proposal'
    return 'source' if '<a href=' in basis else 'proposal'


def portal(frame, kind, centre):
    key = frame['key']
    node = f'{key}.{kind}'
    caption = f'<figcaption>{PORTAL_NAMES[kind]}</figcaption>'
    tag = f'<span class="portal-tag">{key} {escape(frame["label"])} · {PORTAL_NAMES[kind]}</span>'
    attrs = (f'class="portal portal-{kind}" id="n-{key}-{kind}" data-node="{node}" '
             f'style="{box_style(centre[0], centre[1], PORTAL_W, PORTAL_H)}" '
             f'aria-label="{key} {PORTAL_NAMES[kind]}"')
    if kind == 'diagram':
        w, h = diagram_box(frame['diagram']['ratio'])
        body = f'{tag}<div class="diagram-box" style="width:{w:g}px;height:{h:g}px">{svg_markup(frame)}</div>'
    elif kind == 'code':
        code = frame['code']
        font = code_font(code['text'], PORTAL_W - 2 * PORTAL_PAD, PORTAL_H - 2 * PORTAL_PAD - 26)
        source = f'<span class="code-file">{escape(code["source"])}</span>' if code['source'] else ''
        body = (f'<div class="code-head"><span class="code-lang">{key} {escape(frame["label"])} · 코드 · '
                f'{escape(code["lang"])}</span>{source}</div>'
                f'<pre style="font-size:{font}px;line-height:{code_leading(code["text"])}"><code>{highlight(code["text"], code["lang"])}</code></pre>')
    else:
        rows = ''.join(f'<tr class="basis-{basis_kind(row["basis"])}{" key" if "<strong>" in row["item"] else ""}">'
                       f'<th scope="row">{row["item"]}</th>'
                       f'<td class="value">{segments(row["value"])}</td>'
                       f'<td class="basis">{row["basis"]}</td></tr>' for row in frame['data'])
        body = (f'{tag}<table style="font-size:{table_font(frame["data"])}px"><thead><tr><th scope="col">항목</th>'
                '<th scope="col">값</th><th scope="col">근거</th></tr>'
                f'</thead><tbody>{rows}</tbody></table>') if rows else '<p class="empty">데이터 준비 중</p>'
    return f'<figure {attrs}><div class="portal-inner">{body}</div>{caption}</figure>'


def frame_markup(frame, local, tilt):
    key, level = frame['key'], frame['level']
    title_font = 180 if len(frame['title']) <= 30 else 160
    points = ''.join(f'<li><b>{index:02d}</b><span>{escape(point)}</span></li>'
                     for index, point in enumerate(frame['points'], 1))
    portals = ''.join(portal(frame, kind, (FRAME_R + x, FRAME_R + PORTAL_Y))
                      for kind, x in zip(PORTAL_KINDS, PORTAL_XS))
    source = f' · {escape(frame["code"]["source"])}' if frame['code']['source'] else ''
    return (
        f'<article class="frame c{frame["cluster"]} level-{level}" id="n-{key}" data-node="{key}" '
        f'style="{box_style(local[0], local[1], 2 * FRAME_R, 2 * FRAME_R, tilt)}" '
        f'aria-label="{key} {escape(frame["title"], quote=True)}">'
        f'<div class="frame-cover" aria-hidden="true"><span class="cover-key">{key}</span>'
        f'<span class="cover-label">{escape(frame["label"])}</span>'
        f'<span class="cover-meta">L{level} · <span class="minutes" data-frame="{key}">'
        f'{frame["minutes"]}분</span></span></div>'
        f'<div class="frame-body">'
        f'<p class="f-kicker"><span class="f-key">{key}</span>{escape(frame["kicker"])}</p>'
        f'<h3 class="f-title" style="font-size:{title_font}px">{keep_together(frame["title"])}</h3>'
        f'<p class="f-message">{keep_together(frame["message"])}</p>'
        f'<ol class="f-points">{points}</ol>{portals}'
        f'<p class="f-foot">{level_tag(level)} · <span class="minutes" data-frame="{key}">{frame["minutes"]}분</span>'
        f'{source}</p></div></article>')


def planet_markup(number, frames, positions):
    planet = PLANETS[number]
    size = 2 * PLANET_R
    frame_html = ''.join(frame_markup(frame, (PLANET_R + x, PLANET_R + y), FRAME_TILT[i % len(FRAME_TILT)])
                         for i, (frame, (x, y)) in enumerate(zip(frames, positions)))
    minutes = sum(frame['minutes'] for frame in frames)
    return (
        f'<section class="planet c{number} part-{planet["part"]}" id="n-p{number}" data-node="p{number}" '
        f'style="{box_style(planet["at"][0], planet["at"][1], size, size, planet["rot"])}" '
        f'aria-label="{number}. {escape(planet["name"], quote=True)}">'
        f'<header class="planet-disc" style="{box_style(PLANET_R, PLANET_R, 2 * DISC_R, 2 * DISC_R)}">'
        f'<span class="pd-part">{number:02d} · {PARTS[planet["part"]]}</span>'
        f'<h2 class="pd-name">{escape(planet["name"])}</h2><p class="pd-en">{escape(planet["en"])}</p>'
        f'<p class="pd-tagline">{keep_together(planet["tagline"])}</p>'
        f'<p class="pd-meta">프레임 {len(frames)}개 · {minutes}분</p></header>'
        f'{frame_html}</section>')


def cover_markup(number):
    """The overview label of a topic. It lives above the route layer, with the planet's own box and tilt."""
    planet = PLANETS[number]
    outcome = (f'<span class="outcome">{escape(planet["outcome"])}<small>{escape(planet["proof"])}</small></span>'
               if planet['outcome'] else '')
    return (f'<div class="planet-cover c{number} part-{planet["part"]}" id="cover-p{number}" aria-hidden="true" '
            f'style="{box_style(planet["at"][0], planet["at"][1], 2 * PLANET_R, 2 * PLANET_R, planet["rot"])}">'
            f'<span class="pc-number">{number:02d}</span><span class="pc-name">{escape(planet["name"])}</span>'
            f'<span class="pc-en">{escape(planet["en"])}</span>{outcome}</div>')


def hub_markup(by_key):
    frames = ''.join(frame_markup(by_key[key], (HUB_R + x, HUB_R + y), tilt)
                     for key, ((x, y), tilt) in HUB_FRAMES.items() if key in by_key)
    return (
        f'<section class="hub c0" id="n-hub" data-node="hub" style="{box_style(0, 0, 2 * HUB_R, 2 * HUB_R)}" '
        'aria-label="표지와 개요">'
        '<header class="hub-title"><p class="hub-eyebrow">FDE 면접 · 온보딩 기술 발표</p>'
        '<h1>고객의 데이터에서,<br><em>운영 가능한 AI까지.</em></h1></header>'
        '<p class="hub-closing">P-101 답변은 재전송에도 같은 상태와 권한 안의 인용\u00a0근거로 만들고, '
        '<em>서명된\u00a0실행물과 게이트\u2060·\u2060복구\u00a0훈련</em> 위에서만 내보낸다.</p>'
        '<div class="hub-detail"><p class="hub-question">이 답변을 어떤 데이터와 권한으로 만들었고,<br>'
        '장애가 난 뒤에도 같은 사실을 재현할 수 있는가?</p>'
        '<p class="hub-case"><span>CASE</span>“부산 공장 P-101의 반복 과열 원인과 점검 순서는?”</p></div>'
        f'{frames}</section>')


# ---------------------------------------------------------------- orbit layer

def polar(radius, degrees):
    a = math.radians(degrees)
    return radius * math.cos(a), radius * math.sin(a)


def arc_path(radius, start, end, inner=None):
    """Clockwise arc from ``start`` to ``end`` degrees; ``inner`` adds radial legs out to that radius."""
    span = (end - start) % 360
    x0, y0 = polar(radius, start)
    x1, y1 = polar(radius, end)
    large = 1 if span > 180 else 0
    path = f'M{x0:.0f},{y0:.0f} A{radius},{radius} 0 {large} 1 {x1:.0f},{y1:.0f}'
    if inner:
        sx, sy = polar(inner, start)
        ex, ey = polar(inner, end)
        path = f'M{sx:.0f},{sy:.0f} L' + path[1:] + f' L{ex:.0f},{ey:.0f}'
    return path


def text_arc(radius, centre, span):
    """Arc for a label centred at ``centre`` degrees, reversed on the lower half so text stays upright."""
    start, end = centre - span / 2, centre + span / 2
    if 0 < centre % 360 < 180:
        x0, y0 = polar(radius, end)
        x1, y1 = polar(radius, start)
        return f'M{x0:.0f},{y0:.0f} A{radius},{radius} 0 0 0 {x1:.0f},{y1:.0f}'
    x0, y0 = polar(radius, start)
    x1, y1 = polar(radius, end)
    return f'M{x0:.0f},{y0:.0f} A{radius},{radius} 0 0 1 {x1:.0f},{y1:.0f}'


def orbit_layer():
    left, top, width, height = -33000, -24000, 66000, 48000
    angle = {n: math.degrees(math.atan2(p['at'][1], p['at'][0])) for n, p in PLANETS.items()}
    reach = math.hypot(*PLANETS[1]['at']) - PLANET_R - 160
    parts = [f'<svg class="orbits" style="left:{left}px;top:{top}px;width:{width}px;height:{height}px" '
             f'viewBox="{left} {top} {width} {height}" aria-hidden="true"><defs>']
    for kind in ('flow', 'platform', 'feedback', 'part'):
        parts.append(f'<marker id="head-{kind}" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="3.2" '
                     f'markerHeight="3.2" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
                     f'class="head {kind}"/></marker>')
    parts.append('</defs>')
    # Orbits and blueprint ticks.
    for radius in (math.hypot(*PLANETS[1]['at']), 23000):
        parts.append(f'<circle class="orbit" r="{radius:.0f}"/>')
    ticks = []
    for degrees in range(0, 360, 5):
        r0, r1 = (23000 - 260, 23000 + 260) if degrees % 15 == 0 else (23000 - 130, 23000 + 130)
        (x0, y0), (x1, y1) = polar(r0, degrees), polar(r1, degrees)
        ticks.append(f'M{x0:.0f},{y0:.0f} L{x1:.0f},{y1:.0f}')
    parts.append(f'<path class="tick" d="{" ".join(ticks)}"/>')
    # The hub: a pump impeller drawn faintly behind the title (the case is an overheating pump, P-101).
    parts.append(f'<circle class="hub-disc" r="{HUB_R}"/>')
    blades = []
    for i in range(7):
        base = i * 360 / 7
        (x0, y0), (cx, cy), (x1, y1) = polar(1300, base), polar(3600, base + 28), polar(5300, base + 70)
        blades.append(f'M{x0:.0f},{y0:.0f} Q{cx:.0f},{cy:.0f} {x1:.0f},{y1:.0f}')
    parts.append(f'<path class="impeller" d="{" ".join(blades)}"/><circle class="impeller" r="1300"/>')
    # Cross-cutting guarantees ring the core.
    parts.append(f'<circle class="spine-ring" r="{HUB_R + 260}"/>')
    for index, (name, korean, centre) in enumerate(SPINE):
        parts.append(f'<path id="spine-{index}" class="guide" d="{text_arc(HUB_R + 560, centre, 80)}"/>'
                     f'<text class="spine-text"><textPath href="#spine-{index}" startOffset="50%">'
                     f'{name} · {escape(korean)}</textPath></text>')
    # Part 1 flows between the four technical planets.
    for index, (source, target, radius, label, kind) in enumerate(FLOWS):
        start, end = angle[source] % 360, angle[target] % 360
        path = arc_path(radius, start, end, inner=reach)
        middle = start + ((end - start) % 360) / 2
        parts.append(f'<path class="halo" d="{path}"/><path class="arrow {kind}" d="{path}" '
                     f'marker-end="url(#head-{kind})"/>')
        parts.append(f'<path id="flow-{index}" class="guide" d="{text_arc(radius, middle, 60)}"/>'
                     f'<text class="flow-text {kind}"><textPath href="#flow-{index}" startOffset="50%">'
                     f'{escape(label)}</textPath></text>')
    # Parts 2 and 3 hang off the hub.
    for number, label in ((5, '2부 · 합의하고 지키기'), (6, '3부 · 측정하며 넓히기')):
        x = PLANETS[number]['at'][0]
        sign = 1 if x > 0 else -1
        x0, x1 = sign * (HUB_R + 1500), x - sign * (PLANET_R + 200)
        parts.append(f'<path class="halo" d="M{x0},0 L{x1},0"/><path class="arrow part" d="M{x0},0 L{x1},0" '
                     f'marker-end="url(#head-part)"/>')
        parts.append(f'<text class="part-text" x="{(x0 + x1) / 2:.0f}" y="-380">{escape(label)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def route_layer(layer):
    """Empty path layers atlas.js fills: 'lines' sits under every circle, so the path shows only between
    topics; 'route' (the stops) sits on top."""
    left, top, width, height = -33000, -24000, 66000, 48000
    return (f'<svg class="route {layer}" id="{layer}" style="left:{left}px;top:{top}px;width:{width}px;'
            f'height:{height}px" viewBox="{left} {top} {width} {height}" aria-hidden="true"></svg>')


# ---------------------------------------------------------------- scene, steps and paths

def scene(frames):
    """World boxes for every camera target and the HTML of the world."""
    by_key = {frame['key']: frame for frame in frames}
    nodes = {'overview': {**OVERVIEW, 'kind': 'overview'},
             'closing': {'x': 0, 'y': 0, 'w': 42000, 'h': 27000, 'rot': 0, 'kind': 'closing'}}
    hub = {'x': 0, 'y': 0, 'rot': 0}
    nodes['hub'] = {**hub, 'w': 2 * HUB_R, 'h': 2 * HUB_R, 'kind': 'hub'}
    placements = {}
    for key, (local, tilt) in HUB_FRAMES.items():
        if key in by_key:
            placements[key] = (hub, local, tilt, 'hub')
    planet_html = []
    for number, planet in PLANETS.items():
        members = [frame for frame in frames if frame['cluster'] == number]
        centre = {'x': planet['at'][0], 'y': planet['at'][1], 'rot': planet['rot']}
        nodes[f'p{number}'] = {**centre, 'w': 2 * PLANET_R, 'h': 2 * PLANET_R, 'kind': 'planet'}
        positions = ring(len(members)) if members else []
        for i, (frame, local) in enumerate(zip(members, positions)):
            placements[frame['key']] = (centre, local, FRAME_TILT[i % len(FRAME_TILT)], f'p{number}')
        planet_html.append(planet_markup(number, members, positions))
    for key, (parent, local, tilt, parent_id) in placements.items():
        box = place(parent, local, tilt)
        nodes[key] = {**box, 'w': 2 * FRAME_R, 'h': 2 * FRAME_R, 'kind': 'frame', 'parent': parent_id}
        for kind, x in zip(PORTAL_KINDS, PORTAL_XS):
            centre = place(box, (x, PORTAL_Y), 0)
            w, h = diagram_box(by_key[key]['diagram']['ratio']) if kind == 'diagram' else (PORTAL_W, PORTAL_H)
            nodes[f'{key}.{kind}'] = {**centre, 'w': round(w, 2), 'h': round(h, 2), 'kind': kind, 'parent': key}
    covers = ''.join(cover_markup(number) for number in PLANETS)
    world = (route_layer('route-lines') + orbit_layer() + hub_markup(by_key) + ''.join(planet_html)
             + covers + route_layer('route'))
    return world, nodes


def ordered_keys(frames):
    """Story order: 0-1, the four technical planets, 0-2, then Part 2 and Part 3."""
    keys = [frame['key'] for frame in frames]
    middle = [key for key in keys if key[0] in '1234']
    return (['0-1'] if '0-1' in keys else []) + middle + (['0-2'] if '0-2' in keys else []), \
        [key for key in keys if key[0] == '5'], [key for key in keys if key[0] == '6']


def steps_and_paths(frames, nodes):
    by_key = {frame['key']: frame for frame in frames}
    steps = [{'id': 'overview', 'kind': 'overview', 'node': 'overview', 'label': '조감도'},
             {'id': 'closing', 'kind': 'closing', 'node': 'closing', 'label': '네 영역의 결과'},
             {'id': 'hub', 'kind': 'hub', 'node': 'hub', 'label': '표지 · 중심 질문'}]
    for number, planet in PLANETS.items():
        steps.append({'id': f'p{number}', 'kind': 'planet', 'node': f'p{number}', 'planet': number,
                      'label': f'{number:02d} {planet["name"]}'})
    for frame in frames:
        key = frame['key']
        if key not in nodes:
            continue
        planet = int(key[0]) or None
        steps.append({'id': key, 'kind': 'frame', 'node': key, 'frame': key, 'planet': planet,
                      'label': f'{key} {frame["label"]}'})
        for kind in PORTAL_KINDS:
            steps.append({'id': f'{key}.{kind}', 'kind': kind, 'node': f'{key}.{kind}', 'frame': key,
                          'planet': planet, 'label': f'{key} {PORTAL_NAMES[kind]}'})
    known = {step['id'] for step in steps}

    def brief(order):
        sequence = ['overview', 'hub']
        for key in order:
            detail = 'diagram' if by_key[key]['level'] < 3 else 'code'
            sequence += [key, f'{key}.{detail}']
        return sequence + ['closing']

    def full(order, opening=True, returns=True):
        sequence, seen = (['overview', 'hub'] if opening else []), set()
        for key in order:
            number = int(key[0])
            if number and number not in seen:
                if returns and seen:
                    sequence.append('overview')
                sequence.append(f'p{number}')
                seen.add(number)
            sequence += [key] + [f'{key}.{kind}' for kind in PORTAL_KINDS]
        return sequence

    main, playbook, scale = ordered_keys(frames)
    natural = {frame['key']: frame['minutes'] for frame in frames}
    paths = {}
    for name, label, total, description in PATHS:
        if name in ('interview', 'deep'):
            order = [key for key in (INTERVIEW if name == 'interview' else DEEP) if key in by_key]
            sequence = brief(order)
        elif name == 'part1':
            order = main
            sequence = full(order[:-1]) + full(order[-1:], opening=False, returns=False) + ['closing']
        elif name == 'part2':
            order = playbook
            sequence = ['overview'] + full(order, opening=False) + ['overview']
        elif name == 'part3':
            order = scale
            sequence = ['overview'] + full(order, opening=False) + ['overview']
        else:
            order = main + playbook + scale
            sequence = (full(main[:-1]) + full(main[-1:], opening=False, returns=False) + ['closing']
                        + full(playbook, opening=False) + ['overview'] + full(scale, opening=False) + ['overview'])
        if not order:
            continue
        missing = [step for step in sequence if step not in known]
        if missing:
            raise ValueError(f'path {name} names unknown steps {missing}')
        minutes = sum(natural[key] for key in order)
        paths[name] = {'label': label, 'description': description, 'steps': sequence,
                       'minutes': total or minutes, 'factor': (total or minutes) / minutes,
                       'frames': order}
    return steps, paths


# ---------------------------------------------------------------- Q&A index

def appendix_section(appendix, title):
    match = re.search(rf'^## {re.escape(title)}\n([\s\S]*?)(?=^## |\Z)', appendix, re.MULTILINE)
    return match[1] if match else ''


def qa_index(appendix, render):
    """Map each frame key to the follow-up questions, roleplays and glossary terms that cite it."""
    index = {}
    questions = appendix_section(appendix, '면접 후속 질문과 설계 판단의 근거')
    for question, answer in re.findall(r'^\| (?!질문 \|)(?!-)(.+?) \| (.+) \|$', questions, re.MULTILINE):
        for key in sorted(set(re.findall(r'\b(\d-\d)\b', answer))):
            index.setdefault(key, []).append(
                f'<div class="qa"><p class="qa-q">{escape(question)}</p>{render(answer)}</div>')
    roleplays = appendix_section(appendix, '고객 상황 롤플레이')
    for match in re.finditer(r'^### (\d+)\. (.+)\n([\s\S]*?)(?=^### |\Z)', roleplays, re.MULTILINE):
        body = match[3]
        for key in sorted(set(re.findall(r'\b(\d-\d)\b', body))):
            index.setdefault(key, []).append(
                f'<div class="qa roleplay"><p class="qa-q">롤플레이 {match[1]}. {escape(match[2])}</p>'
                f'{render(body)}</div>')
    glossary = appendix_section(appendix, '용어집')
    for term, meaning, keys in re.findall(r'^\| (?!용어 \|)(?!-)(.+?) \| (.+?) \| (.+?) \|$', glossary, re.MULTILINE):
        for key in sorted(set(re.findall(r'\b(\d-\d)\b', keys))):
            index.setdefault(key, []).append(
                f'<div class="qa glossary"><p class="qa-q">용어 · {render(term)[3:-4]}</p>{render(meaning)}</div>')
    return index


def render(frames, appendix, render_markdown, revision):
    """Return (world_html, atlas_json) for the presentation template."""
    world, nodes = scene(frames)
    steps, paths = steps_and_paths(frames, nodes)
    qa = qa_index(appendix, render_markdown)
    data = {
        'revision': revision,
        'planets': {str(n): {k: p.get(k) for k in ('name', 'en', 'tagline', 'outcome', 'proof', 'part')}
                    for n, p in PLANETS.items()},
        'parts': PARTS,
        'frames': {frame['key']: {
            'title': frame['title'], 'kicker': frame['kicker'], 'label': frame['label'],
            'message': frame['message'], 'points': frame['points'], 'minutes': frame['minutes'],
            'cluster': frame['cluster'], 'level': frame['level'], 'speaker': frame['notes'],
            'analysis': frame['analysis'], 'layout': frame['layout'], 'data': frame['data_html'],
            'qa': ''.join(qa.get(frame['key'], [])),
        } for frame in frames},
        'nodes': nodes, 'steps': steps, 'paths': paths,
    }
    # Escape "</" so the JSON can never close the surrounding <script> element.
    return world, json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
