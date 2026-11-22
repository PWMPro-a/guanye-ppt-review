import json,tempfile,unittest
from pathlib import Path
from cloud.build_professional_package import build

class BuildTests(unittest.TestCase):
 def test_stage_only_copies_runtime_without_building_python(self):
  with tempfile.TemporaryDirectory(prefix="观页 打包 ") as t:
   root=Path(__file__).resolve().parent; base=Path(t); cfg=base/'input'; cfg.mkdir(); out=base/'out'
   (cfg/'professional.json').write_text(json.dumps({'url':'https://example.invalid','anonKey':'sb_publishable_fixture_api_key','workspaceId':'c2c14344-0bd0-4dc3-96f5-617ad0fdf424'}),encoding='utf-8')
   (cfg/'runtime.json').write_text(json.dumps({'port':8765,'hnPpt2':{'enabled':False},'workers':{'review':False,'image':False}}),encoding='utf-8')
   hn=base/'HN-PPT2.exe'; hn.write_bytes(b'fixture')
   result=build(root,cfg,out,hn,stage_only=True)
   self.assertTrue((out/'public'/'index.html').is_file())
   self.assertTrue((out/'hn-ppt2'/'HN-PPT2.exe').is_file())
   self.assertEqual(result['hasHnPpt2'],True)
 def test_refuses_overwrite(self):
  with tempfile.TemporaryDirectory(prefix="观页 打包 ") as t:
   base=Path(t); cfg=base/'input'; cfg.mkdir(); out=base/'out'; out.mkdir()
   with self.assertRaises(ValueError): build(Path(__file__).resolve().parent,cfg,out,None,stage_only=True)

if __name__=='__main__': unittest.main()
