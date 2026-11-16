"""Compile viewed edits into lineage changes and rebase their inverse intentions."""
import copy
import uuid
from . import collab_spans as spans


def execute(current, change, stamp, restoring=False):
    current = copy.deepcopy(current)
    removed = change['remove']
    if removed:
        prefix, actual, suffix = spans.resolve_selection(current, removed)
        old_gap = spans.gap(prefix + suffix, spans.extent(prefix))
        remaining = prefix + suffix
    else:
        actual, remaining, old_gap = [], current, None
    inserted = copy.deepcopy(change['insert'])
    for item in inserted:
        for old in remaining:
            if item['uid'] == old['uid'] and max(item['start_ms'], old['start_ms']) < min(item['end_ms'], old['end_ms']):
                raise ValueError('edit would duplicate source lineage')
        if not restoring:
            item['stamp'] = stamp
    if inserted:
        at = spans.resolve_gap(remaining, change['gap'])
        prefix, suffix = spans.split(remaining, at)
        result = prefix + inserted + suffix
    else:
        result = remaining
    inverse = {'remove': inserted, 'insert': actual, 'gap': old_gap}
    return spans.normalized(result), inverse


def compile_action(view, action, stamp, base_revision=None):
    kind = action['kind']
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
    prefix, selected, suffix = spans.cut(view, start, end)
    if kind == 'delete':
        return {'remove': selected, 'insert': [], 'gap': None}
    target = action['to_ms']
    if type(target) is not int or start <= target <= end:
        raise ValueError('move destination must be outside its selection')
    adjusted = target - (end - start) if target > end else target
    return {'remove': selected, 'insert': selected,
            'gap': spans.gap(prefix + suffix, adjusted, base_revision)}


def apply(view, current, actions, stamp, base_revision=None):
    inverses = []
    current = copy.deepcopy(current)
    initial = copy.deepcopy(current)
    for action in actions:
        kind = action['kind']
        if kind in ('delete', 'move'):
            lower = min(action['start_ms'], spans.extent(current))
            upper = min(action['end_ms'], spans.extent(current))
            if upper <= lower:
                raise ValueError('selected content is no longer available')
            adjusted = dict(action, start_ms=lower, end_ms=upper)
            if kind == 'move':
                adjusted['to_ms'] = min(action['to_ms'], spans.extent(current))
        elif kind == 'insert':
            adjusted = dict(action, at_ms=min(action['at_ms'], spans.extent(current)))
        else:
            raise ValueError('unknown collaborative edit kind')
        change = compile_action(current, adjusted, stamp)
        current, inverse = execute(current, change, stamp)
        inverses.insert(0, inverse)
    return current, [{'remove': current, 'insert': initial, 'gap': {'left': None, 'right': None, 'empty': True}}]


def undo(current, inverses, stamp):
    restored = copy.deepcopy(current)
    previous = copy.deepcopy(current)
    for change in inverses:
        replacement = copy.deepcopy(change['insert'])
        for item in replacement:
            item['stamp'] = stamp
        restored = replacement
    reverse = [{'remove': restored, 'insert': previous, 'gap': {'left': None, 'right': None, 'empty': True}}]
    return restored, reverse
