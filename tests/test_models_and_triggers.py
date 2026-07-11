import unittest

from engine.trigger_resolver import combo_text, modifier_names, normalize_input_name
from macro.actions import clone_action_tree, iter_action_tree


class TriggerResolverTests(unittest.TestCase):
    def test_mouse_side_button_aliases_share_one_canonical_name(self):
        for alias in ("鼠标侧键1", "XButton1", "mouse4", "BrowserBack"):
            self.assertEqual(normalize_input_name(alias), "鼠标侧键 1")
        for alias in ("鼠标侧键2", "XButton2", "mouse5", "BrowserForward"):
            self.assertEqual(normalize_input_name(alias), "鼠标侧键 2")

    def test_modifier_order_is_stable(self):
        self.assertEqual(modifier_names("Alt+Ctrl+Shift"), ["Ctrl", "Shift", "Alt"])
        self.assertEqual(combo_text("Alt+Ctrl", "F8"), "Ctrl+Alt+F8")


class ActionModelTests(unittest.TestCase):
    def test_tree_iteration_is_preorder(self):
        actions = [{
            "action_id": "root",
            "children": [{"action_id": "child", "children": []}],
        }]
        self.assertEqual(
            [item["action_id"] for item in iter_action_tree(actions)],
            ["root", "child"],
        )

    def test_clone_has_no_shared_child_containers(self):
        actions = [{
            "action_id": "root",
            "children": [{"action_id": "child", "children": []}],
        }]
        copied = clone_action_tree(actions)
        copied[0]["children"][0]["action_id"] = "changed"
        copied[0]["children"].append({"action_id": "new", "children": []})
        self.assertEqual(actions[0]["children"][0]["action_id"], "child")
        self.assertEqual(len(actions[0]["children"]), 1)


if __name__ == "__main__":
    unittest.main()
