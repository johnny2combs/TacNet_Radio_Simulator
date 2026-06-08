import time
import threading
from radio_client import RadioClientGUI
from tkinter import Tk

def test():
    gui = RadioClientGUI()
    def auto_connect():
        print("Auto-connecting...")
        gui._toggle_connect()
        print("Connected!")
        time.sleep(2)
        print("Still running?", gui._core._running)
        gui._on_close()
    
    threading.Timer(1.0, auto_connect).start()
    gui.run()

if __name__ == "__main__":
    test()
