// End-to-end batch publishing against a temporary HOME and synthetic skills.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const {spawn}=require("node:child_process");
const path=require("node:path"), fs=require("node:fs"), assert=require("node:assert/strict");
const root=path.resolve(__dirname,"..");
const artifacts=path.join(__dirname,"artifacts","skills-bulk");
fs.mkdirSync(artifacts,{recursive:true});
const fixture=spawn(process.env.PYTHON||"python",["tests/skills_bulk_fixture_server.py"],{cwd:root,stdio:["ignore","pipe","pipe"]});
let stderr=""; fixture.stderr.on("data",d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{
  let output=""; fixture.stdout.on("data",d=>{output+=d; const match=output.match(/READY (\d+)/); if(match)resolve(`http://127.0.0.1:${match[1]}`);});
  fixture.on("exit",code=>reject(new Error(`fixture ${code}: ${stderr}`)));
});
let browser; const errors=[];
async function step(name,fn) { await fn(); console.log("PASS "+name); }

(async()=>{try {
  const url=await ready;
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
  page.on("pageerror",e=>errors.push(e.message));
  let applies=0; page.on("request",request=>{if(request.method()==="POST"&&request.url().endsWith("/api/capabilities/apply")) applies++;});
  await page.goto(url);
  // Permit this standalone regression to run before the host integrates its script.
  if(!await page.evaluate(()=>typeof skillsBulk!=="undefined"))
    await page.addScriptTag({path:path.join(root,"ui","features","capabilities","skills-bulk.js")});
  await page.locator('a[href="#library"]').first().click();
  await page.locator("#skills-bulk-toolbar").waitFor();
  const itemInfo=await page.evaluate(()=>workFeatures.library.items.map(({id,name,targetPaths})=>({id,name,targetPaths})));
  const alpha=itemInfo.find(x=>x.name==="fixture-alpha"), beta=itemInfo.find(x=>x.name==="fixture-beta");
  const alphaFile=path.join(alpha.targetPaths.codex,"SKILL.md"), betaFile=path.join(beta.targetPaths.pi,"SKILL.md");

  await step("only managed skills are selectable and the bulk preview is read-only",async()=>{
    assert.equal(await page.locator("[data-skill-bulk-item]").count(),2);
    assert.equal(await page.locator("#content .feature-row").filter({hasText:"fixture-instructions"}).locator("[data-skill-bulk-item]").count(),0);
    await page.locator('[data-skill-bulk="toggle-skills"]').click();
    assert.equal(await page.locator("[data-skill-bulk-item]:checked").count(),2);
    await page.locator('[data-skill-bulk="preview"]').click();
    await page.locator('[data-skill-bulk="toggle-conflicts"]').waitFor();
    assert.equal(await page.locator("[data-skills-force]").count(),2);
    assert.match(await page.locator("#editor-body").innerText(),/fixture-alpha/);
    assert.match(await page.locator("#editor-body").innerText(),/fixture-beta/);
    await page.evaluate(async()=>{await document.fonts.ready;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
    await page.waitForTimeout(400); // Capture after the dialog's entrance transition.
    await page.screenshot({path:path.join(artifacts,"preview.png"),fullPage:true});
    await page.setViewportSize({width:700,height:900});
    const layout=await page.evaluate(()=>{
      const dialog=document.querySelector('#editor-dialog'), body=document.querySelector('#editor-body');
      const footer=document.querySelector('#editor-footer'), action=footer.querySelector('[data-skill-bulk="repreview"]');
      const d=dialog.getBoundingClientRect(), f=footer.getBoundingClientRect(), a=action.getBoundingClientRect();
      return {noPageOverflow:document.documentElement.scrollWidth<=innerWidth,
        noDialogOverflow:dialog.scrollWidth<=dialog.clientWidth,bodyScrolls:body.scrollHeight>body.clientHeight,
        footerVisible:f.top>=d.top&&f.bottom<=d.bottom&&f.bottom<=innerHeight,
        actionVisible:a.left>=d.left&&a.right<=d.right&&a.top>=d.top&&a.bottom<=d.bottom,
        footerTop:f.top};
    });
    assert.equal(layout.noPageOverflow,true);
    assert.equal(layout.noDialogOverflow,true);
    assert.equal(layout.bodyScrolls,true);
    assert.equal(layout.footerVisible,true);
    assert.equal(layout.actionVisible,true);
    await page.screenshot({path:path.join(artifacts,"preview-narrow.png"),fullPage:true});
    await page.locator('#editor-body').evaluate(el=>{el.scrollTop=el.scrollHeight;});
    assert.ok(Math.abs(await page.locator('#editor-footer').evaluate(el=>el.getBoundingClientRect().top)-layout.footerTop)<1);
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(fs.readFileSync(alphaFile,"utf8").trim(),"old fixture-alpha");
    assert.equal(fs.readFileSync(betaFile,"utf8").trim(),"old fixture-beta");
    assert.equal(applies,0);
  });

  await step("force choices need repreview and confirmation; stale target fails independently",async()=>{
    const oldPlans=await page.evaluate(()=>skillsBulk.entries.map(x=>x.plan.planId));
    await page.locator('[data-skill-bulk="toggle-conflicts"]').click();
    assert.equal(await page.locator("[data-skills-force]:checked").count(),2);
    assert.equal(await page.locator('[data-skill-bulk="apply"]').count(),0);
    await page.locator('[data-skill-bulk="repreview"]').click();
    await page.locator('[data-skill-bulk="apply"]').waitFor();
    assert.equal(await page.locator('[data-skills-force][data-was-forced="true"]:checked').count(),2);
    for(const id of oldPlans) {
      const data=await page.evaluate(async planId=>(await fetch('/api/capabilities/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({planId})})).json(),id);
      assert.equal(data.discarded,false,"replaced plan was already discarded");
    }
    // Choosing differently after a successful forced preview must not apply the older choice.
    await page.locator("[data-skills-force]").first().uncheck();
    assert.equal(await page.locator('[data-skill-bulk="apply"]').isDisabled(),true);
    await page.locator("[data-skills-force]").first().check();
    assert.equal(await page.locator('[data-skill-bulk="apply"]').isEnabled(),true);
    await page.locator('[data-skill-bulk="apply"]').click();
    assert.match(await page.locator("#confirm-description").innerText(),/覆盖 2/);
    await page.locator("#confirm-cancel").click();
    assert.equal(applies,0);
    assert.equal(fs.readFileSync(alphaFile,"utf8").trim(),"old fixture-alpha");
    fs.writeFileSync(betaFile,"changed after preview\n");
    await page.locator('[data-skill-bulk="apply"]').click();
    await page.locator("#confirm-ok").click();
    await page.waitForFunction(()=>document.querySelector('#editor-body')?.innerText.includes('应用失败'));
    const alphaResult=page.locator(`[data-skills-entry="${alpha.id}"]`), betaResult=page.locator(`[data-skills-entry="${beta.id}"]`);
    assert.match(await alphaResult.innerText(),/已发布，恢复点/);
    assert.match(await betaResult.innerText(),/应用失败.*目标在预览后发生变化/);
    assert.match(fs.readFileSync(alphaFile,"utf8"),/new fixture-alpha/);
    assert.equal(fs.readFileSync(betaFile,"utf8"),"changed after preview\n");
    assert.equal(applies,2);
    const history=await page.evaluate(async id=>(await fetch(`/api/capabilities/${id}/history`)).json(),alpha.id);
    assert.equal(history.transactions.length,1);
    const refreshed=await page.evaluate(()=>workFeatures.library.items);
    assert.equal(refreshed.find(x=>x.id===alpha.id).targetStatus.codex,"current");
    assert.equal(refreshed.find(x=>x.id===beta.id).targetStatus.pi,"conflict");
    assert.match(await page.locator("#content .feature-row").filter({hasText:"fixture-alpha"}).innerText(),/与发布版一致/);
    assert.equal(refreshed.find(x=>x.id===alpha.id).mode,"manual");
    assert.equal(refreshed.find(x=>x.id===beta.id).mode,"manual");
    await page.locator('#editor-footer [data-action="close-editor"]').click();
  });

  await step("single-item preview offers select all without overriding blocked targets",async()=>{
    fs.writeFileSync(alphaFile,"independent edit\n");
    await page.locator("#content .feature-row").filter({hasText:"fixture-alpha"}).locator('[data-feature="preview"]').click();
    assert.equal(await page.locator('#editor-dialog').evaluate(el=>el.classList.contains('skills-bulk-dialog')),false);
    assert.equal(await page.locator('#editor-body').evaluate(el=>getComputedStyle(el).overflowY),"visible");
    await page.locator('[data-skill-bulk="toggle-single-conflicts"]').click();
    assert.equal(await page.locator('[name="work-force"]:checked').count(),1);
    await page.locator('[data-skill-bulk="toggle-single-conflicts"]').click();
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.evaluate(()=>showWorkPreview({itemId:"synthetic-blocked",hasConflicts:true,results:[
      {agent:"codex",status:"conflict",blockedByRecovery:true,detail:"此目标有未完成事务"}
    ]}));
    assert.equal(await page.locator('[name="work-force"]').count(),0);
    assert.equal(await page.locator('[data-skill-bulk="toggle-single-conflicts"]').count(),0);
    await page.locator('#editor-footer [data-action="close-editor"]').click();
  });

  await step("blocked batch targets are visible but never included in select all",async()=>{
    const pattern=`**/api/capabilities/${beta.id}/preview`;
    await page.route(pattern,route=>route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({
      planId:"b".repeat(32),itemId:beta.id,hasConflicts:true,warnings:[],results:[
        {agent:"pi",status:"conflict",blockedByRecovery:true,detail:"此目标有未完成事务"}
      ]})}));
    await page.locator('[data-skill-bulk="preview"]').click();
    await page.locator(`[data-skills-entry="${beta.id}"]`).waitFor();
    assert.match(await page.locator(`[data-skills-entry="${beta.id}"]`).innerText(),/未完成事务/);
    assert.equal(await page.locator(`[data-skills-entry="${beta.id}"] [data-skills-force]`).count(),0);
    await page.locator('[data-skill-bulk="toggle-conflicts"]').click();
    assert.equal(await page.locator(`[data-skills-entry="${beta.id}"] [data-skills-force]:checked`).count(),0);
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.unroute(pattern);
  });

  await step("repreview failure is shown per item and no obsolete plan survives",async()=>{
    await page.locator('[data-skill-bulk="preview"]').click();
    await page.locator('[data-skill-bulk="toggle-conflicts"]').waitFor();
    const oldPlans=await page.evaluate(()=>skillsBulk.entries.map(x=>x.plan.planId));
    await page.locator('[data-skill-bulk="toggle-conflicts"]').click();
    const pattern=`**/api/capabilities/${alpha.id}/preview`;
    await page.route(pattern,route=>route.fulfill({status:409,contentType:"application/json",body:JSON.stringify({error:"模拟预览失败"})}));
    await page.locator('[data-skill-bulk="repreview"]').click();
    await page.waitForFunction(()=>document.querySelector('#editor-body')?.innerText.includes('模拟预览失败'));
    assert.match(await page.locator(`[data-skills-entry="${alpha.id}"]`).innerText(),/预览失败.*模拟预览失败/);
    assert.match(await page.locator(`[data-skills-entry="${beta.id}"]`).innerText(),/已选择覆盖/);
    for(const id of oldPlans) {
      const data=await page.evaluate(async planId=>(await fetch('/api/capabilities/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({planId})})).json(),id);
      assert.equal(data.discarded,false);
    }
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.unroute(pattern);
  });

  await step("closing during initial preview discards the late plan",async()=>{
    let interceptedResolve, releaseResolve, latePlan;
    const intercepted=new Promise(resolve=>interceptedResolve=resolve), release=new Promise(resolve=>releaseResolve=resolve);
    const pattern=`**/api/capabilities/${alpha.id}/preview`;
    await page.route(pattern,async route=>{
      interceptedResolve(); await release;
      const response=await route.fetch(), payload=await response.json(); latePlan=payload.planId;
      await route.fulfill({response});
    });
    await page.locator('[data-skill-bulk="preview"]').click();
    await intercepted;
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.locator('#editor-dialog').waitFor({state:"hidden"});
    const lateDiscard=page.waitForResponse(r=>r.url().endsWith('/api/capabilities/discard')&&
      JSON.parse(r.request().postData()||'{}').planId===latePlan);
    releaseResolve();
    await lateDiscard;
    await page.unroute(pattern);
    assert.equal(await page.locator('#editor-dialog').isVisible(),false);
    const data=await page.evaluate(async planId=>(await fetch('/api/capabilities/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({planId})})).json(),latePlan);
    assert.equal(data.discarded,false);
    assert.equal(applies,2);
  });

  await step("closing while repreview is in flight discards old and late plans",async()=>{
    await page.locator('[data-skill-bulk="preview"]').click();
    await page.locator('[data-skill-bulk="toggle-conflicts"]').waitFor();
    const oldPlans=await page.evaluate(()=>skillsBulk.entries.map(x=>x.plan.planId));
    await page.locator('[data-skill-bulk="toggle-conflicts"]').click();
    let interceptedResolve, releaseResolve, latePlan;
    const intercepted=new Promise(resolve=>interceptedResolve=resolve), release=new Promise(resolve=>releaseResolve=resolve);
    const pattern=`**/api/capabilities/${alpha.id}/preview`;
    await page.route(pattern,async route=>{
      interceptedResolve(); await release;
      const response=await route.fetch(), payload=await response.json(); latePlan=payload.planId;
      await route.fulfill({response});
    });
    await page.locator('[data-skill-bulk="repreview"]').click();
    await intercepted;
    assert.equal(await page.locator('[data-skills-force]').first().isDisabled(),true);
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.locator('#editor-dialog').waitFor({state:"hidden"});
    const lateDiscard=page.waitForResponse(r=>r.url().endsWith('/api/capabilities/discard')&&
      JSON.parse(r.request().postData()||'{}').planId===latePlan);
    releaseResolve();
    await lateDiscard;
    await page.unroute(pattern);
    assert.equal(await page.locator('#editor-dialog').isVisible(),false);
    for(const id of [...oldPlans,latePlan]) {
      const data=await page.evaluate(async planId=>(await fetch('/api/capabilities/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({planId})})).json(),id);
      assert.equal(data.discarded,false,"cancelled plan was already discarded");
    }
    assert.equal(applies,2);
  });

  await step("a lost apply response is marked uncertain and never retried",async()=>{
    await page.locator('[data-skill-bulk="preview"]').click();
    await page.locator('[data-skill-bulk="toggle-conflicts"]').waitFor();
    await page.locator('[data-skill-bulk="toggle-conflicts"]').click();
    await page.locator('[data-skill-bulk="repreview"]').click();
    await page.locator('[data-skill-bulk="apply"]').waitFor();
    let count=0;
    await page.route('**/api/capabilities/apply',async route=>{
      if(count++===0) { await route.fetch(); await route.abort(); }
      else await route.continue();
    });
    await page.locator('[data-skill-bulk="apply"]').click();
    await page.locator('#confirm-ok').click();
    await page.waitForFunction(()=>document.querySelector('#editor-body')?.innerText.includes('结果待确认'));
    assert.match(await page.locator(`[data-skills-entry="${alpha.id}"]`).innerText(),/结果待确认/);
    assert.match(await page.locator(`[data-skills-entry="${beta.id}"]`).innerText(),/已发布，恢复点/);
    assert.equal(count,2,"no automatic retry of the uncertain plan");
    const history=await page.evaluate(async id=>(await fetch(`/api/capabilities/${id}/history`)).json(),alpha.id);
    assert.equal(history.transactions.length,2,"server committed despite losing the response");
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.unroute('**/api/capabilities/apply');
  });
  assert.deepEqual(errors,[]);
  console.log("PASS isolated skills bulk browser acceptance");
} catch(e) { console.error(e); process.exitCode=1; }
finally { if(browser)await browser.close(); fixture.kill(); }})();
