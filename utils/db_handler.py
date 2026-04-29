import sqlite3
import os
from datetime import datetime

class DBHandler:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化最洁净的数据库结构"""
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 1:抓取, 2:已推送, 3:待重抓, 4:待重推, 5:更名成功, 6:导出标准链接
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT UNIQUE,
                title TEXT,
                url TEXT,
                filename TEXT,
                folder_name TEXT,
                user_url TEXT,
                media_type TEXT,
                create_time INTEGER,
                status INTEGER DEFAULT 1,
                pikpak_task_id TEXT,
                file_hash TEXT,
                file_size INTEGER,
                pikpak_file_id TEXT,
                pikpak_link TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pushed_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def save_tasks(self, tasks_data):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        for t in tasks_data:
            # 使用 UPSERT (ON CONFLICT) 逻辑: 
            # 如果 item_id 已存在, 且处于重试状态 (status=3), 则重置为待处理 (status=1)
            cursor.execute('''
                INSERT INTO tasks 
                (item_id, title, url, filename, folder_name, user_url, media_type, create_time, status, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(item_id) DO UPDATE SET
                    status = excluded.status,
                    extracted_at = excluded.extracted_at
                WHERE tasks.status = 3
            ''', (t['item_id'], t['title'], t['url'], t['filename'], t['folder_name'],
                  t['user_url'], t['media_type'], t['create_time'], t.get('status', 1)))
        conn.commit()
        conn.close()

    def get_pending_push(self, limit=700):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM tasks
            WHERE (
                (status = 1 AND datetime(extracted_at, 'localtime') > datetime('now', 'localtime', '-130 minutes'))
                OR status = 4
            )
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_users_by_status(self, status):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT folder_name, user_url FROM tasks WHERE status = ?", (status,))
        res = cursor.fetchall()
        conn.close()
        return res

    def update_status(self, item_ids, status, extra=None):
        """统一的更新状态方法，支持额外字段"""
        if not item_ids: return
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        sql = f"UPDATE tasks SET status = ?"
        params = [status]
        if extra:
            for k, v in extra.items():
                sql += f", {k} = ?"
                params.append(v)
        sql += f" WHERE item_id = ?"
        batch_params = [(*params, iid) for iid in item_ids]
        cursor.executemany(sql, batch_params)
        conn.commit()
        conn.close()

    def batch_update_by_item_id(self, item_ids, status, extra_fields=None):
        """兼容旧调用的别名"""
        self.update_status(item_ids, status, extra_fields)

    def get_processed_folder_names(self):
        """返回数据库中已存在记录的所有 folder_name 集合（用于跳过已抓取博主）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT folder_name FROM tasks")
        result = {row[0] for row in cursor.fetchall()}
        conn.close()
        return result

    def get_url_expired_tasks(self):
        """返回 Status=2 且 pushed_at 超过 3 小时的任务（Douyin URL 已失效，需驱逐出 PikPak 队列）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT item_id, pikpak_task_id FROM tasks
            WHERE status = 2
            AND pushed_at IS NOT NULL
            AND datetime(pushed_at, 'localtime') <= datetime('now', 'localtime', '-180 minutes')
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def mark_expired_as_retry(self):
        """将超过 130 分钟未推送的 Status 1 任务标记为 Status 3 (待重抓)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE tasks SET status = 3
            WHERE status = 1
            AND datetime(extracted_at, 'localtime') <= datetime('now', 'localtime', '-130 minutes')
        ''')
        conn.commit()
        conn.close()
