"""Opt-in GPT Image channel for cover training. No retries or remote URL fetches."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from io import BytesIO
import json
import re
from uuid import UUID, uuid4

from PIL import Image

from cloud.ai_gateway import DirectTransport, GatewayError
from cloud.professional_worker_secrets import _load_payload, _save_payload
from cloud.professional_image_settings import image_fingerprint, cover_settings_path
from cloud.windows_image_worker import WorkerError

ORIGIN = 'https://img.937ai.chat'
ENDPOINT = ORIGIN + '/v1/images/edits'
# The provider requires desktop browser-compatible request headers on its API.
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}
MODELS = ('gpt-image-2.6-sunburst', 'gpt-image-2.5-sunburst')
MAX_IMAGE = 20 * 1024 * 1024
MAX_REFERENCE = 8 * 1024 * 1024
ALLOWED_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}


@dataclass(frozen=True)
class CoverImageChannel:
    key: str = field(repr=False)
    model: str = MODELS[0]
    transport: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.key, str) or not re.fullmatch(r'[\x21-\x7e]{16,512}', self.key) or self.model not in MODELS:
            raise ValueError('训练生图凭据或模型无效')

    def generate(self, page, reference):
        raw, mime = reference
        prompt = page.get('prompt')
        if mime not in ALLOWED_TYPES or not 1 <= len(raw) <= MAX_REFERENCE or not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000:
            raise WorkerError('训练参考图或页面提示词无效')
        # The original is the style reference for a NEW page, not a request to
        # patch the previous candidate. Do not use the provider's URL output.
        boundary = 'guanye-' + uuid4().hex
        separator = ('--' + boundary + '\r\n').encode('ascii')
        parts = []
        for key, value in (('model', self.model), ('prompt', prompt), ('n', '1'), ('size', '1536x1024'), ('output_format', 'png')):
            parts += [separator, f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode('ascii'), value.encode('utf-8'), b'\r\n']
        parts += [separator, f'Content-Disposition: form-data; name="image"; filename="reference.{ALLOWED_TYPES[mime]}"\r\nContent-Type: {mime}\r\n\r\n'.encode('ascii'), raw,
                  f'\r\n--{boundary}--\r\n'.encode('ascii')]
        transport = self.transport or DirectTransport((MAX_IMAGE * 4 // 3) + 65536)
        headers = {**BROWSER_HEADERS, 'Authorization': 'Bearer ' + self.key,
                   'Content-Type': 'multipart/form-data; boundary=' + boundary}
        if page.get('pageId') is not None:
            try:
                headers['Idempotency-Key'] = str(UUID(page['pageId']))
            except (AttributeError, TypeError, ValueError):
                raise WorkerError('训练生图页面标识无效；未发送模型') from None
        try:
            result = transport('POST', ENDPOINT, headers, b''.join(parts), 180.0)
        except (OSError, GatewayError):
            raise WorkerError('训练生图渠道结果未知；已停止，不自动重发') from None
        if result.status in (401, 403):
            raise WorkerError(f'训练生图渠道拒绝访问（{result.status}）；未自动切换模型或重发')
        if result.status != 200:
            raise WorkerError(f'训练生图请求未取得结果（HTTP {result.status}）；不会自动重发')
        try:
            data = json.loads(result.body)['data']
            if not isinstance(data, list) or len(data) != 1:
                raise ValueError
            encoded = data[0]['b64_json']
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE + 2) // 3 * 4:
                raise ValueError
            image = base64.b64decode(encoded, validate=True)
            if not 1 <= len(image) <= MAX_IMAGE:
                raise ValueError
            with Image.open(BytesIO(image)) as opened:
                if opened.format != 'PNG' or min(opened.size) < 1 or max(opened.size) > 4096 or getattr(opened, 'is_animated', False):
                    raise ValueError
                width, height = opened.size
                opened.verify()
            with Image.open(BytesIO(image)) as opened:
                opened.load()
        except (AttributeError, KeyError, IndexError, OverflowError, TypeError, ValueError, OSError, SyntaxError, Image.DecompressionBombError):
            raise WorkerError('训练生图未返回可解码的单张 PNG；不会自动重发') from None
        return image, 'image/png', width, height


def _path(config):
    return cover_settings_path(config).with_suffix('.cover-channel.dpapi')


def _entropy(config):
    return image_fingerprint(config) + b'\0cover-channel-v1'


def save_channel(config, channel):
    _save_payload(_path(config), _entropy(config), {'key': channel.key, 'model': channel.model})


def load_channel(config):
    try:
        payload = _load_payload(_path(config), _entropy(config))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or set(payload) != {'key', 'model'}:
        raise ValueError('训练生图渠道配置无效')
    return CoverImageChannel(**payload)
