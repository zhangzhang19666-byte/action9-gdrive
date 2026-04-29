import json
import re
import time
import random
import logging
import requests
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from utils.db_handler import DBHandler

logger = logging.getLogger("RescueExtractor")

class MediaExtractor:
    @staticmethod
    def get_video_url(video_data: dict, quality: str = "low") -> Optional[str]:
        if not video_data: return None
        sources = []
        for key in ("play_addr_h264", "play_addr", "play_addr_265"):
            addr = video_data.get(key, {})
            if addr.get("url_list"):
                sources.append({"urls": addr["url_list"], "size": addr.get("data_size", 0)})
        if not sources: return None
        sources.sort(key=lambda x: x["size"], reverse=(quality == "high"))
        url = sources[0]["urls"][0]
        return ("https:" + url) if url.startswith("//") else url

    @staticmethod
    def extract_all_media(aweme: dict, video_quality: str = "low") -> Optional[dict]:
        aweme_id = aweme.get("aweme_id")
        if not aweme_id: return None
        desc = (aweme.get("desc") or f"视频_{aweme_id}")[:80].strip()
        safe_desc = re.sub(r'[\\/:*?"<>|\n\r]', '', desc)[:50]
        create_time = aweme.get("create_time", 0)
        date_prefix = (datetime.fromtimestamp(create_time).strftime("%Y%m%d") if create_time else "00000000")
        aweme_type = aweme.get("aweme_type")
        images = aweme.get("images") or []
        result = {"aweme_id": aweme_id, "desc": desc, "safe_desc": safe_desc, "aweme_type": aweme_type,
                  "create_time": create_time, "date_prefix": date_prefix, "videos": [], "images": [], "audio": None}
        is_mixed = (aweme_type == 68 or len(images) > 0)
        music = aweme.get("music")
        if music and is_mixed:
            urls = music.get("play_url", {}).get("url_list", [])
            if urls:
                au = urls[0]
                result["audio"] = {"url": ("https:" + au) if au.startswith("//") else au,
                                   "filename": f"{date_prefix}_{safe_desc}_{aweme_id[-8:]}_audio.mp3",
                                   "title": music.get("title"), "duration": music.get("duration")}
        v_idx = i_idx = 0
        for img in images:
            v_info = img.get("video")
            if v_info:
                v_idx += 1
                urls = v_info.get("play_addr", {}).get("url_list", [])
                if urls:
                    v_type = "livephoto" if img.get("clip_type") == 5 else "clip"
                    vu = urls[0]
                    result["videos"].append({"url": ("https:" + vu) if vu.startswith("//") else vu,
                                             "filename": f"{date_prefix}_{safe_desc}_{aweme_id[-8:]}_{v_type}_{v_idx}.mp4",
                                             "type": v_type, "duration_ms": v_info.get("duration")})
            else:
                i_idx += 1
                urls = img.get("url_list") or img.get("download_url_list") or []
                if urls:
                    iu = urls[0]
                    if iu.startswith("//"): iu = "https:" + iu
                    ext = ("webp" if ".webp" in iu else ("png" if ".png" in iu else "jpg"))
                    result["images"].append({"url": iu, "filename": f"{date_prefix}_{safe_desc}_{aweme_id[-8:]}_img_{i_idx}.{ext}"})
        if not images:
            v_data = aweme.get("video", {})
            if v_data and aweme_type == 0:
                v_url = MediaExtractor.get_video_url(v_data, video_quality)
                if v_url:
                    result["videos"].append({"url": v_url, "filename": f"{date_prefix}_{safe_desc}_{aweme_id[-8:]}.mp4",
                                             "type": "video", "duration_ms": aweme.get("duration")})
        return result

class DouYinAPIClient:
    _PARAMS_BASE = {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web", "pc_client_type": "1", "version_code": "190600", "version_name": "19.6.0"}
    def __init__(self, cookie_str: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                                     "Referer": "https://www.douyin.com/", "Accept": "application/json, text/plain, */*", "Cookie": cookie_str})
    def get_all_posts(self, sec_uid: str) -> List[dict]:
        all_awemes = []; cursor = 0; has_more = True; page = 0
        while has_more:
            page += 1
            params = {**self._PARAMS_BASE, "sec_user_id": sec_uid, "max_cursor": cursor, "count": 20}
            try: 
                resp = self.session.get("https://www.douyin.com/aweme/v1/web/aweme/post/", params=params, timeout=20).json()
            except Exception: 
                break
            aweme_list = resp.get("aweme_list") or []
            all_awemes.extend(aweme_list)
            cursor = resp.get("max_cursor", 0); has_more = bool(resp.get("has_more", False))
            if not aweme_list: break
            time.sleep(random.uniform(0.5, 1.2))
        return all_awemes

class BatchProcessor:
    def __init__(self, json_file: str, output_root: str, cookie_path: str, limit: int = 10, download_all: bool = False, target_username: str = "", max_per_user: int = 0):
        self.output_root = Path(output_root)
        self.cookie_path = Path(cookie_path)
        self.limit = limit
        self.download_all = download_all
        self.target_username = target_username.strip()
        self.max_per_user = max_per_user  # 0 = 不限制
        self.global_db = DBHandler(str(self.output_root / "global_tasks.db"))
        
        all_authors = json.loads(Path(json_file).read_text(encoding="utf-8"))
        
        if self.target_username:
            self.main_queue = [a for a in all_authors if a['username'] == self.target_username]
            logger.info(f"🎯 单用户模式: {self.target_username} (找到 {len(self.main_queue)} 条匹配)")
        else:
            processed = self.global_db.get_processed_folder_names()
            pending = [a for a in all_authors if a['username'] not in processed]

            # 从中位数向两侧极值扩散：
            # 请求密度从温和逐渐向两极延伸，避免开场即高强度或全是零散请求
            pending_sorted = sorted(pending, key=lambda x: int(x.get('works') or 0), reverse=True)
            n = len(pending_sorted)
            mid = n // 2
            interleaved = [pending_sorted[mid]]
            for offset in range(1, mid + 1):
                if mid - offset >= 0:
                    interleaved.append(pending_sorted[mid - offset])  # 向大值扩散
                if mid + offset < n:
                    interleaved.append(pending_sorted[mid + offset])  # 向小值扩散

            self.main_queue = interleaved
            logger.info(f"📂 博主总数 {len(all_authors)}，DB 已有 {len(processed)} 个，"
                        f"待处理 {len(self.main_queue)} 个（大小交替排列）。")
        
        # 获取需要重抓的博主 (Status 3)
        self.retry_queue = self.global_db.get_users_by_status(3)
        
        self.batch_buffer = [] 
        self.session_flush_count = 0

    def _flush_buffer(self):
        if not self.batch_buffer: return
        logger.info(f"🚀 [状态管理] 正在落盘 {len(self.batch_buffer)} 条抓取结果 (Status 1)")
        self.global_db.save_tasks(self.batch_buffer)
        self.batch_buffer = []
        self.session_flush_count += 1

    def process_user(self, username, profile_url, client: DouYinAPIClient, index, total):
        logger.info(f"[{index}/{total}] 🚀 正在抓取: {username}")
        try:
            match = re.search(r"user/([A-Za-z0-9_\-]+)", profile_url)
            if not match: return False
            sec_uid = match.group(1)
            
            aweme_list = client.get_all_posts(sec_uid)
            if self.max_per_user > 0 and len(aweme_list) > self.max_per_user:
                logger.info(f"  ✂️  {username} 共 {len(aweme_list)} 条，按 --max-per-user={self.max_per_user} 截取最新部分")
                aweme_list = aweme_list[:self.max_per_user]
            count = 0
            for aweme in aweme_list:
                item = MediaExtractor.extract_all_media(aweme)
                if not item: continue
                
                base = {"title": item["desc"], "folder_name": username, "user_url": profile_url, "create_time": item["create_time"]}
                non_video_status = 1 if self.download_all else 3

                # 视频 item_id: 第一个视频保持原 aweme_id，后续视频增加索引后缀
                for i, v in enumerate(item["videos"]):
                    v_item_id = str(item['aweme_id']) if i == 0 else f"{item['aweme_id']}_v_{i}"
                    self.batch_buffer.append({**base, "item_id": v_item_id, "url": v["url"], "filename": v["filename"], "media_type": "video", "status": 1})
                    count += 1
                
                # 图片和音频仍需后缀以保证同一帖子内不冲突
                for i, img in enumerate(item["images"]):
                    self.batch_buffer.append({**base, "item_id": f"{item['aweme_id']}_i_{i}", "url": img["url"], "filename": img["filename"], "media_type": "image", "status": non_video_status})
                    count += 1
                if item["audio"]:
                    a = item["audio"]
                    self.batch_buffer.append({**base, "item_id": f"{item['aweme_id']}_a", "url": a["url"], "filename": a["filename"], "media_type": "audio", "status": non_video_status})
                    count += 1

            if len(self.batch_buffer) >= 900:
                self._flush_buffer()
            return True
        except Exception as e:
            logger.error(f"  ❌ {username} 异常: {e}")
            return False


    def run(self):
        cookie_data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookie_data.get("cookies", []))
        client = DouYinAPIClient(cookie_str)

        # 仅在全局扫描模式下处理重试队列，避免单博主循环模式下重复抓取
        if self.retry_queue and not self.target_username:
            logger.info(f"♻️ 发现 {len(self.retry_queue)} 个博主需要重抓...")
            for i, (name, url) in enumerate(self.retry_queue, 1):
                self.process_user(name, url, client, i, len(self.retry_queue))
                if i < len(self.retry_queue):
                    time.sleep(20)  # retry 用户不知道 works 数，保守用 20s
            # retry_queue 结束后重置，避免占用主队列的 700 阈值配额
            self.session_flush_count = 0

        processed_count = 0
        for i, author in enumerate(self.main_queue, 1):
            if self.session_flush_count > 0:
                logger.info(f"🛑 [900 阈值停机] 采集任务达标，退出。")
                break

            self.process_user(author['username'], author['profileUrl'], client, i, len(self.main_queue))
            processed_count += 1
            if self.limit > 0 and processed_count >= self.limit: break

            if i < len(self.main_queue):
                time.sleep(20)
            
        self._flush_buffer()
        logger.info("🎉 抓取阶段结束。")
