"""Exercise member editing in an isolated live workspace, preserving the real team's data."""
import hashlib
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent


def main():
    config = json.loads((HERE / 'private-deployment.json').read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7897'}))

    def call(route, body=None, token=None, method=None, service=False):
        key = config['serviceKey'] if service else config['anonKey']
        req = urllib.request.Request(
            config['url'] + route, data=None if body is None else json.dumps(body).encode(),
            method=method or ('GET' if body is None else 'POST'),
            headers={'apikey': key, 'Authorization': 'Bearer ' + (token or key),
                     'Content-Type': 'application/json', 'Prefer': 'return=representation'})
        try:
            with opener.open(req, timeout=45) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(str(exc.code) + ': ' + exc.read().decode()[:300]) from None

    w = str(uuid.uuid4())
    invitation = secrets.token_urlsafe(24)
    users = []
    deck_id = 'member-test-' + uuid.uuid4().hex
    try:
        call('/rest/v1/workspaces', {'id': w, 'name': 'Disposable permissions test',
             'owner_email': 'no-owner@example.test',
             'invite_hash': hashlib.sha256(invitation.encode()).hexdigest()}, service=True)
        for index in range(3):
            email = 'member-test-' + uuid.uuid4().hex + '@example.com'
            password = secrets.token_urlsafe(24)
            user = call('/auth/v1/admin/users', dict(email=email, password=password, email_confirm=True), service=True)
            users.append({'id': user['id']})
            users[-1]['token'] = call('/auth/v1/token?grant_type=password', dict(email=email, password=password))['access_token']

        def rpc(user, name, **args):
            return call('/rest/v1/rpc/' + name, dict(w=w, **args), token=user['token'])

        a, b, outsider = users
        for user in (a, b):
            rpc(user, 'join_workspace', display_name='Ordinary reviewer', invitation=invitation)
        roles = call('/rest/v1/members?select=role&workspace_id=eq.' + w, service=True)
        assert [row['role'] for row in roles] == ['reviewer', 'reviewer']
        deck = {'id': deck_id, 'title': 'Test', 'images': [{'id': 'first', 'path': w + '/test/first.png'}]}
        rpc(b, 'add_review_deck', deck=deck)
        old = rpc(a, 'get_review_state')
        old['reviews'][deck_id] = {'scores': {'visual': 4, 'layout': 5}, 'note': 'A personal note'}
        old['reviews'][deck_id].update(mode='page', pages={'first': {'scores': {'visual': 3, 'layout': 4}, 'note': 'Page note'}})
        rpc(a, 'save_review_state', incoming=old)
        stale = rpc(a, 'get_review_state')
        state = rpc(b, 'get_review_state')
        state['maxScore'] = 10
        state['dimensions'].append({'id': 'content', 'name': '内容'})
        state['reviews'][deck_id] = {'scores': {'visual': 6, 'layout': 8}, 'note': 'B personal note'}
        rpc(b, 'save_review_state', incoming=state)
        current = rpc(a, 'get_review_state')
        assert current['reviews'][deck_id]['scores'] == {'visual': 8, 'layout': 10}
        assert current['reviews'][deck_id]['note'] == 'A personal note'
        assert current['reviews'][deck_id]['pages']['first']['scores'] == {'visual': 6, 'layout': 8}
        assert current['reviews'][deck_id]['pages']['first']['note'] == 'Page note'
        assert current['dimensions'][-1]['id'] == 'content'
        try:
            rpc(a, 'save_review_state', incoming=stale)
        except RuntimeError as exc:
            assert '评分标准已更新' in str(exc)
        else:
            raise AssertionError('Stale settings were accepted')
        deck['images'].append({'id': 'second', 'path': w + '/test/second.png'})
        rpc(b, 'add_review_deck', deck=deck)
        try:
            rpc(outsider, 'add_review_deck', deck=deck)
        except RuntimeError as exc:
            assert '加入团队' in str(exc)
        else:
            raise AssertionError('Nonmember import was accepted')
        assert rpc(b, 'get_review_state')['reviews'][deck_id]['note'] == 'B personal note'
        print('PASS: live ordinary members edit shared scale/dimensions and import/append; individual reviews, rescaling and stale-write protection hold')
    finally:
        call('/rest/v1/reviews?workspace_id=eq.' + w, method='DELETE', service=True)
        call('/rest/v1/decks?workspace_id=eq.' + w, method='DELETE', service=True)
        call('/rest/v1/workspaces?id=eq.' + w, method='DELETE', service=True)
        for user in users:
            call('/auth/v1/admin/users/' + user['id'], method='DELETE', service=True)


if __name__ == '__main__':
    main()
