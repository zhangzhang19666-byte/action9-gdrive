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

# ================= 配置区 =================
DB_PATH = "output/global_tasks.db"
COOKIE_PATH = "input/cookies/douyin_cookie_1.json"
DOWNLOAD_DIR = "output/downloads"
# ==========================================

_ARIA2_AVAILABLE = shutil.which("aria2c") is not None

def _download(url: str, filepath: str):
    """下载文件，优先使用 aria2c，失败回退 requests"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    filename = os.path.basename(filepath)
    
    if _ARIA2_AVAILABLE:
        cmd = [
            "aria2c", "-x", "8", "-s", "8", "-k", "5M",
            "--max-tries=3", "--retry-wait=5", "--connect-timeout=20", "--timeout=300",
            "--user-agent=Mozilla/5.0", "--allow-overwrite=true",
            "-d", os.path.dirname(filepath), "-o", filename, url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(filepath):
            return True
        print(f"⚠️ aria2c 下载失败，改用 requests...")

    try:
        with requests.get(url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=60) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return False

def get_history_count(db_path, username):
    """查询该用户在数据库中已存在的视频总数"""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # 检查表是否存在
    cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name='tasks'")
    if cur.fetchone()[0] == 0:
        return 0
    
    cur.execute("SELECT COUNT(*) FROM tasks WHERE folder_name=? AND media_type='video'", (username,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def main():
    print("="*50)
    print("🚀 抖音单用户 极速抓取与下载器")
    print("="*50)
    
    # 1. 获取输入并提取 URL
    text = input("\n👉 请粘贴抖音分享文本或链接: \n")
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        print("❌ 未能在文本中找到有效的 HTTP/HTTPS 链接！")
        return
    
    raw_url = url_match.group(0)
    print(f"\n🔍 提取到链接: {raw_url}")
    
    # 2. 解析真实 URL 和 sec_uid
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

    # 3. 初始化 API 客户端
    if not os.path.exists(COOKIE_PATH):
        print(f"❌ 找不到 Cookie 文件: {COOKIE_PATH}")
        return
    
    cookie_data = json.loads(Path(COOKIE_PATH).read_text(encoding="utf-8"))
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookie_data.get("cookies", []))
    client = DouYinAPIClient(cookie_str)
    db = DBHandler(DB_PATH)

    # 4. 获取全量数据以获取 Username 和比对
    print("\n⏳ 正在抓取该用户主页所有数据，请稍候...")
    aweme_list = client.get_all_posts(sec_uid)
    if not aweme_list:
        print("⚠️ 该用户没有发布任何作品，或 Cookie 已失效。")
        return
    
    # 从第一条数据中提取用户名
    author_info = aweme_list[0].get("author", {})
    username = author_info.get("nickname") or sec_uid
    
    # 5. 历史比对统计
    history_count = get_history_count(DB_PATH, username)
    print(f"\n👤 博主: 【{username}】")
    print(f"📊 数据库历史记录: 已存在 {history_count} 个视频")
    print(f"📡 网页实际抓取到: {len(aweme_list)} 个作品 (含图文)")

    # 6. 解析并入库
    batch_buffer = []
    for aweme in aweme_list:
        item = MediaExtractor.extract_all_media(aweme)
        if not item: continue
        
        base = {
            "title": item["desc"], 
            "folder_name": username, 
            "user_url": long_url, 
            "create_time": item["create_time"]
        }
        
        for v in item["videos"]:
            batch_buffer.append({
                **base, 
                "item_id": str(item['aweme_id']), 
                "url": v["url"], 
                "filename": v["filename"], 
                "media_type": "video", 
                "status": 1  # 1表示待下载
            })
            
    if batch_buffer:
        db.save_tasks(batch_buffer)
    
    # 7. 统计新增视频
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE folder_name=? AND media_type='video' AND status=1 ORDER BY create_time DESC", (username,))
    pending_videos = [dict(r) for r in cur.fetchall()]
    conn.close()

    new_count = len(pending_videos)
    print(f"🎉 成功筛选出新增待下载视频: {new_count} 个！")

    if new_count == 0:
        print("✅ 没有需要下载的新视频，任务结束。")
        return

    # 8. 开始下载
    print(f"\n🚀 开始下载 {new_count} 个新视频到本地: {DOWNLOAD_DIR}/{username}/")
    for i, video in enumerate(pending_videos, 1):
        filename = video["filename"] or f"{video['item_id']}.mp4"
        save_path = os.path.join(DOWNLOAD_DIR, username, filename)
        
        print(f"[{i}/{new_count}] ⬇️ 正在下载: {filename} ...")
        success = _download(video["url"], save_path)
        
        if success:
            db.update_status([video["item_id"]], 2, extra={"pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"    ✅ 完成! (已更新数据库状态为已下载)")
        else:
            db.update_status([video["item_id"]], 4)  # 4表示失败
            print(f"    ❌ 失败! (可后续重试)")
            
    print(f"\n🎉 抓取并下载完毕！所有视频保存在: {os.path.abspath(DOWNLOAD_DIR)}")

if __name__ == "__main__":
    main()
