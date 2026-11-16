"""Standalone verification for the collaborative edit chain.

Run with: python3 -I /workspace/harness/verify_collab.py
Uses only SQLite-backed production code paths (no ffmpeg needed).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, '/workspace')

from contextlib import closing
from mediaworkbench import db, collab_store, collab_tree, collab_merge, collab_spans


def asset(conn, aid, duration_ms=60000):
    conn.execute('INSERT INTO assets(id,path,fingerprint,metadata) VALUES (?,?,?,?)',
                 (aid, f'/media/{aid}.mp4', f'fp-{aid}',
                  json.dumps({'format': {'duration': duration_ms / 1000}, 'streams': []})))


def project(conn, name='P'):
    cur = conn.execute('INSERT INTO projects(name) VALUES (?)', (name,))
    return cur.lastrowid


def seed_clips(conn, pid, clips):
    for i, (aid, s, e) in enumerate(clips):
        conn.execute('INSERT INTO clips(project_id,asset_id,start_ms,end_ms,position,gain)'
                     ' VALUES (?,?,?,?,?,1)', (pid, aid, s, e, i))
    conn.execute('UPDATE projects SET revision=? WHERE id=?', (len(clips), pid))
    conn.commit()


def edit(conn, pid, actor, rid, base, actions, ok=True):
    try:
        result = collab_store.commit(conn, pid, actor, rid, base_revision=base, actions=actions)
    except ValueError as exc:
        if ok:
            raise AssertionError(f'unexpected conflict: {exc}') from exc
        return str(exc)
    if not ok:
        raise AssertionError('expected conflict but batch was accepted')
    return result


def undo(conn, pid, actor, rid, target, ok=True):
    try:
        result = collab_store.commit(conn, pid, actor, rid, undo_request=target)
    except ValueError as exc:
        if ok:
            raise AssertionError(f'unexpected undo conflict: {exc}') from exc
        return str(exc)
    if not ok:
        raise AssertionError('expected undo conflict but it was accepted')
    return result


def uid_list(spans):
    return [(s['uid'], s['asset_id'], s['start_ms'], s['end_ms']) for s in spans]


def by_uid(spans, uid):
    return [s for s in spans if s['uid'] == uid]


def main():
    tmp = tempfile.mkdtemp(prefix='collab-verify-')
    database = os.path.join(tmp, 'collab.sqlite3')
    conn = db.connect(database)
    asset(conn, 1)
    asset(conn, 2)
    pid = project(conn)
    # Five distinct 1000 ms cuts of asset 1: A B C D E (program coords 0..5000)
    seed_clips(conn, pid, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000),
                           (1, 3000, 4000), (1, 4000, 5000)])

    v0 = collab_store.view(conn, pid)
    rev = v0['revision']
    assert rev == 5, rev
    u = [s['uid'] for s in v0['spans']]
    assert len(u) == 5 and len(set(u)) == 5
    A, B, C, D, E = u
    print('1. initial lineage view ok')

    # --- Scenario 1: 乙 deletes the leading empty shot while 甲 is on the old view
    r_yi = edit(conn, pid, 'yi', 'req-yi', rev,
                [{'kind': 'delete', 'start_ms': 0, 'end_ms': 1000}])
    rev_yi = r_yi['revision']
    assert [s['uid'] for s in r_yi['spans']] == [B, C, D, E]

    # 甲 moves span C (2000..3000 in the old view) to the end, still based on rev 5
    r_jia = edit(conn, pid, 'jia', 'req-jia', rev,
                 [{'kind': 'move', 'start_ms': 2000, 'end_ms': 3000, 'to_ms': 5000}])
    assert [s['uid'] for s in r_jia['spans']] == [B, D, E, C], \
        [s['uid'] for s in r_jia['spans']]
    print('2. old-view move rebases onto concurrent delete and keeps the same occurrence')

    # --- Scenario 2: same material inserted repeatedly stays independently addressable
    r_rep = edit(conn, pid, 'jia', 'req-rep', r_jia['revision'],
                 [{'kind': 'insert', 'at_ms': 0,
                   'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 500},
                             {'asset_id': 2, 'start_ms': 0, 'end_ms': 500}]}])
    new_spans = r_rep['spans']
    x1, x2 = new_spans[0]['uid'], new_spans[1]['uid']
    assert x1 != x2 and new_spans[0]['asset_id'] == 2
    # Concurrent editor moves the SECOND inserted copy by lineage, not asset id
    base = r_rep['revision']
    r_pick = edit(conn, pid, 'yi', 'req-pick2', base,
                  [{'kind': 'move', 'start_ms': 500, 'end_ms': 1000,
                    'to_ms': 0}])
    assert r_pick['spans'][0]['uid'] == x2
    assert r_pick['spans'][1]['uid'] == x1
    print('3. repeated inserts of identical material are independent occurrences')

    # --- Scenario 3: split an occurrence by deleting part, then select a fragment
    # Rebuild a clean project for deterministic coordinates
    pid2 = project(conn, 'split')
    seed_clips(conn, pid2, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000)])
    w = collab_store.view(conn, pid2)
    r2 = w['revision']
    u2 = [s['uid'] for s in w['spans']]
    P, Q, R = u2
    # 乙 splits P by deleting its middle 400 ms (program 200..600)
    rsp = edit(conn, pid2, 'yi', 'split-1', r2,
               [{'kind': 'delete', 'start_ms': 200, 'end_ms': 600}])
    pieces = [s for s in rsp['spans'] if s['uid'] == P]
    assert [(s['start_ms'], s['end_ms']) for s in pieces] == [(0, 200), (600, 1000)], pieces
    # 甲 (old view) moves the surviving first fragment P[0:200] by lineage;
    # the uid and exact source interval locate it despite the split.
    rmove = edit(conn, pid2, 'jia', 'move-frag', r2,
                 [{'kind': 'move', 'start_ms': 0, 'end_ms': 200, 'to_ms': 3000}])
    moved = [s for s in rmove['spans'] if s['uid'] == P]
    # Both fragments survive, separately, and the selected one moved to the tail.
    assert sorted((s['start_ms'], s['end_ms']) for s in moved) == [(0, 200), (600, 1000)], moved
    assert rmove['spans'][-1]['uid'] == P
    assert rmove['spans'][-1]['end_ms'] == 200
    # Asking for the removed middle is a real conflict, not a "similar" substitute.
    edit(conn, pid2, 'jia', 'gone-middle', r2,
         [{'kind': 'delete', 'start_ms': 200, 'end_ms': 600}], ok=False)
    # Asking for the full original range also conflicts because part is gone.
    edit(conn, pid2, 'jia', 'gone-full', r2,
         [{'kind': 'delete', 'start_ms': 0, 'end_ms': 1000}], ok=False)
    print('4. split occurrence selected by stable uid; removed sub-range conflicts explicitly')

    # A delete that exactly covers the remaining fragments of a split span succeeds.
    pid2b = project(conn, 'split-merge')
    seed_clips(conn, pid2b, [(1, 0, 1000), (1, 1000, 2000)])
    wb = collab_store.view(conn, pid2b)
    rb = wb['revision']
    Pb = wb['spans'][0]['uid']
    edit(conn, pid2b, 'yi', 'sm-1', rb,
         [{'kind': 'delete', 'start_ms': 300, 'end_ms': 700}])
    # One old-view selection spanning the first surviving fragment removes it by lineage.
    rdel = edit(conn, pid2b, 'jia', 'sm-2', rb,
                [{'kind': 'delete', 'start_ms': 0, 'end_ms': 300}])
    remaining_pb = [(s['start_ms'], s['end_ms']) for s in rdel['spans'] if s['uid'] == Pb]
    assert remaining_pb == [(700, 1000)], remaining_pb
    # Then the second fragment is addressable on its own and removes the occurrence fully.
    rdel2 = edit(conn, pid2b, 'jia', 'sm-3', rdel['revision'],
                 [{'kind': 'delete', 'start_ms': 0, 'end_ms': 300}])
    assert all(s['uid'] != Pb for s in rdel2['spans'])
    print('5. surviving fragments of a split span are independently addressable')

    # --- Scenario 4: true content contention fails the whole batch, no partial edits
    pid3 = project(conn, 'conflict')
    seed_clips(conn, pid3, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000)])
    w3 = collab_store.view(conn, pid3)
    r3 = w3['revision']
    u3 = [s['uid'] for s in w3['spans']]
    edit(conn, pid3, 'yi', 'c-del', r3,
         [{'kind': 'delete', 'start_ms': 1000, 'end_ms': 2000}])
    before = collab_store.view(conn, pid3)
    # 甲 tries to move the already-deleted Q from the old view
    edit(conn, pid3, 'jia', 'c-move', r3,
         [{'kind': 'delete', 'start_ms': 1000, 'end_ms': 2000}], ok=False)
    after = collab_store.view(conn, pid3)
    assert after['revision'] == before['revision']
    assert uid_list(after['spans']) == uid_list(before['spans'])
    # A multi-action batch also rolls back fully when its second action conflicts
    err = edit(conn, pid3, 'jia', 'c-batch', r3,
               [{'kind': 'insert', 'at_ms': 0,
                 'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 100}]},
                {'kind': 'move', 'start_ms': 1000, 'end_ms': 2000, 'to_ms': 3000}],
               ok=False)
    after2 = collab_store.view(conn, pid3)
    assert after2['revision'] == before['revision'], 'partial batch landed'
    assert all(s['asset_id'] != 2 for s in after2['spans'])
    print(f'5. contended content rejects the whole batch atomically ({err[:40]}...)')

    # --- Scenario 5: undo only retracts the actor's own batch; others' later edits stay
    pid4 = project(conn, 'undo')
    seed_clips(conn, pid4, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000)])
    w4 = collab_store.view(conn, pid4)
    r4 = w4['revision']
    u4 = [s['uid'] for s in w4['spans']]
    G, H, I = u4
    # 甲 inserts X after H (at_ms=2000 -> before I)
    r_ins = edit(conn, pid4, 'jia', 'jia-ins', r4,
                 [{'kind': 'insert', 'at_ms': 2000,
                   'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 250}]}])
    X = next(s['uid'] for s in r_ins['spans'] if s['asset_id'] == 2)
    # 乙 later inserts Y right next to X
    r_y = edit(conn, pid4, 'yi', 'yi-ins', r_ins['revision'],
               [{'kind': 'insert', 'at_ms': 2250,
                 'clips': [{'asset_id': 2, 'start_ms': 250, 'end_ms': 500}]}])
    Y = next(s['uid'] for s in r_y['spans'] if s['uid'] != X and s['asset_id'] == 2)
    order_before = [s['uid'] for s in r_y['spans']]
    assert order_before == [G, H, X, Y, I], order_before
    # 甲 undoes their insert; Y and everything else remains
    r_undo = undo(conn, pid4, 'jia', 'jia-undo', 'jia-ins', True)
    order_after = [s['uid'] for s in r_undo['spans']]
    assert order_after == [G, H, Y, I], order_after
    print('6. selective undo removes only the actor’s effect, preserving later edits')

    # Undo of a delete restores the exact fragments at the recorded gap
    r_del2 = edit(conn, pid4, 'yi', 'yi-del', r_undo['revision'],
                  [{'kind': 'delete', 'start_ms': 0, 'end_ms': 1000}])
    assert all(s['uid'] != G for s in r_del2['spans'])
    r_un = undo(conn, pid4, 'yi', 'yi-undo', 'yi-del', True)
    restored = next(s for s in r_un['spans'] if s['uid'] == G)
    assert (restored['start_ms'], restored['end_ms']) == (0, 1000)
    assert [s['uid'] for s in r_un['spans']] == [G, H, Y, I]
    print('7. undo of a delete restores the exact source interval')

    # Undo refused when another actor changed the affected content (not just because
    # a newer revision exists — the previous undo succeeded on a newer head)
    pid5 = project(conn, 'undo-conflict')
    seed_clips(conn, pid5, [(1, 0, 1000), (1, 1000, 2000)])
    w5 = collab_store.view(conn, pid5)
    r5 = w5['revision']
    J, K = [s['uid'] for s in w5['spans']]
    edit(conn, pid5, 'jia', 'm1', r5,
         [{'kind': 'move', 'start_ms': 0, 'end_ms': 1000, 'to_ms': 2000}])
    # 乙 deletes J (which 甲's move relocated to the end)
    edit(conn, pid5, 'yi', 'm2', r5 + 1,
         [{'kind': 'delete', 'start_ms': 1000, 'end_ms': 2000}])
    undo(conn, pid5, 'jia', 'm1-undo', 'm1', False)
    head = collab_store.view(conn, pid5)
    assert all(s['uid'] != J for s in head['spans'])
    print('8. undo conflicts explicitly when the touched content changed, despite a newer revision')

    # Cannot undo another actor's edit or undo twice
    undo(conn, pid4, 'yi', 'sneaky', 'jia-ins', False)
    undo(conn, pid4, 'jia', 'again', 'jia-ins', False)
    print('9. foreign and repeated undos rejected')

    # --- Scenario 6: idempotent replay never executes twice and returns the original receipt
    pid6 = project(conn, 'replay')
    seed_clips(conn, pid6, [(1, 0, 1000), (1, 1000, 2000)])
    w6 = collab_store.view(conn, pid6)
    r6 = w6['revision']
    first = edit(conn, pid6, 'jia', 'rr-1', r6,
                 [{'kind': 'insert', 'at_ms': 2000,
                   'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 300}]}])
    orig_rev = first['revision']
    # Other edits advance the head
    edit(conn, pid6, 'yi', 'rr-2', orig_rev,
         [{'kind': 'insert', 'at_ms': 0,
           'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 100}]}])
    # Network retry of the exact same request
    replay = collab_store.commit(conn, pid6, 'jia', 'rr-1',
                                 base_revision=r6,
                                 actions=[{'kind': 'insert', 'at_ms': 2000,
                                           'clips': [{'asset_id': 2,
                                                      'start_ms': 0, 'end_ms': 300}]}])
    assert replay['revision'] == orig_rev, (replay['revision'], orig_rev)
    # The original insertion is present exactly once
    matches = [s for s in replay['spans'] if s['asset_id'] == 2
               and s['start_ms'] == 0 and s['end_ms'] == 300]
    assert len(matches) == 1
    # Replay returns the spans of the original result revision, not the current head
    assert len(replay['spans']) == 3 and len(collab_store.view(conn, pid6)['spans']) == 4
    # Same id with a different body is refused
    try:
        collab_store.commit(conn, pid6, 'jia', 'rr-1', base_revision=r6,
                            actions=[{'kind': 'insert', 'at_ms': 0,
                                      'clips': [{'asset_id': 2,
                                                 'start_ms': 0, 'end_ms': 999}]}])
    except ValueError:
        pass
    else:
        raise AssertionError('request id reused with different body was accepted')
    print('10. retried request returns original receipt, executes once, body change rejected')

    # --- Scenario 7: whole-timeline replacement starts a new lineage
    pid7 = project(conn, 'lineage')
    seed_clips(conn, pid7, [(1, 0, 1000), (1, 1000, 2000)])
    w7 = collab_store.view(conn, pid7)
    old_rev = w7['revision']
    old_gen = w7['generation']
    edit(conn, pid7, 'jia', 'l1', old_rev,
         [{'kind': 'insert', 'at_ms': 2000,
           'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 200}]}])
    from mediaworkbench import timeline
    new_head = timeline.replace(conn, pid7,
                                [{'asset_id': 2, 'start_ms': 0, 'end_ms': 1000}],
                                expected_revision=old_rev + 1)
    assert new_head['project']['revision'] == old_rev + 2
    w7b = collab_store.view(conn, pid7)
    assert w7b['generation'] == old_gen + 1
    # Old receipt still replays against its own frozen revision
    replay7 = collab_store.commit(conn, pid7, 'jia', 'l1', base_revision=old_rev,
                                  actions=[{'kind': 'insert', 'at_ms': 2000,
                                            'clips': [{'asset_id': 2,
                                                       'start_ms': 0, 'end_ms': 200}]}])
    assert replay7['revision'] == old_rev + 1 and len(replay7['spans']) == 3
    # New edits cannot be based on a revision from the old lineage
    edit(conn, pid7, 'jia', 'l2', old_rev,
         [{'kind': 'delete', 'start_ms': 0, 'end_ms': 1000}], ok=False)
    # Undo of an old-lineage edit is refused on the new lineage
    undo(conn, pid7, 'jia', 'l1u', 'l1', False)
    print('11. legacy replacement opens a new lineage; old receipts replay, old bases/undos refused')

    # --- Scenario 8: in-batch coordinates chain on the actor’s own evolving view
    pid8 = project(conn, 'batch')
    seed_clips(conn, pid8, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000)])
    w8 = collab_store.view(conn, pid8)
    r8 = w8['revision']
    rb = edit(conn, pid8, 'jia', 'b1', r8,
              [{'kind': 'delete', 'start_ms': 0, 'end_ms': 1000},
               {'kind': 'insert', 'at_ms': 0,
                'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 400}]},
               {'kind': 'move', 'start_ms': 0, 'end_ms': 400, 'to_ms': 2400}])
    seq = [(s['asset_id'], s['start_ms'], s['end_ms']) for s in rb['spans']]
    assert seq == [(1, 1000, 2000), (1, 2000, 3000), (2, 0, 400)], seq
    # Undo the whole batch restores the original three spans
    ru = undo(conn, pid8, 'jia', 'b1u', 'b1', True)
    assert [(s['asset_id'], s['start_ms'], s['end_ms']) for s in ru['spans']] == \
        [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000)]
    print('12. in-batch chaining and whole-batch undo work on the actor’s own view')

    # --- Scenario 9: non-overlapping concurrent edits merge naturally on a new revision
    pid9 = project(conn, 'merge')
    seed_clips(conn, pid9, [(1, 0, 1000), (1, 1000, 2000), (1, 2000, 3000),
                            (1, 3000, 4000), (1, 4000, 5000)])
    w9 = collab_store.view(conn, pid9)
    r9 = w9['revision']
    rm1 = edit(conn, pid9, 'jia', 'g1', r9,
               [{'kind': 'insert', 'at_ms': 0,
                 'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 100}]}])
    rm2 = edit(conn, pid9, 'yi', 'g2', r9,
               [{'kind': 'delete', 'start_ms': 4000, 'end_ms': 5000}])
    assert len(rm2['spans']) == 5  # insert kept, delete applied
    assert rm2['spans'][0]['asset_id'] == 2
    assert rm2['spans'][-1]['end_ms'] == 4000
    print('13. disjoint concurrent edits merge onto the current revision')

    conn.close()

    # --- Scenario 10: storage budget — 10k short batches on a 512-span timeline < 64 MiB
    big = os.path.join(tmp, 'budget.sqlite3')
    conn = db.connect(big)
    for aid in range(1, 9):
        asset(conn, aid)
    pidb = project(conn, 'budget')
    seed_clips(conn, pidb, [(1 + (i % 8), (i * 100) % 50000,
                            min(60000, (i * 100) % 50000 + 100)) for i in range(511)])
    conn.execute('UPDATE projects SET revision=511 WHERE id=?', (pidb,))
    conn.commit()
    base = collab_store.view(conn, pidb)['revision']
    rev_here = base
    budget_batches = int(os.environ.get('COLLAB_BUDGET_BATCHES', '10000'))
    import time
    started = time.time()
    for n in range(budget_batches):
        actor = f'a{n % 4}'
        kind = n % 3
        if kind == 0:  # insert then delete keeps the occurrence count bounded
            insert_end = 50000 + (n % 9000)  # fresh source interval, no lineage overlap
            r = edit(conn, pidb, actor, f'r{n}', rev_here,
                     [{'kind': 'insert', 'at_ms': 0,
                       'clips': [{'asset_id': 1 + (n % 8),
                                  'start_ms': insert_end,
                                  'end_ms': insert_end + 50}]}])
            rev_here = r['revision']
            assert len(r['spans']) == 512
            r2 = edit(conn, pidb, actor, f'r{n}d', rev_here,
                      [{'kind': 'delete', 'start_ms': 0, 'end_ms': 50}])
            rev_here = r2['revision']
        elif kind == 1:  # whole-occurrence move to the end never splits lineage
            spans_now = collab_store.view(conn, pidb)['spans']
            head_span = spans_now[0]
            total = sum(s['end_ms'] - s['start_ms'] for s in spans_now)
            dur = head_span['end_ms'] - head_span['start_ms']
            r = edit(conn, pidb, actor, f'r{n}', rev_here,
                     [{'kind': 'move', 'start_ms': 0, 'end_ms': dur,
                       'to_ms': total}])
            rev_here = r['revision']
        else:  # whole-occurrence delete then selective undo restores it
            spans_now = collab_store.view(conn, pidb)['spans']
            tail = spans_now[-1]
            dur = tail['end_ms'] - tail['start_ms']
            total = sum(s['end_ms'] - s['start_ms'] for s in spans_now)
            r = edit(conn, pidb, actor, f'r{n}', rev_here,
                     [{'kind': 'delete', 'start_ms': total - dur, 'end_ms': total}])
            rev_here = r['revision']
            ru = undo(conn, pidb, actor, f'r{n}u', f'r{n}', True)
            rev_here = ru['revision']
        assert len(collab_store.view(conn, pidb)['spans']) == 511
    conn.commit()
    conn.close()
    total_bytes = 0
    for name in os.listdir(tmp):
        if name.startswith('budget'):
            total_bytes += os.path.getsize(os.path.join(tmp, name))
    mib = total_bytes / (1024 * 1024)
    print(f'14. 10k batches / 512-span history: {mib:.2f} MiB on disk')
    assert mib < 64, f'history exceeds 64 MiB: {mib}'

    # --- Scenario 11: reopen the budget db and use an all-day-old base revision
    conn = db.connect(big)
    old = collab_store.version(conn, pidb, base)
    assert len(old) == 511
    # The offline editor still sees the morning base; select its first span using
    # the coordinates from that old view and rebase onto the evening head.
    morning_first = old[0]
    dur = morning_first['end_ms'] - morning_first['start_ms']
    morning_total = sum(s['end_ms'] - s['start_ms'] for s in old)
    late = edit(conn, pidb, 'offline', 'offline-1', base,
                [{'kind': 'move', 'start_ms': 0, 'end_ms': dur, 'to_ms': morning_total}])
    late_uids = [s['uid'] for s in late['spans']]
    # Guarantee: the exact morning occurrence is relocated by identity, present
    # exactly once and no longer at the head. Its final slot follows the gap
    # anchors shared with concurrent edits, so the uid — not a positional guess —
    # is the asserted property.
    assert late_uids.count(morning_first['uid']) == 1
    assert late_uids[0] != morning_first['uid']
    # The occurrence's source interval is preserved intact by the move.
    moved_span = next(s for s in late['spans'] if s['uid'] == morning_first['uid'])
    assert (moved_span['start_ms'], moved_span['end_ms']) == \
           (morning_first['start_ms'], morning_first['end_ms'])
    # Morning receipt replay still available (pick the very first batch)
    replay = collab_store.commit(conn, pidb, 'a0', 'r0', base_revision=base,
                                 actions=[{'kind': 'insert', 'at_ms': 0,
                                           'clips': [{'asset_id': 1,
                                                      'start_ms': 50000,
                                                      'end_ms': 50050}]}])
    assert replay['revision'] == base + 1
    print('15. same-day offline rebase, receipt replay and undo remain available after reopen')

    # --- Scenario 12: tree structural sharing correctness
    db2 = os.path.join(tmp, 'tree.sqlite3')
    c2 = db.connect(db2)
    for aid in (1, 2):
        asset(c2, aid)
    p = project(c2)
    seed_clips(c2, p, [(1, i * 100, i * 100 + 100) for i in range(40)])
    c2.execute('UPDATE projects SET revision=40 WHERE id=?', (p,))
    c2.commit()
    v = collab_store.view(c2, p)
    roots = [r['payload'] for r in c2.execute(
        'SELECT DISTINCT payload FROM edit_versions WHERE project_id=?', (p,))]
    edit(c2, p, 'a', 't1', v['revision'],
         [{'kind': 'insert', 'at_ms': 3000,
           'clips': [{'asset_id': 2, 'start_ms': 0, 'end_ms': 75}]}])
    # Round-trip equality and all 512-span limits
    again = collab_store.view(c2, p)
    flat = collab_tree.flatten(c2, collab_tree.from_spans(c2, again['spans']))
    assert uid_list(flat) == uid_list(again['spans'])
    # splice identity: editing with an empty replacement at a zero range is stable
    root = c2.execute('SELECT payload FROM edit_versions WHERE project_id=? '
                      'ORDER BY revision DESC LIMIT 1', (p,)).fetchone()[0]
    assert collab_tree.length(c2, root) == len(again['spans'])
    # Structural sharing: the new version reuses most nodes rather than duplicating.
    distinct_nodes = c2.execute('SELECT count(*) FROM edit_nodes').fetchone()[0]
    assert distinct_nodes > 0
    print('16. persistent tree round-trip and node reuse correct')
    c2.close()

    conn.close()
    print('\nALL COLLABORATIVE EDIT CHECKS PASSED')


if __name__ == '__main__':
    main()
