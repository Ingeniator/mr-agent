import unittest

from diff import Fresh, parse_diff

MODIFIED = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,4 +1,4 @@
 one
-two
+TWO
 three
-
+four
"""

ADDED_AND_DELETED = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+fresh
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-keep
"""


class ParseDiffTest(unittest.TestCase):
    def test_numbers_new_side_lines(self) -> None:
        annotated, files = parse_diff(MODIFIED)
        self.assertIn("    1   one", annotated)
        self.assertIn("      - two", annotated)
        self.assertIn("    2 + TWO", annotated)
        self.assertIn("    3   three", annotated)
        self.assertIn("    4 + four", annotated)
        # Новый номер → старый номер; у добавленной строки старого нет.
        self.assertEqual(files["a.py"].lines, {1: 1, 2: None, 3: 3, 4: None})
        self.assertEqual(files["a.py"].added(), {2, 4})

    def test_removed_lines_by_old_number(self) -> None:
        _, files = parse_diff(MODIFIED)
        self.assertEqual(files["a.py"].removed, {2: "two", 4: ""})

    def test_old_line_between_and_after_hunks(self) -> None:
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -2,3 +2,4 @@
 two
+new
 three
 four
@@ -10,4 +11,3 @@
 ten
-eleven
 twelve
 thirteen
"""
        file = parse_diff(diff)[1]["a.py"]
        self.assertEqual(file.old_line(1), 1)  # до первого ханка
        self.assertIsNone(file.old_line(3))
        self.assertEqual(file.old_line(8), 7)  # между ханками
        self.assertEqual(file.old_line(12), 12)
        self.assertEqual(file.old_line(20), 20)  # после последнего

    def test_old_line_after_an_empty_old_side(self) -> None:
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -0,0 +1,2 @@
+one
+two
"""
        file = parse_diff(diff)[1]["a.py"]
        self.assertIsNone(file.old_line(2))
        self.assertEqual(file.old_line(3), 1)

    def test_new_and_deleted_files(self) -> None:
        _, files = parse_diff(ADDED_AND_DELETED)
        self.assertEqual(files["new.py"].old_path, "new.py")
        # Удалённый файл — по старому пути, не по /dev/null.
        self.assertEqual(files["gone.py"].new_path, "/dev/null")
        self.assertEqual(files["gone.py"].removed, {1: "keep"})

    def test_path_with_space(self) -> None:
        diff = MODIFIED.replace("a/a.py", "a/my file.py\t", 1)
        diff = diff.replace("b/a.py\n@@", "b/my file.py\t\n@@")
        _, files = parse_diff(diff)
        self.assertIn("my file.py", files)

    def test_form_feed_in_a_line_keeps_numbering(self) -> None:
        # splitlines() разрезал бы строку по \x0c и сдвинул номера.
        _, files = parse_diff(MODIFIED.replace("+TWO", "+T\x0cWO"))
        self.assertEqual(files["a.py"].added(), {2, 4})

    def test_fresh_marks_and_filters(self) -> None:
        fresh = Fresh(added={("a.py", 4)}, removed={"a.py": {2}})
        annotated, files = parse_diff(MODIFIED + ADDED_AND_DELETED, fresh)
        self.assertIn("    4 * four", annotated)
        self.assertIn("    2 + TWO", annotated)
        self.assertIn("      ~ two", annotated)
        self.assertIn("      - \n", annotated)
        # Файлы без свежих изменений в промпт не попадают, но в files остаются.
        self.assertNotIn("new.py", annotated)
        self.assertIn("new.py", files)


if __name__ == "__main__":
    unittest.main()
