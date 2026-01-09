#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os
import re
import time
import ssl
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, Optional, Tuple, List

import yaml
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

URL = "https://webapps.elsevier.cn/st-wechat/manuscript-query"


# ----------------- basic utils -----------------

def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fp_from_obj(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def now_ts() -> int:
    return int(time.time())

def _pick_value_after(label: str, text: str) -> Optional[str]:
    """
    提取字段后面的原始展示值，如：
    2
    2+**
    """
    m = re.search(
        rf"{re.escape(label)}\*?\s*[:：]?\s*\n?\s*([0-9]+(?:\+\*\*)?)",
        text
    )
    if not m:
        return None
    return m.group(1)

def dump_debug(page, prefix: str) -> Tuple[str, str, str]:
    ts = now_ts()
    png = f"{prefix}_{ts}.png"
    htmlf = f"{prefix}_{ts}.html"
    jsonf = f"{prefix}_{ts}.inputs.json"

    try:
        page.screenshot(path=png, full_page=True)
    except Exception:
        png = ""

    try:
        with open(htmlf, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        htmlf = ""

    rows = []
    try:
        inputs = page.locator("input")
        n = inputs.count()
        for i in range(n):
            el = inputs.nth(i)
            try:
                visible = el.is_visible()
            except Exception:
                visible = False
            rows.append(
                {
                    "i": i,
                    "visible": visible,
                    "type": el.get_attribute("type"),
                    "name": el.get_attribute("name"),
                    "id": el.get_attribute("id"),
                    "placeholder": el.get_attribute("placeholder"),
                    "aria_label": el.get_attribute("aria-label"),
                    "class": el.get_attribute("class"),
                }
            )
        with open(jsonf, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    except Exception:
        jsonf = ""

    return png, htmlf, jsonf


# ----------------- parsing -----------------

def _pick(pattern: str, text: str, group: int = 1) -> Optional[str]:
    m = re.search(pattern, text, flags=re.I | re.S)
    if not m:
        return None
    val = m.group(group).strip()
    return val if val else None


def _pick_int_after(label: str, text: str) -> Optional[int]:
    m = re.search(rf"{re.escape(label)}\s*[:：]?\s*\n?\s*([0-9]+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def parse_result(raw: str) -> Dict[str, Any]:
    raw = raw.strip()

    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]

    title = None
    if lines:
        if len(lines) >= 2 and ("同行评审" in lines[0] or "进度" in lines[0] or "审稿" in lines[0]):
            title = lines[1]
        else:
            for ln in lines[:10]:
                if len(ln) >= 20:
                    title = ln
                    break

    structured = {
        "title": title,
        "updated_at": _pick(r"更新时间\s*[:：]?\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", raw),
        "progress_status": _pick(r"当前进度\s*[:：]?\s*\n?\s*([A-Za-z][A-Za-z \-]+)", raw),
        "review_completed": _pick_int_after("评审完成", raw),
        "review_accepted": _pick_value_after("接受评审邀请", raw),
        "review_invited": _pick_value_after("发出评审邀请", raw),
        "manuscript_number": _pick(r"Manuscript\s*Number\s*\n?\s*([A-Z]+-D-[0-9]{2}-[0-9]+)", raw),
        "journal": _pick(r"(?:^|\n)期刊\s*\n\s*([^\n]+)", raw),
        "submitted_at": _pick(r"提交日期\s*\n?\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", raw),
    }
    return structured


# ----------------- playwright helpers -----------------

def _fill_by_heading(page, heading_pattern: str, value: str, timeout_ms: int = 15000) -> None:
    anchor = page.get_by_text(re.compile(heading_pattern, re.I)).first
    anchor.wait_for(state="visible", timeout=timeout_ms)
    inp = anchor.locator("xpath=following::input[not(@type='hidden')][1]").first
    inp.wait_for(state="visible", timeout=timeout_ms)
    inp.fill(value)


def _check_consent(page) -> None:
    cb = page.locator("input[type='checkbox']:visible").first
    if cb.count() == 0:
        return
    try:
        if not cb.is_checked():
            cb.check()
    except Exception:
        try:
            cb.click(force=True)
        except Exception:
            pass


def _wait_query_enabled(page, timeout_ms: int = 20000) -> None:
    btn = page.locator("#query").first
    btn.wait_for(state="visible", timeout=15000)
    page.wait_for_function(
        "() => { const b=document.querySelector('#query'); return b && !b.disabled; }",
        timeout=timeout_ms,
    )


def _click_query(page) -> None:
    page.locator("#query").first.click()


def _looks_like_form_page(text: str) -> bool:
    t = text.lower()
    hits = sum(1 for kw in ["manuscript number", "last name", "first name"] if kw in t)
    return hits >= 2 and ("条款" in text or "terms" in t)


def _is_blocked(text: str) -> bool:
    t = text.lower()
    blocked_keywords = ["验证码", "人机", "访问过于频繁", "验证", "captcha", "security check"]
    return any(k.lower() in t for k in blocked_keywords)


def query_once(page, manuscript_number: str, last_name: str, first_name: str) -> str:
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    # 你当前这套可用就保持不动
    _fill_by_heading(page, r"Manuscript\s*Number|稿件|编号", manuscript_number)
    _fill_by_heading(page, r"Last\s*Name|姓", last_name)
    _fill_by_heading(page, r"First\s*Name|名", first_name)

    _check_consent(page)
    _wait_query_enabled(page, timeout_ms=20000)

    before = (page.inner_text("body") or "")[:1200]
    _click_query(page)

    page.wait_for_timeout(1000)
    try:
        page.wait_for_function(
            """(before) => {
                const t = (document.body && document.body.innerText) ? document.body.innerText.slice(0,1200) : '';
                return t && t !== before;
            }""",
            arg=before,
            timeout=30000,
        )
    except PWTimeoutError:
        dump_debug(page, "fail_no_change_after_click")

    page.wait_for_timeout(1200)
    body = (page.inner_text("body") or "").strip()
    if not body:
        dump_debug(page, "fail_empty_body")
        raise RuntimeError("Empty body after submit")

    if _is_blocked(body):
        dump_debug(page, "blocked")

    return body


# ----------------- email (HTML table + highlight) -----------------

def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html_diff(label: str, old: Optional[dict], new: dict) -> str:
    fields = [
        ("title", "论文标题"),
        ("progress_status", "当前进度"),
        ("review_completed", "评审完成"),
        ("review_accepted", "接受评审邀请"),
        ("review_invited", "发出评审邀请"),
        ("journal", "期刊"),
        ("manuscript_number", "稿件编号"),
        ("updated_at", "更新时间"),
        ("submitted_at", "提交日期"),
    ]

    def fmt(v):
        if v is None or v == "":
            return "-"
        return str(v)

    rows = []
    for key, label_cn in fields:
        old_v = fmt(old.get(key) if isinstance(old, dict) else None)
        new_v = fmt(new.get(key))

        changed = isinstance(old, dict) and old_v != new_v
        style = "background:#fff3cd;" if changed else ""
        rows.append(
            f"<tr style='{style}'>"
            f"<td style='padding:8px;font-weight:600;white-space:nowrap;'>{_escape(label_cn)}</td>"
            f"<td style='padding:8px;'>{_escape(old_v)}</td>"
            f"<td style='padding:8px;'>{_escape(new_v)}</td>"
            f"</tr>"
        )

    return f"""\
<html>
  <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.5;">
    <h2 style="margin:0 0 10px 0;">Elsevier 论文状态更新提醒</h2>
    <div style="margin:0 0 14px 0;"><b>标签：</b>{_escape(label)}</div>

    <table border="1" cellspacing="0" cellpadding="0" style="border-collapse:collapse;min-width:720px;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px;">字段</th>
          <th style="padding:8px;">之前</th>
          <th style="padding:8px;">当前</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>

    <div style="margin-top:12px;color:#666;font-size:12px;">
      说明：高亮行表示与上一次记录相比发生变化。
    </div>
  </body>
</html>
"""


def send_email(mail_cfg: dict, subject: str, html_body: str) -> None:
    host = mail_cfg["host"]
    port = int(mail_cfg.get("port", 465))
    username = mail_cfg["username"]
    password = mail_cfg["password"]
    mail_from = mail_cfg.get("from", username)
    to_list = mail_cfg["to"]
    if isinstance(to_list, str):
        to_list = [to_list]

    msg = MIMEMultipart("alternative")
    msg["From"] = mail_from
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()

    # 关键点：不要用 with SMTP_SSL(...) as s: 以避免 QUIT 返回异常字节导致 __exit__ 抛错
    s = smtplib.SMTP_SSL(host, port, timeout=25, context=ctx)
    try:
        s.login(username, password)
        s.sendmail(mail_from, to_list, msg.as_string())
        # 尽量优雅退出，但不让 QUIT 的怪响应影响整体成功
        try:
            s.quit()
        except Exception:
            try:
                s.close()
            except Exception:
                pass
    finally:
        try:
            s.close()
        except Exception:
            pass


# ----------------- main -----------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="config yaml path")
    ap.add_argument("--once", action="store_true", help="run once and exit (send email per policy)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state_file = cfg.get("state_file", "elsevier_state.json")
    interval_seconds = int(cfg.get("interval_seconds", 1800))

    papers = cfg.get("papers", {})
    if not papers:
        raise SystemExit("No papers found in config: papers:")

    mail_cfg = cfg.get("email", {})
    mail_enabled = bool(mail_cfg.get("enabled", False))

    send_on_first_run = bool(mail_cfg.get("send_on_first_run", True))
    send_on_change = bool(mail_cfg.get("send_on_change", True))
    send_on_no_change = bool(mail_cfg.get("send_on_no_change", False))
    subject_prefix = str(mail_cfg.get("subject_prefix", "[Elsevier]")).strip() or "[Elsevier]"

    state = load_state(state_file)

    def run_once() -> None:
        nonlocal state
        any_sent = False

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            page = context.new_page()

            for label, item in papers.items():
                mn = str(item["manuscript_number"]).strip()
                ln = str(item["last_name"]).strip()
                fn = str(item["first_name"]).strip()

                print(f"[check] {label}")
                try:
                    raw = query_once(page, mn, ln, fn)
                    structured = parse_result(raw)
                except Exception as e:
                    try:
                        dump_debug(page, "fail_exception")
                    except Exception:
                        pass
                    raw = f"ERROR: {type(e).__name__}: {e}"
                    structured = {"error": raw}

                fp = fp_from_obj(structured)

                prev = state.get(label, {})
                prev_fp = prev.get("fingerprint")
                first_run = prev_fp is None
                changed = (prev_fp is not None and prev_fp != fp)

                # 写入 state：text 内嵌 JSON + raw_text 兜底
                state[label] = {
                    "fingerprint": fp,
                    "text": structured,
                    "raw_text": raw[:4000],
                    "ts": now_ts(),
                    "manuscript_number": mn,
                    "last_name": ln,
                    "first_name": fn,
                }

                print(f"  prev_fp={prev_fp}")
                print(f"  curr_fp={fp}")
                print(f"  changed={changed}, first_run={first_run}")

                # 决策是否发信
                should_send = False
                reason = ""
                if mail_enabled:
                    if first_run and send_on_first_run:
                        should_send = True
                        reason = "first_run"
                    elif changed and send_on_change:
                        should_send = True
                        reason = "changed"
                    elif (not first_run and not changed) and send_on_no_change:
                        should_send = True
                        reason = "no_change"

                if should_send:
                    new_obj = structured
                    old_obj = prev.get("text") if isinstance(prev.get("text"), dict) else None
                    progress = (new_obj.get("progress_status") or "Unknown").strip()
                    subject = f"{subject_prefix} {progress} · {label}"
                    html = render_html_diff(label, old_obj, new_obj)
                    send_email(mail_cfg, subject, html)
                    any_sent = True
                    print(f"  [mail] sent ({reason})")

            browser.close()

        save_state(state_file, state)
        if not any_sent:
            print("Done (once). No email sent by policy.")
        else:
            print("Done (once). Email sent.")

    if args.once:
        run_once()
        return

    # loop mode
    while True:
        run_once()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
