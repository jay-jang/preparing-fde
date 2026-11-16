"""Build the Korean presentation reader for GitHub Pages, without runtime CDNs."""
from html import escape
from pathlib import Path
import re
import shutil
import subprocess

import markdown

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build/pages'
SOURCE = ROOT / 'docs/fde-prezi-ko.md'


def render(text):
    result = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    return re.sub(r'(<table>[\s\S]*?</table>)', r'<div class="table-scroll">\1</div>', result)


def main():
    source = SOURCE.read_text(encoding='utf-8')
    parts = re.split(r'^## (Frame \d-\d[^\n]*)\n', source, flags=re.MULTILINE)
    if len(parts) != 29:
        raise ValueError('the presentation must contain all 14 frames')
    sections, navigation = [], []
    for index in range(1, len(parts), 2):
        heading, body = parts[index:index + 2]
        key, level, title = re.fullmatch(r'Frame (\d-\d) \| (Level [123][^|]*) \| (.+)', heading).groups()
        cluster = int(key[0])
        navigation.append(f'<a href="#frame-{key}" data-frame="{key}"><span>{key}</span>{escape(title)}</a>')
        # The appendix after the final frame is kept outside the speaker notes.
        tail = ''
        if key == '0-2':
            body, tail = re.split(r'(?=^## 면접 후속)', body, maxsplit=1, flags=re.MULTILINE)
        diagram = re.search(r'```mermaid\n([\s\S]*?)\n```', body)
        image = ROOT / f'assets/diagrams/frame-{key}.png'
        if diagram is None or not image.is_file():
            raise ValueError(f'missing diagram for frame {key}')
        figure = (f'<figure><a href="assets/diagrams/frame-{key}.png" target="_blank" rel="noopener" '
                  f'aria-label="프레임 {key} 도식 크게 보기"><img src="assets/diagrams/frame-{key}.png" '
                  f'alt="{escape(title, quote=True)} 아키텍처 도식" loading="lazy"></a>'
                  '<figcaption>도식을 선택하면 크게 볼 수 있습니다.</figcaption></figure>\n'
                  '<details class="diagram-source"><summary>Mermaid 원본 보기</summary><pre><code>'
                  + escape(diagram[1]) + '</code></pre></details>\n')
        body = body[:diagram.start()] + figure + body[diagram.end():]
        speaker_marker = '**[발표자 노트 (Speaker Script)]**'
        if speaker_marker not in body:
            raise ValueError(f'missing speaker notes for frame {key}')
        body, speaker = body.split(speaker_marker, 1)
        body = re.sub(r'^\*\*\[([^\n(]+) \([^\n]*\)\]\*\*$', r'### \1', body, flags=re.MULTILINE)
        sections.append(
            f'<article class="frame cluster-{cluster}" id="frame-{key}" data-frame="{key}">'
            f'<div class="frame-meta"><span>FRAME {key}</span><span>{escape(level.strip())}</span></div>'
            f'<h2>{escape(title)}</h2>{render(body)}'
            '<details class="speaker"><summary>발표자 노트 펼치기</summary>'
            f'<div>{render(speaker)}</div></details></article>')
        if tail:
            sections.append('<section class="appendix" id="appendix"><div class="section-label">REFERENCE</div>'
                            + render(tail) + '</section>')
    intro = parts[0].split('\n', 1)[1]
    template = (ROOT / 'web/template.html').read_text(encoding='utf-8')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    substitutions = {'{{NAVIGATION}}': '\n'.join(navigation), '{{CONTENT}}': '\n'.join(sections),
                     '{{BACKGROUND}}': render(intro), '{{REVISION}}': revision[:7]}
    for token, content in substitutions.items():
        template = template.replace(token, content)
    if re.search(r'\{\{[A-Z_]+\}\}', template):
        raise ValueError('unexpanded page template')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'index.html').write_text(template, encoding='utf-8')
    for name in ('style.css', 'app.js', 'favicon.svg'):
        shutil.copy2(ROOT / 'web' / name, OUTPUT / name)
    shutil.copytree(ROOT / 'assets/diagrams', OUTPUT / 'assets/diagrams', dirs_exist_ok=True)
    downloads = OUTPUT / 'downloads'
    downloads.mkdir(exist_ok=True)
    shutil.copy2(SOURCE, downloads / SOURCE.name)
    shutil.copy2(ROOT / 'deliverables/fde-prezi-kit.zip', downloads / 'fde-prezi-kit.zip')
    (OUTPUT / '.nojekyll').touch()
    (OUTPUT / 'revision.txt').write_text(revision + '\n', encoding='utf-8')
    print(f'Built {OUTPUT}: 14 frames, 14 diagrams, revision {revision[:7]}')


if __name__ == '__main__':
    main()
