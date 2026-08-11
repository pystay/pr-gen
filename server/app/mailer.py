"""邮件抽象：配置 RESEND_API_KEY 走真实 API，否则落盘（本地/测试模式）。

落盘邮件保存在 MAIL_OUT_DIR/ 下，文件名含时间戳与收件人，
验收与开发可直接检查文件内容。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Settings

RESEND_ENDPOINT = "https://api.resend.com/emails"


class MailError(RuntimeError):
    pass


def send_quote_email(
    settings: Settings,
    to: str,
    subject: str,
    html: str,
    pdf_path: Path,
) -> dict:
    """发送报价邮件。返回 {channel: 'resend'|'file', ...}。"""
    if settings.resend_api_key:
        return _send_resend(settings, to, subject, html, pdf_path)
    return _send_to_file(settings, to, subject, html, pdf_path)


def _send_resend(settings: Settings, to: str, subject: str, html: str,
                 pdf_path: Path) -> dict:
    """Resend API：multipart 邮件（HTML + PDF 附件）。"""
    boundary = "----prgen" + str(int(time.time() * 1000))
    attachment_name = pdf_path.name
    pdf_bytes = pdf_path.read_bytes()

    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="from"\r\n\r\n{settings.mail_from}',
        f'--{boundary}\r\nContent-Disposition: form-data; name="to"\r\n\r\n{to}',
        f'--{boundary}\r\nContent-Disposition: form-data; name="subject"\r\n\r\n{subject}',
        f'--{boundary}\r\nContent-Disposition: form-data; name="html"\r\n\r\n{html}',
        f'--{boundary}\r\nContent-Disposition: form-data; name="attachments"; '
        f'filename="{attachment_name}"\r\nContent-Type: application/pdf\r\n\r\n',
    ]
    body = ("\r\n".join(parts)).encode("utf-8") + pdf_bytes + \
        f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise MailError(f"Resend 返回 {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MailError(f"无法连接 Resend: {exc}") from exc
    return {"channel": "resend", "response": raw}


def _safe_filename(to: str) -> str:
    """收件人邮箱 → 安全文件名（仅保留白名单字符，杜绝路径注入）。"""
    return "".join(c if c.isalnum() or c in "._+-" else "_" for c in to)


def _send_to_file(settings: Settings, to: str, subject: str, html: str,
                  pdf_path: Path) -> dict:
    """本地模拟：邮件元信息落盘（含 PDF 附件复制）。"""
    out_dir = settings.mail_out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    meta = {
        "to": to,
        "subject": subject,
        "html": html,
        "attachment": pdf_path.name,
        "channel": "file",
        "sent_at": ts,
    }
    safe_to = _safe_filename(to)
    meta_file = out_dir / f"quote-{ts}-{safe_to}.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    # 附件副本
    copy = out_dir / f"quote-{ts}-{pdf_path.name}"
    copy.write_bytes(pdf_path.read_bytes())
    return {"channel": "file", "meta_file": str(meta_file), "attachment_copy": str(copy)}
