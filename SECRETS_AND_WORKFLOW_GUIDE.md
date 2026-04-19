# 🔒 Action9 项目：安全架构、开发工作流与 Token 获取指南

本文档是针对你当前高安全性架构（公开库跑加密代码 + 私密库放核心数据）的完整备忘录。**请务必仔细阅读并妥善保存此文档（不要推送到公开仓库）。**

---

## 🏗️ 核心架构与“双库分离”原则（重点与难点）

为了实现你“代码和核心逻辑不被他人冒用和抄袭”的需求，我们对项目进行了彻底的安全改造。

### 1. 架构解析
*   **私密数据库 (`DATA_REPO`)**: 存储你的 SQLite 数据库 (`global_tasks.db`) 和抖音 Cookie。由于库是私有的，外人无法访问你的核心业务数据和账号凭证。
*   **公开代码库 (Public Repo)**: 用于运行 GitHub Actions。这里面**只存放经过 PyArmor 加密混淆后的代码（乱码）**。任何人都无法看懂你的爬虫逻辑。
*   **GitHub Secrets**: 所有的密码、Token、网盘授权，全部通过 GitHub Secrets 以环境变量的形式在运行时注入，代码中不再包含任何明文凭证。

### 2. 后期开发与修改代码的正确流程（绝对不能错！）

**🚨 致命警告：绝对不要弄丢你本地的明文源代码！一旦本地源码丢失，云端的加密代码任何人（包括 AI）都无法还原修改！建议将本地源码备份到 U盘 或 另一个完全私有的 Git 仓库中。**

如果你后期需要让 Claude Code 或 Gemini 增加新需求、修改 Bug，**必须**按照以下三步走：

1.  **本地修改与测试 (AI 介入)**：
    在本地 `E:\action9_gdrive\` 目录下，让 AI 阅读和修改原始的 `.py` 明文文件（如 `action9_loop.py`, `core/extractor_v2.py`）。
2.  **一键加密打包**：
    代码修改测试无误后，在本地终端运行加密脚本：
    ```powershell
    .\build_protected.ps1
    ```
    这会将你的最新明文代码编译成机器乱码，并输出到 `dist` 文件夹中。
3.  **推送加密代码到公开库**：
    进入 `dist` 文件夹，将更新后的加密代码推送到你的公开 GitHub 仓库触发运行：
    ```powershell
    cd dist
    git add .
    git commit -m "feat: 更新业务逻辑"
    git push origin main
    ```

---

## 🔑 7 个核心 Secret 的获取指南 (保姆级教程)

为了让 GitHub Actions 能够成功运行，你需要去你的**公开仓库** -> **Settings** -> **Secrets and variables** -> **Actions** 中添加以下 7 个 Repository secrets。

### 1. Google Drive 相关凭证 (3个)

#### 1.1 `G_CLIENT_ID` 和 `1.2 G_CLIENT_SECRET`
*   **获取步骤**：
    1. 登录 [Google Cloud Console (谷歌云控制台)](https://console.cloud.google.com/)。
    2. 确保左上角选中了你之前创建的那个项目。
    3. 左侧菜单 -> **API 和服务 (APIs & Services)** -> **凭据 (Credentials)**。
    4. 在 **OAuth 2.0 客户端 ID** 列表中，点击你的客户端名称（如 Web client 或 Desktop client）。
    5. 页面右侧会显示“客户端 ID”和“客户端密钥”，将它们分别复制。

#### 1.3 `G_REFRESH_TOKEN` (重点获取步骤)
由于刷新令牌只在首次授权时显示，我们需要重新生成一个。
*   **获取步骤**：
    1. 浏览器打开 [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/)。
    2. 点击右上角的 **齿轮图标 ⚙️** (OAuth 2.0 configuration)。
    3. 勾选 **Use your own OAuth credentials**。
    4. 将刚才获取的 `Client ID` 和 `Client secret` 填入，点击 Close。
    5. 在左侧 **Step 1** 列表中，找到并展开 **Drive API v3**。
    6. 勾选第一项：`https://www.googleapis.com/auth/drive` （全权限）。
    7. 点击下方的蓝色按钮 **Authorize APIs**。
    8. 在弹出的 Google 登录框中选择你的账号，并点击“允许/继续”授权。
    9. 网页跳回后进入 **Step 2**，点击蓝色的 **Exchange authorization code for tokens** 按钮。
    10. 在右侧生成的 JSON 面板中，找到 `Refresh token:` 后面的那串长字符，复制它！

### 2. Google Drive 目录配置 (1个)

#### 2.1 `GDRIVE_ROOT_FOLDER`
*   **获取步骤**：
    1. 网页登录你的 Google Drive (谷歌云盘)。
    2. 双击进入你想要用来存放下载视频的那个根文件夹。
    3. 查看浏览器上方的地址栏 URL，例如：`https://drive.google.com/drive/folders/1fHOUUBEKVb4RMgYWqDWSmwKLSaILhwe5`
    4. URL 最后面那段乱码（如 `1fHOUUBEKVb4RMgYWqDWSmwKLSaILhwe5`）就是你要复制的 Folder ID。

### 3. GitHub 数据库与工作流凭证 (3个)

#### 3.1 `DATA_REPO`
*   **获取步骤**：这个不需要去哪里找，就是你私密数据仓库的名字，格式为 `用户名/仓库名`。
*   **填写示例**：`zhangzhang19666-byte/douyin-pikpak-data`

#### 3.2 `DATA_REPO_TOKEN`
这是用于让公开仓库有权限去你的私密仓库读写数据库 (DB) 和 Cookie 的令牌。
*   **获取步骤**：
    1. 登录 GitHub，点击右上角头像 -> **Settings**。
    2. 左侧菜单拉到最底，点击 **Developer settings**。
    3. 点击 **Personal access tokens** -> **Tokens (classic)**。
    4. 点击右上角 **Generate new token (classic)**。
    5. Note 填个名字（如：Data Repo Access）。
    6. Expiration 建议选 90 days 或 No expiration (注意安全)。
    7. **Select scopes** (勾选权限)：只需要勾选 **`repo`** (Full control of private repositories) 即可。
    8. 页面拉到底部，点击 Generate token，复制生成的以 `ghp_` 开头的字符串。

#### 3.3 `GH_PAT`
这是用于在 5小时运行时间到达上限时，自动触发下一次 GitHub Actions 运行的令牌。
*   **获取步骤**：
    1. 同样在 **Tokens (classic)** 页面，点击 **Generate new token (classic)**。
    2. Note 填名字（如：Workflow Trigger）。
    3. **Select scopes** (勾选权限)：只需要勾选 **`workflow`** (Update GitHub Action workflows) 即可。
    4. 页面拉到底部，点击 Generate token，复制生成的以 `ghp_` 开头的字符串。

---
✅ **将以上 7 个值全部配置到你的公开仓库 Secrets 后，你的自动化工作流就可以完美运行了！**
