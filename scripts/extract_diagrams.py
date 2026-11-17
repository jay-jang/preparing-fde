"""Extract editable Mermaid diagrams and a Prezi asset index from the handout."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT/'docs/fde-prezi-ko.md').read_text(encoding='utf-8')
out = ROOT/'assets/diagrams'
out.mkdir(parents=True, exist_ok=True)
index = []
headings = re.findall(r'^## Frame (\d-\d) ', source, re.MULTILINE)
for frame, text in re.findall(r'^## Frame (\d-\d) ([\s\S]*?)(?=^## |\Z)', source, re.MULTILINE):
    diagrams = re.findall(r'```mermaid\n([\s\S]*?)\n```', text)
    if len(diagrams) != 1:
        raise ValueError(f'expected one diagram for {frame}')
    filename = f'frame-{frame}'
    (out/(filename+'.mmd')).write_text(diagrams[0]+'\n', encoding='utf-8')
    index.append({'frame': frame, 'source': filename+'.mmd', 'png': filename+'.png', 'svg': filename+'.svg',
                  'title': text.splitlines()[0].strip(' |')})
if not index or len(index) != len(headings):
    raise ValueError('every frame needs exactly one Mermaid diagram')
(out/'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(f'Extracted {len(index)} diagrams to {out}')
