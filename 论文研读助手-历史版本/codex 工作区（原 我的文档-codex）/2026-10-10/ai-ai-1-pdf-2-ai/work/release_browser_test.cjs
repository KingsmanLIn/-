const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/ASUS/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const work = __dirname;
const input = JSON.parse(fs.readFileSync(path.join(work, 'release-smoke-input.json'), 'utf8'));
const out = path.join(work, '..', 'outputs');
const py = path.join(input.stage, 'runtime', 'python.exe');
const report = {version:'1.3.3', synthetic:true, real_api:false, checks:[], errors:[]};
const delay = ms => new Promise(r=>setTimeout(r,ms));
const env = {...process.env, PAPER_READER_NO_DIALOG:'1', PAPER_READER_NO_BROWSER:'1'};
for (const key of Object.keys(env)) if (/^(PAPER_READER_.*KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|DEEPSEEK_API_KEY)$/i.test(key)) delete env[key];
let child, browser, page;
const check = (name, value, evidence=null) => {
  report.checks.push({name, ok:!!value, evidence});
  assert(value, name);
};
(async()=>{
  try {
    const empty = JSON.parse(fs.readFileSync(path.join(input.stage,'data/library.json'),'utf8'));
    check('Release starts with empty library and AI disabled', empty.papers.length===0 && empty.settings.ai.provider==='none');
    child=cp.spawn(py,['-I',path.join(input.stage,'server.py'),'--data-dir',input.data,'--port','18765','--no-worker'],
      {cwd:input.stage, env, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let errors=''; child.stderr.on('data',b=>{errors += b.toString()}); child.stdout.resume();
    let instance;
    for(let i=0;i<100;i++){
      try {
        instance=JSON.parse(fs.readFileSync(path.join(input.data,'run-instance.json'),'utf8'));
        const r=await fetch(`http://127.0.0.1:${instance.port}/api/health`);
        if(r.ok) break;
        instance=null;
      } catch {instance=null;}
      if(child.exitCode!==null) throw Error('Portable server exited: '+errors);
      await delay(100);
    }
    check('Portable server becomes healthy',!!instance);
    const base=`http://127.0.0.1:${instance.port}`;
    const unauthorized=await fetch(base+'/api/state');
    check('Anonymous library access refused',unauthorized.status===403);
    // Capability is consumed in memory only and is never printed or saved in reports.
    const cap=cp.spawnSync(py,['-I','-c','import launcher_pipe,sys; c,e=launcher_pipe.request_bootstrap(sys.argv[1]); print(c); sys.exit(0 if c else 1)',instance.pipe],
      {cwd:input.stage,env,windowsHide:true,encoding:'utf8'});
    check('Current-user launcher pipe produces session bootstrap',cap.status===0 && cap.stdout.trim().length>10);
    browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
    const context=await browser.newContext({viewport:{width:1366,height:768}});
    page=await context.newPage();
    const jsErrors=[]; page.on('pageerror',e=>jsErrors.push(e.message));
    let answerFetches=0;
    page.on('request',r=>{if(new URL(r.url()).pathname==='/api/analysis/'+input.paper_id) answerFetches++});
    await page.goto(base+'/#bootstrap='+encodeURIComponent(cap.stdout.trim()));
    await page.waitForFunction(()=>document.querySelector('#pdf-canvas')?.width>100);
    await page.waitForFunction(()=>document.querySelector('#pdf-loading')?.hidden || getComputedStyle(document.querySelector('#pdf-loading')).display==='none');
    check('Authenticated reader opens synthetic PDF', (await page.locator('#paper-title').textContent()).includes('合成验收论文'));
    check('Bootstrap removed from address',!page.url().includes('bootstrap='));
    await page.waitForTimeout(700);
    const dimensions=await page.evaluate(()=>{
      const p=document.querySelector('#pdf-page').getBoundingClientRect(), v=document.querySelector('#pdf-scroll').getBoundingClientRect();
      return {page:[p.width,p.height],viewport:[v.width,v.height],zoom:document.querySelector('#zoom-select').value};
    });
    check('Entire PDF page fits its reading viewport',await page.evaluate(()=>{
      const p=document.querySelector('#pdf-page').getBoundingClientRect(), v=document.querySelector('#pdf-scroll').getBoundingClientRect();
      return p.height<=v.height+3 && p.width<=v.width+3;
    }),dimensions);
    await page.locator('#pdf-scroll').hover(); await page.mouse.wheel(0,500);
    await page.waitForFunction(()=>document.querySelector('#page-input').value==='2');
    check('Mouse wheel turns one complete PDF page',await page.locator('#page-input').inputValue()==='2');
    await page.screenshot({path:path.join(out,'1.3.3-阅读界面-1366x768.png')});
    await page.locator('[data-tab="card"]').click();
    check('AI answers are hidden before user click',await page.locator('.ai-panel').count()===0 && answerFetches===0);
    const before=await page.evaluate(async()=>JSON.stringify((await (await fetch('/api/state')).json()).papers[0].fields));
    await page.locator('.ai-open').first().click();
    await page.locator('.ai-answer').first().waitFor({state:'visible'});
    check('Explicit click reveals verified reading reference',answerFetches>0 && await page.locator('.ai-page').count()>0);
    const after=await page.evaluate(async()=>JSON.stringify((await (await fetch('/api/state')).json()).papers[0].fields));
    check('Revealing reference does not overwrite personal reading card',before===after);
    await page.screenshot({path:path.join(out,'1.3.3-AI参考-按需显示.png')});
    await page.locator('#toggle-notes').click();
    await page.waitForTimeout(300);
    check('Collapsing notes increases PDF reading width',await page.evaluate(()=>document.querySelector('#pdf-scroll').getBoundingClientRect().width>700));
    await page.setViewportSize({width:1920,height:1080});
    await page.waitForTimeout(400);
    check('Full page still fits after resize',await page.evaluate(()=>{
      const p=document.querySelector('#pdf-page').getBoundingClientRect(),v=document.querySelector('#pdf-scroll').getBoundingClientRect();
      return p.height<=v.height+3 && p.width<=v.width+3;
    }));
    await page.screenshot({path:path.join(out,'1.3.3-整页阅读-1920x1080.png')});
    check('No browser JavaScript errors',jsErrors.length===0,jsErrors);
  } catch(e){
    if(page) await page.screenshot({path:path.join(out,'1.3.3-验收失败截图.png')});
    report.errors.push({message:e.message,stack:e.stack});
    process.exitCode=1;
  } finally {
    if(browser) await browser.close();
    if(child && child.exitCode===null){child.kill(); await delay(300);}
    report.pass=report.errors.length===0 && report.checks.every(c=>c.ok);
    fs.writeFileSync(path.join(work,'fixed-release-browser.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  }
})();
