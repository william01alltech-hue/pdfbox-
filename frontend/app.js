// 設定後端 API 基本網址
const API_BASE_URL = "http://127.0.0.1:8000";

// 全域狀態
let mergeFiles = [];
let convertFile = null;
let deferredPrompt = null;

// PWA 註冊 Service Worker
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("./sw.js")
      .then((reg) => console.log("Service Worker 註冊成功:", reg.scope))
      .catch((err) => console.error("Service Worker 註冊失敗:", err));
  });
}

// 監聽安裝 PWA 事件
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  const btnInstall = document.getElementById("btn-install");
  if (btnInstall) {
    btnInstall.classList.remove("hidden");
  }
});

document.getElementById("btn-install")?.addEventListener("click", async () => {
  if (deferredPrompt) {
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    console.log(`使用者安裝選擇: ${outcome}`);
    deferredPrompt = null;
    document.getElementById("btn-install").classList.add("hidden");
  }
});

// 切換 Tab 功能
function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.remove("active");
  });
  document.querySelectorAll(".workspace").forEach((space) => {
    space.classList.remove("active");
  });

  if (tabName === "merge") {
    document.getElementById("tab-merge").classList.add("active");
    document.getElementById("work-merge").classList.add("active");
  } else if (tabName === "convert") {
    document.getElementById("tab-convert").classList.add("active");
    document.getElementById("work-convert").classList.add("active");
  }
}

// 初始化拖曳上傳與點擊事件
setupDragAndDrop(
  "drop-zone-merge",
  "file-input-merge",
  handleMergeFilesSelected
);
setupDragAndDrop(
  "drop-zone-convert",
  "file-input-convert",
  handleConvertFileSelected
);

function setupDragAndDrop(zoneId, inputId, onFilesSelected) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);

  if (!zone || !input) return;

  // 點擊區域觸發選擇檔案
  zone.addEventListener("click", () => input.click());

  // 拖曳進入
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });

  // 拖曳離開
  zone.addEventListener("dragleave", () => {
    zone.classList.remove("dragover");
  });

  // 放開檔案
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files.length > 0) {
      onFilesSelected(e.dataTransfer.files);
    }
  });

  // 檔案選取改變
  input.addEventListener("change", (e) => {
    if (e.target.files.length > 0) {
      onFilesSelected(e.target.files);
    }
  });
}

// ----------------- PDF 合併處理邏輯 (純前端) -----------------
function handleMergeFilesSelected(files) {
  for (let file of files) {
    if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
      mergeFiles.push(file);
    }
  }
  renderMergeFileList();
}

function renderMergeFileList() {
  const listEl = document.getElementById("file-list-merge");
  const actionBar = document.getElementById("action-bar-merge");
  if (!listEl) return;

  listEl.innerHTML = "";

  if (mergeFiles.length === 0) {
    actionBar.classList.add("hidden");
    return;
  }

  mergeFiles.forEach((file, index) => {
    const sizeMB = (file.size / (1024 * 1024)).toFixed(2);
    const item = document.createElement("div");
    item.className = "file-item";
    item.innerHTML = `
      <div class="file-info">
        <span class="file-icon">📄</span>
        <div class="file-details">
          <div class="file-name" title="${file.name}">${file.name}</div>
          <div class="file-size">${sizeMB} MB</div>
        </div>
      </div>
      <div class="file-actions">
        <button class="btn-icon" onclick="removeMergeFile(${index})">🗑️</button>
      </div>
    `;
    listEl.appendChild(item);
  });

  if (mergeFiles.length >= 2) {
    actionBar.classList.remove("hidden");
  } else {
    actionBar.classList.add("hidden");
  }
}

function removeMergeFile(index) {
  mergeFiles.splice(index, 1);
  renderMergeFileList();
}

document.getElementById("btn-merge-run")?.addEventListener("click", () => {
  if (mergeFiles.length < 2) return;

  showStatus("正在準備合併...", "請稍候，我們正在後台執行處理");

  // 使用 Web Worker 進行合併以防止 UI 卡死
  const worker = new Worker("./worker.js");
  const fileBuffers = [];
  let loadedCount = 0;

  // 讀取所有檔案為 ArrayBuffer 並發送給 Worker
  mergeFiles.forEach((file, index) => {
    const reader = new FileReader();
    reader.onload = function (e) {
      fileBuffers[index] = e.target.result;
      loadedCount++;
      if (loadedCount === mergeFiles.length) {
        showStatus("合併中...", "正在載入頁面並重新打包");
        worker.postMessage({ type: "MERGE_PDFS", files: fileBuffers });
      }
    };
    reader.onerror = () => {
      hideStatus();
      alert("讀取檔案失敗：" + file.name);
    };
    reader.readAsArrayBuffer(file);
  });

  worker.onmessage = (event) => {
    const { type, progress, pdfBytes, message } = event.data;

    if (type === "PROGRESS") {
      updateStatusProgress(progress, `合併進度: ${progress}%`);
    } else if (type === "SUCCESS") {
      hideStatus();
      downloadBlob(new Blob([pdfBytes], { type: "application/pdf" }), "merged_document.pdf");
      worker.terminate();
    } else if (type === "ERROR") {
      hideStatus();
      alert("合併失敗: " + message);
      worker.terminate();
    }
  };
});

// ----------------- PDF 轉 Word 處理邏輯 (FastAPI 後端) -----------------
function handleConvertFileSelected(files) {
  const file = files[0];
  if (file && (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"))) {
    convertFile = file;
    renderConvertFileInfo();
  } else {
    alert("請選擇 PDF 格式的檔案進行轉換！");
  }
}

function renderConvertFileInfo() {
  const infoEl = document.getElementById("file-info-convert");
  const actionBar = document.getElementById("action-bar-convert");
  if (!infoEl) return;

  infoEl.innerHTML = "";

  if (!convertFile) {
    actionBar.classList.add("hidden");
    return;
  }

  const sizeMB = (convertFile.size / (1024 * 1024)).toFixed(2);
  const item = document.createElement("div");
  item.className = "file-item";
  item.innerHTML = `
    <div class="file-info">
      <span class="file-icon">📄</span>
      <div class="file-details">
        <div class="file-name" title="${convertFile.name}">${convertFile.name}</div>
        <div class="file-size">${sizeMB} MB</div>
      </div>
    </div>
    <div class="file-actions">
      <button class="btn-icon" onclick="clearConvertFile()">🗑️</button>
    </div>
  `;
  infoEl.appendChild(item);
  actionBar.classList.remove("hidden");
}

function clearConvertFile() {
  convertFile = null;
  renderConvertFileInfo();
}

document.getElementById("btn-convert-run")?.addEventListener("click", async () => {
  if (!convertFile) return;

  showStatus("正在上傳檔案...", "準備傳送至後端轉檔引擎，大檔案可能需要較長時間");
  updateStatusProgress(10);

  try {
    // 1. 上傳檔案
    const formData = new FormData();
    formData.append("file", convertFile);

    const uploadRes = await fetch(`${API_BASE_URL}/api/v1/upload`, {
      method: "POST",
      body: formData,
    });

    if (!uploadRes.ok) {
      const errData = await uploadRes.json();
      throw new Error(errData.detail || "檔案上傳失敗");
    }

    const uploadData = await uploadRes.json();
    const fileId = uploadData.file_id;

    updateStatusProgress(50, "上傳成功！正在進行 Word 排版解析...");

    // 2. 開始轉換
    const convertRes = await fetch(`${API_BASE_URL}/api/v1/convert/pdf-to-word`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ file_id: fileId }),
    });

    if (!convertRes.ok) {
      const errData = await convertRes.json();
      throw new Error(errData.detail || "PDF 轉 Word 失敗");
    }

    const convertData = await convertRes.json();
    updateStatusProgress(90, "轉換成功！正在準備下載...");

    // 3. 下載檔案
    const downloadUrl = `${API_BASE_URL}${convertData.download_url}`;
    
    // 取得原始檔名並修改副檔名為 .docx
    const originalName = convertFile.name;
    const outputName = originalName.substring(0, originalName.lastIndexOf(".")) + ".docx";
    
    // 開始下載
    const fileRes = await fetch(downloadUrl);
    const blob = await fileRes.blob();
    
    hideStatus();
    downloadBlob(blob, outputName);
  } catch (error) {
    hideStatus();
    console.error("轉換流程出錯:", error);
    alert(`轉換失敗：${error.message}`);
  }
});

// ----------------- 通用輔助函式 -----------------
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function showStatus(title, description) {
  const panel = document.getElementById("status-panel");
  const titleEl = document.getElementById("status-title");
  const descEl = document.getElementById("status-desc");
  const bar = document.getElementById("progress-bar");

  if (panel && titleEl && descEl && bar) {
    titleEl.textContent = title;
    descEl.textContent = description;
    bar.style.width = "0%";
    panel.classList.remove("hidden");
  }
}

function updateStatusProgress(percent, description = null) {
  const bar = document.getElementById("progress-bar");
  const descEl = document.getElementById("status-desc");

  if (bar) {
    bar.style.width = `${percent}%`;
  }
  if (description && descEl) {
    descEl.textContent = description;
  }
}

function hideStatus() {
  const panel = document.getElementById("status-panel");
  if (panel) {
    panel.classList.add("hidden");
  }
}
