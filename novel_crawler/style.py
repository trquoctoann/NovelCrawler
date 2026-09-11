"""Versioned literary edits: retain old prose and re-edit it under an explicit new style."""


def apply_style_revision(store, cfg, book):
    revision = cfg['agent'].get('style_revision', 'legacy')
    old = store.db.execute('SELECT style_revision FROM books WHERE id=?', (book['id'],)).fetchone()[0]
    if revision == old:
        return 0
    if store.db.execute('SELECT 1 FROM final_artifacts WHERE book_id=?', (book['id'],)).fetchone():
        raise ValueError('A published artifact exists; use a new book edition for style changes')
    if store.db.execute("SELECT 1 FROM calls WHERE book_id=? AND status='reserved'", (book['id'],)).fetchone():
        raise ValueError('Finish or recover the active call before changing style')
    with store.transaction():
        rows = store.db.execute('SELECT number,edited FROM chapters WHERE book_id=? AND edited IS NOT NULL',
                                (book['id'],)).fetchall()
        for ch in rows:
            store.db.execute('INSERT OR IGNORE INTO edited_history VALUES(?,?,?,?)',
                             (book['id'], ch['number'], old, ch['edited']))
            store.db.execute("UPDATE chapters SET translated=edited,edited=NULL,state='translated',error=NULL "
                             'WHERE book_id=? AND number=?', (book['id'], ch['number']))
        store.db.execute("UPDATE chapters SET state='translated',error=NULL WHERE book_id=? AND state='edit_failed' "
                         'AND translated IS NOT NULL', (book['id'],))
        for term in book.get('contextual_terms', []):
            store.db.execute("UPDATE book_terms SET kind='preferred' WHERE book_id=? AND source=?", (book['id'], term))
        store.db.execute('UPDATE books SET style_revision=? WHERE id=?', (revision, book['id']))
    return len(rows)
