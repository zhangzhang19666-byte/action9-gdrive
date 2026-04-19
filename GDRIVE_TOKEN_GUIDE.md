# Google Drive API — Token 申请全流程

> 本文档基于实际踩坑经验整理，重点标注了容易出错的地方。

---

## 一、创建 Google Cloud 项目

1. 访问 [console.cloud.google.com](https://console.cloud.google.com)
2. 顶部下拉 → **New Project** → 填写项目名 → Create
3. 确认左上角已切换到新项目

---

## 二、启用 Google Drive API

1. 左侧菜单 → **APIs & Services** → **Library**
2. 搜索 `Google Drive API` → 点击 → **Enable**

---

## 三、配置 OAuth 同意屏幕

1. **APIs & Services** → **OAuth consent screen**
2. User Type 选 **External** → Create
3. 填写：
   - App name（随意）
   - User support email（填自己邮箱）
   - Developer contact email（填自己邮箱）
4. **Scopes 页**：点 **Add or Remove Scopes**
   - 搜索 `drive`
   - ⚠️ **必须选** `https://www.googleapis.com/auth/drive`（完整读写权限）
   - ❌ **不能只选** `drive.readonly`，否则上传时返回 403
5. **Test users 页**：Add Users → 填入自己的 Google 账号邮箱
   - ⚠️ 不加自己为测试用户，授权时会报 "access blocked"
6. 保存完成

---

## 四、创建 OAuth 2.0 凭据

1. **APIs & Services** → **Credentials** → **+ Create Credentials** → **OAuth client ID**
2. Application type 选 **Desktop app**
3. 填写名称 → Create
4. 弹出窗口下载 JSON 文件（`client_secret_xxx.json`）
   - 里面包含 `client_id` 和 `client_secret`，**妥善保存**

---

## 五、获取 Refresh Token（本地运行授权脚本）

### 5.1 安装依赖

```bash
pip install google-auth-oauthlib
```

### 5.2 授权脚本 `get_gdrive_token.py`

```python
from google_auth_oauthlib.flow import InstalledAppFlow
import json

# ⚠️ 必须是 drive（完整权限），不能是 drive.readonly
SCOPES = ['https://www.googleapis.com/auth/drive']

flow = InstalledAppFlow.from_client_secrets_file(
    'client_secret_xxx.json',   # 替换为你下载的 JSON 文件名
    scopes=SCOPES
)
creds = flow.run_local_server(port=0)

print("=== 复制以下信息到 secrets ===")
print(f"client_id:     {creds.client_id}")
print(f"client_secret: {creds.client_secret}")
print(f"refresh_token: {creds.refresh_token}")
```

### 5.3 运行流程

```bash
python get_gdrive_token.py
```

1. 浏览器自动打开 Google 授权页
2. 选择你的 Google 账号（必须是步骤三中添加的测试用户）
3. 点击 **Allow**（可能需要点两次，第一次是警告页）
4. 浏览器显示 `The authentication flow has completed` → 回到终端
5. 终端打印出 `client_id`、`client_secret`、`refresh_token`

---

## 六、将三个值配置到 GitHub Secrets

| Secret 名 | 对应值 |
|-----------|--------|
| `G_CLIENT_ID` | `client_id` |
| `G_CLIENT_SECRET` | `client_secret` |
| `G_REFRESH_TOKEN` | `refresh_token` |
| `GDRIVE_ROOT_FOLDER` | 目标文件夹 ID（见下方说明） |

### 获取 GDrive 文件夹 ID

打开 Google Drive，进入目标文件夹，地址栏末尾那串字符即为 folder ID：
```
https://drive.google.com/drive/folders/1fHOUUBEKVb4RMgYWqDWSmwKLSaILhwe5
                                        ↑ 这一串就是 folder ID
```

---

## 七、Token 使用原理（程序内部）

```
client_id + client_secret + refresh_token
        ↓  每次运行时请求
   access_token（有效期 1 小时）
        ↓
   用于所有 GDrive API 请求
```

- `refresh_token` **永久有效**（除非手动吊销或长期不用）
- `access_token` 每次从 `refresh_token` 换取，程序自动处理
- 程序每 ~50 分钟自动刷新一次 `access_token`，无需手动干预

---

## 八、容易出错的地方（踩坑汇总）

### ❌ 错误1：Scope 选了 `drive.readonly`

**现象**：上传文件时返回 `403 Forbidden`  
**原因**：`drive.readonly` 只有读权限，无法写入/上传  
**修复**：scope 必须改为 `https://www.googleapis.com/auth/drive`  
**注意**：改了 scope 之后必须**重新运行授权脚本**，旧的 `refresh_token` 失效，要换新的

---

### ❌ 错误2：没有把自己加为 Test User

**现象**：授权时报 `Access blocked: This app's request is invalid`  
**修复**：OAuth 同意屏幕 → Test users → Add Users → 加入自己的 Gmail

---

### ❌ 错误3：直接复用旧 Refresh Token（Scope 变更后）

**现象**：上传仍然 403，明明已经改了 scope  
**原因**：旧 token 是用旧 scope 授权的，权限不会自动升级  
**修复**：重新走一遍授权流程，生成新的 `refresh_token`

---

### ❌ 错误4：Application Type 选了 Web application

**现象**：`run_local_server` 报错，或需要配置回调 URL  
**修复**：创建凭据时 Application Type 选 **Desktop app**

---

### ❌ 错误5：Folder ID 填错

**现象**：GDrive API 返回 `404 File not found`  
**检查**：确认 folder ID 从地址栏复制，且该文件夹你的账号有写权限

---

## 九、快速验证（可选）

运行以下命令，确认 token 有效且有写权限：

```python
import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
r = requests.post(TOKEN_URL, data={
    "client_id":     "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
    "grant_type":    "refresh_token",
})
access_token = r.json()["access_token"]

# 列出根目录文件（验证读权限）
r2 = requests.get(
    "https://www.googleapis.com/drive/v3/files",
    headers={"Authorization": f"Bearer {access_token}"},
    params={"pageSize": 5, "fields": "files(name,id)"}
)
print(r2.json())
```

如果返回文件列表，token 正常。
