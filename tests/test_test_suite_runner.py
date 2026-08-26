from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts import run_test_suite


class TestSuiteRunnerTests(unittest.TestCase):
    def test_only_explicit_fast_allowlist_is_available(self) -> None:
        args = run_test_suite.parse_args([])
        self.assertFalse(hasattr(args, "suite"))
        self.assertGreater(len(run_test_suite.FAST_INVOCATIONS), 1)
        self.assertTrue(
            all(len(invocation.unittest_args) == 1 for invocation in run_test_suite.FAST_INVOCATIONS)
        )
        self.assertTrue(
            all("discover" not in invocation.unittest_args for invocation in run_test_suite.FAST_INVOCATIONS)
        )

    def test_fast_command_uses_one_explicit_module_without_discovery(self) -> None:
        invocation = run_test_suite.FAST_INVOCATIONS[0]
        command = run_test_suite._unittest_command(invocation)

        self.assertEqual(
            command,
            [
                run_test_suite.sys.executable,
                "-m",
                "unittest",
                "-q",
                *invocation.unittest_args,
            ],
        )
        self.assertNotIn("discover", command)

    def test_failure_output_is_bounded_to_a_short_tail(self) -> None:
        output = "\n".join(f"line-{index}" for index in range(30))

        bounded = run_test_suite._bounded_child_output(output, max_lines=4, max_chars=100)

        self.assertTrue(bounded.startswith("[仅保留失败输出末尾]"))
        self.assertNotIn("line-0", bounded)
        self.assertIn("line-29", bounded)
        self.assertLessEqual(len(bounded), 120)

    @mock.patch.object(run_test_suite, "_terminate_process_tree")
    @mock.patch.object(run_test_suite.subprocess, "Popen")
    def test_timeout_terminates_child_tree_and_returns_bounded_failure(
        self,
        popen: mock.Mock,
        terminate: mock.Mock,
    ) -> None:
        process = popen.return_value
        process.communicate.side_effect = subprocess.TimeoutExpired(cmd=["python"], timeout=3)
        process.pid = 123

        result = run_test_suite._run_child(run_test_suite.FAST_INVOCATIONS[0], 3)

        self.assertEqual(result, 124)
        terminate.assert_called_once_with(process)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], [run_test_suite.sys.executable, "-m", "unittest"])
        self.assertEqual(command[3], "-q")
        self.assertEqual(command[-1], run_test_suite.FAST_TEST_MODULES[0])
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)

    @mock.patch.object(run_test_suite, "_run_child", return_value=0)
    def test_main_runs_plan_one_child_at_a_time(self, run_child: mock.Mock) -> None:
        result = run_test_suite.main(["--timeout-seconds", "12"])

        self.assertEqual(result, 0)
        self.assertEqual(run_child.call_count, len(run_test_suite.FAST_INVOCATIONS))
        self.assertEqual(
            [call.args[0] for call in run_child.call_args_list],
            list(run_test_suite.FAST_INVOCATIONS),
        )
        self.assertTrue(all(call.args[1] == 12 for call in run_child.call_args_list))

    @mock.patch.object(run_test_suite, "_run_child", side_effect=(7, 0, 0))
    def test_main_stops_after_first_failed_child(self, run_child: mock.Mock) -> None:
        result = run_test_suite.main(["--timeout-seconds", "12"])

        self.assertEqual(result, 1)
        run_child.assert_called_once_with(run_test_suite.FAST_INVOCATIONS[0], 12)


if __name__ == "__main__":
    unittest.main()
