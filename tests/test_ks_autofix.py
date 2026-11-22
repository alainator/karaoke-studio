"""ks autofix: only the clear fixes. ⚠ lines heard 1-15 s late or beyond the 2.5 s early tolerance; three or more
neighbouring ⚠ lines off by the same amount move together; lines marked fine, unmarked lines, lines not heard and
small offsets are left alone."""
import importlib.machinery, importlib.util, os, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("ks_tool", os.path.join(HERE, "tools", "ks"))
_spec = importlib.util.spec_from_loader("ks_tool", _loader)
ks = importlib.util.module_from_spec(_spec)
_loader.exec_module(ks)


def L(n, weak=True, ok=False):
    return [{"weak": weak, "ok": ok} for _ in range(n)]


class AutofixPlanTests(unittest.TestCase):
    def test_late_line_moves(self):
        lines = L(5)
        self.assertEqual(ks.autofix_plan({"offsets": {"2": -3.2}}, lines), ("line", 2, -3.2))

    def test_small_or_tolerated_offsets_stay(self):
        lines = L(5)
        self.assertIsNone(ks.autofix_plan({"offsets": {"0": -0.6, "1": 2.0, "2": None}}, lines))

    def test_early_beyond_tolerance_moves(self):
        self.assertEqual(ks.autofix_plan({"offsets": {"1": 3.5}}, L(3)), ("line", 1, 3.5))

    def test_unmarked_or_fine_lines_are_left(self):
        lines = L(4); lines[1]["weak"] = False; lines[2]["ok"] = True
        self.assertIsNone(ks.autofix_plan({"offsets": {"1": -5.0, "2": -5.0}}, lines))

    def test_a_section_moves_together(self):
        offs = {"3": -9.7, "4": -9.6, "5": -9.8, "6": -9.7, "9": -2.0}
        self.assertEqual(ks.autofix_plan({"offsets": offs}, L(12)), ("section", 3, 6, -9.7))

    def test_far_single_lines_are_not_guessed(self):
        self.assertIsNone(ks.autofix_plan({"offsets": {"2": -40.0}}, L(5)))

    def test_latest_first(self):
        self.assertEqual(ks.autofix_plan({"offsets": {"1": -2.0, "4": -6.0, "7": 3.0}}, L(9)), ("line", 4, -6.0))


if __name__ == "__main__":
    unittest.main()
