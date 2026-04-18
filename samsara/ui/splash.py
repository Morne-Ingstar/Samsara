"""
Samsara Splash Screen

Loading splash screen with animated "De-articulating Splines..." text,
shown during application startup.
"""

import time
import tkinter as tk
from tkinter import ttk


class SplashScreen:
    """Loading splash screen shown during startup"""

    def __init__(self):
        self.start_time = time.time()
        self.min_display_time = 3.0  # Minimum seconds to show splash

        # Create hidden root that will be reused by the app
        self.root = tk.Tk()
        self.root.withdraw()  # Hide root

        # Create splash as Toplevel
        self.splash = tk.Toplevel(self.root)
        self.splash.title("Samsara")
        self.splash.overrideredirect(True)  # No window decorations
        self.splash.attributes('-topmost', True)

        # Window size and centering
        width, height = 350, 165
        x = (self.splash.winfo_screenwidth() // 2) - (width // 2)
        y = (self.splash.winfo_screenheight() // 2) - (height // 2)
        self.splash.geometry(f"{width}x{height}+{x}+{y}")

        # Dark theme
        self.splash.configure(bg='#2d2d2d')

        # App name
        tk.Label(self.splash, text="Samsara", font=('Segoe UI', 20, 'bold'),
                bg='#2d2d2d', fg='#00CED1').pack(pady=(25, 2))

        # Flavour subtitle with animated dots
        self._dot_count = 0
        self._subtitle_var = tk.StringVar(value="De-articulating Splines.")
        tk.Label(self.splash, textvariable=self._subtitle_var,
                 font=('Segoe UI', 9, 'italic'), bg='#2d2d2d',
                 fg='#666666').pack(pady=(0, 5))
        self._animate_dots()

        # Status text
        self.status_var = tk.StringVar(value="Starting...")
        self.status_label = tk.Label(self.splash, textvariable=self.status_var,
                                      font=('Segoe UI', 10), bg='#2d2d2d', fg='#aaaaaa')
        self.status_label.pack(pady=(5, 15))

        # Progress bar
        self.progress = ttk.Progressbar(self.splash, length=280, mode='indeterminate')
        self.progress.pack(pady=(0, 20))
        self.progress.start(15)

        # Force splash to be visible
        self.splash.lift()
        self.splash.focus_force()
        self.splash.update_idletasks()
        self.splash.update()
        self.root.update_idletasks()
        self.root.update()

    def _animate_dots(self):
        """Cycle the subtitle dots: . → .. → ... → . ..."""
        self._dot_count = (self._dot_count % 3) + 1
        self._subtitle_var.set("De-articulating Splines" + "." * self._dot_count)
        try:
            self.splash.after(500, self._animate_dots)
        except Exception:
            pass  # splash may be closing

    def set_status(self, text):
        """Update status text"""
        self.status_var.set(text)
        self.splash.update_idletasks()
        self.splash.update()
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        """Close the splash screen but keep root for app to reuse"""
        try:
            # Ensure minimum display time
            elapsed = time.time() - self.start_time
            if elapsed < self.min_display_time:
                remaining = self.min_display_time - elapsed
                # Update splash during wait
                wait_until = time.time() + remaining
                while time.time() < wait_until:
                    self.splash.update()
                    time.sleep(0.05)

            self.progress.stop()
            self.splash.destroy()
        except:
            pass

    def get_root(self):
        """Return the root window for app to reuse"""
        return self.root

