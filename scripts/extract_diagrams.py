"""Extract editable Mermaid diagrams and a Prezi asset index from the handout."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT/'docs/fde-prezi-ko.md').read_text(encoding='utf-8')
out = ROOT/'assets/diagrams'
out.mkdir(parents=True, exist_ok=True)
index = []
for frame, text in re.findall(r'^## Frame ([0-4]-[1-3]) ([\s\S]*?)(?=^## |\Z)', source, re.MULTILINE):
    diagrams = re.findall(r'```mermaid\n([\s\S]*?)\n```', text)
    if len(diagrams) != 1:
        raise ValueError(f'expected one diagram for {frame}')
    filename = f'frame-{frame}'
    (out/(filename+'.mmd')).write_text(diagrams[0]+'\n', encoding='utf-8')
    index.append({'frame': frame, 'source': filename+'.mmd', 'png': filename+'.png',
                  'title': text.splitlines()[0].strip(' |')})
if len(index) != 14:
    raise ValueError('expected 14 frames')
(out/'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(f'Extracted {len(index)} diagrams to {out}')
