"""Professional Linux queue consumers; no DPAPI or desktop service required."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import signal
import sqlite3
from threading import Event
from uuid import UUID

from cloud.ai_gateway import Gateway, GatewayConfig, _env_int
from cloud.ecs_image_provider import ImageProvider, ImageRequestUncertain
from cloud.professional_image_client import ProfessionalImageClient
from cloud.professional_intake import IntakeTaskError, ProfessionalIntakeWorker
from cloud.professional_review_worker import ReviewBroker, TRACKS, review_one
from cloud.windows_image_worker import LeaseHeartbeat, LocalState, SupabaseAuthConfig, SupabaseTokenProvider, WorkerError

LOG = logging.getLogger('guanye.ecs')
ZONE = timezone(timedelta(hours=8))


class ECSImageClient(ProfessionalImageClient):
    get_job = ProfessionalImageClient.worker_job


class LimitReached(WorkerError):
    pass


class DailyLimits:
    """Reserve from a shared, persistent ledger before starting countable work."""

    def __init__(self, database, lane, limit, *, now=lambda: datetime.now(ZONE)):
        self.database, self.lane, self.limit, self.now = database, lane, limit, now
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS quota(day text, lane text, used integer NOT NULL, PRIMARY KEY(day,lane))')
            db.execute('CREATE TABLE IF NOT EXISTS image_request(page_id text PRIMARY KEY, state text NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.database, timeout=15)

    def remaining(self):
        with closing(self.connect()) as db:
            row = db.execute('SELECT used FROM quota WHERE day=? AND lane=?',
                             (self.now().date().isoformat(), self.lane)).fetchone()
        return max(0, self.limit - (row[0] if row else 0))

    def available(self):
        return self.remaining() > 0

    def reserve(self, *, page_id=None):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if page_id is not None:
                if db.execute('SELECT 1 FROM image_request WHERE page_id=?', (page_id,)).fetchone():
                    raise ImageRequestUncertain('上次渠道受理状态待核对，已阻止重复扣费')
            day = self.now().date().isoformat()
            row = db.execute('SELECT used FROM quota WHERE day=? AND lane=?', (day, self.lane)).fetchone()
            if row and row[0] >= self.limit:
                raise LimitReached('已达到今日处理上限', 429)
            db.execute('INSERT INTO quota VALUES (?,?,1) ON CONFLICT(day,lane) DO UPDATE SET used=used+1',
                       (day, self.lane))
            if page_id is not None:
                db.execute("INSERT INTO image_request VALUES (?,'sent')", (page_id,))

    def pending_requests(self):
        with closing(self.connect()) as db:
            return [row[0] for row in db.execute('SELECT page_id FROM image_request ORDER BY page_id')]

    def clear_request(self, page_id):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM image_request WHERE page_id=?', (page_id,))


class LimitedIntake(ProfessionalIntakeWorker):
    def __init__(self, *args, limits, **kwargs):
        super().__init__(*args, **kwargs)
        self.limits = limits

    def claim(self):
        return super().claim() if self.limits.available() else None

    def _import_task(self, task, raw):
        try:
            self.limits.reserve()
        except WorkerError:
            raise IntakeTaskError('已达到今日转换上限，请联系管理员') from None
        return super()._import_task(task, raw)


class LimitedModel:
    def __init__(self, model, limits):
        self.model, self.limits = model, limits

    def chat_json(self, messages):
        self.limits.reserve()
        return self.model.chat_json(messages)


class ImageQueue:
    def __init__(self, client, provider, limits, state):
        self.client, self.provider, self.limits, self.state = client, provider, limits, state

    def handle(self, page):
        page_id = page['pageId']
        with LeaseHeartbeat(self.client, page, 1800):
            artifact = self.state.load_spool(page_id)
            if artifact is None:
                reference = self.client.reference_image(page) if page['hasReferenceImage'] else None
                try:
                    artifact = self.provider.generate(page, reference, before_send=lambda: self.limits.reserve(page_id=page_id))
                except ImageRequestUncertain:
                    LOG.warning('image attempt needs reconciliation: %s', page_id)
                    self.client.fail(page, '渠道受理状态待核对')
                    return
                except LimitReached:
                    # Another process consumed the last reservation. No provider request was sent.
                    return
                except WorkerError:
                    self.client.fail(page, '生图请求未完成')
                    self.limits.clear_request(page_id)
                    return
                self.state.save_spool(page_id, *artifact)
            # Failure here leaves the durable image available to the next lease.
            self.client.complete(page, *artifact)
            self.state.clear_spool(page_id)
            self.limits.clear_request(page_id)

    def run(self, stop, once=False):
        pending = set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            while not stop.is_set():
                slots = min(2 - len(pending), max(0, self.limits.remaining() - len(pending)))
                if slots:
                    try:
                        pages = self.client.claim('guanye-ecs-image', slots, 1800)
                    except Exception:
                        LOG.warning('image queue unavailable')
                        pages = []
                    pending.update(pool.submit(self.handle, page) for page in pages)
                if not pending:
                    if once:
                        return
                    stop.wait(5)
                    continue
                done, pending = wait(pending, timeout=None if once else 5, return_when=FIRST_COMPLETED)
                for task in done:
                    if task.exception() is not None:
                        LOG.warning('image completion deferred to the queue lease')
                if once:
                    return


def required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise ValueError('missing ' + name)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('lane', choices=('intake', 'review', 'image'))
    parser.add_argument('--once', action='store_true')
    repairs=parser.add_mutually_exclusive_group()
    repairs.add_argument('--pending-image-requests', action='store_true')
    repairs.add_argument('--acknowledge-unbilled-page', type=UUID)
    parser.add_argument('--provider-confirmed-unbilled', action='store_true')
    args = parser.parse_args(argv)
    repair=args.pending_image_requests or args.acknowledge_unbilled_page
    if (repair and args.lane!='image' or repair and args.once
            or args.provider_confirmed_unbilled != bool(args.acknowledge_unbilled_page)):
        parser.error('image reconciliation requires the image lane and explicit provider confirmation before clearing')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    config = GatewayConfig.from_env(require_service_role=False)
    public = {'url': config.supabase_url, 'anonKey': config.supabase_anon_key, 'workspaceId': config.workspace_id}
    root = Path(os.environ.get('GUANYE_STATE_DIR', '/state')) / config.workspace_id
    root.mkdir(parents=True, exist_ok=True)
    defaults = {'intake': 30, 'review': 60, 'image': 20}
    limits = DailyLimits(root / 'limits.sqlite3', args.lane,
                        _env_int('GUANYE_DAILY_' + args.lane.upper(), defaults[args.lane], 1, 1000))
    if repair:
        spool=LocalState(root / 'image')
        if args.pending_image_requests:
            for page_id in limits.pending_requests():
                print(page_id, 'awaiting-upload' if spool.load_spool(page_id) else 'needs-provider-reconciliation')
        else:
            page_id=str(args.acknowledge_unbilled_page)
            if spool.load_spool(page_id) or page_id not in limits.pending_requests():
                parser.error('request is absent or has an image awaiting upload')
            limits.clear_request(page_id)
        return 0
    auth = SupabaseAuthConfig(public['url'], public['anonKey'], required('GUANYE_WORKER_EMAIL'),
                              required('GUANYE_WORKER_PASSWORD'))
    tokens = SupabaseTokenProvider(auth)
    if args.lane == 'intake':
        worker = LimitedIntake(public, tokens=tokens, worker_name='guanye-ecs-intake', limits=limits,
                               log=LOG.info)
        return worker.run(stop, once=args.once)
    if args.lane == 'image':
        model_name=required('GUANYE_IMAGE_MODEL')
        if model_name!='gpt-image-2.6-sunburst':
            parser.error('本工作区只启用 gpt-image-2.6-sunburst')
        image = ImageProvider(required('GUANYE_IMAGE_API_BASE'), required('GUANYE_IMAGE_API_KEY'),
                              model_name, size='1536x864', response_format='b64_json',
                              result_origins=os.environ.get('GUANYE_IMAGE_RESULT_ORIGINS', '').split())
        client = ECSImageClient(public['url'], public['workspaceId'], public['anonKey'], tokens,
                                required('GUANYE_IMAGE_WORKER_TOKEN'))
        ImageQueue(client, image, limits, LocalState(root / 'image')).run(stop, args.once)
        return 0
    required('GUANYE_MODEL_API_KEY')
    broker = ReviewBroker(public, tokens, required('GUANYE_REVIEW_WORKER_TOKEN'))
    model = LimitedModel(Gateway(config), limits)
    while not stop.is_set():
        try:
            worked = False
            for kind in TRACKS:
                if not stop.is_set() and limits.available():
                    worked = review_one(broker, model, kind) or worked
            if args.once:
                return 0
            if not worked:
                stop.wait(5)
        except Exception:
            LOG.warning('review queue unavailable')
            if args.once:
                return 1
            stop.wait(15)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
