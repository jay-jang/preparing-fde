"""Structure-aware chunking of parsed manual blocks, NOT a PDF parser or a tokenizer.

Input is the ordered block list a layout parser produced for one document version:
headings with a level, paragraphs, numbered inspection steps, a table header and
its rows, each with a page number. Invariants:

- A numbered procedure (consecutive steps 1..n; a new step 1 starts the next one)
  and a table (header plus rows) are atomic units and never cross a chunk boundary.
  A note between two steps splits the procedure and raises: the parser must attach
  notes and warnings to their step.
- A chunk never spans two sections; it carries doc ID, version, heading path and
  page span, so evaluation labels can use (document, version, section/page span)
  instead of chunk IDs that change with the strategy. Content before the first
  heading raises, so every chunk has a non-empty heading path.
- Boundaries fall only between sentences, steps or rows, so codes such as P-101
  or ZX-19 are never cut.
- A unit over the budget raises ValueError. Only split_oversized=True splits it,
  at sentence, step or row boundaries, repeating the table header in each part.
- IDs are deterministic: doc:vN:ordinal, plus a content hash to skip re-embedding.

estimate_tokens() is a deterministic budget estimate, not any model's tokenizer.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import re

KINDS = ('heading', 'paragraph', 'step', 'table_header', 'table_row')
_DOC_ID = re.compile(r'[A-Za-z0-9._-]{1,120}')
_STEP = re.compile(r'(\d{1,3})[.)] \S')
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


def estimate_tokens(text) -> int:
    """Per whitespace-separated word: ceil(ASCII chars / 4) + 1 per non-ASCII char.

    About four ASCII characters per token is a common English rule of thumb;
    one token per Hangul syllable is a conservative assumption. Calibrate the
    budget against the embedding model's tokenizer on a sample and keep a margin.
    """
    if type(text) is not str:
        raise TypeError('text must be str')
    total = 0
    for word in text.split():
        ascii_chars = sum(1 for ch in word if ord(ch) < 128)
        total += math.ceil(ascii_chars / 4) + len(word) - ascii_chars
    return total


@dataclass(frozen=True)
class Block:
    kind: str
    text: str
    page: int
    level: int = 0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f'unknown block kind: {self.kind!r}')
        if type(self.text) is not str or not self.text.strip() or len(self.text) > 20_000:
            raise ValueError('block text must be non-empty and bounded')
        if type(self.page) is not int or self.page < 1:
            raise ValueError('page must be a positive int')
        if type(self.level) is not int or (self.kind == 'heading') != (self.level >= 1):
            raise ValueError('only headings carry a level >= 1')


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    version: int
    ordinal: int
    heading_path: tuple[str, ...]
    pages: tuple[int, int]
    kinds: tuple[str, ...]
    part: tuple[int, int] | None
    text: str
    tokens: int
    content_hash: str

    @property
    def section(self) -> str:
        return ' > '.join(self.heading_path)


@dataclass(frozen=True)
class _Piece:
    kind: str
    path: tuple[str, ...]
    lines: tuple[str, ...]
    pages: tuple[int, ...]
    part: tuple[int, int] | None = None

    @property
    def tokens(self):
        return sum(estimate_tokens(line) for line in self.lines)


def _units(blocks):
    path, units, open_unit, last_page = [], [], None, 0

    def close():
        nonlocal open_unit
        if open_unit is not None:
            if open_unit['kind'] == 'table' and len(open_unit['lines']) < 2:
                raise ValueError('table header without rows')
            units.append(_Piece(open_unit['kind'], tuple(path), tuple(open_unit['lines']),
                                tuple(open_unit['pages'])))
            open_unit = None

    for block in blocks:
        if type(block) is not Block:
            raise TypeError('blocks must be Block instances')
        if block.page < last_page:
            raise ValueError('pages must not go backwards; re-run the parser')
        last_page = block.page
        text = ' '.join(block.text.split())
        if block.kind != 'heading' and not path:
            raise ValueError('content before the first heading; emit the title as a level-1 heading')
        if block.kind == 'heading':
            close()
            if block.level > len(path) + 1:
                raise ValueError('heading level skipped')
            path = path[:block.level - 1] + [text]
        elif block.kind == 'paragraph':
            close()
            units.append(_Piece('paragraph', tuple(path), (text,), (block.page,)))
        elif block.kind == 'step':
            match = _STEP.match(text)
            if not match:
                raise ValueError('step must start with its number, e.g. "1. "')
            number = int(match[1])
            if number == 1 or open_unit is None or open_unit['kind'] != 'procedure':
                close()
                if number != 1:
                    raise ValueError(f'step {number} does not continue a procedure: a step was '
                                     'lost, or a note between steps split the procedure')
                open_unit = {'kind': 'procedure', 'lines': [], 'pages': []}
            elif number != len(open_unit['lines']) + 1:
                raise ValueError(f'step {number} out of sequence: a step was lost or reordered')
            open_unit['lines'].append(text)
            open_unit['pages'].append(block.page)
        elif block.kind == 'table_header':
            close()
            open_unit = {'kind': 'table', 'lines': [text], 'pages': [block.page]}
        else:
            if open_unit is None or open_unit['kind'] != 'table':
                raise ValueError('table row without its header')
            open_unit['lines'].append(text)
            open_unit['pages'].append(block.page)
    close()
    return units


def _split(unit, max_tokens):
    """Split at sentence, step or row boundaries only; repeat a table header."""
    if unit.kind == 'paragraph':
        items = [(s, unit.pages[0]) for s in _SENTENCE_END.split(unit.lines[0])]
        header = ()
    elif unit.kind == 'table':
        items = list(zip(unit.lines[1:], unit.pages[1:]))
        header = ((unit.lines[0], unit.pages[0]),)
    else:
        items, header = list(zip(unit.lines, unit.pages)), ()
    budget = max_tokens - sum(estimate_tokens(line) for line, _ in header)
    groups, current, used = [], [], 0
    for line, page in items:
        cost = estimate_tokens(line)
        if cost > budget:
            raise ValueError(f'one {unit.kind} boundary item exceeds {max_tokens} tokens')
        if current and used + cost > budget:
            groups.append(current)
            current, used = [], 0
        current.append((line, page))
        used += cost
    groups.append(current)
    if unit.kind == 'paragraph':  # sentences of one paragraph rejoin on one line
        groups = [[(' '.join(line for line, _ in g), g[0][1])] for g in groups]
    return [_Piece(unit.kind, unit.path, tuple(line for line, _ in header + tuple(g)),
                   tuple(page for _, page in header + tuple(g)), (i, len(groups)))
            for i, g in enumerate(groups, 1)]


def chunk_blocks(doc_id, version, blocks, *, max_tokens, split_oversized=False):
    """Return chunks in document order; raise instead of truncating anything."""
    if type(doc_id) is not str or not _DOC_ID.fullmatch(doc_id):
        raise ValueError('invalid document id')
    if type(version) is not int or version < 1:
        raise ValueError('version must be a positive int')
    if type(max_tokens) is not int or max_tokens < 1 or type(split_oversized) is not bool:
        raise ValueError('invalid budget or split flag')
    units = _units(blocks)
    if not units:
        raise ValueError('document has no content blocks')
    groups = []
    for unit in units:
        if unit.tokens <= max_tokens:
            last = groups[-1] if groups else None
            if (last and last[-1].part is None and last[-1].path == unit.path
                    and sum(p.tokens for p in last) + unit.tokens <= max_tokens):
                last.append(unit)
            else:
                groups.append([unit])
        elif not split_oversized:
            raise ValueError(f'{unit.kind} of {unit.tokens} tokens exceeds {max_tokens}; '
                             'raise the budget or allow boundary splits')
        else:
            groups.extend([piece] for piece in _split(unit, max_tokens))
    chunks = []
    for ordinal, group in enumerate(groups, 1):
        text = '\n'.join(line for piece in group for line in piece.lines)
        pages = [page for piece in group for page in piece.pages]
        path = group[0].path
        digest = hashlib.sha256(json.dumps([path, text], ensure_ascii=False).encode()).hexdigest()
        chunks.append(Chunk(
            chunk_id=f'{doc_id}:v{version}:{ordinal:04d}', doc_id=doc_id, version=version,
            ordinal=ordinal, heading_path=path, pages=(min(pages), max(pages)),
            kinds=tuple(piece.kind for piece in group), part=group[0].part, text=text,
            tokens=estimate_tokens(text), content_hash='sha256:' + digest))
    return chunks
