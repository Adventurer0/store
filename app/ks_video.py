"""Official Kuaishou video API; separate credentials from merchant OpenAPI."""
import httpx
from . import db
from .connectors import Blocked, Rejected, Uncertain
from .publishing import trusted_url
from .security import redact


class KuaishouVideo:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport
        creds = store.get('credentials', {})
        self.params = {'app_id': creds.get('video_app_id', ''), 'access_token': creds.get('video_access_token', '')}
        if not store.get('video_enabled'):
            raise Blocked('此店铺未启用视频发布')
        if not all(self.params.values()):
            raise Blocked('请配置快手内容开放平台 Video App ID 和 Video Access Token（需 user_video_publish 权限）')

    def validate(self, payload):
        if not payload.get('cover_id'):
            raise Blocked('快手官方视频接口必须选择本地封面图片')
        cover = db.one('SELECT * FROM media WHERE id=? AND kind=\'image\'', (payload['cover_id'],))
        if not cover or cover['mime'] not in ('image/jpeg', 'image/png') or cover['size'] >= 10 * 1024 * 1024 or not (db.DATA / cover['path']).is_file():
            raise Blocked('快手封面须为有效的 JPG / PNG，且小于 10MB')
        if payload.get('attach_product'):
            l = payload.get('listing', {})
            if not str(l.get('external_id', '')).isdigit() or not l.get('owned_product'):
                raise Blocked('快手挂商品需填写该店的数字商品 ID，并确认是该账号的自建商品')
        return cover

    def request(self, client, method, url, publishing=False, **kwargs):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or 'result' not in result:
                raise ValueError()
        except (httpx.HTTPError, ValueError):
            raise (Uncertain if publishing else Rejected)('快手发布结果未知，请核对后台，勿重复发布' if publishing else '快手视频上传未完成；请检查授权与上传网络') from None
        if str(result['result']) != '1':
            raise Rejected(redact('快手视频接口拒绝：' + str(result.get('error_msg', result['result'])), self.store['credentials']))
        return result

    def publish(self, payload, media_path):
        cover = self.validate(payload)
        path = db.DATA / cover['path']
        with httpx.Client(timeout=120, follow_redirects=False, transport=self.transport) as client:
            start = self.request(client, 'POST', 'https://open.kuaishou.com/openapi/photo/start_upload', params=self.params)
            token, endpoint = start.get('upload_token'), start.get('endpoint')
            if not token or not isinstance(endpoint, str):
                raise Rejected('快手发起上传未返回令牌 / 网关')
            host = trusted_url('https://' + endpoint, ('gifshow.com', 'kuaishou.com', 'ksapisrv.com'))
            count = 0
            with open(media_path, 'rb') as video:
                while chunk := video.read(8 * 1024 * 1024):
                    self.request(client, 'POST', host + '/api/upload/fragment', params={'upload_token': token, 'fragment_id': count}, content=chunk, headers={'Content-Type': 'video/mp4'})
                    count += 1
            if not count:
                raise Blocked('视频文件为空')
            self.request(client, 'POST', host + '/api/upload/complete', params={'upload_token': token, 'fragment_count': count})
            data = {'caption': payload['title']}
            if payload.get('attach_product'):
                data['merchant_product_id'] = str(payload['listing']['external_id'])
            with path.open('rb') as image:
                result = self.request(client, 'POST', 'https://open.kuaishou.com/openapi/photo/publish', publishing=True,
                                      params={**self.params, 'upload_token': token}, data=data,
                                      files={'cover': (cover['name'], image, cover['mime'])})
            video = result.get('video_info') or {}
            if not video.get('photo_id'):
                raise Uncertain('快手已受理但缺少 photo_id，请核对后台，勿重发')
            return {'message': '快手已受理视频，商品 ID 已随发布请求提交；请回查作品和挂载结果',
                    'photo_id': str(video['photo_id']), 'merchant_product_id': data.get('merchant_product_id', ''),
                    'pending': video.get('pending', True)}
