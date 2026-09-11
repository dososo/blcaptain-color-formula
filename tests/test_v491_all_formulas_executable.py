"""v4.9.1 用户合同：媒体支持决定执行入口，研发沿革不向用户暴露。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blcaptain_color as bc  # noqa: E402
import combos  # noqa: E402
import suggest  # noqa: E402


CATALOG = ROOT / "references" / "recipes.json"
CLI = ROOT / "scripts" / "blcaptain_color.py"
HIDDEN_USER_FIELDS = {
    "status", "media_status", "catalog_status", "execution_tier",
    "explicit_selection_required", "auto_recommendable", "aesthetic_maturity",
}


class AllFormulasExecutableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = bc.load_catalog(CATALOG)

    def test_catalog_media_counts_are_derived_not_handwritten(self):
        recipes = self.catalog["recipes"]
        self.assertEqual(sum("photo" in item["media_types"] for item in recipes), 31)
        self.assertEqual(sum("video" in item["media_types"] for item in recipes), 31)

    def test_every_declared_media_is_selectable(self):
        for recipe in self.catalog["recipes"]:
            for media_type in recipe["media_types"]:
                with self.subTest(recipe=recipe["id"], media=media_type):
                    self.assertEqual(
                        bc.find_recipe(self.catalog, recipe["id"], media_type),
                        recipe,
                    )

    def test_smart_catalog_contains_every_supported_formula(self):
        for media_type, expected_count in (("photo", 31), ("video", 31)):
            with self.subTest(media=media_type):
                rows = combos.load_tagged_catalog(CATALOG, media_type=media_type)
                self.assertEqual(len(rows), expected_count)

    def test_list_styles_has_full_counts_and_no_lifecycle_tiers(self):
        for media_type, expected_count in (("photo", 31), ("video", 31)):
            with self.subTest(media=media_type):
                result = subprocess.run(
                    [sys.executable, str(CLI), "list-styles", "--catalog", str(CATALOG),
                     "--media-type", media_type],
                    cwd=ROOT, text=True, capture_output=True, check=True,
                )
                rows = json.loads(result.stdout)["styles"]
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(all(media_type in item["media_types"] for item in rows))
                self.assertTrue(all(not (HIDDEN_USER_FIELDS & set(item)) for item in rows))

    def test_plan_style_projection_hides_internal_lifecycle(self):
        for recipe_id, media_type in (("cream-soft", "photo"),
                                      ("japanese-airy", "video")):
            recipe = bc.recipe_for_media(
                bc.find_recipe(self.catalog, recipe_id, media_type), media_type)
            payload = bc.style_payload(recipe, media_type)
            self.assertFalse(HIDDEN_USER_FIELDS & set(payload))
            self.assertNotIn("待重新晋级", json.dumps(payload, ensure_ascii=False))

    def test_smart_recommendation_previews_a_bounded_ranked_subset(self):
        self.assertEqual(8, suggest.smart_preflight_budget(3))
        self.assertEqual(12, suggest.smart_preflight_budget(12))


if __name__ == "__main__":
    unittest.main()
