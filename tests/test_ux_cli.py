import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest import mock

import chapspan.cli as cli
from chapspan import handoff
from chapspan import __version__
from chapspan.library import import_txt, paragraph_ends
from chapspan.model import Scope
from chapspan.reading import normalize_group_budget
from chapspan.runs import RunRecorder, create_run, load_run, read_run_records, resolve_run
from chapspan.service import commit_state, deliver_output, prepare_output


class UxCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "样本书.txt"
        self.book = self.root / "书库"
        chapters = ["序言\n前文内容。\n"]
        for index in range(1, 9):
            shift = "星港" if index <= 4 else "星舰母舰"
            token = "青铜钥匙" if index in (2, 6) else "木门"
            chapters.append(
                f"第{index}章 标题{index}\n"
                f"主角来到{shift}大厅，观察墙上的{token}。\n"
                f"第{index}章第二段描述环境与角色神态变化，用于测试段落和提示。\n"
            )
            if index == 3:
                chapters.append("作者有话说\n今天聊聊设定。\n")
        self.source.write_text("\n".join(chapters), encoding="utf-8")
        self.snapshot = import_txt(self.book, self.source)["snapshot_id"]

    def tearDown(self):
        self.temp.cleanup()

    def span_process(self, *args, cwd=None, environ=None):
        pythonpath = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "utf-8"}
        if environ:
            env.update(environ)
        return subprocess.run(
            [sys.executable, "-B", "-m", "chapspan.cli", *[str(a) for a in args]],
            cwd=cwd or self.root,
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def test_help_topics_and_version(self):
        ver = self.span_process("--version")
        self.assertEqual(ver.returncode, 0, ver.stderr)
        self.assertIn(__version__, ver.stdout)

        root_help = self.span_process("--help")
        self.assertEqual(root_help.returncode, 0, root_help.stderr)
        self.assertIn("探索与取材", root_help.stdout)
        self.assertIn("内置帮助专题", root_help.stdout)

        topic_index = self.span_process("help")
        self.assertEqual(topic_index.returncode, 0, topic_index.stderr)
        for topic in ("quickstart", "method", "agent", "json", "run", "citations", "troubleshooting"):
            self.assertIn(topic, topic_index.stdout)
            res = self.span_process("help", topic)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(res.stdout.strip())

        bad_topic = self.span_process("help", "nonexistent")
        self.assertNotEqual(bad_topic.returncode, 0)
        self.assertIn("可选专题", bad_topic.stderr)

        resume_help = self.span_process("resume", "--help")
        self.assertEqual(resume_help.returncode, 0)
        self.assertIn("固定输出 JSON", resume_help.stdout)
        index_help = self.span_process("research", "index", "--help")
        self.assertEqual(index_help.returncode, 0)
        self.assertIn("--section 行号", index_help.stdout)

    def test_global_flags_accepted_after_subcommands_without_breaking_import_txt(self):
        toc_after = self.span_process("overview", "--toc", "--as-of", "4", "--text", self.source)
        self.assertEqual(toc_after.returncode, 0, toc_after.stderr)
        self.assertIn("第1章 标题1", toc_after.stdout)

        db_after = self.span_process(
            "overview", "--library", self.book, "--snapshot", self.snapshot, "--as-of", "4", "--limit", "2"
        )
        self.assertEqual(db_after.returncode, 0, db_after.stderr)
        self.assertEqual(len(json.loads(db_after.stdout)["chapters"]), 2)

        lib2 = self.root / "后置书库"
        imported = self.span_process("import-txt", self.source, "--encoding", "utf-8-sig", "--library", lib2)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(json.loads(imported.stdout)["created"])

        dangling = self.span_process("find", "星港", "--text")
        self.assertEqual(dangling.returncode, 2)
        self.assertIn("expected one argument", dangling.stderr)

    def test_run_task_dir_hint_and_collect_run_rejection(self):
        task_dir = self.root / "任务"
        init_res = json.loads(
            self.span_process("research", "init", task_dir, "--text", self.source, "--as-of", "5").stdout
        )
        actual_run = init_res["run_directory"]

        wrong_dir = self.span_process("--run", task_dir, "find", "星港")
        self.assertNotEqual(wrong_dir.returncode, 0)
        self.assertIn(actual_run, wrong_dir.stderr)

        bad_collect = self.span_process("--run", actual_run, "collect", "list", "线索")
        self.assertEqual(bad_collect.returncode, 1)
        self.assertIn("collect 属于书库操作，不支持 --run", bad_collect.stderr)

    def test_actionable_handoff_state_and_receipt_hints(self):
        states = self.root / "状态目录"
        missing_state = self.span_process("--text", self.source, "--state-dir", states, "read", "--from-result", "state_notfound")
        self.assertEqual(missing_state.returncode, 1)
        self.assertIn("找不到短状态柄", missing_state.stderr)
        self.assertNotIn("Errno 2", missing_state.stderr)

        cmp_out = self.root / "cmp.json"
        cmp_res = self.span_process(
            "--text", self.source, "--output", cmp_out,
            "compare", "--left", "1-4", "--right", "5-9", "--as-of", "all", "--min-support", "2",
        )
        self.assertEqual(cmp_res.returncode, 0, cmp_res.stderr)
        bare_cmp = self.span_process("--text", self.source, "read", "--from-result", cmp_out)
        self.assertEqual(bare_cmp.returncode, 1)
        self.assertIn("compare 结果", bare_cmp.stderr)
        self.assertIn("--pick 1..", bare_cmp.stderr)

        overlap_cmp = self.span_process(
            "--text", self.source, "compare", "--left", "1-5", "--right", "3-8", "--as-of", "all", "--min-support", "2"
        )
        self.assertEqual(overlap_cmp.returncode, 0, overlap_cmp.stderr)
        self.assertIn("有章节重叠", overlap_cmp.stdout)
        self.assertIn("过滤外围 1 章", overlap_cmp.stdout)

        ov_out = self.root / "ov.txt"
        ov_res = self.span_process(
            "--text", self.source, "--state-dir", states, "--output", ov_out,
            "overview", "--as-of", "all", "--limit", "3",
        )
        self.assertEqual(ov_res.returncode, 0, ov_res.stderr)
        self.assertIn("过滤外围：", ov_res.stdout)
        self.assertIn("后续目录：--after-chapter", ov_res.stdout)
        self.assertIn("下一步：", ov_res.stdout)
        self.assertIn("-m chapspan.cli --text", ov_res.stdout)

        multi_zero = self.span_process(
            "--text", self.source, "find", "--as-of", "6",
            "--query", '{"all":["星港"]}', "--query", '{"all":["青铜钥匙"]}',
        )
        self.assertEqual(multi_zero.returncode, 0, multi_zero.stderr)
        self.assertNotIn("加 --examples 1", multi_zero.stdout)
        self.assertIn("本页坐标：", multi_zero.stdout)
        self.assertNotIn("1=C", multi_zero.stdout)
        self.assertIn("回读首处：", multi_zero.stdout)

    def test_regressions_multiquery_state_continuation_budget_and_inspect_eof(self):
        states = self.root / "回归状态"
        multi = self.span_process(
            "--text", self.source, "--state-dir", states,
            "find", "--as-of", "6", "--examples", "1", "--budget", "4000",
            "--query", '{"all":["星港"]}', "--query", '{"all":["青铜钥匙"]}',
        )
        self.assertEqual(multi.returncode, 0, multi.stderr)
        self.assertNotIn("<状态柄或结果文件>", multi.stdout)
        handle = re.search(r"read --from-result (state_[0-9a-f]{24}) --example Q1E1", multi.stdout)
        self.assertIsNotNone(handle, multi.stdout)
        expanded = self.span_process(
            "--text", self.source, "--state-dir", states,
            "read", "--from-result", handle.group(1), "--example", "Q1E1", "--before", "1", "--after", "1",
        )
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertIn("星港大厅", expanded.stdout)

        # Continuation with larger --budget after find --examples must expand per-group cap cleanly.
        partial = self.span_process(
            "--text", self.source, "--state-dir", states,
            "find", "段落", "--as-of", "6", "--examples", "2", "--budget", "8", "--spread",
        )
        self.assertEqual(partial.returncode, 0, partial.stderr)
        partial_id = re.search(r"state_[0-9a-f]{24}", partial.stdout).group()
        continued = self.span_process(
            "--text", self.source, "--state-dir", states,
            "read", "--from-result", partial_id, "--budget", "4000", "--format", "json",
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        continued_data = json.loads(continued.stdout)
        self.assertTrue(continued_data["complete"])
        self.assertGreater(continued_data["read_characters"], 40)

        # Last chapter with no trailing newline keeps final body line in --inspect and suppresses global diagnostics.
        eof_txt = self.root / "无尾换行.txt"
        eof_txt.write_text("第一章 首\n开篇行。\n第二章 终\n末章最后一句。", encoding="utf-8")
        inspected = self.span_process("--text", eof_txt, "overview", "--as-of", "all", "--inspect", "C0002", "--format", "json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        insp_data = json.loads(inspected.stdout)
        self.assertNotIn("chapter_diagnostics", insp_data)
        self.assertEqual(insp_data["inspection"]["source_end_line"], 4)
        self.assertEqual(insp_data["inspection"]["source_lines"][-1]["text"], "末章最后一句。")

    def test_regressions_flag_hoisting_run_isolation_deferred_state_and_errors(self):
        states = self.root / "错检状态"
        # Subcommand option values colliding with global flags must not be hoisted.
        titled = self.span_process(
            "overview", "--as-of", "6", "--title=--format", "--text", self.source, "--format", "json",
        )
        self.assertEqual(titled.returncode, 0, titled.stderr)
        self.assertEqual(json.loads(titled.stdout)["chapters"], [])
        mis_opt = self.span_process(
            "overview", "--as-of", "6", "--title", "--format", "--text", self.source, "--format", "json",
        )
        self.assertNotEqual(mis_opt.returncode, 0)
        self.assertIn("--title", mis_opt.stderr)

        # Explicit --text inside a run dir / SPAN_RUN_DIR must not fail or touch its calls.jsonl.
        init = json.loads(self.span_process("research", "init", self.root / "运行任务", "--text", self.source, "--as-of", "3").stdout)
        run_dir = Path(init["run_directory"])
        other = self.root / "另一本书.txt"
        other.write_text("第一章 甲\n独立文本内容。\n", encoding="utf-8")
        standalone = self.span_process(
            "--text", other, "overview", "--as-of", "1", "--format", "json",
            cwd=run_dir, environ={"SPAN_RUN_DIR": str(run_dir)},
        )
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertEqual(json.loads(standalone.stdout)["chapters"][0]["title"], "第一章 甲")
        self.assertEqual((run_dir / "calls.jsonl").read_bytes(), b"")

        # Existing --output target must not pollute --state-dir or record a state_id.
        existing = self.root / "已有.txt"
        existing.write_text("保留", encoding="utf-8")
        out_fail = self.span_process(
            "--text", self.source, "--state-dir", states, "--output", existing,
            "read", "--chapter", "C0001", "--as-of", "1",
        )
        self.assertNotEqual(out_fail.returncode, 0)
        self.assertFalse(states.exists() and list(states.glob("state_*.json")))
        run_out_fail = self.span_process(
            "--run", run_dir, "--output", existing, "read", "--chapter", "C0001", "--budget", "4",
        )
        self.assertNotEqual(run_out_fail.returncode, 0)
        self.assertEqual(list((run_dir / "states").glob("state_*.json")), [])
        last_rec = json.loads((run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last_rec["failed_stage"], "output")
        self.assertIsNone(last_rec["state_id"])

        # Descriptive error checks + blank-only chapter + handoff include_peripheral inheritance.
        missing_db = self.span_process("--library", self.root / "空书库", "overview", "--snapshot", "snap_0", "--as-of", "1")
        self.assertNotEqual(missing_db.returncode, 0)
        self.assertIn("workbench.db", missing_db.stderr)

        bad_chap = self.span_process("--text", self.source, "overview", "--as-of", "6", "--inspect", "c0001")
        self.assertNotEqual(bad_chap.returncode, 0)
        self.assertIn("章节参数须为 C0001 或正整数", bad_chap.stderr)

        self.assertEqual(paragraph_ends("\n", "lines"), ())
        blank_txt = self.root / "空白章.txt"
        blank_txt.write_text("第一章 空\n\n第二章 有\n正文。\n", encoding="utf-8")
        blank_para = self.span_process("--text", blank_txt, "read", "--as-of", "2", "--paragraph", "C0001:1")
        self.assertNotEqual(blank_para.returncode, 0)
        self.assertIn("没有正文段落", blank_para.stderr)

        periph = json.loads(self.span_process(
            "--text", self.source, "--state-dir", states, "overview", "--as-of", "6", "--include-peripheral", "--format", "json"
        ).stdout)
        periph_file = self.root / "含外围.json"
        periph_file.write_text(json.dumps(periph, ensure_ascii=False), encoding="utf-8")
        narrowed = json.loads(self.span_process(
            "--text", self.source, "read", "--from-result", periph_file, "--pick", "5", "--as-of", "5", "--format", "json"
        ).stdout)
        self.assertTrue(narrowed["scope"]["include_peripheral"])

        from_three = json.loads(self.span_process(
            "--text", self.source, "overview", "--from-chapter", "3", "--as-of", "6", "--format", "json"
        ).stdout)
        from_three_file = self.root / "从三章起.json"
        from_three_file.write_text(json.dumps(from_three, ensure_ascii=False), encoding="utf-8")
        inverted = self.span_process(
            "--text", self.source, "read", "--from-result", from_three_file, "--pick", "1", "--as-of", "2"
        )
        self.assertNotEqual(inverted.returncode, 0)
        self.assertIn("收窄后的末章", inverted.stderr)

    def test_continuation_narrowed_scope_filters_delivery_refs_and_rejects_cut_resume(self):
        book = self.root / "收窄续读书.txt"
        book.write_text(
            "第一章 甲起\n"
            "第一段含甲词和长段内容，甲词用来测试跨章与多组筛选边界。\n"
            "第二段继续扩展甲词场景，保证预算受限时留下待续。\n"
            "作者的话\n"
            "外围SECRET内容，收窄后不应出现在续读结果。\n"
            "第二章 乙中\n"
            "乙中首段只有甲词但没有乙词，这里写足够长的句子以便续读两页以上。\n"
            "乙中第二段再写一行内容确保第三页仍然保留待续状态。\n"
            "第三章 丙止\n"
            "第三章第一段出现乙词，用来保持第二组仍在范围内，并留下长段预算测试续读。\n"
            "第三章第二段继续补足乙词上下文，让第三页仍可从状态文件继续。\n",
            encoding="utf-8",
        )
        states = self.root / "收窄状态"

        # 1) Include peripheral chapter first: C1, C2 peripheral, C4.
        p1_raw = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "C0001:0-5", "C0002:0-20", "C0004:0-50",
            "--as-of", "4", "--include-peripheral", "--group-budget", "15", "--budget", "40", "--format", "json",
        )
        self.assertEqual(p1_raw.returncode, 0, p1_raw.stderr)
        p1_file = self.root / "page1_periph.json"
        p1_file.write_text(p1_raw.stdout, encoding="utf-8")

        # Also prepare a non-peripheral multi-chapter session via --as-of 4 (C2 peripheral invisible), reading C1 + C3 + C4.
        p1_noperiph = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "C0001:0-5", "C0003:0-50", "C0004:0-50",
            "--as-of", "4", "--group-budget", "12", "--budget", "45", "--format", "json",
        )
        self.assertEqual(p1_noperiph.returncode, 0, p1_noperiph.stderr)
        p1_np_file = self.root / "page1_np.json"
        p1_np_file.write_text(p1_noperiph.stdout, encoding="utf-8")

        # Narrow to C3 only (--as-of 3 --from-chapter 3): drops both C1 and C4 from delivery & continuation.
        p2_txt = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", p1_np_file, "--from-chapter", "3", "--as-of", "3",
        )
        self.assertEqual(p2_txt.returncode, 0, p2_txt.stderr)
        self.assertNotIn("第一章", p2_txt.stdout)
        self.assertNotIn("第三章", p2_txt.stdout)
        self.assertIn("第二章", p2_txt.stdout)
        p2_state_id = re.search(r"state_[0-9a-f]{24}", p2_txt.stdout).group()
        saved_state = json.loads((states / f"{p2_state_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {int(r["target_span"][1:5]) for r in saved_state["continuation"]["delivery"]},
            {3},
        )
        # Page 3 from the narrowed state succeeds cleanly and stays in C3.
        p3_json = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", p2_state_id, "--budget", "200", "--format", "json",
        )
        self.assertEqual(p3_json.returncode, 0, p3_json.stderr)
        p3_data = json.loads(p3_json.stdout)
        self.assertEqual([g["group"] for g in p3_data["groups"]], [2])
        self.assertNotIn("第一章", json.dumps(p3_data, ensure_ascii=False))
        self.assertNotIn("第三章", json.dumps(p3_data, ensure_ascii=False))

        # Narrowing away all remaining groups fails cleanly (C0001 completed on page 1, C0003/C0004 > as_of=1).
        no_group_err = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", p1_np_file, "--as-of", "1",
        )
        self.assertNotEqual(no_group_err.returncode, 0)
        self.assertIn("超出当前授权或章节范围", no_group_err.stderr)

        # 2) Multi-query find with spread: Q1 (C1) filtered out by --from-chapter 3; Q2 (C4) keeps Q2/Q2E1 & --continue-group rejects Q1.
        mq = self.span_process(
            "--text", book, "--state-dir", states,
            "find", "--query", '{"all":["甲词"]}', "--query", '{"all":["乙词"]}',
            "--as-of", "4", "--examples", "1", "--spread", "--budget", "12", "--format", "json",
        )
        self.assertEqual(mq.returncode, 0, mq.stderr)
        mq_file = self.root / "mq_page1.json"
        mq_file.write_text(mq.stdout, encoding="utf-8")

        bad_cg = self.span_process(
            "--text", book, "read", "--from-result", mq_file, "--from-chapter", "3", "--continue-group", "Q1E1",
        )
        self.assertNotEqual(bad_cg.returncode, 0)
        self.assertIn("Q2E1", bad_cg.stderr)
        self.assertNotIn("Q1", bad_cg.stderr)

        mq_p2_txt = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", mq_file, "--from-chapter", "3", "--continue-group", "Q2E1", "--budget", "12",
        )
        self.assertEqual(mq_p2_txt.returncode, 0, mq_p2_txt.stderr)
        self.assertIn("Q2E1", mq_p2_txt.stdout)
        self.assertNotIn("Q1E1", mq_p2_txt.stdout)
        self.assertNotIn("外围SECRET", mq_p2_txt.stdout)
        mq_p2_id = re.search(r"state_[0-9a-f]{24}", mq_p2_txt.stdout).group()
        mq_p2_state = json.loads((states / f"{mq_p2_id}.json").read_text(encoding="utf-8"))
        self.assertEqual([r["reference"] for r in mq_p2_state["group_refs"]], ["Q2E1"])
        self.assertEqual([r["reference"] for r in mq_p2_state["example_refs"]], ["Q2E1"])

        mq_p3_json = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", mq_p2_id, "--budget", "500", "--format", "json",
        )
        self.assertEqual(mq_p3_json.returncode, 0, mq_p3_json.stderr)
        mq_p3_data = json.loads(mq_p3_json.stdout)
        self.assertEqual([g["group"] for g in mq_p3_data["groups"]], [2])
        self.assertEqual([r["reference"] for r in mq_p3_data["example_refs"]], ["Q2E1"])

        # 3) Single group spanning C1 and C4 where resume is in C1: --from-chapter 3 cuts off resume and must reject explicitly.
        cross_p1 = self.span_process(
            "--text", book, "--state-dir", states,
            "read", "C0001:0-25", "C0004:0-50", "--as-of", "4", "--budget", "8", "--format", "json",
        )
        self.assertEqual(cross_p1.returncode, 0, cross_p1.stderr)
        cross_file = self.root / "cross_p1.json"
        cross_file.write_text(cross_p1.stdout, encoding="utf-8")
        cut_resume = self.span_process(
            "--text", book, "read", "--from-result", cross_file, "--from-chapter", "3",
        )
        self.assertNotEqual(cut_resume.returncode, 0)
        self.assertIn("C0001:", cut_resume.stderr)
        self.assertIn("超出当前授权或章节范围", cut_resume.stderr)

        # 4) Verify peripheral exclusion drops peripheral chapter from inherited --include-peripheral result if excluded by --as-of / check p1_file as-of 1.
        periph_cut = self.span_process(
            "--text", book, "read", "--from-result", p1_file, "--as-of", "1", "--format", "json",
        )
        self.assertNotEqual(periph_cut.returncode, 0)
        self.assertIn("超出当前授权或章节范围", periph_cut.stderr)

    def test_failure_logging_stays_locked_and_surfaces_double_failure(self):
        init = json.loads(
            self.span_process("research", "init", self.root / "锁研究", "--text", self.source, "--as-of", "3").stdout
        )
        run_dir = Path(init["run_directory"])
        existing = self.root / "占用.txt"
        existing.write_text("保留", encoding="utf-8")

        # 1) Verify failure recorder.call runs inside run_write_lock and double-failure surfaces both errors once.
        lock_state = {"held": 0}
        record_events = []
        orig_lock = cli.run_write_lock
        orig_call = RunRecorder.call

        @contextmanager
        def tracking_lock(info):
            lock_state["held"] += 1
            try:
                with orig_lock(info):
                    yield
            finally:
                lock_state["held"] -= 1

        def failing_call(rec, args, *, status, scope, state_id, failed_stage=None, error=None):
            record_events.append((status, failed_stage, lock_state["held"]))
            self.assertGreater(lock_state["held"], 0)
            if status == "failure":
                raise OSError("磁盘已满")
            return orig_call(rec, args, status=status, scope=scope, state_id=state_id, failed_stage=failed_stage, error=error)

        stderr_buf = io.StringIO()
        with mock.patch.object(cli, "run_write_lock", tracking_lock), mock.patch.object(RunRecorder, "call", failing_call):
            with redirect_stderr(stderr_buf):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--run", str(run_dir), "--output", str(existing), "read", "--chapter", "C0001", "--budget", "8"])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(lock_state["held"], 0)
        self.assertEqual(len(record_events), 1)
        self.assertEqual(record_events[0], ("failure", "output", 1))
        err_text = stderr_buf.getvalue()
        self.assertIn("目标已存在", err_text)
        self.assertIn("占用.txt", err_text)
        self.assertIn("失败日志写入失败", err_text)
        self.assertIn("磁盘已满", err_text)

        # 2) Subprocess lock contention during a failing run preserves contiguous seq numbers.
        entered = self.root / "entered.flag"
        proceed = self.root / "proceed.flag"
        pythonpath = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "utf-8"}
        worker_code = (
            "import time, pathlib, chapspan.cli as c\n"
            "entered = pathlib.Path(r'" + str(entered) + "')\n"
            "proceed = pathlib.Path(r'" + str(proceed) + "')\n"
            "orig = c.deliver_output\n"
            "def paused(*a, **kw):\n"
            "    entered.write_text('1', encoding='utf-8')\n"
            "    deadline = time.monotonic() + 5.0\n"
            "    while not proceed.exists() and time.monotonic() < deadline:\n"
            "        time.sleep(0.02)\n"
            "    return orig(*a, **kw)\n"
            "c.deliver_output = paused\n"
            f"raise SystemExit(c.main(['--run', r'{run_dir}', '--output', r'{existing}', 'read', '--chapter', 'C0001', '--budget', '8']))\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", worker_code],
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            import time
            deadline = time.monotonic() + 5.0
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(entered.exists(), "worker did not acquire run lock in time")
            competing = self.span_process("--run", run_dir, "overview", "--format", "json")
            self.assertEqual(competing.returncode, 1)
            self.assertIn("运行正由另一个进程写入", competing.stderr)
            proceed.write_text("go", encoding="utf-8")
            _, p_err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("目标已存在", p_err)
        finally:
            if proc.poll() is None:
                proceed.write_text("go", encoding="utf-8")
                proc.kill()
                proc.wait()

        # Subsequent valid call obtains seq=2 cleanly and calls.jsonl has contiguous [1, 2].
        ok_call = self.span_process("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(ok_call.returncode, 0, ok_call.stderr)
        records = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual([row["sequence"] for row in records], [1, 2])
        self.assertEqual([row["status"] for row in records], ["failure", "success"])
        run_info = resolve_run(cli.build_parser().parse_args(["--run", str(run_dir), "overview"]))
        self.assertEqual(len(run_info.records()), 2)

    def test_group_budget_source_and_legacy_compatibility(self):
        book = self.root / "预算源.txt"
        book.write_text(
            "第一章 源一\n" + ("甲词" * 40) + "\n\n第二章 源二\n" + ("甲词" * 40) + "\n",
            encoding="utf-8",
        )
        states = self.root / "预算状态"

        # 1) Explicit --group-budget equal to --budget keeps fixed per-group ceiling on --budget increase.
        p1 = json.loads(self.span_process(
            "--text", book, "--state-dir", states,
            "read", "C0001:0-70", "C0002:0-70", "--as-of", "2", "--budget", "12", "--group-budget", "12", "--format", "json",
        ).stdout)
        self.assertEqual(p1["continuation"]["group_budget_source"], "explicit")
        p1_file = self.root / "explicit_eq.json"
        p1_file.write_text(json.dumps(p1, ensure_ascii=False), encoding="utf-8")

        p2 = json.loads(self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", p1_file, "--budget", "100", "--format", "json",
        ).stdout)
        self.assertEqual([g["read_characters"] for g in p2["groups"]], [12, 12])
        self.assertEqual(p2["continuation"]["group_budget"], 12)
        self.assertEqual(p2["continuation"]["group_budget_source"], "explicit")

        # 2) Legacy state without group_budget_source conservatively preserves group_budget even when group_budget == budget.
        legacy = json.loads(json.dumps(p1, ensure_ascii=False))
        legacy["continuation"].pop("group_budget_source")
        legacy_file = self.root / "legacy_cont.json"
        legacy_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        p2_legacy = json.loads(self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", legacy_file, "--budget", "100", "--format", "json",
        ).stdout)
        self.assertEqual([g["read_characters"] for g in p2_legacy["groups"]], [12, 12])
        self.assertEqual(p2_legacy["continuation"]["group_budget"], 12)

        # Explicit --group-budget raises the limit when continuing (including legacy states).
        p2_raised = json.loads(self.span_process(
            "--text", book, "--state-dir", states,
            "read", "--from-result", legacy_file, "--budget", "100", "--group-budget", "30", "--format", "json",
        ).stdout)
        self.assertEqual([g["read_characters"] for g in p2_raised["groups"]], [30, 30])
        self.assertEqual(p2_raised["continuation"]["group_budget_source"], "explicit")

        # 3) Shared-budget ungrouped continuation rejects switching to --group-budget.
        shared_p1 = self.root / "shared_p1.json"
        shared_p1.write_text(self.span_process(
            "--text", book, "read", "C0001:0-70", "--as-of", "2", "--budget", "10", "--format", "json",
        ).stdout, encoding="utf-8")
        shared_bad = self.span_process(
            "--text", book, "read", "--from-result", shared_p1, "--group-budget", "20",
        )
        self.assertNotEqual(shared_bad.returncode, 0)
        self.assertIn("共享预算", shared_bad.stderr)

    def test_compare_examples_preserves_explicit_group_budget_and_normalization(self):
        # 1) Unit checks for normalize_group_budget.
        self.assertEqual(normalize_group_budget(10), (10, "auto"))
        self.assertEqual(normalize_group_budget(10, None), (10, "auto"))
        self.assertEqual(normalize_group_budget(10, 100), (100, "explicit"))
        self.assertEqual(normalize_group_budget(150, 100), (100, "explicit"))
        with self.assertRaises(ValueError):
            normalize_group_budget(0, 100)
        with self.assertRaises(ValueError):
            normalize_group_budget(10, 0)

        # 2) compare --examples with --budget 10 --group-budget 100 preserves group_budget=100 for read --from-result.
        book = self.root / "比较预算.txt"
        book.write_text(
            "第一章 左\n" + ("甲乙。" * 60) + "\n\n第二章 右\n" + ("丙丁。" * 60) + "\n",
            encoding="utf-8",
        )
        cmp_res = json.loads(self.span_process(
            "--text", book, "compare", "--as-of", "2", "--left", "1", "--right", "2",
            "--min-len", "2", "--max-len", "2", "--examples", "1",
            "--budget", "10", "--group-budget", "100", "--format", "json",
        ).stdout)
        cont = cmp_res["reading"]["continuation"]
        self.assertEqual(cont["group_budget"], 100)
        self.assertEqual(cont["group_budget_source"], "explicit")
        self.assertLessEqual(cmp_res["reading"]["read_characters"], 10)

        cmp_file = self.root / "cmp_budget.json"
        cmp_file.write_text(json.dumps(cmp_res, ensure_ascii=False), encoding="utf-8")
        resumed = json.loads(self.span_process(
            "--text", book, "read", "--from-result", cmp_file, "--budget", "150", "--format", "json",
        ).stdout)
        self.assertGreater(resumed["read_characters"], 10)
        self.assertLessEqual(resumed["read_characters"], 150)
        self.assertTrue(all(group["budget"] <= 100 for group in resumed["groups"]))

        # Matches direct read --budget 10 --group-budget 100 inheritance behavior.
        direct_read = json.loads(self.span_process(
            "--text", book, "read", "--as-of", "2", "--chapter", "1",
            "--budget", "10", "--group-budget", "100", "--format", "json",
        ).stdout)
        self.assertEqual(direct_read["continuation"]["group_budget"], 100)
        self.assertEqual(direct_read["continuation"]["group_budget_source"], "explicit")
        direct_file = self.root / "direct_budget.json"
        direct_file.write_text(json.dumps(direct_read, ensure_ascii=False), encoding="utf-8")
        direct_resumed = json.loads(self.span_process(
            "--text", book, "read", "--from-result", direct_file, "--budget", "150", "--format", "json",
        ).stdout)
        self.assertGreater(direct_resumed["read_characters"], 10)
        self.assertEqual(direct_resumed["groups"][0]["budget"], 100)

    def test_deliver_output_atomic_overwrite_false_and_rollback(self):
        states = self.root / "交付状态"
        first = json.loads(self.span_process(
            "--text", self.source, "read", "C0002:0-40", "--as-of", "6", "--budget", "10", "--format", "json",
        ).stdout)
        prepared = prepare_output(first, is_text=True, output_path=self.root / "out.txt")

        # 1) Pre-existing output target raises FileExistsError with target path and leaves existing file intact.
        existing = self.root / "已有交付.txt"
        existing.write_text("重要内容", encoding="utf-8")
        with self.assertRaises(FileExistsError) as ctx:
            deliver_output(existing, first, prepared, is_text=True, state_dir=states)
        self.assertEqual(Path(ctx.exception.filename), existing)
        self.assertEqual(existing.read_text(encoding="utf-8"), "重要内容")
        self.assertFalse(states.exists() and list(states.glob("state_*.json")))

        # 2) Write failure inside atomic temp file leaves no target or temp files.
        failed_target = self.root / "失败输出.txt"
        orig_ntf = tempfile.NamedTemporaryFile

        def failing_ntf(*args, **kwargs):
            tmp = orig_ntf(*args, **kwargs)
            orig_write = tmp.write

            def boom(data):
                orig_write(data[:4])
                raise OSError("模拟磁盘写失败")

            tmp.write = boom
            return tmp

        with mock.patch("chapspan.service.tempfile.NamedTemporaryFile", failing_ntf):
            with self.assertRaises(OSError):
                deliver_output(failed_target, first, prepared, is_text=True, state_dir=states)
        self.assertFalse(failed_target.exists())
        self.assertEqual(list(self.root.glob("失败输出.txt.*")), [])

        # 3) Companion conflict rolls back exclusive output while preserving pre-existing companion and shared state.
        conflict_target = self.root / "伴生冲突.txt"
        companion = self.root / "伴生冲突.txt.state.json"
        companion_payload = '{"state_version":1,"scope":{"snapshot_id":"other","as_of":1,"first_chapter":1,"include_peripheral":false}}'
        companion.write_text(companion_payload, encoding="utf-8")
        with self.assertRaises(ValueError):
            deliver_output(conflict_target, first, prepared, is_text=True, state_dir=states)
        self.assertFalse(conflict_target.exists())
        self.assertEqual([p.name for p in states.glob("state_*.json")], [f"{prepared.state_id}.json"])
        self.assertEqual(companion.read_text(encoding="utf-8"), companion_payload)

        # 4) Interleaved writer B commits shared state before A commits; A's companion failure rolls back A's output without deleting B's state.
        interleaved_states = self.root / "并发共享状态"
        interleaved_target = self.root / "并发A.txt"
        interleaved_companion = self.root / "并发A.txt.state.json"
        interleaved_companion.write_text(companion_payload, encoding="utf-8")
        shared_state_path = interleaved_states / f"{prepared.state_id}.json"
        real_commit = commit_state

        def writer_b_then_a(prepared_arg, directory):
            real_commit(prepared_arg, directory)
            return real_commit(prepared_arg, directory)

        with mock.patch("chapspan.service.commit_state", side_effect=writer_b_then_a):
            with self.assertRaises(ValueError):
                deliver_output(interleaved_target, first, prepared, is_text=True, state_dir=interleaved_states)
        self.assertFalse(interleaved_target.exists())
        self.assertEqual(interleaved_companion.read_text(encoding="utf-8"), companion_payload)
        self.assertTrue(shared_state_path.exists())
        resumed = json.loads(self.span_process(
            "--text", self.source, "--state-dir", interleaved_states,
            "read", "--from-result", prepared.state_id, "--budget", "10", "--format", "json",
        ).stdout)
        self.assertGreater(resumed["read_characters"], 0)
        self.assertEqual(resumed["scope"]["snapshot_id"], self.snapshot)

        # 5) A companion validation failure leaves neither half of the output pair.
        new_comp_target = self.root / "新建伴生失败.txt"
        new_comp = self.root / "新建伴生失败.txt.state.json"
        with mock.patch("chapspan.handoff._verify_state_file", side_effect=[None, ValueError("伴生校验失败")]):
            with self.assertRaises(ValueError):
                deliver_output(new_comp_target, first, prepared, is_text=True, state_dir=states)
        self.assertFalse(new_comp_target.exists())
        self.assertFalse(new_comp.exists())
        self.assertTrue((states / f"{prepared.state_id}.json").exists())

    def test_companion_published_by_another_writer_survives_output_failure(self):
        output = self.root / "导出.txt"
        companion = output.with_name(output.name + ".state.json")
        states = self.root / "states"
        result = cli.run(cli.build_parser().parse_args(
            ["--library", str(self.book), "overview", "--snapshot", self.snapshot, "--as-of", "1"]
        ))
        prepared = prepare_output(result, is_text=True, output_path=output)
        other = handoff.prepare_state({**result, "scope": vars(Scope(self.snapshot, 2))})[1]

        def write_interleaved(path, raw):
            if path == companion:
                handoff.write_state_file(path, other)
            handoff.write_state_file(path, raw)

        with mock.patch("chapspan.service.write_state_file", side_effect=write_interleaved):
            with self.assertRaisesRegex(ValueError, "不同内容"):
                deliver_output(output, result, prepared, is_text=True, state_dir=states)
        self.assertFalse(output.exists())
        self.assertEqual(companion.read_bytes(), other)
        self.assertEqual((states / f"{prepared.state_id}.json").read_bytes(), prepared.state_raw)

    def test_read_run_records_validates_event_schemas(self):
        path = self.root / "日志校验.txt"
        path.write_text("第一章 首\n正文。\n第二章 次\n续文。\n", encoding="utf-8")
        book = self.root / "日志书库"
        sid = import_txt(book, path)["snapshot_id"]
        valid_scope = {"snapshot_id": sid, "as_of": 1, "first_chapter": 1, "include_peripheral": False}
        expanded_scope = {"snapshot_id": sid, "as_of": 2, "first_chapter": 1, "include_peripheral": False}

        def base(seq, event, **fields):
            return {
                "record_version": 1,
                "run_id": info.run_id,
                "sequence": seq,
                "time": "2026-11-15T00:00:00.000Z",
                "process_id": 123,
                "execution_id": "exec_1",
                "event": event,
                **fields,
            }

        info = create_run(self.root / "校验运行", source={"kind": "library", "path": str(book)},
                          snapshot_id=sid, initial_scope=Scope(sid, 1))
        log_path = info.root / "calls.jsonl"
        valid_rows = [
            base(1, "call", call_id="call_1", command="overview", origin="overview", status="success",
                 scope=valid_scope, result_path="results/1.json", state_id=None, failed_stage=None, error=None),
            base(2, "call", call_id="call_2", command="read", origin=None, status="failure",
                 scope=None, result_path=None, state_id=None, failed_stage="bind", error="绑定失败"),
            base(3, "call", call_id="call_3", command="read", origin="read", status="failure",
                 scope=valid_scope, result_path="results/3.json", state_id=None, failed_stage="output", error="输出存在"),
            base(4, "scope_expand", previous_scope=valid_scope, scope=expanded_scope),
        ]
        log_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in valid_rows) + "\n", encoding="utf-8")
        self.assertEqual(len(read_run_records(info)), 4)

        bad_cases = [
            ("缺少有效元信息", {"record_version": 1, "run_id": info.run_id, "sequence": 1, "event": "scope_expand"}),
            ("范围扩展字段无效", base(1, "scope_expand", previous_scope=valid_scope, scope=None)),
            ("成功记录字段无效", base(1, "call", call_id="c1", command="read", origin=None, status="success",
                                     scope=valid_scope, result_path=None, state_id=None, failed_stage=None, error=None)),
            ("失败记录字段无效", base(1, "call", call_id="c1", command="read", origin="read", status="failure",
                                     scope=None, result_path=None, state_id=None, failed_stage=None, error="err")),
            ("缺少调用来源", base(1, "call", call_id="c1", command="read", origin=None, status="failure",
                                 scope=None, result_path=None, state_id=None, failed_stage="execute", error="err")),
            ("缺少成功范围", base(1, "call", call_id="c1", command="read", origin="read", status="success",
                                 scope=None, result_path=None, state_id=None, failed_stage=None, error=None)),
        ]
        for expected_msg, bad in bad_cases:
            log_path.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, expected_msg):
                read_run_records(info)

    def test_library_as_of_all_overview_characters_and_chapter_range(self):
        path = self.root / "交互修整.txt"
        content = (
            "第一章 起点\n甲内容第一段。\n"
            "第二章 途中\n乙内容加长版本在这里。\n"
            "请假条\n今天有事休息一天。\n"
            "第三章 恢复\n丙内容继续推进。\n"
            "第四章 后续\n丁内容超出边界。\n"
        )
        path.write_text(content, encoding="utf-8")
        book = self.root / "修整书库"
        snap = import_txt(book, path)["snapshot_id"]

        ov_txt = self.span_process("--text", path, "overview", "--as-of", "all", "--format", "json")
        ov_lib = self.span_process("--library", book, "overview", "--snapshot", snap, "--as-of", "all", "--format", "json")
        self.assertEqual(ov_lib.returncode, 0, ov_lib.stderr)
        ov_txt_data, ov_lib_data = json.loads(ov_txt.stdout), json.loads(ov_lib.stdout)
        self.assertEqual(ov_txt_data["scope"]["as_of"], 5)
        self.assertEqual(ov_txt_data["chapters"], ov_lib_data["chapters"])
        self.assertIn("characters", ov_lib_data["chapters"][0])
        text_ov = self.span_process("--text", path, "overview", "--as-of", "all").stdout
        self.assertIn(" 字 · ", text_ov)

        bad_title_asof = self.span_process("--library", book, "overview", "--snapshot", snap, "--as-of", "第一章")
        self.assertNotEqual(bad_title_asof.returncode, 0)
        self.assertIn("C0012", bad_title_asof.stderr)

        def run_json(*cli_args):
            proc = self.span_process(*cli_args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        ranged = run_json("--library", book, "read", "--snapshot", snap, "--as-of", "C0004", "--chapter", "C0001-C0004")
        self.assertEqual([f["chapter_title"] for f in ranged["fragments"]], ["第一章 起点", "第二章 途中", "第三章 恢复"])
        ranged_grouped = run_json("--text", path, "read", "--as-of", "4", "--chapter", "1..4", "--group-budget", "200", "--format", "json")
        self.assertEqual(len(ranged_grouped["groups"]), 3)
        out_of_scope_range = self.span_process("--text", path, "read", "--as-of", "3", "--chapter", "C0001-C0005")
        self.assertNotEqual(out_of_scope_range.returncode, 0)
        self.assertIn("超出当前允许范围", out_of_scope_range.stderr)

    def test_diagnostic_pagination_resume_line_and_material_index_recovery(self):
        path = self.root / "诊断与恢复.txt"
        content = (
            "第一章 起点\n甲内容第一段。\n"
            "第二章 途中\n乙内容加长版本在这里。\n"
            "请假条\n今天有事休息一天。\n"
            "第三章 恢复\n丙内容继续推进。\n"
            "第四章 后续\n丁内容超出边界。\n"
        )
        path.write_text(content, encoding="utf-8")
        book = self.root / "恢复书库"
        snap = import_txt(book, path)["snapshot_id"]

        def run_json(*cli_args):
            proc = self.span_process(*cli_args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        diag_path = self.root / "诊断分页.txt"
        diag_path.write_text(
            "第一章 首\n正文\n" + "".join(f"第{n}章紧连\n段落\n" for n in range(2, 10)) + "第十章 尾\n末段\n",
            encoding="utf-8",
        )
        diag_text = self.span_process("--text", diag_path, "overview", "--as-of", "all").stdout
        self.assertIn("展示 2/9", diag_text)
        self.assertIn("续看 --diagnostic-offset 2", diag_text)
        self.assertIn("JSON 下一页 --diagnostic-offset 6", diag_text)

        narrow_run = Path(run_json(
            "--library", book, "research", "init", self.root / "收窄研究", "--snapshot", snap, "--as-of", "C0004", "--from-chapter", "2",
        )["run_directory"])
        draft = narrow_run / "当前稿.md"
        draft.write_text(
            f"---\nsnapshot_id: {snap}\nknowledge_horizon: 4\n---\n# 核对\n"
            "低于起点：[@C0001:0-3]\n外围过滤：[@C0003:0-3]\n超上限：[@C0005:0-3]\n",
            encoding="utf-8",
        )
        rejected = self.span_process("--run", narrow_run, "resume")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("第 8 行 [@C0005:0-3]", rejected.stderr)
        self.assertIn("上限 C0004", rejected.stderr)

        draft.write_text(
            f"---\nsnapshot_id: {snap}\nknowledge_horizon: 4\n---\n# 核对\n"
            "低于起点：[@C0001:0-3]\n外围过滤：[@C0003:0-3]\n合法：[@C0004:0-3]\n",
            encoding="utf-8",
        )
        resumed = run_json("--run", narrow_run, "resume")
        notes = {row["citation"]: row.get("note") for row in resumed["citations"]}
        self.assertIn("起始范围", notes["[@C0001:0-3]"])
        self.assertIn("--include-peripheral", notes["[@C0003:0-3]"])
        self.assertIsNone(notes["[@C0004:0-3]"])

        run_json("--run", narrow_run, "research", "index")
        index_md = (narrow_run / "材料索引.md").read_text(encoding="utf-8")
        self.assertNotIn("| C0001 | — | — | [@C0001:0-3]（1 次） | 无交付记录 1 | — | `read --chapter C0001` |", index_md)
        self.assertNotIn("| C0003 | — | — | [@C0003:0-3]（1 次） | 无交付记录 1 | — | `read --chapter C0003` |", index_md)
        self.assertIn("| C0004 | 第三章 恢复 |", index_md)

    def test_limit_bounds_documented_and_enforced_across_commands(self):
        for subcommand in (("find",), ("snapshots",), ("collect", "list")):
            help_proc = self.span_process(*subcommand, "--help")
            self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
            self.assertIn("1–100", help_proc.stdout)

        help_json = self.span_process("help", "json")
        self.assertEqual(help_json.returncode, 0, help_json.stderr)
        self.assertIn("citation_count", help_json.stdout)
        self.assertIn("skipped_inline_citations", help_json.stdout)

        bad_calls = [
            ("--text", self.source, "find", "星港", "--as-of", "4", "--limit", "0"),
            ("--text", self.source, "find", "星港", "--as-of", "4", "--limit", "101"),
            ("--text", self.source, "find", "--as-of", "4", "--query", '{"all":["星港"],"limit":101}'),
            ("--library", self.book, "snapshots", "--limit", "0"),
            ("--library", self.book, "snapshots", "--limit", "101"),
            ("--library", self.book, "collect", "list", "专题", "--snapshot", self.snapshot, "--as-of", "4", "--limit", "0"),
            ("--library", self.book, "collect", "list", "专题", "--snapshot", self.snapshot, "--as-of", "4", "--limit", "101"),
        ]
        for args in bad_calls:
            proc = self.span_process(*args)
            self.assertNotEqual(proc.returncode, 0, args)
            self.assertIn("1 到 100 之间的整数", proc.stderr)

    def test_resume_and_index_citation_counts_and_inline_skip_recovery(self):
        run_init = json.loads(
            self.span_process("--library", self.book, "research", "init", self.root / "引用反馈",
                              "--snapshot", self.snapshot, "--as-of", "3").stdout
        )
        run_dir = Path(run_init["run_directory"])
        draft = run_dir / "当前稿.md"

        def run_json(*args):
            proc = self.span_process(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        # 1. Valid draft without citations (ordinary inline code and fenced block must not trigger skip count).
        draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 3\n---\n"
            "# 空稿\n说明 `read [@C0009:0-2]` 与 `[@示例]`。\n\n```text\n[@C0009:0-2]\n```\n",
            encoding="utf-8",
        )
        empty_res = run_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(empty_res["citation_count"], 0)
        self.assertEqual(empty_res["skipped_inline_citations"], 0)
        self.assertEqual(empty_res["citations"], [])
        self.assertNotIn("note", empty_res)

        # 2. Pure-citation inline code only -> skipped count + recovery hint -> re-verify with flag.
        draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 3\n---\n"
            "# 仅行内\n甲 `[@C0001:0-2] [@C0001:0-2]`\n乙 `[@C0002:0-2]`\n"
            "普通 `read [@C0009:0-2]` 与围栏：\n```text\n[@C0009:0-2]\n```\n",
            encoding="utf-8",
        )
        inline_only = run_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(inline_only["citation_count"], 0)
        self.assertEqual(inline_only["skipped_inline_citations"], 3)
        self.assertEqual(inline_only["citations"], [])
        self.assertIn("3 处", inline_only["note"])
        self.assertIn("--include-inline-citations", inline_only["note"])

        # Receipt via --output carries the same counts and recovery hint.
        receipt_out = self.root / "校稿回执.json"
        receipt = run_json("--run", run_dir, "resume", "--citations-only", "--output", receipt_out)
        self.assertEqual(receipt["citations"], 0)
        self.assertEqual(receipt["skipped_inline_citations"], 3)
        self.assertIn("--include-inline-citations", receipt["citation_note"])

        reverified = run_json("--run", run_dir, "resume", "--citations-only", "--include-inline-citations")
        self.assertEqual(reverified["citation_count"], 2)
        self.assertEqual(reverified["skipped_inline_citations"], 0)
        self.assertNotIn("note", reverified)
        self.assertEqual([row["citation"] for row in reverified["citations"]], ["[@C0001:0-2]", "[@C0002:0-2]"])

        # 3. Mixed draft: bare prose + pure inline + ordinary inline + fenced, across resume & research index.
        draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 3\n---\n"
            "# 混合\n"
            "## 第一节\n裸引用 [@C0001:0-2] 再引 [@C0001:0-2]，行内 `[@C0002:0-2]`。\n"
            "## 第二节\n裸引用 [@C0002:0-2]，普通 `[@示例]`。\n```markdown\n[@C0009:0-2]\n```\n",
            encoding="utf-8",
        )
        mixed_resume = run_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(mixed_resume["citation_count"], 2)
        self.assertEqual(mixed_resume["skipped_inline_citations"], 1)
        self.assertIn("已检查 2 条去重引用", mixed_resume["note"])
        self.assertIn("跳过 1 处", mixed_resume["note"])

        idx_full = run_json("--run", run_dir, "research", "index")
        self.assertEqual(idx_full["citations"], 3)
        self.assertEqual(idx_full["skipped_inline_citations"], 1)
        self.assertIn("1 处", idx_full["note"])
        index_md = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("默认跳过行内纯引用坐标：1 处", index_md)

        # --section line 6 ("## 第一节") has 2 bare occurrences + 1 skipped inline; line 8 ("## 第二节") has 0 skipped.
        sec1 = run_json("--run", run_dir, "research", "index", "--section", "6")
        self.assertEqual(sec1["citation_count"], 2)
        self.assertEqual(sec1["skipped_inline_citations"], 1)
        self.assertIn("--include-inline-citations", sec1["note"])

        sec2 = run_json("--run", run_dir, "research", "index", "--section", "8")
        self.assertEqual(sec2["citation_count"], 1)
        self.assertEqual(sec2["skipped_inline_citations"], 0)
        self.assertNotIn("note", sec2)

        idx_included = run_json("--run", run_dir, "research", "index", "--include-inline-citations")
        self.assertEqual(idx_included["citations"], 4)
        self.assertEqual(idx_included["skipped_inline_citations"], 0)

    def test_cross_entry_printed_boundary_consistency(self):
        tight = self.root / "tight.txt"
        tight.write_text("第一章 开始\nSAFE\n第二章后文\nSECRET_FUTURE\n第三章 结束\nEND\n", encoding="utf-8")

        # 1) Printed boundary with tight future heading must be rejected across direct, init, expand and --from-result.
        direct_read = self.span_process("--text", tight, "read", "--as-of", "第一章", "--chapter", "1", "--format", "json")
        self.assertNotEqual(direct_read.returncode, 0)
        self.assertIn("疑似章题超过指定阅读进度", direct_read.stderr)

        direct_overview = self.span_process("--text", tight, "overview", "--as-of", "第一章")
        self.assertNotEqual(direct_overview.returncode, 0)
        self.assertIn("疑似章题超过指定阅读进度", direct_overview.stderr)

        init_task = self.root / "init_fail"
        init_res = self.span_process("--text", tight, "research", "init", init_task, "--as-of", "第一章")
        self.assertNotEqual(init_res.returncode, 0)
        self.assertIn("疑似章题超过指定阅读进度", init_res.stderr)
        self.assertFalse((init_task / "runs").exists() and any((init_task / "runs").iterdir()))

        expand_task = self.root / "expand_fail"
        created = json.loads(self.span_process(
            "--text", tight, "research", "init", expand_task, "--as-of", "0",
        ).stdout)
        run_dir = Path(created["run_directory"])
        expand_res = self.span_process("--run", run_dir, "research", "expand", "--as-of", "第一章")
        self.assertNotEqual(expand_res.returncode, 0)
        self.assertIn("疑似章题超过指定阅读进度", expand_res.stderr)
        records = read_run_records(load_run(run_dir))
        self.assertFalse(any(row["event"] == "scope_expand" for row in records))
        self.assertEqual(records[-1]["status"], "failure")
        still_empty = self.span_process("--run", run_dir, "read", "--chapter", "1", "--format", "json")
        self.assertNotEqual(still_empty.returncode, 0)

        # --as-of C0001 succeeds across all three paths; passing --as-of 第一章 via --from-result still rejects.
        c1_direct = json.loads(self.span_process(
            "--text", tight, "read", "--as-of", "C0001", "--chapter", "1", "--format", "json",
        ).stdout)
        c1_file = self.root / "c1_result.json"
        c1_file.write_text(json.dumps(c1_direct, ensure_ascii=False), encoding="utf-8")
        handoff_fail = self.span_process(
            "--text", tight, "read", "--from-result", c1_file, "--as-of", "第一章", "--chapter", "1", "--format", "json",
        )
        self.assertNotEqual(handoff_fail.returncode, 0)
        self.assertIn("疑似章题超过指定阅读进度", handoff_fail.stderr)

        c1_init = self.span_process("--text", tight, "research", "init", self.root / "c1_init", "--as-of", "C0001")
        self.assertEqual(c1_init.returncode, 0, c1_init.stderr)
        c1_expand = self.span_process("--run", run_dir, "research", "expand", "--as-of", "C0001")
        self.assertEqual(c1_expand.returncode, 0, c1_expand.stderr)

        # 2) Clean chapter boundaries work identically across all entries with printed boundary 第一章.
        clean_direct = json.loads(self.span_process(
            "--text", self.source, "overview", "--as-of", "第一章", "--format", "json",
        ).stdout)
        clean_file = self.root / "clean_ov.json"
        clean_file.write_text(json.dumps(clean_direct, ensure_ascii=False), encoding="utf-8")
        clean_handoff = json.loads(self.span_process(
            "--text", self.source, "read", "--from-result", clean_file, "--as-of", "第一章", "--pick", "1", "--format", "json",
        ).stdout)
        clean_init = json.loads(self.span_process(
            "--text", self.source, "research", "init", self.root / "clean_init", "--as-of", "第一章",
        ).stdout)
        clean_expand_base = json.loads(self.span_process(
            "--text", self.source, "research", "init", self.root / "clean_expand", "--as-of", "0",
        ).stdout)
        clean_expand = json.loads(self.span_process(
            "--run", clean_expand_base["run_directory"], "research", "expand", "--as-of", "第一章",
        ).stdout)
        self.assertEqual(clean_direct["scope"]["as_of"], 2)
        self.assertEqual(clean_handoff["scope"]["as_of"], 2)
        self.assertEqual(clean_init["scope"]["as_of"], 2)
        self.assertEqual(clean_expand["scope"]["as_of"], 2)

    def test_printed_boundary_diagnostics_use_the_run_and_result_intersection(self):
        source = self.root / "分卷.txt"
        source.write_text("第一章 甲\n甲文。\n第一章 乙\n乙文。\n第一章 丙\n丙文。\n", encoding="utf-8")
        output = self.root / "第二章目.json"
        previous = self.span_process("--text", source, "overview", "--from-chapter", "2",
                                     "--as-of", "2", "--output", output)
        self.assertEqual(previous.returncode, 0, previous.stderr)
        created = self.span_process("--text", source, "research", "init", self.root / "任务",
                                    "--as-of", "all")
        self.assertEqual(created.returncode, 0, created.stderr)
        run = json.loads(created.stdout)["run_directory"]
        result = self.span_process("--run", run, "read", "--from-result", output,
                                   "--as-of", "第一章", "--pick", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C0002", result.stderr)
        self.assertNotIn("C0001", result.stderr)
        self.assertNotIn("C0003", result.stderr)

    def test_research_accepts_displayed_chapter_coordinates(self):
        created = self.span_process("--text", self.source, "research", "init", self.root / "任务",
                                    "--from-chapter", "C0002", "--as-of", "C0003")
        self.assertEqual(created.returncode, 0, created.stderr)
        run = json.loads(created.stdout)["run_directory"]
        expanded = self.span_process("--run", run, "research", "expand", "--from-chapter", "C0001")
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(json.loads(expanded.stdout)["scope"]["first_chapter"], 1)

    def test_handoff_printed_boundary_checks_only_deliverable_chapters(self):
        source = self.root / "错插.txt"
        source.write_text("第一章 甲\n甲文。\n第九章 乙\n乙文。\n第三章 丙\n丙文。\n", encoding="utf-8")
        output = self.root / "第一章目.json"
        previous = self.span_process("--text", source, "overview", "--as-of", "1", "--output", output)
        self.assertEqual(previous.returncode, 0, previous.stderr)
        selection = self.span_process("--text", source, "read", "--from-result", output,
                                      "--as-of", "第三章", "--pick", "1", "--format", "json")
        self.assertEqual(selection.returncode, 0, selection.stderr)
        page = json.loads(selection.stdout)
        self.assertEqual(page["scope"]["as_of"], 1)
        self.assertEqual(page["fragments"][0]["text"].strip(), "甲文。")

    def test_malformed_result_scope_validation(self):
        bad_cases = [
            {"scope": {"as_of": 1, "first_chapter": 1, "include_peripheral": False}},
            {"scope": {"snapshot_id": "", "as_of": 1, "first_chapter": 1, "include_peripheral": False}},
            {"scope": {"snapshot_id": self.snapshot, "as_of": True, "first_chapter": 1, "include_peripheral": False}},
            {"scope": {"snapshot_id": self.snapshot, "as_of": 1, "first_chapter": "1", "include_peripheral": False}},
            {"scope": {"snapshot_id": self.snapshot, "as_of": 1, "first_chapter": 0, "include_peripheral": False}},
            {"scope": {"snapshot_id": self.snapshot, "as_of": 1, "first_chapter": 1, "include_peripheral": "false"}},
        ]
        for idx, payload in enumerate(bad_cases):
            broken = self.root / f"broken_{idx}.json"
            broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            lib_res = self.span_process("--library", self.book, "overview", "--as-of", "all", "--from-result", broken)
            self.assertNotEqual(lib_res.returncode, 0)
            self.assertIn("span:", lib_res.stderr)
            self.assertNotIn("Traceback", lib_res.stderr)
            self.assertIn("重新生成", lib_res.stderr)

            txt_res = self.span_process("--text", self.source, "overview", "--from-result", broken)
            self.assertNotEqual(txt_res.returncode, 0)
            self.assertIn("span:", txt_res.stderr)
            self.assertNotIn("Traceback", txt_res.stderr)

        # Bound run logs execute failure cleanly without unhandled exceptions.
        created = json.loads(self.span_process(
            "--library", self.book, "research", "init", self.root / "r05_run",
            "--snapshot", self.snapshot, "--as-of", "3",
        ).stdout)
        run_dir = Path(created["run_directory"])
        run_fail = self.span_process("--run", run_dir, "overview", "--from-result", self.root / "broken_0.json")
        self.assertNotEqual(run_fail.returncode, 0)
        self.assertIn("span:", run_fail.stderr)
        records = read_run_records(load_run(run_dir))
        self.assertEqual((records[-1]["status"], records[-1]["failed_stage"]), ("failure", "execute"))

    def test_unclosed_citation_detection_and_exclusions(self):
        # 1) Unclosed [@C... and [@snap_... are reported as malformed with line number and missing-] note.
        draft = self.root / "unclosed.md"
        long_snap = "a" * 90
        draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 4\n---\n\n"
            "# 节一\n"
            "结论。[@C0001:0-2\n"
            f"长引用。[@snap_{long_snap}/C0001:0-2\n",
            encoding="utf-8",
        )
        resumed = json.loads(self.span_process(
            "--text", self.source, "resume", draft, "--as-of", "4", "--citations-only",
        ).stdout)
        self.assertEqual(resumed["citation_count"], 2)
        first_cit, second_cit = resumed["citations"]
        self.assertEqual(first_cit["citation"], "[@C0001:0-2")
        self.assertEqual(first_cit["status"], "malformed")
        self.assertIn("第 7 行", first_cit["note"])
        self.assertIn("缺少右方括号", first_cit["note"])
        self.assertEqual(second_cit["status"], "malformed")
        self.assertLessEqual(len(second_cit["citation"]), 80)
        self.assertIn("第 8 行", second_cit["note"])
        self.assertIn("缺少右方括号", second_cit["note"])

        # research index agrees on citation counts and malformed status for the same draft.
        created = json.loads(self.span_process(
            "--text", self.source, "research", "init", self.root / "r06_run", "--as-of", "4",
        ).stdout)
        run_dir = Path(created["run_directory"])
        indexed = json.loads(self.span_process(
            "--run", run_dir, "research", "index", "--draft", draft,
        ).stdout)
        self.assertEqual(indexed["citations"], 2)
        index_md = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("格式错误", index_md)
        sec_idx = json.loads(self.span_process(
            "--run", run_dir, "research", "index", "--draft", draft, "--section", "6",
        ).stdout)
        self.assertEqual(sec_idx["citation_count"], 2)
        self.assertEqual([row["status"] for row in sec_idx["citations"]], ["malformed", "malformed"])

        # 2) Valid zero-citation draft, Pandoc [@smith2020, escaped \[@C..., inline code, and fenced code stay silent.
        clean_draft = self.root / "clean_zero.md"
        clean_draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 4\n---\n\n"
            "普通段落和未闭合 [@doe2021 与 \\[@C0001:0-2。\n"
            "行内代码 `[@C0001:0-2` 与 `[@snap_123`。\n"
            "```markdown\n[@C0001:0-2\n```\n",
            encoding="utf-8",
        )
        clean_res = json.loads(self.span_process(
            "--text", self.source, "resume", clean_draft, "--as-of", "4", "--citations-only",
        ).stdout)
        self.assertEqual(clean_res["citation_count"], 0)
        self.assertEqual(clean_res["citations"], [])
        self.assertNotIn("note", clean_res)

    def test_navigation_limits_and_peripheral_hint(self):
        # U01: 1-33 reports 32-selection limit and recovery path; 1-101 reports 100-chapter range width; helps match.
        many = self.root / "many.txt"
        many.write_text("".join(f"第{i}章 标题\n正文。\n" for i in range(1, 35)), encoding="utf-8")
        over_sel = self.span_process("--text", many, "read", "--as-of", "all", "--chapter", "1-33", "--format", "json")
        self.assertNotEqual(over_sel.returncode, 0)
        self.assertIn("32", over_sel.stderr)
        self.assertIn("--from-result", over_sel.stderr)

        over_span = self.span_process("--text", many, "read", "--as-of", "all", "--chapter", "1-101", "--format", "json")
        self.assertNotEqual(over_span.returncode, 0)
        self.assertIn("100", over_span.stderr)

        read_help = self.span_process("read", "--help").stdout
        self.assertIn("100", read_help)
        self.assertIn("32", read_help)
        quick_help = self.span_process("help", "quickstart").stdout
        self.assertIn("100", quick_help)
        self.assertIn("32", quick_help)

        # U02: peripheral overflow hint points to overview --include-peripheral --toc, which lists all 7 peripheral chapters.
        periph = self.root / "peripheral.txt"
        periph.write_text("第一章 开始\n正文。\n" + "".join(f"作者的话 {i}\n副文本。\n" for i in range(1, 8)), encoding="utf-8")
        ov_default = self.span_process("--text", periph, "overview", "--as-of", "all").stdout
        self.assertNotIn("overview --format json", ov_default)
        self.assertIn("overview --include-peripheral --toc", ov_default)

        ov_toc = self.span_process("--text", periph, "overview", "--as-of", "all", "--include-peripheral", "--toc").stdout
        self.assertEqual(ov_toc.count("[外围]"), 7)

    def test_actionable_error_feedback_for_agent_efficiency(self):
        # 1. Character range overflow reports chapter character count
        over_chars = self.span_process("--text", self.source, "read", "C0001:0-99999", "--as-of", "4")
        self.assertNotEqual(over_chars.returncode, 0)
        self.assertIn("共 ", over_chars.stderr)
        self.assertIn("字符", over_chars.stderr)

        # 2. overview --offset guides user to --after-chapter
        bad_offset = self.span_process("--text", self.source, "overview", "--as-of", "4", "--offset", "2")
        self.assertNotEqual(bad_offset.returncode, 0)
        self.assertIn("--after-chapter", bad_offset.stderr)

        # 3. Peripheral chapter within scope hints --include-peripheral; out-of-scope peripheral does not leak
        periph = self.root / "periph_scope.txt"
        periph.write_text("第一章 正篇一\n甲。\n作者的话\n感言。\n第二章 正篇二\n乙。\n后记\n尾声。\n", encoding="utf-8")
        periph_lib = self.root / "periph_lib"
        snap = json.loads(self.span_process("--library", periph_lib, "import-txt", periph).stdout)["snapshot_id"]

        for source_args, extra_args in (
            (("--text", periph), ()),
            (("--library", periph_lib), ("--snapshot", snap)),
        ):
            in_scope_periph = self.span_process(
                *source_args, "overview", *extra_args, "--as-of", "3", "--inspect", "C0002"
            )
            self.assertNotEqual(in_scope_periph.returncode, 0)
            self.assertIn("不可读取", in_scope_periph.stderr)
            self.assertIn("--include-peripheral", in_scope_periph.stderr)

            beyond_scope = self.span_process(
                *source_args, "overview", *extra_args, "--as-of", "3", "--inspect", "C0004"
            )
            self.assertNotEqual(beyond_scope.returncode, 0)
            self.assertIn("不可读取", beyond_scope.stderr)
            self.assertNotIn("--include-peripheral", beyond_scope.stderr)

        # 4. Run scope violation echoes current and requested scope bounds
        init_res = json.loads(
            self.span_process("--text", periph, "research", "init", self.root / "scope_task", "--as-of", "2").stdout
        )
        out_of_run = self.span_process("--run", init_res["run_directory"], "overview", "--as-of", "3")
        self.assertNotEqual(out_of_run.returncode, 0)
        self.assertIn("C0001–C0002", out_of_run.stderr)
        self.assertIn("C0001–C0003", out_of_run.stderr)

    def test_cold_start_ergonomics_and_safe_diagnostics(self):
        # 1. Decode failure suggests gb18030 and import-txt accepts --encoding before subcommand
        gb_book = self.root / "gb_book.txt"
        gb_book.write_text("第一章 开篇\n汉字编码测试正文。\n", encoding="gb18030")
        bad_decode = self.span_process("--text", gb_book, "overview", "--as-of", "1")
        self.assertNotEqual(bad_decode.returncode, 0)
        self.assertIn("--encoding gb18030", bad_decode.stderr)

        hoisted_preview = self.span_process("--encoding", "gb18030", "import-txt", gb_book, "--preview")
        self.assertEqual(hoisted_preview.returncode, 0, hoisted_preview.stderr)
        preview_data = json.loads(hoisted_preview.stdout)
        self.assertEqual(preview_data["heading_count"], 1)
        self.assertEqual(preview_data["encoding"], "gb18030")

        dup_encoding = self.span_process(
            "--encoding", "gb18030", "import-txt", gb_book, "--preview", "--encoding", "gbk"
        )
        self.assertNotEqual(dup_encoding.returncode, 0)

        # 2. overview --limit up to 2000 returns characters and paragraph_count in one call
        many_book = self.root / "many_stats.txt"
        many_book.write_text("".join(f"第{i}章 第{i}节\n段落甲。\n段落乙。\n" for i in range(1, 151)), encoding="utf-8")
        many_lib = self.root / "many_lib"
        many_snap = json.loads(self.span_process("--library", many_lib, "import-txt", many_book).stdout)["snapshot_id"]
        for ov_args in (
            ("--text", many_book, "overview", "--as-of", "all", "--limit", "200", "--format", "json"),
            ("--library", many_lib, "overview", "--snapshot", many_snap, "--as-of", "all", "--limit", "200"),
        ):
            ov = json.loads(self.span_process(*ov_args).stdout)
            self.assertEqual(len(ov["chapters"]), 150)
            self.assertIsNone(ov["next_after_chapter"])
            self.assertIn("characters", ov["chapters"][0])
            self.assertEqual(ov["chapters"][0]["paragraph_count"], 2)

        # 3. Duplicate printed numbers report safe Cxxxx coordinates without leaking titles or post-run chapters
        dup_book = self.root / "dup_vol.txt"
        dup_book.write_text(
            "第一章 初入江湖\n甲。\n第二章 启程\n乙。\n第一章 秘密卷二标题\n丙。\n",
            encoding="utf-8",
        )
        dup_err = self.span_process("--text", dup_book, "overview", "--as-of", "第一章")
        self.assertNotEqual(dup_err.returncode, 0)
        self.assertIn("C0001", dup_err.stderr)
        self.assertIn("C0003", dup_err.stderr)
        self.assertNotIn("秘密卷二标题", dup_err.stderr)

        run_dir = json.loads(
            self.span_process("--text", dup_book, "research", "init", self.root / "dup_task", "--as-of", "2").stdout
        )["run_directory"]
        run_dup_err = self.span_process("--run", run_dir, "overview", "--as-of", "第二章")
        self.assertEqual(run_dup_err.returncode, 0)
        dup_book_vol2 = self.root / "dup_vol2.txt"
        dup_book_vol2.write_text(
            "第一章 卷一\n甲。\n第一章 隐秘卷二\n乙。\n第二章 尾声\n丙。\n",
            encoding="utf-8",
        )
        run_dir2 = json.loads(
            self.span_process("--text", dup_book_vol2, "research", "init", self.root / "dup_task2", "--as-of", "2").stdout
        )["run_directory"]
        scoped_dup = self.span_process("--run", run_dir2, "overview", "--as-of", "第一章")
        self.assertNotEqual(scoped_dup.returncode, 0)
        self.assertIn("C0001", scoped_dup.stderr)
        self.assertIn("C0002", scoped_dup.stderr)

        run_dir1 = json.loads(
            self.span_process("--text", dup_book_vol2, "research", "init", self.root / "dup_task1", "--as-of", "1").stdout
        )["run_directory"]
        strict_dup = self.span_process("--run", run_dir1, "overview", "--as-of", "第一章")
        self.assertNotEqual(strict_dup.returncode, 0)
        self.assertIn("C0001", strict_dup.stderr)
        self.assertNotIn("C0002", strict_dup.stderr)

        # 4. find matches include integer chapter field and --from-chapter accepts Cxxxx
        found = json.loads(
            self.span_process(
                "--text", dup_book, "find", "丙", "--from-chapter", "C0002", "--as-of", "C0003", "--format", "json"
            ).stdout
        )
        self.assertEqual(found["matches"][0]["chapter"], 3)
        self.assertEqual(found["scope"]["first_chapter"], 2)

        # 5. Tight-heading preview and error directly provide reusable chapter pattern
        tight_book = self.root / "tight_vol.txt"
        tight_book.write_text("第一章引子\n甲。\n第二章出发\n乙。\n", encoding="gb18030")
        decode_hint = self.span_process("--text", tight_book, "overview", "--as-of", "2")
        self.assertNotEqual(decode_hint.returncode, 0)
        self.assertIn("import-txt --preview --encoding gb18030", decode_hint.stderr)

        tight_preview = json.loads(
            self.span_process("--encoding", "gb18030", "import-txt", tight_book, "--preview").stdout
        )
        self.assertEqual(tight_preview["heading_count"], 0)
        self.assertEqual(tight_preview["tight_candidate_count"], 2)
        suggested = tight_preview["suggested_chapter_pattern"]

        tight_err = self.span_process("--text", tight_book, "--encoding", "gb18030", "overview", "--as-of", "2")
        self.assertNotEqual(tight_err.returncode, 0)
        self.assertIn(suggested, tight_err.stderr)

        fixed_ov = json.loads(
            self.span_process(
                "--text", tight_book, "--encoding", "gb18030", "--chapter-pattern", suggested,
                "overview", "--as-of", "2", "--format", "json",
            ).stdout
        )
        self.assertEqual([c["title"] for c in fixed_ov["chapters"]], ["第一章引子", "第二章出发"])


if __name__ == "__main__":
    unittest.main()
