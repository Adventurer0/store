// DOM interaction tests with an in-memory API double. This is not browser / live-platform QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const delay = () => new Promise(resolve => setTimeout(resolve, 10));
const deferred = () => {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
async function until(fn) {
  for (let i=0;i<100;i++) { if(fn()) return; await delay(); }
  throw Error('UI condition did not become true');
}

test('owner can create store, multi-SKU product, mapping and review a video batch', async()=>{
  const root=path.join(__dirname,'..');
  const dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:3000/',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window, doc=w.document, requests=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
  let authenticated=false, initialized=false;
  const platform={name:'抖音小店',video_name:'抖音',color:'#333333',commerce:['product_create','order_sync'],link:true,portal:'https://fxg.jinritemai.com/'};
  const state={stores:[],products:[],listings:[],media:[{id:'video01',name:'测试.mp4',kind:'video',size:1024,created:1}],stats:{products:0,orders:0,pending:0,issues:0},platforms:{douyin:platform},engine_ready:true,desktop_ready:false,max_upload_mb:1024,version:'0.1.0'};
  const jobs=[];
  w.fetch=async(url,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    requests.push({url,method,body,headers:options.headers});
    let result;
    if(url==='/api/auth/status') result={initialized,authenticated,csrf:authenticated?'test-csrf':'',username:'admin'};
    else if(url==='/api/auth/setup') {initialized=true;authenticated=true;result={ok:true,csrf:'test-csrf'};}
    else if(url==='/api/auth/logout') {authenticated=false;result={ok:true};}
    else if(url==='/api/state') result=JSON.parse(JSON.stringify(state));
    else if(url==='/api/stores'&&method==='POST') {const s={...body,id:'store01',created:1,video_status:'valid',commerce_status:'unverified'};delete s.credentials;state.stores.push(s);result=s;}
    else if(url==='/api/products'&&method==='POST') {const p={...body,id:'product01',version:1,created:1,updated:1};state.products.push(p);state.stats.products=1;result=p;}
    else if(url==='/api/products/product01/listings/store01') {state.listings=[{product_id:'product01',store_id:'store01',...body}];result={ok:true};}
    else if(url==='/api/jobs/preflight') result={ready:1,items:[{store_id:'store01',store_name:'我的抖音店',platform:'douyin',kind:body.kind,problem:'',payload:{}}]};
    else if(url==='/api/jobs'&&method==='POST') {jobs.push({id:'job01',store_id:'store01',store_name:'我的抖音店',platform:'douyin',kind:body.kind,status:'queued',scheduled:1,attempts:0,payload:body,result:{message:'等待执行'}});result={jobs:[{id:'job01',status:'queued',duplicate:false}]};state.stats.pending=1;}
    else if(url.startsWith('/api/jobs?')) result={rows:jobs,total:jobs.length};
    else if(url.startsWith('/api/orders?')) result={rows:[],total:0,offset:0,limit:50};
    else if(url==='/api/audit') result=[];
    else throw Error('Unexpected API request '+method+' '+url);
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
  };
  const click=selector=>{const el=doc.querySelector(selector);assert.ok(el,selector);el.click();};
  const submit=selector=>doc.querySelector(selector).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  const set=(form,name,value)=>{const el=doc.querySelector(form).elements.namedItem(name);assert.ok(el,name);el.value=value;};
  try {
    w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
    await until(()=>doc.querySelector('#auth-form'));
    set('#auth-form','password','test-only-password-123');
    submit('#auth-form');
    await until(()=>doc.querySelector('#main h1'));
    assert.equal(doc.querySelector('#shell').hidden,false);
    click('[data-action="new-store"][data-platform="douyin"]');
    set('#store-form','name','我的抖音店');
    submit('#store-form');
    await until(()=>state.stores.length===1&&!doc.querySelector('#modal').open);
    assert.equal(requests.find(r=>r.url==='/api/stores').body.platform,'douyin');
    assert.equal(requests.find(r=>r.url==='/api/stores').body.commerce_enabled,true);
    assert.equal('api_enabled' in requests.find(r=>r.url==='/api/stores').body,false);
    assert.deepEqual(Object.keys(requests.find(r=>r.url==='/api/stores').body.credentials),['video_app_id','video_access_token']);
    click('[data-action="new-product"]');
    set('#product-form','title','我的银色骑行行李箱');
    const first=doc.querySelector('#sku-rows .sku-row');
    first.querySelector('[data-key="code"]').value='SILVER';
    first.querySelector('[data-key="name"]').value='银色';
    first.querySelector('[data-key="price"]').value='1299.90';
    first.querySelector('[data-key="stock"]').value='10';
    click('[data-action="add-sku"]');
    const second=doc.querySelectorAll('#sku-rows .sku-row')[1];
    second.querySelector('[data-key="code"]').value='BLACK';
    second.querySelector('[data-key="name"]').value='黑色';
    second.querySelector('[data-key="price"]').value='1399';
    second.querySelector('[data-key="stock"]').value='2';
    submit('#product-form');
    await until(()=>state.products.length===1&&!doc.querySelector('#modal').open);
    assert.equal(state.products[0].skus[0].price_cents,129990);
    assert.equal(state.products[0].skus.length,2);
    w.location.hash='products';
    await until(()=>doc.querySelector('#product-search'));
    click('[data-action="mappings"]');
    set('#mapping-form','store01_id','90071992547409999');
    set('#mapping-form','store01_link','https://example.com/merchant-item');
    submit('#mapping-form');
    await until(()=>state.listings.length===1&&!doc.querySelector('#modal').open);
    assert.equal(state.listings[0].external_id,'90071992547409999');
    w.location.hash='videos';
    await until(()=>doc.querySelector('#video-form'));
    set('#video-form','media_id','video01');
    set('#video-form','product_id','product01');
    set('#video-form','title','骑行箱测试视频');
    doc.querySelector('#video-form [name="store_ids"]').checked=true;
    submit('#video-form');
    await until(()=>doc.querySelector('[data-action="commit-batch"]'));
    const preflight=requests.find(r=>r.url==='/api/jobs/preflight');
    assert.deepEqual(preflight.body.store_ids,['store01']);
    assert.equal(preflight.body.attach_product,true);
    assert.equal(preflight.headers['X-CSRF-Token'],'test-csrf');
    assert.equal(jobs.length,0,'preflight must not publish');
    click('[data-action="commit-batch"]');
    await until(()=>jobs.length===1&&doc.querySelector('#main').textContent.includes('等待执行'));
    w.location.hash='orders';
    await until(()=>doc.querySelector('#order-search'));
    assert.match(doc.querySelector('#main').textContent,/暂无订单/);
    w.location.hash='settings';
    await until(()=>doc.querySelector('#main').textContent.includes('模板变量'));
    click('#logout');
    await until(()=>doc.querySelector('#shell').hidden && doc.querySelector('#auth-form'));
    assert.equal(doc.querySelector('#shell').hidden,true);
  } finally {w.close();}
});

function navigationFixture(){
  const root=path.join(__dirname,'..');
  const dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:3000/#products',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[],timers=new Map(),previewTimers=new Map();let timerId=0,previewTimerId=-1,pending=null;
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  w.setInterval=(callback,ms)=>{assert.equal(ms,5000);timers.set(++timerId,callback);return timerId};
  w.clearInterval=id=>timers.delete(id);
  const realTimeout=w.setTimeout.bind(w),realClearTimeout=w.clearTimeout.bind(w);
  w.setTimeout=(callback,ms,...args)=>{if(ms===2000){const id=previewTimerId--;previewTimers.set(id,callback);return id}return realTimeout(callback,ms,...args)};
  w.clearTimeout=id=>{if(!previewTimers.delete(id))realClearTimeout(id)};
  const store={id:'s1',name:'导航测试店',platform:'kuaishou',commerce_status:'valid',commerce_enabled:true,video_enabled:true,templates:{publish_defaults:{kuaishou_preview:{category_path:['箱包','女包','手提包'],brand:'无品牌',material:'PU',closure:'拉链',freight_template:'默认运费'}}}};
  const product={id:'p1',title:'待发布测试商品',version:1,image_ids:['image01'],skus:[{code:'A',name:'默认规格',price_cents:100,stock:5}]};
  const state={stores:[store],products:[product],listings:[],media:[],stats:{products:1,orders:0,pending:0,issues:1},platforms:{kuaishou:{name:'快手小店',video_name:'快手',commerce:['product_create'],color:'#f80'}},commerce_browser_ready:true};
  const problem='请配置电商网页工作流 commerce_browser.workflows.product_create；平台页面需要适配后才能执行';
  const jobs=[{id:'j1',store_id:'s1',store_name:store.name,platform:store.platform,kind:'product_create',status:'blocked',attempts:0,payload:{product},result:{message:problem}}];
  let preview={ready:true,active:true,status:'needs_input',job_status:'running',filled:['商品名称','价格'],needs_manual:['请核对规格图片'],message:'表单已准备，等待手工核对',submitted_by_app:false};
  w.fetch=async(url,options={})=>{
    requests.push({url,method:options.method||'GET',body:options.body?JSON.parse(options.body):null});
    if(pending&&pending.match(url)){const held=pending;pending=null;await held.gate.promise}
    let result;
    if(url==='/api/auth/status')result={authenticated:true,initialized:true,csrf:'test'};
    else if(url==='/api/state')result=state;
    else if(url==='/api/stores/s1/commerce/readiness')result={store_id:'s1',platform:'kuaishou',platform_name:'快手小店',login_status:'valid',engine_ready:true,session_ready:true,actions:[{action:'product_create',label:'创建商品',configured:false,ready:false,status:'adapter_missing',summary:'该平台的创建商品适配尚未完成',problem}]};
    else if(url==='/api/stores/s1/commerce/prepare-product'&&options.method==='POST')result={jobs:[{id:'preview1',status:'queued'}]};
    else if(url==='/api/jobs/preview1/preview')result=preview;
    else if(url==='/api/jobs/preview1/preview/close'&&options.method==='POST'){preview={...preview,active:false,status:'closed',message:'预览已结束'};result={ok:true,closing:true}}
    else if(url==='/api/jobs/preflight')result={ready:0,items:[{store_id:store.id,store_name:store.name,platform:store.platform,kind:'product_create',payload:{product},problem}]};
    else if(url==='/api/jobs'&&options.method==='POST')result={jobs:[{id:'j1',status:'blocked'}]};
    else if(url.startsWith('/api/jobs?'))result={rows:jobs,total:1};
    else if(url.startsWith('/api/orders?'))result={rows:[],total:0};
    else if(url==='/api/inventory')result={rows:[],alerts:[],ledger:[]};
    else if(url==='/api/audit')result=[];
    else throw Error('Unexpected API '+url);
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
  };
  const click=s=>{assert.ok(d.querySelector(s),s);d.querySelector(s).click()};
  const heading=()=>d.querySelector('#main h1')?.textContent;
  const tick=()=>Promise.all([...timers.values()].map(callback=>callback()));
  const tickPreview=()=>{const ready=[...previewTimers.entries()];previewTimers.clear();return Promise.all(ready.map(([,callback])=>callback()))};
  const hold=match=>{const gate=deferred();pending={match,gate};return gate};
  w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
  return {w,d,requests,timers,previewTimers,jobs,click,heading,tick,tickPreview,hold,problem,state,setPreview:report=>{preview={...preview,...report}},close:()=>w.close()};
}

test('saving a blocked publish creates one poller and leaving tasks cancels polling and stale results',async()=>{
  const f=navigationFixture();
  try{
    await until(()=>f.d.querySelector('[data-action="publish-one"]'));
    f.click('[data-action="publish-one"]');
    f.d.querySelector('#batch-form [name="store_ids"]').checked=true;
    f.d.querySelector('#batch-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.d.querySelector('[data-action="commit-batch"]'));
    f.click('[data-action="commit-batch"]');
    await until(()=>f.heading()==='每一步执行，都看得见。'&&f.timers.size>0);
    // The original hashchange + explicit render created two timers here.
    await delay();
    assert.equal(f.timers.size,1);
    const gate=f.hold(url=>url.startsWith('/api/jobs?'));
    const polling=f.tick();
    await until(()=>f.requests.filter(r=>r.url.startsWith('/api/jobs?')).length>=2);
    f.click('#nav [href="#stores"]');
    await until(()=>f.heading()==='让店铺，连在一起。');
    gate.resolve();await polling;await delay();
    assert.equal(f.heading(),'让店铺，连在一起。');
    assert.equal(f.d.querySelector('#breadcrumb').textContent,'店铺连接');
    assert.equal(f.timers.size,0);
    const jobReads=f.requests.filter(r=>r.url.startsWith('/api/jobs?')).length;
    for(let i=0;i<3;i++)await f.tick(); // Advance three 5-second polling periods.
    assert.equal(f.requests.filter(r=>r.url.startsWith('/api/jobs?')).length,jobReads);
    assert.equal(f.heading(),'让店铺，连在一起。');
    for(const [route,title] of [['products','一份商品资料，管理所有店铺。'],['orders','每笔订单，都有迹可循。'],['inventory','一份实物库存，分配给所有店。'],['videos','一条好内容，抵达更多渠道。'],['settings','让工作台，按你的方式运转。'],['overview','每个店铺，都井井有条。']]){
      f.click('#nav [href="#tasks"]');await until(()=>f.heading()==='每一步执行，都看得见。');
      f.click(`#nav [href="#${route}"]`);await until(()=>f.heading()===title);
      await f.tick();assert.equal(f.heading(),title);assert.equal(f.timers.size,0);
    }
  }finally{f.close()}
});

test('late page responses and errors cannot overwrite a newer route',async()=>{
  const f=navigationFixture();
  try{
    await until(()=>f.heading()==='一份商品资料，管理所有店铺。');
    for(const [route,match,reject] of [
      ['tasks',url=>url.startsWith('/api/jobs?'),false],
      ['inventory',url=>url==='/api/inventory',false],
      ['orders',url=>url.startsWith('/api/orders?'),true],
      ['settings',url=>url==='/api/audit',false],
    ]){
      const before=f.requests.filter(r=>match(r.url)).length,gate=f.hold(match);
      f.click(`#nav [href="#${route}"]`);
      await until(()=>f.requests.filter(r=>match(r.url)).length>before);
      f.click('#nav [href="#stores"]');await until(()=>f.w.location.hash==='#stores'&&f.heading()==='让店铺，连在一起。'&&f.d.querySelector('#breadcrumb').textContent==='店铺连接');
      if(reject)gate.reject(Error('Delayed old request failed'));else gate.resolve();
      await delay();
      assert.equal(f.heading(),'让店铺，连在一起。',route);
      assert.equal(f.d.querySelector('#main .error-state'),null,route);
      assert.equal(f.d.querySelector('#breadcrumb').textContent,'店铺连接');
      assert.equal(f.timers.size,0,route);
    }
  }finally{f.close()}
});

test('missing platform adapters show an honest diagnosis and blocked products reopen preflight instead of retrying',async()=>{
  const f=navigationFixture();
  try{
    await until(()=>f.d.querySelector('[data-action="publish-one"]'));
    f.click('[data-action="publish-one"]');
    f.d.querySelector('#batch-form [name="store_ids"]').checked=true;
    f.d.querySelector('#batch-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.d.querySelector('[data-action="commit-batch"]'));
    assert.match(f.d.querySelector('#modal .channel-row small').textContent,/创建商品适配尚未完成/);
    assert.doesNotMatch(f.d.querySelector('#modal .channel-row small').textContent,/commerce_browser/);
    assert.equal(f.d.querySelector('#modal details pre').textContent,f.problem);
    f.click('#modal [data-action="commerce-readiness"]');
    await until(()=>f.d.querySelector('#modal h2')?.textContent==='店铺接入检查');
    assert.match(f.d.querySelector('#modal tbody').textContent,/尚未适配/);
    assert.match(f.d.querySelector('#modal').textContent,/普通使用无需编写工作流/);
    f.click('#modal [data-action="close"]');
    f.click('#nav [href="#tasks"]');
    await until(()=>f.d.querySelector('[data-action="job-repreflight"]'));
    assert.equal(f.d.querySelector('[data-action="job-retry"]'),null);
    assert.match(f.d.querySelector('.status-message > div').textContent,/创建商品适配尚未完成/);
    f.click('[data-action="job-repreflight"]');
    assert.equal(f.d.querySelector('#batch-form [name="store_ids"]').checked,true);
    assert.equal(f.d.querySelector('#batch-form [name="kind"]').value,'product_create');
    assert.equal(f.requests.filter(r=>r.url.includes('/action')).length,0);
  }finally{f.close()}
});

for(const upload of [false,true])test(`Kuaishou form preview requires an explicit action and sends confirmed business fields (upload=${upload})`,async()=>{
  const f=navigationFixture();
  try{
    await until(()=>f.d.querySelector('[data-action="kuaishou-preview"]'));
    f.click('[data-action="kuaishou-preview"]');
    const form=f.d.querySelector('#commerce-preview-form');
    assert.ok(form);
    assert.equal(form.elements.preview_brand.value,'无品牌');
    assert.equal(form.elements.preview_category_path.value,'箱包\n女包\n手提包');
    assert.equal(form.elements.upload_images.checked,false);
    assert.equal(f.requests.filter(r=>r.url.includes('prepare-product')).length,0,'Opening the dialog must not upload');
    assert.match(form.textContent,/程序不点击保存草稿或提交审核/);
    assert.equal(form.querySelector('[name="templates"]'),null,'No workflow JSON is required');
    form.elements.preview_brand.value='本次测试品牌';
    form.elements.preview_freight_template.value='本次运费';
    if(upload){form.elements.upload_images.checked=true;form.elements.upload_images.dispatchEvent(new f.w.Event('change',{bubbles:true}));assert.equal(form.querySelector('[type="submit"]').textContent,'上传图片并准备表单')}
    form.dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.d.querySelector('#modal h2')?.textContent==='快手填表预览结果');
    const sent=f.requests.find(r=>r.url==='/api/stores/s1/commerce/prepare-product');
    assert.deepEqual(sent.body,{product_id:'p1',upload_images:upload,business:{category_path:['箱包','女包','手提包'],brand:'本次测试品牌',material:'PU',closure:'拉链',freight_template:'本次运费'}});
    assert.equal(f.state.stores[0].templates.publish_defaults.kuaishou_preview.brand,'无品牌','Preview edits must not modify store defaults');
    assert.match(f.d.querySelector('#modal').textContent,/未由程序提交审核/);
    assert.match(f.d.querySelector('#modal').textContent,/请核对规格图片/);
    assert.equal(f.requests.filter(r=>r.method==='POST').length,1,'Only the form preparation endpoint is called');
    f.click('#modal [data-action="commerce-preview-close"]');
    await until(()=>f.d.querySelector('#modal').textContent.includes('预览已结束'));
    assert.equal(f.requests.filter(r=>r.url==='/api/jobs/preview1/preview/close').length,1);
    assert.equal(f.requests.some(r=>r.url==='/api/jobs'),false,'Preview never queues product_create');
  }finally{f.close()}
});

test('preview progress polls queued jobs and stops on an escaped terminal failure',async()=>{
  const f=navigationFixture();
  try{
    f.setPreview({job_status:'queued',status:'waiting',active:false,ready:false,message:'正在准备',filled:[],needs_manual:[]});
    await until(()=>f.d.querySelector('[data-action="kuaishou-preview"]'));
    f.click('[data-action="kuaishou-preview"]');
    f.d.querySelector('#commerce-preview-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.d.querySelector('[data-preview-message]')?.textContent==='正在准备');
    assert.equal(f.previewTimers.size,1);
    assert.equal(f.d.querySelector('[data-action="commerce-preview-close"]').disabled,true);
    const message='平台第一步缺少主图 <script>alert(1)</script>';
    f.setPreview({job_status:'blocked',status:'blocked',active:false,ready:false,message});
    await f.tickPreview();
    assert.equal(f.d.querySelector('[data-preview-message]').textContent,message);
    assert.equal(f.d.querySelector('#modal script'),null);
    assert.match(f.d.querySelector('#modal').textContent,/需要处理/);
    assert.equal(f.previewTimers.size,0);
    const reads=f.requests.filter(r=>r.url==='/api/jobs/preview1/preview').length;
    await f.tickPreview();
    assert.equal(f.requests.filter(r=>r.url==='/api/jobs/preview1/preview').length,reads);
  }finally{f.close()}
});

test('late preview polls cannot reopen after navigation or replace another modal',async()=>{
  const f=navigationFixture();
  try{
    f.setPreview({job_status:'queued',status:'waiting',active:false,ready:false,message:'正在准备',filled:[],needs_manual:[]});
    await until(()=>f.d.querySelector('[data-action="kuaishou-preview"]'));
    f.click('[data-action="kuaishou-preview"]');
    f.d.querySelector('#commerce-preview-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.previewTimers.size===1);
    let gate=f.hold(url=>url==='/api/jobs/preview1/preview'),poll=f.tickPreview();
    f.click('#nav [href="#stores"]');await until(()=>f.heading()==='让店铺，连在一起。');
    gate.resolve();await poll;
    assert.equal(f.d.querySelector('#modal').open,false);
    assert.equal(f.previewTimers.size,0);
    assert.equal(f.heading(),'让店铺，连在一起。');
    f.click('#nav [href="#products"]');await until(()=>f.d.querySelector('[data-action="kuaishou-preview"]'));
    f.click('[data-action="kuaishou-preview"]');
    f.d.querySelector('#commerce-preview-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>f.previewTimers.size===1);
    gate=f.hold(url=>url==='/api/jobs/preview1/preview');poll=f.tickPreview();
    f.click('#modal [data-action="close"]');f.click('[data-action="edit-product"]');
    gate.resolve();await poll;
    assert.equal(f.d.querySelector('#modal h2').textContent,'编辑商品资料');
    assert.equal(f.previewTimers.size,0);
  }finally{f.close()}
});

test('commerce and video sessions use distinct login actions and stores refresh independently',async()=>{
  const root=path.join(__dirname,'..');
  const dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:3000/#stores',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  const store={id:'store01',name:'独立登录店',platform:'tmall',commerce_enabled:1,video_enabled:1,commerce_status:'unverified',video_status:'valid',templates:{commerce_browser:{logged_in:{css:'.merchant-ready'}}}};
  const state={stores:[store],products:[],listings:[],media:[],stats:{products:0,orders:0,pending:0,issues:0},platforms:{tmall:{name:'淘宝 / 天猫',video_name:'淘宝光合',color:'#f4832b',commerce:['product_create','product_update','product_list','product_delist','order_sync'],portal:'https://myseller.taobao.com/',video_portal:'https://creator.guanghe.taobao.com/'}},commerce_browser_ready:true,engine_ready:true,desktop_ready:true};
  w.fetch=async(url,options={})=>{const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method||'GET',body});let r;
    if(url==='/api/auth/status')r={authenticated:true,initialized:true,csrf:'csrf'};
    else if(url==='/api/state')r=state;
    else if(url.startsWith('/api/jobs?'))r={rows:[],total:0};
    else if(/^\/api\/stores\/store01\/(commerce|account)\/(login|check|logout)$/.test(url))r={jobs:[{id:'login01'}]};
    else if(url==='/api/stores/store01'&&options.method==='PUT')r={ok:true};
    else throw Error('Unexpected API '+url);
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(r))};
  };
  const click=s=>{assert.ok(d.querySelector(s),s);d.querySelector(s).click()};
  const back=async()=>{w.location.hash='stores';await until(()=>d.querySelector('[data-action="commerce-login"]'))};
  try{
    w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
    await until(()=>d.querySelector('[data-action="commerce-login"]'));
    assert.match(d.querySelector('[aria-label="电商后台会话"]').textContent,/尚未登录/);
    assert.match(d.querySelector('[aria-label="视频发布会话"]').textContent,/登录有效/);
    assert.equal(d.querySelector('[aria-label="电商后台会话"] a').href,'https://myseller.taobao.com/');
    assert.equal(d.querySelector('[aria-label="视频发布会话"] a').href,'https://creator.guanghe.taobao.com/');
    click('[data-action="commerce-login"]');
    await until(()=>d.querySelector('.desktop-frame'));
    assert.match(d.querySelector('#modal').textContent,/登录页会在下方桌面打开/);
    assert.equal(requests.filter(r=>r.url==='/api/stores/store01/commerce/login').length,1);
    assert.equal(requests.filter(r=>r.url.includes('/account/')).length,0);
    click('[data-action="close"]');
    store.commerce_status='valid';
    click('[data-action="refresh-stores"]');
    await until(()=>d.querySelector('[aria-label="电商后台会话"]').textContent.includes('登录有效'));
    click('[data-action="account-login"]');
    await until(()=>d.querySelector('.desktop-frame'));
    assert.equal(requests.filter(r=>r.url==='/api/stores/store01/account/login').length,1);
    click('[data-action="close"]');
    click('[data-action="commerce-check"]');
    await until(()=>requests.some(r=>r.url==='/api/stores/store01/commerce/check'));
    await until(()=>w.location.hash==='#tasks');
    await back();
    click('[data-action="commerce-logout"]');
    await until(()=>requests.some(r=>r.url==='/api/stores/store01/commerce/logout'));
    await until(()=>w.location.hash==='#tasks');
    await back();
    click('[data-action="edit-store"]');
    assert.equal(d.querySelector('[name="app_key"]'),null);
    assert.equal(d.querySelector('[name="access_token"]'),null);
    assert.equal(d.querySelector('[name="api_enabled"]'),null);
    assert.ok(d.querySelector('[name="commerce_enabled"]'));
    d.querySelector('#store-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>requests.some(r=>r.url==='/api/stores/store01'&&r.method==='PUT'));
    const saved=requests.find(r=>r.url==='/api/stores/store01'&&r.method==='PUT').body;
    assert.equal(saved.commerce_enabled,true);
    assert.deepEqual(saved.templates.commerce_browser,store.templates.commerce_browser);
    assert.deepEqual(Object.keys(saved.credentials),['video_app_id','video_access_token']);
  }finally{w.close()}
});

test('one local product queues all six platforms once and inventory form sends bounded allocations',async()=>{
  const root=path.join(__dirname,'..'),dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:3000/#products',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  const stores=['douyin','kuaishou','wechat','tmall','jd','pdd'].map((p,i)=>({id:'s'+i,name:'店铺'+i,platform:p,video_status:'unverified',commerce_status:'valid'}));
  const product={id:'p1',version:1,title:'共享商品',category:'箱包',skus:[{code:'BOX',name:'银色',price_cents:100,stock:10}],updated:1};
  const state={stores,products:[product],listings:[],media:[],stats:{products:1,orders:0,pending:0,issues:0},platforms:Object.fromEntries(stores.map(s=>[s.platform,{name:s.platform,color:'#333',commerce:['product_create','order_sync'],link:true}])),version:'0.2.0'};
  let jobs=[];
  w.fetch=async(url,options={})=>{const body=options.body?JSON.parse(options.body):null;requests.push({url,body});let r;
    if(url==='/api/auth/status')r={authenticated:true,initialized:true,csrf:'csrf'};
    else if(url==='/api/state')r=state;
    else if(url==='/api/jobs/preflight')r={ready:6,items:stores.map(s=>({store_name:s.name,platform:s.platform,kind:body.kind,payload:{request:{title:'共享商品'}},problem:''}))};
    else if(url==='/api/jobs'){jobs=stores.map((s,i)=>({id:'j'+i,store_name:s.name,platform:s.platform,kind:body.kind,status:'queued',payload:{},result:{},created:1}));r={jobs};}
    else if(url.startsWith('/api/jobs?'))r={rows:jobs,total:6};
    else if(url==='/api/inventory')r={rows:[{...product.skus[0],product_id:'p1',version:1,title:product.title,held:0,unallocated:10,allocations:[]}],alerts:[],ledger:[]};
    else if(url==='/api/products/p1/allocations')r={ok:true};
    else throw Error('Unexpected API '+url);
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(r))};
  };
  const click=s=>{assert.ok(d.querySelector(s),s);d.querySelector(s).click()},submit=s=>d.querySelector(s).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  try{
    w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
    await until(()=>d.querySelector('[data-action="publish-one"]'));
    click('[data-action="publish-one"]');
    assert.equal(d.querySelectorAll('#batch-form .store-check').length,6);
    assert.match(d.querySelector('#batch-form').textContent,/电商后台 · 登录有效/);
    for(const el of d.querySelectorAll('#batch-form [name="store_ids"]'))el.checked=true;
    submit('#batch-form');await until(()=>d.querySelector('[data-action="commit-batch"]'));
    const plan=requests.find(r=>r.url==='/api/jobs/preflight').body;
    assert.deepEqual(plan.store_ids,['s0','s1','s2','s3','s4','s5']);assert.deepEqual(plan.product_ids,['p1']);
    assert.equal(requests.filter(r=>r.url==='/api/jobs').length,0);
    click('[data-action="commit-batch"]');await until(()=>jobs.length===6&&!d.querySelector('#modal').open);
    assert.equal(requests.filter(r=>r.url==='/api/jobs').length,1);
    w.location.hash='inventory';await until(()=>d.querySelector('[data-action="allocate"]'));
    click('[data-action="allocate"]');
    for(const [i,value] of [4,3,3].entries())d.querySelector(`[name="0_s${i}"]`).value=value;
    submit('#allocation-form');await until(()=>requests.some(r=>r.url==='/api/products/p1/allocations'));
    assert.deepEqual(requests.find(r=>r.url==='/api/products/p1/allocations').body,{version:1,quantities:{BOX:{s0:4,s1:3,s2:3,s3:0,s4:0,s5:0}}});
  }finally{w.close()}
});

test('workbench jobs name the batch and link back only when a batch identity exists',async()=>{
  const f=navigationFixture();
  try{
    await until(()=>f.heading()==='一份商品资料，管理所有店铺。');
    f.jobs.push({id:'w1',store_id:'s1',store_name:'导航测试店',platform:'kuaishou',kind:'workbench_submit',status:'uncertain',attempts:1,payload:{group_id:'group-abc',product:{title:'批次商品'}},result:{message:'平台提交结果无法确认'}},
                {id:'w2',store_id:'s1',store_name:'导航测试店',platform:'kuaishou',kind:'workbench_collect',status:'blocked',attempts:1,payload:{product:{title:'旧任务商品'}},result:{message:'请重新读取平台要求'}});
    f.w.location.hash='tasks';
    await until(()=>f.heading()==='每一步执行，都看得见。');
    const links=f.d.querySelectorAll('a[href="#publishing/group-abc"]');
    assert.equal(links.length,1);
    assert.equal(links[0].textContent,'打开发布批次');
    assert.match(f.d.querySelector('#main').textContent,/统一提交审核/);
    assert.match(f.d.querySelector('#main').textContent,/读取平台要求/);
    assert.match(f.d.querySelector('#main').textContent,/批次商品/);
    assert.equal(f.d.querySelectorAll('a[href^="#publishing/"]').length,1,'a task without a batch identity gets no link');
  }finally{f.close()}
});

test('a completed commerce login closes the desktop and refreshes the store card',async()=>{
  const root=path.join(__dirname,'..');
  const dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:3000/#stores',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  const store={id:'store01',name:'抖音登录店',platform:'douyin',commerce_enabled:1,video_enabled:1,commerce_status:'unverified',video_status:'unverified',templates:{}};
  const state={stores:[store],products:[],listings:[],media:[],stats:{products:0,orders:0,pending:0,issues:0},platforms:{douyin:{name:'抖音小店',video_name:'抖音',color:'#303644',commerce:['product_create'],portal:'https://fxg.jinritemai.com/',login_portal:'https://fxg.jinritemai.com/login/common'}},commerce_browser_ready:true,engine_ready:true,desktop_ready:true};
  let loginJob=null;
  w.fetch=async(url,options={})=>{const method=options.method||'GET';requests.push({url,method});let r;
    if(url==='/api/auth/status')r={authenticated:true,initialized:true,csrf:'csrf'};
    else if(url==='/api/state')r=JSON.parse(JSON.stringify(state));
    else if(url==='/api/stores/store01/commerce/login'&&method==='POST'){loginJob={id:'login01',store_id:'store01',kind:'commerce_login',status:'queued',result:{message:'等待执行'}};r={jobs:[{id:'login01',status:'queued'}]}}
    else if(url.startsWith('/api/jobs?'))r={rows:loginJob?[loginJob]:[],total:loginJob?1:0};
    else throw Error('Unexpected API '+url);
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(r))};
  };
  const realTimeout=w.setTimeout.bind(w),realClear=w.clearTimeout.bind(w),loginTimers=new Map();let timerId=0;
  w.setTimeout=(callback,ms,...args)=>{if(ms===1200||ms===3000){loginTimers.set(++timerId,callback);return timerId}return realTimeout(callback,ms,...args)};
  w.clearTimeout=id=>{if(!loginTimers.delete(id))realClear(id)};
  const tickLogin=async()=>{const pending=[...loginTimers.values()];loginTimers.clear();await Promise.all(pending.map(callback=>callback()))};
  try{
    w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
    await until(()=>d.querySelector('[data-action="commerce-login"]'));
    assert.equal(d.querySelector('[aria-label="电商后台会话"] a').href,'https://fxg.jinritemai.com/login/common');
    d.querySelector('[data-action="commerce-login"]').click();
    await until(()=>d.querySelector('#modal[open]')&&loginJob);
    assert.equal(requests.filter(r=>r.url==='/api/stores/store01/commerce/login').length,1);
    loginJob={...loginJob,status:'running'};
    await tickLogin();
    await until(()=>/浏览器已打开/.test(d.querySelector('#desktop-state').textContent));
    loginJob={...loginJob,status:'succeeded',result:{message:'登录成功，浏览器窗口已自动关闭'}};
    await tickLogin();
    await until(()=>!d.querySelector('#modal').open);
    assert.match(d.querySelector('#toast').textContent,/登录成功/);
    assert.equal(loginTimers.size,0);
  }finally{w.close()}
});
