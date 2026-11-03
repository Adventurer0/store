// Business-form DOM tests. Platform observations are API doubles, never live-platform evidence.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');

const source=fs.readFileSync(path.join(__dirname,'../app/static/publishing.js'),'utf8');
const clone=value=>JSON.parse(JSON.stringify(value));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function until(condition){for(let n=0;n<100;n++){if(condition())return;await flush();}throw Error('DOM condition did not become true');}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject};}
const observedField=(key,label,type='text',extra={})=>({key,semantic:key,label,type,required:true,scope:'common',observed:true,...extra});
function fixture({count=1,scores=[94,96],status='prepared',ready=true}={}){
  const drafts=Array.from({length:count},(_,index)=>{
    const id='draft'+(index+1),skus=[{code:'SKU-'+index,name:'银色',price_cents:1299,stock:0}];
    const common_values={title:'商品'+(index+1),description:'真实商品资料',image_ids:['image01'],skus};
    const stores=scores.map((score,i)=>({store_id:'store'+(i+1),store_name:'店铺'+(i+1),platform:['douyin','pdd'][i%2],platform_name:['抖音小店','拼多多'][i%2],status,stage:'product_details',complete:true,
      fields:[observedField('category'+i,'平台类目','select',{semantic:'category',scope:'store',options:[{value:'cat-'+i,label:'类目'+i},{value:'other-'+i,label:'另一类目'+i}]})],
      values:{['category'+i]:'cat-'+i},missing:[],quality:{score,observed_at:1800000000},quality_passed:ready&&score>90,
      local_quality:{score:98,threshold:95,comparison:'gte',passed:ready,blockers:ready?[]:['本地预检未通过']},local_quality_passed:ready,
      platform_quality_required:true,platform_quality_ok:ready&&score>90,problems:[],ready:ready&&score>90,receipt:{}}));
    return {id,product_id:'product'+(index+1),product_title:common_values.title,revision:1,common_values,store_values:{},common_fields:[observedField('title','商品标题'),observedField('description','商品描述','textarea'),observedField('image_ids','主图','images'),observedField('skus','规格','skus')],stores,ready:stores.every(s=>s.ready),minimum_quality:90,quality_comparison:'gt'};
  });
  return {id:'group01',drafts,ready:drafts.every(d=>d.ready),status:'editing'};
}
function mount({group=fixture(),api,remember=true,hash='#publishing',stored}={}){
  const dom=new JSDOM('<main id="main"></main><dialog id="modal"></dialog>',{url:'http://localhost/'+hash,runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[],toasts=[],timers=new Map();let timerId=0;
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  w.setTimeout=callback=>{const id=++timerId;timers.set(id,callback);return id};
  w.clearTimeout=id=>timers.delete(id);
  const state={products:[1,2].map(i=>({id:'product'+i,title:'商品'+i,cover_id:'image01'})),stores:[1,2].map(i=>({id:'store'+i,name:'店铺'+i,platform:i===1?'douyin':'pdd',commerce_status:'valid'})),media:[{id:'image01',kind:'image',name:'主图 <原始>.png'}]};
  const context={getState:()=>state,toast:(message,error)=>toasts.push({message,error}),modal:(title,body)=>{d.querySelector('#modal').innerHTML='<h2>'+title+'</h2>'+body;d.querySelector('#modal').showModal()},closeModal:()=>d.querySelector('#modal').close(),api:async(url,options={})=>{
    const request={url,method:options.method||'GET',body:options.body===undefined?undefined:clone(options.body)};requests.push(request);
    const result=api?await api(request):group;
    return clone(result);
  }};
  if(remember)w.localStorage.setItem('shopdock-publish-group',(stored||group).id);
  w.eval(source);
  return {dom,w,d,context,requests,toasts,pw:w.PublishWorkbench,
    async open(){await w.PublishWorkbench.page(context)},
    click(selector){const element=d.querySelector(selector);assert.ok(element,selector);element.click();return element},
    edit(selector,value){const element=d.querySelector(selector);assert.ok(element,selector);element.value=value;element.dispatchEvent(new w.Event('input',{bubbles:true}));return element},
    async tick(){const callbacks=Array.from(timers.values());timers.clear();for(const callback of callbacks)await callback();await flush()},
    async leave(){w.location.hash='products';w.PublishWorkbench.leave();d.querySelector('#main').innerHTML='<p id="other-page">其他页面</p>';await flush()},
    close(){w.PublishWorkbench.leave();w.close()}
  };
}

for(const operation of ['audit','retry-failed'])test('late '+operation+' completion cannot overwrite navigation or a different batch',async()=>{
  const group=fixture({status:operation==='audit'?'submitted':'needs_input',ready:false});
  if(operation==='audit')group.drafts[0].stores[0].receipt={external_id:'123'};
  const waiting=deferred();
  const other=fixture();other.id='group02';other.drafts[0].product_title='另一个批次';
  const ui=mount({group,api:request=>{
    if(request.method==='POST'&&request.url.endsWith('/'+operation))return waiting.promise;
    return request.url.endsWith('group02')?other:group;
  }});
  try{
    await ui.open();
    ui.click('[data-pw-action="'+operation+'"]');
    await until(()=>ui.requests.some(r=>r.method==='POST'));
    ui.w.location.hash='publishing/group02';
    await ui.pw.page(ui.context);
    const before=ui.requests.length;
    waiting.resolve({});await flush();await flush();
    assert.match(ui.d.querySelector('#main').textContent,/另一个批次/);
    assert.equal(ui.requests.length,before,'no completion read against the new batch');
    assert.equal(ui.toasts.length,0,'no stale completion message');
    await ui.leave();
    assert.ok(ui.d.querySelector('#other-page'));
  }finally{ui.close()}
});

test('actual common controls render once, category stays per store, values and schema text are escaped',async()=>{
  const group=fixture();
  const dangerous='商品 "测试" </textarea><img data-injected src=x onerror="alert(1)">';
  group.drafts[0].common_values.title=dangerous;
  group.drafts[0].product_title=dangerous;
  const special='custom" autofocus onfocus="alert(1)';
  group.drafts[0].stores[0].fields.push(observedField(special,'属性 <script>text</script>','text',{scope:'store'}));
  group.drafts[0].stores[0].values[special]='{"note":"<b>原样</b>"}';
  let saved;
  const ui=mount({group,api:request=>{
    if(request.method==='PUT'){saved=request.body;group.drafts[0].revision++;return group.drafts[0]}
    return group;
  }});
  try{
    await ui.open();
    assert.equal(ui.d.querySelectorAll('[data-pw-key="title"]').length,1);
    assert.equal(ui.d.querySelectorAll('select[data-pw-scope="store"]').length,2);
    assert.equal(ui.d.querySelector('[data-pw-key="category0"]').value,'cat-0');
    assert.equal(ui.d.querySelector('[data-pw-key="category1"]').value,'cat-1');
    assert.equal(ui.d.querySelector('[data-pw-key="title"]').value,dangerous);
    assert.equal(ui.d.querySelector('[data-injected]'),null);
    assert.equal(ui.d.querySelector('[autofocus],[onfocus],script'),null);
    const custom=Array.from(ui.d.querySelectorAll('[data-pw-key]')).find(el=>el.dataset.pwKey===special);
    assert.equal(custom.value,'{"note":"<b>原样</b>"}');
    assert.equal(ui.d.querySelectorAll('[data-pw-key*="score"],input[name*="score"],textarea[name*="workflow"]').length,0);
    assert.equal(ui.d.querySelector('img.sd-zoom-source').dataset.zoomSrc,'/api/media/image01');
    const imageCheckbox=ui.d.querySelector('.pw-image-options input');
    imageCheckbox.click();assert.equal(imageCheckbox.checked,false,'image selection remains a checkbox');
    ui.edit('[data-pw-key="title"]','修改后的真实商品');
    ui.edit('[data-pw-key="category1"]','other-1');
    ui.click('[data-pw-action="save"]');
    await until(()=>saved&&ui.d.querySelector('[data-pw-action="save"]').disabled===false);
    assert.equal(saved.revision,1);
    assert.equal(saved.common_values.title,'修改后的真实商品');
    assert.equal(saved.store_values.store2.category1,'other-1');
    assert.equal(saved.store_values.store1[special],'{"note":"<b>原样</b>"}');
    assert.equal(saved.common_values.skus[0].price_cents,1299);
    assert.equal(saved.common_values.skus[0].stock,0);
    assert.deepEqual(Object.keys(saved).sort(),['common_values','revision','store_values']);
    assert.equal('workflow' in saved,false);
  }finally{ui.close()}
});

test('group operations collect, prepare and finally confirmed submit cover multiple products and stores once',async()=>{
  const group=fixture({count:2,ready:false,status:'collected'});
  let collectQueued=false,prepareQueued=false;
  const ui=mount({group,remember:false,api:request=>{
    if(request.url==='/publish-groups'&&request.method==='POST')return group;
    if(request.method==='PUT'){
      const draft=group.drafts.find(d=>request.url.endsWith(d.id));
      assert.equal(request.body.revision,draft.revision);
      draft.common_values=request.body.common_values;draft.store_values=request.body.store_values;draft.revision++;
      return draft;
    }
    if(request.url.endsWith('/collect')){collectQueued=true;for(const d of group.drafts)for(const s of d.stores)s.status='queued_collect';return {jobs:[{id:'c1'},{id:'c2'},{id:'c3'},{id:'c4'}]}}
    if(request.url.endsWith('/prepare')){prepareQueued=true;for(const d of group.drafts)for(const s of d.stores)s.status='queued_prepare';return {jobs:[{id:'p1'},{id:'p2'},{id:'p3'},{id:'p4'}]}}
    if(request.url.endsWith('/submit')){for(const d of group.drafts)for(const s of d.stores)s.status='queued_submit';return {jobs:[{id:'s1'},{id:'s2'},{id:'s3'},{id:'s4'}]}}
    return group;
  }});
  try{
    await ui.open();ui.pw.launch(ui.context,['product1','product2']);
    for(const input of ui.d.querySelectorAll('[name="store_ids"]'))input.checked=true;
    const form=ui.d.querySelector('[data-pw-form="new"]');form.dispatchEvent(new ui.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>ui.d.querySelectorAll('.pw-draft').length===2);
    const creation=ui.requests.find(r=>r.url==='/publish-groups'&&r.method==='POST');
    assert.deepEqual(creation.body,{product_ids:['product1','product2'],store_ids:['store1','store2']});
    ui.click('[data-pw-action="collect"]');
    await until(()=>collectQueued&&ui.d.querySelector('[data-pw-action="collect"]').disabled);
    assert.equal(ui.requests.filter(r=>r.url.endsWith('/collect')).length,1);
    for(const d of group.drafts)for(const s of d.stores)s.status='collected';
    await ui.tick();
    await until(()=>!ui.d.querySelector('[data-pw-action="prepare"]').disabled);
    ui.edit('[data-pw-draft="draft1"] [data-pw-key="title"]','一次填写多平台');
    ui.d.querySelector('#pw-upload').checked=true;
    ui.click('[data-pw-action="prepare"]');
    await until(()=>prepareQueued);
    const preparation=ui.requests.find(r=>r.url.endsWith('/prepare'));
    assert.deepEqual(preparation.body,{revisions:{draft1:2,draft2:2},upload_images:true},'consent survives save and re-render');
    assert.equal(ui.requests.filter(r=>r.method==='PUT').length,2);
    assert.equal(ui.requests.filter(r=>r.url.endsWith('/prepare')).length,1);
    for(const draft of group.drafts){draft.ready=true;for(const store of draft.stores){store.status='prepared';store.ready=true;store.quality_passed=true;store.quality.score=95;store.platform_quality_ok=true;store.local_quality_passed=true;store.local_quality={...store.local_quality,passed:true,score:98}}}
    group.ready=true;await ui.tick();
    await until(()=>!ui.d.querySelector('[data-pw-action="submit"]').disabled);
    ui.click('[data-pw-action="submit"]');
    await until(()=>ui.d.querySelector('[data-pw-action="confirm-submit"]'));
    assert.equal(ui.requests.filter(r=>r.url.endsWith('/submit')).length,0,'opening confirmation does not submit');
    ui.click('[data-pw-action="confirm-submit"]');
    await until(()=>ui.requests.some(r=>r.url.endsWith('/submit'))&&!ui.d.querySelector('#modal').open);
    assert.equal(ui.requests.filter(r=>r.url.endsWith('/submit')).length,1);
    assert.deepEqual(ui.requests.find(r=>r.url.endsWith('/submit')).body.revisions,{draft1:2,draft2:2});
    assert.equal(ui.requests.some(r=>r.url.includes('/publish-drafts/')&&r.method==='POST'),false,'actions use the group endpoint');
  }finally{ui.close()}
});

test('strict score gate has no score input, rejects 90, and respects expired backend evidence at 91',async()=>{
  for(const setting of [{score:90,ready:false,passed:false},{score:89,ready:false,passed:false},{score:91,ready:false,passed:false},{score:91,ready:true,passed:true}]){
    const group=fixture({scores:[setting.score],ready:setting.ready});
    group.drafts[0].stores[0].quality_passed=setting.passed;
    const ui=mount({group});
    try{
      await ui.open();
      assert.equal(ui.d.querySelector('[data-pw-action="submit"]').disabled,!setting.ready);
      assert.equal(ui.d.querySelector('[data-pw-key="score"],input[name="score"]'),null);
      assert.match(ui.d.querySelector('.pw-score').textContent,/95/);
      if(!setting.ready){ui.click('[data-pw-action="submit"]');assert.equal(ui.d.querySelector('[data-pw-action="confirm-submit"]'),null)}
      assert.equal(ui.requests.some(r=>r.method==='POST'),false);
    }finally{ui.close()}
  }
});

test('polling preserves dirty controls and their displayed revision until an explicit save',async()=>{
  const group=fixture({status:'queued_collect',ready:false});let saved;
  const ui=mount({group,api:request=>{
    if(request.method==='PUT'){saved=request.body;group.drafts[0].revision=2;return group.drafts[0]}
    return group;
  }});
  try{
    await ui.open();
    const input=ui.edit('[data-pw-key="title"]','正在编辑，不可覆盖');
    group.drafts[0].common_values.title='后台返回的旧标题';
    group.drafts[0].revision=7;
    for(const store of group.drafts[0].stores)store.status='collected';
    await ui.tick();
    assert.equal(ui.d.querySelector('[data-pw-key="title"]'),input);
    assert.equal(input.value,'正在编辑，不可覆盖');
    assert.match(ui.d.querySelector('.pw-live-status').textContent,/内容已保留/);
    assert.equal(ui.d.querySelector('[data-pw-action="save"]').disabled,false,'completed jobs allow saving without discarding edits');
    assert.equal(ui.d.querySelector('[data-pw-action="submit"]').disabled,true);
    ui.click('[data-pw-action="save"]');
    await until(()=>saved&&!ui.d.querySelector('[data-pw-action="save"]').disabled);
    assert.equal(saved.revision,1,'background responses cannot silently replace the revision associated with displayed values');
    assert.equal(saved.common_values.title,'正在编辑，不可覆盖');
  }finally{ui.close()}
});

test('late group reads, queue responses and save responses cannot render over another page',async()=>{
  for(const kind of ['read','collect','save']){
    const group=fixture(),pending=deferred();let held=false;
    const ui=mount({group,api:async request=>{
      if((kind==='read'&&request.method==='GET')||(kind==='collect'&&request.url.endsWith('/collect'))||(kind==='save'&&request.method==='PUT')){held=true;await pending.promise}
      return kind==='save'&&request.method==='PUT'?{revision:2}:group;
    }});
    try{
      let initial;
      if(kind==='read')initial=ui.open();
      else{await ui.open();if(kind==='save')ui.edit('[data-pw-key="title"]','迟响应修改');ui.click('[data-pw-action="'+(kind==='save'?'save':'collect')+'"]')}
      await until(()=>held);
      await ui.leave();
      const count=ui.requests.length;
      pending.resolve();if(initial)await initial;await flush();await flush();
      assert.equal(ui.d.querySelector('#main').textContent,'其他页面',kind+' must not render after leaving');
      assert.equal(ui.requests.length,count,kind+' must not start a follow-up reload after leaving');
    }finally{pending.resolve();ui.close()}
  }
});

test('edits during a slow save are preserved and must be saved again before preparing',async()=>{
  const group=fixture(),pending=deferred();let put=false;
  const ui=mount({group,api:async request=>{
    if(request.method==='PUT'){put=true;await pending.promise;group.drafts[0].revision=2;return group.drafts[0]}
    return group;
  }});
  try{
    await ui.open();
    ui.edit('[data-pw-key="title"]','第一次修改');ui.d.querySelector('#pw-upload').checked=true;
    ui.click('[data-pw-action="prepare"]');await until(()=>put);
    const latest=ui.edit('[data-pw-key="title"]','保存期间的新修改');
    pending.resolve();await until(()=>ui.toasts.some(t=>t.message.includes('再次保存'))&&!ui.d.querySelector('[data-pw-action="save"]').disabled);
    assert.equal(ui.d.querySelector('[data-pw-key="title"]'),latest);
    assert.equal(latest.value,'保存期间的新修改');
    assert.equal(ui.requests.some(r=>r.url.endsWith('/prepare')),false);
    assert.equal(ui.d.querySelector('[data-pw-action="submit"]').disabled,true);
  }finally{pending.resolve();ui.close()}
});

const batchSummary=(id,extra={})=>({id,created:1800000000,updated:1800000000,product_count:1,product_titles:['旧批次商品'],store_count:2,target_count:2,submitted_count:0,uncertain_count:0,status:'ready',drafts:[],...extra});

test('a deep link wins over stale local storage and a missing batch returns to the record list',async()=>{
  const group=fixture();group.id='group02';group.drafts[0].product_title='深链批次';
  let staleRequested=false;
  const deep=mount({group,hash:'#publishing/group02',stored:{id:'stale-batch'},api:request=>{
    if(request.url==='/publish-groups/stale-batch'){staleRequested=true;throw Error('local storage must not win over the URL')}
    if(request.url==='/publish-groups/group02')return group;
    if(request.url==='/publish-groups')return {groups:[batchSummary('group02')],total:1};
    return group;
  }});
  try{
    await deep.open();
    assert.equal(staleRequested,false,'only the batch named in the URL may be read');
    assert.equal(deep.w.location.hash,'#publishing/group02');
    assert.equal(deep.w.localStorage.getItem('shopdock-publish-group'),'group02');
    assert.match(deep.d.querySelector('#main').textContent,/深链批次/);
  }finally{deep.close()}

  const gone=mount({group:fixture(),hash:'#publishing/gone',stored:{id:'gone'},api:request=>{
    if(request.url==='/publish-groups/gone'){const error=Error('发布批次不存在');error.status=404;throw error}
    if(request.url==='/publish-groups')return {groups:[batchSummary('keep-batch',{product_titles:['保留的批次']})],total:1};
    return fixture();
  }});
  try{
    await gone.open();
    assert.equal(gone.w.localStorage.getItem('shopdock-publish-group'),null,'a deleted batch must not stay remembered');
    assert.equal(gone.w.location.hash,'#publishing');
    assert.match(gone.d.querySelector('#main').textContent,/此发布批次不存在/);
    assert.match(gone.d.querySelector('#main').textContent,/保留的批次/);
  }finally{gone.close()}
});

test('history lists saved batches and deletion requires an explicit confirmation',async()=>{
  let groups=[batchSummary('g-old')];
  const ui=mount({group:fixture(),hash:'#publishing',remember:false,api:async request=>{
    if(request.url==='/publish-groups'&&request.method==='GET')return {groups,total:groups.length};
    if(request.url==='/publish-groups/g-old'&&request.method==='DELETE'){groups=[];return {ok:true,drafts:1}}
    return fixture();
  }});
  try{
    await ui.open();
    assert.match(ui.d.querySelector('#main').textContent,/旧批次商品/);
    assert.match(ui.d.querySelector('#main').textContent,/可提交/);
    ui.click('#main [data-pw-delete="g-old"]');
    await until(()=>ui.d.querySelector('[data-pw-action="confirm-delete"]'));
    assert.equal(ui.requests.some(r=>r.method==='DELETE'),false,'opening the confirmation must not delete anything');
    ui.click('[data-pw-action="confirm-delete"]');
    await until(()=>ui.requests.some(r=>r.method==='DELETE')&&!ui.d.querySelector('#modal').open);
    assert.equal(ui.requests.find(r=>r.method==='DELETE').url,'/publish-groups/g-old');
    await until(()=>/还没有发布批次/.test(ui.d.querySelector('#main').textContent));
  }finally{ui.close()}

  const editor=mount({group:fixture(),api:request=>request.url==='/publish-groups'?{groups:[batchSummary('g-old')],total:1}:fixture()});
  try{
    await editor.open();
    editor.click('[data-pw-action="history"]');
    await until(()=>editor.d.querySelector('#modal').open&&/旧批次商品/.test(editor.d.querySelector('#modal').textContent));
    assert.match(editor.d.querySelector('#modal').textContent,/可提交/);
    editor.click('#modal [data-pw-action="close-history"]');
    await until(()=>!editor.d.querySelector('#modal').open);
  }finally{editor.close()}
});

test('update mode is sent explicitly and is not the default create request',async()=>{
  const group=fixture();group.id='g-update';
  const ui=mount({group,remember:false,api:request=>{
    if(request.url==='/publish-groups'&&request.method==='POST')return group;
    if(request.url==='/publish-groups/g-update')return group;
    if(request.url==='/publish-groups')return {groups:[]};
    return group;
  }});
  try{
    await ui.open();
    ui.pw.launch(ui.context,['product1']);
    const mode=ui.d.querySelector('#pw-mode');assert.ok(mode,'mode selector');
    mode.value='update';
    ui.d.querySelectorAll('[name="product_ids"]')[0].checked=true;
    ui.d.querySelectorAll('[name="store_ids"]')[0].checked=true;
    const form=ui.d.querySelector('[data-pw-form="new"]');form.dispatchEvent(new ui.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>ui.requests.some(r=>r.url==='/publish-groups'&&r.method==='POST'));
    const creation=ui.requests.find(r=>r.url==='/publish-groups'&&r.method==='POST');
    assert.deepEqual(creation.body,{product_ids:['product1'],store_ids:['store1'],mode:'update'});
  }finally{ui.close()}
});
