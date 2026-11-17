"""Package the handout, diagrams, verified code and the built zoom deck into deliverables/fde-prezi-kit.zip.

Run scripts/build_pages.py first. The offline copy keeps the handout Markdown but not the archive itself,
and the new archive is also copied into build/pages/downloads so the site never serves a stale kit.
"""
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'deliverables/fde-prezi-kit.zip'
SOURCES = ['README.md', 'requirements.txt', 'requirements-pages.txt', '.github/workflows/check.yml',
           'docs', 'fde', 'sql', 'deploy', 'tests', 'scripts', 'assets/diagrams', 'web']
# A fixed timestamp keeps the archive byte-identical when the inputs have not changed.
STAMP = (2026, 11, 17, 0, 0, 0)


def files(path):
    if path.is_file():
        yield path
    else:
        yield from sorted(p for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts)


def main():
    deck = ROOT / 'build/pages'
    if not (deck / 'index.html').is_file():
        raise SystemExit('build the pages first: python scripts/build_pages.py')
    entries = [(source, 'fde-prezi-kit/' + source.relative_to(ROOT).as_posix())
               for name in SOURCES for source in files(ROOT / name)]
    published = [source for source in files(deck) if source.relative_to(deck).parts[0] != 'downloads']
    published.append(deck / 'downloads' / 'fde-prezi-ko.md')
    entries += [(source, 'fde-prezi-kit/presentation/' + source.relative_to(deck).as_posix()) for source in published]
    TARGET.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(TARGET, 'w', zipfile.ZIP_DEFLATED) as archive:
        for source, name in entries:
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())
    (deck / 'downloads').mkdir(exist_ok=True)
    shutil.copy2(TARGET, deck / 'downloads' / TARGET.name)
    print(f'Wrote {TARGET.relative_to(ROOT)}: {len(entries)} files')


if __name__ == '__main__':
    main()
