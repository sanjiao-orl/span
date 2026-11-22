"""Command-line adapter for the shared application services."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from io import TextIOWrapper
from pathlib import Path

from . import __version__
from .display import build_command_context
from .help_topics import HELP_TOPICS, render_help_topic
from .material_index import generate_material_index
from .model import CONVENTION_FIELDS, MAX_CHAPTER_RANGE_SPAN, MAX_RESULT_BYTES, MAX_SELECTIONS
from .runs import RunRecorder, bind_run, json_bytes, resolve_run, run_write_lock
from .service import (
    ExecutionOutcome,
    TEXT_COMMANDS,
    commit_state,
    deliver_output,
    expand_research,
    init_research_run,
    prepare_output,
    run_operation,
    validate_args,
)


GLOBAL_VALUE_FLAGS = ("--library", "--text", "--run", "--state-dir", "--format", "--output")
TEXT_VALUE_FLAGS = ("--encoding", "--chapter-pattern", "--paragraphs", "--heading-level")
TEXT_BOOL_FLAGS = ("--single-chapter",)


def _single_value_flags(parser):
    return {
        opt
        for action in parser._actions
        if action.option_strings and action.nargs is None
        for opt in action.option_strings
    }


def _subparser_chain(root, head):
    parsers = [root]
    names = []
    current = root
    i = 0
    while i < len(head):
        token = head[i]
        value_flags = _single_value_flags(current)
        if token in value_flags:
            i += 2
            continue
        if any(token.startswith(flag + "=") for flag in value_flags):
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        subparsers = next(
            (action for action in current._actions if isinstance(action, argparse._SubParsersAction)),
            None,
        )
        if subparsers is not None and token in subparsers.choices:
            names.append(token)
            current = subparsers.choices[token]
            parsers.append(current)
            i += 1
            continue
        break
    return parsers, (names[0] if names else None)


class SpanArgumentParser(argparse.ArgumentParser):
    """ArgumentParser allowing global source and output flags before or after the subcommand."""

    def _hoist_global_flags(self, raw_args):
        argv = list(raw_args)
        dash_index = argv.index("--") if "--" in argv else len(argv)
        head, tail = argv[:dash_index], argv[dash_index:]

        parsers, subcommand = _subparser_chain(self, head)
        hoist_flags = set(GLOBAL_VALUE_FLAGS)
        hoist_bool_flags = set()
        if subcommand not in ("import", "import-txt"):
            hoist_flags |= set(TEXT_VALUE_FLAGS)
            hoist_bool_flags |= set(TEXT_BOOL_FLAGS)
        non_hoist_value_flags = set().union(*(_single_value_flags(parser) for parser in parsers)) - hoist_flags

        hoisted = []
        rest = []
        i = 0
        while i < len(head):
            token = head[i]
            if token in hoist_flags:
                if i + 1 >= len(head):
                    self.error(f"argument {token}: expected one argument")
                hoisted.extend((token, head[i + 1]))
                i += 2
            elif any(token.startswith(flag + "=") for flag in hoist_flags):
                hoisted.append(token)
                i += 1
            elif token in hoist_bool_flags:
                hoisted.append(token)
                i += 1
            elif token in non_hoist_value_flags and i + 1 < len(head):
                rest.extend((token, head[i + 1]))
                i += 2
            else:
                rest.append(token)
                i += 1
        return hoisted + rest + tail

    def parse_known_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        if self.prog == "span":
            args = self._hoist_global_flags(args)
            _, self._active_subcommand = _subparser_chain(self, args)
        return super().parse_known_args(args, namespace)

    def error(self, message):
        is_overview = self.prog == "span overview" or (
            self.prog == "span" and getattr(self, "_active_subcommand", None) == "overview"
        )
        if is_overview and "--offset" in message:
            message += "（提示：overview 目录翻页请用 --after-chapter 章序；诊断明细翻页请用 --diagnostic-offset）"
        super().error(message)


def default_state_directory():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Span" / "states"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "span" / "states"


def state_directory(args):
    if args.state_dir is not None:
        return args.state_dir
    if args.library is not None:
        return args.library / "states"
    return default_state_directory()


def text_view(args):
    if args.format is not None:
        return args.format == "text"
    return (args.command in TEXT_COMMANDS and args.text is not None
            and (args.output is None or args.output.suffix.casefold() != ".json"))


def boundary_arg(value):
    try:
        return int(value)
    except ValueError:
        return value.strip()


def chapter_arg(value):
    try:
        return int(value.removeprefix("C"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("章节参数须为 C0001 或正整数") from exc


CHAPTER_RANGE_RE = re.compile(r"^\s*(C?[0-9]+)\s*(?:-|–|—|\.\.)\s*(C?[0-9]+)\s*$")


def chapter_selection_arg(value):
    matched = CHAPTER_RANGE_RE.fullmatch(value)
    if matched is None:
        return chapter_arg(value)
    start = chapter_arg(matched.group(1))
    end = chapter_arg(matched.group(2))
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"章节区间须为正整数且起止顺序正确：{value}")
    if end - start + 1 > MAX_CHAPTER_RANGE_SPAN:
        raise argparse.ArgumentTypeError(
            f"单个 --chapter 区间宽度最多 {MAX_CHAPTER_RANGE_SPAN} 章"
            f"（单次读取展开后合计最多 {MAX_SELECTIONS} 个选区）：{value}"
        )
    return (start, end)


def query_group_arg(value):
    try:
        result = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "独立查询组须为 JSON 对象（例如 PowerShell：'--query ''{\"all\":[\"灯\"],\"near\":2}'''）"
        ) from exc
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("独立查询组须为 JSON 对象")
    return result


def add_scope(parser, *, from_document=False, from_result=False, required=True):
    if not from_document:
        parser.add_argument("--snapshot", required=required and not from_result, metavar="ID",
                            help="从 snapshots 或导入回执取得的快照 ID"
                            + ("；使用 --from-result 时可沿用" if from_result else ""))
    parser.add_argument("--as-of", type=boundary_arg,
                        required=required and not from_result and not from_document, metavar="末章",
                        help="允许读取的末章（含）。all 表示全书；--text 支持原书章号或章题，--library 支持内部章序或 C编号"
                        + ("；使用 --from-result 或 --run 时可沿用范围" if from_result or from_document else ""))
    parser.add_argument("--from-chapter", type=chapter_arg, default=None, metavar="章序",
                        help="收窄本次范围的起始章（支持 C0001 或正整数，默认 1；运行内沿用当前范围）")
    if from_result:
        parser.add_argument("--include-peripheral", action="store_const", const=True, default=None,
                            help="包含范围内已标记的外围文字；使用 --from-result 时默认沿用上一步设置")
    else:
        parser.add_argument("--include-peripheral", action="store_true", help="包含范围内已标记的外围文字")


def add_handoff(parser, *, pick=False):
    parser.add_argument("--from-result", metavar="文件或续读值",
                        help="使用 --output 保存的文本或 JSON 结果，也可用续读值；- 从标准输入读取结果")
    if pick:
        parser.add_argument("--pick", type=int, action="append", default=[], metavar="序号",
                            help="选择上一步结果的第几个条目，从 1 开始，可重复")


def add_output(parser, *, default=None):
    parser.add_argument("--format", choices=("json", "text"), default=default,
                        help="书库、resume 或 .json 输出文件默认 JSON，直接 TXT 的取材命令默认文本")
    parser.add_argument("--output", type=Path, metavar="文件", default=default,
                        help="按当前格式写入新的 UTF-8 文件并打印短回执，可用作下一步输入")


def build_parser():
    parser = SpanArgumentParser(
        prog="span",
        description=(
            "寻章 Span · 本地长篇小说研究工作台\n\n"
            "探索与取材（--text / --library / --run 均可用）：\n"
            "  overview    目录、节分类汇总、章界诊断与 --inspect 核查\n"
            "  find        字面词检索，支持布尔 --all/--any/--exclude、--near 与 --query\n"
            "  compare     在允许范围内无预设对比两个章节区间的显著变化词项\n"
            "  read        读取坐标/段落/整章、--sample 抽读、--group-budget 并读或续读\n\n"
            "书库与研究运行：\n"
            "  import      导入 TXT / Markdown / EPUB 建快照（别名 import-txt；加 --preview 先体检，不落库）\n"
            "  snapshots   列出书库版本与快照 ID\n"
            "  collect     在 --library 书库中批量收藏、列出或移除范围（不支持 --run）\n"
            "  research    创建或推进独立研究运行（init / expand / index）\n"
            "  resume      读取磁盘 Markdown 稿并检查原文引用状态（固定输出 JSON）\n\n"
            "内置帮助专题：\n"
            "  help        查看 quickstart / method / agent / json / run / citations / troubleshooting"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "快速开始\n"
            "  span --text 书.txt overview --as-of 第十二章 --toc\n"
            "  span --text 文稿.md --heading-level 2 overview --as-of all --toc\n"
            "  span --text 书.epub overview --as-of all --toc\n"
            "  span --text 书.txt read --as-of 第十二章 --sample 3\n"
            "  span --library 书库 snapshots\n"
            "  span help quickstart   # 安装后独立环境也可查看完整说明\n\n"
            "参数位置说明：--text / --library / --run / --format / --output / --state-dir 放在子命令前后均可；\n"
            "              --encoding / --chapter-pattern / --paragraphs / --heading-level / --single-chapter\n"
            "              用于 --text 或 import / import-txt（前后均可）。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="显示安装版本")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--library", type=Path, metavar="目录", help="已有书库目录（子命令前后均可）")
    source.add_argument("--text", type=Path, metavar="文件", help="直接读取 TXT、Markdown 或 EPUB，无需先建书库（子命令前后均可）")
    source.add_argument("--run", type=Path, metavar="运行目录", help="绑定 research init 返回的 run_directory")
    text_options = parser.add_argument_group("直接读取 TXT / Markdown / EPUB")
    text_options.add_argument("--encoding", dest="text_encoding", metavar="编码", help="TXT/Markdown 默认 UTF-8（兼容 BOM；不用于 EPUB）")
    text_options.add_argument("--chapter-pattern", dest="text_pattern", metavar="正则", help="匹配整个原书章标题（EPUB 传入时覆盖目录分章）")
    text_options.add_argument("--paragraphs", dest="text_paragraphs", choices=("lines", "blank-lines"),
                              help="默认每个非空行起一段")
    text_options.add_argument("--heading-level", dest="text_heading_level", type=int, metavar="层级",
                              help="Markdown ATX 分章标题层级（1–6；多层级 Markdown 须显式指定）")
    text_options.add_argument("--single-chapter", dest="text_single_chapter", action="store_true",
                              help="显式将整份文档作为单个正文章 C0001 读取（不与分章规则同用）")
    add_output(parser)
    parser.add_argument("--state-dir", type=Path, metavar="目录",
                        help="短状态柄保存与恢复目录；书库默认用 书库\\states，直接文件用系统用户状态目录")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    ingest = commands.add_parser(
        "import",
        aliases=["import-txt"],
        help="导入单个 TXT / Markdown / EPUB 并发布不可变快照（兼容别名 import-txt）",
        description="导入 TXT、Markdown 或 EPUB 到 --library 书库并返回快照 ID；加 --preview 时仅体检分章、短行样例与章长分布，不创建或修改书库。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span import 书.txt --preview\n"
            "  span import 文稿.md --preview\n"
            "  span import 书.epub --preview\n"
            "  span --library 书库 import 书.epub\n"
            "  span --library 书库 import-txt 文稿.md --heading-level 2\n"
            "  span --library 书库 import-txt 书.txt --encoding gb18030\n\n"
            "主要 JSON 字段：snapshot_id, created, chapters, unclassified_chapters, peripheral_chapters,\n"
            "               heading_count, tight_candidate_count, chapter_length_stats, parsing"
        ),
    )
    ingest.add_argument("source", type=Path, metavar="文件", help="源 TXT、Markdown 或 EPUB 文件路径")
    ingest.add_argument("--encoding", default=None, help="源文件编码，默认 UTF-8（兼容 BOM；不用于 EPUB；放子命令前后均可）")
    ingest.add_argument("--chapter-pattern", default=None, help="匹配整个去除首尾空白后的章标题行的正则（放子命令前后均可）")
    ingest.add_argument("--paragraphs", choices=("lines", "blank-lines"), default=None,
                        help="按非空行分段（默认），或按空行分隔自然段（放子命令前后均可）")
    ingest.add_argument("--heading-level", type=int, default=None, metavar="层级",
                        help="Markdown ATX 分章标题层级（1–6；放子命令前后均可）")
    ingest.add_argument("--single-chapter", action="store_true",
                        help="显式将整份文档作为单个正文章 C0001 导入（放子命令前后均可）")
    ingest.add_argument("--preview", action="store_true", help="仅查看分章体检、短行样例、Markdown 层级或 EPUB 目录/Spine 状态，不写入书库")

    snapshots = commands.add_parser(
        "snapshots",
        help="列出书库中的导入版本、来源文件名和快照 ID",
        description="查看 --library 书库已导入的全部不可变快照版本及解析设置，固定输出 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --library 书库 snapshots\n"
            "  span --library 书库 overview --snapshot <snapshot_id> --as-of 12 --toc\n\n"
            "主要 JSON 字段：snapshots[{snapshot_id, source_name, encoding, parsing}], next_after_snapshot"
        ),
    )
    snapshots.add_argument("--limit", type=int, default=20, help="每页版本数（1–100，默认 20）")
    snapshots.add_argument("--after-snapshot", default="", metavar="ID", help="使用上一页的 next_after_snapshot")

    overview = commands.add_parser(
        "overview",
        help="列出当前范围内的章目和起读入口",
        description="查看允许范围内的章目、正篇/未分类/外围分类汇总与章界诊断，或用 --inspect 核查指定章的来源行。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt overview --as-of 第十二章 --toc\n"
            "  span --text 书.txt overview --as-of 12 --title '城门'\n"
            "  span --text 书.txt overview --as-of 12 --inspect C0002 --line 35\n\n"
            "主要 JSON 字段：chapters[{chapter, title, kind, first_paragraph, source_body_line, paragraph_count, characters}],\n"
            "               chapter_kind_counts, next_after_chapter, chapter_diagnostics"
        ),
    )
    add_output(overview, default=argparse.SUPPRESS)
    add_scope(overview, from_result=True)
    add_handoff(overview)
    overview.add_argument("--toc", action="store_true", help="轻量章目（默认每页最多 2000 条，另受输出大小限制）")
    overview.add_argument("--title", metavar="词", help="按章题包含的字面词筛选，保留同名章目")
    overview.add_argument("--limit", type=int, help="每页章目数（普通目录默认 20，--toc 默认 2000，上限 2000 且受输出体积保护）")
    overview.add_argument("--after-chapter", type=int, default=0, help="使用上一页的 next_after_chapter")
    overview.add_argument("--diagnostic-offset", type=int, default=0, metavar="起点",
                          help="使用 chapter_diagnostics.next_diagnostic_offset 翻看诊断明细")
    overview.add_argument("--inspect", type=chapter_arg, metavar="C0001", help="就地核查该章的来源行与解析设置")
    overview.add_argument("--line", type=int, metavar="来源行", help="核查该章内的指定来源行，需与 --inspect 同用")

    find = commands.add_parser(
        "find",
        help="查字面词，按章或邻近段落做布尔组合（全部／任一／排除）",
        description="查询正文中的字面词。组合条件默认逐章判断，加 --near 后在同章的连续段落内判断。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例（支持 --text / --library / --run）\n"
            "  span --text 书.txt find '城门' '灯' --near 2 --as-of 第十二章\n"
            "  span --library 书库 find --any '铜钥匙' --any '青铜匙' --snapshot ID --as-of 12\n"
            "  span --text 书.txt find '城门' --exclude '雨' --spread --as-of 12 --output 城门.txt\n"
            "  # 多组查询（PowerShell 单引号包 JSON）：\n"
            "  span --text 书.txt find --as-of 12 --examples 1 --query '{\"all\":[\"灯\"]}' --query '{\"any\":[\"钥匙\"]}'\n\n"
            "主要 JSON 字段：term_counts, match_count, hit_chapter_count, matches[].read_spans,\n"
            "               examples.fragments[].read_span, next_offset（多组返回 queries[]）\n"
            "命中章视图：hit_chapters[].read_span, next_distribution_offset"
        ),
    )
    add_output(find, default=argparse.SUPPRESS)
    add_scope(find, from_result=True)
    add_handoff(find)
    find.add_argument("terms", nargs="*", metavar="词", help="全部必含的字面词（AND）")
    find.add_argument("--query", dest="query_groups", type=query_group_arg, action="append", default=[],
                      metavar="JSON", help='独立查询组，可重复；如 {"all":["灯"],"near":2,"offset":0}')
    conditions = find.add_argument_group("查询条件（至少一个正向词）")
    conditions.add_argument("--all", dest="all_terms", action="append", default=[], metavar="词",
                            help="追加必含词，可重复（AND）")
    conditions.add_argument("--any", dest="any_terms", action="append", default=[], metavar="词",
                            help="可重复，同组至少命中一个（OR）")
    conditions.add_argument("--exclude", action="append", default=[], metavar="词",
                            help="可重复，任一排除词出现即排除该章或窗口（NOT）")
    conditions.add_argument("--near", type=int, nargs="?", const=6, metavar="段数",
                            help="所有条件改在同章的连续段落窗口内判定，只写 --near 时为 6 段")
    paging = find.add_argument_group("匹配与分布分页")
    paging.add_argument("--limit", type=int, default=20, help="匹配、分布或命中章的页大小（1–100，默认 20）")
    paging.add_argument("--offset", type=int, default=0, help="匹配分页起点，使用 next_offset")
    paging.add_argument("--distribution-offset", type=int, default=0,
                        help="分布或命中章的分页起点，使用 next_distribution_offset")
    paging.add_argument("--compact", action="store_true", help="保留查询统计和回读坐标，省略分布和词面位置明细")
    paging.add_argument("--hit-chapters", action="store_true", help="只列有匹配的章、各章次数和一处回读位置，不展开例段")
    preview = find.add_argument_group("原文预览")
    preview.add_argument("--examples", type=int,
                         help="展开 N 项（0 到 10），直接读 TXT 默认 3，书库、多组查询和命中章视图默认 0")
    preview.add_argument("--spread", action="store_true", help="按命中章序等距取例，默认从 3 个章各取一处")
    preview.add_argument("--distribution", action="store_true", help="文本按可见章位列分布，连续零值合并显示")
    preview.add_argument("--budget", type=int, default=4000, help="例段共享正文字符预算，1–24000，默认 4000")

    compare_cmd = commands.add_parser(
        "compare",
        help="在允许范围内对比两个章节区间的差异词项或浏览基础候选",
        description=(
            "对比当前允许范围内的两个章节区间：默认 contrast 视图用长度归一率与对照分数 G2 发现显著差异词项；\n"
            "加 --view base 可跳过包含抑制、G2 过滤与重叠屏蔽，按原始出现浏览基础候选（支持 --sort / --contains / --offset）。\n"
            "默认仅返回统计与代表坐标（不耗正文预算），可用 --examples 或后续 read --from-result ... --pick N 并读。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt compare --left 1-100 --right 101-200 --as-of 200 --min-support 5 --min-chapters 2\n"
            "  span --text 书.txt compare --left 1-100 --right 101-200 --as-of 200 --view base --sort total --contains 骑士\n"
            "  span --text 书.txt compare --left 1-100 --right 101-200 --as-of 200 --explain 骑士 --explain AWSL\n"
            "  span --text 书.txt read --from-result state_... --pick 1 --group-budget 1200 --before 1 --after 1\n"
            "  span --run 运行目录 compare --left 1-100 --right 101-200 --examples 2 --budget 1200\n\n"
            "主要 JSON 字段：left_interval, right_interval, candidate_count, next_offset,\n"
            "               candidates[{rank, term, trend, g2_score, left, right, read_spans}], explanations[], reading"
        ),
    )
    add_output(compare_cmd, default=argparse.SUPPRESS)
    add_scope(compare_cmd, from_result=True)
    add_handoff(compare_cmd)
    compare_cmd.add_argument("--left", required=True, metavar="起-止", help="左区间（如 1-100、C0001-C0100、第一章:第五十章）")
    compare_cmd.add_argument("--right", required=True, metavar="起-止", help="右区间（如 101-200、C0101-C0200）")
    compare_cmd.add_argument("--view", choices=("contrast", "base"), default="contrast",
                             help="查看模式：contrast（默认差异精选与去重）或 base（精选前基础候选，按原始统计浏览）")
    compare_cmd.add_argument("--sort", choices=("total", "g2", "left", "right", "delta"), default=None,
                             help="base 视图排序：total（总次数，默认）、g2、left（左次数）、right（右次数）、delta（出现率差）")
    compare_cmd.add_argument("--contains", metavar="词", default=None,
                             help="仅在 base 视图保留包含该字面词的候选")
    compare_cmd.add_argument("--explain", action="append", default=None, metavar="词",
                             help="复核指定词在当前 contrast 筛选各阶段的去向，可重复（最多 16 个）")
    compare_cmd.add_argument("--offset", type=int, default=0, help="候选分页起点，配合 next_offset 使用（默认 0）")
    compare_cmd.add_argument("--limit", type=int, default=20, help="最多返回的词条数（1–100，默认 20）")
    compare_cmd.add_argument("--min-support", type=int, default=3, help="强势侧最少出现次数（默认 3）")
    compare_cmd.add_argument("--min-chapters", type=int, default=1, help="强势侧最少命中章数（默认 1）")
    compare_cmd.add_argument("--min-len", type=int, default=2, help="最短词长，默认 2")
    compare_cmd.add_argument("--max-len", type=int, default=8, help="最长词长，默认 8")
    compare_cmd.add_argument("--direction", choices=("all", "right", "left"), default="all",
                             help="差异方向：all（双向）、right（右升/右现）、left（左高/左现），默认 all")
    compare_cmd.add_argument("--examples", type=int, default=0,
                             help="为前 N 个候选同时取回左右代表段落（0–10，默认 0 不读正文）")
    compare_cmd.add_argument("--budget", type=int, default=4000, help="配合 --examples 使用的总正文字符预算，默认 4000")
    compare_cmd.add_argument("--group-budget", type=int, help="配合 --examples 使用时限制每组代表段落预算上限")

    read = commands.add_parser(
        "read",
        help="合并、扩读、分组并读或续读多个原文范围",
        description="按字符范围、段落、整章或收藏回读原文；多选区默认合并顺序交付，比较任务请用 --group-budget 分组并读。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt read --as-of 第十二章 --sample 3\n"
            "  span --text 书.txt read --chapter C0002 --chapter C0008 --group-budget 2000 --budget 4000 --as-of 12\n"
            "  span --text 书.txt read --from-result state_... [--pick 1] [--continue-group 2]\n"
            "  span --text 书.txt read --from-result find.txt --example 1 --before 1 --after 2\n\n"
            "主要 JSON 字段：fragments[].read_span, source_citation, read_characters, complete,\n"
            "               delivery[], continuation（使用 --group-budget 时正文见 passages[]）"
        ),
    )
    add_output(read, default=argparse.SUPPRESS)
    add_scope(read, from_result=True)
    add_handoff(read, pick=True)
    read.add_argument("spans", nargs="*", metavar="范围", help="C0001:0-10 或 [@C0001:0-10]，可传多个")
    read.add_argument("--chapter", type=chapter_selection_arg, action="append", default=[], metavar="C0001",
                      help=f"选择整章或章序区间（如 C0002 或 C0020-C0023），可重复；"
                           f"单个区间宽度 ≤ {MAX_CHAPTER_RANGE_SPAN} 章，单次读取展开后合计 ≤ {MAX_SELECTIONS} 个选区；"
                           "超出本页预算时仅返回部分，需续读")
    read.add_argument("--sample", type=int, metavar="章数",
                      help="在可读正文章中等距选章，按独立预算取章首；用 --after 指定只取前若干段")
    read.add_argument("--paragraph", action="append", default=[], metavar="C0001:1-3", help="按段读取，含首尾段")
    read.add_argument("--item", type=int, action="append", default=[], metavar="ID", help="读收藏范围（仅 --library），可重复")
    read.add_argument("--as-matches", action="store_true", help="将位置参数保留为查询匹配范围")
    read.add_argument("--example", action="append", default=[], metavar="序号",
                      help="单组用数字；多组用 Q1E2；区别于当前匹配页的 --pick，可重复")
    read.add_argument("--whole-paragraphs", action="store_true", help="读取选区所及的完整段落")
    read.add_argument("--before", type=int, metavar="段数", help="向前扩读段数")
    read.add_argument("--after", type=int, metavar="段数", help="向后扩读段数")
    read.add_argument("--budget", type=int, help="本批正文字符总预算，1–24000，默认 4000；大结果可保存后按项查看")
    read.add_argument("--group-budget", type=int,
                      help="按各组独立分页并读并指定每组硬上限（均分仅为起始分配，未用余量会回收给其余未完组，不突破此上限）")
    read.add_argument("--continue-group", action="append", default=[], metavar="组号",
                      help="只续指定待续组；普通分组用数字组号，多组查询用 Q1 或 Q1E1，可重复")
    read.add_argument("--resume", metavar="C0001:10",
                      help="旧式续读：保留原选区与扩读参数，并传入 next；也可直接 --from-result 上一页.json")

    collect = commands.add_parser(
        "collect",
        help="在 --library 书库中批量收藏、查看或移除已选范围",
        description="管理 --library 书库中的持久收藏集（固定输出 JSON；仅限 --library 模式，不支持 --text 或 --run）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --library 书库 collect add '城门专题' C0002:0-20 --snapshot ID --as-of 12 --note '批注'\n"
            "  span --library 书库 collect add '城门专题' --from-result 回读.json --pick 1 --snapshot ID --as-of 12\n"
            "  span --library 书库 collect list '城门专题' --snapshot ID --as-of 12\n"
            "  span --library 书库 collect remove 1 2 --snapshot ID --as-of 12"
        ),
    )
    actions = collect.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="加入收藏集，重复范围保留已有记录")
    add_scope(add, from_result=True)
    add_handoff(add, pick=True)
    add.add_argument("collection", metavar="收藏集", help="目标收藏集名称")
    add.add_argument("spans", nargs="*", metavar="C0001:0-10", help="字符范围；也可从回读或单组命中章结果选取范围")
    add.add_argument("--note", help="为这批新收藏附上批注")
    listing = actions.add_parser("list", help="列出当前范围内的收藏")
    add_scope(listing, required=False)
    listing.add_argument("collection", metavar="收藏集", help="目标收藏集名称")
    listing.add_argument("--after-id", type=int, default=0, help="使用上一页的 next_after_id")
    listing.add_argument("--limit", type=int, default=20, help="每页收藏条目数（1–100，默认 20）")
    remove = actions.add_parser("remove", help="按收藏 ID 移除")
    add_scope(remove, required=False)
    remove.add_argument("item_ids", type=int, nargs="+", metavar="ID", help="collect list 返回的整数收藏 ID")

    resume = commands.add_parser(
        "resume",
        help="读取磁盘当前稿并检查原文引用",
        description="校验 Markdown 稿件与原文坐标状态（固定输出 JSON；需保存至文件可用 --output）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt resume 研究稿.md --as-of 第十二章 --citations-only\n"
            "  span --run 运行目录 resume   # 默认读取 <run_directory>\\当前稿.md 与当前范围\n\n"
            "主要 JSON 字段：scope, collection_id, citations[{citation, status, note}], markdown"
        ),
    )
    resume.add_argument("--format", choices=("json", "text"), default=argparse.SUPPRESS,
                        help="resume 固定输出 JSON；传 --format text 将报错")
    resume.add_argument("--output", type=Path, metavar="文件", default=argparse.SUPPRESS,
                        help="将校验 JSON 写入新的 UTF-8 文件并打印短回执")
    resume.add_argument("draft", type=Path, nargs="?", metavar="Markdown",
                        help="当前稿路径；绑定运行时默认读取运行目录中的 当前稿.md")
    resume.add_argument("--draft-horizon", type=int, metavar="章序",
                        help="在原运行内检查无 YAML 稿头的报告时，显式确认稿件形成进度")
    resume.add_argument("--citations-only", action="store_true", help="只返回引用检查与范围，不重复稿件正文")
    resume.add_argument("--include-inline-citations", action="store_true", help="也检查单独包在反引号内的引用坐标")
    add_scope(resume, from_document=True)

    research = commands.add_parser(
        "research",
        help="创建或推进独立研究运行",
        description=(
            "管理独立研究运行目录（init / expand / index，固定输出 JSON）。\n"
            "init 会在 <任务目录>\\runs\\run_... 创建运行；后续 --run 需传回执中的 run_directory。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt research init D:\\研究\\任务 --as-of 第十二章\n"
            "  span --run D:\\研究\\任务\\runs\\run_... research expand --as-of 20\n"
            "  span --run D:\\研究\\任务\\runs\\run_... research index --draft 报告.md --section 36"
        ),
    )
    actions = research.add_subparsers(dest="action", required=True)
    initialize = actions.add_parser(
        "init",
        help="创建固定来源、快照与初始范围的运行",
        description="在 <任务目录>\\runs\\ 下新建独立运行目录，并在回执返回 run_directory 与 draft_header。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --text 书.txt research init D:\\研究\\任务 --as-of 第十二章\n"
            "  span --library 书库 research init D:\\研究\\任务 --snapshot ID --as-of 12"
        ),
    )
    initialize.add_argument("task_directory", type=Path, metavar="任务目录", help="放置 runs\\run_... 的上层任务目录")
    initialize.add_argument("--snapshot", metavar="ID", help="书库初始化时显选快照；直接 TXT 自动核对")
    initialize.add_argument("--as-of", type=boundary_arg, required=True, metavar="末章",
                            help="初始允许读取的末章；all 表示全书，TXT 支持原书章号或章题，书库支持内部章序或 C编号")
    initialize.add_argument("--from-chapter", type=chapter_arg, default=1, metavar="章序",
                            help="初始允许的起始章（正整数或 C编号，默认 1）")
    initialize.add_argument("--include-peripheral", action="store_true", help="初始范围即包含外围章")
    initialize.add_argument("--purpose", metavar="文本", help="可选：本次研究或取材用途（<= 500 字）")
    initialize.add_argument("--audience", metavar="文本", help="可选：目标读者或受众（<= 500 字）")
    initialize.add_argument("--evaluation-framework", dest="evaluation_framework", metavar="文本",
                            help="可选：评价框架或分析角度（<= 500 字）")
    initialize.add_argument("--external-policy", dest="external_policy", metavar="文本",
                            help="可选：外部资料与联网策略（<= 500 字）")
    initialize.add_argument("--version-note", dest="version_note", metavar="文本",
                            help="可选：底本、版本或连载说明（<= 500 字）")
    expand = actions.add_parser(
        "expand",
        help="显式扩大运行当前范围并保留历史",
        description="只允许扩大 --run 绑定的当前范围（后移 --as-of、提前 --from-chapter 或启用 --include-peripheral）。",
    )
    expand.add_argument("--as-of", type=boundary_arg, metavar="末章", help="扩大后的允许末章")
    expand.add_argument("--from-chapter", type=chapter_arg, metavar="章序",
                        help="提前后的起始章（正整数或 C编号，须不大于当前起始章）")
    expand.add_argument("--include-peripheral", action="store_true", help="将范围内的外围章纳入当前运行")
    index = actions.add_parser(
        "index",
        help="重建材料索引，或按稿件标题行号只看一节引用",
        description="依据 calls.jsonl 与规范结果核对当前稿引文，生成 材料索引.md；加 --section 行号 时只返回局部 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            "  span --run D:\\研究\\任务\\runs\\run_... research index\n"
            "  span --run D:\\研究\\任务\\runs\\run_... research index --draft 报告.md --section 36"
        ),
    )
    index.add_argument("--draft", type=Path, metavar="Markdown", help="显式选择报告；默认使用运行内的 当前稿.md")
    index.add_argument("--draft-horizon", type=int, metavar="章序",
                       help="检查无 YAML 稿头的报告时，显式确认稿件形成进度")
    index.add_argument("--section", type=int, metavar="行号",
                       help="只返回稿件中该 1-based 整数行号对应的 ATX # 标题及子节引用，不重建文件")
    index.add_argument("--offset", type=int, metavar="引用偏移", help="按 --section 的 next_offset 续看")
    index.add_argument("--include-inline-citations", action="store_true", help="也索引单独包在反引号内的引用坐标")

    help_cmd = commands.add_parser(
        "help",
        help="查看内置使用专题（quickstart / method / agent / json / run / citations / troubleshooting）",
        description="脱离源码仓库，直接查看安装版自带的操作指南、JSON 字段与排错专题。",
    )
    help_cmd.add_argument("topic", nargs="?", metavar="专题名",
                          help="可选：" + "、".join(HELP_TOPICS))
    return parser


def run(args):
    def set_scope(scope):
        args._actual_scope = scope

    outcome = run_operation(args, state_dir=state_directory(args), on_scope=set_scope)
    args._actual_scope = outcome.scope
    return outcome.result


def execute_research(args, recorder):
    if args.action == "init":
        return init_research_run(
            args.task_directory, as_of=args.as_of, from_chapter=args.from_chapter,
            include_peripheral=args.include_peripheral, library=args.library, snapshot=args.snapshot,
            text=args.text, text_encoding=args.text_encoding, text_pattern=args.text_pattern,
            text_paragraphs=args.text_paragraphs, text_heading_level=args.text_heading_level,
            text_single_chapter=args.text_single_chapter,
            conventions={field: getattr(args, field) for field in CONVENTION_FIELDS},
        )
    if recorder is None:
        raise ValueError(f"research {args.action} 需要 --run、SPAN_RUN_DIR 或运行目录内的 cwd")
    if args.action == "expand":
        return expand_research(recorder, as_of=args.as_of, from_chapter=args.from_chapter,
                               include_peripheral=args.include_peripheral)
    result = generate_material_index(
        recorder.run, recorder.current_scope, draft=args.draft, draft_horizon=args.draft_horizon,
        section=args.section, offset=0 if args.offset is None else args.offset,
        include_inline=args.include_inline_citations,
    )
    return ExecutionOutcome(result, recorder.current_scope)


def _same_executable(path_a, path_b):
    if not path_a or not path_b:
        return False
    try:
        return os.path.samefile(path_a, path_b)
    except OSError:
        return Path(path_a).resolve() == Path(path_b).resolve()


def _resolve_launcher_file(argv0, *, which=shutil.which):
    if not argv0:
        return None
    candidate = Path(argv0)
    if candidate.exists():
        return str(candidate)
    if os.name == "nt" and not candidate.suffix:
        exe_candidate = candidate.with_suffix(".exe")
        if exe_candidate.exists():
            return str(exe_candidate)
    if not candidate.is_absolute() and os.sep not in str(argv0) and "/" not in str(argv0):
        return which(str(argv0))
    return None


def _resolve_cli_prog(argv0=None, *, executable=None, which=shutil.which, cli_file=None):
    argv0 = sys.argv[0] if argv0 is None and sys.argv else argv0
    py_exe = sys.executable if executable is None else executable
    cli_path = Path(__file__) if cli_file is None else Path(cli_file)
    launcher = _resolve_launcher_file(argv0, which=which)
    if launcher is not None and _same_executable(launcher, cli_path):
        if _same_executable(which("python"), py_exe):
            return ("python", "-m", "chapspan.cli")
        return (str(py_exe), "-m", "chapspan.cli")
    if launcher is not None:
        if _same_executable(which("span"), launcher):
            return ("span",)
        return (str(launcher),)
    return ("span",)


def _capture_invocation(args, *, prog_prefix=("span",)):
    return {
        "prog_prefix": tuple(prog_prefix),
        "run": getattr(args, "run", None),
        "text": getattr(args, "text", None),
        "library": getattr(args, "library", None),
        "state_dir": getattr(args, "state_dir", None),
        "format": getattr(args, "format", None),
        "text_encoding": getattr(args, "text_encoding", None),
        "text_pattern": getattr(args, "text_pattern", None),
        "text_paragraphs": getattr(args, "text_paragraphs", None),
        "text_heading_level": getattr(args, "text_heading_level", None),
        "text_single_chapter": bool(getattr(args, "text_single_chapter", False)),
    }


def execute_command(args, recorder=None, status=None, raw_invocation=None):
    indexing = args.command == "research" and args.action == "index"
    run_scope = recorder.current_scope if recorder is not None else None

    def set_scope(scope):
        args._actual_scope = scope
        if status is not None:
            status["scope"] = scope

    if args.command == "research":
        outcome = execute_research(args, recorder)
    else:
        run_conventions = recorder.run.conventions if recorder is not None else None
        outcome = run_operation(
            args,
            state_dir=state_directory(args),
            run_scope=run_scope,
            run_conventions=run_conventions,
            on_scope=set_scope,
        )
        if args.command == "resume" and len(json_bytes(outcome.result)) > MAX_RESULT_BYTES:
            advice = ("请用 --citations-only 省去正文" if not args.citations_only
                      else "请在运行内使用 research index --section 按节查看，或拆分稿件")
            raise ValueError(f"稿件恢复结果超过输出上限，{advice}")
    set_scope(outcome.scope)
    if status is not None:
        status["committed"] = outcome.record_committed
    result = outcome.result
    if recorder is not None and not indexing and not outcome.record_committed:
        recorder.save_result(result)
    if status is not None:
        status["stage"] = "render"
    is_text = text_view(args)
    distribution = not getattr(args, "hit_chapters", False) and (
        getattr(args, "distribution", False) or bool(getattr(args, "distribution_offset", 0))
    )
    cmd_ctx = build_command_context(
        raw_invocation,
        outcome.scope,
        run_scope=run_scope,
        in_run=(recorder is not None),
        text_options=result.get("text_options") if isinstance(result, dict) else None,
    )
    prepared = prepare_output(
        result, is_text=is_text, output_path=args.output, distribution=distribution, command_context=cmd_ctx,
    )
    state_dir = state_directory(args)
    if args.output is not None:
        if status is not None:
            status["stage"] = "output"
        payload = deliver_output(
            args.output, result, prepared, is_text=is_text, state_dir=state_dir, command_context=cmd_ctx,
        )
        state_id = prepared.state_id
    else:
        state_id = commit_state(prepared, state_dir)
        payload = prepared.payload
    if recorder is not None and not outcome.record_committed:
        if status is not None:
            status["stage"] = "record"
        recorder.call(args, status="success", scope=outcome.scope, state_id=state_id)
        if status is not None:
            status["committed"] = True
    return payload


def _format_cli_error(exc, log_error=None):
    if isinstance(exc, FileExistsError):
        message = f"span: 目标已存在，请为 --output 选择新文件：{exc.filename}"
    else:
        message = f"span: {exc}"
    if log_error is not None:
        message += f"\nspan: 失败日志写入失败：{log_error}"
    return f"{message}\n"


def main(argv=None, *, prog_prefix=None):
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    effective_prog = (
        tuple(prog_prefix)
        if prog_prefix is not None
        else (_resolve_cli_prog() if argv is None else ("span",))
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help":
        try:
            print(render_help_topic(args.topic))
        except ValueError as exc:
            parser.exit(1, f"span: {exc}\n")
        return 0
    status = {"stage": "bind", "scope": None, "committed": False}
    raw_invocation = _capture_invocation(args, prog_prefix=effective_prog)
    try:
        run_info = resolve_run(args)
        if run_info is None:
            validate_args(args, is_text=text_view(args))
            status["stage"] = "execute"
            payload = execute_command(args, status=status, raw_invocation=raw_invocation)
        else:
            with run_write_lock(run_info):
                recorder = RunRecorder(run_info)
                try:
                    status["stage"] = "bind"
                    if args.command != "research":
                        bind_run(args, run_info, recorder.current_scope)
                    validate_args(args, is_text=text_view(args))
                    status["stage"] = "execute"
                    payload = execute_command(args, recorder, status=status, raw_invocation=raw_invocation)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    log_error = None
                    if not status["committed"]:
                        try:
                            recorder.call(
                                args, status="failure", scope=status["scope"],
                                state_id=None, failed_stage=status["stage"], error=exc,
                            )
                            status["committed"] = True
                        except (OSError, ValueError, sqlite3.Error) as logging_exc:
                            log_error = logging_exc
                    parser.exit(1, _format_cli_error(exc, log_error))
        print(payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, _format_cli_error(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
