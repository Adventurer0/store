'use strict';
window.PublishWorkbench=(()=>{
  let ctx=null,group=null,liveGroup=null,groupId='',timer=null,serial=0,dirty=false,editVersion=0,busy=false,historyGroups=[],historyTotal=0,pendingDelete=null;
  const $=s=>document.querySelector(s),all=s=>Array.from(document.querySelectorAll(s));
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const action=(label,a,attrs='',disabled=false)=>`<button type="button" class="btn ${a==='submit'?'primary':''}" data-pw-action="${a}" ${attrs} ${disabled?'disabled':''}>${label}</button>`;
  const names={uncollected:'待读取平台要求',category:'填写类目',product_details:'完善商品信息',unidentified:'页面尚未识别',needs_input:'待补资料',prepared:'已回填检查',submitted:'已提交审核',uncertain:'结果待核对',queued_collect:'正在读取要求',running_collect:'正在读取要求',queued_prepare:'正在回填检查',running_prepare:'正在回填检查',queued_submit:'等待提交',running_submit:'正在提交'};
  const groupNames={editing:'编辑中',ready:'可提交',working:'执行中',submitted:'已提交',uncertain:'结果待核对'};
  const groupTones={submitted:'green',ready:'blue',working:'blue',uncertain:'orange'};
  const auditNames={unchecked:'未回查',checking:'回查中',pending:'审核中',approved:'审核通过',rejected:'审核驳回',offline:'已下架',unknown:'状态未知'};
  const when=n=>n?new Date(n*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'—';
  const media=()=>ctx?.getState?.().media||ctx?.state?.media||[];
  function closeModal(){if(ctx.closeModal)ctx.closeModal();else $('#modal').close();}
  function leave(){serial++;clearTimeout(timer);timer=null;}
  function hashGroup(){const [name,id]=location.hash.slice(1).split('/');return name==='publishing'&&id?decodeURIComponent(id):'';}
  function syncHash(id){try{if(location.hash!=='#publishing/'+id)window.history.replaceState(null,'','#publishing/'+id)}catch{}}
  function clearHash(){try{if(location.hash!=='#publishing')window.history.replaceState(null,'','#publishing')}catch{}}
  function current(n,id=groupId){return n===serial && id===groupId && location.hash.slice(1).split('/')[0]==='publishing';}
  function remember(id){groupId=id;try{localStorage.setItem('shopdock-publish-group',id)}catch{}}
  function forget(id){if(groupId===id)groupId='';try{if(localStorage.getItem('shopdock-publish-group')===id)localStorage.removeItem('shopdock-publish-group')}catch{}}
  function revisions(){return Object.fromEntries((group?.drafts||[]).map(d=>[d.id,d.revision]));}
  function hasJobs(data=liveGroup||group){return (data?.drafts||[]).some(d=>d.stores.some(s=>/^(queued|running)_/.test(s.status)||s.audit_status==='checking'));}
  function canSubmit(data=liveGroup||group){return data?.ready===true && data.drafts.length>0 && data.drafts.every(d=>d.ready===true&&d.stores.length>0&&d.stores.every(s=>s.ready===true&&s.local_quality_passed===true&&s.platform_quality_ok===true));}
  function syncActions(){for(const button of all('#main [data-pw-action]')){const type=button.dataset.pwAction;if(['collect','save','prepare','audit'].includes(type))button.disabled=busy||hasJobs();if(type==='retry-failed')button.disabled=busy||dirty||hasJobs();if(type==='submit')button.disabled=busy||dirty||hasJobs()||!canSubmit();}}
  function markDirty(event){if(event.target.closest('.pw-draft')){dirty=true;editVersion++;syncActions();}}
  function valueOf(f,value){return value===undefined?f.value:value;}
  function field(f,value,draftId,scope,storeId=''){
    const v=valueOf(f,value),id=esc('pw-'+draftId+'-'+scope+'-'+storeId+'-'+f.key);
    const attrs=`data-pw-key="${esc(f.key)}" data-pw-type="${esc(f.type)}" data-pw-scope="${scope}" data-pw-store="${esc(storeId)}"`;
    const required=f.required?'<b class="pw-required">必填</b>':'';
    let input='';
    if(f.type==='images'){
      const ids=Array.isArray(v)?v:[];
      input=`<div class="pw-image-options" ${attrs}>${media().filter(m=>m.kind==='image').map(m=>`<label class="pw-image-option"><input type="checkbox" value="${esc(m.id)}" ${ids.includes(m.id)?'checked':''}><img src="/api/media/${esc(m.id)}" alt="${esc(m.name)}" loading="lazy" class="sd-zoom-source" data-zoom-src="/api/media/${esc(m.id)}"><span>${esc(m.name)}</span></label>`).join('')||'<p>本地仓库还没有图片，请先上传素材。</p>'}</div>`;
    }else if(f.type==='skus'){
      const skus=Array.isArray(v)?v:[];
      input=`<div ${attrs} class="pw-sku-editor"><div class="pw-sku-head"><span>外部 SKU 编码</span><span>规格名称</span><span>售价 / 元</span><span>平台规格属性</span></div>${skus.map(s=>`<div class="pw-sku-row"><input aria-label="外部 SKU 编码" data-pw-sku="code" value="${esc(s.code)}"><input aria-label="规格名称" data-pw-sku="name" value="${esc(s.name)}"><input aria-label="售价 / 元" type="number" min="0" step="0.01" data-pw-sku="price" value="${Number(s.price_cents||0)/100}"><input aria-label="平台规格属性" data-pw-sku="attributes" value="${esc(Object.entries(s.attributes||{}).map(([k,x])=>k+'='+x).join('；'))}" placeholder="颜色=银色；尺寸=20寸"></div>`).join('')}<small>新建商品库存固定为 0；发布后再从统一库存分配额度。规格属性按真实商品填写。</small></div>`;
    }else if(f.type==='boolean'){
      input=`<input id="${id}" type="checkbox" ${attrs} ${v===true?'checked':''}>`;
    }else if(f.type==='select'&&f.options?.length){
      input=`<select id="${id}" ${attrs}><option value="">请选择</option>${f.options.map(o=>`<option value="${esc(o.value)}" ${String(o.value)===String(v)?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`;
    }else if(f.type==='multiselect'&&f.options?.length){
      const values=Array.isArray(v)?v.map(String):[];
      input=`<select id="${id}" multiple ${attrs}>${f.options.map(o=>`<option value="${esc(o.value)}" ${values.includes(String(o.value))?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`;
    }else if(['textarea','multiselect'].includes(f.type)){
      input=`<textarea id="${id}" ${attrs} ${f.max_length?'maxlength="'+Number(f.max_length)+'"':''}>${esc(Array.isArray(v)?v.join('\n'):v||'')}</textarea>`;
    }else{
      input=`<input id="${id}" ${attrs} type="${['number','integer'].includes(f.type)?'number':'text'}" value="${esc(v??'')}" ${f.max_length?'maxlength="'+Number(f.max_length)+'"':''} ${f.min!==undefined?'min="'+Number(f.min)+'"':''} ${f.max!==undefined?'max="'+Number(f.max)+'"':''} ${f.type==='integer'?'step="1"':''}>`;
    }
    return `<div class="field pw-field"><label for="${id}">${esc(f.label)} ${required}</label>${input}${f.help?`<small class="hint">${esc(f.help)}</small>`:''}</div>`;
  }
  function score(s){const local=s.local_quality||{},platform=s.quality||{},n=platform.score;const platformText=typeof n==='number'?(n>90?'平台 '+n+' 分':'平台 '+n+' 分未达标'):'平台评分未提供';return `<div class="pw-score ${local.passed?'pass':'pending'}"><strong>${typeof local.score==='number'?esc(local.score)+' 分':'本地评分不可用'}</strong><small>本地预检 ≥95 · ${platformText}${s.platform_quality_required?'（本店要求）':''}${platform.observed_at?' · '+new Date(platform.observed_at*1000).toLocaleTimeString('zh-CN'):''}</small></div>`;}
  function storeCard(s,d){
    // The business-form API already removes shared controls and retains explicit per-store overrides.
    const fields=s.fields||[];
    return `<section class="pw-store"><div class="pw-store-head"><div><strong>${esc(s.store_name)}</strong><small>${esc(s.platform_name||s.platform)} · ${esc(names[s.stage]||s.stage||'待读取')} · ${esc(names[s.status]||s.status)}</small></div>${score(s)}</div>${s.problems?.length?`<div class="notice">${s.problems.map(esc).join('<br>')}</div>`:''}${s.missing?.length?`<ul class="pw-missing">${s.missing.map(x=>`<li>${esc(x.label||x.key)}：${esc(x.message||'需要补充')}</li>`).join('')}</ul>`:''}<div class="pw-fields">${fields.map(f=>field(f,s.values?.[f.key],d.id,'store',s.store_id)).join('')||'<p class="inline-help">读取后会在这里列出平台差异项；公共信息不会重复填写。</p>'}</div>${!s.complete?'<p class="pw-stage-note">当前阶段尚未读取完整。保存资料并回填后，继续读取下一步的实际要求。</p>':''}${s.receipt?.external_id?`<p class="inline-help">平台商品编号：${esc(s.receipt.external_id)} · 审核：${esc(auditNames[s.audit_status]||s.audit_status||'未回查')}${s.audit_message?`<br>${esc(s.audit_message)}`:''}${s.audit_code?` · 原错误码 ${esc(s.audit_code)}`:''}</p>`:''}</section>`;
  }
  function historyRows(groups){
    if(!groups.length)return '<div class="card-body"><div class="empty"><strong>还没有发布批次。</strong><p>新建批次后，可在这里回到未提交的草稿继续填写。</p></div></div>';
    return `<div class="table-wrap"><table><thead><tr><th>批次</th><th>状态</th><th>目标</th><th>最近更新</th><th>操作</th></tr></thead><tbody>${groups.map(g=>{
      const title=(g.product_titles||[])[0]||'未命名商品',count=Number(g.product_count)||0;
      const locked=['submitted','uncertain','working'].includes(g.status);
      return `<tr><td><strong>${esc(title)}</strong><div class="subtle">${count>1?`等 ${count} 件商品 · `:''}批次 ${esc(String(g.id||'').slice(0,8))}</div></td><td><span class="badge ${groupTones[g.status]||''}">${esc(groupNames[g.status]||g.status)}</span>${g.uncertain_count?`<div class="subtle">待核对 ${Number(g.uncertain_count)}</div>`:''}</td><td>${Number(g.store_count)||0} 家店铺 · ${Number(g.target_count)||0} 项${g.submitted_count?`<div class="subtle">已提交 ${Number(g.submitted_count)}</div>`:''}</td><td class="subtle">${when(g.updated)}</td><td><div class="row-actions"><button type="button" class="link-btn" data-pw-open="${esc(g.id)}">打开</button><button type="button" class="link-btn" data-pw-delete="${esc(g.id)}" ${locked?'disabled':''}>删除</button></div></td></tr>`}).join('')}</tbody></table></div>`;
  }
  async function loadHistory(){const data=await ctx.api('/publish-groups');historyGroups=Array.isArray(data?.groups)?data.groups:[];historyTotal=Number(data?.total)||historyGroups.length;return historyGroups;}
  const historyCaption=()=>historyTotal>historyGroups.length?`共 ${historyTotal} 个 · 显示最近 ${historyGroups.length} 个`:`最近 ${historyGroups.length} 个批次`;
  async function showHistory(){
    const n=serial;
    let groups;
    try{groups=await loadHistory()}catch(e){ctx.toast(e.message,true);return false}
    if(n!==serial||location.hash.slice(1).split('/')[0]!=='publishing')return false;
    ctx.modal('发布记录',`<p class="inline-help">未提交的批次可以继续填写；已提交、执行中或待核对的批次保留用于对账，不能删除。</p>${historyRows(groups)}<div class="form-footer">${historyCaption()} · ${action('关闭','close-history')}</div>`,true);
    return true;
  }
  async function home(n,message=''){
    let groups=[],failure='';
    try{groups=await loadHistory()}catch(e){failure=e.message}
    if(n!==serial||location.hash.slice(1).split('/')[0]!=='publishing')return false;
    $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">UNIFIED PUBLISHING</div><h1>一次填写，发布到多家店。</h1><p>读取平台当前必填项，合并公共资料；本地预检达到 95 分，平台评分可用时还须高于 90 分。</p></div><div class="actions">${action('新建发布批次','new')}</div></div>`+(message?`<div class="notice">${esc(message)}</div>`:'')+`<section class="card"><div class="card-head"><h2>发布记录</h2><small>${historyCaption()}</small></div>${failure?`<div class="card-body"><div class="notice error">${esc(failure)}</div><div class="actions mt">${action('重试','history-refresh')}</div></div>`:historyRows(groups)}</section><div class="notice info">本地预检未达 95 分或存在硬性阻断时，提交按钮不会放行。未提交草稿保存在本机，只有你确认提交后才会写入平台。</div>`;
    return true;
  }
  function render(){
    const main=$('#main');if(!main||!group)return;
    main.innerHTML=`<div class="page-heading"><div><div class="eyebrow">ONE FORM · EVERY STORE</div><h1>统一填写，逐店核对，一次提交。</h1><p>公共资料只填一次，平台差异集中补充。本地预检须达到 95 分；平台评分可用时，还须高于 90 分。</p></div><div class="actions">${action('发布记录','history')}${action('新建发布批次','new')}</div></div><ol class="pw-steps"><li>选择商品和店铺</li><li>读取实际必填项</li><li>本地统一填写</li><li>回填与评分检查</li><li>统一提交审核</li></ol><div class="pw-live-status" role="status">${hasJobs()?'任务正在执行，页面自动更新。':'资料保存在本机发布草稿中。'}</div><div class="pw-actions">${action('读取 / 刷新平台要求','collect','',hasJobs())}${action('保存填写内容','save','',hasJobs())}<label class="check-label"><input type="checkbox" id="pw-upload">允许将选定商品图片上传至目标店铺，用于回填检查</label>${action('回填并检查质量','prepare','',hasJobs())}${group.drafts.some(d=>d.stores.some(s=>s.status==='needs_input'))?action('重试失败店铺','retry-failed','',dirty||hasJobs()):''}${group.drafts.some(d=>d.stores.some(s=>s.receipt?.external_id))?action('回查审核状态','audit','',hasJobs()):''}${action('统一提交审核','submit','',!canSubmit()||dirty||hasJobs())}</div><div class="notice info">本地评分只用于提前拦截问题，不冒充平台分数。任一店铺缺资料、本地预检低于95分、平台评分可用但未高于90分、评分失效或提交回执未适配时，不会将该批次标为发布成功。</div>${group.drafts.map(d=>`<article class="card pw-draft" data-pw-draft="${esc(d.id)}"><div class="card-head"><h2>${esc(d.product_title)}</h2><small>${d.mode==='update'?'更新 · ':''}资料版本 ${d.revision}</small></div><div class="card-body"><section class="pw-common"><h3>公共资料 <small>自动引用本地商品，跨平台重复项只填一次</small></h3><div class="pw-fields">${(d.common_fields||[]).map(f=>field(f,d.common_values?.[f.key],d.id,'common')).join('')||'<p class="inline-help">先读取各平台要求。商品标题、图片、价格与 SKU 将从本地资料带入，实际兼容字段才合并。</p>'}</div></section><div class="pw-stores">${d.stores.map(s=>storeCard(s,d)).join('')}</div></div></article>`).join('')}`;
  }
  async function load(n,force=false,id=groupId){
    const editorVersion=editVersion;
    const data=await ctx.api('/publish-groups/'+id);
    if(!current(n,id))return false;
    liveGroup=data;
    if(!dirty||(force&&editVersion===editorVersion)){group=data;dirty=false;render();}
    else if($('.pw-live-status'))$('.pw-live-status').textContent='平台状态已更新，正在填写的内容已保留；保存后显示新要求。';
    syncActions();
    clearTimeout(timer);
    if(hasJobs(data))timer=setTimeout(()=>load(n,false,id).catch(e=>{if(current(n,id))ctx.toast(e.message,true)}),2000);
    return true;
  }
  async function openGroup(id){
    if(!id||!ctx)return false;
    remember(id);syncHash(id);dirty=false;closeModal();
    await page(ctx);
    return true;
  }
  function confirmDelete(id){
    const item=historyGroups.find(g=>g.id===id)||{};
    const token=ctx.modal('删除发布批次',`<p>将删除此批次 ${Number(item.product_count)||0} 件商品在本机的发布草稿和平台字段填写记录。不会删除本地商品资料，也不会影响平台上已经提交的商品。</p>`+(dirty&&id===groupId?'<div class="notice">当前页面还有未保存修改，会一并放弃。</div>':'')+'<div class="notice">已提交、执行中或待核对的批次不能删除。</div><div class="form-error" data-pw-error role="alert"></div><div class="form-footer">'+action('取消','close-history')+action('确认删除','confirm-delete','data-pw-delete-id="'+esc(id)+'"')+'</div>');
    pendingDelete={id,token};
  }
  async function deleteGroup(id,token){
    if(!id)return false;
    await ctx.api('/publish-groups/'+encodeURIComponent(id),{method:'DELETE'});
    if(ctx.modalCurrent&&!ctx.modalCurrent(token))return false;
    ctx.toast('发布批次已删除');
    closeModal();
    pendingDelete=null;
    if(id===groupId){forget(id);dirty=false;clearHash();return home(serial)}
    if(!groupId)return home(serial);
    return load(serial,true);
  }
  async function page(context){
    leave();ctx=context;dirty=false;const n=serial;
    const fromHash=hashGroup();
    if(fromHash){groupId=fromHash;remember(groupId);}
    else if(!groupId){try{groupId=localStorage.getItem('shopdock-publish-group')||''}catch{}}
    if(!groupId){await home(n);return}
    if(!fromHash)syncHash(groupId);
    try{await load(n,true)}
    catch(e){
      if(n!==serial)return;
      if(e.status===404){forget(groupId??'');clearHash();await home(n,'此发布批次不存在或已被删除，已返回发布记录。')}
      else if(current(n))$('#main').innerHTML=`<div class="notice error">${esc(e.message)}</div><div class="actions">${action('返回发布记录','history-page')}${action('重新读取','history-refresh')}</div>`;
    }
  }
  function launch(context,productIds=[]){
    ctx=context;const state=ctx.getState?.()||ctx.state||{},ids=new Set(productIds);
    ctx.modal('统一发布：选择商品和目标店铺',`<form data-pw-form="new"><div class="field"><label for="pw-mode">操作方式</label><select id="pw-mode" name="mode"><option value="create">新建商品</option><option value="update">更新已关联商品</option></select><span class="hint">更新要求所选商品在每个目标店铺都已有平台商品 ID；只提交更新，不会生成新商品。</span></div><div class="pw-picker"><section><h3>本地商品</h3>${(state.products||[]).map(p=>`<label class="check-label"><input type="checkbox" name="product_ids" value="${esc(p.id)}" ${ids.has(p.id)?'checked':''}>${p.cover_id?`<img class="pw-mini sd-zoom-source" src="/api/media/${esc(p.cover_id)}" alt="" data-zoom-src="/api/media/${esc(p.cover_id)}">`:''}${esc(p.title)}</label>`).join('')||'<p>请先创建本地商品。</p>'}</section><section><h3>目标电商店铺</h3>${(state.stores||[]).map(s=>`<label class="check-label"><input type="checkbox" name="store_ids" value="${esc(s.id)}">${esc(s.name)} · ${esc(state.platforms?.[s.platform]?.name||s.platform)} · ${s.commerce_status==='valid'?'已登录':'需登录电商后台'}</label>`).join('')||'<p>请先添加店铺。</p>'}</section></div><div class="notice info">读取平台要求不会上传图片或提交商品。类目、运费模板等平台差异会分别保存。</div><div role="alert" class="form-error" data-pw-error></div><div class="form-footer"><button class="btn primary" type="submit">建立统一填写表单</button></div></form>`,true);
  }
  function readFields(container,scope,sid){
    const result={};
    for(const el of container.querySelectorAll('[data-pw-key]')){
      if(el.dataset.pwScope!==scope||(sid!==undefined&&el.dataset.pwStore!==sid))continue;
      const typ=el.dataset.pwType;let v;
      if(typ==='images')v=Array.from(el.querySelectorAll('input:checked')).map(x=>x.value);
      else if(typ==='skus')v=Array.from(el.querySelectorAll('.pw-sku-row')).map(row=>{
        const get=k=>row.querySelector('[data-pw-sku="'+k+'"]').value;
        const attributes={};for(const part of get('attributes').split(/[；;\n]/)){const [k,...rest]=part.split('=');if(k?.trim()&&rest.length)attributes[k.trim()]=rest.join('=').trim()}
        return {code:get('code'),name:get('name'),price_cents:Math.round(Number(get('price'))*100),stock:0,...(Object.keys(attributes).length?{attributes}:{})};
      });
      else if(typ==='boolean')v=el.checked;
      else if(typ==='multiselect')v=el.tagName==='SELECT'?Array.from(el.selectedOptions).map(x=>x.value):el.value.split('\n').map(x=>x.trim()).filter(Boolean);
      else if(['number','integer'].includes(typ))v=el.value===''?'':Number(el.value);
      else v=el.value;
      result[el.dataset.pwKey]=v;
    }
    return result;
  }
  async function save(n=serial,id=groupId){
    if(!current(n,id))return false;
    const version=editVersion;
    const pending=[];
    for(const draft of group.drafts){
      const node=$('[data-pw-draft="'+draft.id+'"]');if(!node)continue;
      const common_values={...draft.common_values,...readFields(node,'common')},store_values={...draft.store_values};
      for(const store of draft.stores)store_values[store.store_id]={...store_values[store.store_id],...readFields(node,'store',store.store_id)};
      pending.push({draft,body:{revision:draft.revision,common_values,store_values}});
    }
    for(const item of pending){
      const result=await ctx.api('/publish-drafts/'+item.draft.id,{method:'PUT',body:item.body});
      if(!current(n,id))return false;
      if(Number.isInteger(result?.revision))item.draft.revision=result.revision;
    }
    if(editVersion!==version){dirty=true;await load(n,false,id);ctx.toast('保存期间还有新的修改，请再次保存后继续');return false;}
    dirty=false;const loaded=await load(n,true,id);return loaded&&!dirty;
  }
  async function queue(kind){
    const n=serial,id=groupId,upload_images=$('#pw-upload')?.checked===true;
    if(!current(n,id))return false;
    if(kind==='submit'&&(dirty||!canSubmit()))throw Error('资料或质量检查已变化，请重新回填检查后再提交');
    if(dirty&&!await save(n,id))return false;
    if(!current(n,id))return false;
    await ctx.api('/publish-groups/'+id+'/'+kind,{method:'POST',body:kind==='collect'?{}:{revisions:revisions(),upload_images}});
    if(!current(n,id))return false;
    return load(n,true,id);
  }
  document.addEventListener('submit',async event=>{
    const form=event.target;if(form.dataset.pwForm!=='new')return;
    event.preventDefault();const button=form.querySelector('button[type=submit]'),n=serial,hash=location.hash;button.disabled=true;
    try{
      const data=new FormData(form),product_ids=data.getAll('product_ids'),store_ids=data.getAll('store_ids'),mode=data.get('mode')==='update'?'update':'create';
      if(!product_ids.length||!store_ids.length)throw Error('请至少选择一件商品和一家目标店铺');
      const created=await ctx.api('/publish-groups',{method:'POST',body:mode==='update'?{product_ids,store_ids,mode}:{product_ids,store_ids}});
      if(n!==serial||location.hash!==hash||!form.isConnected||!$('#modal').open)return;
      group=created;liveGroup=created;remember(group.id);dirty=false;closeModal();
      if(location.hash.slice(1).split('/')[0]==='publishing'){syncHash(group.id);await page(ctx)}else location.hash='publishing/'+group.id;
    }catch(e){form.querySelector('[data-pw-error]').textContent=e.message}finally{button.disabled=false}
  });
  document.addEventListener('input',markDirty);
  document.addEventListener('change',markDirty);
  document.addEventListener('click',async event=>{
    const opener=event.target.closest('[data-pw-open]');
    if(opener){if(!ctx||busy)return;busy=true;try{await openGroup(opener.dataset.pwOpen)}catch(e){ctx.toast(e.message,true)}finally{busy=false}return}
    const remover=event.target.closest('[data-pw-delete]');
    if(remover){if(!ctx||busy||remover.disabled)return;confirmDelete(remover.dataset.pwDelete);return}
    const button=event.target.closest('[data-pw-action]');if(!button||button.disabled||!ctx)return;
    const type=button.dataset.pwAction;if(busy)return;busy=true;button.disabled=true;syncActions();
    try{
      if(type==='new')launch(ctx);
      if(type==='history')await showHistory();
      if(type==='history-page'){forget(groupId);dirty=false;clearHash();await home(serial);}
      if(type==='history-refresh'){if(groupId)await load(serial,true);else await home(serial);}
      if(type==='close-history')ctx.closeModal();
      if(type==='confirm-delete'){
        const pending=pendingDelete;
        try{await deleteGroup(pending?pending.id:button.dataset.pwDeleteId,pending?pending.token:undefined)}
        catch(e){const area=$('#modal [data-pw-error]');if(area)area.textContent=e.message;else throw e}
      }
      if(type==='audit'){
        const n=serial,id=groupId;
        await ctx.api('/publish-groups/'+id+'/audit',{method:'POST',body:{}});
        if(current(n,id)){ctx.toast('已加入审核状态回查队列');await load(n,true,id);}
      }
      if(type==='retry-failed'){
        if(dirty)throw Error('资料已修改，请先保存再重试失败店铺');
        const n=serial,id=groupId;
        await ctx.api('/publish-groups/'+id+'/retry-failed',{method:'POST',body:{upload_images:$('#pw-upload')?.checked===true}});
        if(current(n,id)){ctx.toast('已仅对失败店铺重新入队，已提交目标保持不变');await load(n,true,id);}
      }
      if(type==='collect')await queue('collect');
      if(type==='save'&&await save())ctx.toast('统一发布资料已保存在本机');
      if(type==='prepare'){
        if(!$('#pw-upload')?.checked)throw Error('回填会把选定商品图片上传至目标店铺，请先勾选图片上传确认');
        await queue('prepare');
      }
      if(type==='submit'){
        if(dirty)throw Error('资料已修改，请保存并重新回填评分后再提交');
        if(!canSubmit())throw Error('还有店铺未通过真实评分或必填检查');
        ctx.modal('确认统一提交审核',`<p>将向 ${group.drafts.length} 件商品的全部目标店铺提交审核。新商品库存为零；每店独立返回实际结果。</p><div class="notice info">各店铺本地预检须达95分；平台评分可用时还须高于90分，提交前会再次检查。跨平台无法保证同时成功。</div><div class="form-footer">${action('确认提交审核','confirm-submit')}</div>`);
      }
      if(type==='confirm-submit'&&await queue('submit')){closeModal();ctx.toast('已统一入队，逐店核对平台回执');}
    }catch(e){ctx.toast(e.message,true)}finally{busy=false;button.disabled=false;syncActions();}
  });
  window.addEventListener('hashchange',()=>{if(location.hash.slice(1).split('/')[0]!=='publishing')leave()});
  return {page,launch,leave};
})();
