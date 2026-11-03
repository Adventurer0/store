"""Small, credential-free markers for an explicitly requested form preview."""
import json
import os
from pathlib import Path
import re


def paths(data_root, job_id, create=False):
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('无效的预览任务标识')
    root = Path(data_root).resolve()
    directory = root / 'commerce-previews'
    marker, stop = directory / (job_id + '.json'), directory / (job_id + '.stop')
    if directory.is_symlink() or marker.is_symlink() or stop.is_symlink():
        raise ValueError('预览标记不能是符号链接')
    if root not in directory.resolve().parents:
        raise ValueError('预览标记超出数据范围')
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return marker, stop


def write_marker(path, data):
    # Only fixed progress fields cross this boundary; never browser HTML/state.
    allowed = ('ready', 'active', 'status', 'filled', 'needs_manual', 'message',
               'submitted_by_app', 'started_at', 'closed_at')
    clean = {key: data[key] for key in allowed if key in data}
    temporary = path.with_suffix('.tmp')
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError('预览标记不能是符号链接')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(clean, handle, ensure_ascii=False)
    temporary.replace(path)


def read_marker(data_root, job_id, running, job_status=None, job_message=''):
    marker, _ = paths(data_root, job_id)
    if not marker.exists():
        result = {'ready': False, 'active': False, 'status': 'waiting' if running else 'closed',
                  'filled': [], 'needs_manual': [], 'submitted_by_app': False,
                  'message': '正在准备快手商品表单，请在浏览器窗口查看进度' if running else '尚无可用的填表预览'}
        if not running and job_message:
            result['message'] = job_message
    else:
        if marker.stat().st_size > 64 * 1024:
            raise ValueError('预览记录格式异常')
        result = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError('预览记录格式异常')
    if job_status:
        result['job_status'] = job_status
    if job_status in ('blocked', 'failed', 'uncertain', 'cancelled'):
        result.update(ready=False, active=False, status=job_status,
                      message=job_message or '填表预览未能继续，请查看任务执行信息')
    if not running:
        result['active'] = False
    return result


def request_close(data_root, job_id):
    _, stop = paths(data_root, job_id, create=True)
    fd = os.open(stop, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
