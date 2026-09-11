import json
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from test_pipeline import setup,seed
from novel_crawler.epub import export_book


def test_epub_joins_sentence_splits_speakers_and_omits_internal_notes(setup):
    s,cfg,book=setup;seed(s,book,1)
    edited={'title':'Trò chuyện','continuity':'Họ trò chuyện.','issues':[],
            'paragraphs':[{'id':1,'text':'Hắn chưa nói','join_previous':False},
                          {'id':2,'text':'xong.\n\n“Phu quân!” “Nương tử!”','join_previous':True}],
            'source_notes':[{'paragraph_ids':[1],'note':'Ghi chú ngữ nghĩa không dành cho người đọc.'}]}
    s.db.execute("UPDATE chapters SET state='edited',edited=? WHERE number=1",(json.dumps(edited),))
    path=export_book(s,book,cfg['output_dir'],preview=True)
    with ZipFile(path) as z:
        root=ET.fromstring(z.read('OEBPS/part-0001.xhtml'))
        paragraphs=root.findall('.//{http://www.w3.org/1999/xhtml}p')
        assert [p.text for p in paragraphs]==['Hắn chưa nói xong.','“Phu quân!”','“Nương tử!”']
        assert 'Ghi chú ngữ nghĩa' not in z.read('OEBPS/part-0001.xhtml').decode()
