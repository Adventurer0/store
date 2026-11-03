const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.join(__dirname,'..');
const wait=()=>new Promise(resolve=>setTimeout(resolve,10));
async function until(predicate){for(let i=0;i<150;i++){if(predicate())return;await wait()}throw Error('UI condition did not become true')}

function fixture(hash='products',publishing=null){
  const dom=new JSDOM(fs.readFileSync(path.join(root,'app/static/index.html'),'utf8'),{url:'http://localhost:8080/#'+hash,runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,requests=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
  let media=[{id:'img1',name:'箱包正面.jpg',kind:'image',mime:'image/jpeg',category:'箱包',tags:['主图'],size:100,created:1},{id:'img2',name:'衣服细节.png',kind:'image',mime:'image/png',category:'服装',tags:['详情'],size:200,created:1},{id:'vid1',name:'箱包视频.mp4',kind:'video',mime:'video/mp4',category:'箱包',tags:['视频'],size:1024,created:1},{id:'img3',name:'未分类图片.png',kind:'image',mime:'image/png',category:'',tags:[],size:100,created:1}];
  const products=[{id:'p1',title:'双肩包',category:'箱包',version:1,image_ids:['img1'],detail_image_ids:[],cover_id:'',description:'',images:[],extra:{},skus:[{code:'BAG',name:'默认',price_cents:9900,stock:3}]},{id:'p2',title:'衬衫',category:'服装',version:1,image_ids:['img2'],skus:[{code:'SHIRT',name:'M',price_cents:2900,stock:8}]}];
  const state={stores:[],products,listings:[],media,stats:{products:2,orders:0,pending:0,issues:0},platforms:{},max_upload_mb:10,version:'test'};
  let heldMedia=null;
  w.fetch=async(url,options={})=>{
    const body=typeof options.body==='string'?JSON.parse(options.body):options.body,record={url,method:options.method||'GET',body};requests.push(record);
    let result,status=200;
    if(url==='/api/auth/status')result={authenticated:true,initialized:true,csrf:'test'};
    else if(url==='/api/state')result={...state,media};
    else if(url.startsWith('/api/media?')){
      if(heldMedia){const gate=heldMedia;heldMedia=null;await gate}
      const query=new URL(url,'http://localhost').searchParams;
      const rows=media.filter(item=>(!query.get('kind')||item.kind===query.get('kind'))&&(!query.has('category')||item.category===query.get('category'))&&(!query.get('tag')||item.tags.includes(query.get('tag')))&&(!query.get('q')||(item.name+' '+item.tags.join(' ')).includes(query.get('q'))));
      result={rows,total:rows.length,categories:[{name:'箱包',count:2},{name:'服装',count:1},{name:'',count:1}],tags:[{name:'主图',count:1},{name:'详情',count:1}]};
    }else if(url==='/api/media/import'){
      record.files=body.getAll('files');record.relativePaths=JSON.parse(body.get('relative_paths'));record.category=body.get('category');record.tags=JSON.parse(body.get('tags'));
      const rows=record.files.map((file,index)=>({id:'new'+index,name:file.name,kind:'image',mime:file.type,category:record.category,tags:record.tags,size:file.size,source_path:record.relativePaths[index],created:1}));media.push(...rows);result={rows,imported:rows.length,duplicates:0,errors:[]};
    }else if(url==='/api/media/batch-update'){
      for(const item of media.filter(item=>body.ids.includes(item.id))){if('category' in body)item.category=body.category;if('tags' in body)item.tags=body.tags}
      result={updated:body.ids};
    }else if(url==='/api/media/batch-delete'){
      if(body.ids.includes('img1')){status=409;result={detail:{message:'素材仍被商品引用',references:[{type:'product',id:'p1',title:'双肩包'}]}}}
      else{media=media.filter(item=>!body.ids.includes(item.id));result={deleted:body.ids}}
    }else if(url.startsWith('/api/orders?'))result={rows:[],total:0};
    else if(url==='/api/inventory')result={rows:[],alerts:[],ledger:[]};
    else if(url==='/api/audit')result=[];
    else throw Error('Unexpected request '+url);
    return {ok:status<400,status,json:async()=>JSON.parse(JSON.stringify(result))};
  };
  w.eval(fs.readFileSync(path.join(root,'app/static/library.js'),'utf8'));
  if(publishing)w.PublishWorkbench=publishing;
  w.eval(fs.readFileSync(path.join(root,'app/static/app.js'),'utf8'));
  const click=selector=>{const element=d.querySelector(selector);assert.ok(element,selector);element.click()};
  const change=(selector,value)=>{const element=d.querySelector(selector);element.value=value;element.dispatchEvent(new w.Event('change',{bubbles:true}))};
  return {w,d,requests,state,click,change,media:()=>media,holdMedia:promise=>{heldMedia=promise},close:()=>w.close()};
}

test('product images are visible in table and cards, category filtering and thumbnail choices preserve selection',async()=>{
  const f=fixture();
  try{
    await until(()=>f.d.querySelector('.product-thumb img'));
    assert.equal(f.d.querySelector('.product-thumb img').getAttribute('src'),'/api/media/img1','First selected image is used without a separate cover');
    f.change('#product-category','服装');
    assert.equal(f.d.querySelectorAll('#product-table tbody tr').length,1);
    assert.match(f.d.querySelector('#product-table').textContent,/衬衫/);
    f.change('#product-category','');f.click('[data-action="product-view"][data-view="cards"]');
    assert.equal(f.d.querySelectorAll('.product-library-card').length,2);
    f.click('[data-action="edit-product"][data-id="p1"]');
    assert.equal(f.d.querySelectorAll('[data-image-choice] img').length,6);
    assert.equal(f.d.querySelector('[name="image_ids"][value="img1"]').checked,true);
    f.change('[data-image-choice-category]','服装');
    assert.equal([...f.d.querySelectorAll('[data-image-choice]')].filter(item=>!item.hidden).length,2);
    assert.equal(f.d.querySelector('[name="image_ids"][value="img1"]').checked,true,'Filtering does not clear checked images');
    const image=f.d.querySelector('[data-image-choice]:not([hidden]) img');
    image.getBoundingClientRect=()=>({left:20,top:20,right:120,bottom:120,width:100,height:100});
    image.dispatchEvent(new f.w.MouseEvent('pointermove',{bubbles:true,clientX:35,clientY:35}));
    const lens=f.d.querySelector('.sd-zoom-panel');assert.ok(lens&&!lens.hidden);const first=lens.style.backgroundPosition;
    image.dispatchEvent(new f.w.MouseEvent('pointermove',{bubbles:true,clientX:100,clientY:100}));
    assert.notEqual(lens.style.backgroundPosition,first,'Moving over an image changes the magnified crop');
    assert.equal(f.w.location.hash,'#products','Magnifying does not navigate');
    image.dispatchEvent(new f.w.MouseEvent('pointerout',{bubbles:true}));assert.equal(lens.hidden,true);
  }finally{f.close()}
});

test('media repository supports type, category including uncategorized, tags, search and direct preview',async()=>{
  const f=fixture('library');
  try{
    await until(()=>f.d.querySelectorAll('.library-item').length===4);
    assert.equal(new URL(f.requests.find(r=>r.url.startsWith('/api/media?')).url,'http://localhost').searchParams.has('category'),false,'All categories omit the parameter');
    assert.ok(f.d.querySelector('.library-item video[controls]'));
    f.change('#library-kind','image');await until(()=>f.d.querySelectorAll('.library-item').length===3);
    f.change('#library-category',JSON.stringify(''));await until(()=>f.d.querySelectorAll('.library-item').length===1);
    assert.match(f.d.querySelector('.library-grid').textContent,/未分类图片/);
    f.change('#library-category','');await until(()=>f.d.querySelectorAll('.library-item').length===3);
    f.change('#library-tag','主图');await until(()=>f.d.querySelectorAll('.library-item').length===1);
    f.click('[data-library-action="preview"][data-id="img1"]');
    assert.equal(f.d.querySelector('.library-preview-image').getAttribute('src'),'/api/media/img1');
    f.click('#modal [data-action="close"]');
    f.change('#library-tag','');await until(()=>f.d.querySelectorAll('.library-item').length===3);
    const input=f.d.querySelector('#library-search');input.value='衣服';input.dispatchEvent(new f.w.Event('input',{bubbles:true}));
    await until(()=>f.d.querySelectorAll('.library-item').length===1&&f.d.querySelector('.library-grid').textContent.includes('衣服'));
  }finally{f.close()}
});

test('directory import sends browser-selected files and relative paths to the local repository',async()=>{
  const f=fixture('library');
  try{
    await until(()=>f.d.querySelector('#library-directory'));
    f.d.querySelector('#library-upload-category').value='新包';f.d.querySelector('#library-upload-tags').value='新品, 主图';
    const files=[new f.w.File(['one'],'front.png',{type:'image/png'}),new f.w.File(['two'],'back.png',{type:'image/png'})];
    Object.defineProperty(files[0],'webkitRelativePath',{value:'选定目录/主图/front.png'});Object.defineProperty(files[1],'webkitRelativePath',{value:'选定目录/主图/back.png'});
    const picker=f.d.querySelector('#library-directory');Object.defineProperty(picker,'files',{configurable:true,value:files});picker.dispatchEvent(new f.w.Event('change',{bubbles:true}));
    await until(()=>f.requests.some(r=>r.url==='/api/media/import')&&f.d.querySelector('.library-progress')?.textContent.includes('导入完成'));
    const upload=f.requests.find(r=>r.url==='/api/media/import');
    assert.equal(upload.files.length,2);assert.deepEqual(upload.relativePaths,['选定目录/主图/front.png','选定目录/主图/back.png']);assert.equal(upload.category,'新包');assert.deepEqual(upload.tags,['新品','主图']);
    assert.equal(f.requests.some(r=>/commerce|publish|prepare-product/.test(r.url)),false,'Local import never contacts a platform');
  }finally{f.close()}
});

test('batch classification persists, referenced deletion reports its owner and unreferenced deletion succeeds',async()=>{
  const f=fixture('library');
  try{
    await until(()=>f.d.querySelector('[data-media-id="img2"]'));
    f.click('[data-media-id="img2"] [data-library-action="metadata"]');
    f.d.querySelector('#library-new-category').value='待发布';f.d.querySelector('#library-new-tags').value='选中,详情';
    f.d.querySelector('#library-metadata-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>!f.d.querySelector('#modal').open&&f.media().find(item=>item.id==='img2').category==='待发布');
    f.click('[data-media-id="img1"] [data-library-action="delete"]');
    assert.equal(f.requests.some(r=>r.url==='/api/media/batch-delete'),false,'Deletion requires confirmation');
    f.click('[data-library-action="confirm-delete"]');
    await until(()=>f.d.querySelector('[data-library-error]')?.textContent.includes('双肩包'));
    assert.ok(f.media().some(item=>item.id==='img1'));
    f.click('[data-library-action="close-modal"]');
    f.click('[data-media-id="img3"] [data-library-action="delete"]');f.click('[data-library-action="confirm-delete"]');
    await until(()=>!f.media().some(item=>item.id==='img3')&&!f.d.querySelector('#modal').open);
  }finally{f.close()}
});

test('a delayed media response does not replace another page',async()=>{
  const f=fixture();
  try{
    await until(()=>f.d.querySelector('#product-search'));
    let release;const gate=new Promise(resolve=>{release=resolve});f.holdMedia(gate);
    f.click('#nav [href="#library"]');await until(()=>f.requests.some(r=>r.url.startsWith('/api/media?')));
    f.click('#nav [href="#products"]');await until(()=>f.d.querySelector('#product-search'));
    release();await wait();assert.ok(f.d.querySelector('#product-search'));assert.equal(f.d.querySelector('.library-grid'),null);
  }finally{f.close()}
});

test('the main navigation and product actions hand off to the publishing module with the selected products',async()=>{
  const calls=[],f=fixture('products',{launch:async(ctx,ids)=>{assert.equal(typeof ctx.api,'function');calls.push([...ids])},page:async ctx=>{assert.equal(ctx.routeCurrent('publishing',ctx.generation),true);f.d.querySelector('#main').textContent='统一发布模块'}});
  try{
    await until(()=>f.d.querySelector('[data-action="publish-one"]'));
    f.click('[data-action="publish-one"][data-id="p1"]');await until(()=>calls.length===1);assert.deepEqual(calls[0],['p1']);
    f.click('.product-select[value="p1"]');f.click('.product-select[value="p2"]');
    // publish-one selected p1 already; check both explicitly for the batch.
    for(const element of f.d.querySelectorAll('.product-select')){element.checked=true;element.dispatchEvent(new f.w.Event('change',{bubbles:true}))}
    f.click('[data-action="product-batch"]');await until(()=>calls.length===2);assert.deepEqual(calls[1].sort(),['p1','p2']);
    f.click('#nav [href="#publishing"]');await until(()=>f.d.querySelector('#main').textContent==='统一发布模块');
  }finally{f.close()}
});

test('creating a new product opens an empty image chooser without a null crash',async()=>{
  const f=fixture();
  try{
    await until(()=>f.d.querySelector('[data-action="new-product"]'));
    f.click('[data-action="new-product"]');
    await until(()=>f.d.querySelector('#modal[open]')&&f.d.querySelector('#product-form'));
    assert.equal(f.d.querySelector('#modal h2').textContent,'新建商品');
    assert.ok(f.d.querySelector('[data-image-chooser]'),'image chooser is rendered');
    assert.equal(f.d.querySelectorAll('[name="image_ids"]:checked').length,0);
    assert.equal(f.d.querySelector('[name="title"]').value,'');
  }finally{f.close()}
});
