#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Action 9 — 逐用户抓取 + 并行下载视频到 GDrive

流程（每个用户独立完整处理）：
  For each user:
    1. 抓取该用户所有内容 URL（写入 DB）
    2. 只下载 media_type=video 的文件 → 并行上传到 GDrive
       根目录: 1fHOUUBEKVb4RMgYWqDWSmwKLSaILhwe5/{用户名}/
  → 继续下一个用户

时间限制：运行满 --time_limit 秒（默认5h）后停止，写续跑标志文件，
          由 workflow 检测后自动触发下一次运行。

支持续传：已下载文件（status=2）跳过，新增内容自动补充
并行：--workers N（默认8），每个 worker 独立下载一个文件再上传
"""
import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from core.extractor_v2 import BatchProcessor
from utils.db_handler import DBHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("Action9")

GDRIVE_API       = "https://www.googleapis.com/drive/v3/files"
GDRIVE_UPLOAD    = "https://www.googleapis.com/upload/drive/v3/files"
GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# GDRIVE_ROOT 由 CLI --gdrive_root 传入，或环境变量 GDRIVE_ROOT_FOLDER
GDRIVE_ROOT      = ""   # 运行时由 main() 赋值
CHUNK_SIZE       = 16 * 1024 * 1024   # 16MB per GDrive chunk
TOKEN_TTL        = 3000                # refresh GDrive token every ~50 min
CONTINUE_FLAG    = Path("output/.continuation_needed")


# ── Google Drive 客户端（线程安全）────────────────────────────

class GDriveClient:

    def __init__(self, client_id, client_secret, refresh_token, gdrive_root: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.gdrive_root   = gdrive_root
        self._lock         = threading.Lock()
        self._refreshed_at = 0.0
        self.token         = self._do_refresh()
        self.folder_cache  = {}
        self._folder_lock  = threading.Lock()

    def _do_refresh(self):
        r = requests.post(GDRIVE_TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }, timeout=30)
        r.raise_for_status()
        t = r.json().get("access_token")
        if not t:
            raise ValueError(f"GDrive token 刷新失败: {r.text}")
        self._refreshed_at = time.time()
        return t

    def _h(self):
        with self._lock:
            if time.time() - self._refreshed_at > TOKEN_TTL:
                logger.info("🔄 GDrive token 自动续期...")
                self.token = self._do_refresh()
            return {"Authorization": f"Bearer {self.token}"}

    def ensure_folder(self, name: str, parent: str = "") -> str:
        parent = parent or self.gdrive_root
        key = f"{parent}/{name}"
        with self._folder_lock:
            if key in self.folder_cache:
                return self.folder_cache[key]
        q = (f"name='{name}' and '{parent}' in parents "
             f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
        r = requests.get(GDRIVE_API, headers=self._h(),
                         params={"q": q, "fields": "files(id)"}, timeout=20)
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            fid = files[0]["id"]
        else:
            r2 = requests.post(GDRIVE_API, headers=self._h(), json={
                "name": name, "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent],
            }, timeout=20)
            r2.raise_for_status()
            fid = r2.json()["id"]
            logger.info(f"   📁 新建文件夹: {name}")
        with self._folder_lock:
            self.folder_cache[key] = fid
        return fid

    def _init_upload(self, filename, folder_id, total):
        r = requests.post(
            f"{GDRIVE_UPLOAD}?uploadType=resumable",
            headers={**self._h(), "Content-Type": "application/json",
                     "X-Upload-Content-Type": "application/octet-stream",
                     "X-Upload-Content-Length": str(total)},
            json={"name": filename, "parents": [folder_id]}, timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"GDrive init 失败 {r.status_code}: {r.text}")
        loc = r.headers.get("Location")
        if not loc:
            raise ValueError("GDrive 未返回 Location")
        return loc

    def upload_file(self, tmp_path: str, filename: str, folder_id: str) -> str:
        """从本地文件上传到 GDrive，返回 file_id"""
        total    = os.path.getsize(tmp_path)
        up_url   = self._init_upload(filename, folder_id, total)
        uploaded, t0, last_log = 0, time.time(), 0.0

        with open(tmp_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                end     = uploaded + len(chunk) - 1
                is_last = (end + 1 >= total)
                
                max_retries = 5
                for attempt in range(max_retries):
                    try:
                        resp = requests.put(up_url, data=chunk, headers={
                            "Content-Type":  "application/octet-stream",
                            "Content-Range": f"bytes {uploaded}-{end}/{total if is_last else '*'}",
                        }, timeout=300)
                        
                        # 5xx server errors might be transient
                        if resp.status_code in (500, 502, 503, 504) and attempt < max_retries - 1:
                            logger.warning(f"   ⚠️  GDrive 服务器错误 {resp.status_code}，正在重试上传块 ({attempt + 1}/{max_retries})...")
                            time.sleep(5)
                            continue
                            
                        if resp.status_code not in (200, 201, 308):
                            raise RuntimeError(f"上传块失败 {resp.status_code}: {resp.text[:120]}")
                        break
                    except requests.exceptions.RequestException as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"   ⚠️  GDrive 块上传中断 ({e})，正在重试 ({attempt + 1}/{max_retries})...")
                            time.sleep(5)
                        else:
                            raise RuntimeError(f"GDrive 块上传重试 {max_retries} 次后仍失败: {e}")
                            
                uploaded += len(chunk)

                mb_done = uploaded / 1024 / 1024
                if mb_done - last_log >= 100 or resp.status_code in (200, 201):
                    elapsed = time.time() - t0
                    spd = uploaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                    logger.info(f"   ⬆️  {filename}: {mb_done:.0f}/{total/1024/1024:.0f}MB  {spd:.1f}MB/s")
                    last_log = mb_done

                if resp.status_code in (200, 201):
                    elapsed = time.time() - t0
                    logger.info(
                        f"   ✅ 上传完成 {filename}  {total/1024/1024:.1f}MB  "
                        f"耗时={elapsed:.0f}s  速度={total/elapsed/1024/1024:.1f}MB/s"
                    )
                    return resp.json().get("id", "")

        raise RuntimeError(f"上传未完成 uploaded={uploaded}/{total}")


# ── 下载辅助 ─────────────────────────────────────────────────

_ARIA2_AVAILABLE = shutil.which("aria2c") is not None

def _download(url: str, tmp_path: str, filename: str) -> None:
    """下载 URL 到 tmp_path。优先 aria2c（8连接），失败回退 requests。"""
    if _ARIA2_AVAILABLE:
        cmd = [
            "aria2c",
            "-x", "8", "-s", "8", "-k", "5M",
            "--max-tries=3", "--retry-wait=5",
            "--connect-timeout=20", "--timeout=300",
            "--user-agent=Mozilla/5.0",
            "--allow-overwrite=true",
            "-d", str(Path(tmp_path).parent),
            "-o", str(Path(tmp_path).name),
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0 and os.path.exists(tmp_path):
            return
        logger.warning(f"   ⚠️  aria2c 失败 (rc={result.returncode})，改用 requests")

    # requests 回退 (支持断点续传与重试)
    max_retries = 5
    for attempt in range(max_retries):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            downloaded = 0
            if os.path.exists(tmp_path):
                downloaded = os.path.getsize(tmp_path)
            
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
                
            with requests.get(url, stream=True, headers=headers, timeout=120) as r:
                if r.status_code == 416: # 已经下载完成
                    return
                r.raise_for_status()
                
                total_remaining = int(r.headers.get("Content-Length", 0))
                if r.status_code == 206:
                    mode = "ab"
                    total = downloaded + total_remaining
                    session_downloaded = 0
                else:
                    mode = "wb"
                    downloaded = 0
                    total = total_remaining
                    session_downloaded = 0

                last_log = downloaded / 1024 / 1024
                t0 = time.time()
                with open(tmp_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            session_downloaded += len(chunk)
                            mb = downloaded / 1024 / 1024
                            if mb - last_log >= 20:
                                elapsed = time.time() - t0
                                spd = session_downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                                tot_s = f"/{total/1024/1024:.0f}" if total else ""
                                logger.info(f"   ⬇️  {filename}: {mb:.0f}{tot_s}MB  {spd:.1f}MB/s")
                                last_log = mb
            # 完整读取未抛异常则成功
            return
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"   ⚠️  requests 下载中止 ({e})，正在断点重试 ({attempt + 1}/{max_retries})...")
                time.sleep(5)
            else:
                raise RuntimeError(f"requests 重试 {max_retries} 次后仍失败: {e}")


# ── DB 辅助 ───────────────────────────────────────────────────

def user_already_scraped(db_path: str, folder_name: str) -> bool:
    """该用户在 DB 中有任意记录 → 已在之前的运行中抓取过，本次跳过"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tasks WHERE folder_name=? LIMIT 1", (folder_name,))
    found = cur.fetchone() is not None
    conn.close()
    return found

def get_pending_videos(db_path: str, folder_name: str) -> list:
    """获取某用户待下载的视频（status IN (1,4) AND media_type='video'）"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM tasks WHERE folder_name=? AND status IN (1,4) AND media_type='video' ORDER BY create_time",
        (folder_name,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def count_by_user(db_path: str, folder_name: str) -> dict:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE folder_name=? GROUP BY status",
        (folder_name,)
    )
    result = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return result

def global_stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT folder_name) FROM tasks")
    users = cur.fetchone()[0]
    cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
    status_counts = {row[0]: row[1] for row in cur.fetchall()}
    # 视频专项统计
    cur.execute("SELECT status, COUNT(*) FROM tasks WHERE media_type='video' GROUP BY status")
    video_counts = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return {"users": users, "status": status_counts, "video": video_counts}


# ── 主控制器 ──────────────────────────────────────────────────

class Action9:

    def __init__(self, db_path, json_file, cookie_path, gdrive: GDriveClient,
                 workers: int = 8, time_limit: int = 18000):
        if not gdrive.gdrive_root:
            raise ValueError("GDRIVE_ROOT_FOLDER 未设置，请通过 --gdrive_root 或环境变量传入")
        self.db_path     = db_path
        self.json_file   = json_file
        self.cookie_path = cookie_path
        self.db          = DBHandler(db_path)
        self.gdrive      = gdrive
        self.workers     = workers
        self.time_limit  = time_limit
        self.start_time  = 0.0
        self._db_lock    = threading.Lock()
        self._cnt_lock   = threading.Lock()
        self.total_users_done = 0
        self.total_files_done = 0
        self.total_files_fail = 0

    def _elapsed(self) -> float:
        return time.time() - self.start_time

    def _time_up(self) -> bool:
        return self._elapsed() >= self.time_limit

    def _print_stats(self):
        s  = global_stats(self.db_path)
        st = s["status"]
        vd = s["video"]
        logger.info(
            f"📊 全局统计 | 用户: {s['users']}  "
            f"视频已下: {vd.get(2,0)}  视频待下: {vd.get(1,0)+vd.get(4,0)}  "
            f"本次: 用户={self.total_users_done} 视频成功={self.total_files_done} 失败={self.total_files_fail}  "
            f"已运行={self._elapsed()/60:.0f}min"
        )

    def _handle_one(self, item: dict, folder_id: str) -> tuple[bool, str]:
        """单文件：下载到 /tmp → 上传 GDrive → 清理"""
        item_id  = item["item_id"]
        url      = item["url"]
        filename = item["filename"] or str(item_id)
        tmp_path = f"/tmp/a9_{item_id}"

        try:
            t0 = time.time()
            _download(url, tmp_path, filename)
            size_mb = os.path.getsize(tmp_path) / 1024 / 1024
            dl_s    = time.time() - t0
            logger.info(f"   ⬇️  完成 {filename}  {size_mb:.1f}MB  下载={dl_s:.0f}s  {size_mb/dl_s:.1f}MB/s")

            fid = self.gdrive.upload_file(tmp_path, filename, folder_id)

            with self._db_lock:
                self.db.update_status([item_id], 2,
                                      {"pikpak_file_id": fid,
                                       "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            return True, ""

        except Exception as e:
            with self._db_lock:
                self.db.update_status([item_id], 4)
            return False, str(e)

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _process_user(self, username: str, idx: int, total: int):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{idx}/{total}] 正在处理 {username}  已运行 {self._elapsed()/60:.0f}min")

        # Step 1: 抓取（如果已存在抓取记录则跳过，但需进入 Step 2 检查未完成的下载）
        if user_already_scraped(self.db_path, username):
            logger.info(f"  ⏭️  {username} 已抓取，跳过 Scraping 步骤。")
        else:
            logger.info("  🔍 抓取中...")
            try:
                BatchProcessor(
                    json_file       = self.json_file,
                    output_root     = str(Path(self.db_path).parent),
                    cookie_path     = self.cookie_path,
                    limit           = 0,
                    download_all    = True,
                    target_username = username,
                ).run()
            except Exception as e:
                logger.error(f"  ❌ 抓取失败: {e}")

        # Step 2: 只下载视频 (不论刚才是否执行了抓取)
        pending = get_pending_videos(self.db_path, username)
        counts  = count_by_user(self.db_path, username)
        logger.info(
            f"  📋 {username}: 待下载视频={len(pending)}  "
            f"已完成={counts.get(2,0)}  (图片/音频不下载)"
        )

        if not pending:
            logger.info("  ✅ 无待下载视频。")
            with self._cnt_lock:
                self.total_users_done += 1
            return

        folder_id = self.gdrive.ensure_folder(username)
        ok_cnt, fail_cnt = 0, 0

        logger.info(f"  🚀 并行下载 {len(pending)} 个视频 (workers={self.workers})")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._handle_one, item, folder_id): item
                for item in pending
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                item  = futures[fut]
                fname = item["filename"] or item["item_id"]
                ok, err = fut.result()
                if ok:
                    ok_cnt += 1
                    with self._cnt_lock:
                        self.total_files_done += 1
                    logger.info(f"  ✅ [{done}/{len(pending)}] {fname}")
                else:
                    fail_cnt += 1
                    with self._cnt_lock:
                        self.total_files_fail += 1
                    logger.error(f"  ❌ [{done}/{len(pending)}] {fname}: {err}")

        logger.info(f"  📊 {username} 完成: 视频成功={ok_cnt}  失败={fail_cnt}")
        with self._cnt_lock:
            self.total_users_done += 1
        self._print_stats()

    def run(self):
        self.start_time = time.time()
        logger.info(f"⚙️  aria2={'可用' if _ARIA2_AVAILABLE else '不可用(requests模式)'}  "
                    f"workers={self.workers}  time_limit={self.time_limit/3600:.1f}h")

        # 1. 在开始循环前，先执行一次全局重试队列 (Status 3) 的抓取
        logger.info("🔄 正在执行启动前的全局失效任务重试 (Status 3)...")
        try:
            BatchProcessor(
                json_file       = self.json_file,
                output_root     = str(Path(self.db_path).parent),
                cookie_path     = self.cookie_path,
                limit           = 0,
                download_all    = True,
            ).run()
        except Exception as e:
            logger.error(f"⚠️  初始重试抓取失败 (已跳过): {e}")

        authors = json.loads(Path(self.json_file).read_text(encoding="utf-8"))
        logger.info(f"👥 共 {len(authors)} 个用户（只下载视频）")
        self._print_stats()

        # 清除上次的续跑标志
        CONTINUE_FLAG.unlink(missing_ok=True)

        for i, author in enumerate(authors, 1):
            if self._time_up():
                logger.info(
                    f"\n⏰ 已运行 {self._elapsed()/3600:.2f}h，到达时间限制，停止处理新用户"
                )
                self._print_stats()
                self._write_continuation()
                return

            self._process_user(
                username = author["username"],
                idx      = i,
                total    = len(authors),
            )

        logger.info("\n🎉 全部用户处理完毕")
        self._print_stats()

        # 如果还有失败项（status=4），也写续跑标志让下次重试
        s  = global_stats(self.db_path)
        vd = s["video"]
        if vd.get(4, 0) > 0:
            logger.info(f"⚠️  仍有 {vd[4]} 个视频失败(status=4)，写续跑标志")
            self._write_continuation()

    def _write_continuation(self):
        CONTINUE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        CONTINUE_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(f"📌 续跑标志已写入: {CONTINUE_FLAG}")


# ── 入口 ──────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Action 9: 逐用户抓取视频 → 并行上传 GDrive")
    p.add_argument("--db",               default="output/global_tasks.db")
    p.add_argument("--json",             default="input/douyin_works.json", dest="json_file")
    p.add_argument("--cookie",           default="input/cookies/douyin_cookie_1.json")
    p.add_argument("--g_client_id",      required=True)
    p.add_argument("--g_client_secret",  required=True)
    p.add_argument("--g_refresh_token",  required=True)
    p.add_argument("--gdrive_root",
                   default=os.environ.get("GDRIVE_ROOT_FOLDER", ""),
                   help="GDrive 根文件夹 ID（也可用环境变量 GDRIVE_ROOT_FOLDER）")
    p.add_argument("--workers",          type=int, default=8,     help="并行 worker 数")
    p.add_argument("--time_limit",       type=int, default=18000, help="运行时间上限（秒），默认5h")
    args = p.parse_args()

    gdrive = GDriveClient(args.g_client_id, args.g_client_secret, args.g_refresh_token,
                          gdrive_root=args.gdrive_root)
    Action9(
        db_path    = args.db,
        json_file  = args.json_file,
        cookie_path= args.cookie,
        gdrive     = gdrive,
        workers    = args.workers,
        time_limit = args.time_limit,
    ).run()


if __name__ == "__main__":
    main()
