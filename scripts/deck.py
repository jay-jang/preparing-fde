"""Render the zoomable Prezi-style canvas from parsed presentation frames.

The canvas is laid out in CSS pixels. Clusters are 2 x 2 grids: a chapter label and three frame cards.
Each card has camera targets for the whole card, its diagram and its code; deck.js computes the rectangles
from the DOM, so only the cluster positions are fixed here.
"""
from html import escape
import json
import re
import unicodedata

CARD_W, CARD_H, GAP, PAD = 2700, 1520, 320, 170
CLUSTER_W = PAD * 2 + CARD_W * 2 + GAP
CLUSTER_H = PAD * 2 + CARD_H * 2 + GAP
MARGIN, SPINE_W, TITLE_H, ROW_GAP, ISLAND_GAP = 700, 1300, 2700, 420, 1500
MAIN_W = MARGIN * 2 + CLUSTER_W * 2 + SPINE_W
ROW_1 = TITLE_H
ROW_2 = TITLE_H + CLUSTER_H + ROW_GAP
MAIN_H = ROW_2 + CLUSTER_H + MARGIN
PART2_X = MAIN_W + ISLAND_GAP
CANVAS_W = PART2_X + CLUSTER_W + MARGIN
CANVAS_H = MAIN_H

CLUSTERS = {
    1: ('데이터 통합과 온톨로지', 'Ingestion & Ontology', '같은 설비를 같은 객체로, 재전송에도 같은 상태로',
        (MARGIN, ROW_1), '재생 가능한 상태'),
    2: ('RAG와 에이전트', 'Retrieval, Grounding & Agents', '허용된 근거 안에서 찾고, 출처를 검증해 답한다',
        (MARGIN + CLUSTER_W + SPINE_W, ROW_1), '허용된 근거'),
    3: ('폐쇄망과 거버넌스', 'Air-gap & Governance', '인터넷 없이 실행되고, 경계와 기록이 남는 배포',
        (MARGIN, ROW_2), '검증된 실행물'),
    4: ('현장 MVP와 운영', 'Prototype to Production', '작게 증명하고, 게이트를 통과한 범위만 넓힌다',
        (MARGIN + CLUSTER_W + SPINE_W, ROW_2), '운영 가능한 책임'),
    5: ('현장 플레이북', 'FDE Field Playbook', '고객과 합의하고, 재현하고, 용량을 계산한다',
        (PART2_X, (ROW_1 + ROW_2 + CLUSTER_H) // 2 - CLUSTER_H // 2 + 300), None),
}
SPINE = [('Identity', '누가 요청했나'), ('Policy', '무엇이 허용되나'), ('Lineage', '어디서 왔나'),
         ('Observability', '무슨 일이 있었나')]
PATHS = {
    'interview': ('면접 15분', ['0-1', '1-1', '1-2', '2-1', '2-2', '2-3', '3-1', '4-1', '0-2'], 15),
    'onboarding': ('온보딩 전체', None, None),
    'playbook': ('현장 플레이북', ['5-1', '5-2', '5-3'], None),
}
KEYWORDS = {
    'python': r'\b(?:import|from|as|def|return|if|else|elif|for|in|with|not|and|or|raise|try|except|class|'
              r'lambda|None|True|False)\b',
    'sql': r'\b(?:INSERT|INTO|VALUES|CREATE|POLICY|ON|FOR|SELECT|TO|USING|AND|NOT|EXISTS|FROM|WHERE|'
           r'UPDATE|SET|CONFLICT|DO|true|false)\b',
    'yaml': r'[A-Za-z_][\w.\-]*(?=:(?:\s|$))',
}
COMMENT = {'python': r'#[^\n]*', 'yaml': r'#[^\n]*', 'sql': r'--[^\n]*'}


def highlight(code, language):
    """Return escaped code with a few token classes; unknown languages are only escaped."""
    if language not in KEYWORDS:
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
    return ''.join(parts)


def code_font(code, width, height):
    """Largest font (px) at which every line fits the code panel without wrapping or clipping."""
    lines = code.split('\n')
    columns = max(sum(1.0 if unicodedata.east_asian_width(c) in 'WF' else .6 for c in line) for line in lines)
    return max(14, min(30, int(min(width / max(columns, 1), height / (len(lines) * 1.5)) * .94)))


def diagram_markup(frame):
    """Inline the vector diagram when it exists (crisp at every zoom), else fall back to the PNG."""
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


def card(frame):
    key, level = frame['key'], frame['level']
    points = ''.join(f'<li><span>{index:02d}</span>{escape(point)}</li>'
                     for index, point in enumerate(frame['points'], 1))
    code = frame['code']
    # Approximate code panel size from the card grid in deck.css; code_font leaves some slack.
    font = code_font(code['text'], 1100, 740)
    source = f'<span>{escape(code["source"])}</span>' if code['source'] else ''
    diagram = frame['diagram']
    return (
        f'<article class="card level-{level}" id="card-{key}" data-frame="{key}">'
        f'<div class="card-meta"><span>{key} · {escape(frame["kicker"])}</span>'
        f'<span class="card-minutes">{frame["minutes"]}분</span></div>'
        f'<h3>{escape(frame["title"])}</h3>'
        f'<p class="message">{escape(frame["message"])}</p>'
        f'<div class="card-body"><div class="card-left"><ol class="points">{points}</ol>'
        f'<figure class="diagram" data-ratio="{diagram["ratio"]:.4f}">'
        f'<div class="diagram-box" id="diagram-{key}">{diagram_markup(frame)}</div></figure></div>'
        f'<div class="code" id="panel-{key}"><div class="code-focus" id="code-{key}"><div class="code-head">'
        f'<span>{escape(code["lang"])}</span>{source}</div>'
        f'<pre style="font-size:{font}px"><code>{highlight(code["text"], code["lang"])}</code></pre></div></div>'
        '</div></article>')


def cluster(number, frames):
    name, english, tagline, (x, y), outcome = CLUSTERS[number]
    members = [frame for frame in frames if frame['cluster'] == number]
    toc = ''.join(f'<li><span>{f["key"]}</span>{escape(f["title"])}</li>' for f in members)
    cards = ''.join(card(frame) for frame in members)
    overlay = f'<div class="outcome" aria-hidden="true"><span>0-2</span>{escape(outcome)}</div>' if outcome else ''
    return (
        f'<section class="cluster c{number}{" part2" if number == 5 else ""}" id="cluster-{number}" '
        f'style="left:{x}px;top:{y}px;'
        f'width:{CLUSTER_W}px;height:{CLUSTER_H}px" aria-label="{number:02d} {escape(name, quote=True)}">'
        f'<header class="chapter"><div class="chapter-number">{number:02d}</div>'
        f'<h2>{escape(name)}</h2><p class="chapter-english">{escape(english)}</p>'
        f'<p class="chapter-tagline">{escape(tagline)}</p><ol class="chapter-toc">{toc}</ol></header>'
        f'{cards}{overlay}</section>')


def arrows():
    """Overview connectors drawn over the spine; the overview step animates them."""
    c = {n: CLUSTERS[n][3] for n in range(1, 5)}
    left_end, right_start = c[1][0] + CLUSTER_W, c[2][0]
    x_left, x_right = c[1][0] + CLUSTER_W // 2, c[2][0] + CLUSTER_W // 2
    gap_top, gap_bottom = ROW_1 + CLUSTER_H, ROW_2
    low_1, low_4 = ROW_1 + CLUSTER_H - 520, ROW_2 + 520
    paths = [
        ('flow', f'M{left_end - 60},{ROW_1 + 300} L{right_start + 10},{ROW_1 + 300}'),
        ('flow', f'M{x_right},{gap_top - 40} L{x_right},{gap_bottom + 10}'),
        ('feedback', f'M{right_start + 60},{low_4} C{right_start - 420},{low_4} {left_end + 420},{low_1} {left_end - 10},{low_1}'),
        ('platform', f'M{x_left},{gap_bottom + 40} L{x_left},{gap_top - 10}'),
    ]
    halos = ''.join(f'<path class="halo" d="{path}"/>' for _, path in paths)
    marks = ''.join(f'<path class="arrow {kind}" d="{path}" marker-end="url(#head-{kind})"/>' for kind, path in paths)
    defs = ''.join(
        f'<marker id="head-{kind}" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="3.4" markerHeight="3.4" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="head {kind}"/></marker>'
        for kind in ('flow', 'feedback', 'platform'))
    return (f'<svg class="connectors" width="{MAIN_W}" height="{MAIN_H}" viewBox="0 0 {MAIN_W} {MAIN_H}" '
            f'aria-hidden="true"><defs>{defs}</defs>{halos}{marks}</svg>')


def spine():
    x, top = MARGIN + CLUSTER_W, ROW_1 + 640
    height = ROW_2 + CLUSTER_H - top
    pillars = ''.join(f'<div><strong>{escape(name)}</strong><span>{escape(korean)}</span></div>'
                      for name, korean in SPINE)
    return (f'<div class="spine" style="left:{x + 150}px;top:{top}px;width:{SPINE_W - 300}px;height:{height}px">'
            f'{pillars}</div>')


def title_band(frames):
    by_key = {frame['key']: frame for frame in frames}
    overview_cards = ''.join(card(by_key[key]) for key in ('0-1', '0-2'))
    return (
        f'<section class="title-band" id="title" style="left:{MARGIN}px;top:{MARGIN - 160}px">'
        '<p class="eyebrow"><span></span>FDE 면접 · 온보딩 기술 발표</p>'
        '<h1>고객의 데이터에서,<br><em>운영 가능한 AI까지.</em></h1>'
        '<p class="central">이 답변을 어떤 데이터와 권한으로 만들었고,<br>장애가 난 뒤에도 같은 사실을 재현할 수 있는가?</p>'
        '<p class="case"><span>CASE</span>“부산 공장 P-101의 반복 과열 원인과 점검 순서는?”</p></section>'
        f'<div class="overview-cards" style="left:{MAIN_W - MARGIN - CARD_W * 2 - GAP}px;top:{MARGIN - 160}px;'
        f'width:{CARD_W * 2 + GAP}px">{overview_cards}</div>')


def steps_and_paths(frames):
    """Camera stops per frame (card, diagram, code) and the three presenter paths."""
    steps = [{'id': 'title', 'target': 'title', 'label': '표지', 'frame': None, 'kind': 'title'},
             {'id': 'overview', 'target': 'main-island', 'label': '조감도', 'frame': '0-1', 'kind': 'overview'},
             {'id': 'closing', 'target': 'main-island', 'label': '0-2 조감도 · 네 영역의 결과', 'frame': '0-2',
              'kind': 'overview'},
             {'id': 'canvas', 'target': 'canvas-bounds', 'label': '전체 캔버스', 'frame': None, 'kind': 'overview'}]
    for number in range(1, 6):
        steps.append({'id': f'c{number}', 'target': f'cluster-{number}', 'frame': None, 'kind': 'cluster',
                      'label': f'{number:02d} {CLUSTERS[number][0]}'})
    levels = {}
    for frame in frames:
        key = frame['key']
        levels[key] = frame['level']
        for suffix, target, kind in (('', 'card', 'card'), ('.diagram', 'diagram', 'diagram'), ('.code', 'code', 'code')):
            label = {'card': frame['title'], 'diagram': '도식', 'code': '코드'}[kind]
            steps.append({'id': key + suffix, 'target': f'{target}-{key}', 'frame': key, 'kind': kind,
                          'label': f'{key} · {label}'})
    keys = [frame['key'] for frame in frames]

    def stops(key, brief):
        """Card, then its detail. The brief interview path zooms into one detail per frame:
        the diagram for Level 1-2 (system contracts) and the code for Level 3 (the invariant)."""
        if not brief:
            return [key, f'{key}.diagram', f'{key}.code']
        return [key, f'{key}.diagram' if levels[key] < 3 else f'{key}.code']

    def expand(order, overview_returns, brief=False):
        result, cluster_seen = ['title'], set()
        for key in order:
            number = int(key[0])
            if key == '0-1':
                result.append('overview')
            elif key == '0-2':
                # 0-2 returns to the whole canvas: the card first, then the overview with the four outcomes.
                result.extend(stops(key, brief) + ['closing'])
                continue
            elif number not in cluster_seen:
                if overview_returns and cluster_seen - {5} and number != 5:
                    result.append('overview')
                if not brief:
                    result.append(f'c{number}')
                cluster_seen.add(number)
            result.extend(stops(key, brief))
        return result

    main = [key for key in keys if key[0] != '5' and key != '0-2'] + ['0-2']
    playbook = [key for key in keys if key[0] == '5']
    minutes = {frame['key']: frame['minutes'] for frame in frames}
    paths = {}
    for name, (label, order, total) in PATHS.items():
        if name == 'onboarding':
            sequence = expand(main, True) + expand(playbook, False)[1:] + ['canvas']
            order = main + playbook
        elif not all(key in levels for key in order):
            raise ValueError(f'path {name} names a missing frame')
        elif name == 'playbook':
            sequence = expand(order, False)[1:] + ['canvas']
        else:
            sequence = expand(order, False, brief=True)
        natural = sum(minutes[key] for key in order)
        paths[name] = {'label': label, 'steps': sequence, 'minutes': total or natural,
                       'factor': (total or natural) / natural}
    return steps, paths


def render(frames, appendix_html, revision):
    """Return (canvas_html, deck_json) for the presentation template."""
    clusters = ''.join(cluster(number, frames) for number in range(1, 6))
    part2 = CLUSTERS[5][3]
    canvas = (
        f'<div class="canvas" id="canvas" style="width:{CANVAS_W}px;height:{CANVAS_H}px">'
        f'<div class="bounds" id="canvas-bounds" style="left:0;top:0;width:{CANVAS_W}px;height:{CANVAS_H}px"></div>'
        f'<div class="bounds" id="main-island" style="left:0;top:0;width:{MAIN_W}px;height:{MAIN_H}px"></div>'
        f'<div class="island" style="left:{MARGIN // 2}px;top:{MARGIN // 2}px;width:{MAIN_W - MARGIN}px;'
        f'height:{MAIN_H - MARGIN}px"></div>'
        f'<div class="island part2" style="left:{part2[0] - MARGIN // 2}px;top:{part2[1] - 1500}px;'
        f'width:{CLUSTER_W + MARGIN}px;height:{CLUSTER_H + 1500 + MARGIN // 2}px"></div>'
        f'{spine()}{arrows()}{title_band(frames)}'
        f'<p class="part-label part2" style="left:{part2[0]}px;top:{part2[1] - 980}px"><span>PART 2</span>'
        '현장에서 경계를 합의하고 지키기</p>'
        f'{clusters}</div>')
    steps, paths = steps_and_paths(frames)
    data = {
        'revision': revision,
        'frames': {frame['key']: {key: frame[key] for key in (
            'title', 'kicker', 'message', 'points', 'minutes', 'cluster', 'level', 'notes', 'analysis', 'layout')}
            for frame in frames},
        'steps': steps, 'paths': paths, 'appendix': appendix_html,
    }
    # Escape "</" so the JSON can never close the surrounding <script> element.
    return canvas, json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
