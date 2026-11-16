"""Compile viewed edits into lineage changes and rebase their inverse intentions."""
import uuid
from . import collab_spans as spans


def execute(current, change, stamp, restoring=False):
    """Apply one lineage change to the current sequence or fail the whole batch.

    Selections resolve by stable occurrence identity and exact source interval,
    so edits based on an older revision never silently land on a similar but
    different occurrence.  Returns the new list, the inverse change and a
    positional op describing what was removed and where content was inserted,
    measured against ``current`` so callers can update the persistent tree
    incrementally.
    """
    current = spans.clone_all(current)
    removed = change['remove']
    if removed:
        prefix, actual, suffix = spans.resolve_selection(current, removed, stamp)
        old_gap = spans.gap(prefix + suffix, spans.extent(prefix))
        remaining = prefix + suffix
        remove_start, remove_end = len(prefix), len(prefix) + len(actual)
    else:
        actual, remaining, old_gap = [], current, None
        remove_start = remove_end = 0
    inserted = spans.clone_all(change['insert'])
    for item in inserted:
        for old in remaining:
            if item['uid'] == old['uid'] and max(item['start_ms'], old['start_ms']) < min(item['end_ms'], old['end_ms']):
                raise ValueError('edit would duplicate source lineage')
        if not restoring:
            item['stamp'] = stamp
    if inserted:
        insert_at = spans.resolve_gap(remaining, change['gap'])
        prefix, suffix = spans.split(remaining, insert_at)
        result = prefix + inserted + suffix
        # The tree splice needs a span-list index, not the millisecond offset.
        insert_index = len(prefix)
    else:
        insert_at = None
        insert_index = None
        result = remaining
    inverse = {'remove': inserted, 'insert': actual, 'gap': old_gap}
    op = {'remove_start': remove_start, 'remove_end': remove_end,
          'insert_at': insert_index, 'insert': inserted}
    return spans.normalized(result), inverse, op

def compile_action(view, action, stamp, base_revision=None):
    """Compile one action against the actor's own viewed sequence."""
    kind = action['kind']
    limit = spans.extent(view)
    if kind == 'insert':
        inserted = []
        for clip in action['clips']:
            inserted.append({'uid': uuid.uuid4().hex, 'stamp': stamp,
                'asset_id': clip['asset_id'], 'born': int(stamp), 'start_ms': clip['start_ms'],
                'end_ms': clip['end_ms'], 'gain': clip.get('gain', 1)})
        if not inserted:
            raise ValueError('insert requires at least one occurrence')
        return {'remove': [], 'insert': inserted, 'gap': spans.gap(view, action['at_ms'], base_revision)}
    if kind not in ('delete', 'move'):
        raise ValueError('unknown collaborative edit kind')
    start, end = action['start_ms'], action['end_ms']
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= limit:
        raise ValueError('selected content is no longer available')
    prefix, selected, suffix = spans.cut(view, start, end, stamp)
    if kind == 'delete':
        return {'remove': selected, 'insert': [], 'gap': None}
    target = action['to_ms']
    if type(target) is not int or not 0 <= target <= limit or start <= target <= end:
        raise ValueError('move destination must be outside its selection')
    adjusted = target - (end - start) if target > end else target
    return {'remove': selected, 'insert': selected,
            'gap': spans.gap(prefix + suffix, adjusted, base_revision)}


def apply(base, current, actions, stamp, base_revision=None):
    """Compile actions on the viewed revision, then rebase them onto current.

    The in-batch coordinates follow the actor's own sequence; the compiled
    lineage changes are replayed against the latest state. Any failed selection
    or gap raises, and the caller rejects the entire batch without partial
    effects.
    """
    view = spans.clone_all(base)
    compiled = []
    for action in actions:
        change = compile_action(view, action, stamp, base_revision)
        view, _, _ = execute(view, change, stamp)
        compiled.append(change)
    result = spans.clone_all(current)
    inverses, ops = [], []
    for change in compiled:
        result, inverse, op = execute(result, change, stamp)
        inverses.insert(0, inverse)
        ops.append(op)
    return spans.normalized(result), inverses, ops


def undo(current, inverses, stamp):
    """Replay one actor's recorded inverse changes against the current state.

    Only the occurrences the original batch touched are removed or restored;
    later edits by other actors stay in place unless they changed the exact
    content the inverse needs, in which case the undo is rejected wholesale.
    """
    result = spans.clone_all(current)
    reverse, ops = [], []
    for change in inverses:
        result, inverse, op = execute(result, change, stamp, restoring=True)
        reverse.insert(0, inverse)
        ops.append(op)
    return spans.normalized(result), reverse, ops
