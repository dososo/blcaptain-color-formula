"""新版首屏样例须数量诚实、原源独立、引用可访问且不含本地路径。"""
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
GALLERY=ROOT/'showcase/真实样例'

class VerifiedGalleryTests(unittest.TestCase):
    def test_actual_counts_and_independent_sources(self):
        m=json.loads((GALLERY/'manifest.json').read_text())
        self.assertIn('不是全部',m['scope'])
        for media in ('photo','video'):
            rows=[e for e in m['entries'] if e['media_type']==media]
            self.assertEqual(len(rows),m['counts'][media])
            self.assertGreater(len(rows),0)
            self.assertEqual(len(rows),len({e['id'] for e in rows}))
            self.assertEqual(len(rows),len({e['source']['url'].split('?')[0] for e in rows}))
            self.assertTrue((GALLERY/f'{media}-contact-sheet.jpg').is_file())
        for e in m['entries']:
            self.assertTrue((GALLERY/e['image']).is_file())
            self.assertTrue(e['source']['license_url'].startswith('http'))

    def test_homepage_uses_new_sheets_and_public_data(self):
        for name in ('README.md','README.en.md'):
            text=(ROOT/name).read_text()
            for media in ('photo','video'):
                self.assertIn(f'showcase/真实样例/{media}-contact-sheet.jpg',text)
                self.assertNotIn(f'main/showcase/formula-atlas/{media}-contact-sheet.jpg',text)
        for name in ('manifest.json','README.md'):
            text=(GALLERY/name).read_text()
            for forbidden in ('/Users/','/var/folders/','receipt_path','plan_path'):
                self.assertNotIn(forbidden,text)

if __name__=='__main__': unittest.main()
