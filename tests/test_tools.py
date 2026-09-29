"""Offline tests for tools.py's student record helpers: student_id lookup
and its removal from the context returned to the model. Uses a temporary
student file; never reads real student data.
"""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402

STUDENTS = {
    "Casey": {
        "student_id": "student_123",
        "path": "Data Analytics Foundations",
        "current_project": "Project 1",
    },
    "Riley": {
        "path": "Data Analytics Foundations",
        "current_project": "Project 3",
    },
}


class StudentRecordTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        students_path = Path(self._tmp.name) / "students.json"
        students_path.write_text(json.dumps(STUDENTS), encoding="utf-8")
        patch = unittest.mock.patch.object(tools, "STUDENTS_PATH", students_path)
        patch.start()
        self.addCleanup(patch.stop)


class GetStudentIdTests(StudentRecordTestCase):
    def test_returns_explicit_student_id(self):
        self.assertEqual(tools.get_student_id("Casey"), "student_123")

    def test_matches_name_the_same_way_as_get_student_context(self):
        self.assertEqual(tools.get_student_id("  Casey \n"), "student_123")

    def test_unknown_student_returns_none(self):
        self.assertIsNone(tools.get_student_id("Nobody"))

    def test_record_without_student_id_returns_none(self):
        self.assertIsNone(tools.get_student_id("Riley"))

    def test_blank_or_non_string_student_id_returns_none(self):
        for value in ("", 42, None, ["student_1"]):
            with self.subTest(value=value):
                with unittest.mock.patch.object(
                    tools,
                    "load_students",
                    return_value={"Casey": {"student_id": value}},
                ):
                    self.assertIsNone(tools.get_student_id("Casey"))


class GetStudentContextTests(StudentRecordTestCase):
    def test_student_id_is_removed_from_model_context(self):
        context = tools.get_student_context("Casey")
        self.assertNotIn("student_id", context)
        expected = dict(STUDENTS["Casey"])
        del expected["student_id"]
        self.assertEqual(context, expected)

    def test_original_record_is_not_mutated(self):
        record = {"student_id": "student_123", "path": "P", "current_project": "1"}
        students = {"Casey": record}
        with unittest.mock.patch.object(
            tools, "load_students", return_value=students
        ):
            context = tools.get_student_context("Casey")
        self.assertIsNot(context, record)
        self.assertEqual(record["student_id"], "student_123")

    def test_record_without_student_id_is_returned_unchanged(self):
        self.assertEqual(tools.get_student_context("Riley"), STUDENTS["Riley"])

    def test_unknown_student_still_returns_none(self):
        self.assertIsNone(tools.get_student_context("Nobody"))

    def test_execute_tool_result_has_no_student_id(self):
        result = tools.execute_tool("get_student_context", {"name": "Casey"})
        self.assertNotIn("student_id", result)


class ExampleDataTests(unittest.TestCase):
    def test_public_example_student_has_student_id(self):
        path = tools.PROJECT_DIR / "data" / "students.example.json"
        students = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(students["Jordan"]["student_id"], "example_student_001")


if __name__ == "__main__":
    unittest.main()
