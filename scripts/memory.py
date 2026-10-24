"""Offline TXT memory store; no model calls, host-log scraping, or background jobs."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone

PREFIX = 'DSH-MEMORY '
MAX_SUMMARY = 2400
MAX_OUTPUT = 4000
DEFAULT_OUTPUT = 2000
MAX_FILE_BYTES = 256 * 1024
MAX_HEADER_BYTES = 8 * 1024
PART_BODY_BYTES = MAX_FILE_BYTES - MAX_HEADER_BYTES - 1
MAX_SCAN_FILES = 200


def resolve_root(explicit=None, config_path=None):
    """Resolve without creating directories; never require a drive or repo layout."""
    skill_dir = Path(__file__).resolve().parents[1]
    config = Path(config_path) if config_path else skill_dir / 'memory.config.json'
    chosen = explicit or os.environ.get('DSH_TOKEN_MEMORY_ROOT')
    if chosen:
        return Path(os.path.expandvars(str(chosen))).expanduser().resolve()
    if config.exists():
        data = json.loads(config.read_text(encoding='utf-8-sig'))
        chosen = data.get('root')
        if not isinstance(chosen, str) or not chosen.strip():
            raise ValueError('memory.config.json must contain a non-empty root string')
        path = Path(os.path.expandvars(chosen)).expanduser()
        return (path if path.is_absolute() else config.parent / path).resolve()
    dsh_home = os.environ.get('DSH_HOME')
    base = Path(os.path.expandvars(dsh_home)).expanduser() if dsh_home else Path.home() / '.dsh'
    return (base / 'token-memory').resolve()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def redact(text):
    text = re.sub(r'(?i)\bBearer\s+[^\s\"<>]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)
    return re.sub(r'(?i)(\b(?:SESSDATA|bili_jct|api[_-]?key|access[_-]?token|password)\b\s*[:=]\s*)([^\s,;]+)', r'\1[REDACTED]', text)


def entries(path, headers_only=False):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Legacy/oversized file exceeds 256 KiB; rearchive as split revision')
    with path.open('rb') as stream:
        consumed = 0
        while True:
            first = stream.readline(MAX_HEADER_BYTES + 1)
            if not first:
                return
            consumed += len(first)
            if len(first) > MAX_HEADER_BYTES or not first.endswith(b'\n'):
                raise ValueError('Memory header exceeds 8 KiB or has no newline')
            text = first.decode('utf-8')
            if not text.startswith(PREFIX):
                raise ValueError('Invalid memory document')
            meta = json.loads(text[len(PREFIX):])
            size = meta.get('body_bytes')
            if size is None:
                # v1 records/checkpoints: one header and the rest is body.
                raw = stream.read(MAX_FILE_BYTES + 1)
                if consumed + len(raw) > MAX_FILE_BYTES:
                    raise ValueError('File grew beyond 256 KiB while reading')
                yield meta, None if headers_only else raw.decode('utf-8')
                return
            if type(size) is not int or size < 0 or consumed + size + 1 > MAX_FILE_BYTES:
                raise ValueError('Invalid body size')
            if headers_only:
                stream.seek(size, 1)
                raw = None
            else:
                raw = stream.read(size)
                if len(raw) != size:
                    raise ValueError('Incomplete record body')
            if stream.read(1) != b'\n':
                raise ValueError('Incomplete record separator')
            consumed += size + 1
            yield meta, None if raw is None else raw.decode('utf-8')


def unpack(path):
    values = list(entries(path))
    if not values:
        raise ValueError('Empty memory document')
    return values[0][0], '\n'.join(body for _, body in values)


def pack(meta, body):
    return PREFIX + json.dumps(meta, ensure_ascii=False, sort_keys=True) + '\n' + body


def split_utf8(text):
    raw = text.encode('utf-8')
    parts = []
    while raw:
        end = min(len(raw), PART_BODY_BYTES)
        if end < len(raw):
            while raw[end] & 0xC0 == 0x80:
                end -= 1
        parts.append(raw[:end].decode('utf-8'))
        raw = raw[end:]
    return parts or ['']


def atomic(path, text):
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('TXT output exceeds 256 KiB including metadata')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root, session):
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            raise ValueError('A real session ID (1..256 chars) is required')
        self.root = Path(root).resolve()
        self.session = session
        self.sid = digest(session)

    def inside(self, path):
        value = Path(path)
        value = (value if value.is_absolute() else self.root / value).resolve()
        if not value.is_relative_to(self.root) or value == self.root:
            raise ValueError('Path is outside memory root')
        return value

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / '.write-lock'
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError('Memory writer is busy or stale lock needs inspection') from None
        try:
            yield
        finally:
            lock.rmdir()

    def records(self, all_sessions=False, headers_only=False):
        items = []
        for kind in ('dialogue', 'learning'):
            folder = self.inside(kind)
            if folder.exists():
                for path in sorted(folder.glob('*.txt')):
                    path = self.inside(path)
                    if path.stat().st_size > MAX_FILE_BYTES:
                        # Never load legacy oversized bodies into memory.
                        continue
                    for meta, _ in entries(path, headers_only=True):
                        if all_sessions or meta.get('session') == self.session:
                            items.append((path, meta))
        for path, meta in sorted(items, key=lambda item: (item[1]['created_utc'], item[1]['id'], item[1].get('part', 1)), reverse=True):
            if headers_only:
                yield path, meta, None
            else:
                for candidate, body in entries(path):
                    if candidate['id'] == meta['id'] and candidate.get('part', 1) == meta.get('part', 1):
                        yield path, meta, body
                        break

    def append_part(self, kind, now, meta, body):
        label = '对话' if kind == 'dialogue' else '学习'
        prefix = f'{label}_{now[:8]}_{self.sid[:12]}_v'
        folder = self.inside(kind)
        folder.mkdir(parents=True, exist_ok=True)
        volumes = sorted(folder.glob(prefix + '*.txt'))
        framed = pack({**meta, 'body_bytes': len(body.encode('utf-8')), 'format_version': 2}, body) + '\n'
        if len(framed.encode('utf-8')) > MAX_FILE_BYTES:
            raise ValueError('Part exceeds file limit')
        if volumes:
            path = self.inside(volumes[-1])
            # Validate the previous volume before replacing it atomically.
            previous = list(entries(path, headers_only=True))
            if any(item['session'] != self.session for item, _ in previous):
                raise ValueError('Volume session identity mismatch')
            if path.stat().st_size + len(framed.encode('utf-8')) <= MAX_FILE_BYTES:
                atomic(path, path.read_text(encoding='utf-8') + framed)
                return path
            number = int(path.stem.rsplit('_v', 1)[1]) + 1
        else:
            number = 1
        path = self.inside(f'{kind}/{prefix}{number:04d}.txt')
        if path.exists():
            raise ValueError('Volume already exists')
        atomic(path, framed)
        return path

    def archive(self, data):
        kind = data.get('kind')
        if kind not in ('dialogue', 'learning'):
            raise ValueError('kind must be dialogue or learning')
        for field in ('key', 'content', 'source', 'title'):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise ValueError('Missing text field: ' + field)
        body = redact(data['content'])
        if len(body) > 2_000_000:
            raise ValueError('Record too large; split into stable event ranges')
        fingerprint = digest(json.dumps([kind, data['key'], data['title'], data['source'], data['content']], ensure_ascii=False))
        parts = split_utf8(body)
        with self.lock():
            existing = {}
            for path, meta, _ in self.records(headers_only=True):
                if meta['kind'] == kind and meta['key'] == data['key']:
                    if meta['fingerprint'] != fingerprint:
                        raise ValueError('Same key has different content; use a revision key')
                    if 'part' not in meta:
                        if path.stat().st_size > MAX_FILE_BYTES:
                            raise ValueError('Legacy oversized record retained; use a new revision key to split')
                        return {'status': 'unchanged', 'path': str(path), 'id': meta['id'], 'parts': 1}
                    existing[meta['part']] = (path, meta)
            record_id = digest(self.session + '\0' + kind + '\0' + data['key'])
            now = next(iter(existing.values()))[1]['created_utc'] if existing else stamp()
            meta = {'kind': kind, 'session': self.session, 'key': data['key'], 'id': record_id,
                    'created_utc': now, 'title': redact(data['title']), 'source': redact(data['source']),
                    'fingerprint': fingerprint}
            if existing and len(existing) == next(iter(existing.values()))[1]['parts']:
                first = existing[min(existing)][0]
                return {'status': 'unchanged', 'path': str(first), 'id': record_id, 'parts': len(existing)}
            if existing and next(iter(existing.values()))[1]['parts'] != len(parts):
                raise ValueError('Incomplete legacy split record; inspect before retry')
            documents = []
            for number, part in enumerate(parts, 1):
                part_meta = {**meta, 'part': number, 'parts': len(parts)}
                if len(pack({**part_meta, 'body_bytes': len(part.encode('utf-8')), 'format_version': 2}, '').encode('utf-8')) > MAX_HEADER_BYTES:
                    raise ValueError('Metadata exceeds 8 KiB; shorten key/title/source')
                documents.append((part_meta, part))
            changed = False
            paths = []
            for number, (part_meta, part) in enumerate(documents, 1):
                if number in existing:
                    paths.append(existing[number][0])
                else:
                    paths.append(self.append_part(kind, now, part_meta, part))
                    changed = True
            return {'status': 'saved' if changed else 'unchanged', 'path': str(paths[0]),
                    'id': record_id, 'parts': len(parts), 'file_limit_bytes': MAX_FILE_BYTES}

    def checkpoint(self, data):
        summary = data.get('summary')
        through = data.get('covered_through')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
            raise ValueError('summary must be 1..2400 characters; no silent truncation')
        with self.lock():
            covered = [meta for _, meta, _ in self.records(headers_only=True) if meta['key'] == through]
            if not covered:
                raise ValueError('covered_through must name an archived key in this session')
            for kind in {meta['kind'] for meta in covered}:
                group = [meta for meta in covered if meta['kind'] == kind]
                total = group[0].get('parts', 1)
                if {meta.get('part', 1) for meta in group} != set(range(1, total + 1)):
                    raise ValueError('Covered record has missing parts; retry archive before checkpoint')
            checkpoint_id = digest(self.session + '\0' + str(through) + '\0' + summary)
            latest = self.inside(f'checkpoints/{self.sid}/latest.txt')
            if latest.exists():
                old, _ = unpack(latest)
                if old['checkpoint_id'] == checkpoint_id:
                    return {'status': 'unchanged', 'checkpoint_id': checkpoint_id, 'path': str(latest)}
            now = stamp()
            meta = {'session': self.session, 'checkpoint_id': checkpoint_id,
                    'covered_through': through, 'created_utc': now}
            text = pack(meta, redact(summary))
            history = self.inside(f'checkpoints/{self.sid}/续接_{now}_{checkpoint_id[:16]}.txt')
            atomic(history, text)
            atomic(latest, text)
            return {'status': 'saved', 'checkpoint_id': checkpoint_id, 'path': str(latest)}

    def restore(self):
        path = self.inside(f'checkpoints/{self.sid}/latest.txt')
        if not path.exists():
            return {'status': 'no_checkpoint', 'session': self.session}
        meta, body = unpack(path)
        if meta['session'] != self.session or len(body) > MAX_SUMMARY:
            raise ValueError('Invalid checkpoint session or size')
        return {'status': 'restored', **meta, 'summary': body, 'characters': len(body)}

    def search(self, query, limit=3, budget=DEFAULT_OUTPUT, all_sessions=False, cursor=None):
        if not 1 <= limit <= 5 or not 512 <= budget <= MAX_OUTPUT:
            raise ValueError('limit must be 1..5; budget must be 512..4000 characters')
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms:
            raise ValueError('Search query cannot be empty')
        candidates = []
        scanned = 0
        more_files = False
        skipped_oversized = sum(1 for kind in ('dialogue', 'learning') for path in self.inside(kind).glob('*.txt') if path.stat().st_size > MAX_FILE_BYTES)
        cursor_found = cursor is None
        last_cursor = None
        for path, meta, _ in self.records(all_sessions, headers_only=True):
            record_cursor = meta['id'] + ':' + str(meta.get('part', 1))
            if not cursor_found:
                if record_cursor == cursor:
                    cursor_found = True
                continue
            if scanned >= MAX_SCAN_FILES:
                more_files = True
                break
            scanned += 1
            last_cursor = record_cursor
            body = next(body for item, body in entries(path) if item['id'] == meta['id'] and item.get('part', 1) == meta.get('part', 1))
            text = meta['title'] + '\n' + meta['source'] + '\n' + body
            score = sum(text.casefold().count(term) for term in terms)
            if score:
                positions = [body.casefold().find(term) for term in terms if term in body.casefold()]
                start = max(0, min(positions, default=0) - 120)
                candidates.append((score, meta['created_utc'], path, meta, body[start:start + 900]))
        hits = []
        if not cursor_found:
            raise ValueError('Search cursor not found in this session scope')
        def output(items):
            return {'hits': items, 'scanned_records': scanned, 'more_records': more_files,
                    'next_cursor': last_cursor if more_files else None,
                    'skipped_oversized': skipped_oversized}
        for score, _, path, meta, snippet in sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
            hit = {'path': str(path.relative_to(self.root)), 'session': meta['session'],
                   'key': meta['key'], 'record_id': meta['id'], 'part': meta.get('part', 1),
                   'title': meta['title'], 'score': score, 'snippet': snippet}
            trial = json.dumps(output(hits + [hit]), ensure_ascii=False)
            if len(trial) > budget:
                available = budget - len(json.dumps(output(hits + [{**hit, 'snippet': ''}]), ensure_ascii=False))
                if available > 0:
                    hit['snippet'] = snippet[:available // 2]
                    if len(json.dumps(output(hits + [hit]), ensure_ascii=False)) <= budget:
                        hits.append(hit)
                break
            hits.append(hit)
            if len(hits) >= limit:
                break
        return output(hits)

    def read(self, path, start=1, lines=25, budget=DEFAULT_OUTPUT, all_sessions=False, offset=0, record_id=None, part=1):
        path = self.inside(path)
        if path.parent not in (self.inside('dialogue'), self.inside('learning')):
            raise ValueError('read only accepts dialogue/learning records')
        values = list(entries(path))
        if not all_sessions and any(meta['session'] != self.session for meta, _ in values):
            raise ValueError('Record belongs to another session')
        if record_id is not None:
            values = [(meta, body) for meta, body in values if meta['id'] == record_id and meta.get('part', 1) == part]
            if not values:
                raise ValueError('Record or part not found in this volume')
        body = '\n'.join(body for _, body in values)
        if start < 1 or not 1 <= lines <= 100 or not 1 <= budget <= MAX_OUTPUT or offset < 0:
            raise ValueError('Invalid line or character budget')
        selected = '\n'.join(body.splitlines()[start - 1:start - 1 + lines])
        selected = selected[offset:]
        truncated = len(selected) > budget
        return {'path': str(path.relative_to(self.root)), 'start_line': start, 'offset': offset,
                'text': selected[:budget], 'truncated': truncated,
                'next_offset': offset + budget if truncated else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', help='Override memory root; see paths for resolved default')
    parser.add_argument('--session', help='Required for archive/checkpoint/restore/search/read')
    parser.add_argument('--all-sessions', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('paths')
    sub.add_parser('init')
    for command in ('archive', 'checkpoint'):
        sub.add_parser(command).add_argument('--input', required=True)
    sub.add_parser('restore')
    search = sub.add_parser('search')
    search.add_argument('--query', required=True)
    search.add_argument('--limit', type=int, default=3)
    search.add_argument('--budget', type=int, default=DEFAULT_OUTPUT)
    search.add_argument('--cursor')
    read = sub.add_parser('read')
    read.add_argument('--path', required=True)
    read.add_argument('--start', type=int, default=1)
    read.add_argument('--lines', type=int, default=25)
    read.add_argument('--budget', type=int, default=DEFAULT_OUTPUT)
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--record-id')
    read.add_argument('--part', type=int, default=1)
    args = parser.parse_args()
    root = resolve_root(args.root)
    if args.command in ('paths', 'init'):
        if args.command == 'init':
            for folder in ('dialogue', 'learning', 'checkpoints'):
                (root / folder).mkdir(parents=True, exist_ok=True)
        print(json.dumps({'root': str(root), 'initialized': args.command == 'init',
                          'file_limit_bytes': MAX_FILE_BYTES, 'default_read_characters': DEFAULT_OUTPUT}, ensure_ascii=False))
        return
    if not args.session:
        parser.error('--session is required for this command')
    store = Store(root, args.session)
    if args.command in ('archive', 'checkpoint'):
        data = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
        result = getattr(store, args.command)(data)
    elif args.command == 'search':
        result = store.search(args.query, args.limit, args.budget, args.all_sessions, args.cursor)
    elif args.command == 'read':
        result = store.read(args.path, args.start, args.lines, args.budget, args.all_sessions, args.offset, args.record_id, args.part)
    else:
        result = store.restore()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
