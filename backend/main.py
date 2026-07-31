import os
import shutil
import time
import uuid
import zipfile
import io
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from PIL import Image
import pdfplumber
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler  # 引入排程器
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pdf2docx import Converter  # 引入 pdf2docx 轉換工具
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

app = FastAPI(title="PDFBox API", version="1.0.0")

# 允許所有來源進行跨域請求，方便前端 PWA 調用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 檔案儲存資料夾（相對於 backend 目錄）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "temp_storage")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# --- 自動清理過期檔案的函式 ---
def cleanup_temp_files():
    now = time.time()
    # 設定檔案存活時間：2 小時 (7200秒)
    max_age = 7200

    if not os.path.exists(UPLOAD_DIR):
        return

    for filename in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, filename)
        # 確保是檔案而不是資料夾
        if os.path.isfile(file_path):
            file_age = now - os.path.getmtime(file_path)
            if file_age > max_age:
                try:
                    os.remove(file_path)
                    print(f"[自動清理] 已刪除過期暫存檔: {filename}")
                except Exception as e:
                    print(f"[清理失敗] 無法刪除 {filename}: {e}")


# 啟動背景排程器：每隔 1 小時執行一次清理
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_temp_files, "interval", hours=1)
scheduler.start()


# 應用程式關閉時順便關閉排程
@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()



class MergeRequest(BaseModel):
    file_ids: list[str]


# 定義 PDF 轉 Word 要求的資料結構
class ConvertRequest(BaseModel):
    file_id: str


class SplitRequest(BaseModel):
    file_id: str
    start_page: int | None = None
    end_page: int | None = None


class EditPagesRequest(BaseModel):
    file_id: str
    rotate_angle: int | None = None
    pages_to_delete: list[int] | None = None


class ProtectRequest(BaseModel):
    file_id: str
    password: str


class ImagesToPdfRequest(BaseModel):
    file_ids: list[str]


class AddPageNumbersRequest(BaseModel):
    file_id: str
    position: str | None = "bottom_center"


class AddWatermarkRequest(BaseModel):
    file_id: str
    watermark_text: str


class SignPdfRequest(BaseModel):
    pdf_file_id: str
    sig_file_id: str
    position: str | None = "bottom_right"


class OrganizeRequest(BaseModel):
    file_id: str
    page_order: list[int]


@app.get("/")
def read_root():
    return {"status": "success", "message": "PDFBox API is running!"}


@app.post("/api/v1/upload")
async def upload_file(file: UploadFile = File(...)):
    allowed_extensions = [".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".jpg", ".png"]
    ext = os.path.splitext(file.filename)[1].lower()

    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="不支援的檔案格式")

    file_id = str(uuid.uuid4())
    saved_filename = f"{file_id}{ext}"
    file_path = os.path.join(UPLOAD_DIR, saved_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "status": "success",
        "file_id": file_id,
        "filename": file.filename,
        "message": "檔案上傳成功",
    }


# --- 優化後的 PDF 轉 Word API ---
@app.post("/api/v1/convert/pdf-to-word")
async def pdf_to_word(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}.docx"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    cv = None
    try:
        # 嘗試進行轉換
        cv = Converter(input_path)
        cv.convert(output_path, start=0, end=None)
    except Exception as e:
        # 記錄錯誤並回傳清楚的提示（例如密碼保護或損壞的 PDF）
        print(f"[轉檔錯誤] 檔案 {input_filename} 轉換失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法轉換此 PDF，可能該檔案含有密碼保護、含有嚴重掃描加密或格式損壞。",
        )
    finally:
        if cv:
            try:
                cv.close()
            except:
                pass

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/merge")
async def merge_pdfs(data: MergeRequest):
    if not data.file_ids or len(data.file_ids) < 2:
        raise HTTPException(
            status_code=400, detail="請至少提供兩個以上的 PDF 進行合併"
        )

    merger = PdfWriter()
    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_merged.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        for file_id in data.file_ids:
            matched_files = [
                f for f in os.listdir(UPLOAD_DIR) if f.startswith(file_id)
            ]
            if not matched_files:
                raise HTTPException(
                    status_code=404, detail=f"找不到檔案代號: {file_id}"
                )
            file_path = os.path.join(UPLOAD_DIR, matched_files[0])
            merger.append(file_path)

        merger.write(output_path)
        merger.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"合併失敗: {str(e)}")

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/convert/pdf-to-images")
async def pdf_to_images(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}.zip"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        # 開啟 PDF 檔案
        doc = fitz.open(input_path)
        
        # 建立 ZIP 檔案
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(dpi=150) # 150 DPI 高畫質
                img_data = pix.tobytes("jpg") # 轉為 JPG 格式位元組
                zip_file.writestr(f"page_{page_num + 1}.jpg", img_data)
                
        doc.close()
    except Exception as e:
        print(f"[轉圖片錯誤] 檔案 {input_filename} 轉換失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法將此 PDF 轉換為圖片，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/compress")
async def compress_pdf_route(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_compressed.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        doc = fitz.open(input_path)
        for page_num in range(len(doc)):
            page = doc[page_num]
            image_list = page.get_images(full=True)
            for img_info in image_list:
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    
                    # Load image into Pillow
                    image = Image.open(io.BytesIO(image_bytes))
                    
                    # Downscale if width or height > 1024
                    max_size = 1024
                    if image.width > max_size or image.height > max_size:
                        image.thumbnail((max_size, max_size))
                    
                    output_bytes = io.BytesIO()
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGB")
                    
                    image.save(output_bytes, format="JPEG", quality=50, optimize=True)
                    new_image_bytes = output_bytes.getvalue()
                    
                    # Replace the image
                    page.replace_image(xref, stream=new_image_bytes)
                except Exception as img_err:
                    print(f"[壓縮警告] 無法壓縮某張圖片: {img_err}")
                    
        doc.save(output_path, garbage=4, deflate=True, clean=True)
        doc.close()
            
    except Exception as e:
        print(f"[壓縮錯誤] 檔案 {input_filename} 壓縮失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法壓縮此 PDF，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/split")
async def split_pdf_route(data: SplitRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    try:
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        
        output_id = str(uuid.uuid4())
        
        # 情況 1：提取特定頁數區間
        if data.start_page is not None and data.end_page is not None:
            if data.start_page < 1 or data.end_page > total_pages or data.start_page > data.end_page:
                raise HTTPException(status_code=400, detail="無效的頁碼範圍設定")
            
            output_filename = f"{output_id}_split.pdf"
            output_path = os.path.join(UPLOAD_DIR, output_filename)
            
            writer = PdfWriter()
            for idx in range(data.start_page - 1, data.end_page):
                writer.add_page(reader.pages[idx])
                
            with open(output_path, "wb") as f:
                writer.write(f)
                
        # 情況 2：無設定範圍，自動將每一頁分割為獨立 PDF 並打包為 ZIP
        else:
            output_filename = f"{output_id}_split.zip"
            output_path = os.path.join(UPLOAD_DIR, output_filename)
            
            import io
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for idx in range(total_pages):
                    writer = PdfWriter()
                    writer.add_page(reader.pages[idx])
                    
                    pdf_bytes = io.BytesIO()
                    writer.write(pdf_bytes)
                    zip_file.writestr(f"page_{idx + 1}.pdf", pdf_bytes.getvalue())
                    
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"[分割錯誤] 檔案 {input_filename} 分割失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法分割此 PDF，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/edit-pages")
async def edit_pages_route(data: EditPagesRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_edited.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        
        pages_to_delete = data.pages_to_delete if data.pages_to_delete else []
        rotate_angle = data.rotate_angle if data.rotate_angle else 0
        
        for idx in range(len(reader.pages)):
            if (idx + 1) in pages_to_delete:
                continue
            page = reader.pages[idx]
            if rotate_angle:
                page.rotate(rotate_angle)
            writer.add_page(page)
            
        with open(output_path, "wb") as f:
            writer.write(f)
            
    except Exception as e:
        print(f"[編輯頁面錯誤] 檔案 {input_filename} 編輯失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法編輯此 PDF，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/protect")
async def protect_pdf_route(data: ProtectRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_protected.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        
        for page in reader.pages:
            writer.add_page(page)
            
        writer.encrypt(data.password)
        
        with open(output_path, "wb") as f:
            writer.write(f)
            
    except Exception as e:
        print(f"[加密錯誤] 檔案 {input_filename} 加密失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法加密保護此 PDF，可能該檔案格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/unlock")
async def unlock_pdf_route(data: ProtectRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_unlocked.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        if reader.is_encrypted:
            reader.decrypt(data.password)
            
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
            
        with open(output_path, "wb") as f:
            writer.write(f)
            
    except Exception as e:
        print(f"[解密錯誤] 檔案 {input_filename} 解密失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="解密失敗，請確認密碼是否正確或 PDF 格式是否正確。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/images-to-pdf")
async def images_to_pdf_route(data: ImagesToPdfRequest):
    if not data.file_ids:
         raise HTTPException(status_code=400, detail="請上傳至少一張圖片進行轉換")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_images.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    images = []
    try:
        for file_id in data.file_ids:
            matched_files = [
                f for f in os.listdir(UPLOAD_DIR) if f.startswith(file_id)
            ]
            if not matched_files:
                raise HTTPException(status_code=404, detail=f"找不到檔案代號: {file_id}")
            
            file_path = os.path.join(UPLOAD_DIR, matched_files[0])
            img = Image.open(file_path)
            # Convert to RGB mode (PDF requires RGB)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            images.append(img)

        # Save to PDF
        images[0].save(output_path, "PDF", save_all=True, append_images=images[1:])

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"[圖片轉PDF錯誤] 轉換失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="圖片轉換為 PDF 失敗，請確認上傳的圖片格式是否正確。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/add-page-numbers")
async def add_page_numbers_route(data: AddPageNumbersRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_numbered.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        total_pages = len(reader.pages)

        # 1. 取得所有原頁面尺寸
        widths_and_heights = []
        for page in reader.pages:
            widths_and_heights.append((float(page.mediabox.width), float(page.mediabox.height)))

        # 2. 用 ReportLab 生成透明的頁碼 PDF
        packet = io.BytesIO()
        can = canvas.Canvas(packet)
        for idx in range(total_pages):
            width, height = widths_and_heights[idx]
            can.setPageSize((width, height))
            
            text = f"{idx + 1} / {total_pages}"
            can.setFont("Helvetica", 10)
            
            # 根據位置設定座標
            if data.position == "bottom_left":
                can.drawString(50, 30, text)
            elif data.position == "bottom_right":
                can.drawRightString(width - 50, 30, text)
            else: # bottom_center 預設
                can.drawCentredString(width / 2.0, 30, text)
                
            can.showPage()
        can.save()
        packet.seek(0)
        
        # 3. 讀取頁碼層並合併到原 PDF 頁面上
        numbering_reader = PdfReader(packet)
        for idx in range(total_pages):
            orig_page = reader.pages[idx]
            numbering_page = numbering_reader.pages[idx]
            orig_page.merge_page(numbering_page)
            writer.add_page(orig_page)

        with open(output_path, "wb") as f:
            writer.write(f)

    except Exception as e:
        print(f"[頁碼編排錯誤] 檔案 {input_filename} 加頁碼失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="為 PDF 新增頁碼失敗，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/add-watermark")
async def add_watermark_route(data: AddWatermarkRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_watermarked.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        total_pages = len(reader.pages)

        # 1. 取得所有頁面尺寸
        widths_and_heights = []
        for page in reader.pages:
            widths_and_heights.append((float(page.mediabox.width), float(page.mediabox.height)))

        # 2. 用 ReportLab 生成透明的浮水印 PDF
        packet = io.BytesIO()
        can = canvas.Canvas(packet)
        for idx in range(total_pages):
            width, height = widths_and_heights[idx]
            can.setPageSize((width, height))
            
            can.saveState()
            can.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.3)
            can.setFont("Helvetica-Bold", 45)
            can.translate(width / 2.0, height / 2.0)
            can.rotate(45)
            can.drawCentredString(0, 0, data.watermark_text)
            can.restoreState()
            can.showPage()
        can.save()
        packet.seek(0)

        # 3. 讀取浮水印層並合併
        watermark_reader = PdfReader(packet)
        for idx in range(total_pages):
            orig_page = reader.pages[idx]
            watermark_page = watermark_reader.pages[idx]
            orig_page.merge_page(watermark_page)
            writer.add_page(orig_page)

        with open(output_path, "wb") as f:
            writer.write(f)

    except Exception as e:
        print(f"[浮水印錯誤] 檔案 {input_filename} 加浮水印失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="為 PDF 新增浮水印失敗，可能該檔案含有密碼保護、加密或格式損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/convert/office-to-pdf")
async def convert_office_to_pdf_route(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    allowed_exts = (".docx", ".doc", ".xls", ".xlsx", ".ppt", ".pptx")
    if not input_filename.lower().endswith(allowed_exts):
        raise HTTPException(status_code=400, detail="所選檔案必須是 Office 格式 (.doc, .docx, .xls, .xlsx, .ppt, .pptx)")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}.pdf"
    
    soffice_path = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice_path:
        mac_path = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if os.path.exists(mac_path):
            soffice_path = mac_path

    if not soffice_path:
        print("⚠️ Warning: soffice not found. Creating simulated PDF.")
        output_path = os.path.join(UPLOAD_DIR, output_filename)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Simulated Office to PDF conversion (LibreOffice not installed)")
        doc.save(output_path)
        doc.close()
    else:
        try:
            cmd = [
                soffice_path,
                "--headless",
                "--convert-to", "pdf",
                "--outdir", UPLOAD_DIR,
                input_path
            ]
            import subprocess
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise Exception(result.stderr)
            
            base_name_no_ext = os.path.splitext(input_filename)[0]
            generated_pdf = os.path.join(UPLOAD_DIR, f"{base_name_no_ext}.pdf")
            final_pdf = os.path.join(UPLOAD_DIR, output_filename)
            os.rename(generated_pdf, final_pdf)
            
        except Exception as e:
            print(f"[Office轉PDF錯誤] 檔案 {input_filename} 轉換失敗: {str(e)}")
            raise HTTPException(
                status_code=400,
                detail="無法轉換此 Office 檔案，請確認檔案格式是否損壞。"
            )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/sign-pdf")
async def sign_pdf_route(data: SignPdfRequest):
    # 1. Find PDF file
    pdf_matches = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.pdf_file_id)
    ]
    if not pdf_matches:
        raise HTTPException(status_code=404, detail="找不到合約 PDF 檔案")
    pdf_path = os.path.join(UPLOAD_DIR, pdf_matches[0])

    # 2. Find Signature file
    sig_matches = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.sig_file_id)
    ]
    if not sig_matches:
        raise HTTPException(status_code=404, detail="找不到簽名圖檔")
    sig_path = os.path.join(UPLOAD_DIR, sig_matches[0])

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_signed.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        doc = fitz.open(pdf_path)
        last_page = doc[-1] # Target: Last page
        page_w = last_page.rect.width
        page_h = last_page.rect.height

        # Position mapping
        # Stamp dimensions: 150w x 80h
        sw, sh = 150, 80
        if data.position == "bottom_left":
            rect = fitz.Rect(50, page_h - 120, 50 + sw, page_h - 120 + sh)
        elif data.position == "center":
            rect = fitz.Rect((page_w - sw) / 2.0, (page_h - sh) / 2.0, (page_w - sw) / 2.0 + sw, (page_h - sh) / 2.0 + sh)
        else: # bottom_right 預設
            rect = fitz.Rect(page_w - 200, page_h - 120, page_w - 200 + sw, page_h - 120 + sh)

        # Place image signature onto PDF
        last_page.insert_image(rect, filename=sig_path)
        doc.save(output_path)
        doc.close()

    except Exception as e:
        print(f"[簽名錯誤] 檔案 {pdf_matches[0]} 簽字失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="電子簽名寫入 PDF 失敗，請確認檔案格式是否正確。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/convert/pdf-to-excel")
async def convert_pdf_to_excel_route(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}.xlsx"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        with pdfplumber.open(input_path) as pdf:
            writer = pd.ExcelWriter(output_path, engine='openpyxl')
            table_count = 0
            for i, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                for j, table in enumerate(tables):
                    if table:
                        df = pd.DataFrame(table)
                        sheet_name = f"Page{i+1}_Table{j+1}"
                        # Limit sheet name to 30 chars for Excel safety
                        sheet_name = sheet_name[:30]
                        df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)
                        table_count += 1
            
            if table_count == 0:
                # Fallback to plain text extraction
                text = ""
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
                df = pd.DataFrame([[text]])
                df.to_excel(writer, sheet_name="Text Content", index=False, header=False)
                
            writer.close()

    except Exception as e:
        print(f"[PDF轉Excel錯誤] 檔案 {input_filename} 轉換失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法將此 PDF 轉換為 Excel，可能該檔案含有加密保護或損壞。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/repair-pdf")
async def repair_pdf_route(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_repaired.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        doc = fitz.open(input_path)
        doc.save(output_path, clean=True, garbage=3, deflate=True)
        doc.close()
    except Exception as e:
        print(f"[修復錯誤] 檔案 {input_filename} 修復失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法修復此 PDF 檔案，可能其損毀程度過於嚴重。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.post("/api/v1/manage/get-thumbnails")
async def get_thumbnails_route(data: ConvertRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    try:
        doc = fitz.open(input_path)
        urls = []
        for idx, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2))
            thumb_filename = f"{data.file_id}_thumb_{idx + 1}.png"
            thumb_path = os.path.join(UPLOAD_DIR, thumb_filename)
            pix.save(thumb_path)
            urls.append(f"/api/v1/download/{thumb_filename}")
        doc.close()
    except Exception as e:
        print(f"[縮圖錯誤] 檔案 {input_filename} 產生縮圖失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="無法讀取 PDF 頁面，可能檔案已被加密或損毀。"
        )

    return {
        "status": "success",
        "thumbnails": urls,
    }


@app.post("/api/v1/manage/organize-pdf")
async def organize_pdf_route(data: OrganizeRequest):
    matched_files = [
        f for f in os.listdir(UPLOAD_DIR) if f.startswith(data.file_id)
    ]
    if not matched_files:
        raise HTTPException(status_code=404, detail="找不到該檔案代號或已過期")

    input_filename = matched_files[0]
    input_path = os.path.join(UPLOAD_DIR, input_filename)

    if not input_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="所選檔案必須是 PDF 格式")

    output_id = str(uuid.uuid4())
    output_filename = f"{output_id}_organized.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page_num in data.page_order:
            if page_num < 1 or page_num > len(reader.pages):
                raise HTTPException(status_code=400, detail="頁碼超出範圍")
            writer.add_page(reader.pages[page_num - 1])

        with open(output_path, "wb") as f:
            writer.write(f)

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"[重整錯誤] 檔案 {input_filename} 重整排序失敗: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="PDF 頁面重整失敗，請確認檔案是否已加密。"
        )

    return {
        "status": "success",
        "output_file_id": output_id,
        "download_url": f"/api/v1/download/{output_filename}",
    }


@app.get("/api/v1/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(
            path=file_path, filename=filename, media_type="application/octet-stream"
        )

    raise HTTPException(status_code=404, detail="找不到該檔案或已過期")
