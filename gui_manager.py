import tkinter as tk
from tkinter import messagebox
import pystray
from PIL import Image
import threading
import os
import webbrowser

class GuiManager:
    def __init__(self, title="AlgoBot Control"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("300x250")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._setup_ui()

    def _setup_ui(self):
        tk.Label(self.root, text="AlgoBot Terminal", font=("Arial", 12, "bold")).pack(pady=10)
        
        tk.Button(self.root, text="Dashboard", command=lambda: webbrowser.open("http://127.0.0.1:8000"), 
                  width=20, bg="#28a745", fg="white").pack(pady=5)
        
        tk.Button(self.root, text="Свернуть в трей", command=self.hide_window, width=20).pack(pady=5)
        
        tk.Label(self.root, text="Бот работает в фоне", fg="gray").pack(pady=10)

    def hide_window(self):
        self.root.withdraw()
        self.show_tray()

    def show_window(self, icon=None):
        if icon:
            icon.stop()
        self.root.after(0, self.root.deiconify)

    def quit_all(self, icon):
        icon.stop()
        self.root.destroy()
        os._exit(0) # Завершаем все потоки, включая asyncio

    def show_tray(self):
        # Создаем иконку (если нет файла, создаем цветной квадрат)
        if os.path.exists("icon.png"):
            image = Image.open("icon.png")
        else:
            image = Image.new('RGB', (64, 64), color=(40, 167, 69))

        menu = pystray.Menu(
            pystray.MenuItem("Развернуть", self.show_window),
            pystray.MenuItem("Выход", self.quit_all)
        )
        self.icon = pystray.Icon("AlgoBot", image, "AlgoBot Trading", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def run(self):
        self.root.mainloop()