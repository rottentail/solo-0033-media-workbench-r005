"""Commit collaborative batches and replay receipts in one short database transaction."""
import hashlib
import json
import uuid
import zlib
from . import db, timeline, collab_merge, collab_tree

LEGACY_FORMAT = 'timeline-checkpoint'
EMPTY_ROOT = ''  # edit_versions.payload is NOT NULL; empty string marks an empty sequence.


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def save_tree(conn, spans):
    root = collab_tree.from_spans(conn, spans)
    return EMPTY_ROOT if root is None else root


def load_tree(conn, root):
    if root == EMPTY_ROOT:
        return []
    if not isinstance(root, str) or len(root) != 32:
        raise ValueError('invalid edit history reference')
    try:
        bytes.fromhex(root)
    except ValueError as exc:
        raise ValueError('invalid edit history reference') from exc
    row = conn.execute('SELECT payload FROM edit_nodes WHERE key=?',
                       (bytes.fromhex(root),)).fetchone()
    if row is None:
        raise ValueError('edit history node is missing')
    document = json.loads(zlib.decompress(row['payload']))
    if document.get('format') == collab_tree.NODE_FORMAT:
        return collab_tree.flatten(conn, root)
    if document.get('format') == LEGACY_FORMAT and document.get('count') == len(document.get('rows', ())):
        return [dict(item) for item in document['rows']]
    raise ValueError('unsupported edit history checkpoint')


def _lineage(conn, project_id, revision):
    row = conn.execute('SELECT generation FROM edit_lineages WHERE project_id=? AND revision=?',
                       (project_id, revision)).fetchone()
    if row is not None:
        return row['generation']
    # Revisions persisted before lineage generations were recorded belong to the
    # original generation 0 as long as their version still exists.
    legacy = conn.execute('SELECT 1 FROM edit_versions WHERE project_id=? AND revision=?',
                          (project_id, revision)).fetchone()
    if legacy is not None:
        return 0
    raise ValueError('viewed revision is unavailable; reopen the timeline')


def version(conn, project_id, revision):
    row = conn.execute('SELECT payload FROM edit_versions WHERE project_id=? AND revision=?',
                       (project_id, revision)).fetchone()
    if row is None:
        raise ValueError('viewed revision is unavailable; reopen the timeline')
    return load_tree(conn, row['payload'])


def _current_generation(conn, project_id):
    row = conn.execute('SELECT max(generation) AS g FROM edit_generations WHERE project_id=?',
                       (project_id,)).fetchone()
    return row['g'] if row and row['g'] is not None else 0


def _record_lineage(conn, project_id, revision, generation):
    conn.execute('INSERT OR REPLACE INTO edit_lineages VALUES (?,?,?)',
                 (project_id, revision, generation))


def seed(conn, project_id):
    """Ensure the current timeline has a collab tree root and lineage metadata."""
    snapshot = timeline.snapshot(conn, project_id)
    revision = snapshot['project']['revision']
    row = conn.execute('SELECT payload FROM edit_versions WHERE project_id=? AND revision=?',
                       (project_id, revision)).fetchone()
    generation = _current_generation(conn, project_id)
    if row:
        return revision, load_tree(conn, row['payload']), generation
    spans = [{'uid': uuid.uuid4().hex, 'stamp': 'initial', 'born': revision,
              'asset_id': clip['asset_id'], 'start_ms': clip['start_ms'],
              'end_ms': clip['end_ms'], 'gain': clip['gain']}
             for clip in snapshot['clips']]
    head = conn.execute('SELECT * FROM edit_heads WHERE project_id=?', (project_id,)).fetchone()
    ids = [clip['id'] for clip in snapshot['clips']]
    if head and head['revision'] < revision:
        old_ids = json.loads(head['clip_ids'])
        if ids[:len(old_ids)] == old_ids:
            spans = version(conn, project_id, head['revision']) + spans[len(old_ids):]
    conn.execute('INSERT OR IGNORE INTO edit_generations VALUES (?,?,?)',
                 (project_id, generation, revision))
    conn.execute('INSERT OR REPLACE INTO edit_heads VALUES (?,?,?)',
                 (project_id, revision, encoded(ids)))
    conn.execute('INSERT INTO edit_versions VALUES (?,?,?)',
                 (project_id, revision, save_tree(conn, spans)))
    _record_lineage(conn, project_id, revision, generation)
    return revision, spans, generation


def view(conn, project_id):
    with db.write(conn):
        revision, spans, generation = seed(conn, project_id)
    return {'project_id': project_id, 'revision': revision,
            'generation': generation, 'spans': spans}


def _receipt_response(conn, row):
    response = json.loads(row['response'])
    result_revision = row['result_revision']
    if result_revision is None:
        # Receipts written before lineage revisions were recorded cannot be
        # re-expanded to their own timeline; the stored response is still valid.
        return response
    response['revision'] = result_revision
    response['spans'] = version(conn, row['project_id'], result_revision)
    return response


def _validate_batch(actions, base_revision):
    if type(base_revision) is not int or base_revision < 0:
        raise ValueError('a viewed revision is required')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
        raise ValueError('one to thirty-two edits are required')
    for action in actions:
        if not isinstance(action, dict) or action.get('kind') not in ('insert', 'delete', 'move'):
            raise ValueError('unknown collaborative edit kind')
        if action['kind'] == 'insert':
            clips = action.get('clips')
            if not isinstance(clips, list) or not clips:
                raise ValueError('insert requires at least one occurrence')
            if type(action.get('at_ms')) is not int or action['at_ms'] < 0:
                raise ValueError('invalid edit position')
        else:
            if type(action.get('start_ms')) is not int or type(action.get('end_ms')) is not int:
                raise ValueError('invalid edit selection')
            if action['kind'] == 'move' and type(action.get('to_ms')) is not int:
                raise ValueError('invalid move destination')


def _root_format(conn, root):
    if root == EMPTY_ROOT:
        return 'empty'
    row = conn.execute('SELECT payload FROM edit_nodes WHERE key=?',
                       (bytes.fromhex(root),)).fetchone()
    if row is None:
        raise ValueError('edit history node is missing')
    return json.loads(zlib.decompress(row['payload'])).get('format')


def _persist_tree(conn, root, result, ops):
    """Apply the batch's positional ops to the persistent tree incrementally.

    Only the leaf touched by each op and the path back to the root are rebuilt;
    every unchanged subtree keeps its content key. An op's positions are indices
    in the sequence seen at that execute step; after each splice the tree holds
    exactly that sequence, so positions map directly with no cross-op
    translation. The final length is checked against the materialized sequence,
    falling back to a full rebuild if normalization fused fragments. A legacy
    (pre-tree) checkpoint root always triggers one full rebuild into the new
    content-addressed format.
    """
    if root == EMPTY_ROOT or _root_format(conn, root) != collab_tree.NODE_FORMAT:
        return save_tree(conn, result)
    current_root = root
    for op in ops:
        if op['remove_end'] > op['remove_start']:
            current_root = collab_tree.splice(
                conn, current_root,
                op['remove_start'], op['remove_end'], [])
        if op['insert_at'] is not None:
            current_root = collab_tree.splice(
                conn, current_root,
                op['insert_at'], op['insert_at'], op['insert'])
    if collab_tree.length(conn, current_root) != len(result):
        return save_tree(conn, result)
    return EMPTY_ROOT if current_root is None else current_root


def _validate_result(conn, result):
    """Validate every resulting clip against asset metadata in one bulk read.

    Asset identity and metadata are immutable after intake, so a single query
    replaces one round trip per occurrence while keeping the same boundary,
    gain and source-duration checks as timeline._validate.
    """
    import math
    asset_ids = {clip['asset_id'] for clip in result}
    rows = conn.execute(
        f"SELECT id, metadata FROM assets WHERE id IN ({','.join('?' for _ in asset_ids)})",
        tuple(asset_ids)).fetchall() if asset_ids else []
    durations = {row['id']: round(float(json.loads(row['metadata'])['format']['duration']) * 1000)
                 for row in rows}
    for clip in result:
        start, end, gain = clip['start_ms'], clip['end_ms'], clip['gain']
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError('invalid clip boundaries')
        if (isinstance(gain, bool) or not isinstance(gain, (float, int))
                or not math.isfinite(gain) or not 0 <= gain <= 2):
            raise ValueError('gain must be between 0 and 2')
        duration = durations.get(clip['asset_id'])
        if duration is None:
            raise ValueError('asset not found')
        if end > duration:
            raise ValueError('clip exceeds source duration')


def _materialize(conn, project_id, result, revision, prior_root, ops):
    if len(result) > 512:
        raise ValueError('timeline exceeds 512 occurrences')
    _validate_result(conn, result)
    conn.execute('DELETE FROM clips WHERE project_id=?', (project_id,))
    conn.executemany(
        'INSERT INTO clips(project_id,asset_id,start_ms,end_ms,position,gain) VALUES (?,?,?,?,?,?)',
        [(project_id, c['asset_id'], c['start_ms'], c['end_ms'], i, c['gain'])
         for i, c in enumerate(result)])
    conn.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (project_id,))
    root = _persist_tree(conn, prior_root, result, ops)
    conn.execute('INSERT INTO edit_versions VALUES (?,?,?)',
                 (project_id, revision + 1, root))
    ids = [r['id'] for r in conn.execute(
        'SELECT id FROM clips WHERE project_id=? ORDER BY position', (project_id,))]
    conn.execute('INSERT OR REPLACE INTO edit_heads VALUES (?,?,?)',
                 (project_id, revision + 1, encoded(ids)))
    _record_lineage(conn, project_id, revision + 1, _current_generation(conn, project_id))


def begin_lineage(conn, project_id, revision):
    """Mark a whole-timeline replacement as a new edit lineage.

    Runs inside the caller's write transaction. Old revisions remain readable
    for their original receipts and frozen exports but can no longer seed new
    edits or undos.
    """
    row = conn.execute('SELECT max(generation) AS g FROM edit_generations WHERE project_id=?',
                       (project_id,)).fetchone()
    generation = (row['g'] if row and row['g'] is not None else 0) + 1
    conn.execute('INSERT INTO edit_generations VALUES (?,?,?)',
                 (project_id, generation, revision))
    conn.execute('DELETE FROM edit_heads WHERE project_id=?', (project_id,))
    return generation


def commit(conn, project_id, actor, request_id, base_revision=None, actions=None, undo_request=None):
    if not all(isinstance(x, str) and 0 < len(x) <= 128 for x in (actor, request_id)):
        raise ValueError('actor and request identity are required')
    if undo_request is None:
        _validate_batch(actions, base_revision)
    elif actions is not None or base_revision is not None or not isinstance(undo_request, str):
        raise ValueError('undo must identify only the original request')
    body = {'base_revision': base_revision, 'actions': actions, 'undo_request': undo_request}
    checksum = hashlib.sha256(encoded(body).encode()).hexdigest()
    with db.write(conn):
        previous = conn.execute(
            'SELECT * FROM edit_receipts WHERE project_id=? AND actor=? AND request_id=?',
            (project_id, actor, request_id)).fetchone()
        if previous:
            if previous['body_sha256'] != checksum:
                raise ValueError('request identity was reused for a different edit')
            # Replays return the original submission, never the current head.
            return _receipt_response(conn, previous)
        revision, current, generation = seed(conn, project_id)
        prior_root = conn.execute(
            'SELECT payload FROM edit_versions WHERE project_id=? AND revision=?',
            (project_id, revision)).fetchone()[0]
        stamp = str(revision + 1)
        if undo_request is not None:
            original = conn.execute(
                'SELECT * FROM edit_receipts WHERE project_id=? AND actor=? AND request_id=?',
                (project_id, actor, undo_request)).fetchone()
            if original is None or original['undone_by'] is not None:
                raise ValueError('edit is absent or was already undone')
            if original['generation'] != generation:
                raise ValueError('edit belongs to a replaced timeline lineage')
            result, inverses, ops = collab_merge.undo(current, json.loads(original['inverse']), stamp)
            base_for_receipt = revision
        else:
            if _lineage(conn, project_id, base_revision) != generation:
                raise ValueError('viewed revision is from a replaced timeline; reopen it')
            base = version(conn, project_id, base_revision)
            result, inverses, ops = collab_merge.apply(base, current, actions, stamp, base_revision)
            base_for_receipt = base_revision
        _materialize(conn, project_id, result, revision, prior_root, ops)
        response = {'project_id': project_id, 'revision': revision + 1,
                    'actor': actor, 'request_id': request_id}
        conn.execute(
            'INSERT INTO edit_receipts(project_id,actor,request_id,body_sha256,response,'
            'inverse,undone_by,base_revision,result_revision,generation) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (project_id, actor, request_id, checksum, encoded(response),
             encoded(inverses), None, base_for_receipt, revision + 1, generation))
        if undo_request is not None:
            conn.execute('UPDATE edit_receipts SET undone_by=? '
                         'WHERE project_id=? AND actor=? AND request_id=?',
                         (request_id, project_id, actor, undo_request))
    response['spans'] = result
    return response
