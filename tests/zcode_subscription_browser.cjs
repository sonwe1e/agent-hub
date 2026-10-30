// Local browser fixture; all account values and client files are synthetic.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const fixture=spawn(process.env.PYTHON||'python',['tests/zcode_subscription_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
let stderr='';fixture.stderr.on('data',d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{let stdout='';fixture.stdout.on('data',d=>{stdout+=d;const m=stdout.match(/READY (\d+)/);if(m)resolve(`http://127.0.0.1:${m[1]}`);});fixture.on('exit',code=>reject(new Error(`fixture ${code}: ${stderr}`)));});
let browser;const passed=[];
const step=async(name,fn)=>{await fn();passed.push(name);console.log('PASS '+name);};
(async()=>{try{
 const url=await ready;
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const card=page.locator('#zcode-subscription-settings');
 await step('existing ZCode login can be connected without copying credentials',async()=>{
   await page.goto(url+'/#settings');await card.locator('[data-zcode-subscription="connect"]').waitFor();
   assert.match(await card.innerText(),/已登录/);
   assert.match(await card.innerText(),/最近成功.*pi.*最近失败或中断.*codex.*fixture 请求受限/s);
   await card.locator('[data-zcode-subscription="connect"]').click();
   await page.waitForFunction(()=>document.querySelector('#zcode-subscription-settings')?.innerText.includes('已接入'));
   const data=await (await page.request.get(url+'/api/providers')).json();
   assert.equal(data.providers['zcode-subscription'].kind,'zcode-subscription');
   assert.doesNotMatch(JSON.stringify(data),/fixture-ui-secret/);
 });
 await step('all five agent selectors offer the account model',async()=>{
   await page.locator('nav a[href="#agents"]').click();
   for(const agent of ['claude','codex','pi','dsh','opencode']){
     await page.locator(`[data-daily="agent-detail"][data-agent="${agent}"]`).click();
     await page.locator(`#editor-body [data-daily="switch"][data-agent="${agent}"]`).click();
     await page.locator('#quick-provider').selectOption('zcode-subscription');
     await page.locator('#quick-model').selectOption('fixture-model');
     await page.locator('#editor-footer [data-action="close-editor"]').click();
     if(await page.locator('#confirm-dialog').isVisible())await page.locator('#confirm-ok').click();
     await page.locator('#editor-dialog').waitFor({state:'hidden'});
   }
 });
 await step('explicit quota refresh shows source, timestamp and preserves keyboard focus',async()=>{
   await page.locator('nav a[href="#settings"]').click();
   const button=card.locator('[data-zcode-subscription="quota"]');await button.focus();await page.keyboard.press('Enter');
   await page.waitForFunction(()=>document.querySelector('#zcode-subscription-settings')?.innerText.includes('已使用 60%'));
   assert.match(await card.innerText(),/fixture 套餐额度.*查询于/s);
   assert.equal(await page.evaluate(()=>document.activeElement?.dataset.zcodeSubscription),'quota');
   await card.locator('[data-zcode-subscription="refresh"]').click();
   await page.waitForFunction(()=>document.querySelector('#zcode-subscription-settings')?.innerText.includes('尚未查询上游额度'));
 });
 await step('narrow settings and disconnect preserve the native login',async()=>{
   await page.locator('nav a[href="#settings"]').click();await card.locator('[data-zcode-subscription="disconnect"]').waitFor();
   await page.setViewportSize({width:390,height:844});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   await card.locator('[data-zcode-subscription="disconnect"]').click();
   await page.locator('#confirm-ok').click();
   await page.locator('#zcode-subscription-settings [data-zcode-subscription="disconnect"]').waitFor({state:'detached'});
   const status=await (await page.request.get(url+'/api/zcode-subscription')).json();
   assert.equal(status.signedIn,true);assert.equal(status.connected,false);assert.equal(status.models,1);
 });
 assert.deepEqual(errors,[]);console.log(`ZCode subscription browser regression: ${passed.length} passed`);
}catch(e){console.error(e);process.exitCode=1;}
finally{if(browser)await browser.close();fixture.kill();}})();
