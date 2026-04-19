# 这是一个将你的代码使用 PyArmor 进行混淆加密的构建脚本
# 运行此脚本后，会生成一个 dist 文件夹，里面是加密后的代码
# 你应该将 dist 文件夹中的内容推送到你的公开 GitHub 仓库，而不是原始源码

Write-Host "开始安装 PyArmor..."
pip install pyarmor

Write-Host "清理旧的构建..."
If (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

Write-Host "开始加密混淆核心代码..."
# 使用 pyarmor 混淆 action9_loop.py 和核心依赖，并指定目标平台为 Linux x86_64 (供 GitHub Actions 运行)
pyarmor gen -O dist --platform linux.x86_64 -r core/ utils/ action9_loop.py action2_single.py

Write-Host "复制其他必要文件到 dist 目录..."
Copy-Item "requirements.txt" -Destination "dist\requirements.txt"
Copy-Item "README.md" -Destination "dist\README.md"

# 创建 GitHub Actions 目录结构
New-Item -Path "dist\.github\workflows" -ItemType Directory -Force | Out-Null
Copy-Item ".github\workflows\action9.yml" -Destination "dist\.github\workflows\action9.yml"
Copy-Item ".github\workflows\action2.yml" -Destination "dist\.github\workflows\action2.yml"

Write-Host "============================================="
Write-Host "✅ 加密构建完成！"
Write-Host "现在，你的加密代码位于 'dist' 文件夹中。"
Write-Host "请将 'dist' 文件夹作为一个新的 Git 仓库（或覆盖到你现有的公开仓库中），"
Write-Host "原始的 .py 文件（当前目录下的源码）请务必留在本地，绝对不要推送到公开仓库！"
Write-Host "============================================="
