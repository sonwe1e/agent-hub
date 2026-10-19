// OAuth uses a local synthetic vendor; no browser account or live files touched.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),out=path.join(__dirname,'artifacts','workbuddy');
fs.mkdirSync(out,{recursive:true});
const fixture=spawn(process.env.PYTHON||'python',['tests/workbuddy_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
let stderr='';fixture.stderr.on('data',d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{let stdout='';fixture.stdout.on('data',d=>{stdout+=d;const m=stdout.match(/READY (\d+)/);if(m)resolve(`http://127.0.0.1:${m[1]}`);});fixture.on('exit',code=>reject(new Error(`fixture ${code}: ${stderr}`)));});
let browser;const passed=[];
const step=async(name,fn)=>{await fn();passed.push(name);console.log('PASS '+name);};
(async()=>{try{
 const url=await ready;
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const card=page.locator('#workbuddy-settings');
 await step('settings displays overseas sign-in without asking for an API key',async()=>{
   await page.goto(url+'/#settings');await card.getByRole('button',{name:'登录 WorkBuddy 海外版',exact:true}).waitFor();
   assert.match(await card.innerText(),/尚未登录/);await page.screenshot({path:path.join(out,'signed-out.png'),fullPage:true});
 });
 await step('browser login link is HTTPS and pending login can be canceled',async()=>{
   await card.locator('[data-workbuddy="login"]').click();await card.locator('.workbuddy-login-link').waitFor();
   assert.equal(new URL(await card.locator('.workbuddy-login-link').getAttribute('href')).protocol,'https:');
   await card.locator('[data-workbuddy="cancel"]').click();await card.locator('.workbuddy-login-link').waitFor({state:'hidden'});
 });
 await step('OAuth completion automatically registers the provider and account model catalog',async()=>{
   await card.locator('[data-workbuddy="login"]').click();await card.locator('.workbuddy-login-link').waitFor();
   await page.request.post(url+'/fixture/authorize');await page.waitForFunction(()=>document.querySelector('#workbuddy-settings')?.innerText.includes('已接入'));
   assert.match(await card.innerText(),/Fixture User/);assert.match(await card.innerText(),/企业账号/);assert.match(await card.innerText(),/6 个已保存模型/);
   await card.locator('[data-workbuddy="connect"]:not([disabled])').waitFor();
   const providers=await (await page.request.get(url+'/api/providers')).json();
   assert.equal(providers.providers['workbuddy-ai'].kind,'workbuddy-ai');assert.doesNotMatch(JSON.stringify(providers),/new-access-secret|new-refresh-secret/);
   await page.screenshot({path:path.join(out,'connected.png'),fullPage:true});
 });
 await step('all four agent model selectors offer WorkBuddy and the discovered models',async()=>{
   await page.locator('nav a[href="#agents"]').click();
   for(const agent of ['claude','codex','pi','dsh']){
     await page.locator(`[data-daily="switch"][data-agent="${agent}"]`).click();
     await page.locator('#quick-provider').selectOption('workbuddy-ai');await page.locator('#quick-model').selectOption('ok');
     assert.equal(await page.locator('#quick-model option').count(),7);
     assert.equal(await page.locator('#quick-model').inputValue(),'ok');
     await page.locator('#editor-footer [data-action="close-editor"]').click();
     if(await page.locator('#confirm-dialog').isVisible())await page.locator('#confirm-ok').click();
     await page.locator('#editor-dialog').waitFor({state:'hidden'});
   }
 });
 await step('refresh, narrow layout, and logout clear account access without losing the catalog',async()=>{
   await page.locator('nav a[href="#settings"]').click();await card.locator('[data-workbuddy="connect"]').click();
   await card.locator('[data-workbuddy="connect"]:not([disabled])').waitFor();
   await page.setViewportSize({width:390,height:844});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   await page.screenshot({path:path.join(out,'connected-mobile.png'),fullPage:true});
   await card.locator('[data-workbuddy="logout"]').click();await card.locator('[data-workbuddy="login"]').waitFor();
   await page.waitForFunction(()=>document.querySelector('#workbuddy-settings')?.innerText.includes('尚未登录'));
   const status=await (await page.request.get(url+'/api/workbuddy')).json();assert.equal(status.signedIn,false);assert.equal(status.connected,false);assert.equal(status.models,6);
 });
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify({passed,errors},null,2));
 console.log('PASS all WorkBuddy UI acceptance checks');
}catch(e){console.error(e);if(browser){for(const p of browser.contexts().flatMap(c=>c.pages())){await p.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error((await p.locator('body').innerText()).slice(-5000));}}process.exitCode=1;
}finally{if(browser)await browser.close();fixture.kill();}})();
