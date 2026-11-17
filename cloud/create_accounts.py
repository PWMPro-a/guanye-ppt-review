"""Provision the initial administrator and two separate reviewer accounts.

Credentials are written only to an ignored local handoff file. Existing users are
reused, and their passwords are never reset by this script.
"""
import json
from pathlib import Path
import secrets
import urllib.request
import urllib.error

HERE=Path(__file__).resolve().parent
SITE='https://pwmpro-a.github.io/guanye-ppt-review/'

def main():
    config=json.loads((HERE/'private-deployment.json').read_text(encoding='utf-8'))
    workspace=json.loads((HERE/'private-workspace.json').read_text(encoding='utf-8'))
    credentials=HERE/'private-accounts.json'
    saved=json.loads(credentials.read_text(encoding='utf-8')) if credentials.exists() else []
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897'}))
    def call(path,body=None,method=None):
        req=urllib.request.Request(config['url']+path,data=None if body is None else json.dumps(body).encode(),method=method or ('GET' if body is None else 'POST'),headers={'apikey':config['serviceKey'],'Authorization':'Bearer '+config['serviceKey'],'Content-Type':'application/json','Prefer':'resolution=merge-duplicates'})
        try:
            with opener.open(req,timeout=60) as r:
                raw=r.read();return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:raise RuntimeError(f'Account request failed: {e.code} {e.read().decode()[:200]}') from None
    owner=call('/rest/v1/workspaces?id=eq.'+config['workspaceId']+'&select=owner_email')[0]['owner_email']
    existing=call('/auth/v1/admin/users?page=1&per_page=1000')['users']
    accounts=[(owner,'管理员','admin'),('reviewer1@guanye.local','评审一','reviewer'),('reviewer2@guanye.local','评审二','reviewer')]
    for email,name,role in accounts:
        user=next((u for u in existing if u['email'].lower()==email.lower()),None)
        record=next((r for r in saved if r['email']==email),None)
        if user is None:
            record=dict(email=email,password=secrets.token_urlsafe(15),name=name,role=role)
            user=call('/auth/v1/admin/users',{'email':email,'password':record['password'],'email_confirm':True,'user_metadata':{'display_name':name}})
            record['userId']=user['id'];saved.append(record)
            credentials.write_text(json.dumps(saved,ensure_ascii=False,indent=2),encoding='utf-8')
        call('/rest/v1/members?on_conflict=workspace_id,user_id',dict(workspace_id=config['workspaceId'],user_id=user['id'],name=name,role=role))
        print('Ready:',name,role)
    lines=['观页 · 团队网站登录信息','',SITE,'','请把不同评审账号分别给不同同事使用，避免两个人共用同一账号。','首次登录后，可在侧栏“修改登录密码”设置自己的密码。','']
    for r in saved:lines += [r['name']+'（'+r['role']+'）','邮箱：'+r['email'],'初始密码：'+r['password'],'']
    lines += ['团队邀请码：'+workspace['invitation'],'','也可注册个人邮箱账号，完成邮件确认后使用姓名和邀请码加入。','上述预建账号无需邮件确认。请保管本文件，不要整体群发。']
    (HERE/'private-login-info.txt').write_text('\n'.join(lines),encoding='utf-8')
    print('Login instructions written to cloud/private-login-info.txt; passwords were not printed.')

if __name__=='__main__':main()
