"""
Samsara Splash Screen

Loading splash screen shown during application startup.
"""

import time
import tkinter as tk
from tkinter import ttk
from typing import Optional


class SplashScreen:
    """Loading splash screen shown during startup."""

    def __init__(self, min_display_time: float = 3.0):
        """
        Initialize splash screen.

        Args:
            min_display_time: Minimum seconds to show splash
        """
        self.start_time = time.time()
        self.min_display_time = min_display_time

        # Create hidden root that will be reused by the app
        self.root = tk.Tk()
        self.root.withdraw()

        # Create splash as Toplevel
        self.splash = tk.Toplevel(self.root)
        self.splash.title("Samsara")
        self.splash.overrideredirect(True)
        self.splash.attributes('-topmost', True)

        # Window size and centering
        width, height = 350, 150
        x = (self.splash.winfo_screenwidth() // 2) - (width // 2)
        y = (self.splash.winfo_screenheight() // 2) - (height // 2)
        self.splash.geometry(f"{width}x{height}+{x}+{y}")

        # Dark theme
        self.splash.configure(bg='#2d2d2d')

        # App name
        tk.Label(
            self.splash,
            text="Samsara",
            font=('Segoe UI', 20, 'bold'),
            bg='#2d2d2d',
            fg='#00CED1',
        ).pack(pady=(25, 5))

        # Status text
        self.status_var = tk.StringVar(value="Starting...")
        self.status_label = tk.Label(
            self.splash,
            textvariable=self.status_var,
            font=('Segoe UI', 10),
            bg='#2d2d2d',
            fg='#aaaaaa',
        )
        self.status_label.pack(pady=(5, 15))

        # Progress bar
        self.progress = ttk.Progressbar(
            self.splash,
            length=280,
            mode='indeterminate',
        )
        self.progress.pack(pady=(0, 20))
        self.progress.start(15)

        # Force splash to be visible
        self._update()

    def _update(self) -> None:
        """Force UI update."""
        self.splash.lift()
        self.splash.focus_force()
        self.splash.update_idletasks()
        self.splash.update()
        self.root.update_idletasks()
        self.root.update()

    def set_status(self, text: str) -> None:
        """
        Update status text.

        Args:
            text: Status message to display
        """
        self.status_var.set(text)
        self._update()

    def close(self) -> None:
        """Close the splash screen but keep root for app to reuse."""
        try:
            # Ensure minimum display time
            elapsed = time.time() - self.start_time
            if elapsed < self.min_display_time:
                remaining = self.min_display_time - elapsed
                wait_until = time.time() + remaining
                while time.time() < wait_until:
                    self.splash.update()
                    time.sleep(0.05)

            self.progress.stop()
            self.splash.destroy()
        except Exception:
            pass

    def get_root(self) -> tk.Tk:
        """
        Return the root window for app to reuse.

        Returns:
            The hidden root Tk window
        """
        return self.root

    def destroy(self) -> None:
        """Fully destroy the splash and root window."""
        try:
            self.progress.stop()
            self.splash.destroy()
            self.root.destroy()
        except Exception:
            pass
