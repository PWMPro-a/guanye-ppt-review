import tempfile,unittest,zipfile
from pathlib import Path
from cloud.build_professional_installer import build
class InstallerBuildTests(unittest.TestCase):
 def test_payload_preserves_app_files(self):
  with tempfile.TemporaryDirectory(prefix="观页 setup ") as t:
   base=Path(t); app=base/'app'; (app/'_internal').mkdir(parents=True); (app/'guanye-professional.exe').write_bytes(b'exe'); (app/'config').mkdir(); (app/'config'/'runtime.json').write_text('{}',encoding='utf-8'); out=base/'out'
   result=build(app,out,stage_only=True)
   with zipfile.ZipFile(out/'payload.zip') as z: self.assertIn('app/guanye-professional.exe',z.namelist())
   self.assertEqual(result['stageOnly'],True)
 def test_refuses_overwrite(self):
  with tempfile.TemporaryDirectory() as t:
   base=Path(t); app=base/'app'; app.mkdir(); (app/'guanye-professional.exe').write_bytes(b'x'); out=base/'out'; out.mkdir()
   with self.assertRaises(ValueError): build(app,out,stage_only=True)
if __name__=='__main__': unittest.main()
