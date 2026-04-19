#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import re
import os
import time
import json
import sqlite3
import requests
import subprocess
import shutil
from pathlib import Path
from core.extractor_v2 import DouYinAPIClient, MediaExtractor
from utils.db_handler import DBHandler
from action9_loop import GDriveClient, _download

# ================= 配置区 =================
DB_PATH = "output/global_tasks.db"
COOKIE_PATH = "input/cookies/douyin_cookie_1.json"
# ==========================================

def get_history_count(db_path, username):
    """查询该用户在数据库中已存在的视频总数"""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name='tasks'")
    if cur.fetchone()[0] == 0:
        return 0
    cur.execute("SELECT COUNT(*) FROM tasks WHERE folder_name=? AND media_type='video'", (username,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def main():
    p = argparse.ArgumentParser(description="Action 2: 单用户抓取与 GDrive 上传")
    p.add_argument("--text", required=True, help="包含抖音链接的分享文本")
    p.add_argument("--g_client_id",      default=os.environ.get("G_CLIENT_ID", ""))
    p.add_argument("--g_client_secret",  default=os.environ.get("G_CLIENT_SECRET", ""))
    p.add_argument("--g_refresh_token",  default=os.environ.get("G_REFRESH_TOKEN", ""))
    p.add_argument("--gdrive_root",      default=os.environ.get("GDRIVE_ROOT_FOLDER", ""))
    args = p.parse_args()

    print("="*50)
    print("🚀 Action 2: 抖音单用户极速抓取与 GDrive 上传器")
    print("="*50)
    
    text = args.text
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        print("❌ 未能在文本中找到有效的 HTTP/HTTPS 链接！")
        return
    
    raw_url = url_match.group(0)
    print(f"\n🔍 提取到链接: {raw_url}")
    
    long_url = raw_url
    if 'v.douyin.com' in raw_url:
        print("🔄 正在解析短链接...")
        try:
            resp = requests.get(raw_url, allow_redirects=True, timeout=10)
            long_url = resp.url
            print(f"✅ 真实链接: {long_url}")
        except Exception as e:
            print(f"❌ 短链接解析失败: {e}")
            return

    sec_uid_match = re.search(r'user/([A-Za-z0-9_\-]+)', long_url)
    if not sec_uid_match:
        print("❌ 未能在链接中提取到 sec_uid (用户唯一标识)！")
        return
    
    sec_uid = sec_uid_match.group(1)
    print(f"🆔 解析到 sec_uid: {sec_uid}")

    if not os.path.exists(COOKIE_PATH):
        print(f"❌ 找不到 Cookie 文件: {COOKIE_PATH}")
        return
    
    cookie_data = json.loads(Path(COOKIE_PATH).read_text(encoding="utf-8"))
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookie_data.get("cookies", []))
    client = DouYinAPIClient(cookie_str)
    db = DBHandler(DB_PATH)

    print("\n⏳ 正在抓取该用户主页所有数据，请稍候...")
    aweme_list = client.get_all_posts(sec_uid)
    if not aweme_list:
        print("⚠️ 该用户没有发布任何作品，或 Cookie 已失效。")
        return
    
    author_info = aweme_list[0].get("author", {})
    username = author_info.get("nickname") or sec_uid
    
    history_count = get_history_count(DB_PATH, username)
    print(f"\n👤 博主: 【{username}】")
    print(f"📊 数据库历史记录: 已存在 {history_count} 个视频")
    print(f"📡 网页实际抓取到: {len(aweme_list)} 个作品 (含图文)")

    batch_buffer = []
    for aweme in aweme_list:
        item = MediaExtractor.extract_all_media(aweme)
        if not item: continue
        base = {"title": item["desc"], "folder_name": username, "user_url": long_url, "create_time": item["create_time"]}
        for v in item["videos"]:
            batch_buffer.append({**base, "item_id": str(item['aweme_id']), "url": v["url"], "filename": v["filename"], "media_type": "video", "status": 1})
            
    if batch_buffer:
        db.save_tasks(batch_buffer)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE folder_name=? AND media_type='video' AND status IN (1,4) ORDER BY create_time", (username,))
    pending_videos = [dict(r) for r in cur.fetchall()]
    conn.close()

    new_count = len(pending_videos)
    print(f"🎉 成功筛选出新增待下载视频: {new_count} 个！")

    if new_count == 0:
        print("✅ 没有需要下载的新视频，任务结束。")
        return

    # 初始化 GDrive 客户端
    if not args.gdrive_root:
        print("⚠️ 未配置 GDRIVE_ROOT_FOLDER，无法上传至 Google Drive。")
        return
        
    gdrive = GDriveClient(args.g_client_id, args.g_client_secret, args.g_refresh_token, gdrive_root=args.gdrive_root)
    folder_id = gdrive.ensure_folder(username)

    print(f"\n🚀 开始逐个下载并上传至 Google Drive: /{username}/")
    for i, video in enumerate(pending_videos, 1):
        filename = video["filename"] or str(video["item_id"])
        tmp_path = f"/tmp/a2_{video['item_id']}"
        
        print(f"[{i}/{new_count}] ⬇️ 正在处理: {filename} ...")
        try:
            _download(video["url"], tmp_path, filename)
            if not os.path.exists(tmp_path):
                raise RuntimeError("下载未生成文件")
            
            print(f"    ⬆️ 正在上传至 Google Drive...")
            fid = gdrive.upload_file(tmp_path, filename, folder_id)
            
            db.update_status([video["item_id"]], 2, extra={"pikpak_file_id": fid, "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"    ✅ 成功! (已更新数据库)")
        except Exception as e:
            db.update_status([video["item_id"]], 4)
            print(f"    ❌ 失败! {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            
    print(f"\n🎉 单用户 {username} 抓取并上传完毕！")

if __name__ == "__main__":
    main()