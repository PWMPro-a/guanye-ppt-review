"""Bounded server-side adapter for a verified OpenAI-compatible image channel."""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from cloud.ai_gateway import DirectTransport
from cloud.windows_image_worker import WorkerError, _detect_image_type, image_dimensions

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 65536


def _https_url(url: str, *, query: bool = False):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query and not query):
        raise ValueError('生图渠道必须使用不含凭据的 HTTPS 地址')
    return parsed


def _origin(url: str, *, query: bool = False):
    parsed = _https_url(url, query=query)
    return parsed.hostname, parsed.port or 443


class ImageRequestUncertain(WorkerError):
    """The supplier may already have accepted or billed this request."""


class ImageProvider:
    def __init__(self, base, key, model, *, result_origins=(), size='1536x1024',
                 quality='medium', timeout=180, transport=None):
        _https_url(base)
        if not all(re.fullmatch(r'[\x21-\x7e]{1,512}', item) for item in (key, model)):
            raise ValueError('缺少有效的生图渠道密钥或模型标识')
        self.base, self.key, self.model = base.rstrip('/'), key, model
        self.result_origins = frozenset(_origin(url) for url in result_origins)
        self.size, self.quality, self.timeout = size, quality, timeout
        self.transport = transport or DirectTransport(MAX_JSON_BYTES)

    def generate(self, page, reference=None, *, before_send=lambda: None):
        prompt = page.get('prompt')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 16000:
            raise WorkerError('页面生图提示词无效', 422)
        expected = page.get('contentType')
        fmt = {'image/png': 'png', 'image/jpeg': 'jpeg', 'image/webp': 'webp'}.get(expected)
        if not fmt:
            raise WorkerError('页面成品格式不受支持', 422)
        fields = {'model': self.model, 'prompt': prompt.strip(), 'n': '1',
                  'size': self.size, 'quality': self.quality, 'output_format': fmt}
        path, mime = '/images/generations', 'application/json'
        if reference is not None:
            raw, ref_mime = reference
            if not 1 <= len(raw) <= MAX_REFERENCE_BYTES or _detect_image_type(raw) != ref_mime:
                raise WorkerError('参考图超限或格式不符', 422)
            path = '/images/edits'
            boundary = 'guanye-' + uuid4().hex
            mime = 'multipart/form-data; boundary=' + boundary
            parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      f'{value}\r\n').encode('utf-8') for name, value in fields.items()]
            ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}[ref_mime]
            parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                           f'filename="reference.{ext}"\r\nContent-Type: {ref_mime}\r\n\r\n').encode('ascii'),
                          raw, f'\r\n--{boundary}--\r\n'.encode('ascii')])
            body = b''.join(parts)
        else:
            body = json.dumps({**fields, 'n': 1}, ensure_ascii=False).encode('utf-8')
        before_send()
        try:
            reply = self.transport('POST', self.base + path,
                                   {'Authorization': 'Bearer ' + self.key, 'Content-Type': mime,
                                    'Accept': 'application/json'}, body, self.timeout)
        except Exception:
            raise ImageRequestUncertain('渠道未确认受理状态，请核对后再重试') from None
        if not 200 <= reply.status < 300:
            if reply.status in (400, 401, 403, 404, 413, 415, 422, 429):
                raise WorkerError('生图渠道拒绝请求', reply.status)
            raise ImageRequestUncertain('渠道未确认受理状态，请核对后再重试')
        try:
            data = json.loads(reply.body)['data']
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError
            item = data[0]
            if isinstance(item.get('b64_json'), str):
                raw = base64.b64decode(item['b64_json'], validate=True)
            else:
                url = item['url']
                if not isinstance(url, str) or _origin(url, query=True) not in self.result_origins:
                    raise ValueError
                result = self.transport('GET', url, {'Accept': 'image/*'}, None, self.timeout)
                if result.status != 200:
                    raise ValueError
                raw = result.body
            if (not 1 <= len(raw) <= MAX_IMAGE_BYTES or _detect_image_type(raw) != expected
                    or None in image_dimensions(raw)):
                raise ValueError
            return raw, expected, *image_dimensions(raw)
        except Exception:
            raise ImageRequestUncertain('渠道已响应，但未能取得有效图片，请核对后再重试') from None
