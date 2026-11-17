"""Build the Prezi-style zoom atlas (index.html) and the handout reader (reader.html) without runtime CDNs."""
import argparse
from html import escape
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

import markdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas  # noqa: E402  (sibling module)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build/pages'
SOURCE = ROOT / 'docs/fde-prezi-ko.md'
FRAME = re.compile(r'^## Frame (\d-\d) \| (Level ([123])[^|]*?) \| (.+)$', re.MULTILINE)
MARKER = re.compile(r'^\*\*\[([^\]\n(]+?) \(([^)\n]+)\)\]\*\*$', re.MULTILINE)
SECTIONS = {'화면 핵심': 'onscreen', '화면 시각화 가이드': 'layout', '핵심 엔지니어링 분석': 'analysis',
            '근거 데이터': 'data', '실전 코드': 'code', '발표자 노트': 'speaker'}
ORDER = ['onscreen', 'layout', 'analysis', 'data', 'code', 'speaker']
FENCE = re.compile(r'(<!-- deck -->\n+)?```(\w+)\n([\s\S]*?)\n```')


def render(text):
    result = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    result = re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', result)
    return re.sub(r'(<table>[\s\S]*?</table>)', r'<div class="table-scroll">\1</div>', result)


def inline(text):
    """Render one table cell of Markdown without the surrounding paragraph."""
    return re.sub(r'^<p>([\s\S]*)</p>$', r'\1', render(text).strip())


def png_size(path):
    header = path.read_bytes()[:24]
    if header[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'{path} is not a PNG')
    return struct.unpack('>II', header[16:24])


def parse_onscreen(key, text):
    kicker = re.search(r'^`([^`\n]+)` · \*\*([^*\n]+)\*\*$', text, re.MULTILINE)
    message = ' '.join(line[1:].strip() for line in text.splitlines() if line.startswith('>'))
    points = [line[2:].strip() for line in text.splitlines() if line.startswith('- ')]
    if not kicker or not message or len(points) != 3:
        raise ValueError(f'frame {key}: on-screen block needs `kicker` · **label**, a > message and three - points')
    return kicker[1], kicker[2], message, points


def parse_data(key, text):
    rows = [line for line in text.splitlines() if line.startswith('|')]
    if len(rows) < 6 or rows[0] != '| 항목 | 값 | 근거 |':
        raise ValueError(f'frame {key}: evidence data needs a | 항목 | 값 | 근거 | table with at least 4 rows')
    cells = [[cell.strip() for cell in row.strip('|').split(' | ')] for row in rows[2:]]
    if any(len(row) != 3 for row in cells):
        raise ValueError(f'frame {key}: every evidence row needs three cells')
    return [{'item': inline(item), 'value': inline(value), 'basis': inline(basis)} for item, value, basis in cells]


def parse_frame(key, level, level_label, title, body):
    found = list(MARKER.finditer(body))
    names = [SECTIONS.get(match[1]) for match in found]
    if names != ORDER:
        raise ValueError(f'frame {key}: sections must be {ORDER}, found {names}')
    sections = {name: body[match.end():(found[i + 1].start() if i + 1 < len(found) else len(body))].strip()
                for i, (name, match) in enumerate(zip(names, found))}
    kicker, label, message, points = parse_onscreen(key, sections['onscreen'])
    data = parse_data(key, sections['data'])
    minutes = re.search(r'발표 시간 (\d+)분', sections['layout'])
    mermaid = re.search(r'```mermaid\n([\s\S]*?)\n```', sections['layout'])
    image = ROOT / f'assets/diagrams/frame-{key}.png'
    if not minutes or not mermaid or not image.is_file():
        raise ValueError(f'frame {key}: missing time, Mermaid diagram or rendered PNG')
    blocks = [match for match in FENCE.finditer(sections['code'])]
    if not blocks:
        raise ValueError(f'frame {key}: missing code block')
    chosen = next((block for block in blocks if block[1]), blocks[0])
    # Label the card's code with a repository file only when the named file's type matches the block.
    extensions = {'python': ('.py',), 'sql': ('.sql',), 'yaml': ('.yaml', '.yml')}.get(chosen[2], ())
    source = next((match for match in re.finditer(r'`((?:fde|sql|deploy|scripts|tests|\.github)/[\w./-]+)`',
                                                   sections['code']) if match[1].endswith(extensions)), None)
    width, height = png_size(image)
    diagram = {'src': f'assets/diagrams/frame-{key}.png', 'w': width, 'h': height, 'ratio': width / height}
    vector = image.with_suffix('.svg')
    if vector.is_file():
        svg = vector.read_text(encoding='utf-8')
        box = re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', svg)
        diagram.update(svg=svg, ratio=float(box[1]) / float(box[2]))
    layout_text = sections['layout'][:mermaid.start()] + sections['layout'][mermaid.end():]
    return {
        'key': key, 'cluster': int(key[0]), 'level': int(level), 'level_label': level_label.strip(),
        'title': title.strip(), 'kicker': kicker, 'label': label, 'message': message, 'points': points,
        'data': data, 'data_html': render(sections['data']),
        'minutes': int(minutes[1]), 'mermaid': mermaid[1], 'sections': sections, 'body': body,
        'diagram': diagram,
        'code': {'lang': chosen[2], 'text': chosen[3], 'source': source[1] if source else ''},
        'notes': render(sections['speaker']), 'analysis': render(sections['analysis']),
        'layout': render(layout_text),
    }


def parse(text):
    matches = list(FRAME.finditer(text))
    if not matches:
        raise ValueError('no frames found')
    frames = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end]
        if index + 1 == len(matches):
            tail = re.search(r'^## (?!Frame )', body, re.MULTILINE)
            body, appendix = (body[:tail.start()], body[tail.start():]) if tail else (body, '')
        elif re.search(r'^## (?!Frame )', body, re.MULTILINE):
            raise ValueError(f'frame {match[1]}: unexpected section between frames')
        frames.append(parse_frame(match[1], match[3], match[2], match[4], body))
    keys = [frame['key'] for frame in frames]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate frame keys')
    return text[:matches[0].start()], frames, appendix


def reader_article(frame):
    key, sections = frame['key'], frame['sections']
    points = ''.join(f'<li>{escape(point)}</li>' for point in frame['points'])
    onscreen = (f'<div class="onscreen"><p class="onscreen-label">화면 핵심 · {escape(frame["kicker"])} · '
                f'{escape(frame["label"])}</p>'
                f'<p class="onscreen-message">{escape(frame["message"])}</p><ol>{points}</ol></div>')
    figure = (f'<figure><a href="{frame["diagram"]["src"]}" target="_blank" rel="noopener" '
              f'aria-label="프레임 {key} 도식 크게 보기"><img src="{frame["diagram"]["src"]}" '
              f'width="{frame["diagram"]["w"]}" height="{frame["diagram"]["h"]}" '
              f'alt="{escape(frame["title"], quote=True)} 아키텍처 도식" loading="lazy"></a>'
              '<figcaption>도식을 선택하면 크게 볼 수 있습니다.</figcaption></figure>\n'
              '<details class="diagram-source"><summary>Mermaid 원본 보기</summary><pre><code>'
              + escape(frame['mermaid']) + '</code></pre></details>\n')
    layout = re.sub(r'```mermaid\n[\s\S]*?\n```', lambda _: figure, sections['layout'], count=1)
    return (f'<article class="frame cluster-{frame["cluster"]}" id="frame-{key}" data-frame="{key}">'
            f'<div class="frame-meta"><span>FRAME {key}</span><span>{escape(frame["level_label"])} · '
            f'{frame["minutes"]}분 · <a href="index.html#onboarding~{key}">캔버스에서 보기 ↗</a></span></div>'
            f'<h2>{escape(frame["title"])}</h2>{onscreen}<h3>화면 시각화 가이드</h3>{render(layout)}'
            f'<h3>핵심 엔지니어링 분석</h3>{frame["analysis"]}<h3>근거 데이터</h3>{frame["data_html"]}'
            f'<h3>실전 코드</h3>{render(sections["code"])}'
            f'<details class="speaker"><summary>발표자 노트 펼치기</summary>'
            f'<div>{frame["notes"]}</div></details></article>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE, help='handout Markdown to build from')
    arguments = parser.parse_args()
    source = arguments.source.read_text(encoding='utf-8')
    intro, frames, appendix = parse(source)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    minutes = {part: sum(frame['minutes'] for frame in frames if frame['cluster'] in clusters)
               for part, clusters in (('main', (0, 1, 2, 3, 4)), ('playbook', (5,)), ('scale', (6,)))}
    values = {'{{REVISION}}': revision[:7], '{{FRAME_COUNT}}': str(len(frames)),
              '{{MAIN_MINUTES}}': str(minutes['main']), '{{PLAYBOOK_MINUTES}}': str(minutes['playbook']),
              '{{SCALE_MINUTES}}': str(minutes['scale'])}

    navigation = '\n'.join(f'<a href="#frame-{f["key"]}" data-frame="{f["key"]}"><span>{f["key"]}</span>'
                           f'{escape(f["title"])}</a>' for f in frames)
    articles = '\n'.join(reader_article(frame) for frame in frames)
    if appendix:
        articles += ('<section class="appendix" id="appendix"><div class="section-label">REFERENCE</div>'
                     + render(appendix) + '</section>')
    reader_values = {**values, '{{NAVIGATION}}': navigation, '{{CONTENT}}': articles,
                     '{{BACKGROUND}}': render(intro.split('\n', 1)[1])}
    world, data = atlas.render(frames, appendix, render, revision[:7])
    atlas_values = {**values, '{{WORLD}}': world, '{{ATLAS_DATA}}': data}

    # Start from an empty directory so files of an earlier viewer never linger on the site.
    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.mkdir(parents=True)
    for template, output, substitutions in (('reader.html', 'reader.html', reader_values),
                                            ('atlas.html', 'index.html', atlas_values)):
        page = (ROOT / 'web' / template).read_text(encoding='utf-8')
        for token, content in substitutions.items():
            page = page.replace(token, content)
        if re.search(r'\{\{[A-Z_]+\}\}', page):
            raise ValueError(f'unexpanded placeholder in {template}')
        (OUTPUT / output).write_text(page, encoding='utf-8')
    for name in ('style.css', 'app.js', 'atlas.css', 'atlas.js', 'favicon.svg'):
        shutil.copy2(ROOT / 'web' / name, OUTPUT / name)
    shutil.copytree(ROOT / 'web/fonts', OUTPUT / 'fonts', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'assets/diagrams', OUTPUT / 'assets/diagrams', dirs_exist_ok=True)
    downloads = OUTPUT / 'downloads'
    downloads.mkdir(exist_ok=True)
    shutil.copy2(arguments.source, downloads / SOURCE.name)
    shutil.copy2(ROOT / 'deliverables/fde-prezi-kit.zip', downloads / 'fde-prezi-kit.zip')
    (OUTPUT / '.nojekyll').touch()
    (OUTPUT / 'revision.txt').write_text(revision + '\n', encoding='utf-8')
    print(f'Built {OUTPUT}: {len(frames)} frames, zoom atlas and reader, revision {revision[:7]}')


if __name__ == '__main__':
    main()
