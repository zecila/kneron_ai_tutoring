import pdfplumber
import pytesseract
from pytesseract import Output as TessOutput
import cv2
import numpy as np
from pdf2image import convert_from_path
from PIL import Image
from pptx.util import Emu
import io

##################################################
# IMAGE REGION OCR
##################################################
def _stroke_width_variance(gray: np.ndarray) -> float:
    """
    Estimate stroke-width consistency via distance transform.
    Text strokes are thin and uniform → low variance of nonzero distances.
    Shapes/diagrams have thick fills or varying widths → high variance.
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    foreground = dist[dist > 0]
    if len(foreground) < 10:
        return 0.0
    return float(np.var(foreground))

def _to_chromatic_gray(img_rgb: np.ndarray) -> np.ndarray:
    """
    Convert an RGB image to grayscale in a way that preserves colored text.
 
    Standard grayscale (0.299R + 0.587G + 0.114B) washes out colored text
    because it weights green heavily — orange, teal, and red text all end up
    nearly as bright as the white background, making Otsu binarization fail.
 
    This transform darkens pixels proportionally to their HSV saturation,
    so any chromatic pixel (orange, teal, red, blue…) becomes darker than
    the neutral white/gray background, regardless of hue or lightness.
 
    Works for both light-bg and dark-bg images (caller inverts first if needed).
    """
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    # Saturation in [0, 255]; JPEG noise lives below ~25 (≈ 0.10 normalized)
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
 
    # Ramp: ignore noise below 0.10, full effect at 0.40+
    sat_mask = np.clip((sat - 0.10) / 0.30, 0, 1)
 
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
 
    # Pull chromatic pixels toward black (max 85% darkening so black text stays black)
    darkened = gray * (1.0 - sat_mask * 0.85)
    return np.clip(darkened, 0, 255).astype(np.uint8)

def should_ocr_region(crop_img_array: np.ndarray) -> bool:
    """
    Gate OCR to regions that likely contain text or equations on a
    uniform (light or dark) background. Rejects photos, pure diagrams,
    filled shapes, and tiny icons.
 
    NOTE: uses chromatic gray internally so colored-text regions are not
    incorrectly rejected by the background-homogeneity check.
    """
    if crop_img_array.ndim == 2:
        gray = crop_img_array
        img_rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    else:
        img_rgb = crop_img_array
        gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
 
    h, w = gray.shape
 
    # --- 1. Size gate ---
    if h < 15 or w < 15:
        return False
 
    # --- 2. Edge density (must have crisp strokes) ---
    edges = cv2.Canny(gray, 50, 150)
    edge_density = np.count_nonzero(edges) / (h * w)
    if edge_density < 0.02:
        return False  # blank / featureless photo
 
    # --- 3. Background homogeneity ---
    # Use chromatic gray so colored text doesn't contaminate the background
    # std-dev estimate. In standard gray, orange-on-white looks uniform;
    # in chromatic gray the orange pixels are dark so the std rises correctly.
    chrom = _to_chromatic_gray(img_rgb)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    _, otsu_bin = cv2.threshold(chrom, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    background_mask = cv2.erode(otsu_bin, kernel, iterations=2)
    background_pixels = chrom[background_mask > 0]
 
    if len(background_pixels) < 50:
        return False
    if np.std(background_pixels) > 40:
        return False  # photo / noisy background
 
    # --- 4. Stroke-width variance (rejects solid shapes/diagrams) ---
    sw_var = _stroke_width_variance(gray)
    if sw_var > 4.5:
        return False
 
    # --- 5. Foreground coverage ---
    _, binary = cv2.threshold(chrom, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    fg_ratio = np.count_nonzero(binary) / (h * w)
    if fg_ratio < 0.01 or fg_ratio > 0.60:
        return False  # essentially blank or fully filled
 
    return True


def preprocess_for_ocr(img_array: np.ndarray) -> np.ndarray:
    """
    Normalize, upscale, and binarize a region for Tesseract.
 
    Key improvements over naive grayscale:
    - Uses chromatic gray conversion so colored text (orange, teal, etc.)
      becomes dark against the light background, not washed out.
    - Inverts dark-background images first so the same pipeline handles both.
    - Adapts the adaptive-threshold block size to image dimensions so small
      images are not over-smoothed by a fixed block=31.
    """
    if img_array.ndim == 2:
        img_rgb = cv2.cvtColor(img_array, cv2.COLOR_GRAY2RGB)
    else:
        img_rgb = img_array
 
    # --- 1. Detect background polarity from corner pixels ---
    # Use standard gray for the polarity check (chromatic gray can darken
    # corner pixels if they happen to be colored, misleading the check).
    gray_std = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    corners = [
        gray_std[0, 0], gray_std[0, -1],
        gray_std[-1, 0], gray_std[-1, -1],
    ]
    is_dark_bg = np.mean(corners) < 128
 
    # --- 2. Normalize to dark-text-on-light-background ---
    # For dark backgrounds: invert the whole image first.
    # After inversion: bg → light, white text → dark (standard gray handles it),
    # colored text on dark → inverted hue, still chromatic → darkened correctly.
    if is_dark_bg:
        img_rgb = cv2.bitwise_not(img_rgb)
 
    # --- 3. Convert to chromatic gray ---
    # Darkens any saturated (chromatic) pixel toward black so orange/teal/red
    # text has strong contrast against the now-light background.
    gray = _to_chromatic_gray(img_rgb)
    h, w = gray.shape
 
    # --- 4. Upscale to a comfortable OCR resolution ---
    # Tesseract performs best above ~150 DPI effective resolution.
    # Target: shortest side ≥ 80px, longest side ≥ 200px, always at least 2×.
    scale = max(80 / min(h, w), 200 / max(h, w), 2.0)
    if scale > 1.0:
        gray = cv2.resize(
            gray,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_LANCZOS4,  # sharper than INTER_CUBIC
        )
    h, w = gray.shape
 
    # --- 5. Contrast normalisation ---
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    gray = clahe.apply(gray)
 
    # --- 6. Gentle denoise (smooths JPEG artifacts before sharpening) ---
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
 
    # --- 7. Unsharp mask to crisp up stroke edges ---
    blurred = cv2.GaussianBlur(gray, (0, 0), 2)
    gray = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
 
    # --- 8. Adaptive threshold with size-aware block ---
    # Fixed block=31 is too coarse for small upscaled images → blobs instead of chars.
    # Block must be odd and ≥ 11; cap at ~1/6 of the shorter dimension.
    block = max(11, min(h, w) // 6)
    if block % 2 == 0:
        block += 1
    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block, 10,
    )
 
    # --- 9. Light morphological cleanup (closes tiny stroke gaps) ---
    morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, morph_kernel)
 
    # --- 10. Padding so Tesseract doesn't clip edge characters ---
    thresh = cv2.copyMakeBorder(
        thresh, 12, 12, 12, 12,
        cv2.BORDER_CONSTANT, value=255,
    )
    return thresh

def _pick_psm(img_array: np.ndarray, is_equation_hint: bool = False) -> str:
    """
    Choose Tesseract page-segmentation mode from region aspect ratio.
      Wide strip (aspect > 5) → PSM 7 (single text line)
      Otherwise            → PSM 6 (uniform block, good for equations)
    """
    if is_equation_hint:
        return "--oem 3 --psm 6 -l equ+eng" # equ is tesseract equation model
    h, w = img_array.shape[:2]
    aspect = w / max(h, 1)
    if aspect > 5:
        return "--oem 3 --psm 7 -l eng"
    return "--oem 3 --psm 6 -l eng"


def ocr_region(img_array: np.ndarray, is_equation_hint: bool = False) -> list[dict]:
    """
    Run OCR on a single cropped image region.
    Returns list of line dicts: {text, confidence, bbox}.
    """
    if not should_ocr_region(img_array):
        return []
 
    processed = preprocess_for_ocr(img_array)
    config = _pick_psm(img_array, is_equation_hint=is_equation_hint)
 
    data = pytesseract.image_to_data(
        processed,
        output_type=TessOutput.DICT,
        config=config,
    )
 
    lines: dict = {}
    n = len(data["text"])
 
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 40:
            continue
 
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = {"words": [], "top": data["top"][i], "confs": []}
 
        lines[key]["words"].append({
            "text": text,
            "x": data["left"][i],
            "y": data["top"][i],
            "w": data["width"][i],
            "h": data["height"][i],
        })
        lines[key]["confs"].append(conf)
 
    if not lines:
        return []
 
    result_lines = []
    for line in sorted(lines.values(), key=lambda l: l["top"]):
        words = sorted(line["words"], key=lambda ww: ww["x"])
        line_text = " ".join(ww["text"] for ww in words).strip()
        if not line_text:
            continue
 
        avg_conf = np.mean(line["confs"]) / 100.0
 
        result_lines.append({
            "text": line_text,
            "confidence": round(avg_conf, 3),
            "bbox": {
                "x0": min(ww["x"] for ww in words),
                "y0": min(ww["y"] for ww in words),
                "x1": max(ww["x"] + ww["w"] for ww in words),
                "y1": max(ww["y"] + ww["h"] for ww in words),
            },
        })
 
    return result_lines


##################################################
# IMAGE EXTRACTION FOR A PAGE
##################################################

def extract_image_elements(page, page_img_pil, page_index, dpi=150):
    """
    Given a pdfplumber page and its rendered PIL image,
    find embedded images, OCR them, and return structured elements.

    page_img_pil: PIL image of the full page rendered at `dpi`
    """
    elements = []
    pdf_w = page.width
    pdf_h = page.height
    img_w, img_h = page_img_pil.size

    # scale factors from PDF coordinates to rendered image pixels
    scale_x = img_w / pdf_w
    scale_y = img_h / pdf_h

    seen_regions = []  # track processed regions to avoid duplicates

    for img_meta in page.images:
        # pdfplumber image bbox is in PDF coordinates
        x0 = img_meta["x0"]
        y0 = img_meta["y0"]
        x1 = img_meta["x1"]
        y1 = img_meta["y1"]

        # skip degenerate bboxes
        if x1 - x0 < 5 or y1 - y0 < 5:
            continue

        # skip full-page background images (decorative)
        is_full_page = (x1 - x0) > pdf_w * 0.85 and (y1 - y0) > pdf_h * 0.85
        if is_full_page:
            continue

        # check for duplicate/overlapping region already processed
        duplicate = False
        for seen in seen_regions:
            overlap_x = max(0, min(x1, seen[2]) - max(x0, seen[0]))
            overlap_y = max(0, min(y1, seen[3]) - max(y0, seen[1]))
            if overlap_x * overlap_y > 0.7 * (x1 - x0) * (y1 - y0):
                duplicate = True
                break
        if duplicate:
            continue
        seen_regions.append((x0, y0, x1, y1))

        # convert PDF coords to pixel coords
        padding = 5
        px0 = max(0, int(x0 * scale_x) - padding)
        px1 = min(img_w, int(x1 * scale_x) + padding)

        # flip y: pdfplumber gives bottom-left origin y, PIL uses top-left origin
        py0 = max(0, int((pdf_h - y1) * scale_y) - padding)
        py1 = min(img_h, int((pdf_h - y0) * scale_y) + padding)

        # crop the region from the rendered page
        crop = page_img_pil.crop((px0, py0, px1, py1))
        crop_array = np.array(crop)

        # check if image is being detected and cropped correctly 
        #crop.save(f"debug_crop_p{page_index}_{len(seen_regions)}.png")

        # run OCR
        ocr_lines = ocr_region(crop_array)

        if not ocr_lines:
            continue

        # join OCR lines into content, flag as ocr_source
        combined_text = " ".join(l["text"] for l in ocr_lines).strip()
        avg_conf = np.mean([l["confidence"] for l in ocr_lines])

        if not combined_text:
            continue

        elements.append({
            "type": "image_text",
            "content": combined_text,
            "ocr_source": True,         # ← LLM should treat this cautiously
            "ocr_confidence": round(float(avg_conf), 3),
            "bbox": {
                "x0": round(x0, 2),
                "y0": round(pdf_h - y1, 2), # convert to top left origin
                "x1": round(x1, 2),
                "y1": round(pdf_h - y0, 2)  # conver to top left origin
            }
        })

    if not elements:
        # no embedded images yielded text — try OCR on the full page render
        full_array = np.array(page_img_pil)
        ocr_lines = ocr_region(full_array)
        if ocr_lines:
            combined_text = " ".join(l["text"] for l in ocr_lines).strip()
            avg_conf = np.mean([l["confidence"] for l in ocr_lines])
            if combined_text:
                elements.append({
                    "type": "image_text",
                    "content": combined_text,
                    "ocr_source": True,
                    "ocr_confidence": round(float(avg_conf), 3),
                    "bbox": {
                        "x0": 0.0,
                        "y0": 0.0,
                        "x1": round(pdf_w, 2),
                        "y1": round(pdf_h, 2)
                    }
                })

    return elements

def extract_image_elements_pptx(shape, slide_num: int, element_counter: int) -> list[dict]:
    """
    OCR a PICTURE shape from a pptx slide.
    Extracts the embedded image bytes, runs the same OCR pipeline
    as PDF image regions, and returns elements in the same JSON schema.
    """
    elements = []

    try:
        # Get image bytes from the shape
        image = shape.image
        img_bytes = image.blob
        img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception as e:
        print(f"  [OCR] Could not extract image bytes from shape: {e}")
        return elements

    img_array = np.array(img_pil)

    # Run OCR
    ocr_lines = ocr_region(img_array)
    if not ocr_lines:
        return elements

    combined_text = " ".join(l["text"] for l in ocr_lines).strip()
    avg_conf = float(np.mean([l["confidence"] for l in ocr_lines]))

    if not combined_text:
        return elements

    # Use x/y/width/height schema to match get_bbox() output used everywhere in pptx
    elements.append({
        "type": "image_text",
        "content": combined_text,
        "ocr_source": True,
        "ocr_confidence": round(avg_conf, 3),
        "bbox": {
            "x": int(shape.left),
            "y": int(shape.top),
            "width": int(shape.width),
            "height": int(shape.height),
        }
    })

    return elements