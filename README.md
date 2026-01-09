# Elsevier Review Monitor

Automatically monitor **Elsevier manuscript review status** and receive **email notifications** when anything changes.

自动监控 Elsevier 论文审稿进度，在状态发生变化时通过 **邮件通知你**，无需反复手动刷新系统。

---

## Features | 功能特性

- Monitor Elsevier manuscript review status  
  监控 Elsevier 投稿系统中的论文审稿状态

- Email notification on status change  
  审稿进度发生变化时自动发送邮件提醒

- Smart change detection (no duplicate spam)  
  基于指纹比对，仅在真实变化时通知

- Structured status parsing  
  自动解析审稿完成数、邀请数、期刊、更新时间等字段

- Headless browser automation (Playwright)  
  使用 Playwright，无需人工操作浏览器

- Supports cron / Nezha Monitor / loop mode  
  支持 cron、哪吒监控、或常驻后台运行

---

## What Can Be Tracked | 可监控内容

| Field | Description |
|------|------------|
| Title | Paper title |
| Progress Status | e.g. `Required Reviews Complete` |
| Review Completed | 已完成评审数 |
| Review Accepted | 接受评审邀请（支持 `2+**`） |
| Review Invited | 发出评审邀请（支持 `2+**`） |
| Journal | 期刊名称 |
| Manuscript Number | 稿件编号 |
| Updated Date | 系统更新时间 |
| Submitted Date | 投稿日期 |

---

## Quick Start | 快速开始

### 1.Install dependencies

```bash
pip install playwright pyyaml requests
playwright install chromium

### 2.Modified Config 
add necessary information(ManuscriptID, last name and first name) into config.
配置文件中填写必要信息

### 3.Run 
```bash
python checkV01.py --config config.yaml --once 

