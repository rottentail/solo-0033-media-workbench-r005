"""Persistent source intervals with occurrence identity, independent of timeline offsets."""


def _clone(span):
    # Span dicts are flat with immutable values, so a shallow copy is a complete
    # independent copy and avoids the large cost of generic deepcopy on hot paths.
    return dict(span)


def clone_all(spans):
    return [dict(span) for span in spans]


def extent(spans):
    return sum(s['end_ms'] - s['start_ms'] for s in spans)


def split(spans, at, stamp=None):
    if type(at) is not int or not 0 <= at <= extent(spans):
        raise ValueError('edit position is outside its viewed timeline')
    before, after, cursor = [], [], 0
    for original in spans:
        span = _clone(original)
        stop = cursor + span['end_ms'] - span['start_ms']
        if stop <= at:
            before.append(span)
        elif cursor >= at:
            after.append(span)
        else:
            left, right = _clone(span), _clone(span)
            left['end_ms'] = right['start_ms'] = span['start_ms'] + at - cursor
            if stamp is not None:
                # A new boundary records the edit that split the occurrence.
                left['stamp'] = right['stamp'] = stamp
            before.append(left)
            after.append(right)
        cursor = stop
    return before, after


def cut(spans, start, end, stamp=None):
    if type(start) is not int or type(end) is not int or not start < end:
        raise ValueError('edit position must have positive integer duration')
    prefix, rest = split(spans, start, stamp)
    selected, suffix = split(rest, end - start, stamp)
    return prefix, selected, suffix


def token(span, at):
    return {'uid': span['uid'], 'source_ms': at}


def gap(spans, at, known_birth=None):
    left, right = split(spans, at)
    return {'left': token(left[-1], left[-1]['end_ms'] - 1) if left else None,
            'right': token(right[0], right[0]['start_ms']) if right else None,
            'empty': not spans, 'known_birth': max((s.get('born', 0) for s in spans), default=0) if known_birth is None else known_birth}


def locate(spans, point):
    if point is None:
        return None
    cursor = 0
    found = []
    for span in spans:
        if span['uid'] == point['uid'] and span['start_ms'] <= point['source_ms'] < span['end_ms']:
            found.append(cursor + point['source_ms'] - span['start_ms'])
        cursor += span['end_ms'] - span['start_ms']
    if len(found) > 1:
        raise ValueError('ambiguous occurrence lineage')
    return found[0] if found else None


def resolve_gap(spans, anchor):
    left, right = locate(spans, anchor['left']), locate(spans, anchor['right'])
    if left is not None and right is not None:
        if left >= right:
            raise ValueError('edit destination was reordered')
        if right > left + 1:
            _, middle, _ = cut(spans, left + 1, right)
            if any(s.get('born', 0) <= anchor['known_birth'] for s in middle):
                raise ValueError('edit destination was split by moved content')
        return right
    if right is not None:
        return right
    if left is not None:
        return left + 1
    # Both anchor occurrences are gone. Restoring into an emptied timeline (for
    # example an undo) places the content at the only remaining position.
    if anchor.get('empty') or not spans:
        return 0
    raise ValueError('edit destination no longer exists')


def normalized(spans):
    result = []
    for original in spans:
        span = _clone(original)
        # Only fuse contiguous pieces produced by the same edit. Fragments of an
        # occurrence that was split by an intervening delete keep their distinct
        # stamps and must remain separate even if they become adjacent again.
        if (result
                and result[-1].get('stamp') == span.get('stamp')
                and all(result[-1][k] == span[k] for k in ('uid', 'stamp', 'asset_id', 'gain'))
                and result[-1]['end_ms'] == span['start_ms']):
            result[-1]['end_ms'] = span['end_ms']
        else:
            result.append(span)
    return result


def _windows(fragments):
    """Collapse adjacent fragments of one occurrence into exact source windows."""
    windows = []
    for span in fragments:
        if (windows and windows[-1]['uid'] == span['uid']
                and windows[-1]['end_ms'] == span['start_ms']
                and windows[-1]['asset_id'] == span['asset_id']
                and windows[-1]['gain'] == span['gain']):
            windows[-1]['end_ms'] = span['end_ms']
        else:
            windows.append({'uid': span['uid'], 'asset_id': span['asset_id'],
                            'gain': span['gain'],
                            'start_ms': span['start_ms'], 'end_ms': span['end_ms']})
    return windows


def _point_index(spans, uid, source_ms):
    matches = [index for index, span in enumerate(spans)
               if span['uid'] == uid and span['start_ms'] <= source_ms < span['end_ms']]
    if len(matches) > 1:
        raise ValueError('ambiguous occurrence lineage')
    return matches[0] if matches else None


def resolve_selection(spans, expected, stamp=None):
    """Resolve an old-view selection against the current sequence by lineage.

    Matching uses stable occurrence identities plus the exact source interval
    selected in the viewed revision.  Repeated inserts of the same asset are
    independent occurrences, a split occurrence still carries its original uid,
    and deleted, replaced or interleaved content makes the whole batch conflict
    rather than silently choosing a similar span.
    """
    if not expected:
        raise ValueError('edit selection must not be empty')
    wanted = extent(expected)
    first = expected[0]
    # The first selected source point identifies the exact occurrence fragment.
    index = _point_index(spans, first['uid'], first['start_ms'])
    if index is None:
        raise ValueError('selected content was removed or changed')
    target = spans[index]
    start = sum(s['end_ms'] - s['start_ms'] for s in spans[:index])
    # The wanted interval must begin inside the located fragment's source extent.
    offset = first['start_ms'] - target['start_ms']
    if offset < 0 or offset >= target['end_ms'] - target['start_ms']:
        raise ValueError('selected content was concurrently changed')
    start += offset
    end = start + wanted
    total = extent(spans)
    if end > total:
        raise ValueError('selected content is incomplete')
    # The interval must finish inside the same occurrence lineage as the view.
    prefix, actual, suffix = cut(spans, start, end, stamp)
    if _windows(actual) != _windows(expected):
        raise ValueError('selected content was concurrently changed')
    return prefix, actual, suffix
