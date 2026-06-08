from PIL import Image
import os

def convert_to_ico(png_path, ico_path):
    if not os.path.exists(png_path):
        print(f"Error: {png_path} not found")
        return
    img = Image.open(png_path)
    # Icon sizes for Windows
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico_path, sizes=icon_sizes)
    print(f"Converted {png_path} to {ico_path}")

if __name__ == "__main__":
    convert_to_ico("server.png", "server.ico")
    convert_to_ico("client.png", "client.ico")
    convert_to_ico("launcher.png", "launcher.ico")
