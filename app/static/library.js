'use strict';
window.MediaLibrary=(()=>{
  let context=null,pageContext=null,requestVersion=0,searchTimer=null,uploading=false;
  const filters={q:'',kind:'',category:'',tag:'',offset:0,limit:60},selected=new Set(),known=new Map();
  let rows=[],report={total:0,categories:[],tags:[]},pendingDelete=null,pendingMetadata=null,progress='';
  const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const url=id=>'/api/media/'+encodeURIComponent(id);
  const size=value=>value>=1024*1024?(value/1024/1024).toFixed(1)+' MB':Math.ceil((value||0)/1024)+' KB';
  const button=(label,action,attributes='',primary=false)=>`<button type="button" class="btn ${primary?'primary':''}" data-library-action="${action}" ${attributes}>${label}</button>`;
  const active=ctx=>!!ctx&&ctx.routeCurrent('library',ctx.generation);
  const tags=value=>[...new Set(String(value||'').split(/[,，\n]/).map(item=>item.trim()).filter(Boolean))];
  const choices=(items,current,emptyLabel,category=false)=>`<option value="">${emptyLabel}</option>${items.filter(item=>category||item.name).map(item=>{const value=category?JSON.stringify(item.name):item.name;return `<option value="${escape(value)}" ${value===current?'selected':''}>${escape(item.name||'未分类')}${item.count==null?'':` (${item.count})`}</option>`}).join('')}`;
  function configure(ctx){context=ctx}
  function leave(){++requestVersion;clearTimeout(searchTimer);searchTimer=null;hideZoom();pageContext=null}
  function productThumb(product){
    const id=product.cover_id||product.image_ids?.[0];
    return id?`<span class="product-thumb product-thumb-large"><img src="${url(id)}" alt="${escape(product.title)}主图" loading="lazy" class="sd-zoom-source" data-zoom-src="${url(id)}"></span>`:'<span class="product-thumb product-thumb-large" aria-label="暂无主图">无主图</span>';
  }
  function productCards(products,checked){return `<div class="product-card-grid">${products.map(product=>`<article class="product-library-card"><label><input type="checkbox" class="product-select" value="${escape(product.id)}" ${checked.has(product.id)?'checked':''} aria-label="选择 ${escape(product.title)}">${escape(product.category||'未分类')}</label>${productThumb(product)}<div class="product-card-body"><strong title="${escape(product.title)}">${escape(product.title)}</strong><div class="money">${new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(Math.min(...product.skus.map(sku=>sku.price_cents))/100)}</div><p class="subtle">${product.skus.length} 个规格 · 库存 ${product.skus.reduce((sum,sku)=>sum+sku.stock,0)}</p><div class="row-actions"><button type="button" class="link-btn" data-action="publish-one" data-id="${escape(product.id)}">多店铺货</button><button type="button" class="link-btn" data-action="edit-product" data-id="${escape(product.id)}">编辑</button><button type="button" class="link-btn" data-action="mappings" data-id="${escape(product.id)}">关联</button></div></div></article>`).join('')}</div>`}
  function imageChoices(product={},media=[]){
    // A new product passes null; a default parameter only covers undefined.
    product=product||{};
    const images=media.filter(item=>item.kind==='image'),categories=[...new Set(images.map(item=>item.category).filter(Boolean))].sort();
    const group=(name,ids)=>`<div class="image-choice-grid">${images.map(item=>`<label class="image-choice" data-image-choice data-image-name="${escape(item.name.toLowerCase())}" data-image-category="${escape(item.category||'')}"><input type="checkbox" name="${name}" value="${escape(item.id)}" ${ids.includes(item.id)?'checked':''}><img src="${url(item.id)}" alt="${escape(item.name)}" loading="lazy" class="sd-zoom-source" data-zoom-src="${url(item.id)}"><span title="${escape(item.name)}">${escape(item.name)}</span><small>${escape(item.category||'未分类')}</small></label>`).join('')||'<p class="inline-help">还没有图片，请先上传或从素材仓库导入。</p>'}</div>`;
    return `<section data-image-chooser><div class="image-choice-toolbar"><input data-image-choice-search placeholder="筛选图片名称" aria-label="筛选商品图片"><select data-image-choice-category aria-label="按素材分类筛选图片"><option value="">全部素材分类</option>${categories.map(category=>`<option value="${escape(category)}">${escape(category)}</option>`).join('')}</select><small class="subtle">勾选后保存商品；悬浮图片可局部放大。</small></div><div class="field"><label>商品主图</label>${group('image_ids',product.image_ids||[])}</div><details><summary>选择详情图</summary>${group('detail_image_ids',product.detail_image_ids||[])}</details></section>`;
  }
  function filterChoices(root){
    const q=root.querySelector('[data-image-choice-search]').value.toLowerCase(),category=root.querySelector('[data-image-choice-category]').value;
    for(const item of root.querySelectorAll('[data-image-choice]'))item.hidden=!!(q&&!item.dataset.imageName.includes(q)||category&&item.dataset.imageCategory!==category);
  }
  async function page(ctx){pageContext=ctx;context=ctx;await load(ctx,true)}
  async function load(ctx=pageContext,initial=false){
    if(!active(ctx))return;
    const version=++requestVersion;
    if(initial)document.querySelector('#main').innerHTML=ctx.heading('素材仓库','图片与视频集中管理，选择文件或目录导入。','','MEDIA LIBRARY')+'<div class="loading">正在读取素材…</div>';
    const query=new URLSearchParams();
    for(const [key,value] of Object.entries(filters)){if(key==='category'){if(value)query.set(key,JSON.parse(value))}else if(value!==''&&value!=null)query.set(key,String(value))}
    const data=await ctx.api('/media?'+query);
    if(!active(ctx)||version!==requestVersion)return;
    rows=data.rows||[];report=data;rows.forEach(item=>known.set(item.id,item));render(ctx);
  }
  function render(ctx){
    if(!active(ctx))return;
    const searchFocused=document.activeElement?.id==='library-search',cursor=document.querySelector('#library-search')?.selectionStart;
    const previousCategory=document.querySelector('#library-upload-category')?.value||'',previousTags=document.querySelector('#library-upload-tags')?.value||'';
    document.querySelector('#main').innerHTML=ctx.heading('素材仓库','图片、视频直接预览；分类与标签便于商品选图。',button('选择文件','pick-files','',true)+button('导入本地目录','pick-directory'),'MEDIA LIBRARY')+`<section class="card"><div class="toolbar library-controls"><label class="search"><input id="library-search" aria-label="搜索素材" placeholder="搜索文件名、标签或来源路径" value="${escape(filters.q)}"></label><div class="field"><label for="library-kind">类型</label><select id="library-kind"><option value="">图片与视频</option><option value="image" ${filters.kind==='image'?'selected':''}>图片</option><option value="video" ${filters.kind==='video'?'selected':''}>视频</option></select></div><div class="field"><label for="library-category">分类</label><select id="library-category">${choices(report.categories||[],filters.category,'全部分类',true)}</select></div><div class="field"><label for="library-tag">标签</label><select id="library-tag">${choices(report.tags||[],filters.tag,'全部标签')}</select></div>${button('刷新','refresh')}</div><div class="library-import-settings"><div class="form-grid"><div class="field"><label for="library-upload-category">本次导入分类</label><input id="library-upload-category" value="${escape(previousCategory)}" placeholder="例如：箱包主图、秋冬视频"></div><div class="field"><label for="library-upload-tags">本次导入标签</label><input id="library-upload-tags" value="${escape(previousTags)}" placeholder="逗号分隔，可留空"></div></div><p class="inline-help">选择文件后开始导入本地仓库，不会上传到电商平台。目录结构保留为来源路径；每个文件按当前分类和标签导入。</p><input type="file" id="library-files" multiple accept=".jpg,.jpeg,.png,.webp,.mp4,.mov,.webm" hidden><input type="file" id="library-directory" multiple webkitdirectory directory hidden></div><div class="library-progress" role="status" ${progress?'':'hidden'}>${escape(progress)}</div><div class="toolbar"><label class="check-label"><input type="checkbox" id="library-select-page" ${rows.length&&rows.every(item=>selected.has(item.id))?'checked':''}>选择当前页</label><span class="library-bulk-summary">已选 ${selected.size} 项</span>${button('分类 / 标签','bulk-metadata',selected.size?'':'disabled')}${button('删除所选','bulk-delete',selected.size?'':'disabled')}${button('清除选择','clear-selection',selected.size?'':'disabled')}</div><div class="library-grid">${rows.map(item=>card(item)).join('')||'<div class="empty"><strong>没有匹配的素材</strong><p>调整筛选条件，或从本地导入图片和视频。</p></div>'}</div><div class="table-footer"><span>共 ${report.total||0} 项 · 第 ${Math.floor(filters.offset/filters.limit)+1} 页</span><div class="actions">${button('上一页','previous',filters.offset?'':'disabled')}${button('下一页','next',filters.offset+rows.length<(report.total||0)?'':'disabled')}</div></div></section>`;
    if(searchFocused){const input=document.querySelector('#library-search');input.focus();if(cursor!=null)input.setSelectionRange(cursor,cursor)}
  }
  function card(item){
    return `<article class="library-item ${selected.has(item.id)?'is-selected':''}" data-media-id="${escape(item.id)}"><div class="library-item-head"><label><input type="checkbox" data-library-select value="${escape(item.id)}" ${selected.has(item.id)?'checked':''} aria-label="选择素材 ${escape(item.name)}">${item.kind==='image'?'图片':'视频'}</label><span>${size(item.size)}</span></div>${item.kind==='image'?`<button class="library-image-button" type="button" data-library-action="preview" data-id="${escape(item.id)}" aria-label="预览 ${escape(item.name)}"><img src="${url(item.id)}" alt="${escape(item.name)}" loading="lazy" class="sd-zoom-source" data-zoom-src="${url(item.id)}"></button>`:`<video src="${url(item.id)}" controls preload="metadata" aria-label="预览 ${escape(item.name)}"></video>`}<div class="library-item-body"><strong title="${escape(item.name)}">${escape(item.name)}</strong><div class="media-meta">${escape(item.category||'未分类')} · ${item.created?new Date(item.created*1000).toLocaleDateString('zh-CN'):''}</div><div class="library-tags">${(item.tags||[]).map(tag=>`<span class="library-tag">${escape(tag)}</span>`).join('')}</div><div class="actions">${button('预览','preview',`data-id="${escape(item.id)}"`)}${button('分类 / 标签','metadata',`data-id="${escape(item.id)}"`)}${button('删除','delete',`data-id="${escape(item.id)}"`)}</div></div></article>`;
  }
  function preview(id){
    const item=known.get(id)||context.getState().media.find(media=>media.id===id);if(!item)return;
    context.modal('素材预览',`<p><strong>${escape(item.name)}</strong> · ${escape(item.category||'未分类')} · ${size(item.size)}</p>${item.kind==='image'?`<img src="${url(id)}" alt="${escape(item.name)}" class="library-preview-image sd-zoom-source" data-zoom-src="${url(id)}">`:`<video src="${url(id)}" controls preload="metadata" class="library-preview-video"></video>`}<p class="inline-help">${escape((item.tags||[]).join(' · '))}${item.source_path?`<br>来源：${escape(item.source_path)}`:''}</p>`,true);
  }
  function deletion(ids){
    if(!ids.length)return;
    const token=context.modal('删除本地素材',`<div class="notice">将从本地仓库删除 ${ids.length} 项素材。仍被商品或活动任务引用的素材会保留，并列出引用位置。</div><ul>${ids.slice(0,30).map(id=>`<li>${escape(known.get(id)?.name||id)}</li>`).join('')}</ul><div class="form-error" data-library-error role="alert"></div><div class="form-footer">${button('取消','close-modal')}${button('确认删除','confirm-delete','',true)}</div>`);
    pendingDelete={ids,token};
  }
  function metadata(ids){
    if(!ids.length)return;const item=ids.length===1?known.get(ids[0]):null;
    const token=context.modal('修改素材分类与标签',`<form id="library-metadata-form"><p class="inline-help">本次修改 ${ids.length} 项素材。</p><label class="check-label"><input type="checkbox" name="apply_category" checked>修改分类</label><div class="field"><label for="library-new-category">分类名称</label><input id="library-new-category" name="category" value="${escape(item?.category||'')}" placeholder="留空清除分类"></div><label class="check-label"><input type="checkbox" name="apply_tags" ${item?'checked':''}>修改标签（替换原标签）</label><div class="field"><label for="library-new-tags">标签</label><input id="library-new-tags" name="tags" value="${escape((item?.tags||[]).join(', '))}" placeholder="逗号分隔，留空清除标签"></div><div class="form-error" data-library-error role="alert"></div><div class="form-footer">${button('取消','close-modal')}<button type="submit" class="btn primary">保存分类与标签</button></div></form>`);
    pendingMetadata={ids,token};
  }
  function modalCurrent(token){return context.modalCurrent?context.modalCurrent(token):document.querySelector('#modal')?.open}
  async function confirmDelete(){
    const pending=pendingDelete;if(!pending||!modalCurrent(pending.token))return;
    try{
      const result=await context.api('/media/batch-delete',{method:'POST',body:{ids:pending.ids}});
      const deleted=result.deleted||pending.ids;deleted.forEach(id=>selected.delete(id));
      if(modalCurrent(pending.token))context.closeModal();pendingDelete=null;
      context.toast(`已删除 ${deleted.length} 项本地素材`);await context.refresh();await load();
    }catch(error){
      if(!modalCurrent(pending.token))return;
      const detail=error.detail||{},references=detail.references||[];
      const area=document.querySelector('[data-library-error]');if(area)area.innerHTML=`${escape(detail.message||error.message)}${references.length?`<ul class="library-references">${references.map(ref=>`<li>${escape(ref.title||ref.id)} · ${escape(ref.type)}</li>`).join('')}</ul>`:''}`;
    }
  }
  function updateProgress(message){progress=message;const el=document.querySelector('.library-progress');if(el){el.hidden=false;el.textContent=message}}
  async function importFiles(files){
    if(uploading){context.toast('已有导入任务正在进行');return}
    const all=[...files],supported=all.filter(file=>/\.(?:jpe?g|png|webp|mp4|mov|webm)$/i.test(file.name)),ignored=all.length-supported.length;
    if(!supported.length){context.toast('没有支持的图片或视频文件',true);return}
    const category=document.querySelector('#library-upload-category')?.value.trim()||'',labels=tags(document.querySelector('#library-upload-tags')?.value);
    uploading=true;let imported=0,duplicates=0,done=0;const errors=[];
    const maxBytes=(context.getState().max_upload_mb||1024)*1024*1024,batches=[];let batch=[],bytes=0;
    for(const file of supported){if(file.size>maxBytes){errors.push({name:file.name,message:'超过单文件上传上限'});continue}if(batch.length&&(batch.length>=20||bytes+file.size>Math.min(maxBytes,256*1024*1024))){batches.push(batch);batch=[];bytes=0}batch.push(file);bytes+=file.size}if(batch.length)batches.push(batch);
    try{
      for(const entries of batches){
        updateProgress(`正在导入 ${done+1}–${done+entries.length} / ${supported.length}…`);
        const body=new FormData();for(const file of entries)body.append('files',file,file.name);
        body.append('relative_paths',JSON.stringify(entries.map(file=>file.webkitRelativePath||file.name)));body.append('category',category);body.append('tags',JSON.stringify(labels));
        try{const result=await context.api('/media/import',{method:'POST',body});imported+=Number(result.imported??result.rows?.length??0);duplicates+=Array.isArray(result.duplicates)?result.duplicates.length:Number(result.duplicates||0);errors.push(...(result.errors||[]))}catch(error){errors.push(...entries.map(file=>({name:file.name,message:error.message})))}
        done+=entries.length;
      }
      updateProgress(`导入完成：新增 ${imported}，重复 ${duplicates}，失败 ${errors.length}${ignored?`，忽略不支持的文件 ${ignored}`:''}${errors.length?'。'+errors.slice(0,8).map(error=>`${error.name}：${error.message}`).join('；'):''}`);
      await context.refresh();await load();context.toast(`素材导入完成，新增 ${imported} 项`,!!errors.length);
    }finally{uploading=false}
  }
  async function handle(action,element){
    const id=element.dataset.id;
    if(action==='pick-files'||action==='pick-directory'){if(uploading){context.toast('已有导入任务正在进行');return}document.querySelector(action==='pick-files'?'#library-files':'#library-directory').click();return}
    if(action==='preview'){preview(id);return}
    if(action==='delete'){deletion([id]);return}
    if(action==='bulk-delete'){deletion([...selected]);return}
    if(action==='confirm-delete'){element.disabled=true;try{await confirmDelete()}finally{element.disabled=false}return}
    if(action==='metadata'){metadata([id]);return}
    if(action==='bulk-metadata'){metadata([...selected]);return}
    if(action==='close-modal'){context.closeModal();return}
    if(action==='clear-selection'){selected.clear();render(pageContext);return}
    if(action==='previous')filters.offset=Math.max(0,filters.offset-filters.limit);
    if(action==='next')filters.offset+=filters.limit;
    if(['previous','next','refresh'].includes(action))await load();
  }
  document.addEventListener('click',event=>{const element=event.target.closest('[data-library-action]');if(!element)return;event.preventDefault();handle(element.dataset.libraryAction,element).catch(error=>context?.toast(error.message,true))});
  document.addEventListener('input',event=>{
    if(event.target.matches('[data-image-choice-search]'))filterChoices(event.target.closest('[data-image-chooser]'));
    if(event.target.id==='library-search'){filters.q=event.target.value;filters.offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>load().catch(error=>context.toast(error.message,true)),250)}
  });
  document.addEventListener('change',event=>{
    const element=event.target;
    if(element.matches('[data-image-choice-category]')){filterChoices(element.closest('[data-image-chooser]'));return}
    if(element.matches('[data-library-select]')){element.checked?selected.add(element.value):selected.delete(element.value);render(pageContext);return}
    if(element.id==='library-select-page'){rows.forEach(item=>element.checked?selected.add(item.id):selected.delete(item.id));render(pageContext);return}
    const key={'library-kind':'kind','library-category':'category','library-tag':'tag'}[element.id];
    if(key){filters[key]=element.value;filters.offset=0;load().catch(error=>context.toast(error.message,true));return}
    if(element.id==='library-files'||element.id==='library-directory'){importFiles(element.files).catch(error=>context.toast(error.message,true));element.value=''}
  });
  document.addEventListener('submit',async event=>{
    if(event.target.id!=='library-metadata-form')return;event.preventDefault();event.stopImmediatePropagation();
    const pending=pendingMetadata;if(!pending||!modalCurrent(pending.token))return;
    const form=event.target,body={ids:pending.ids};if(form.elements.apply_category.checked)body.category=form.elements.category.value.trim();if(form.elements.apply_tags.checked)body.tags=tags(form.elements.tags.value);
    const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try{await context.api('/media/batch-update',{method:'POST',body});if(modalCurrent(pending.token))context.closeModal();pendingMetadata=null;await context.refresh();await load();context.toast('素材分类与标签已保存')}catch(error){if(modalCurrent(pending.token))form.querySelector('[data-library-error]').textContent=error.message}finally{submit.disabled=false}
  });
  let zoom=null,zoomSource=null;
  function hideZoom(){if(zoom)zoom.hidden=true;zoomSource=null}
  function magnify(event){
    const source=event.target.closest?.('img[data-zoom-src]');if(!source){hideZoom();return}
    const rect=source.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
    const naturalWidth=source.naturalWidth||rect.width,naturalHeight=source.naturalHeight||rect.height,scale=Math.min(rect.width/naturalWidth,rect.height/naturalHeight),width=naturalWidth*scale,height=naturalHeight*scale,left=rect.left+(rect.width-width)/2,top=rect.top+(rect.height-height)/2;
    const x=event.clientX-left,y=event.clientY-top;if(x<0||y<0||x>width||y>height){hideZoom();return}
    if(!zoom){zoom=document.createElement('div');zoom.className='sd-zoom-panel';zoom.setAttribute('aria-hidden','true')}
    const layer=source.closest('dialog')||document.body;if(zoom.parentElement!==layer)layer.appendChild(zoom);zoom.hidden=false;zoomSource=source;
    const bounds=layer.tagName==='DIALOG'?layer.getBoundingClientRect():{left:0,top:0,right:window.innerWidth,bottom:window.innerHeight};
    const panel=window.innerWidth<650?210:260,multiplier=Math.max(3,panel/Math.min(width,height)+1),leftCandidate=rect.right+12;
    zoom.style.width=zoom.style.height=panel+'px';zoom.style.left=Math.max(bounds.left+8,Math.min(leftCandidate+panel>bounds.right?rect.left-panel-12:leftCandidate,bounds.right-panel-8))+'px';zoom.style.top=Math.max(bounds.top+8,Math.min(event.clientY-panel/2,bounds.bottom-panel-8))+'px';zoom.style.backgroundImage=`url("${source.dataset.zoomSrc}")`;zoom.style.backgroundSize=`${width*multiplier}px ${height*multiplier}px`;zoom.style.backgroundPosition=`${Math.max(panel-width*multiplier,Math.min(0,panel/2-x*multiplier))}px ${Math.max(panel-height*multiplier,Math.min(0,panel/2-y*multiplier))}px`;
  }
  document.addEventListener('pointermove',magnify);
  document.addEventListener('pointerout',event=>{if(zoomSource&&event.target===zoomSource)hideZoom()});
  window.addEventListener('scroll',hideZoom,true);window.addEventListener('blur',hideZoom);
  return {configure,page,leave,productThumb,productCards,imageChoices,hideZoom};
})();
