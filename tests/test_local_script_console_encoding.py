"""Regression tests for Windows cp1252 console compatibility."""
import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LocalScriptConsoleEncodingTests(unittest.TestCase):
    def test_download_models_print_literals_are_cp1252_safe(self):
        source = (ROOT / "download_models.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        printed_literals = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "print":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    printed_literals.append(arg.value)

        self.assertTrue(printed_literals, "expected download_models.py to print status messages")
        for message in printed_literals:
            with self.subTest(message=message):
                message.encode("cp1252", errors="strict")


if __name__ == "__main__":
    unittest.main(verbosity=2)
