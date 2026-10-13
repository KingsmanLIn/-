from pathlib import Path
import json, shutil, sys, tempfile, uuid
work=Path(__file__).resolve().parent
stage=next((work.parent/'outputs').glob('*1.3.3*便携版'))
code=work/'review-1.3.2'
for source in code.glob('*.py'):
    shutil.copy2(source,stage/source.name)
data=work/('release-browser-data-'+uuid.uuid4().hex[:8])
assert not data.exists()
data.mkdir()
tmp=work/'release-child-scratch';tmp.mkdir(exist_ok=True)
tempfile.tempdir=str(tmp)
sys.path[:0]=[str(stage),str(code/'tests')]
import server,analysis,providers,pdf_fixtures
from card_fixtures import build_card,pages_from_prompt
fixture=work/'release-synthetic-paper.pdf'
pdf_fixtures.write_pdf(fixture,pdf_fixtures.zh_document())
store=server.Store(data)
paper=store.import_pdf(fixture.read_bytes(),'合成验收论文.pdf')
store.update_ai_settings({'provider':'ollama','model':'synthetic-model',
                          'base_url':'http://127.0.0.1:1'})
fake=providers.FakeProvider(script=['success'],payload={},model='synthetic-model')
fake.payload_for=lambda request,_:build_card(pages_from_prompt(request.user))
manager=analysis.AnalysisManager(store,provider_factory=lambda _:fake)
manager.enqueue(paper['id'],reason='manual')
manager.process_once()
assert store.analysis_meta(paper['id'])['status']=='ready',store.analysis_meta(paper['id'])
manager.pause(True)
record={'stage':str(stage),'data':str(data),'paper_id':paper['id'],'pdf':str(fixture),
        'ready':True,'stub_calls':len(fake.calls),'extraction':'portable Python child'}
(work/'release-smoke-input.json').write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
print(json.dumps(record,ensure_ascii=False))
