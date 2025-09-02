import os
import shutil
import glob
import io
from pathlib import Path
import cv2
import numpy as np
from openai import OpenAI
import base64
from PIL import Image
import json

chrome_network_error = "/client/desktop_env/setup_refer_images/chrome_network_error.png"
desktop = "/client/desktop_env/setup_refer_images/standard_windows.png"
white = "/client/desktop_env/setup_refer_images/white.png"
chrome_white = "/client/desktop_env/setup_refer_images/chrome_white.png"
edge_white = "/client/desktop_env/setup_refer_images/edge_white.png"
vscode_standard = "/client/desktop_env/setup_refer_images/vscode_standard.png"

skip_domain_list = ["file_explorer", "settings", "microsoft_paint", "notepad", "claude"]

_api_key = os.environ.get("OPENAI_API_KEY_FOR_CHECK_SETUP")
_base_url = os.environ.get("OPENAI_BASE_URL_FOR_CHECK_SETUP")

if not _api_key or not _base_url:
    raise RuntimeError(
        "Missing OPENAI_API_KEY_FOR_CHECK_SETUP or OPENAI_BASE_URL_FOR_CHECK_SETUP environment variables"
    )

client = OpenAI(api_key=_api_key, base_url=_base_url)
model = "gpt-4o"

def encode_image(image):
    """
    Supports PIL image objects, image paths, or byte streams, encoding images to base64 strings
    """
    if isinstance(image, str):
        # Consider it as a file path
        with open(image, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    elif isinstance(image, Image.Image):
        # PIL image object
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')
    elif isinstance(image, bytes):
        # Byte stream
        return base64.b64encode(image).decode('utf-8')
    else:
        raise ValueError("encode_image: Input must be an image path string, PIL.Image object, or byte stream")


def call_client(query, screenshot_path):
    base64_image = encode_image(screenshot_path)

    messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64_image}"
                        }
                    } if base64_image else None,
                    {
                        "type": "text",
                        "text": query
                    },
                ],
            },
        ]

    retry_time = 3
    while retry_time > 0:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=1000
            )
            reply = completion.choices[0].message.content
            return reply
        except Exception as e:
            if retry_time == 1:
                raise e
            else:
                pass
        retry_time -= 1

def compare_image_similarity(img, standard):
    """Compare similarity between two images (excluding bottom portion)"""
    try:
        # Read standard image
        std_data = cv2.imread(standard)

        # Read input image
        if isinstance(img, str):
            # If it's a string, it might be base64 encoded
            if 'base64,' in img:  # Remove possible Data URL prefix
                img = img.split('base64,')[-1]
            img_bytes = base64.b64decode(img)
        elif isinstance(img, bytes):
            # If it's already a byte stream, use it directly
            img_bytes = img
        else:
            print(f"Unsupported image input type: {type(img)}")
            return 0
        
        img_array = np.frombuffer(img_bytes, dtype=np.uint8)
        img_data = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        
        # Check if images are successfully loaded
        if img_data is None or std_data is None:
            print(f"Image loading failed. img: {img[:30]}..., std: {standard}")
            return 0
        
        # Ensure images are successfully loaded
        if img_data is None or std_data is None:
            print(f"Unable to read image files. img: {img}, std: {standard}")
            return 0
        
        # Ensure both images have the same size
        if img_data.shape != std_data.shape:
            img_data = cv2.resize(img_data, (std_data.shape[1], std_data.shape[0]))

        # Crop bottom portion
        height = img_data.shape[0]
        top = int(height * 0.15)
        bottom = int(height * 0.95)
        img_cropped = img_data[top:bottom, :, :]
        std_cropped = std_data[top:bottom, :, :]

        # Convert to grayscale
        img_gray = cv2.cvtColor(img_cropped, cv2.COLOR_BGR2GRAY)
        std_gray = cv2.cvtColor(std_cropped, cv2.COLOR_BGR2GRAY)

        # Calculate mean squared error
        diff = img_gray.astype(np.float32) - std_gray.astype(np.float32) # Need to convert int8 to float32 to prevent overflow!!!
        mse  = np.mean(diff ** 2)        
        
        if mse == 0:
            similarity = 1.0
        else:
            # Calculate PSNR (Peak Signal-to-Noise Ratio)
            psnr = 10 * np.log10((255**2) / mse)
            # Convert to similarity between 0 and 1
            similarity = min(1.0, psnr / 40)  # PSNR is usually between 20-40
    
            
        return similarity
    
    except Exception as e:
        print(f"Error comparing image similarity: {e}")
        return 0
    
def check_setup_error_for_vscode(first_screenshot):
    """
    Check if the setup state of VS Code has error
    Return: True if the first screenshot is the standard welcome page of VS Code, False otherwise
    """
    if compare_image_similarity(first_screenshot, vscode_standard) > 0.999:
        return False

    prompt = """Please check if there is an error in the setup state of VSCode.
    Typical errors: all black page, \"The window is not responding\", \"Another instance of Code is running but not responding.\".
    Note: Asking whether to trust the author is not a setup error.

    Your answer: "Yes" or "No".
    """
    reply = call_client(prompt, first_screenshot)

    print(f"Reply: {reply}\n")
    
    if "Yes" in reply  or "yes" in reply:
        return True
    else:
        return False    

def check_setup_error_similarity(error_type, first_screenshot):
    """
    Check if the first screenshot is a setup error
    Return: True if the first screenshot is a setup error, False otherwise
    """
    similarity_threshold = 0.995
    if error_type == "desktop":
        similarity = compare_image_similarity(first_screenshot, desktop)
    elif error_type == "network":
        similarity = compare_image_similarity(first_screenshot, chrome_network_error)
    elif error_type == "white":
        similarity = compare_image_similarity(first_screenshot, white)
    elif error_type == "chrome_white":
        similarity = compare_image_similarity(first_screenshot, chrome_white)
    elif error_type == "three_tabs":    
        similarity = compare_image_similarity(first_screenshot, three_tabs)
    elif error_type == "edge_white":
        similarity = compare_image_similarity(first_screenshot, edge_white)

    
    if similarity >= similarity_threshold:
        return True
    else:
        return False


def check_setup_state_has_error(domain, first_screenshot):
    """
    Check if the setup state of the domain has error
    first_screenshot: bytes
    Return: True if the setup state of the domain has error, False otherwise
    """
    # These domains' set up states do not need to be checked
    if domain in skip_domain_list:
        return False

    # Check if the first screenshot is still the desktop
    if check_setup_error_similarity("desktop", first_screenshot):
        return True
    
    # Check if the first screenshot is a white screen
    if check_setup_error_similarity("white", first_screenshot):
        return True
    
    if domain == "chrome":
        # check if there is network error
        if check_setup_error_similarity("network", first_screenshot):
            return True
        # check if the first screenshot is a chrome white screen
        if check_setup_error_similarity("chrome_white", first_screenshot):
            return True

    if domain == "vs_code":
        if check_setup_error_for_vscode(first_screenshot):
            return True

    if domain == "msedge":
        # check if the first screenshot is a edge white screen
        if check_setup_error_similarity("edge_white", first_screenshot):
            return True

    return False
