// Web Worker 用於在後台執行 CPU 密集型 PDF 合併工作，避免卡死 UI 主執行緒
importScripts("https://unpkg.com/pdf-lib@1.17.1/dist/pdf-lib.min.js");

self.onmessage = async (event) => {
  const { type, files } = event.data;

  if (type === "MERGE_PDFS") {
    try {
      console.log("[Worker] 開始合併 PDF 檔案...");
      const { PDFDocument } = PDFLib;
      const mergedPdf = await PDFDocument.create();

      for (let i = 0; i < files.length; i++) {
        // files[i] 是一個 ArrayBuffer
        const pdfDoc = await PDFDocument.load(files[i]);
        const copiedPages = await mergedPdf.copyPages(
          pdfDoc,
          pdfDoc.getPageIndices()
        );
        copiedPages.forEach((page) => mergedPdf.addPage(page));

        // 回傳進度給主執行緒
        self.postMessage({
          type: "PROGRESS",
          progress: Math.round(((i + 1) / files.length) * 100)
        });
      }

      console.log("[Worker] 正在匯出合併後的 PDF...");
      const mergedPdfBytes = await mergedPdf.save();
      
      // 將成果傳回主執行緒
      self.postMessage({
        type: "SUCCESS",
        pdfBytes: mergedPdfBytes
      }, [mergedPdfBytes.buffer]); // 使用 Transferable ArrayBuffer 節省複製記憶體開銷
    } catch (error) {
      console.error("[Worker] 合併 PDF 發生錯誤:", error);
      self.postMessage({
        type: "ERROR",
        message: error.message || "合併 PDF 失敗"
      });
    }
  }
};
