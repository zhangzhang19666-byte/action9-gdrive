#!/usr/bin/env python3
"""
gdrive_db_sync.py — 将 global_tasks.db 上传/下载到 Google Drive

用法:
  python gdrive_db_sync.py --action upload   [--db_path output/global_tasks.db]
  python gdrive_db_sync.py --action download [--db_path output/global_tasks.db]

凭证通过环境变量读取: G_CLIENT_ID, G_CLIENT_SECRET, G_REFRESH_TOKEN, GDRIVE_ROOT_FOLDER
也可通过 CLI 参数传入: --g_client_id, --g_client_secret, --g_refresh_token, --gdrive_db_folder
"""
import argparse
import os
import sys
import time

import requests

GDRIVE_API       = "https://www.googleapis.com/drive/v3/files"
GDRIVE_UPLOAD    = "https://www.googleapis.com/upload/drive/v3/files"
GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK_SIZE       = 16 * 1024 * 1024  # 16MB
DB_FILENAME      = "global_tasks.db"


def _get_token(client_id, client_secret, refresh_token):
    r = requests.post(GDRIVE_TOKEN_URL, data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"Token 刷新失败: {r.text}")
    return tok


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _find_file(token, folder_id):
    """在 folder_id 中查找 global_tasks.db，返回 file_id 或 None。"""
    q = f"name='{DB_FILENAME}' and '{folder_id}' in parents and trashed=false"
    r = requests.get(GDRIVE_API, headers=_auth(token),
                     params={"q": q, "fields": "files(id,size)"}, timeout=20)
    r.raise_for_status()
    files = r.json().get("files", [])
    if files:
        size = int(files[0].get("size", 0))
        print(f"  📄 找到 {DB_FILENAME}  ID={files[0]['id']}  大小={size/1024/1024:.1f}MB")
        return files[0]["id"]
    return None


def _init_resumable(token, folder_id, total, existing_id=None):
    """初始化 GDrive 可恢复上传，返回上传 URI。"""
    hdrs = {
        **_auth(token),
        "Content-Type": "application/json",
        "X-Upload-Content-Type": "application/octet-stream",
        "X-Upload-Content-Length": str(total),
    }
    if existing_id:
        url = f"{GDRIVE_UPLOAD}/{existing_id}?uploadType=resumable"
        r = requests.patch(url, headers=hdrs, json={}, timeout=30)
    else:
        url = f"{GDRIVE_UPLOAD}?uploadType=resumable"
        r = requests.post(url, headers=hdrs,
                          json={"name": DB_FILENAME, "parents": [folder_id]}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Init 上传失败 {r.status_code}: {r.text[:200]}")
    loc = r.headers.get("Location")
    if not loc:
        raise RuntimeError("响应中无 Location 头")
    return loc


def do_upload(token, db_path, folder_id):
    total = os.path.getsize(db_path)
    print(f"📦 准备上传: {db_path}  大小={total/1024/1024:.1f}MB")

    existing_id = _find_file(token, folder_id)
    if existing_id:
        print("  🔄 将覆盖更新现有文件...")
    else:
        print("  ➕ GDrive 中无此文件，将新建...")

    up_url = _init_resumable(token, folder_id, total, existing_id)
    uploaded, t0 = 0, time.time()

    with open(db_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            end     = uploaded + len(chunk) - 1
            is_last = (end + 1 >= total)

            for attempt in range(5):
                try:
                    resp = requests.put(up_url, data=chunk, headers={
                        "Content-Type":  "application/octet-stream",
                        "Content-Range": f"bytes {uploaded}-{end}/{total if is_last else '*'}",
                    }, timeout=300)
                    if resp.status_code in (500, 502, 503, 504) and attempt < 4:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if resp.status_code not in (200, 201, 308):
                        raise RuntimeError(f"块上传失败 {resp.status_code}: {resp.text[:200]}")
                    break
                except requests.RequestException as e:
                    if attempt < 4:
                        time.sleep(5)
                    else:
                        raise RuntimeError(f"块上传重试失败: {e}")

            uploaded += len(chunk)
            elapsed = time.time() - t0
            speed   = uploaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
            print(f"  ⬆️  {uploaded/1024/1024:.0f}/{total/1024/1024:.0f}MB  {speed:.1f}MB/s", flush=True)

            if resp.status_code in (200, 201):
                elapsed = time.time() - t0
                fid = resp.json().get("id", existing_id or "")
                print(f"✅ DB 上传完成  {total/1024/1024:.1f}MB  "
                      f"耗时={elapsed:.0f}s  速度={total/elapsed/1024/1024:.1f}MB/s  ID={fid}")
                return

    raise RuntimeError(f"上传未完成 {uploaded}/{total}")


def do_download(token, folder_id, dest_path):
    file_id = _find_file(token, folder_id)
    if not file_id:
        print(f"⚠️  GDrive 中未找到 {DB_FILENAME}，跳过下载")
        sys.exit(1)

    print(f"⬇️  从 GDrive 下载 DB → {dest_path}")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

    r = requests.get(f"{GDRIVE_API}/{file_id}?alt=media",
                     headers=_auth(token), stream=True, timeout=600)
    r.raise_for_status()

    total      = int(r.headers.get("Content-Length", 0))
    downloaded = 0
    t0         = time.time()

    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                elapsed = time.time() - t0
                speed   = downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                tot_s   = f"/{total/1024/1024:.0f}" if total else ""
                print(f"  ⬇️  {downloaded/1024/1024:.0f}{tot_s}MB  {speed:.1f}MB/s", flush=True)

    elapsed = time.time() - t0
    print(f"✅ DB 下载完成  {downloaded/1024/1024:.1f}MB  耗时={elapsed:.0f}s")


def main():
    p = argparse.ArgumentParser(description="GDrive DB 同步工具")
    p.add_argument("--action", choices=["upload", "download"], required=True)
    p.add_argument("--db_path",          default="output/global_tasks.db")
    p.add_argument("--g_client_id",      default=os.environ.get("G_CLIENT_ID"))
    p.add_argument("--g_client_secret",  default=os.environ.get("G_CLIENT_SECRET"))
    p.add_argument("--g_refresh_token",  default=os.environ.get("G_REFRESH_TOKEN"))
    p.add_argument("--gdrive_db_folder", default=os.environ.get("GDRIVE_ROOT_FOLDER"),
                   help="GDrive 文件夹 ID（DB 存放位置，默认与视频根目录相同）")
    args = p.parse_args()

    if not all([args.g_client_id, args.g_client_secret,
                args.g_refresh_token, args.gdrive_db_folder]):
        print("❌ 缺少 Google 认证参数（G_CLIENT_ID / G_CLIENT_SECRET / "
              "G_REFRESH_TOKEN / GDRIVE_ROOT_FOLDER）")
        sys.exit(1)

    print("🔑 获取 GDrive token...")
    token = _get_token(args.g_client_id, args.g_client_secret, args.g_refresh_token)

    if args.action == "upload":
        if not os.path.exists(args.db_path):
            print(f"❌ DB 文件不存在: {args.db_path}")
            sys.exit(1)
        do_upload(token, args.db_path, args.gdrive_db_folder)
    else:
        do_download(token, args.gdrive_db_folder, args.db_path)


if __name__ == "__main__":
    main()
