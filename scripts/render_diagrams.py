"""Render the extracted Mermaid sources to PNG (handout, Prezi import) and SVG (zoom deck) in the deck colours.

Requires Mermaid CLI (set MMDC to its path if it is not on PATH). PUPPETEER_CONFIG may point to a
puppeteer JSON config, e.g. to use a locally installed Chromium in an offline environment.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Border colour and node tint per cluster; failure nodes use the shared "fail" class.
PALETTE = {'0': ('#1b5b49', '#e9f0ea'), '1': ('#356584', '#eaf1f6'), '2': ('#26796d', '#e6f2ef'),
           '3': ('#7d648d', '#f1edf4'), '4': ('#9b6235', '#f6eee7'), '5': ('#5f7a2a', '#eff3e4')}


def config(cluster):
    border, tint = PALETTE[cluster]
    return {'theme': 'base',
            'themeVariables': {'fontFamily': 'Pretendard, "Noto Sans KR", sans-serif', 'fontSize': '18px',
                               'primaryColor': tint, 'primaryBorderColor': border, 'primaryTextColor': '#203630',
                               'lineColor': '#4d5d57', 'secondaryColor': '#f7f6f0', 'tertiaryColor': '#ffffff',
                               'clusterBkg': '#f7f6f0', 'clusterBorder': '#cfd2c4', 'edgeLabelBackground': '#ffffff',
                               'actorBkg': tint, 'actorBorder': border, 'signalColor': '#4d5d57',
                               'noteBkgColor': '#f7f6f0', 'noteBorderColor': '#cfd2c4'},
            'flowchart': {'htmlLabels': True, 'curve': 'basis', 'nodeSpacing': 36, 'rankSpacing': 50,
                          'padding': 14, 'wrappingWidth': 220},
            'sequence': {'mirrorActors': False, 'actorMargin': 60}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('frames', nargs='*', help='frame keys such as 1-2; default: every .mmd source')
    parser.add_argument('--dir', default=str(ROOT / 'assets/diagrams'))
    parser.add_argument('--scale', default='2', help='PNG device scale factor')
    parser.add_argument('--no-svg', action='store_true', help='skip the SVG used by the zoom deck')
    arguments = parser.parse_args()
    directory = Path(arguments.dir)
    mmdc = os.environ.get('MMDC') or shutil.which('mmdc')
    if not mmdc:
        raise SystemExit('Mermaid CLI not found; install @mermaid-js/mermaid-cli or set MMDC')
    sources = ([directory / f'frame-{key}.mmd' for key in arguments.frames] if arguments.frames
               else sorted(directory.glob('frame-*.mmd')))
    with tempfile.TemporaryDirectory() as temporary:
        for source in sources:
            cluster = source.stem.split('-')[1]
            settings = Path(temporary) / f'cluster-{cluster}.json'
            settings.write_text(json.dumps(config(cluster)), encoding='utf-8')
            base = [mmdc, '-i', str(source), '-c', str(settings), '-b', 'white', '-w', '1400']
            if os.environ.get('PUPPETEER_CONFIG'):
                base += ['-p', os.environ['PUPPETEER_CONFIG']]
            subprocess.run(base + ['-o', str(source.with_suffix('.png')), '-s', arguments.scale],
                           check=True, capture_output=True)
            if not arguments.no_svg:
                # A per-frame id keeps the inlined SVG styles and markers from colliding in one page.
                subprocess.run(base + ['-o', str(source.with_suffix('.svg')), '-I', f'mmd-{source.stem[6:]}'],
                               check=True, capture_output=True)
            print(f'rendered {source.stem}')


if __name__ == '__main__':
    main()
