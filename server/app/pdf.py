"""报价单 PDF 生成（reportlab）。

布局：公司 Logo（可配置 logo 路径，缺省用文字占位）、报价编号、日期、
报价明细表（基础价格 / 部署附加费 / 定制附加费 / 总价）、有效期（默认 30 天）、条款。
自动注册中文字体（SimHei 等），缺失时回退 Helvetica。
"""

from __future__ import annotations

import html
import time
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .config import Settings
from .quoting import QuoteRequest, QuoteResult

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/simhei.ttf",           # 黑体
    "C:/Windows/Fonts/msyh.ttc",             # 微软雅黑
    "/System/Library/Fonts/PingFang.ttc",    # macOS
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _register_cjk_font() -> str:
    """注册中文字体，返回字体名；失败返回 'Helvetica'。"""
    for path in _FONT_CANDIDATES:
        p = Path(path)
        if p.exists():
            try:
                name = "CJK"
                pdfmetrics.registerFont(TTFont(name, str(p)))
                return name
            except Exception:
                continue
    return "Helvetica"


def generate_quote_pdf(
    settings: Settings,
    req: QuoteRequest,
    quote: QuoteResult,
    out_dir: Path,
) -> Path:
    """生成报价单 PDF，返回文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"quote-{ts}.pdf"

    font = _register_cjk_font()
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName=font, fontSize=20)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font, fontSize=13,
                        textColor=colors.HexColor("#1f3a5f"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName=font, fontSize=10)
    small = ParagraphStyle("small", parent=styles["BodyText"], fontName=font,
                           fontSize=8.5, textColor=colors.grey)

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
    )
    story = []

    # 头部：Logo / 品牌 + 报价单标题
    logo = settings.data_dir / "logo.png"
    head = []
    if logo.exists():
        head.append(Image(str(logo), width=40 * mm, height=14 * mm))
    else:
        head.append(Paragraph(f"<b>{settings.app_name} 企业版报价单</b>", h1))
    head.append(Spacer(1, 4))
    head.append(Paragraph(
        f"报价编号：Q-{ts}　　日期：{date.today().isoformat()}"
        f"　　有效期：{settings.quote_valid_days} 天（至 "
        f"{(date.today() + timedelta(days=settings.quote_valid_days)).isoformat()}）",
        small,
    ))
    story.extend(head)
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("客户信息", h2))
    story.append(Paragraph(f"公司：{html.escape(req.company)}", body))
    story.append(Paragraph(f"联系人：{html.escape(req.contact_email)}", body))
    story.append(Paragraph(
        f"预估开发者数量：{req.dev_count} 人　|　部署环境：{req.environment.value}"
        f"　|　定制开发：{'是' if req.needs_custom else '否'}",
        body,
    ))
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("报价明细（美元/年）", h2))
    env_note = "（私有 IDC 部署附加 25%）" if req.environment.value == "private_idc" else ""
    custom_note = "（含定制开发附加 40%）" if req.needs_custom else ""
    rows = [
        ["项目", "金额 (USD)"],
        [f"基础价格（含 {max(0, req.dev_count - int(settings.quote['min_devs']))} 席以上增量）", f"{quote.base_price:,.2f}"],
        [f"部署附加费{env_note}", f"{quote.deployment_fee:,.2f}"],
        [f"定制开发附加费{custom_note}", f"{quote.custom_fee:,.2f}"],
        ["<b>最终报价（美元/年）</b>", f"<b>{quote.total:,.2f}</b>"],
    ]
    table = Table(rows, colWidths=[110 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef3f8")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("条款说明", h2))
    story.append(Paragraph(
        "1. 本报价基于所填写的预估开发者数量与部署环境，最终价格以合同为准。\n"
        "2. 报价自发出之日起 30 天内有效，逾期需重新询价。\n"
        "3. 价格单位为美元（USD），按年计费；含标准部署支持与基础培训。\n"
        "4. 定制开发范围以双方确认的需求文档为准，费用按人天另行评估。",
        body,
    ))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(
        f"{settings.app_name} · AI 驱动的 PR 描述生成器 · 企业私有部署",
        small,
    ))

    doc.build(story)
    return path
