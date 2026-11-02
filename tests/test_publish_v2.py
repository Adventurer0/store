import json
from pathlib import Path
from urllib.parse import parse_qs
import httpx
import pytest
from app import db, jobs
from app.connectors import Commerce, Blocked, Uncertain, Rejected, product_payload, normalize_order
from app.ks_video import KuaishouVideo
from app.publishing import materialize_request, upload_image, capture_sku_map, external_number


def store(platform):
    return {'id': 's1','name':'测试店','platform':platform,'api_enabled':True,'video_enabled':True,'credentials':{'app_key':'key','app_secret':'secret','sign_secret':'sign','access_token':'token','video_app_id':'video-key','video_access_token':'video-token'},'templates':{'publish_defaults':{'category_id':'123','freight_id':'0','native':{}}}}


def product():
    return {'id':'p1','title':'测试商品','description':'一份公共描述','images':['https://example.com/1.jpg','https://example.com/2.jpg','https://example.com/3.jpg'],'skus':[{'code':'BOX','name':'银色','price_cents':100,'stock':10}]}


@pytest.mark.parametrize('platform,field,stockfield',[('douyin','spec_prices','stock_num'),('wechat','skus','stock_num'),('kuaishou','skuList','skuStock')])
def test_three_platform_single_source_mapping_and_zero_stock(platform,field,stockfield):
    s,p=store(platform),product()
    body=product_payload(s,p,{'publish_config':{'category_id':'999'}},'product_create')
    assert body.get('title',body.get('name'))==p['title']
    skus=json.loads(body[field]) if isinstance(body[field],str) else body[field]
    assert skus[0][stockfield]==0
    assert skus[0].get('price',skus[0].get('sale_price',skus[0].get('skuSalePrice')))==100
    assert str(body.get('category_leaf_id',body.get('categoryId',body.get('cats',[{}])[0].get('cat_id'))))=='999'
    assert p['skus'][0]['stock']==10


def test_required_mapping_per_store_and_existing_product_guard():
    s,p=store('kuaishou'),product()
    s['templates']['publish_defaults']['category_id']='__REQUIRED__'
    with pytest.raises(Blocked):product_payload(s,p,{},'product_create')
    s=store('kuaishou')
    with pytest.raises(Blocked):product_payload(s,p,{'external_id':'123'},'product_create')
    with pytest.raises(Blocked):product_payload(s,p,{'external_id':'123'},'product_update')
    body=product_payload(s,p,{'external_id':'123','sku_map':{'BOX':'456'}},'product_update')
    assert body['skuList'][0]['skuId']==456 and 'skuStock' not in body['skuList'][0]


def make_media(kind='image',blob=b'png',mime='image/png',mid=None):
    mid=mid or db.uid();path='media/'+mid+('.png' if kind=='image' else '.mp4')
    (db.DATA/path).write_bytes(blob)
    with db.connection() as c:c.execute('INSERT INTO media VALUES(?,?,?,?,?,?,?,?)',(mid,'test.png' if kind=='image' else 'test.mp4',kind,mime,path,len(blob),'hash',db.now()))
    return mid,path


def test_kuaishou_official_link_flow_and_large_goods_id(owner):
    cover,_=make_media();vid,path=make_media('video',b'video'*100,'video/mp4')
    p={'title':'测试标题','cover_id':cover,'attach_product':True,'listing':{'external_id':'90071992547409999','owned_product':True}}
    seen=[]
    def handle(req):
        seen.append(req.url.path)
        if req.url.path.endswith('start_upload'):
            assert req.url.params['app_id']=='video-key' and req.url.params['access_token']=='video-token'
            return httpx.Response(200,json={'result':1,'upload_token':'up-token','endpoint':'uploader.gifshow.com'})
        if req.url.path.endswith('fragment'):
            assert req.content==b'video'*100 and req.url.params['fragment_id']=='0'
        if req.url.path.endswith('complete'):assert req.url.params['fragment_count']=='1'
        if req.url.path.endswith('publish'):
            assert b'name="merchant_product_id"\r\n\r\n90071992547409999' in req.content
            assert b'name="cover"' in req.content and b'video-token' not in req.content
            return httpx.Response(200,json={'result':1,'video_info':{'photo_id':'photo123','pending':True}})
        return httpx.Response(200,json={'result':1})
    result=KuaishouVideo(store('kuaishou'),httpx.MockTransport(handle)).publish(p,db.DATA/path)
    assert result['photo_id']=='photo123' and result['merchant_product_id']=='90071992547409999'
    assert seen==['/openapi/photo/start_upload','/api/upload/fragment','/api/upload/complete','/openapi/photo/publish']


def test_kuaishou_chunking_never_loads_full_video(owner):
    cover,_=make_media();vid,path=make_media('video',b'x'*(8*1024*1024+1),'video/mp4')
    fragments=[]
    def handle(req):
        if req.url.path.endswith('start_upload'):return httpx.Response(200,json={'result':1,'upload_token':'up','endpoint':'uploader.gifshow.com'})
        if req.url.path.endswith('fragment'):fragments.append(len(req.content))
        return httpx.Response(200,json={'result':1,'video_info':{'photo_id':'p'}})
    KuaishouVideo(store('kuaishou'),httpx.MockTransport(handle)).publish({'title':'a','cover_id':cover},db.DATA/path)
    assert fragments==[8*1024*1024,1]


@pytest.mark.parametrize('variant',['missing_creds','missing_cover','not_owned','not_digit'])
def test_kuaishou_preflight_blocks_missing_inputs(owner,variant):
    cover,_=make_media();s=store('kuaishou')
    p={'title':'a','cover_id':cover,'attach_product':True,'listing':{'external_id':'123','owned_product':True}}
    if variant=='missing_creds':s['credentials'].pop('video_access_token')
    if variant=='missing_cover':p['cover_id']=''
    if variant=='not_owned':p['listing']['owned_product']=False
    if variant=='not_digit':p['listing']['external_id']='wrong'
    with pytest.raises(Blocked):KuaishouVideo(s).validate(p)


def test_kuaishou_publish_timeout_is_uncertain_and_does_not_expose_token(owner):
    cover,_=make_media();_,path=make_media('video',b'x','video/mp4')
    def handle(req):
        if req.url.path.endswith('start_upload'):return httpx.Response(200,json={'result':1,'upload_token':'up','endpoint':'uploader.gifshow.com'})
        if req.url.path.endswith('publish'):raise httpx.ReadTimeout('video-token',request=req)
        return httpx.Response(200,json={'result':1})
    with pytest.raises(Uncertain) as exc:KuaishouVideo(store('kuaishou'),httpx.MockTransport(handle)).publish({'title':'a','cover_id':cover},db.DATA/path)
    assert 'video-token' not in str(exc.value)


@pytest.mark.parametrize('endpoint',['localhost','uploader.gifshow.com@localhost','127.0.0.1','evil.com','uploader.gifshow.com:8080'])
def test_kuaishou_rejects_unknown_upload_host(owner,endpoint):
    cover,_=make_media();_,path=make_media('video',b'x','video/mp4')
    api=KuaishouVideo(store('kuaishou'),httpx.MockTransport(lambda r:httpx.Response(200,json={'result':1,'upload_token':'up','endpoint':endpoint})))
    with pytest.raises(Blocked):api.publish({'title':'a','cover_id':cover},db.DATA/path)


def test_wechat_local_image_upload_field_and_per_store_cache(owner):
    mid,_=make_media();seen=[]
    def handle(req):
        seen.append(req)
        assert req.url.path=='/shop/ec/basics/img/upload'
        assert req.url.params['upload_type']=='0'
        assert b'name="media"' in req.content
        return httpx.Response(200,json={'errcode':0,'img_info':{'temp_img_url':'https://mmecimage.cn/uploaded'}})
    api=Commerce(store('wechat'),httpx.MockTransport(handle))
    original={'head_imgs':['https://pending.shopdock.invalid/'+mid]}
    assert materialize_request(api,original)['head_imgs']==['https://mmecimage.cn/uploaded']
    assert materialize_request(api,original)['head_imgs']==['https://mmecimage.cn/uploaded'] and len(seen)==1
    assert original['head_imgs'][0].endswith(mid)


def test_kuaishou_new_product_signed_endpoint_and_images(owner):
    mid,_=make_media()
    def handle(req):
        if req.url.path.endswith('image/upload'):
            assert b'name="imgBytes"' in req.content
            assert b'uploadType' in req.content
            return httpx.Response(200,json={'result':1,'data':{'imageUrl':'https://p1.ecukwai.com/1.jpg'}})
        assert req.url.path=='/open/item/new'
        body=parse_qs(req.content.decode())
        assert json.loads(body['param'][0])['title']=='测试商品'
        return httpx.Response(200,json={'result':1,'data':{'kwaiItemId':123}})
    api=Commerce(store('kuaishou'),httpx.MockTransport(handle))
    assert upload_image(api,mid)=='https://p1.ecukwai.com/1.jpg'
    assert api.mutate_product('product_create',{'title':'测试商品'})['data']['kwaiItemId']==123


def test_douyin_binary_image_protocol_no_public_server_required(owner):
    mid,_=make_media();seen=[]
    def handle(req):
        seen.append(req.url.path)
        path=req.url.path
        if path=='/open/materialToken':return httpx.Response(200,json={'code':10000,'data':{'auth_query':'https://upload.bytedance.com/address'}})
        if path=='/address':return httpx.Response(200,json={'Result':{'UploadAddress':{'UploadHosts':['upload.bytedance.com'],'StoreInfos':[{'StoreUri':'store/file.png','Auth':'auth'}]}}})
        if path=='/store/file.png':
            assert req.method=='PUT' and req.content==b'png' and req.headers['Authorization']=='auth'
            return httpx.Response(200,json={'success':0})
        if path=='/material/uploadImageSync':
            assert json.loads(req.content)['file_uri']=='store/file.png'
            return httpx.Response(200,json={'code':10000,'data':{'material_info':{'byte_url':'https://p3.ecombdimg.com/file'}}})
        raise AssertionError(path)
    assert upload_image(Commerce(store('douyin'),httpx.MockTransport(handle)),mid)=='https://p3.ecombdimg.com/file'
    assert len(seen)==4


def test_sku_map_uses_explicit_external_keys_not_position():
    p=product()
    assert capture_sku_map(store('kuaishou'),p,{'skuIdMapping':[{'relSkuId':external_number(p['id'],'BOX'),'kwaiSkuId':90071992547409999}]})=={'BOX':'90071992547409999'}
    assert capture_sku_map(store('wechat'),p,{'skus':[{'sku_id':1}]})=={}


def test_order_normalizer_retains_only_mapping_fields():
    raw={'order_id':'123','status':20,'update_time':50,'order_detail':{'product_infos':[{'product_id':123,'sku_id':456,'sku_cnt':2,'title':'a'}],'delivery_info':{'phone':'secret'}}}
    row=normalize_order('wechat',raw)
    assert row['inventory_state']=='sold' and row['items'][0]['product_id']=='123'
    assert row['remote_updated']==50 and 'secret' not in json.dumps(row)
    row=normalize_order('kuaishou',{'orderBaseInfo':{'oid':1,'status':888},'orderItemInfo':{'skuId':456,'itemId':123,'num':2}},status_map={'888':'sold'})
    assert row['inventory_state']=='sold'


def test_legacy_templates_cannot_copy_warehouse_stock_to_every_store():
    s=store('wechat');s['templates']={'product_create':{'skus':'{{product.skus}}'},'product_update':{'product_id':'{{listing.external_id}}','skus':'{{product.skus}}'}}
    p=product()
    assert product_payload(s,p,{},'product_create')['skus'][0]['stock']==0
    assert 'stock' not in product_payload(s,p,{'external_id':'123'},'product_update')['skus'][0]


def test_multi_store_create_saved_ids_are_isolated(owner,monkeypatch,commerce_setup):
    stores=[]
    for platform in ('douyin','kuaishou','wechat','tmall','jd','pdd'):
        st=store(platform)
        stores.append(owner.post('/api/stores',json={k:st[k] for k in ('name','platform','api_enabled','video_enabled','credentials','templates')}).json())
        commerce_setup(stores[-1])
    p=owner.post('/api/products',json={k:v for k,v in product().items() if k!='id'}).json()
    body={'kind':'product_create','product_ids':[p['id']],'store_ids':[s['id'] for s in stores]}
    pre=owner.post('/api/jobs/preflight',json=body).json()
    assert pre['ready']==6
    seen=[]
    def mutate(self,kind,payload):
        seen.append((self.platform,payload))
        return {'external_id':str(len(seen))}
    monkeypatch.setattr('app.commerce_browser.CommerceBrowser.mutate_product',mutate)
    owner.post('/api/jobs',json=body)
    worker=jobs.Worker()
    for _ in range(6):worker.process(worker.claim())
    assert len(seen)==6 and {p for p,_ in seen}=={'douyin','wechat','kuaishou','tmall','jd','pdd'}
    assert len(db.all_rows('SELECT * FROM listings'))==6
    assert len({r['external_id'] for r in db.all_rows('SELECT * FROM listings')})==6
    # Another click cannot create another remote item now that IDs are mapped.
    assert owner.post('/api/jobs/preflight',json=body).json()['ready']==0


def test_video_credentials_never_returned(owner):
    r=owner.post('/api/stores',json={'name':'ks','platform':'kuaishou','credentials':{'video_app_id':'video-secret-id','video_access_token':'video-secret-token'}})
    assert r.status_code==200
    assert 'video-secret' not in r.text and 'video-secret' not in owner.get('/api/state').text
