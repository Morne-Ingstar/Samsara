"""
Voice Training Module - Recognition calibration and custom vocabulary
"""
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import customtkinter as ctk
import sounddevice as sd
import numpy as np
import json
import threading
import time
import logging
from pathlib import Path
from datetime import datetime

# Set up logging
log_dir = Path(__file__).parent
log_file = log_dir / 'voice_training.log'

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
logger.info("Voice Training module loading...")


class VoiceTrainingWindow:
    """Voice training and recognition calibration interface"""
    
    def __init__(self, app):
        logger.info("Initializing VoiceTrainingWindow")
        try:
            self.app = app
            self.window = None
            self.monitoring = False
            self.test_phrases = [
                "the quick brown fox jumps over the lazy dog",
                "pack my box with five dozen liquor jugs",
                "sphinx of black quartz judge my vow",
                "how vexingly quick daft zebras jump",
                "the five boxing wizards jump quickly"
            ]
            self.custom_vocab = []
            self.corrections_dict = {}
            self.load_training_data()
            logger.info("VoiceTrainingWindow initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing VoiceTrainingWindow: {e}", exc_info=True)
            raise
        
    def load_training_data(self):
        """Load custom vocabulary and corrections from config"""
        logger.info("Loading training data...")
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        if training_file.exists():
            try:
                with open(training_file, 'r') as f:
                    data = json.load(f)
                    self.custom_vocab = data.get('vocabulary', [])
                    self.corrections_dict = data.get('corrections', {})
                logger.info(f"Loaded {len(self.custom_vocab)} vocab items, {len(self.corrections_dict)} corrections")
            except Exception as e:
                logger.error(f"Could not load training data: {e}", exc_info=True)
        else:
            logger.info("No training data file found, starting fresh")
    
    def save_training_data(self):
        """Save custom vocabulary and corrections"""
        logger.info("Saving training data...")
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        try:
            data = {
                'vocabulary': self.custom_vocab,
                'corrections': self.corrections_dict
            }
            with open(training_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info("Training data saved successfully")
            return True
        except Exception as e:
            logger.error(f"Could not save training data: {e}", exc_info=True)
            return False
    
    def show(self):
        """Display the voice training window"""
        logger.info("Opening voice training window...")
        try:
            if self.window is not None:
                logger.info("Window already exists, bringing to front")
                self.window.lift()
                self.window.focus_force()
                return

            # Set CustomTkinter appearance
            ctk.set_appearance_mode("system")
            ctk.set_default_color_theme("blue")

            self.window = ctk.CTkToplevel(self.app.root)
            self.window.title("Samsara Voice Training")
            self.window.geometry("750x750")
            self.window.resizable(True, True)
            self.window.minsize(700, 650)

            # Ensure window appears on top
            self.window.lift()
            self.window.focus_force()
            self.window.after(100, lambda: self.window.lift())

            # Use grid layout for reliable structure
            self.window.grid_rowconfigure(0, weight=1)
            self.window.grid_columnconfigure(0, weight=1)

            # Create tabview
            self.tabview = ctk.CTkTabview(self.window, corner_radius=10)
            self.tabview.grid(row=0, column=0, sticky='nsew', padx=20, pady=(20, 10))

            # Add tabs
            self.tabview.add("Calibration")
            self.tabview.add("Vocabulary")
            self.tabview.add("Corrections")
            self.tabview.add("Advanced")

            # Create tab contents
            logger.info("Creating calibration tab")
            self.create_calibration_tab()

            logger.info("Creating vocabulary tab")
            self.create_vocabulary_tab()

            logger.info("Creating corrections tab")
            self.create_corrections_tab()

            logger.info("Creating advanced tab")
            self.create_advanced_tab()

            # Close button at bottom
            btn_frame = ctk.CTkFrame(self.window, fg_color="transparent", height=60)
            btn_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 20))
            btn_frame.grid_propagate(False)

            ctk.CTkButton(btn_frame, text="Close", width=120, height=40,
                         command=self.close).pack(side='right', pady=10)

            self.window.protocol("WM_DELETE_WINDOW", self.close)
            logger.info("Voice training window opened successfully")
        except Exception as e:
            logger.error(f"Error opening voice training window: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to open Voice Training:\n{e}\n\nCheck voice_training.log for details")
    
    def create_calibration_tab(self):
        """Create the calibration and testing tab"""
        try:
            tab = self.tabview.tab("Calibration")

            # Microphone Level Monitor section
            ctk.CTkLabel(tab, text="Microphone Level Monitor",
                        font=ctk.CTkFont(size=16, weight="bold")).pack(anchor='w', pady=(15, 10))

            monitor_frame = ctk.CTkFrame(tab, corner_radius=10)
            monitor_frame.pack(fill='x', pady=(0, 20), padx=5)

            self.level_canvas = tk.Canvas(monitor_frame, height=40, bg='#333333', highlightthickness=1)
            self.level_canvas.pack(fill='x', padx=15, pady=(15, 5))

            self.level_label = ctk.CTkLabel(monitor_frame, text="Volume: 0%")
            self.level_label.pack(pady=5)

            self.monitor_btn = ctk.CTkButton(monitor_frame, text="Start Monitoring",
                                             width=150, command=self.toggle_monitoring)
            self.monitor_btn.pack(pady=10)

            ctk.CTkLabel(monitor_frame, text="Speak at your normal volume. Green = good level, Red = too loud",
                        text_color="gray").pack(pady=(0, 15))

            # Test Phrases Section
            ctk.CTkLabel(tab, text="Recognition Test Phrases",
                        font=ctk.CTkFont(size=16, weight="bold")).pack(anchor='w', pady=(10, 10))

            ctk.CTkLabel(tab, text="Test recognition accuracy by speaking these phrases:",
                        text_color="gray").pack(anchor='w', pady=(0, 10))

            # Frame for test phrases
            self.test_results_frame = ctk.CTkFrame(tab, corner_radius=10)
            self.test_results_frame.pack(fill='both', expand=True, padx=5)

            self.create_test_phrase_widgets()
            logger.info("Calibration tab created")
        except Exception as e:
            logger.error(f"Error creating calibration tab: {e}", exc_info=True)
            raise
    
    def create_test_phrase_widgets(self):
        """Create widgets for each test phrase"""
        try:
            for widget in self.test_results_frame.winfo_children():
                widget.destroy()

            self.phrase_results = []

            for i, phrase in enumerate(self.test_phrases):
                frame = ctk.CTkFrame(self.test_results_frame, fg_color="transparent")
                frame.pack(fill='x', padx=15, pady=8)

                # Phrase text
                phrase_label = ctk.CTkLabel(frame, text=f"#{i+1}: {phrase}", wraplength=400, anchor='w')
                phrase_label.pack(side='left', fill='x', expand=True)

                # Test button
                test_btn = ctk.CTkButton(frame, text="Test", width=70,
                                        command=lambda p=phrase, idx=i: self.test_phrase(p, idx))
                test_btn.pack(side='right', padx=5)

                # Status indicator
                status_label = ctk.CTkLabel(frame, text="o", font=ctk.CTkFont(size=16), width=30)
                status_label.pack(side='right', padx=5)

                self.phrase_results.append((phrase, status_label))
        except Exception as e:
            logger.error(f"Error creating test phrase widgets: {e}", exc_info=True)
    
    def toggle_monitoring(self):
        """Start/stop microphone level monitoring"""
        try:
            if self.monitoring:
                self.stop_monitoring()
            else:
                self.start_monitoring()
        except Exception as e:
            logger.error(f"Error toggling monitoring: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to toggle monitoring: {e}")
    
    def start_monitoring(self):
        """Start monitoring microphone levels"""
        try:
            logger.info("Starting microphone monitoring")
            self.monitoring = True
            self.monitor_btn.configure(text="Stop Monitoring")
            
            def monitor_loop():
                stream = None
                try:
                    # Open stream
                    stream = sd.InputStream(
                        samplerate=16000,
                        channels=1,
                        dtype=np.float32,
                        device=self.app.config.get('microphone'),
                        blocksize=1024
                    )
                    stream.start()
                    logger.info("Monitoring stream started")
                    
                    while self.monitoring:
                        try:
                            data, _ = stream.read(1024)
                            rms = np.sqrt(np.mean(data**2))
                            db = 20 * np.log10(rms + 1e-10)
                            
                            # Normalize to 0-100 range
                            level = max(0, min(100, (db + 60) * 2))
                            
                            # Update UI in main thread
                            if self.window and self.window.winfo_exists():
                                self.window.after(0, self.update_level_display, level)
                            
                            time.sleep(0.05)
                        except Exception as e:
                            logger.error(f"Error in monitoring loop: {e}")
                            break
                except Exception as e:
                    logger.error(f"Error starting monitoring stream: {e}", exc_info=True)
                finally:
                    if stream:
                        stream.stop()
                        stream.close()
                    logger.info("Monitoring stream closed")
            
            threading.Thread(target=monitor_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"Error starting monitoring: {e}", exc_info=True)
            self.monitoring = False
            self.monitor_btn.configure(text="Start Monitoring")
            messagebox.showerror("Error", f"Failed to start monitoring: {e}")
    
    def stop_monitoring(self):
        """Stop monitoring microphone levels"""
        logger.info("Stopping microphone monitoring")
        self.monitoring = False
        self.monitor_btn.configure(text="Start Monitoring")
        self.level_canvas.delete('all')
        self.level_label.configure(text="Volume: 0%")
    
    def update_level_display(self, level):
        """Update the level meter visualization"""
        try:
            if not self.window or not self.window.winfo_exists():
                return
            
            self.level_canvas.delete('all')
            width = self.level_canvas.winfo_width()
            height = self.level_canvas.winfo_height()
            
            # Calculate bar width
            bar_width = (width - 4) * (level / 100)
            
            # Color based on level
            if level < 30:
                color = '#ff4444'  # Too quiet
            elif level < 70:
                color = '#44ff44'  # Good
            else:
                color = '#ffaa44'  # Too loud
            
            # Draw bar
            self.level_canvas.create_rectangle(2, 2, bar_width + 2, height - 2, 
                                              fill=color, outline='')
            
            # Draw threshold markers
            good_start = width * 0.3
            good_end = width * 0.7
            self.level_canvas.create_line(good_start, 0, good_start, height, fill='#666', dash=(2, 2))
            self.level_canvas.create_line(good_end, 0, good_end, height, fill='#666', dash=(2, 2))
            
            self.level_label.configure(text=f"Volume: {int(level)}%")
        except Exception as e:
            logger.error(f"Error updating level display: {e}")
    
    def test_phrase(self, phrase, idx):
        """Test recognition of a specific phrase"""
        try:
            logger.info(f"Testing phrase: {phrase}")
            status_label = self.phrase_results[idx][1]
            status_label.configure(text="...", text_color="#1f6aa5")
            self.window.update()

            def test():
                try:
                    # Record audio for 5 seconds
                    logger.info("Recording test phrase...")
                    audio = sd.rec(int(5 * 16000), samplerate=16000, channels=1, dtype=np.float32,
                                 device=self.app.config.get('microphone'))
                    sd.wait()
                    audio = audio.flatten()

                    # Transcribe
                    logger.info("Transcribing test phrase...")
                    segments, info = self.app.model.transcribe(
                        audio,
                        language=self.app.config['language'],
                        beam_size=5,
                        vad_filter=True
                    )

                    result = "".join([segment.text for segment in segments]).strip().lower()
                    expected = phrase.lower()

                    logger.info(f"Expected: {expected}")
                    logger.info(f"Got: {result}")

                    # Compare
                    if result == expected:
                        self.window.after(0, lambda: status_label.configure(text="OK", text_color="green"))
                        logger.info("Perfect match!")
                    else:
                        self.window.after(0, lambda: status_label.configure(text="X", text_color="red"))
                        logger.info("Mismatch detected")

                        # Show detailed result in popup
                        similarity = self.calculate_similarity(expected, result)
                        self.window.after(0, lambda: messagebox.showinfo(
                            "Test Result",
                            f"Expected:\n{expected}\n\nGot:\n{result}\n\nAccuracy: {similarity:.1f}%"
                        ))

                except Exception as e:
                    logger.error(f"Test phrase error: {e}", exc_info=True)
                    self.window.after(0, lambda: status_label.configure(text="!", text_color="orange"))

            threading.Thread(target=test, daemon=True).start()
        except Exception as e:
            logger.error(f"Error starting phrase test: {e}", exc_info=True)
    
    def calculate_similarity(self, s1, s2):
        """Calculate similarity percentage between two strings"""
        # Simple word-based similarity
        words1 = set(s1.split())
        words2 = set(s2.split())
        
        if not words1 and not words2:
            return 100.0
        if not words1 or not words2:
            return 0.0
        
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        
        return (intersection / union) * 100
    
    def create_vocabulary_tab(self):
        """Create the custom vocabulary tab"""
        try:
            tab = self.tabview.tab("Vocabulary")

            ctk.CTkLabel(tab, text="Custom Vocabulary",
                        font=ctk.CTkFont(size=16, weight="bold")).pack(anchor='w', pady=(15, 5))
            ctk.CTkLabel(tab, text="Add words/phrases that Whisper often misrecognizes (technical terms, names, etc.)",
                        text_color="gray", wraplength=600).pack(anchor='w', pady=(0, 15))

            # Input frame
            input_frame = ctk.CTkFrame(tab, fg_color="transparent")
            input_frame.pack(fill='x', pady=(0, 10))

            ctk.CTkLabel(input_frame, text="Word/Phrase:").pack(side='left')
            self.vocab_entry = ctk.CTkEntry(input_frame, width=300)
            self.vocab_entry.pack(side='left', padx=10)
            self.vocab_entry.bind('<Return>', lambda e: self.add_vocabulary())

            ctk.CTkButton(input_frame, text="Add", width=80, command=self.add_vocabulary).pack(side='left')

            # List frame
            list_frame = ctk.CTkFrame(tab, corner_radius=10)
            list_frame.pack(fill='both', expand=True, pady=(0, 10))

            ctk.CTkLabel(list_frame, text="Custom Words:", text_color="gray").pack(anchor='w', padx=15, pady=(10, 5))

            # Use tk.Listbox inside CTk frame (CTk doesn't have a listbox)
            listbox_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
            listbox_frame.pack(fill='both', expand=True, padx=15, pady=(0, 10))

            scrollbar = ttk.Scrollbar(listbox_frame)
            scrollbar.pack(side='right', fill='y')

            self.vocab_listbox = tk.Listbox(listbox_frame, yscrollcommand=scrollbar.set, height=12,
                                           bg='#333333', fg='white', selectbackground='#1f6aa5')
            self.vocab_listbox.pack(side='left', fill='both', expand=True)
            scrollbar.configure(command=self.vocab_listbox.yview)

            # Populate list
            self.refresh_vocab_list()

            # Buttons
            btn_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
            btn_frame.pack(pady=10)

            ctk.CTkButton(btn_frame, text="Remove Selected", width=130,
                         command=self.remove_vocabulary).pack(side='left', padx=5)
            ctk.CTkButton(btn_frame, text="Clear All", width=100, fg_color="gray40",
                         command=self.clear_vocabulary).pack(side='left', padx=5)

            # Info
            info_text = """How it works: These words will be added to Whisper's initial_prompt parameter,
biasing the model toward recognizing them correctly. Works best for proper nouns,
technical jargon, and domain-specific terminology."""

            ctk.CTkLabel(tab, text=info_text, text_color="gray", wraplength=600, justify='left').pack(pady=10)
            logger.info("Vocabulary tab created")
        except Exception as e:
            logger.error(f"Error creating vocabulary tab: {e}", exc_info=True)
            raise
    
    def add_vocabulary(self):
        """Add word to custom vocabulary"""
        try:
            word = self.vocab_entry.get().strip()
            if word and word not in self.custom_vocab:
                self.custom_vocab.append(word)
                self.vocab_listbox.insert(tk.END, word)
                self.vocab_entry.delete(0, tk.END)
                self.save_training_data()
                logger.info(f"Added to vocabulary: {word}")
        except Exception as e:
            logger.error(f"Error adding vocabulary: {e}", exc_info=True)
    
    def remove_vocabulary(self):
        """Remove selected word from vocabulary"""
        try:
            selection = self.vocab_listbox.curselection()
            if selection:
                idx = selection[0]
                word = self.vocab_listbox.get(idx)
                self.vocab_listbox.delete(idx)
                self.custom_vocab.remove(word)
                self.save_training_data()
                logger.info(f"Removed from vocabulary: {word}")
        except Exception as e:
            logger.error(f"Error removing vocabulary: {e}", exc_info=True)
    
    def clear_vocabulary(self):
        """Clear all vocabulary"""
        try:
            if messagebox.askyesno("Confirm", "Remove all custom vocabulary?"):
                self.custom_vocab = []
                self.vocab_listbox.delete(0, tk.END)
                self.save_training_data()
                logger.info("Cleared vocabulary")
        except Exception as e:
            logger.error(f"Error clearing vocabulary: {e}", exc_info=True)
    
    def refresh_vocab_list(self):
        """Refresh the vocabulary listbox"""
        try:
            self.vocab_listbox.delete(0, tk.END)
            for word in self.custom_vocab:
                self.vocab_listbox.insert(tk.END, word)
        except Exception as e:
            logger.error(f"Error refreshing vocab list: {e}", exc_info=True)
    
    def create_corrections_tab(self):
        """Create the corrections dictionary tab"""
        try:
            tab = self.tabview.tab("Corrections")

            ctk.CTkLabel(tab, text="Corrections Dictionary",
                        font=ctk.CTkFont(size=16, weight="bold")).pack(anchor='w', pady=(15, 5))
            ctk.CTkLabel(tab, text="Teach the system your common corrections (Whisper says X -> You meant Y)",
                        text_color="gray", wraplength=600).pack(anchor='w', pady=(0, 15))

            # Input frame
            input_frame = ctk.CTkFrame(tab, fg_color="transparent")
            input_frame.pack(fill='x', pady=(0, 10))

            ctk.CTkLabel(input_frame, text="Whisper says:").pack(side='left')
            self.wrong_entry = ctk.CTkEntry(input_frame, width=180)
            self.wrong_entry.pack(side='left', padx=10)

            ctk.CTkLabel(input_frame, text="->").pack(side='left')

            ctk.CTkLabel(input_frame, text="You meant:").pack(side='left', padx=(10, 0))
            self.correct_entry = ctk.CTkEntry(input_frame, width=180)
            self.correct_entry.pack(side='left', padx=10)
            self.correct_entry.bind('<Return>', lambda e: self.add_correction())

            ctk.CTkButton(input_frame, text="Add", width=80, command=self.add_correction).pack(side='left')

            # List frame
            list_frame = ctk.CTkFrame(tab, corner_radius=10)
            list_frame.pack(fill='both', expand=True, pady=(10, 0))

            ctk.CTkLabel(list_frame, text="Correction Rules:", text_color="gray").pack(anchor='w', padx=15, pady=(10, 5))

            # Treeview for corrections (keep ttk.Treeview as CTk doesn't have one)
            tree_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
            tree_frame.pack(fill='both', expand=True, padx=15, pady=(0, 10))

            scrollbar = ttk.Scrollbar(tree_frame)
            scrollbar.pack(side='right', fill='y')

            self.corrections_tree = ttk.Treeview(tree_frame, columns=('wrong', 'correct'),
                                                 show='headings', yscrollcommand=scrollbar.set, height=10)
            self.corrections_tree.heading('wrong', text='Whisper Says')
            self.corrections_tree.heading('correct', text='Correct Text')
            self.corrections_tree.column('wrong', width=280)
            self.corrections_tree.column('correct', width=280)
            self.corrections_tree.pack(side='left', fill='both', expand=True)
            scrollbar.configure(command=self.corrections_tree.yview)

            # Populate corrections
            self.refresh_corrections_tree()

            # Buttons
            btn_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
            btn_frame.pack(pady=10)

            ctk.CTkButton(btn_frame, text="Remove Selected", width=130,
                         command=self.remove_correction).pack(side='left', padx=5)
            ctk.CTkButton(btn_frame, text="Clear All", width=100, fg_color="gray40",
                         command=self.clear_corrections).pack(side='left', padx=5)

            # Info
            info_text = """How it works: After Whisper transcribes your speech, these corrections are
automatically applied as a post-processing step."""

            ctk.CTkLabel(tab, text=info_text, text_color="gray", wraplength=600, justify='left').pack(pady=10)
            logger.info("Corrections tab created")
        except Exception as e:
            logger.error(f"Error creating corrections tab: {e}", exc_info=True)
            raise
    
    def add_correction(self):
        """Add a correction rule"""
        try:
            wrong = self.wrong_entry.get().strip()
            correct = self.correct_entry.get().strip()
            
            if wrong and correct:
                self.corrections_dict[wrong] = correct
                self.corrections_tree.insert('', tk.END, values=(wrong, correct))
                self.wrong_entry.delete(0, tk.END)
                self.correct_entry.delete(0, tk.END)
                self.save_training_data()
                logger.info(f"Added correction: '{wrong}' -> '{correct}'")
        except Exception as e:
            logger.error(f"Error adding correction: {e}", exc_info=True)
    
    def remove_correction(self):
        """Remove selected correction rule"""
        try:
            selection = self.corrections_tree.selection()
            if selection:
                item = self.corrections_tree.item(selection[0])
                wrong = item['values'][0]
                
                self.corrections_tree.delete(selection[0])
                if wrong in self.corrections_dict:
                    del self.corrections_dict[wrong]
                self.save_training_data()
                logger.info(f"Removed correction: {wrong}")
        except Exception as e:
            logger.error(f"Error removing correction: {e}", exc_info=True)
    
    def clear_corrections(self):
        """Clear all corrections"""
        try:
            if messagebox.askyesno("Confirm", "Remove all correction rules?"):
                self.corrections_dict = {}
                for item in self.corrections_tree.get_children():
                    self.corrections_tree.delete(item)
                self.save_training_data()
                logger.info("Cleared corrections")
        except Exception as e:
            logger.error(f"Error clearing corrections: {e}", exc_info=True)
    
    def refresh_corrections_tree(self):
        """Refresh the corrections treeview"""
        try:
            for item in self.corrections_tree.get_children():
                self.corrections_tree.delete(item)
            
            for wrong, correct in self.corrections_dict.items():
                self.corrections_tree.insert('', tk.END, values=(wrong, correct))
        except Exception as e:
            logger.error(f"Error refreshing corrections tree: {e}", exc_info=True)
    
    def create_advanced_tab(self):
        """Create advanced recognition settings tab"""
        try:
            tab = self.tabview.tab("Advanced")

            ctk.CTkLabel(tab, text="Advanced Recognition Settings",
                        font=ctk.CTkFont(size=16, weight="bold")).pack(anchor='w', pady=(15, 15))

            # Model size info
            ctk.CTkLabel(tab, text="Model Selection", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 5))
            model_frame = ctk.CTkFrame(tab, corner_radius=10)
            model_frame.pack(fill='x', pady=(0, 15))

            current_model = self.app.config.get('model_size', 'base')
            ctk.CTkLabel(model_frame, text=f"Current model: {current_model}").pack(anchor='w', padx=15, pady=(15, 5))

            info_text = """tiny: Fastest | base: Recommended | small: Better | medium: Very good | large-v3: Best (GPU)
Change in main Settings -> requires restart"""
            ctk.CTkLabel(model_frame, text=info_text, text_color="gray", justify='left').pack(anchor='w', padx=15, pady=(0, 15))

            # Language settings
            ctk.CTkLabel(tab, text="Language", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 5))
            lang_frame = ctk.CTkFrame(tab, corner_radius=10)
            lang_frame.pack(fill='x', pady=(0, 15))

            lang_row = ctk.CTkFrame(lang_frame, fg_color="transparent")
            lang_row.pack(fill='x', padx=15, pady=15)

            ctk.CTkLabel(lang_row, text="Current language:").pack(side='left')

            self.lang_var = tk.StringVar(value=self.app.config.get('language', 'en'))
            lang_combo = ctk.CTkComboBox(lang_row, variable=self.lang_var, width=100, state='readonly',
                                        values=['en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'ru', 'zh', 'ja', 'ko'])
            lang_combo.pack(side='left', padx=10)

            ctk.CTkButton(lang_row, text="Apply", width=80, command=self.apply_language).pack(side='left')

            # Prompt engineering
            ctk.CTkLabel(tab, text="Initial Prompt", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 5))
            prompt_frame = ctk.CTkFrame(tab, corner_radius=10)
            prompt_frame.pack(fill='both', expand=True, pady=(0, 15))

            ctk.CTkLabel(prompt_frame, text="Custom context for Whisper (combined with vocabulary):",
                        text_color="gray").pack(anchor='w', padx=15, pady=(15, 5))

            # Use tk.Text inside CTk frame
            self.prompt_text = tk.Text(prompt_frame, height=4, wrap=tk.WORD, bg='#333333', fg='white',
                                      insertbackground='white')
            self.prompt_text.pack(fill='both', expand=True, padx=15, pady=5)

            current_prompt = self.app.config.get('initial_prompt', '')
            self.prompt_text.insert('1.0', current_prompt)

            ctk.CTkLabel(prompt_frame, text="Add context like: 'Technical discussion about Python, React...'",
                        text_color="gray").pack(anchor='w', padx=15, pady=5)

            ctk.CTkButton(prompt_frame, text="Save Prompt", width=120, command=self.save_prompt).pack(pady=10)

            # Export/Import
            ctk.CTkLabel(tab, text="Backup & Restore", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 5))
            export_frame = ctk.CTkFrame(tab, corner_radius=10)
            export_frame.pack(fill='x')

            btn_row = ctk.CTkFrame(export_frame, fg_color="transparent")
            btn_row.pack(pady=15)

            ctk.CTkButton(btn_row, text="Export Training Data", width=150, command=self.export_data).pack(side='left', padx=5)
            ctk.CTkButton(btn_row, text="Import Training Data", width=150, command=self.import_data).pack(side='left', padx=5)

            logger.info("Advanced tab created")
        except Exception as e:
            logger.error(f"Error creating advanced tab: {e}", exc_info=True)
            raise
    
    def apply_language(self):
        """Apply language change"""
        try:
            new_lang = self.lang_var.get()
            self.app.config['language'] = new_lang
            self.app.save_config()
            messagebox.showinfo("Language Changed", f"Language set to: {new_lang}\n\nChange takes effect immediately.")
            logger.info(f"Language changed to: {new_lang}")
        except Exception as e:
            logger.error(f"Error applying language: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to change language: {e}")
    
    def save_prompt(self):
        """Save custom initial prompt"""
        try:
            prompt = self.prompt_text.get('1.0', tk.END).strip()
            self.app.config['initial_prompt'] = prompt
            self.app.save_config()
            messagebox.showinfo("Saved", "Initial prompt saved successfully!")
            logger.info("Initial prompt saved")
        except Exception as e:
            logger.error(f"Error saving prompt: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to save prompt: {e}")
    
    def export_data(self):
        """Export training data to file"""
        from tkinter import filedialog
        try:
            filename = filedialog.asksaveasfilename(
                defaultextension=".json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
                initialfile="voice_training_backup.json"
            )
            
            if filename:
                data = {
                    'vocabulary': self.custom_vocab,
                    'corrections': self.corrections_dict,
                    'initial_prompt': self.app.config.get('initial_prompt', ''),
                    'language': self.app.config.get('language', 'en')
                }
                with open(filename, 'w') as f:
                    json.dump(data, f, indent=2)
                messagebox.showinfo("Success", f"Training data exported to:\n{filename}")
                logger.info(f"Exported training data to: {filename}")
        except Exception as e:
            logger.error(f"Error exporting data: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to export: {e}")
    
    def import_data(self):
        """Import training data from file"""
        from tkinter import filedialog
        try:
            filename = filedialog.askopenfilename(
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
            
            if filename:
                with open(filename, 'r') as f:
                    data = json.load(f)
                
                # Import vocabulary
                if 'vocabulary' in data:
                    self.custom_vocab = data['vocabulary']
                    self.refresh_vocab_list()
                
                # Import corrections
                if 'corrections' in data:
                    self.corrections_dict = data['corrections']
                    self.refresh_corrections_tree()
                
                # Import prompt
                if 'initial_prompt' in data:
                    self.app.config['initial_prompt'] = data['initial_prompt']
                    self.prompt_text.delete('1.0', tk.END)
                    self.prompt_text.insert('1.0', data['initial_prompt'])
                
                # Import language
                if 'language' in data:
                    self.app.config['language'] = data['language']
                    self.lang_var.set(data['language'])
                
                self.save_training_data()
                self.app.save_config()
                
                messagebox.showinfo("Success", "Training data imported successfully!")
                logger.info(f"Imported training data from: {filename}")
        except Exception as e:
            logger.error(f"Error importing data: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to import: {e}")
    
    def close(self):
        """Close the window"""
        try:
            if self.monitoring:
                self.stop_monitoring()
            
            self.window.destroy()
            self.window = None
            logger.info("Voice training window closed")
        except Exception as e:
            logger.error(f"Error closing window: {e}", exc_info=True)
    
    def get_initial_prompt(self):
        """Get the complete initial prompt with vocabulary"""
        try:
            parts = []
            
            # Add custom prompt
            custom_prompt = self.app.config.get('initial_prompt', '')
            if custom_prompt:
                parts.append(custom_prompt)
            
            # Add vocabulary
            if self.custom_vocab:
                vocab_text = ", ".join(self.custom_vocab)
                parts.append(f"Common terms: {vocab_text}")
            
            prompt = " ".join(parts) if parts else None
            if prompt:
                logger.debug(f"Using initial prompt: {prompt}")
            return prompt
        except Exception as e:
            logger.error(f"Error building initial prompt: {e}", exc_info=True)
            return None
    
    def apply_corrections(self, text):
        """Apply corrections dictionary to transcribed text"""
        try:
            original = text
            for wrong, correct in self.corrections_dict.items():
                text = text.replace(wrong, correct)
            if text != original:
                logger.debug(f"Applied corrections: '{original}' -> '{text}'")
            return text
        except Exception as e:
            logger.error(f"Error applying corrections: {e}", exc_info=True)
            return text
