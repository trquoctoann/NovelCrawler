from email.message import EmailMessage
from email.utils import make_msgid
import os
import smtplib
import ssl
import json

from .epub import export_book, verify_epub


def send_book(store, book, config):
    cfg = config['kindle']
    if not cfg['enabled']:
        return 'disabled'
    # One delivery per book, including ambiguous outcomes; never auto-resend.
    previous = store.db.execute('SELECT state FROM delivery WHERE book_id=?', (book['id'],)).fetchone()
    if previous:
        return previous['state']
    path = export_book(store, book, config['output_dir'])
    if not cfg['sender'] or not cfg['recipient'].lower().endswith('@kindle.com'):
        raise ValueError('Configure sender and personal @kindle.com recipient')
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = cfg['sender'], cfg['recipient'], book['title']
    msg['Message-ID'] = make_msgid()
    msg.set_content('Completed personal reading edition.')
    msg.add_attachment(path.read_bytes(), maintype='application', subtype='epub+zip', filename=path.name)
    payload = msg.as_bytes()
    if len(payload) > cfg['max_message_bytes']:
        raise ValueError('Email including attachment exceeds configured MIME size limit')
    if cfg.get('transport', 'smtp') == 'gmail_connector':
        folder = store.path.parent / 'outbox'
        folder.mkdir(exist_ok=True)
        snapshot = folder / path.name
        temp = snapshot.with_suffix('.tmp')
        temp.write_bytes(path.read_bytes())
        temp.replace(snapshot)
        entry = {'book_id': book['id'], 'from_address': cfg['sender'], 'to': cfg['recipient'],
                 'subject': book['title'], 'path': str(snapshot.resolve()),
                 'sha256': verify_epub(snapshot), 'size': snapshot.stat().st_size}
        manifest = folder / (book['id'] + '.json')
        temp = manifest.with_suffix('.tmp')
        temp.write_text(json.dumps(entry, ensure_ascii=False), encoding='utf-8')
        temp.replace(manifest)
        store.db.execute('INSERT INTO delivery VALUES(?,?,?,?,?)',
                         (book['id'], entry['sha256'], 'ready', msg['Message-ID'], None))
        return 'ready'
    if not cfg['smtp_host']:
        raise ValueError('Configure SMTP host')
    username, password = os.environ[cfg['username_env']], os.environ[cfg['password_env']]
    try:
        with smtplib.SMTP(cfg['smtp_host'], cfg['smtp_port'], timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            # Connecting/authenticating cannot deliver mail. Reserve only when
            # transmission is about to start; earlier failures are safe to retry.
            store.db.execute('INSERT INTO delivery(book_id,sha256,state,message_id) VALUES(?,?,?,?)',
                             (book['id'], verify_epub(path), 'sending', msg['Message-ID']))
            refused = smtp.sendmail(cfg['sender'], [cfg['recipient']], payload)
            if refused:
                raise RuntimeError('Recipient refused by SMTP')
            store.db.execute("UPDATE delivery SET state='submitted' WHERE book_id=?", (book['id'],))
        return 'submitted' # SMTP accepted, NOT a claim of arrival on the physical Kindle
    except Exception as exc:
        store.db.execute("UPDATE delivery SET state='unknown',error=? WHERE book_id=? AND state='sending'",
                         (type(exc).__name__, book['id']))
        raise
