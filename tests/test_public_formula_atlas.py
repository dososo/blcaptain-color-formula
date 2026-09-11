"""公开公式图谱必须完整，并且不泄露本机素材路径。"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicFormulaAtlasTests(unittest.TestCase):
    def test_catalog_declares_31_photo_and_31_video_formulas(self):
        recipes = json.loads((ROOT / "references/recipes.json").read_text(encoding="utf-8"))["recipes"]
        self.assertEqual(31, sum("photo" in recipe["media_types"] for recipe in recipes))
        self.assertEqual(31, sum("video" in recipe["media_types"] for recipe in recipes))

    def test_manifest_paths_exactly_match_declared_catalog_paths(self):
        recipes = json.loads((ROOT / "references/recipes.json").read_text(encoding="utf-8"))["recipes"]
        manifest = json.loads((ROOT / "showcase/formula-atlas/manifest.json").read_text(encoding="utf-8"))
        expected = {(recipe["id"], media) for recipe in recipes for media in recipe["media_types"]}
        actual = {(entry["id"], entry["media_type"]) for entry in manifest["entries"]}
        self.assertEqual(expected, actual)
        self.assertEqual(len(actual), len(manifest["entries"]))

    def test_checked_in_atlas_and_docs_cover_all_62_paths(self):
        manifest = json.loads((ROOT / "showcase/formula-atlas/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({"photo": 31, "video": 31}, manifest["counts"])
        self.assertEqual(62, len(manifest["entries"]))
        for language in ("FORMULAS.md", "FORMULAS.en.md"):
            text = (ROOT / language).read_text(encoding="utf-8")
            for entry in manifest["entries"]:
                self.assertIn(entry["image"], text)
                self.assertIn(entry["id"], text)
                self.assertTrue((ROOT / "showcase/formula-atlas" / entry["image"]).is_file())

    def test_public_atlas_contains_no_private_or_internal_paths(self):
        paths = [ROOT / "FORMULAS.md", ROOT / "FORMULAS.en.md",
                 ROOT / "showcase/formula-atlas/manifest.json"]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for forbidden in ("/Users/", "/var/folders/", "acceptance_ref", "comparison_path"):
                self.assertNotIn(forbidden, text, path.name)


if __name__ == "__main__":
    unittest.main()
