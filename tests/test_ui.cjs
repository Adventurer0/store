// DOM interaction tests with an in-memory API double. This is not browser / live-platform QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const delay = () => new Promise(resolve => setTimeout(resolve, 10));
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
    assert.match(d.querySelector('#modal').textContent,/电商与视频分别登录/);
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
