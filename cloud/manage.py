"""Supabase Management API setup without browser automation.

Token: cloud/private-token.txt or SUPABASE_ACCESS_TOKEN. No secrets are printed.
Commands are incremental; a project is created only when explicitly requested.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
PRIVATE=HERE/'private-deployment.json'
SITE='https://pwmpro-a.github.io/guanye-ppt-review/'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['inventory','create','status','configure','upload','pages'])
    args=parser.parse_args()
    token=os.environ.get('SUPABASE_ACCESS_TOKEN')
    if not token:
        path=HERE/'private-token.txt'
        if not path.exists():
            raise SystemExit('Missing cloud/private-token.txt. Save the temporary Supabase access token there.')
        token=path.read_text(encoding='utf-8-sig').strip()
    if not token.startswith('sbp_'):
        raise SystemExit('Expected a Supabase Management API personal access token')
    proxy=os.environ.get('HTTPS_PROXY','http://127.0.0.1:7897')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':proxy}))
    def api(path,body=None,method=None):
        payload=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        req=urllib.request.Request('https://api.supabase.com'+path,data=payload,method=method or ('GET' if body is None else 'POST'),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=60) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Supabase API {exc.code}: {exc.read().decode()[:500]}') from None
    config=json.loads(PRIVATE.read_text(encoding='utf-8')) if PRIVATE.exists() else {}
    def checkpoint():PRIVATE.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.command=='inventory':
        profile=api('/v1/profile')
        print(json.dumps({'profile':{k:profile.get(k) for k in ('id','primary_email')},'organizations':api('/v1/organizations'),'projects':[{k:p.get(k) for k in ('id','name','status','organization_id','region')} for p in api('/v1/projects')]},ensure_ascii=False))
        return
    if args.command=='create':
        if config.get('ref'):print('A project is already recorded; use status/configure.');return
        organizations=api('/v1/organizations')
        organization=next((o for o in organizations if o['name']=='观页评审'),None)
        if organization is None:
            organization=api('/v1/organizations',{'name':'观页评审'})
        slug=organization.get('slug') or organization['id']
        config['organization']=slug;checkpoint()
        existing=[p for p in api('/v1/projects') if p['name']=='guanye-ppt-review' and (p.get('organization_slug')==slug or p.get('organization_id')==organization.get('id'))]
        if existing:project=existing[0]
        else:
            config.setdefault('databasePassword',secrets.token_urlsafe(30));checkpoint()
            project=api('/v1/projects',{'name':'guanye-ppt-review','organization_slug':slug,'db_pass':config['databasePassword'],'region_selection':{'type':'smartGroup','code':'apac'},'auth_site_url':SITE})
        config['ref']=project['id'];config['url']='https://'+project['id']+'.supabase.co';checkpoint()
        print('Project recorded:',project['id'],project.get('status'));return
    if not config.get('ref'):raise SystemExit('Run create first')
    ref=config['ref']
    if args.command=='status':
        p=api('/v1/projects/'+ref);print(json.dumps({k:p.get(k) for k in ('id','name','status','region')},ensure_ascii=False));return
    if args.command=='configure':
        profile=api('/v1/profile')
        email=profile.get('primary_email') or profile.get('email')
        if not email:raise SystemExit('Owner email absent from Supabase profile; configure it explicitly before bootstrap')
        workspace=HERE/'private-workspace.json'
        if not workspace.exists():subprocess.run([sys.executable,str(HERE/'prepare_workspace.py'),'--owner-email',email],cwd=ROOT,check=True)
        config['workspaceId']=json.loads(workspace.read_text())['workspaceId'];checkpoint()
        if not config.get('schemaInstalled'):
            sql='begin;\n'+(HERE/'schema.sql').read_text(encoding='utf-8')+'\n'+(HERE/'bootstrap.sql').read_text(encoding='utf-8')+'\ncommit;'
            api('/v1/projects/'+ref+'/database/query',{'query':sql})
            config['schemaInstalled']=True;checkpoint()
        api('/v1/projects/'+ref+'/config/auth',{'site_url':SITE,'uri_allow_list':SITE,'password_min_length':8},method='PATCH')
        keys=api('/v1/projects/'+ref+'/api-keys?reveal=true')
        public=next((k for k in keys if k.get('type')=='publishable'),None) or next((k for k in keys if k.get('name')=='anon'),None)
        private=next((k for k in keys if k.get('name')=='service_role'),None) or next((k for k in keys if k.get('type')=='secret'),None)
        if not public or not private:raise SystemExit('Project keys are not available yet')
        config['anonKey']=public['api_key'];config['serviceKey']=private['api_key'];checkpoint()
        print('Schema, workspace, auth URLs and project credentials configured.');return
    if args.command=='upload':
        env={**os.environ,'SUPABASE_URL':config['url'],'SUPABASE_SERVICE_ROLE_KEY':config['serviceKey'],'WORKSPACE_ID':config['workspaceId'],'HTTPS_PROXY':proxy}
        subprocess.run([sys.executable,str(HERE/'publish_library.py')],cwd=ROOT,env=env,check=True);return
    if args.command=='pages':
        for key,value in {'SUPABASE_URL':config['url'],'SUPABASE_ANON_KEY':config['anonKey'],'WORKSPACE_ID':config['workspaceId']}.items():
            result=subprocess.run(['gh','variable','set',key,'--repo','PWMPro-a/guanye-ppt-review'],input=value,text=True,shell=os.name=='nt',capture_output=True)
            if result.returncode:raise RuntimeError('Could not set GitHub variable '+key+': '+result.stderr[:300])
        subprocess.run(['gh','workflow','run','pages.yml','--repo','PWMPro-a/guanye-ppt-review'],shell=os.name=='nt',check=True)
        print('Pages deployment dispatched:',SITE)

if __name__=='__main__':
    try:main()
    except (RuntimeError,KeyError,urllib.error.URLError) as exc:raise SystemExit(str(exc))
