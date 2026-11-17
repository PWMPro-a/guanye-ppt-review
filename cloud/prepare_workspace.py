"""Generate a private workspace bootstrap. Does not contact external services."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import uuid

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--owner-email', required=True)
    args=parser.parse_args()
    if '@' not in args.owner_email:
        raise SystemExit('Use the owner email that will sign into the review site')
    root=Path(__file__).resolve().parent
    target=root/'bootstrap.sql'
    private=root/'private-workspace.json'
    if target.exists() or private.exists():
        raise SystemExit('Private workspace files already exist; reuse them rather than replacing invitations')
    workspace=str(uuid.uuid4())
    invitation=secrets.token_urlsafe(18)
    quote=lambda text:"'"+text.replace("'","''")+"'"
    sql='insert into public.workspaces(id,name,owner_email,invite_hash) values ('+','.join(map(quote,[workspace,'观页评审团队',args.owner_email.strip().lower(),hashlib.sha256(invitation.encode()).hexdigest()]))+');\n'
    target.write_text(sql,encoding='utf-8')
    private.write_text(json.dumps(dict(workspaceId=workspace,invitation=invitation),indent=2),encoding='utf-8')
    print('Created ignored cloud/bootstrap.sql and cloud/private-workspace.json. Keep invitation private.')

if __name__=='__main__':
    main()
