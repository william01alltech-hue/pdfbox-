#!/bin/bash

# Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== PDFBox PWA GitHub 一鍵推送工具 ===${NC}"

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo -e "${RED}錯誤：本機未安裝 git。請先安裝 Git 後再執行此指令碼。${NC}"
    exit 1
fi

# Initialize git if not already
if [ ! -d ".git" ]; then
    echo -e "${YELLOW}偵測到專案尚未初始化 Git，正在初始化...${NC}"
    git init
    git branch -M main
fi

# Add remote if provided
CURRENT_REMOTE=$(git remote -v | grep origin | head -n 1)

if [ -z "$CURRENT_REMOTE" ]; then
    echo -e "${YELLOW}目前未綁定 GitHub 遠端倉庫。${NC}"
    read -p "請輸入您的 GitHub 倉庫 URL (例如 https://github.com/username/repo.git): " REPO_URL
    if [ ! -z "$REPO_URL" ]; then
        git remote add origin "$REPO_URL"
        echo -e "${GREEN}✓ 已成功綁定遠端倉庫：$REPO_URL${NC}"
    else
        echo -e "${RED}未輸入網址，跳過綁定步驟。您可以在稍後手動使用 'git remote add origin <URL>' 綁定。${NC}"
    fi
else
    echo -e "${GREEN}✓ 已偵測到已綁定的遠端倉庫：${NC}"
    git remote -v
fi

# Stage files
echo -e "${BLUE}正在追蹤專案檔案...${NC}"
git add .

# Prompt for commit message
read -p "請輸入 Commit 訊息 [預設: 'feat: init PDFBox PWA release']: " COMMIT_MSG
if [ -z "$COMMIT_MSG" ]; then
    COMMIT_MSG="feat: init PDFBox PWA release"
fi

# Commit
git commit -m "$COMMIT_MSG"
echo -e "${GREEN}✓ 本地提交成功！${NC}"

# Push to GitHub
echo -e "${BLUE}正在推送程式碼至 GitHub (main 分支)...${NC}"
if git push -u origin main; then
    echo -e "${GREEN}🎉 程式碼已成功推送至 GitHub！您隨時可以開始進行 Render 或 Cloudflare Pages 部署！${NC}"
else
    echo -e "${RED}❌ 推送失敗。請確認：${NC}"
    echo -e "1. 您的 GitHub 倉庫已建立（請勿勾選自動產生 README/gitignore，以防衝突）。"
    echo -e "2. 您的本機 Git 已取得 GitHub 的存取權限。"
    echo -e "3. 如果您在建立倉庫時勾選了自動產生檔案，請先執行 'git pull origin main --rebase' 再重新推送。"
fi
