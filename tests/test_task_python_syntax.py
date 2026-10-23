"""Compile embedded task commands without importing GUI dependencies or running them."""

import json
from pathlib import Path
import unittest


TASKS = (
    Path(__file__).resolve().parents[1]
    / "src/win-arena-container/client/evaluation_examples_windows"
)


def python_commands(value, location="task"):
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, list) and command:
            executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
            if executable.startswith("python") and "-c" in command:
                yield location, command[command.index("-c") + 1]
        for key, child in value.items():
            yield from python_commands(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from python_commands(child, f"{location}[{index}]")


class TaskPythonSyntaxTests(unittest.TestCase):
    def test_selected_tasks_have_valid_python_commands(self):
        selection = json.loads((TASKS / "test_all.json").read_text(encoding="utf-8"))
        checked = 0
        for domain, task_ids in selection.items():
            for task_id in task_ids:
                path = TASKS / "examples" / domain / f"{task_id}.json"
                task = json.loads(path.read_text(encoding="utf-8"))
                for location, source in python_commands(task):
                    with self.subTest(task=task_id, location=location):
                        compile(source, f"{path.name}:{location}", "exec")
                    checked += 1
        self.assertGreater(checked, 0, "No embedded Python commands were checked")


if __name__ == "__main__":
    unittest.main()
