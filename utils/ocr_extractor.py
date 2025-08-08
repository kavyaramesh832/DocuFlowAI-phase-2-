import os
import logging
from typing import Optional
from contextlib import contextmanager
import re
from PIL import Image
from pdf2image import convert_from_path
import docx2txt
import concurrent.futures
import pytesseract
import pdfplumber

# Set up Tesseract path
pytesseract.pytesseract.tesseract_cmd = r"C:\Users\kavya\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"

# Logging configuration
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class TimeoutError(Exception):
    """Custom timeout exception"""
    pass

@contextmanager
def timeout(seconds, task_name="task"):
    """Timeout wrapper for any function"""
    with concurrent.futures.ThreadPoolExecutor() as executor:
        def run(func, *args, **kwargs):
            try:
                return executor.submit(func, *args, **kwargs).result(timeout=seconds)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(f"{task_name} timed out after {seconds} seconds")
        yield run

def extract_text(file_path: str) -> str:
    """Main function to extract text from various file formats."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.pdf':
            text = extract_from_pdf(file_path)
        elif ext in ['.jpg', '.jpeg', '.png']:
            text = extract_from_image(file_path)
        elif ext == '.docx':
            text = extract_from_docx(file_path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        logger.debug(f"[OCR] Extracted Text from {ext.upper()} (preview): {text[:500]}")

        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/debug_extracted_text.txt", "w", encoding="utf-8") as f:
                f.write(text)
            logger.debug("[OCR] Full extracted text written to logs/debug_extracted_text.txt")
        except Exception as log_error:
            logger.warning(f"[WARN] Could not save extracted text to file: {str(log_error)}")

        return text

    except Exception as e:
        logger.exception(f"[ERROR] Text extraction failed for {file_path}")
        raise Exception(f"Text extraction failed: {str(e)}")

def extract_from_pdf(file_path: str) -> str:
    """Extract text from PDF, using embedded text first, then OCR if needed."""
    try:
        # First try embedded text extraction
        try:
            with pdfplumber.open(file_path) as pdf:
                embedded_text = "\n".join([page.extract_text() or "" for page in pdf.pages[:3]])
            if embedded_text.strip():
                logger.debug("[PDF] Extracted embedded text successfully (no OCR needed)")
                return embedded_text
        except Exception as e:
            logger.debug(f"[PDF] Embedded text extraction failed: {e}")

        # Convert first 3 pages to images
        with timeout(60, "PDF conversion") as run:
            images = run(convert_from_path, file_path, first_page=1, last_page=3, dpi=150, fmt='jpeg')

        # Process pages in parallel
        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(lambda p: process_pdf_page(p[0], p[1]), enumerate(images, start=1)))

        extracted_text = "\n".join(results)

        if not extracted_text.strip() or "[OCR timeout - page skipped]" in extracted_text:
            logger.info("Attempting fallback OCR method...")
            return extract_from_pdf_fallback(file_path)

        logger.debug(f"[OCR] Extracted PDF Text (preview): {extracted_text[:500]}")
        return extracted_text.strip()

    except TimeoutError as e:
        raise Exception(f"PDF processing timed out: {str(e)}")
    except Exception as e:
        raise Exception(f"Failed to extract text from PDF: {str(e)}")

def process_pdf_page(page_number: int, image: Image.Image) -> str:
    """OCR for a single PDF page."""
    logger.debug(f"Processing page {page_number} of PDF")
    image = preprocess_image(image)
    try:
        with timeout(30, f"OCR page {page_number}") as run:
            text = run(pytesseract.image_to_string, image, lang='eng',
                       config='--psm 1 --oem 1 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.,!?@#$%^&*()_+-=[]{}|;:,.<>/ ')
            if not text.strip():
                text = run(pytesseract.image_to_string, image, lang='eng', config='--psm 6 --oem 1')
        return f"--- Page {page_number} ---\n{text.strip()}"
    except TimeoutError:
        logger.warning(f"OCR timeout on page {page_number}, skipping")
        return f"--- Page {page_number} ---\n[OCR timeout - page skipped]"

def extract_from_pdf_fallback(file_path: str) -> str:
    """Fallback method for PDF OCR with different PSM modes."""
    try:
        with timeout(90, "PDF fallback conversion") as run:
            images = run(convert_from_path, file_path, first_page=1, last_page=1, dpi=100, fmt='jpeg')
        
        if not images:
            raise Exception("Could not convert PDF to image")

        image = preprocess_image(images[0])
        configs = ['--psm 4 --oem 1', '--psm 6 --oem 1', '--psm 12 --oem 1', '--psm 8 --oem 1']

        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(lambda cfg: run_ocr_config(image, cfg), configs))

        best_text = max(results, key=lambda t: len(t.strip()))
        return f"--- Page 1 (Fallback Method) ---\n{best_text.strip()}" if best_text.strip() else "--- Page 1 ---\n[Unable to extract text]"

    except Exception as e:
        logger.error(f"Fallback PDF extraction failed: {str(e)}")
        return "--- Page 1 ---\n[Text extraction failed - please try a different document format]"

def extract_from_image(file_path: str) -> str:
    """Extract text from image using multiple OCR configurations in parallel."""
    try:
        with timeout(30, "Image open") as run:
            image = run(Image.open, file_path)

        image = preprocess_image(image)
        configs = ['--psm 1 --oem 1', '--psm 3 --oem 1', '--psm 6 --oem 1', '--psm 4 --oem 1']

        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(lambda cfg: run_ocr_config(image, cfg), configs))

        best_text = max(results, key=lambda t: len(t.strip()))
        if not best_text.strip():
            raise Exception("No text found in image")

        logger.debug(f"[OCR] Extracted Image Text (preview): {best_text[:300]}")
        return best_text.strip()

    except TimeoutError as e:
        raise Exception(f"Image processing timed out: {str(e)}")
    except Exception as e:
        raise Exception(f"Failed to extract text from image: {str(e)}")

def run_ocr_config(image: Image.Image, config: str) -> str:
    """Run OCR with a given config (with timeout)."""
    try:
        with timeout(10, f"OCR config {config}") as run:
            return run(pytesseract.image_to_string, image, lang='eng', config=config)
    except TimeoutError:
        return ""

def extract_from_docx(file_path: str) -> str:
    """Extract text from DOCX files."""
    try:
        text = docx2txt.process(file_path)
        if not text.strip():
            raise Exception("No text found in DOCX file")
        
        logger.debug(f"[OCR] Extracted DOCX Text (preview): {text[:500]}")
        return text.strip()
    except Exception as e:
        raise Exception(f"Failed to extract text from DOCX: {str(e)}")

def clean_text(text: str) -> str:
    """Clean and normalize extracted text."""
    if not text:
        return ""
    
    cleaned = text.lower()
    cleaned = re.sub(r'[^a-z0-9\s]', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = cleaned.strip()
    
    logger.debug(f"[CLEAN] Cleaned Text (preview): {cleaned[:300]}")
    logger.debug(f"[CLEAN] Original vs Cleaned (side-by-side):\nORIGINAL: {text[:200]}\nCLEANED : {cleaned[:200]}")
    return cleaned

def preprocess_image(image: Image.Image) -> Image.Image:
    """Resize, convert to grayscale, and optionally binarize image for better OCR."""
    width, height = image.size
    if width > 2000 or height > 2000:
        ratio = min(2000 / width, 2000 / height)
        image = image.resize((int(width * ratio), int(height * ratio)), Image.LANCZOS)
    
    image = image.convert("L")  # Grayscale
    image = image.point(lambda x: 0 if x < 200 else 255, '1')  # Binarization
    return image
