import os
import time
import io
import base64
from PIL import ImageDraw, ImageGrab


KEYBOARD_KEYS = ['\t', '\n', '\r', ' ', '!', '"', '#', '$', '%', '&', "'", '(', ')', '*', '+', ',', '-', '.', '/', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', ':', ';', '<', '=', '>', '?', '@', '[', '\\', ']', '^', '_', '`', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', '{', '|', '}', '~', 'accept', 'add', 'alt', 'altleft', 'altright', 'apps', 'backspace', 'browserback', 'browserfavorites', 'browserforward', 'browserhome', 'browserrefresh', 'browsersearch', 'browserstop', 'capslock', 'clear', 'convert', 'ctrl', 'ctrlleft', 'ctrlright', 'decimal', 'del', 'delete', 'divide', 'down', 'end', 'enter', 'esc', 'escape', 'execute', 'f1', 'f10', 'f11', 'f12', 'f13', 'f14', 'f15', 'f16', 'f17', 'f18', 'f19', 'f2', 'f20', 'f21', 'f22', 'f23', 'f24', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'final', 'fn', 'hanguel', 'hangul', 'hanja', 'help', 'home', 'insert', 'junja', 'kana', 'kanji', 'launchapp1', 'launchapp2', 'launchmail', 'launchmediaselect', 'left', 'modechange', 'multiply', 'nexttrack', 'nonconvert', 'num0', 'num1', 'num2', 'num3', 'num4', 'num5', 'num6', 'num7', 'num8', 'num9', 'numlock', 'pagedown', 'pageup', 'pause', 'pgdn', 'pgup', 'playpause', 'prevtrack', 'print', 'printscreen', 'prntscrn', 'prtsc', 'prtscr', 'return', 'right', 'scrolllock', 'select', 'separator', 'shift', 'shiftleft', 'shiftright', 'sleep', 'space', 'stop', 'subtract', 'tab', 'up', 'volumedown', 'volumemute', 'volumeup', 'win', 'winleft', 'winright', 'yen', 'command', 'option', 'optionleft', 'optionright']


def get_screenshot():
    screenshot = ImageGrab.grab()
    return screenshot


def encode_image(image):
    # encode image to base64 string
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')


def save_screenshot(screenshot, path):
    screenshot.save(path, format="PNG")


def get_mllm_messages(instruction, base64_image):
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_image}"
                    },
                },
            ],
        },
    ]
    return messages


# def get_element_info_from_position(x, y):
#     # get the UI element info at the specified coordinates
#     try:
#         element = desktop.from_point(x, y)
#         # get the rectangle coordinates of the element
#         rect = element.rectangle()

#         return {
#             "name": element.element_info.name,
#             "coordinates": {
#                 "left": rect.left,
#                 "top": rect.top,
#                 "right": rect.right,
#                 "bottom": rect.bottom
#             }
#         }
#     except Exception as e:
#         print(f"Error occurs when get element from position: {e}")
#         return None


def mark_screenshot(original_screenshot, coordinates, rect=None):
    screenshot = original_screenshot.copy()
    x, y = coordinates
    point = {"x": x, "y": y}

    if rect is not None:
        # create a drawable object
        draw = ImageDraw.Draw(screenshot)
        # draw the rectangle
        draw.rectangle(
            [(rect["left"], rect["top"]), (rect["right"], rect["bottom"])],
            outline="red",
            width=3  # line width
        )

    if point is not None:
        draw = ImageDraw.Draw(screenshot)

        # calculate the top-left and bottom-right coordinates of the solid circle
        radius = 3
        left = point["x"] - radius
        top = point["y"] - radius
        right = point["x"] + radius
        bottom = point["y"] + radius

        # draw the solid circle
        draw.ellipse(
            [(left, top), (right, bottom)],
            fill="red"
        )

        # add a larger hollow circle
        circle_radius = 18
        circle_left = point["x"] - circle_radius
        circle_top = point["y"] - circle_radius
        circle_right = point["x"] + circle_radius
        circle_bottom = point["y"] + circle_radius

        # draw the hollow circle
        draw.ellipse(
            [(circle_left, circle_top), (circle_right, circle_bottom)],
            outline="red",
            width=2
        )

    return screenshot


def record_in_md(directory_path, task_description, screenshot_path, output, external_reflection=None,
                 first_event=False):
    file_name = "inference_record.md"
    with open(os.path.join(directory_path, file_name), "a", encoding="utf-8") as file:
        if first_event:
            file.write(f"# Inference Task\n")
            file.write(f"**Description:** {task_description}\n\n")
        file.write(f"### {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        file.write(f"**Screenshot:**\n")
        file.write(f'<img src="{screenshot_path}" width="100%" height="100%">\n\n')
        file.write(f"**External Reflection:**\n{external_reflection}\n\n") if external_reflection else None
        file.write(f"**Output:**\n{output}\n\n")


def log(message, filename="agent.log"):
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
    # open the file with UTF-8 encoding
    with open(filename, 'a', encoding='utf-8') as file:
        file.write(f"{current_time}\n{message}\n\n")


def print_in_green(message):
    print(f"\033[92m{message}\033[0m")


def resize_image_openai(image):
    """
    Resize the image to OpenAI's input resolution so that text written on it doesn't get processed any further.
    
    Steps:
    1. If the image's largest side is greater than 2048, scale it down so that the largest side is 2048, maintaining aspect ratio.
    2. If the shortest side of the image is longer than 768px, scale it so that the shortest side is 768px.
    3. Return the resized image.
    
    Reference: https://platform.openai.com/docs/guides/vision/calculating-costs
    """
    max_size = 2048
    target_short_side = 768
    
    out_w, out_h = image.size

    # Step 0: return the image without scaling if it's already within the target resolution
    if out_w <= max_size and out_h <= max_size and min(out_w, out_h) <= target_short_side:
        return image, out_w, out_h, 1.0
    
    # Initialize scale_factor
    scale_factor = 1.0
    
    # Step 1: Calculate new size to fit within a 2048 x 2048 square
    max_dim = max(out_w, out_h)
    if max_dim > max_size:
        scale_factor = max_size / max_dim
        out_w = int(out_w * scale_factor)
        out_h = int(out_h * scale_factor)
    
    # Step 2: Calculate new size if the shortest side is longer than 768px
    min_dim = min(out_w, out_h)
    if min_dim > target_short_side:
        new_scale_factor  = target_short_side / min_dim
        out_w = int(out_w * new_scale_factor)
        out_h = int(out_h * new_scale_factor)
        # Combine scale factors from both steps
        scale_factor *= new_scale_factor
    
    # Perform the resize operation once
    resized_image = image.resize((out_w, out_h))
    
    return resized_image, out_w, out_h, scale_factor

