"""Content-addressed persistent sequence tree for timeline spans.

Every node is immutable and stored once under the hash of its payload, so short
edits on long timelines share every unchanged subtree instead of copying the
whole 512-occurrence sequence.  Roots are either leaf nodes (up to ``FANOUT``
spans) or internal nodes whose children are equal-height subtrees; internal
nodes that grow past ``FANOUT`` are rebalanced while they are written.

Unchanged subtrees are referenced by their existing content keys, so only the
leaf holding an edit and the path back to the root are newly written.
"""
import collections
import hashlib
import json
import threading
import zlib

FANOUT = 16
NODE_FORMAT = 'span-node'

# Nodes are immutable once stored, so decoded payloads can be shared safely
# across requests and threads. The cache bounds memory; a 512-span tree has only
# a few dozen nodes, so the working set of a long editing session stays cached.
_NODE_CACHE_LIMIT = 4096
_node_cache = collections.OrderedDict()
_node_lock = threading.Lock()


def _encoded(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


def _store_node(conn, document):
    raw = _encoded(document)
    key = hashlib.blake2b(raw, digest_size=16).digest()
    # INSERT OR IGNORE makes repeated content collapse onto one stored node.
    conn.execute('INSERT OR IGNORE INTO edit_nodes VALUES (?,?)',
                 (key, zlib.compress(raw, level=6)))
    with _node_lock:
        _node_cache[key] = document
        _node_cache.move_to_end(key)
        while len(_node_cache) > _NODE_CACHE_LIMIT:
            _node_cache.popitem(last=False)
    return key.hex()


def _store(conn, node):
    """Write a leaf/internal dict, rebalancing internal overflow. Returns root hex.

    Internal children may be hex references to stored nodes or in-memory node
    dicts produced by partial takes; the latter are stored first.
    """
    if node['kind'] == 'leaf':
        return _store_node(conn, {'format': NODE_FORMAT, 'kind': 'leaf',
                                  'spans': node['spans']})
    children = [child if isinstance(child, str) else _store(conn, child)
                for child in node['children']]
    if len(children) <= FANOUT:
        return _store_node(conn, {'format': NODE_FORMAT, 'kind': 'internal',
                                  'children': children})
    middle = len(children) // 2
    left = _store(conn, {'kind': 'internal', 'children': children[:middle]})
    right = _store(conn, {'kind': 'internal', 'children': children[middle:]})
    return _store_node(conn, {'format': NODE_FORMAT, 'kind': 'internal',
                              'children': [left, right]})


def _load(conn, root):
    if isinstance(root, dict):
        return root
    if not isinstance(root, str) or len(root) != 32:
        raise ValueError('invalid edit history reference')
    try:
        key = bytes.fromhex(root)
    except ValueError as exc:
        raise ValueError('invalid edit history reference') from exc
    with _node_lock:
        cached = _node_cache.get(key)
        if cached is not None:
            _node_cache.move_to_end(key)
            return cached
    row = conn.execute('SELECT payload FROM edit_nodes WHERE key=?',
                       (key,)).fetchone()
    if row is None:
        raise ValueError('edit history node is missing')
    document = json.loads(zlib.decompress(row['payload']))
    if document.get('format') != NODE_FORMAT or document.get('kind') not in ('leaf', 'internal'):
        raise ValueError('unsupported edit history node')
    with _node_lock:
        _node_cache[key] = document
        _node_cache.move_to_end(key)
        while len(_node_cache) > _NODE_CACHE_LIMIT:
            _node_cache.popitem(last=False)
    return document


def _node_length(conn, node):
    if isinstance(node, str):
        return _node_length(conn, _load(conn, node))
    if node['kind'] == 'leaf':
        return len(node['spans'])
    return sum(_node_length(conn, child) for child in node['children'])


def length(conn, root):
    if root is None:
        return 0
    return _node_length(conn, root)


def flatten(conn, root):
    if root is None:
        return []
    document = _load(conn, root)
    if document['kind'] == 'leaf':
        return [dict(span) for span in document['spans']]
    result = []
    for child in document['children']:
        result.extend(flatten(conn, child))
    return result


def from_spans(conn, spans):
    """Build a balanced tree root for an in-memory span sequence."""
    if not spans:
        return None
    level = [{'kind': 'leaf', 'spans': [dict(span) for span in spans[i:i + FANOUT]]}
             for i in range(0, len(spans), FANOUT)]
    while len(level) > 1:
        level = [{'kind': 'internal', 'children': level[i:i + FANOUT]}
                 for i in range(0, len(level), FANOUT)]
    return _store(conn, level[0])


def _take(conn, node, count):
    if count <= 0:
        return None
    if node['kind'] == 'leaf':
        if count >= len(node['spans']):
            return node
        return {'kind': 'leaf', 'spans': [dict(s) for s in node['spans'][:count]]}
    taken = []
    remaining = count
    for child in node['children']:
        size = _node_length(conn, child)
        if remaining >= size:
            taken.append(child)
            remaining -= size
            continue
        if remaining:
            part = _take(conn, _load(conn, child), remaining)
            if part is not None:
                taken.append(part)
        break
    if not taken:
        return None
    if len(taken) == 1:
        return taken[0]
    return {'kind': 'internal', 'children': taken}


def _drop(conn, node, count):
    if count <= 0:
        return node
    if node['kind'] == 'leaf':
        if count >= len(node['spans']):
            return None
        return {'kind': 'leaf', 'spans': [dict(s) for s in node['spans'][count:]]}
    kept = []
    remaining = count
    for index, child in enumerate(node['children']):
        size = _node_length(conn, child)
        if remaining >= size:
            remaining -= size
            continue
        if remaining:
            part = _drop(conn, _load(conn, child), remaining)
            if part is not None:
                kept.append(part)
        else:
            kept.append(child)
        kept.extend(node['children'][index + 1:])
        break
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return {'kind': 'internal', 'children': kept}


def _join(conn, left, right):
    """Concatenate two in-memory trees (either may be None)."""
    if left is None:
        return right
    if right is None:
        return left
    left, right = _load(conn, left), _load(conn, right)
    if left['kind'] == 'leaf' and right['kind'] == 'leaf':
        return _store(conn, {'kind': 'leaf',
                             'spans': [dict(s) for s in left['spans']]
                             + [dict(s) for s in right['spans']]})
    if left['kind'] == right['kind']:
        return _store(conn, {'kind': 'internal',
                             'children': left['children'] + right['children']})
    if left['kind'] == 'internal':
        return _store(conn, {'kind': 'internal',
                             'children': left['children'] + [right]})
    return _store(conn, {'kind': 'internal',
                         'children': [left] + right['children']})


def splice(conn, root, start, end, inserted):
    """Return a new root replacing spans [start, end) with ``inserted``.

    Only the leaf holding the edit and the path to the root are rebuilt; every
    other subtree keeps its content-addressed identity. The returned root is
    always a stored hex key (or ``None`` for an empty sequence).
    """
    total = length(conn, root)
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= total:
        raise ValueError('edit position is outside its viewed timeline')
    if start == 0 and end == total:
        return from_spans(conn, inserted)
    document = _load(conn, root) if root is not None else None
    prefix = _take(conn, document, start) if document is not None else None
    suffix = _drop(conn, document, end) if document is not None else None
    middle = from_spans(conn, inserted)
    joined = _join(conn, _join(conn, prefix, middle), suffix)
    if joined is None:
        return None
    return joined if isinstance(joined, str) else _store(conn, joined)
