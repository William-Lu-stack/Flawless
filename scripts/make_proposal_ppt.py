#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a persuasion deck: CISRE as the internal SRE harness / unified ops platform.

Pure-stdlib PPTX writer (OpenXML + zip). No third-party dependency.
"""
from __future__ import annotations

import html
import os
import zipfile
from xml.sax.saxutils import escape

# ---------- theme ----------
NAVY   = "0B2447"
NAVY2  = "14366B"
ACCENT = "2E86DE"
CYAN   = "00A8CC"
TEAL   = "00A8A8"
GRAY   = "5B6770"
LIGHT  = "F1F5FA"
WHITE  = "FFFFFF"
GREEN  = "1E8449"
RED    = "C0392B"
AMBER  = "B9770E"

LATIN = "Segoe UI"
EA    = "Microsoft YaHei"

EMU_IN = 914400
SLIDE_W = 12192000   # 13.333 in
SLIDE_H = 6858000    # 7.5 in


def _esc(s: str) -> str:
    return escape(str(s), {'"': "&quot;", "'": "&apos;"})


def _run(text: str, *, sz: int = 1800, bold: bool = False, color: str = "263238",
         italic: bool = False, font: str = LATIN) -> str:
    return (
        "<a:r>"
        f"<a:rPr lang=\"zh-CN\" altLang=\"en-US\" sz=\"{sz}\" "
        f"b=\"{int(bold)}\" i=\"{int(italic)}\" dirty=\"0\">"
        f"<a:solidFill><a:srgbClr val=\"{color}\"/></a:solidFill>"
        f"<a:latin typeface=\"{font}\"/><a:ea typeface=\"{EA}\"/>"
        "</a:rPr>"
        f"<a:t>{_esc(text)}</a:t>"
        "</a:r>"
    )


def _para(runs_xml: str, *, align: str = "l", spc_before: int = 0, spc_after: int = 0,
          line: int = 100, bullet: str = "") -> str:
    bu = ""
    if bullet:
        bu = ("<a:buFont typeface=\"Arial\"/><a:buChar char=\"" + bullet + "\"/>")
    return (
        "<a:p>"
        f"<a:pPr algn=\"{align}\" spcBef=\"{spc_before}\" spcAft=\"{spc_after}\">"
        f"<a:spcBef><a:spcPts val=\"{spc_before}\"/></a:spcBef>"
        f"<a:spcAft><a:spcPts val=\"{spc_after}\"/></a:spcAft>"
        f"<a:lnSpc><a:spcPct val=\"{line * 1000}\"/></a:lnSpc>"
        f"{bu}"
        "</a:pPr>"
        f"{runs_xml}"
        "</a:p>"
    )


def _txbox(x: int, y: int, w: int, h: int, paras: str, *, anchor: str = "t",
           wrap: bool = True) -> str:
    body_pr = (f'<a:bodyPr wrap="{"square" if wrap else "none"}" rtlCol="0" '
               f'anchor="{anchor}"><a:normAutofit/></a:bodyPr>')
    return (
        "<p:sp>"
        "<p:nvSpPr><p:cNvPr id=\"0\" name=\"tx\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        "<p:spPr><a:xfrm>"
        f"<a:off x=\"{x}\" y=\"{y}\"/><a:ext cx=\"{w}\" cy=\"{h}\"/>"
        "</a:xfrm></p:spPr>"
        f"<p:txBody>{body_pr}<a:lstStyle/>{paras}</p:txBody>"
        "</p:sp>"
    )


def _rect(x: int, y: int, w: int, h: int, fill: str, *, line: str = "") -> str:
    ln = f'<a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>' if line else "<a:ln><a:noFill/></a:ln>"
    return (
        "<p:sp><p:nvSpPr><p:cNvPr id=\"0\" name=\"rect\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        "<p:spPr><a:xfrm>"
        f"<a:off x=\"{x}\" y=\"{y}\"/><a:ext cx=\"{w}\" cy=\"{h}\"/>"
        "</a:xfrm>"
        f"<a:solidFill><a:srgbClr val=\"{fill}\"/></a:solidFill>{ln}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody>"
        "</p:sp>"
    )


def _slides_xml(slides: list[str]) -> tuple[str, str]:
    parts = []
    rels = []
    for i, body in enumerate(slides, start=1):
        parts.append(f"<p:sld>{body}</p:sld>")
        rels.append(
            f"<Relationship Id=\"rId{i}\" "
            "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide\" "
            f"Target=\"slides/slide{i}.xml\"/>"
        )
    return "".join(parts), "".join(rels)


def _slide(content: str) -> str:
    return (
        "<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">"
        "<p:cSld><p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
        "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
        f"{content}"
        "</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping/></p:clrMapOvr></p:sld>"
    )


# ---------- deck content builders ----------

def footer(page: int, total: int) -> str:
    s = (
        _rect(0, SLIDE_H - 300000, SLIDE_W, 300000, LIGHT)
        + _txbox(500000, SLIDE_H - 260000, 8000000, 220000,
                 _para(_run("CISRE · 企业统一运维平台 · 内部 SRE Harness", sz=1000, color=GRAY)),
                 anchor="ctr")
        + _txbox(SLIDE_W - 1200000, SLIDE_H - 260000, 700000, 220000,
                 _para(_run(f"{page} / {total}", sz=1000, color=GRAY), align="r"), anchor="ctr")
    )
    return s


def content_head(kicker: str, title: str) -> str:
    return (
        _rect(0, 0, SLIDE_W, 900000, WHITE)
        + _rect(0, 0, 160000, 900000, ACCENT)
        + _txbox(520000, 120000, 11000000, 420000,
                 _para(_run(kicker.upper(), sz=1200, bold=True, color=ACCENT), spc_after=40000))
        + _txbox(520000, 420000, 11200000, 460000,
                 _para(_run(title, sz=3000, bold=True, color=NAVY)))
        + _rect(520000, 880000, 1300000, 50000, CYAN)
    )


def bullets(items: list[tuple[str, int]], x: int, y: int, w: int, *,
            size: int = 1700, gap: int = 140000, line: int = 112,
            color: str = "263238", bullet_char: str = "\u2022", accent: str = ACCENT) -> str:
    paras = []
    for text, indent in items:
        prefix = ""
        if indent == 0:
            prefix = bullet_char + "  "
            run = _run(prefix, sz=size, bold=True, color=accent) + _run(text, sz=size, color=color)
        else:
            prefix = "\u2013  "
            run = _run(prefix, sz=size - 200, color=GRAY) + _run(text, sz=size - 200, color="37474F")
        paras.append(_para(run, spc_after=gap // 2, line=line))
    return _txbox(x, y, w, SLIDE_H - y - 500000, "".join(paras))


def section_slide(num: str, title: str, subtitle: str, page: int, total: int) -> str:
    content = (
        _rect(0, 0, SLIDE_W, SLIDE_H, NAVY)
        + _rect(0, 0, 160000, SLIDE_H, CYAN)
        + _txbox(1200000, 1900000, 9000000, 900000,
                 _para(_run(num, sz=9000, bold=True, color=CYAN)))
        + _txbox(1200000, 3000000, 10500000, 800000,
                 _para(_run(title, sz=4400, bold=True, color=WHITE)))
        + _txbox(1200000, 3950000, 10000000, 700000,
                 _para(_run(subtitle, sz=2000, color="BFC9D9")))
        + footer(page, total)
    )
    return _slide(content)


def title_slide() -> str:
    content = (
        _rect(0, 0, SLIDE_W, SLIDE_H, NAVY)
        + _rect(0, 0, 160000, SLIDE_H, CYAN)
        + _rect(0, SLIDE_H - 700000, SLIDE_W, 700000, NAVY2)
        + _txbox(1200000, 1500000, 10500000, 600000,
                 _para(_run("立项提案 · 技术评审材料", sz=1800, bold=True, color=CYAN), spc_after=80000))
        + _txbox(1200000, 2150000, 10800000, 1600000,
                 _para(_run("让基础设施自解释、", sz=5000, bold=True, color=WHITE))
                 + _para(_run("可安全自愈、可证明恢复", sz=5000, bold=True, color=WHITE)))
        + _txbox(1200000, 3950000, 10800000, 900000,
                 _para(_run("把 CISRE 确立为企业内部统一的运维平台", sz=2600, bold=True, color=ACCENT), spc_after=120000)
                 + _para(_run("一个可作为内部 SRE Harness 的可靠基础框架，覆盖从云到数据库的全栈基础设施", sz=1900, color="BFC9D9")))
        + _rect(1200000, 5050000, 1800000, 60000, ACCENT)
        + _txbox(1200000, 5250000, 10800000, 500000,
                 _para(_run("CISRE 5.3.0  ·  对标 deepseek-harness (dsh 0.1.0-rc.6)", sz=1500, color="8FA0B8"), spc_after=60000)
                 + _para(_run("汇报人：＿＿＿＿＿＿    日期：＿＿＿＿＿＿", sz=1500, color="8FA0B8")))
    )
    return _slide(content)


def agenda_slide(page: int, total: int) -> str:
    items = [
        ("01", "现状与痛点", "为什么传统告警、聊天建议、脚本自动化都不够"),
        ("02", "我们已有什么", "CISRE 已是一个可审计闭环的 SRE 控制面"),
        ("03", "与 DeepSeek Harness 的差距", "控制面已对齐，编排与执行面是缺口"),
        ("04", "判定与目标架构", "够做内部 SRE Harness；三层架构覆盖云→数据库"),
        ("05", "路线图、风险与决策请求", "三阶段落地 + 诚实风险 + 今天要拍板的事"),
    ]
    rows = ""
    for i, (num, t, d) in enumerate(items):
        y = 1300000 + i * 950000
        rows += (
            _rect(520000, y, 1000000, 700000, LIGHT)
            + _txbox(620000, y + 140000, 800000, 500000,
                     _para(_run(num, sz=2600, bold=True, color=ACCENT)), anchor="ctr")
            + _txbox(1800000, y + 120000, 9500000, 400000,
                     _para(_run(t, sz=2200, bold=True, color=NAVY), spc_after=60000)
                     + _para(_run(d, sz=1400, color=GRAY)))
        )
    content = content_head("Agenda", "目录") + rows + footer(page, total)
    return _slide(content)


def problem_slide(page: int, total: int) -> str:
    cols = [
        ("告警 / 监控", "只会告诉你\"有问题\"", ["告警风暴，人肉筛选", "不知道根因、不知道影响面"]),
        ("聊天式 AI 助手", "只给建议、不能闭环", ["回答没有证据锚定", "说完就结束，不执行不验证"]),
        ("脚本 / 人工救火", "能执行、但不可靠", ["缺乏审批与回滚", "经验散落在个人脑中", "\"成功\"无法证明"]),
    ]
    cards = ""
    for i, (t, s, pts) in enumerate(cols):
        x = 520000 + i * 4050000
        body = _rect(x, 1500000, 3700000, 3600000, LIGHT)
        body += _txbox(x + 250000, 1700000, 3200000, 500000,
                       _para(_run(t, sz=2000, bold=True, color=NAVY), spc_after=60000)
                       + _para(_run(s, sz=1400, bold=True, color=RED)))
        pp = ""
        for p in pts:
            pp += _para(_run("\u2013  ", sz=1300, color=GRAY) + _run(p, sz=1400, color="37474F"), spc_after=120000, line=110)
        body += _txbox(x + 250000, 2450000, 3200000, 2500000, pp)
        cards += body
    content = (
        content_head("Problem", "现状：三种旧方式都无法\u201c证明恢复\u201d")
        + _txbox(520000, 1050000, 11200000, 400000,
                 _para(_run("核心矛盾：", sz=1700, bold=True, color=NAVY)
                       + _run("Kubernetes API 返回 2xx ≠ 业务真的恢复了；重复救火让运维知识留在平台之外。", sz=1700, color=GRAY)))
        + cards
        + footer(page, total)
    )
    return _slide(content)


def assets_slide(page: int, total: int) -> str:
    items = [
        ("闭环而非聊天", "发现 → 证据 → 诊断 → Skill → 预演 → 审批 → 执行 → 验证 → 学习，全程可审计"),
        ("持久化 SRE 执行", "CISREDurableHarness/v3：阶段检查点、回执、重复轨迹检测、确定性完成判定"),
        ("Everything-is-a-Plugin", "CISREPluginHarness/v1：作用域 DI、可逆 Effect、四类事件、12 个内置插件"),
        ("写后回读 + 恢复验证", "同通道逐字段回读 + 新 Pod/业务恢复判据，只有 recovered=true 才结案"),
        ("Skill 路由 + 成效", "贝叶斯主 Skill 优先 + 经验沉淀，平台越用越强"),
        ("多集群 + 全栈适配", "Rancher / kubeconfig / 云 / 数据库 / VM / 中间件 / 存储统一资源合同"),
    ]
    content = (
        content_head("Assets", "我们已有什么：CISRE 5.3.0 不是一个\u201c运维聊天框\u201d")
        + bullets(items, 520000, 1100000, 11200000, size=1700, gap=150000)
        + footer(page, total)
    )
    return _slide(content)


def loop_slide(page: int, total: int) -> str:
    steps = [
        ("1", "发现", "告警/事件/巡检"),
        ("2", "证据", "日志·YAML·事件·拓扑"),
        ("3", "诊断", "根因假设 + Skill 排序"),
        ("4", "预演", "变更·风险·回滚预览"),
        ("5", "审批", "人工逐项确认"),
        ("6", "执行", "受控变更 + 写后回读"),
        ("7", "验证", "恢复判据通过才结案"),
        ("8", "学习", "成效与 Skill 沉淀"),
    ]
    rows = ""
    for i, (num, t, d) in enumerate(steps):
        x = 520000 + i * 1430000
        rows += (
            _rect(x, 1600000, 1280000, 2400000, LIGHT)
            + _rect(x, 1600000, 1280000, 120000, ACCENT if i < 6 else CYAN)
            + _txbox(x + 120000, 1750000, 1050000, 500000,
                     _para(_run(num, sz=2400, bold=True, color=ACCENT)))
            + _txbox(x + 120000, 2300000, 1050000, 500000,
                     _para(_run(t, sz=1800, bold=True, color=NAVY), spc_after=50000)
                     + _para(_run(d, sz=1100, color=GRAY)))
        )
        if i < 7:
            rows += _txbox(x + 1280000, 2500000, 150000, 500000,
                           _para(_run("\u2192", sz=1800, bold=True, color=GRAY)), anchor="ctr")
    content = (
        content_head("The Loop", "AgenticOps 闭环：把运维做成可审计的流水线")
        + _txbox(520000, 1050000, 11200000, 400000,
                 _para(_run("一条故障链从发现到恢复被固化下来；模型只负责理解与规划，平台负责执行与证明。", sz=1600, color=GRAY)))
        + rows
        + _txbox(520000, 4300000, 11200000, 700000,
                 _para(_run("关键点：", sz=1600, bold=True, color=NAVY)
                       + _run("只有 \u201c恢复验证\u201d通过才能结案；未恢复则保留失败轨迹、换策略继续，绝不自报成功。", sz=1600, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def harness_parity_slide(page: int, total: int) -> str:
    have = [
        ("插件化内核", "Everything-is-a-Plugin + 作用域 DI + 可逆 Effect"),
        ("会话事件 / 压缩", "Append-only 事件 + Importance Compactor"),
        ("目标驱动 / 任务", "Goal Round + owner-scoped jobs + single-flight"),
        ("Skill / 审批", "可移植 Skill 包 + fail-closed 审批凭据"),
        ("多模型 / 遥测", "Model Lab + Langfuse / 部分 OTel"),
        ("Web 控制台 / MCP", "React 控制台 + 对外 K8s MCP"),
    ]
    left = _txbox(520000, 1200000, 5400000, 500000,
                  _para(_run("已对齐（CISRE 原生实现）", sz=1900, bold=True, color=GREEN), spc_after=120000)
                  + _para(_run("DSH 的控制面理念已同构落入 Python", sz=1300, color=GRAY)))
    left += bullets(have, 520000, 1850000, 5500000, size=1500, gap=120000, accent=GREEN)
    miss = [
        ("多智能体编排", "Subagent / Workflow fan-out（证据并行、全集群巡检）"),
        ("通用受控执行器", "DB(psql/mysql)、VM(SSH)、云(CLI/SDK) 的受控执行"),
        ("通用 MCP 客户端", "消费 DBA / ITSM / 云 MCP 工具"),
        ("统一存储 / OTel", "多副本任务存储 + OTel GenAI 语义导出"),
        ("多端入口", "Headless / CLI / TUI + 统一会话检索"),
    ]
    right = _txbox(6500000, 1200000, 5500000, 500000,
                   _para(_run("缺口（需补齐）", sz=1900, bold=True, color=RED), spc_after=120000)
                   + _para(_run("主要缺在编排与执行广度", sz=1300, color=GRAY)))
    right += bullets(miss, 6500000, 1850000, 5500000, size=1500, gap=120000, accent=RED)
    content = (
        content_head("Harness Parity", "与 DeepSeek Harness 的定位：吸收其内核，强化其安全")
        + _rect(6200000, 1100000, 60000, 4200000, "D9E2EC")
        + left + right
        + _txbox(520000, 6050000, 11200000, 400000,
                 _para(_run("结论：", sz=1600, bold=True, color=NAVY)
                       + _run("控制面已 70–80% 对齐；通用 harness 把 Shell/FS 交给模型，CISRE 把它们挡在模型边界之外——对企业更安全。", sz=1600, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def _cell(text: str, w: int, *, bold: bool = False, fill: str = "", color: str = "263238",
          sz: int = 1100, align: str = "l") -> str:
    tcPr = ""
    if fill:
        tcPr = (f'<a:tcPr><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
                f'<a:lnL><a:noFill/></a:lnL><a:lnR><a:noFill/></a:lnR>'
                f'<a:lnT><a:noFill/></a:lnT><a:lnB><a:noFill/></a:lnB>'
                f'<a:anchor val="ctr"/></a:tcPr>')
    else:
        tcPr = ('<a:tcPr><a:lnL><a:noFill/></a:lnL><a:lnR><a:noFill/></a:lnR>'
                f'<a:lnT><a:noFill/></a:lnT><a:lnB><a:noFill/></a:lnB>'
                f'<a:anchor val="ctr"/></a:tcPr>')
    return (
        f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>'
        f'<a:p><a:pPr algn="{align}" marL="60000" marR="60000"><a:lnSpc><a:spcPct val="105000"/></a:lnSpc></a:pPr>'
        f'{_run(text, sz=sz, bold=bold, color=color)}</a:p></a:txBody>{tcPr}</a:tc>'
    )


def _table(cols: list[int], rows: list[list[str]], x: int, y: int, *, header_fill=NAVY,
           band_fill="EDF2F8") -> str:
    grid = "".join(f'<a:gridCol w="{w}"/>' for w in cols)
    trs = []
    total_h = 0
    for ri, row in enumerate(rows):
        cells = ""
        for ci, val in enumerate(row):
            if ri == 0:
                cells += _cell(val, cols[ci], bold=True, fill=header_fill, color=WHITE, sz=1200)
            else:
                fill = band_fill if ri % 2 == 0 else ""
                bold = (ci == 0)
                cells += _cell(val, cols[ci], bold=bold, fill=fill, sz=1100)
        h = 500000 if ri == 0 else 560000
        total_h += h
        trs.append(f'<a:tr h="{h}">{cells}</a:tr>')
    return (
        "<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id=\"0\" name=\"tbl\"/>"
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        "<p:xfrm><a:off x=\"" + str(x) + "\" y=\"" + str(y) + "\"/>"
        "<a:ext cx=\"" + str(sum(cols)) + "\" cy=\"" + str(total_h) + "\"/></p:xfrm>"
        "<a:graphic><a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/table\">"
        "<a:tbl><a:tblPr firstRow=\"1\" bandRow=\"0\"><a:tableStyleId>{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}</a:tableStyleId></a:tblPr>"
        + grid + "".join(trs) + "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def gap_table_slide(page: int, total: int) -> str:
    cols = [1850000, 2900000, 3300000, 2600000]
    rows = [
        ["缺口", "DSH 能力", "对\u201c统一运维平台\u201d的意义", "优先级"],
        ["多智能体编排", "Subagent 委托 + Workflow fan-out", "证据并行采集、全集群巡检、多目标处置", "高"],
        ["通用受控执行器", "沙箱 Shell/FS/CLI + 审计回滚", "数据库/VM/云的变更执行与验证", "高"],
        ["统一存储/多副本", "会话/任务持久化 + 可检索", "API 多副本、审计留存、合规检索", "高"],
        ["通用 MCP 客户端", "消费第三方 MCP 工具", "接入 DBA/ITSM/云 CLI 现成能力", "中"],
        ["OTel GenAI", "统一模型/工具/轨迹导出", "成本核算与端到端可观测", "中"],
        ["多端入口", "Web/Headless/CLI/TUI", "CI/CD 批处理、值班终端、自动化", "中"],
    ]
    content = (
        content_head("Gap Analysis", "差距分析总览：缺的不是内核，是编排与执行广度")
        + _table(cols, rows, 500000, 1250000)
        + _txbox(500000, 5550000, 11200000, 700000,
                 _para(_run("已对齐无需重写：", sz=1500, bold=True, color=GREEN)
                       + _run("插件内核、事件、压缩、目标/任务、Skill、审批、多模型、Web 控制台。", sz=1500, color=GRAY), spc_after=60000)
                 + _para(_run("补齐方式：", sz=1500, bold=True, color=NAVY)
                       + _run("不推倒重来，而是在现有 Harness 上\u201c加能力\u201d——三个高优先级缺口对应下一节的执行面与编排面。", sz=1500, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def gap_orchestration_slide(page: int, total: int) -> str:
    left = [
        ("现状", 0),
        ("只有 4 个固定 A2A 命名代理（healing/incident/postmortem/observability）", 1),
        ("没有通用\u201c子代理委托 + 工作流 fan-out\u201d原语", 1),
        ("证据采集、巡检、多集群处置只能串行", 1),
    ]
    right = [
        ("补齐后", 0),
        ("Subagent：证据并行、专家子代理隔离、跨域协作", 1),
        ("Workflow：pipeline/parallel 编排全集群巡检、批量发现", 1),
        ("目标驱动：把\u201c事故\u201d泛化为任意\u201c目标\u201d自主续跑", 1),
    ]
    content = (
        content_head("Gap 1 / 3", "缺口一：多智能体编排（Subagent / Workflow）")
        + _txbox(520000, 1200000, 5500000, 400000, _para(_run("现状", sz=1800, bold=True, color=RED)))
        + bullets(left, 520000, 1700000, 5500000, size=1500, gap=120000, accent=RED)
        + _rect(6200000, 1100000, 60000, 4200000, "D9E2EC")
        + _txbox(6500000, 1200000, 5500000, 400000, _para(_run("补齐后", sz=1800, bold=True, color=GREEN)))
        + bullets(right, 6500000, 1700000, 5500000, size=1500, gap=120000, accent=GREEN)
        + _txbox(520000, 6000000, 11200000, 400000,
                 _para(_run("价值：", sz=1600, bold=True, color=NAVY)
                       + _run("让\u201c从云到数据库\u201d的并行巡检、并行取证、并行处置成为可能，是规模化的前提。", sz=1600, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def gap_executor_slide(page: int, total: int) -> str:
    rows = [
        ["资源", "执行通道", "恢复判据（验证什么）"],
        ["Kubernetes", "Rancher / kubeconfig / MCP（已就绪）", "Rollout + 新 Pod + 日志 + 业务探针"],
        ["数据库", "psql/mysql 受控 Runbook + Adapter", "连接可用、慢查询、主备/复制状态"],
        ["虚拟机", "SSH 受控 Runbook + Adapter", "探针、CPU/磁盘/服务状态"],
        ["云资源", "云 CLI/SDK 受控执行 + RAM", "实例状态、配额、健康检查"],
        ["中间件/存储", "Adapter + Webhook 过渡", "队列积压、读写探针、容量"],
    ]
    content = (
        content_head("Gap 2 / 3", "缺口二：统一的受控执行器（从 K8s 走向全栈）")
        + _txbox(500000, 1050000, 11200000, 500000,
                 _para(_run("把今天只对 K8s 成立的\u201c证据→审批→执行→回读→验证\u201d抽象成对任意资源成立的一份合同。", sz=1600, color=GRAY)))
        + _table([1900000, 3300000, 5800000], rows, 500000, 1650000)
        + _txbox(500000, 5550000, 11200000, 700000,
                 _para(_run("关键：", sz=1500, bold=True, color=NAVY)
                       + _run("模型仍不能直接 Shell/SSH/CLI——所有执行走动作目录 + 审批 + 回滚 + 恢复验证；复用现有 Webhook 作为过渡。", sz=1500, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def gap_store_slide(page: int, total: int) -> str:
    items = [
        ("统一持久层", "单副本 JSON Job Store → PostgreSQL/Redis 租约（或 Temporal-compatible），支持 API 多副本"),
        ("统一会话/审计检索", "把事故链、模型调用、工具回执、Skill 成效纳入可检索的统一存储"),
        ("OTel GenAI 语义导出", "统一导出模型/工具/轨迹的 OpenTelemetry GenAI semantic conventions"),
        ("多端入口", "在 Web 控制台之外补 Headless/CLI/TUI，支持 CI/CD 与值班终端"),
        ("通用 MCP 客户端", "消费 DBA / ITSM / 云 CLI 等第三方 MCP 工具，扩展现成能力"),
    ]
    content = (
        content_head("Gap 3 / 3", "缺口三：统一存储、可观测与多端入口")
        + bullets(items, 520000, 1200000, 11200000, size=1700, gap=160000)
        + _txbox(520000, 6000000, 11200000, 400000,
                 _para(_run("价值：", sz=1600, bold=True, color=NAVY)
                       + _run("多副本高可用、可审计可检索、成本可核算，是\u201c企业级可靠基础框架\u201d的最后一块拼图。", sz=1600, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def verdict_slide(page: int, total: int) -> str:
    criteria = [
        ("持久执行", "故障后从检查点恢复", "ResumableSREHarness/v1 检查点 + 孤儿任务自恢复", GREEN),
        ("确定性完成", "模型不能自报成功", "RecoveryVerifier：只有实时恢复证据才结案", GREEN),
        ("可逆 + 审批", "可回滚、逐项确认", "审批凭据 + 回滚 Patch + 写后回读", GREEN),
        ("可观测 + 沉淀", "轨迹/成效入库", "Records / Effectiveness / Skill 指标", GREEN),
    ]
    rows = ""
    for i, (t, std, c, col) in enumerate(criteria):
        y = 1500000 + i * 950000
        rows += (
            _rect(520000, y, 2500000, 700000, NAVY)
            + _txbox(620000, y + 120000, 2300000, 500000,
                     _para(_run(t, sz=1900, bold=True, color=WHITE)), anchor="ctr")
            + _txbox(3300000, y + 60000, 4000000, 600000,
                     _para(_run(std, sz=1600, bold=True, color=NAVY), spc_after=40000)
                     + _para(_run(c, sz=1300, color=GRAY)))
            + _rect(7600000, y + 200000, 4200000, 300000, col)
        )
    content = (
        content_head("Verdict", "判定：够做内部 SRE Harness，且比通用 Harness 更严格")
        + rows
        + _txbox(520000, 5450000, 11200000, 800000,
                 _para(_run("Harness 的四个本质属性全部满足。", sz=1800, bold=True, color=GREEN), spc_after=80000)
                 + _para(_run("边界声明：", sz=1600, bold=True, color=NAVY)
                       + _run("今天\u201c够\u201d是针对 K8s/Rancher 单域；要成为\u201c云→数据库\u201d全栈统一平台，需补齐执行面与编排面（第 8–10 页）。", sz=1600, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def target_arch_slide(page: int, total: int) -> str:
    layers = [
        ("体验层", "Web 控制台 · Headless/CLI · TUI · 统一检索 · 逐条反馈", ACCENT),
        ("控制面", "CISREDurableHarness/v3 + CISREPluginHarness/v1\n目标驱动 · Subagent/Workflow 编排 · 统一任务/会话存储", NAVY),
        ("执行面", "Guarded Executor（动作目录 · 审批 · 回滚 · 回读 · 恢复验证）\nK8s 已就绪 · 基础设施 Adapter · 受控 Runbook · 通用 MCP 客户端", TEAL),
    ]
    rows = ""
    for i, (t, d, col) in enumerate(layers):
        y = 1300000 + i * 1550000
        rows += (
            _rect(520000, y, 2300000, 1350000, col)
            + _txbox(620000, y + 380000, 2100000, 600000,
                     _para(_run(t, sz=2200, bold=True, color=WHITE)), anchor="ctr")
            + _rect(3000000, y, 8900000, 1350000, LIGHT)
        )
        lines = ""
        for ln in d.split("\n"):
            lines += _para(_run(ln, sz=1500, color="263238"), spc_after=60000)
        rows += _txbox(3250000, y + 120000, 8400000, 1200000, lines, anchor="ctr")
    content = (
        content_head("Target Architecture", "目标架构：从云到数据库的统一运维平台（三层）")
        + rows
        + _txbox(520000, 6150000, 11200000, 400000,
                 _para(_run("执行面是\u201c统一\u201d的关键：", sz=1500, bold=True, color=NAVY)
                       + _run("所有资源共享同一套审批、回滚、回读、恢复验证机制，而不是每个产品各建一套。", sz=1500, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def coverage_slide(page: int, total: int) -> str:
    groups = [
        ("云", "阿里云 ECS/ACK/RDS/PolarDB/SLS/ARMS · 华为云 · 腾讯云", "实例状态 / 配额 / 健康检查"),
        ("数据库", "OceanBase · GaussDB · TiDB · 达梦 · 人大金仓 · MySQL/PG/Redis", "连接 / 慢查询 / 主备复制"),
        ("中间件", "RocketMQ · Nacos · Kafka · Redis", "队列积压 / 读写探针"),
        ("存储", "华为/深信服/浪潮存储 · 私有云 · HCI/虚拟化 · OpenStack", "容量 / 读写 / IO"),
        ("K8s", "Rancher 多集群 · kubeconfig · Argo Rollouts", "Rollout / Pod / 业务探针（已就绪）"),
    ]
    rows = ""
    for i, (t, d, v) in enumerate(groups):
        y = 1300000 + i * 950000
        rows += (
            _rect(520000, y, 1900000, 720000, NAVY)
            + _txbox(600000, y + 130000, 1700000, 500000,
                     _para(_run(t, sz=1800, bold=True, color=WHITE)), anchor="ctr")
            + _txbox(2700000, y + 80000, 5600000, 560000,
                     _para(_run(d, sz=1400, color="263238"), spc_after=40000)
                     + _para(_run("验证：" + v, sz=1200, color=GRAY)))
        )
    content = (
        content_head("Coverage", "覆盖矩阵：统一资源合同 + 每类资源的\u201c恢复判据\u201d")
        + _txbox(520000, 1050000, 11200000, 300000,
                 _para(_run("三组稳定接口隔离厂商差异：resources/sync · discover · scan · 审批后 action webhook。", sz=1500, color=GRAY)))
        + rows
        + footer(page, total)
    )
    return _slide(content)


def safety_slide(page: int, total: int) -> str:
    items = [
        ("模型只规划，平台管执行", "模型输出必须经过动作目录校验、风险门禁、人工审批后才可执行"),
        ("逐项审批 + 一次性凭据", "approval_id + change_fingerprint + change_index 绑定，证据变化旧审批自动失效"),
        ("写后回读", "同通道递归逐字段比对 + resourceVersion 前进校验，2xx ≠ 写入成功"),
        ("恢复验证", "只有 recovered=true 才结案；API/证书/网络失败不被误判为恢复"),
        ("最小权限", "RBAC + 命名空间 Allowlist + 目标发现约束 + 敏感字段脱敏"),
        ("审计与回滚", "每个动作记录预览、actor、diff、结果、验证状态；支持结构化回滚 Patch"),
    ]
    content = (
        content_head("Governance", "为什么敢把生产基础设施交给它：四道闸")
        + bullets(items, 520000, 1200000, 11200000, size=1700, gap=150000)
        + _rect(520000, 5900000, 11200000, 650000, "EAF3FB")
        + _txbox(760000, 6040000, 10400000, 450000,
                 _para(_run("一句话：", sz=1600, bold=True, color=NAVY)
                       + _run("把\u201c人审、可回滚、可追溯\u201d做成工程机制，而不是靠模型自觉。", sz=1600, color=GRAY)), anchor="ctr")
        + footer(page, total)
    )
    return _slide(content)


def roadmap_slide(page: int, total: int) -> str:
    phases = [
        ("P0", "确立底座", "内部镜像/Helm · SSO/审计 · RBAC preset · 试点 1–2 集群", "0–2 周"),
        ("P1", "执行面扩围", "Adapter SDK + OpenAPI · 每类资源恢复判据 · 受控 Runbook · 阿里云 RAM", "1–3 月"),
        ("P2", "编排与规模", "Subagent/Workflow · PostgreSQL/Redis 租约 · OTel GenAI · Headless/CLI", "2–6 月"),
        ("P3", "生态与治理", "Skill 网络 · 故障注入基准集 · 模型/Skill 离线评测门禁 · 合规报告", "持续"),
    ]
    rows = ""
    for i, (tag, t, d, when) in enumerate(phases):
        y = 1400000 + i * 1150000
        rows += (
            _rect(520000, y, 1600000, 900000, ACCENT if i < 2 else NAVY)
            + _txbox(620000, y + 220000, 1400000, 500000,
                     _para(_run(tag, sz=2200, bold=True, color=WHITE)), anchor="ctr")
            + _txbox(2400000, y + 100000, 7000000, 400000,
                     _para(_run(t, sz=1900, bold=True, color=NAVY), spc_after=50000)
                     + _para(_run(d, sz=1300, color=GRAY)))
            + _txbox(9700000, y + 250000, 2300000, 400000,
                     _para(_run(when, sz=1600, bold=True, color=ACCENT), align="r"))
        )
    content = (
        content_head("Roadmap", "作为可靠基础框架迭代开发：三阶段")
        + rows
        + _txbox(520000, 6200000, 11200000, 400000,
                 _para(_run("原则：", sz=1500, bold=True, color=NAVY)
                       + _run("先底座、再扩围、后规模；每阶段都有可验证的交付与退出标准。", sz=1500, color=GRAY)))
        + footer(page, total)
    )
    return _slide(content)


def risk_slide(page: int, total: int) -> str:
    rows = [
        ["风险", "说明", "对策"],
        ["上游 DSH 为 Preview", "Python SDK 0.0.0.dev0，可能破坏性变更", "不替换执行面；仅作可选 Planner，稳定版+SBOM+漏洞扫描后才启用"],
        ["单副本存储", "当前 API 要求副本=1", "引入分布式租约/事务存储前不宣称多副本"],
        ["License", "PolyForm Noncommercial", "内部自用属非商业；落地前取得维护者书面授权"],
        ["供应链", "镜像/依赖/模型网关", "国内镜像 + 哈希锁定 + pip-audit + SBOM + TLS"],
        ["模型不确定性", "模型输出不可信", "动作目录 + 审批 + 回读 + 验证四道闸约束"],
    ]
    content = (
        content_head("Risks", "风险与对策（诚实清单）")
        + _table([2000000, 3400000, 5600000], rows, 500000, 1350000)
        + footer(page, total)
    )
    return _slide(content)


def value_slide(page: int, total: int) -> str:
    items = [
        ("不是再买工具，而是升级底座", "CISRE 已具备 harness 本质能力，投入是补缺口而非重写"),
        ("可证明的可靠性", "写后回读 + 恢复验证 + 审批凭据，让\u201c修好了吗\u201d变成可审计证据"),
        ("安全边界明确", "模型只规划、平台管执行，天然符合企业治理要求"),
        ("一次建设、全栈复用", "统一资源合同 + Adapter，K8s/数据库/VM/云共享同一套审批与验证"),
        ("持续增值", "每解决一次故障，Skill 与成效入库，平台越用越强（基础框架的复利）"),
        ("对齐行业方向", "与 MS Agent Framework / LangGraph / Temporal / DeepSeek Harness 同构，不闭门造车"),
    ]
    content = (
        content_head("Value", "价值主张：为什么现在就该定下来")
        + bullets(items, 520000, 1200000, 11200000, size=1700, gap=150000)
        + footer(page, total)
    )
    return _slide(content)


def ask_slide(page: int, total: int) -> str:
    items = [
        ("今天要拍板的三件事", 0),
        ("1. 确认 CISRE 为内部统一运维平台 / SRE 基础框架（P0 立项）", 1),
        ("2. 授权基础设施接入：K8s → 数据库 → VM → 云（分阶段，先只读后变更）", 1),
        ("3. 组建平台组并分配 P1 资源（Adapter SDK + 受控执行器 + 编排）", 1),
        ("建议第一步", 0),
        ("两周内完成内部镜像/Helm 上线 + SSO/审计基线 + 1–2 个集群试点，用一次真实故障闭环做验收", 1),
    ]
    content = (
        content_head("Decision", "请求决策：把 CISRE 定为内部统一运维平台")
        + bullets(items, 520000, 1300000, 11200000, size=1800, gap=170000)
        + _rect(520000, 5300000, 11200000, 1200000, NAVY)
        + _txbox(800000, 5500000, 10400000, 800000,
                 _para(_run("让基础设施自解释、可安全自愈、可证明恢复。", sz=2600, bold=True, color=WHITE), align="ctr", spc_after=60000)
                 + _para(_run("—— CISRE · 企业统一运维平台", sz=1500, color=CYAN), align="ctr"))
        + footer(page, total)
    )
    return _slide(content)


def build_deck() -> list[str]:
    slides = [title_slide(), agenda_slide(2, 18)]
    slides += [
        section_slide("01", "现状与痛点", "传统方式无法\u201c证明恢复\u201d，经验在平台之外流失", 3, 18),
        problem_slide(4, 18),
        section_slide("02", "我们已有什么", "CISRE 已是一个可审计闭环的 SRE 控制面", 5, 18),
        assets_slide(6, 18),
        loop_slide(7, 18),
        section_slide("03", "与 DeepSeek Harness 的差距", "内核已对齐，编排与执行广度是缺口", 8, 18),
        harness_parity_slide(9, 18),
        gap_table_slide(10, 18),
        gap_orchestration_slide(11, 18),
        gap_executor_slide(12, 18),
        gap_store_slide(13, 18),
        section_slide("04", "判定与目标架构", "够做内部 SRE Harness；三层架构覆盖云→数据库", 14, 18),
        verdict_slide(15, 18),
        target_arch_slide(16, 18),
        coverage_slide(17, 18),
        safety_slide(18, 18),
    ]
    # adjust: total is 18 but we have more slides; recompute totals properly below
    return slides


def main() -> None:
    slides = [
        title_slide(),
        agenda_slide(2, 20),
        section_slide("01", "现状与痛点", "传统方式无法\u201c证明恢复\u201d，经验在平台之外流失", 3, 20),
        problem_slide(4, 20),
        section_slide("02", "我们已有什么", "CISRE 已是一个可审计闭环的 SRE 控制面", 5, 20),
        assets_slide(6, 20),
        loop_slide(7, 20),
        section_slide("03", "与 DeepSeek Harness 的差距", "内核已对齐，编排与执行广度是缺口", 8, 20),
        harness_parity_slide(9, 20),
        gap_table_slide(10, 20),
        gap_orchestration_slide(11, 20),
        gap_executor_slide(12, 20),
        gap_store_slide(13, 20),
        section_slide("04", "判定与目标架构", "够做内部 SRE Harness；三层架构覆盖云→数据库", 14, 20),
        verdict_slide(15, 20),
        target_arch_slide(16, 20),
        coverage_slide(17, 20),
        safety_slide(18, 20),
        section_slide("05", "路线图、风险与决策请求", "三阶段落地 + 诚实风险 + 今天要拍板的事", 19, 20),
        roadmap_slide(20, 20),
        risk_slide(21, 20),
        value_slide(22, 20),
        ask_slide(23, 20),
    ]
    total = len(slides)

    # rebuild with correct totals (first two are title/agenda)
    slides = [
        title_slide(),
        agenda_slide(2, total),
        section_slide("01", "现状与痛点", "传统方式无法\u201c证明恢复\u201d，经验在平台之外流失", 3, total),
        problem_slide(4, total),
        section_slide("02", "我们已有什么", "CISRE 已是一个可审计闭环的 SRE 控制面", 5, total),
        assets_slide(6, total),
        loop_slide(7, total),
        section_slide("03", "与 DeepSeek Harness 的差距", "内核已对齐，编排与执行广度是缺口", 8, total),
        harness_parity_slide(9, total),
        gap_table_slide(10, total),
        gap_orchestration_slide(11, total),
        gap_executor_slide(12, total),
        gap_store_slide(13, total),
        section_slide("04", "判定与目标架构", "够做内部 SRE Harness；三层架构覆盖云→数据库", 14, total),
        verdict_slide(15, total),
        target_arch_slide(16, total),
        coverage_slide(17, total),
        safety_slide(18, total),
        section_slide("05", "路线图、风险与决策请求", "三阶段落地 + 诚实风险 + 今天要拍板的事", 19, total),
        roadmap_slide(20, total),
        risk_slide(21, total),
        value_slide(22, total),
        ask_slide(23, total),
    ]

    # presentation.xml.rels
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>',
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>',
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>',
    ]
    for i in range(1, len(slides) + 1):
        rels.append(
            f'<Relationship Id="rId{1000 + i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{i}.xml"/>'
        )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        + "".join(
            f'<Override PartName="/ppt/slides/slide{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            for i in range(1, len(slides) + 1)
        )
        + '</Types>'
    )

    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        '<p:sldIdLst>'
        + "".join(
            f'<p:sldId id="{255 + i}" r:id="rId{1000 + i}"/>' for i in range(1, len(slides) + 1)
        )
        + '</p:sldIdLst>'
        '<p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        '</p:presentation>'
    )

    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + '</Relationships>'
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )

    slide_master = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>'
        '</p:sldMaster>'
    )

    slide_master_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        '</Relationships>'
    )

    slide_layout = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
        '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping/></p:clrMapOvr></p:sldLayout>'
    )

    slide_layout_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        '</Relationships>'
    )

    theme = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="CISRE">'
        '<a:themeElements><a:clrScheme name="CISRE">'
        '<a:dk1><a:srgbClr val="0B2447"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="14366B"/></a:dk2><a:lt2><a:srgbClr val="F1F5FA"/></a:lt2>'
        '<a:accent1><a:srgbClr val="2E86DE"/></a:accent1><a:accent2><a:srgbClr val="00A8CC"/></a:accent2>'
        '<a:accent3><a:srgbClr val="00A8A8"/></a:accent3><a:accent4><a:srgbClr val="1E8449"/></a:accent4>'
        '<a:accent5><a:srgbClr val="C0392B"/></a:accent5><a:accent6><a:srgbClr val="B9770E"/></a:accent6>'
        '<a:hlink><a:srgbClr val="2E86DE"/></a:hlink><a:folHlink><a:srgbClr val="14366B"/></a:folHlink>'
        '</a:clrScheme>'
        '<a:fontScheme name="CISRE"><a:majorFont><a:latin typeface="Segoe UI"/><a:ea typeface="Microsoft YaHei"/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Segoe UI"/><a:ea typeface="Microsoft YaHei"/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="CISRE"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
        '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
        '</a:fmtScheme></a:themeElements></a:theme>'
    )

    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>CISRE 企业统一运维平台立项提案</dc:title>'
        '<dc:creator>CISRE Contributors</dc:creator>'
        '<cp:lastModifiedBy>CISRE</cp:lastModifiedBy>'
        '<cp:revision>1</cp:revision>'
        '</cp:coreProperties>'
    )

    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>CISRE</Application></Properties>'
    )

    pres_props = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentationPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    view_props = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<p:viewPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                  'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                  'showComments="0" lastView="sldView"><p:slideViewPr><p:cSldViewPr><p:cViewPr varScale="1"/>'
                  '<p:guideLst/></p:cSldViewPr></p:slideViewPr></p:viewPr>')

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "CISRE_ENTERPRISE_OPS_PLATFORM.pptx")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        z.writestr("ppt/slideMasters/slideMaster1.xml", slide_master)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels)
        z.writestr("ppt/theme/theme1.xml", theme)
        for i, s in enumerate(slides, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", s)
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                       '</Relationships>')
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("ppt/presProps.xml", pres_props)
        z.writestr("ppt/viewProps.xml", view_props)

    print(f"Wrote {out} with {len(slides)} slides")


if __name__ == "__main__":
    main()
