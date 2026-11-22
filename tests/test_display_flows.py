import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from chapspan import cli
from chapspan.cli import _resolve_cli_prog
from chapspan.display import format_text
from chapspan.library import read_text_source
from chapspan.model import Scope
from chapspan.search import find_passages
from chapspan.sources import ScopedText


@contextmanager
def _chdir(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_cli(argv, *, cwd=None):
    out = io.StringIO()
    err = io.StringIO()
    exit_code = 0
    ctx = _chdir(cwd) if cwd is not None else _chdir(Path.cwd())
    with ctx, redirect_stdout(out), redirect_stderr(err):
        try:
            exit_code = cli.main(list(argv))
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
    return exit_code, out.getvalue(), err.getvalue()


def _extract_command(output, prefix):
    for line in output.splitlines():
        if line.startswith(prefix):
            _, _, command = line.partition("：")
            return command.strip()
    raise AssertionError(f"Expected line starting with {prefix!r} in:\n{output}")


def _exec_printed_command(output, prefix, *, cwd=None):
    cmd = _extract_command(output, prefix)
    parts = shlex.split(cmd, posix=True)
    if not parts or parts[0] != "span":
        raise AssertionError(f"Expected printed command starting with 'span', got: {cmd!r}")
    return cmd, _run_cli(parts[1:], cwd=cwd)


class DisplayFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _build_sparse_book(self):
        book_dir = self.root / "my books"
        book_dir.mkdir(exist_ok=True)
        path = book_dir / "sample (v1).txt"
        path.write_text(
            "第一章 雾夜\n"
            "第一段信号灯闪烁。\n"
            "第二段信号灯熄灭。\n"
            "第三段信号灯重亮。\n"
            "第二章 归途\n"
            "夜路平静。\n"
            "第三章 港口\n"
            "远处看见信号灯。\n"
            "第四章 荒原\n"
            "风沙漫天。\n"
            "第五章 塔楼\n"
            "高塔亮着信号灯。\n",
            encoding="utf-8",
        )
        return path

    def _build_compare_book(self):
        path = self.root / "对比样本 (v1).txt"
        left_block = "铁剑出鞘。\n" * 6 + "旧城门守卫。\n" * 4
        overlap_block = "铁剑与玄铁剑并置。\n" * 3
        right_block = "玄铁剑鸣响。\n" * 6 + "新港星舰起航。\n" * 4
        path.write_text(
            f"第一章 旧事\n{left_block}"
            f"第二章 转折\n{overlap_block}"
            f"第三章 新章\n{right_block}"
            "上架感言\n感谢大家订阅支持！\n"
            f"第四章 尾声\n{right_block}",
            encoding="utf-8",
        )
        return path

    def test_sparse_find_screen_compact_flag_and_multiquery_modes(self):
        book = self._build_sparse_book()
        states = self.root / "state dir"
        rel_book = Path("my books") / "sample (v1).txt"
        rel_states = Path("state dir")

        code, out, err = _run_cli(
            ["--text", str(rel_book), "--state-dir", str(rel_states), "find", "信号灯", "--as-of", "5"],
            cwd=self.root,
        )
        self.assertEqual(code, 0, err)
        self.assertIn('"信号灯" 5 次 / 3 章（C0001 · C0003 · C0005） · 范围 C0001–C0005', out)
        self.assertNotIn("区段（", out)
        self.assertIn("例段 3：C0001(3)（跨章 --spread）", out)
        self.assertIn("[例1] 第一章 雾夜 [@C0001:0-10]", out)
        self.assertNotIn("本页坐标：", out)
        self.assertNotIn("快照 ", out)
        self.assertIn("其余：--hit-chapters 看各章 | --examples 0 看坐标", out)
        _, (ex_code, ex_out, ex_err) = _exec_printed_command(out, "扩读（1–3）", cwd=self.root)
        self.assertEqual(ex_code, 0, ex_err)
        self.assertIn("第二段信号灯熄灭。", ex_out)

        # --spread unifies single-line [例1] header across default and --spread.
        code_sp, out_sp, err_sp = _run_cli(
            ["--text", str(rel_book), "--state-dir", str(rel_states), "find", "信号灯", "--spread", "--as-of", "5"],
            cwd=self.root,
        )
        self.assertEqual(code_sp, 0, err_sp)
        self.assertIn("例段 3（跨章）：C0001 · C0003 · C0005", out_sp)
        self.assertIn("[例1] 第一章 雾夜 [@C0001:0-10]", out_sp)
        self.assertIn("[例2] 第三章 港口 [@C0003:0-9]", out_sp)
        self.assertIn("[例3] 第五章 塔楼 [@C0005:0-9]", out_sp)

        # --compact alone on --text does NOT suppress examples.
        code_c, out_c, err_c = _run_cli(
            ["--text", str(rel_book), "--state-dir", str(rel_states), "find", "信号灯", "--compact", "--as-of", "5"],
            cwd=self.root,
        )
        self.assertEqual(code_c, 0, err_c)
        self.assertIn("[例1] 第一章 雾夜", out_c)

        # --examples 0 renders compact coordinates + verbatim executable direct read (preserving non-default parsing).
        gb_book = self.root / "gb books" / "gb (1).txt"
        gb_book.parent.mkdir()
        gb_book.write_bytes("第一章 雾夜\n信号灯亮了。\n".encode("gb18030"))
        code_z, out_z, err_z = _run_cli(
            ["--text", str(gb_book), "--encoding", "gb18030", "find", "信号灯", "--examples", "0", "--as-of", "1"],
            cwd=self.root,
        )
        self.assertEqual(code_z, 0, err_z)
        self.assertIn("本页坐标：1=C0001:0-3", out_z)
        cmd_z, (rz_code, rz_out, rz_err) = _exec_printed_command(out_z, "回读首处", cwd=self.root)
        self.assertIn("--encoding gb18030", cmd_z)
        self.assertEqual(rz_code, 0, rz_err)
        self.assertIn("信号灯亮了。", rz_out)

        # Multi-query without examples omits 1= numbering and tutorial; --examples 1 & --spread unify [Q1E1].
        code_mq, out_mq, err_mq = _run_cli(
            [
                "--text", str(rel_book), "find", "--as-of", "5",
                "--query", '{"all":["信号灯"]}', "--query", '{"all":["夜路"]}',
            ],
            cwd=self.root,
        )
        self.assertEqual(code_mq, 0, err_mq)
        self.assertNotIn("加 --examples 1", out_mq)
        self.assertNotIn("1=C", out_mq)
        self.assertIn("本页坐标：C0001:3-6 · C0001:13-16 · C0001:23-26 · C0003:4-7 · C0005:4-7", out_mq)
        _, (mq_r_code, mq_r_out, mq_r_err) = _exec_printed_command(out_mq, "回读首处", cwd=self.root)
        self.assertEqual(mq_r_code, 0, mq_r_err)
        self.assertIn("第一段信号灯闪烁。", mq_r_out)

        for extra in ((), ("--spread",)):
            c_e, o_e, e_e = _run_cli(
                [
                    "--text", str(rel_book), "--state-dir", str(states), "find", "--as-of", "5",
                    "--examples", "1", *extra,
                    "--query", '{"all":["信号灯"]}', "--query", '{"all":["夜路"]}',
                ],
                cwd=self.root,
            )
            self.assertEqual(c_e, 0, e_e)
            self.assertIn("[Q1E1] 第一章 雾夜", o_e)
            self.assertIn("[Q2E1] 第二章 归途", o_e)

    def test_compare_to_read_ungrouped_grouped_overlap_continuation_and_legacy(self):
        book = self._build_compare_book()
        states = self.root / "compare states"

        # Interval 1-2 vs 2-4 has overlap on C0002 and filtered peripheral chapter on right (上架感言 -> C0004).
        code, cmp_out, err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "compare", "--left", "1-2", "--right", "2-5", "--as-of", "all", "--min-support", "3",
            ],
            cwd=self.root,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("对比：左 C0001–C0002（2 章", cmp_out)
        self.assertIn("右 C0002–C0005（3 章", cmp_out)
        self.assertIn("过滤外围 1 章", cmp_out)
        self.assertIn("有章节重叠", cmp_out)
        self.assertNotIn("筛选规则：", cmp_out)
        self.assertNotIn("计数口径：", cmp_out)

        # Find pick indices for 铁剑 (or another candidate present on both sides with overlap in C0002).
        code_j, cmp_json_raw, err_j = _run_cli(
            [
                "--text", str(book), "compare", "--left", "1-2", "--right", "2-5", "--as-of", "all",
                "--min-support", "3", "--format", "json",
            ],
            cwd=self.root,
        )
        self.assertEqual(code_j, 0, err_j)
        cmp_data = json.loads(cmp_json_raw)
        both_idx = next(
            i for i, c in enumerate(cmp_data["candidates"], 1)
            if c["left"]["read_spans"] and c["right"]["read_spans"]
        )
        both_term = cmp_data["candidates"][both_idx - 1]["term"]
        state_id = re.search(r"state_[0-9a-f]{24}", cmp_out).group(0)

        # 1) Grouped read (verbatim printed command when both_idx == 1, or same candidate index).
        _, (g_code, g_out, g_err) = _exec_printed_command(cmp_out, "回读（1–", cwd=self.root)
        self.assertEqual(g_code, 0, g_err)
        top_term = cmp_data["candidates"][0]["term"]
        self.assertIn(f"「{top_term}」", g_out)

        g_both_code, g_both_out, g_both_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "read", "--from-result", state_id, "--pick", str(both_idx), "--group-budget", "1200",
            ],
            cwd=self.root,
        )
        self.assertEqual(g_both_code, 0, g_both_err)
        self.assertIn(f"[组1 · 「{both_term}」左]", g_both_out)
        self.assertIn(f"[组2 · 「{both_term}」右]", g_both_out)

        # 2) Ungrouped read (user omits --group-budget) preserves candidate term and side!
        u_code, u_out, u_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "read", "--from-result", state_id, "--pick", str(both_idx),
            ],
            cwd=self.root,
        )
        self.assertEqual(u_code, 0, u_err)
        self.assertIn(f"「{both_term}」左", u_out)
        self.assertIn(f"「{both_term}」右", u_out)

        # 3) Overlapping left/right spans into same paragraph: body printed ONCE, attribution clear in BOTH modes.
        overlap_state_payload = {
            "state_version": 1,
            "scope": cmp_data["scope"],
            "candidates": [{
                "term": "铁剑",
                "left_spans": ["C0002:0-2"],
                "right_spans": ["C0002:5-7"],
                "read_spans": ["C0002:0-2", "C0002:5-7"],
            }],
        }
        overlap_file = self.root / "overlap.state.json"
        overlap_file.write_text(json.dumps(overlap_state_payload, ensure_ascii=False), encoding="utf-8")

        ug_ov_c, ug_ov_out, ug_ov_err = _run_cli(
            ["--text", str(book), "read", "--from-result", str(overlap_file), "--pick", "1"],
            cwd=self.root,
        )
        self.assertEqual(ug_ov_c, 0, ug_ov_err)
        self.assertIn("「铁剑」左、右", ug_ov_out)
        self.assertEqual(ug_ov_out.count("铁剑与玄铁剑并置。"), 1)

        gp_ov_c, gp_ov_out, gp_ov_err = _run_cli(
            ["--text", str(book), "read", "--from-result", str(overlap_file), "--pick", "1", "--group-budget", "1200"],
            cwd=self.root,
        )
        self.assertEqual(gp_ov_c, 0, gp_ov_err)
        self.assertIn("[组1 · 「铁剑」左]", gp_ov_out)
        self.assertIn("[组2 · 「铁剑」右]", gp_ov_out)
        self.assertIn("（见已显示的正文 1）", gp_ov_out)
        self.assertEqual(gp_ov_out.count("铁剑与玄铁剑并置。"), 1)

        # 4) Truncated ungrouped compare -> read continuation preserves context_labels on page 2!
        p1_c, p1_out, p1_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "read", "--from-result", state_id, "--pick", str(both_idx), "--budget", "10",
            ],
            cwd=self.root,
        )
        self.assertEqual(p1_c, 0, p1_err)
        self.assertIn("未开始：", p1_out)
        _, (p2_c, p2_out, p2_err) = _exec_printed_command(p1_out, "续读", cwd=self.root)
        self.assertEqual(p2_c, 0, p2_err)
        self.assertIn(f"「{both_term}」", p2_out)

        # 5) Legacy compare states without "term" (side-only and coord-only) degrade cleanly.
        legacy_side = self.root / "legacy_side.json"
        legacy_side.write_text(json.dumps({
            "scope": cmp_data["scope"],
            "candidates": [{"left_spans": ["C0001:0-2"], "right_spans": ["C0003:0-3"], "read_spans": ["C0001:0-2"]}],
        }, ensure_ascii=False), encoding="utf-8")
        ls_c, ls_out, ls_err = _run_cli(
            ["--text", str(book), "read", "--from-result", str(legacy_side), "--pick", "1"],
            cwd=self.root,
        )
        self.assertEqual(ls_c, 0, ls_err)
        self.assertIn("[@C0001:0-6] · 左", ls_out)
        self.assertIn("[@C0003:0-7] · 右", ls_out)

        legacy_coord = self.root / "legacy_coord.json"
        legacy_coord.write_text(json.dumps({
            "scope": cmp_data["scope"],
            "candidates": [{"read_spans": ["C0001:0-2"]}],
        }, ensure_ascii=False), encoding="utf-8")
        lc_c, lc_out, lc_err = _run_cli(
            ["--text", str(book), "read", "--from-result", str(legacy_coord), "--pick", "1"],
            cwd=self.root,
        )
        self.assertEqual(lc_c, 0, lc_err)
        self.assertIn("[@C0001:0-6]", lc_out)

    def test_compare_empty_provable_messages(self):
        en_book = self.root / "english.txt"
        en_book.write_text("Chapter 1 One\nWhale and ship.\nChapter 2 Two\nAhab and sea.\n", encoding="utf-8")
        c1, out1, err1 = _run_cli(
            ["--text", str(en_book), "compare", "--left", "1", "--right", "2", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(c1, 0, err1)
        self.assertIn("左右区间均没有达到最小词长（2 字）的连续汉字串；已知词请用 find。", out1)

        short_han = self.root / "short_han.txt"
        short_han.write_text("第一章 甲\n甲。乙。丙。\n第二章 丁\n丁。戊。己。\n", encoding="utf-8")
        c2, out2, err2 = _run_cli(
            ["--text", str(short_han), "compare", "--left", "1", "--right", "2", "--as-of", "all", "--min-len", "2"],
            cwd=self.root,
        )
        self.assertEqual(c2, 0, err2)
        self.assertIn("左右区间均没有达到最小词长（2 字）的连续汉字串；已知词请用 find。", out2)

        cn_book = self._build_compare_book()
        c3, out3, err3 = _run_cli(
            ["--text", str(cn_book), "compare", "--left", "1-2", "--right", "3-5", "--as-of", "all", "--offset", "99"],
            cwd=self.root,
        )
        self.assertEqual(c3, 0, err3)
        self.assertIn("当前分页起点 --offset 99 之后无候选，可减小 --offset。", out3)

    def test_verbatim_next_commands_across_paths_outputs_and_runs(self):
        book = self._build_sparse_book()
        states = self.root / "custom states"
        out_dir = self.root / "out files"
        out_dir.mkdir()

        # 1. Truncated read with space/paren path and custom --state-dir
        r1_c, r1_out, r1_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "read", "--chapter", "C0001-C0003", "--budget", "25", "--as-of", "5",
            ],
            cwd=self.root,
        )
        self.assertEqual(r1_c, 0, r1_err)
        self.assertEqual(r1_out.count("未完成"), 1)  # no triple repetition
        _, (r2_c, r2_out, r2_err) = _exec_printed_command(r1_out, "续读", cwd=self.root)
        self.assertEqual(r2_c, 0, r2_err)
        self.assertIn("第三段信号灯重亮。", r2_out)

        # 2. --output receipts: find (never 序号), find --hit-chapters, compare, truncated read
        f_out = out_dir / "find (1).txt"
        rc1, ro1, re1 = _run_cli(
            ["--text", str(book), "--state-dir", str(states), "--output", str(f_out), "find", "信号灯", "--as-of", "5"],
            cwd=self.root,
        )
        self.assertEqual(rc1, 0, re1)
        self.assertNotIn("--example 序号", ro1)
        self.assertIn("--example 1", ro1)
        _, (fc, fo, fe) = _exec_printed_command(ro1, "下一步", cwd=self.root)
        self.assertEqual(fc, 0, fe)
        self.assertIn("信号灯", fo)

        hc_out = out_dir / "hc (1).txt"
        rc2, ro2, re2 = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states), "--output", str(hc_out),
                "find", "信号灯", "--hit-chapters", "--as-of", "5",
            ],
            cwd=self.root,
        )
        self.assertEqual(rc2, 0, re2)
        _, (hc_c, hc_o, hc_e) = _exec_printed_command(ro2, "下一步", cwd=self.root)
        self.assertEqual(hc_c, 0, hc_e)
        self.assertIn("信号灯", hc_o)

        tr_out = out_dir / "tr (1).txt"
        rc3, ro3, re3 = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states), "--output", str(tr_out),
                "read", "--chapter", "C0001-C0002", "--budget", "15", "--as-of", "5",
            ],
            cwd=self.root,
        )
        self.assertEqual(rc3, 0, re3)
        _, (tc, to, te) = _exec_printed_command(ro3, "下一步", cwd=self.root)
        self.assertEqual(tc, 0, te)
        self.assertIn("第二段信号灯熄灭。", to)

        # 3. Explicit --run with spaces AND implicit cwd inside run_directory
        task_dir = self.root / "research task"
        init_c, init_out, init_err = _run_cli(
            ["--text", str(book), "research", "init", str(task_dir), "--as-of", "5"],
            cwd=self.root,
        )
        self.assertEqual(init_c, 0, init_err)
        run_dir = Path(json.loads(init_out)["run_directory"])

        run_f_c, run_f_out, run_f_err = _run_cli(
            ["--run", str(run_dir), "find", "信号灯"],
            cwd=self.root,
        )
        self.assertEqual(run_f_c, 0, run_f_err)
        _, (rf2_c, rf2_o, rf2_e) = _exec_printed_command(run_f_out, "扩读（1–3）", cwd=self.root)
        self.assertEqual(rf2_c, 0, rf2_e)
        self.assertIn("第二段信号灯熄灭。", rf2_o)

        cwd_r_c, cwd_r_out, cwd_r_err = _run_cli(
            ["read", "--chapter", "C0001", "--budget", "15"],
            cwd=run_dir,
        )
        self.assertEqual(cwd_r_c, 0, cwd_r_err)
        cmd_cwd, (cwd2_c, cwd2_o, cwd2_e) = _exec_printed_command(cwd_r_out, "续读", cwd=run_dir)
        self.assertNotIn("--run", cmd_cwd)
        self.assertEqual(cwd2_c, 0, cwd2_e)
        self.assertIn("第二段信号灯熄灭。", cwd2_o)

        # 4. --continue-group distinguishes all-pending vs specific partial / not_started / deferred groups
        g1_c, g1_out, g1_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "read", "--chapter", "C0001", "--chapter", "C0003", "--chapter", "C0005",
                "--group-budget", "5", "--budget", "15", "--as-of", "5",
            ],
            cwd=self.root,
        )
        self.assertEqual(g1_c, 0, g1_err)
        self.assertIn("续全部：", g1_out)
        self.assertIn("续 组1：", g1_out)
        _, (g2_c, g2_out, g2_e) = _exec_printed_command(g1_out, "续 组1：", cwd=self.root)
        self.assertEqual(g2_c, 0, g2_e)
        self.assertIn("本轮未续：组2", g2_out)
        self.assertIn("续 组2（未续）：", g2_out)

        ns_c, ns_out, ns_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states),
                "find", "--as-of", "5", "--examples", "2", "--budget", "12",
                "--query", '{"all":["信号灯"]}', "--query", '{"all":["段"]}',
            ],
            cwd=self.root,
        )
        self.assertEqual(ns_c, 0, ns_err)
        self.assertIn("未开始：Q1E2", ns_out)
        self.assertIn("Q2E2", ns_out)
        self.assertIn("续 Q1：", ns_out)
        _, (ns2_c, ns2_out, ns2_e) = _exec_printed_command(ns_out, "续 Q1：", cwd=self.root)
        self.assertEqual(ns2_c, 0, ns2_e)
        self.assertIn("本轮未续：Q2", ns2_out)
        _, (ns3_c, ns3_out, ns3_e) = _exec_printed_command(ns2_out, "续读", cwd=self.root)
        self.assertEqual(ns3_c, 0, ns3_e)
        self.assertIn("[Q2]", ns3_out)
        self.assertNotIn("未完成", ns3_out)

    def test_golden_screen_snapshots(self):
        # Snapshot 1: Sparse find (3 examples all in C0001 while hits span C0001, C0003, C0005)
        book = self._build_sparse_book()
        snap, chapters, _ = read_text_source(book)
        scoped = ScopedText(chapters, Scope(snap, 5))
        sparse = find_passages(scoped, all_terms=["信号灯"], examples=3)
        rendered_find = format_text({"scope": vars(scoped.scope), **sparse}, reference="state_sparse_demo")
        expected_find = "\n".join([
            '"信号灯" 5 次 / 3 章（C0001 · C0003 · C0005） · 范围 C0001–C0005',
            "例段 3：C0001(3)（跨章 --spread）",
            "",
            "[例1] 第一章 雾夜 [@C0001:0-10]",
            "第一段信号灯闪烁。",
            "",
            "[例2] 第一章 雾夜 [@C0001:10-20]",
            "第二段信号灯熄灭。",
            "",
            "[例3] 第一章 雾夜 [@C0001:20-30]",
            "第三段信号灯重亮。",
            "",
            "---",
            "扩读（1–3）：read --from-result state_sparse_demo --example 1 --before 1 --after 1",
            "其余：--hit-chapters 看各章 | --examples 0 看坐标",
        ])
        self.assertEqual(rendered_find, expected_find)

        # Snapshot 2: Truncated read (C0001 partial, C0002 not started)
        trunc_result = {
            "scope": {"snapshot_id": snap, "as_of": 2, "first_chapter": 1, "include_peripheral": False},
            "fragments": [{
                "chapter_title": "第一章 雾夜",
                "source_body_line": 2,
                "kind": "body",
                "requested_spans": ["C0001:0-27"],
                "match_spans": [],
                "target_span": "C0001:0-27",
                "read_span": "C0001:0-9",
                "citation": "[@C0001:0-9]",
                "source_citation": f"[@{snap}/C0001:0-9]",
                "text": "第一段信号灯闪烁。\n",
            }],
            "read_characters": 9,
            "complete": False,
            "next": "C0001:9",
            "delivery": [
                {
                    "group": 1, "chapter_title": "第一章 雾夜", "target_span": "C0001:0-27",
                    "requested_spans": ["C0001:0-27"], "read_spans": ["C0001:0-9"],
                    "read_characters": 9, "status": "partial", "next": "C0001:9",
                },
                {
                    "group": 1, "chapter_title": "第二章 归途", "target_span": "C0002:0-5",
                    "requested_spans": ["C0002:0-5"], "read_spans": [],
                    "read_characters": 0, "status": "not_started", "next": "C0002:0",
                },
            ],
            "continuation": {"groups": [{"group": 1}]},
        }
        expected_trunc = "\n".join([
            "范围 C0001–C0002",
            "",
            "第一章 雾夜 [@C0001:0-9]（未完成 9/27 字）",
            "第一段信号灯闪烁。",
            "",
            "---",
            "未开始：C0002:0-5",
            "续读：read --from-result state_trunc_demo",
        ])
        self.assertEqual(format_text(trunc_result, reference="state_trunc_demo"), expected_trunc)

        # Snapshot 3: Ungrouped compare -> read with side attribution & merged overlap
        cmp_read_result = {
            "scope": {"snapshot_id": snap, "as_of": 3, "first_chapter": 1, "include_peripheral": False},
            "fragments": [
                {
                    "chapter_title": "第一章 雾夜",
                    "source_body_line": 2,
                    "kind": "body",
                    "requested_spans": ["C0001:0-9"],
                    "match_spans": ["C0001:0-9"],
                    "target_span": "C0001:0-9",
                    "read_span": "C0001:0-9",
                    "citation": "[@C0001:0-9]",
                    "source_citation": f"[@{snap}/C0001:0-9]",
                    "text": "第一段信号灯闪烁。\n",
                    "context_labels": ["「信号灯」左", "「信号灯」右"],
                },
                {
                    "chapter_title": "第三章 港口",
                    "source_body_line": 8,
                    "kind": "body",
                    "requested_spans": ["C0003:0-8"],
                    "match_spans": ["C0003:0-8"],
                    "target_span": "C0003:0-8",
                    "read_span": "C0003:0-8",
                    "citation": "[@C0003:0-8]",
                    "source_citation": f"[@{snap}/C0003:0-8]",
                    "text": "远处看见信号灯。\n",
                    "context_labels": ["「信号灯」右"],
                },
            ],
            "read_characters": 17,
            "complete": True,
            "next": None,
            "delivery": [
                {
                    "group": 1, "chapter_title": "第一章 雾夜", "target_span": "C0001:0-9",
                    "requested_spans": ["C0001:0-9"], "read_spans": ["C0001:0-9"],
                    "read_characters": 9, "status": "complete", "next": None,
                },
                {
                    "group": 1, "chapter_title": "第三章 港口", "target_span": "C0003:0-8",
                    "requested_spans": ["C0003:0-8"], "read_spans": ["C0003:0-8"],
                    "read_characters": 8, "status": "complete", "next": None,
                },
            ],
            "continuation": None,
        }
        expected_cmp_read = "\n".join([
            "范围 C0001–C0003",
            "",
            "第一章 雾夜 [@C0001:0-9] · 「信号灯」左、右",
            "第一段信号灯闪烁。",
            "",
            "第三章 港口 [@C0003:0-8] · 「信号灯」右",
            "远处看见信号灯。",
        ])
        self.assertEqual(format_text(cmp_read_result), expected_cmp_read)

    def test_overview_toc_inspect_sample_and_output_receipt_batch2(self):
        book_dir = self.root / "batch2 books"
        book_dir.mkdir(exist_ok=True)
        book = book_dir / "diag (v2).txt"
        book.write_text(
            "楔子 引言\n序段正文。\n"
            "第一章 启程\n这里是一段足够用来测试截断阅读的长正文内容，包含多次出现的词语信号灯与雾夜记录。\n"
            "请假条\n今天请假一天。\n"
            "第三章 跳号\n章节内容到此结束。\n",
            encoding="utf-8",
        )
        states = self.root / "batch2 states"

        # 1) overview & overview --toc: no snapshot hash, C0001 on line 2, diagnostics below '---'
        for extra in ([], ["--toc"]):
            code, out, err = _run_cli(
                ["--text", str(book), "--state-dir", str(states), "overview", "--as-of", "all", *extra],
                cwd=self.root,
            )
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertNotIn("快照 ", out)
            self.assertTrue(lines[0].startswith("目录（本页 3 章 · 范围可见 "), lines[0])
            self.assertTrue(lines[1].startswith("C0001 "), lines[1])
            sep_idx = out.index("---")
            self.assertLess(sep_idx, out.index("章界诊断："))
            self.assertLess(sep_idx, out.index("过滤外围：1 章"))

        # Empty overview --title message
        em_c, em_out, em_err = _run_cli(
            ["--text", str(book), "overview", "--as-of", "all", "--title", "不存在的章题"],
            cwd=self.root,
        )
        self.assertEqual(em_c, 0, em_err)
        self.assertEqual(em_out.splitlines()[1], "当前条件无匹配章目。")

        # 2) overview --output: concise receipt, saved file with executable 回读（1–N）
        ov_file = book_dir / "目录 (保存).txt"
        ov_c, ov_out, ov_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states), "--output", str(ov_file),
                "overview", "--as-of", "all",
            ],
            cwd=self.root,
        )
        self.assertEqual(ov_c, 0, ov_err)
        self.assertNotIn("结果未在终端展开", ov_out)
        self.assertIn("章界诊断：1 处（核查 overview --inspect C编号）", ov_out)
        self.assertIn("过滤外围：1 章（纳入 --include-peripheral）", ov_out)
        saved_ov = ov_file.read_text(encoding="utf-8")
        self.assertIn("回读（1–3）：", saved_ov)

        _, (next_c, next_out, next_err) = _exec_printed_command(ov_out, "下一步", cwd=self.root)
        self.assertEqual(next_c, 0, next_err)
        self.assertIn("序段正文。", next_out)
        _, (pick_c, pick_out, pick_err) = _exec_printed_command(saved_ov, "回读（1–3）", cwd=self.root)
        self.assertEqual(pick_c, 0, pick_err)
        self.assertIn("序段正文。", pick_out)

        # 3) overview --inspect preserves snapshot hash, parsing JSON, and source lines
        insp_c, insp_out, insp_err = _run_cli(
            ["--text", str(book), "overview", "--as-of", "all", "--inspect", "C0002"],
            cwd=self.root,
        )
        self.assertEqual(insp_c, 0, insp_err)
        self.assertIn("快照 snap_", insp_out)
        self.assertIn("解析：", insp_out)
        self.assertIn("修正解析：", insp_out)
        self.assertIn("→ 行 3 | 第一章 启程", insp_out)

        # 4) read --sample: single-line header directly followed by [组1], diagnostics in footer if present
        samp_c, samp_out, samp_err = _run_cli(
            ["--text", str(book), "read", "--as-of", "all", "--sample", "2", "--budget", "400"],
            cwd=self.root,
        )
        self.assertEqual(samp_c, 0, samp_err)
        samp_lines = samp_out.splitlines()
        self.assertEqual(samp_lines[0], "章首抽读 2 章（仅章首片段，整章用 read --chapter C编号） · 范围 C0001–C0004")
        self.assertEqual(samp_lines[1], "")
        self.assertEqual(samp_lines[2], "[组1]")
        self.assertEqual(samp_out.count("范围 "), 1)
        self.assertLess(samp_out.index("---"), samp_out.index("章界诊断："))

        # Clean book sample (no diagnostics -> no footer separator)
        clean_book = self._build_sparse_book()
        cs_c, cs_out, cs_err = _run_cli(
            ["--text", str(clean_book), "read", "--as-of", "all", "--sample", "2", "--budget", "400"],
            cwd=self.root,
        )
        self.assertEqual(cs_c, 0, cs_err)
        self.assertTrue(cs_out.splitlines()[0].startswith("章首抽读 2 章"))
        self.assertNotIn("---", cs_out)

        # 5) Truncated read --output vs complete JSON --output receipt
        trunc_out_file = self.root / "trunc_read.txt"
        tr_c, tr_out, tr_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states), "--output", str(trunc_out_file),
                "read", "--as-of", "all", "--chapter", "C0002", "--budget", "6",
            ],
            cwd=self.root,
        )
        self.assertEqual(tr_c, 0, tr_err)
        self.assertIn("选区交付：部分 1（仍有待续）", tr_out)
        self.assertNotIn("本批仍有待续内容", tr_out)
        self.assertNotIn("结果未在终端展开", tr_out)

        json_out_file = self.root / "trunc_read.json"
        jr_c, jr_out, jr_err = _run_cli(
            [
                "--text", str(book), "--state-dir", str(states), "--format", "json",
                "--output", str(json_out_file), "read", "--as-of", "all", "--chapter", "C0002", "--budget", "6",
            ],
            cwd=self.root,
        )
        self.assertEqual(jr_c, 0, jr_err)
        self.assertEqual(json.loads(jr_out)["note"], "结果未在终端展开")

    def test_review_fixes_snapshot_state_dir_spread_empty_toc_sample_json_and_prog(self):
        # 1. [P1] Direct --text read_span_cmd binds --snapshot and rejects modified source files.
        src = self.root / "source (live).txt"
        src.write_text("第一章 开场\n这里只有目标句子。\n", encoding="utf-8")
        c0, out0, err0 = _run_cli(
            ["--text", str(src), "find", "目标", "--examples", "0", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(c0, 0, err0)
        cmd_read = _extract_command(out0, "回读首处")
        self.assertIn("--snapshot snap_", cmd_read)
        _, (ok_c, ok_out, ok_err) = _exec_printed_command(out0, "回读首处", cwd=self.root)
        self.assertEqual(ok_c, 0, ok_err)
        self.assertIn("目标句子", ok_out)

        src.write_text("第一章 开场\n这里是另外内容。\n", encoding="utf-8")
        _, (bad_c, bad_out, bad_err) = _exec_printed_command(out0, "回读首处", cwd=self.root)
        self.assertNotEqual(bad_c, 0)
        self.assertNotIn("另外内容", bad_out)
        self.assertIn("快照", bad_err)

        # 2. [P2] Both from_result_cmd (even saved_file=True) and read_span_cmd keep --state-dir;
        # continuation works even when default state dir is unwritable.
        custom_states = self.root / "my states (writable)"
        long_para = "长段落前缀。" + ("文字填充。" * 980)
        self.assertGreater(len(long_para), 4800)
        long_book = self.root / "long para.txt"
        long_book.write_text(f"第一章 极长\n{long_para}\n", encoding="utf-8")
        ov_saved = self.root / "long_ov.txt"

        blocked_env_base = self.root / "not_a_directory"
        blocked_env_base.write_text("blocker", encoding="utf-8")
        env_patch = {"LOCALAPPDATA": str(blocked_env_base), "XDG_STATE_HOME": str(blocked_env_base)}

        ov_c, ov_out, ov_err = _run_cli(
            ["--text", str(long_book), "--state-dir", str(custom_states), "--output", str(ov_saved),
             "overview", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(ov_c, 0, ov_err)
        self.assertIn("--state-dir", _extract_command(ov_out, "下一步"))
        self.assertIn("--state-dir", _extract_command(ov_saved.read_text(encoding="utf-8"), "回读"))

        f_long_c, f_long_out, f_long_err = _run_cli(
            ["--text", str(long_book), "--state-dir", str(custom_states), "find", "填充", "--examples", "0", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(f_long_c, 0, f_long_err)
        cmd_long = _extract_command(f_long_out, "回读首处")
        self.assertIn("--state-dir", cmd_long)
        with mock.patch.dict(os.environ, env_patch):
            # Execute printed command while default state dir is blocked -> still writes truncation state to custom_states!
            _, (r_long_c, r_long_out, r_long_err) = _exec_printed_command(f_long_out, "回读首处", cwd=self.root)
            self.assertEqual(r_long_c, 0, r_long_err)
            self.assertIn("续读：", r_long_out)
            self.assertIn("--state-dir", _extract_command(r_long_out, "续读"))
            _, (r_next_c, r_next_out, r_next_err) = _exec_printed_command(r_long_out, "续读", cwd=self.root)
            self.assertEqual(r_next_c, 0, r_next_err)
            self.assertIn("文字填充。", r_next_out)

        # 3. [P2] --spread points next page to --examples 0 --offset N; multi-query keeps per-group next_offset.
        spread_book = self.root / "spread.txt"
        spread_book.write_text(
            "第一章 一\n信号灯甲。\n信号灯乙。\n第二章 二\n信号灯丙。\n第三章 三\n信号灯丁。\n",
            encoding="utf-8",
        )
        sp_c, sp_out, sp_err = _run_cli(
            ["--text", str(spread_book), "find", "信号灯", "--spread", "--limit", "1", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(sp_c, 0, sp_err)
        self.assertIn("下一页坐标 --examples 0 --offset 1", sp_out)
        self.assertNotIn("| 下一页 --offset 1", sp_out)
        p2_c, p2_out, p2_err = _run_cli(
            ["--text", str(spread_book), "find", "信号灯", "--examples", "0", "--limit", "1", "--offset", "1", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(p2_c, 0, p2_err)
        self.assertIn("本页坐标：1=C0001:6-9", p2_out)

        mq_c, mq_out, mq_err = _run_cli(
            ["--text", str(spread_book), "find", "--as-of", "all", "--examples", "1", "--limit", "1", "--spread",
             "--query", '{"all":["信号灯"]}', "--query", '{"all":["甲"]}'],
            cwd=self.root,
        )
        self.assertEqual(mq_c, 0, mq_err)
        self.assertIn("下一页：Q1 设 offset=1（配 --examples 0）", mq_out)

        # 4. overview skips empty leading chapters for --pick and omits action if whole page is empty.
        empty_first = self.root / "empty_first.txt"
        empty_first.write_text("第一章 空章\n第二章 有内容\n真实正文段落。\n", encoding="utf-8")
        ef_out = self.root / "ef_ov.txt"
        ef_c, ef_stdout, ef_err = _run_cli(
            ["--text", str(empty_first), "--state-dir", str(custom_states), "--output", str(ef_out),
             "overview", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(ef_c, 0, ef_err)
        self.assertIn("--pick 2", _extract_command(ef_stdout, "下一步"))
        ef_file_txt = ef_out.read_text(encoding="utf-8")
        self.assertIn("--pick 2", _extract_command(ef_file_txt, "回读"))
        _, (ef_p_c, ef_p_out, ef_p_err) = _exec_printed_command(ef_stdout, "下一步", cwd=self.root)
        self.assertEqual(ef_p_c, 0, ef_p_err)
        self.assertIn("真实正文段落。", ef_p_out)

        all_empty = self.root / "all_empty_page.txt"
        all_empty.write_text("第一章 空一\n第二章 空二\n第三章 正篇\n终章有字。\n", encoding="utf-8")
        ae_out = self.root / "ae_ov.txt"
        ae_c, ae_stdout, ae_err = _run_cli(
            ["--text", str(all_empty), "--state-dir", str(custom_states), "--output", str(ae_out),
             "overview", "--as-of", "all", "--limit", "2"],
            cwd=self.root,
        )
        self.assertEqual(ae_c, 0, ae_err)
        self.assertIn("后续目录：--after-chapter 2", ae_stdout)
        self.assertNotIn("下一步：", ae_stdout)
        self.assertNotIn("回读", ae_out.read_text(encoding="utf-8"))

        # 5. read --sample diagnostic pagination explicitly points to overview.
        gap_book = self.root / "gap8.txt"
        gap_book.write_text(
            "".join(f"第{n * 2 - 1}章 跳\n段落{n}。\n" for n in range(1, 10)),
            encoding="utf-8",
        )
        gs_c, gs_out, gs_err = _run_cli(
            ["--text", str(gap_book), "read", "--sample", "2", "--as-of", "all", "--budget", "400"],
            cwd=self.root,
        )
        self.assertEqual(gs_c, 0, gs_err)
        self.assertIn("续看 overview --diagnostic-offset 2", gs_out)
        self.assertIn("JSON 下一页 overview --diagnostic-offset 6", gs_out)
        self.assertNotIn("续看 --diagnostic-offset", gs_out)

        # 6. Explicit --format json preserves --format json in receipt next_action.
        json_find_file = self.root / "find_out.json"
        jf_c, jf_stdout, jf_err = _run_cli(
            ["--text", str(spread_book), "--state-dir", str(custom_states), "--format", "json",
             "--output", str(json_find_file), "find", "信号灯", "--examples", "0", "--as-of", "all"],
            cwd=self.root,
        )
        self.assertEqual(jf_c, 0, jf_err)
        jf_receipt = json.loads(jf_stdout)
        self.assertIn("--format json", jf_receipt["next_action"])
        next_parts = shlex.split(jf_receipt["next_action"], posix=True)
        n_c, n_out, n_err = _run_cli(next_parts[1:], cwd=self.root)
        self.assertEqual(n_c, 0, n_err)
        self.assertIn("delivery", json.loads(n_out))

    def test_resolve_cli_prog_rules(self):
        fake_root = self.root / "prog_matrix"
        path_bin = fake_root / "path_bin"
        venv_bin = fake_root / "my venv" / "Scripts"
        other_py_bin = fake_root / "other_py"
        for d in (path_bin, venv_bin, other_py_bin):
            d.mkdir(parents=True)

        exe_suffix = ".exe" if os.name == "nt" else ""
        path_span = path_bin / f"span{exe_suffix}"
        venv_span = venv_bin / f"span{exe_suffix}"
        path_py = path_bin / f"python{exe_suffix}"
        other_py = other_py_bin / f"python{exe_suffix}"
        cli_mod = Path(cli.__file__)
        for f in (path_span, venv_span, path_py, other_py):
            f.write_text("", encoding="utf-8")

        def make_which(mapping):
            return lambda name: mapping.get(name)

        # Rule 1: invoked span matches PATH span -> short 'span'
        self.assertEqual(
            _resolve_cli_prog(str(path_span), executable=str(path_py), which=make_which({"span": str(path_span)}), cli_file=cli_mod),
            ("span",),
        )
        # Rule 2: unactivated venv launcher differs from PATH span -> launcher path
        self.assertEqual(
            _resolve_cli_prog(str(venv_span), executable=str(other_py), which=make_which({"span": str(path_span)}), cli_file=cli_mod),
            (str(venv_span),),
        )
        # Rule 3a: python -m chapspan.cli matches PATH python (even when PATH also has unrelated span) -> python -m chapspan.cli
        self.assertEqual(
            _resolve_cli_prog(
                str(cli_mod), executable=str(path_py),
                which=make_which({"python": str(path_py), "span": str(path_span)}),
                cli_file=cli_mod,
            ),
            ("python", "-m", "chapspan.cli"),
        )
        # Rule 3b: python -m chapspan.cli differs from PATH python -> explicit executable -m chapspan.cli
        self.assertEqual(
            _resolve_cli_prog(
                str(cli_mod), executable=str(other_py),
                which=make_which({"python": str(path_py)}),
                cli_file=cli_mod,
            ),
            (str(other_py), "-m", "chapspan.cli"),
        )

    def test_powershell_real_execution_tricky_paths_and_quoted_launcher(self):
        pwsh = shutil.which("pwsh")
        if os.name != "nt" or pwsh is None:
            self.skipTest("Requires Windows PowerShell 7 (pwsh) for real PS parser & execution check")

        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": src_dir, "PYTHONIOENCODING": "utf-8"}

        # 1. File with curly apostrophe (U+2019), leading '@', spaces, and parentheses, run via real python -m chapspan.cli subprocess.
        tricky_dir = self.root / "dir (ps)"
        tricky_dir.mkdir()
        curly_book = tricky_dir / "The Reader\u2019s Book.txt"
        curly_book.write_text("第一章 初章\n这里藏着重要线索A。\n", encoding="utf-8")
        at_book = tricky_dir / "@notes.txt"
        at_book.write_text("第一章 笔记\n这里记录@线索B。\n", encoding="utf-8")
        states = self.root / "ps states (’@)"

        for target_book, needle, check_text in ((curly_book, "线索A", "重要线索A"), (at_book, "线索B", "记录@线索B")):
            p1 = subprocess.run(
                [sys.executable, "-B", "-m", "chapspan.cli", "--text", str(target_book),
                 "--state-dir", str(states), "find", needle, "--examples", "0", "--as-of", "all"],
                cwd=self.root, env=env, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            printed_cmd = _extract_command(p1.stdout, "回读首处")
            self.assertIn("-m chapspan.cli", printed_cmd)
            p2 = subprocess.run(
                [pwsh, "-NoProfile", "-Command", printed_cmd],
                cwd=self.root, env=env, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertEqual(p2.returncode, 0, f"pwsh failed for {printed_cmd!r}:\n{p2.stderr}")
            self.assertIn(check_text, p2.stdout)

        # 2. Unactivated launcher in path with spaces & parentheses -> prints "& '...\span.ps1' ..." and executes in pwsh.
        launcher_dir = self.root / "span launcher (v1)"
        launcher_dir.mkdir()
        launcher_ps1 = launcher_dir / "span.ps1"
        escaped_py = sys.executable.replace("'", "''")
        launcher_ps1.write_text(f"& '{escaped_py}' -B -m chapspan.cli @args\n", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(
                ["--text", str(curly_book), "--state-dir", str(states), "find", "线索A", "--examples", "0", "--as-of", "all"],
                prog_prefix=(str(launcher_ps1),),
            )
        self.assertEqual(code, 0)
        launcher_cmd = _extract_command(buf.getvalue(), "回读首处")
        self.assertTrue(launcher_cmd.startswith("& '"), launcher_cmd)
        p3 = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launcher_cmd],
            cwd=self.root, env=env, capture_output=True, encoding="utf-8", check=False,
        )
        self.assertEqual(p3.returncode, 0, f"pwsh launcher failed for {launcher_cmd!r}:\n{p3.stderr}")
        self.assertIn("重要线索A", p3.stdout)


if __name__ == "__main__":
    unittest.main()
