#!/bin/bash
# MilRadio — Raspberry Pi Setup & Launch Script

echo "============================================================"
echo " MilRadio Raspberry Pi Setup"
echo "============================================================"

# 1. Install System Dependencies
echo "[1/4] Installing system libraries (PortAudio, TK, build tools, Opus, etc.)..."
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv portaudio19-dev python3-tk libatlas-base-dev libopenjp2-7 build-essential python3-dev libopus-dev

# 2. Setup Virtual Environment
echo "[2/4] Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# 3. Install Python Requirements
echo "[3/4] Installing Python dependencies..."
pip install --upgrade pip
pip install -r Requirements.txt
pip install pystray pillow requests opuslib  # Extra UI/Network deps

# 4. Create Launch Scripts
echo "[4/4] Creating launch scripts..."

# Headless Server (Web UI + Backend)
cat <<EOF > launch_server.sh
#!/bin/bash
source venv/bin/activate
python3 radio_web.py "\$@"
EOF
chmod +x launch_server.sh

# Server Tray (Only if using Desktop/Monitor on Pi)
cat <<EOF > launch_tray.sh
#!/bin/bash
source venv/bin/activate
python3 radio_server_tray.py "\$@"
EOF
chmod +x launch_tray.sh

# Radio Client (Requires Desktop/Monitor)
cat <<EOF > launch_client.sh
#!/bin/bash
source venv/bin/activate
python3 radio_client.py "\$@"
EOF
chmod +x launch_client.sh

echo ""
echo "============================================================"
echo " SETUP COMPLETE"
echo "============================================================"
echo "To start the Headless Server (Recommended for Pi):"
echo "  ./launch_server.sh"
echo ""
echo "To start the GUI Client (Requires Desktop):"
echo "  ./launch_client.sh"
echo "============================================================"
