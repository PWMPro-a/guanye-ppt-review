"""Verify deployed Supabase permissions with disposable test users; clean up afterwards."""
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.request

HERE=Path(__file__).resolve().parent

def main():
    config=json.loads((HERE/'private-deployment.json').read_text(encoding='utf-8'))
    workspace=json.loads((HERE/'private-workspace.json').read_text(encoding='utf-8'))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897'}))
    def call(route,body=None,token=None,method=None,service=False):
        key=config['serviceKey'] if service else config['anonKey']
        data=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(config['url']+route,data=data,method=method or ('GET' if data is None else 'POST'),headers={'apikey':key,'Authorization':'Bearer '+(token or key),'Content-Type':'application/json','Prefer':'return=representation'})
        try:
            with opener.open(req,timeout=60) as response:
                raw=response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode()[:300]
            raise RuntimeError(f'{exc.code}: {detail}') from None
    users=[]
    deck_id='test-'+secrets.token_hex(8)
    path=config['workspaceId']+'/'+deck_id+'/image.png'
    try:
        for i in range(3):
            email='guanye-test-'+secrets.token_hex(8)+'@example.com'
            password=secrets.token_urlsafe(24)
            user=call('/auth/v1/admin/users',dict(email=email,password=password,email_confirm=True),service=True)
            users.append({'id':user['id']})
            auth=call('/auth/v1/token?grant_type=password',dict(email=email,password=password))
            users[-1]['token']=auth['access_token']
        a,b,c=users
        def rpc(user,name,**args):return call('/rest/v1/rpc/'+name,dict(w=config['workspaceId'],**args),token=user['token'])
        for user,name in [(a,'测试管理员'),(b,'测试评审')]:rpc(user,'join_workspace',display_name=name,invitation=workspace['invitation'])
        call('/rest/v1/members?workspace_id=eq.'+config['workspaceId']+'&user_id=eq.'+a['id'],{'role':'admin'},method='PATCH',service=True)
        rpc(a,'add_review_deck',deck={'id':deck_id,'name':'disposable test','title':'disposable test','images':[{'id':'test','path':path}]})
        first=rpc(a,'get_review_state');second=rpc(b,'get_review_state')
        first['reviews'][deck_id]={'scores':{dim['id']:4 for dim in first['dimensions']},'note':'test A'}
        second['reviews'][deck_id]={'scores':{dim['id']:2 for dim in second['dimensions']},'note':'test B'}
        rpc(a,'save_review_state',incoming=first);rpc(b,'save_review_state',incoming=second)
        assert rpc(a,'get_review_state')['reviews'][deck_id]['note']=='test A'
        assert rpc(b,'get_review_state')['reviews'][deck_id]['note']=='test B'
        try:rpc(a,'save_review_state',incoming=first)
        except RuntimeError as exc:assert '另一窗口' in str(exc)
        else:raise AssertionError('stale revision accepted')
        assert len(call('/rest/v1/reviews?deck_id=eq.'+deck_id,token=a['token']))==2
        assert call('/rest/v1/decks?select=id',token=c['token'])==[]
        try:
            changed=call('/rest/v1/reviews?deck_id=eq.'+deck_id+'&user_id=eq.'+a['id'],{'data':{'scores':{}}},token=b['token'],method='PATCH')
            assert changed==[]
        except RuntimeError as exc:assert '403:' in str(exc)
        private_deck=call('/rest/v1/decks?select=data&id=neq.'+deck_id+'&limit=1',service=True)
        if private_deck:
            image=private_deck[0]['data']['images'][0]['path']
            signed=call('/storage/v1/object/sign/ppt-library/'+image,{'expiresIn':60},token=b['token'])
            assert signed.get('signedURL')
            try:call('/storage/v1/object/sign/ppt-library/'+image,{'expiresIn':60},token=c['token'])
            except RuntimeError:pass
            else:raise AssertionError('outsider signed private image')
        print('PASS: live email login, invitations, independent scores, team reads, revision conflict, RLS and private image access')
    finally:
        # Remove only records created above, preserving all user data.
        call('/rest/v1/reviews?deck_id=eq.'+deck_id,method='DELETE',service=True)
        call('/rest/v1/decks?id=eq.'+deck_id,method='DELETE',service=True)
        for user in users:call('/auth/v1/admin/users/'+user['id'],method='DELETE',service=True)

if __name__=='__main__':main()
