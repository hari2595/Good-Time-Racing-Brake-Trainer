import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from backend_winmm import Backend as WinMMBackend
from drills import run_daily_precision, run_car_specific, run_random_hold
import time


# Placeholder for database save (later with SQLite)
def save_run_to_db(drill_type, result_data):
    # TODO: Implement SQLite insert here
    print(f"[DB] Saving run for {drill_type}: {result_data}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pedal Trainer")
        self.geometry("800x600")

        # Backend
        self.backend = WinMMBackend()
        self.buffer = []
        self.drill_type = None

        # UI elements
        self.create_widgets()

        # Ask for drill type
        self.select_drill_type()

    def create_widgets(self):
        self.tree = ttk.Treeview(self, columns=("axis",), show='headings')
        self.tree.heading("axis", text="Axis Readings")
        self.tree.pack(fill="x", pady=5)

        self.start_btn = tk.Button(self, text="Start", command=self.start_drill)
        self.start_btn.pack(pady=5)

        self.stop_btn = tk.Button(self, text="Stop", command=self.stop_drill)
        self.stop_btn.pack(pady=5)

        self.output_box = tk.Text(self, height=10, state="disabled")
        self.output_box.pack(fill="both", expand=True)

    def select_drill_type(self):
        drills = ["Daily Precision", "Car Specific", "Random Hold"]
        choice = simpledialog.askstring(
            "Select Drill",
            "Choose drill type:\n1 - Daily Precision\n2 - Car Specific\n3 - Random Hold"
        )
        if not choice:
            self.destroy()
            return

        if choice == "1":
            self.drill_type = "Daily Precision"
        elif choice == "2":
            self.drill_type = "Car Specific"
        elif choice == "3":
            self.drill_type = "Random Hold"
        else:
            messagebox.showerror("Invalid Choice", "Please restart and choose 1, 2, or 3")
            self.destroy()

    def start_drill(self):
        if not self.drill_type:
            messagebox.showerror("No Drill Selected", "Please restart and choose a drill.")
            return

        self.buffer.clear()
        self.backend.apply_device(0)
        self._loop()

    def stop_drill(self):
        self.after_cancel(self._after_id)
        self.prompt_save_discard()

    def prompt_save_discard(self):
        choice = messagebox.askyesno("Save Run?", "Do you want to save this run?")
        if choice:
            save_run_to_db(self.drill_type, self.buffer)
        self.buffer.clear()

    def _loop(self):
        vals = self.backend.read_raw(self.backend.dev_id)
        self.tree.delete(*self.tree.get_children())
        if vals:
            for name, v in zip(self.backend.AXIS_NAMES, vals):
                self.tree.insert('', 'end', values=(f'{name}: {v}',))

        sample = self.backend.poll()
        if sample:
            t, b, tb, raw = sample
            self.buffer.append((t, b, tb))

        self._after_id = self.after(8, self._loop)


if __name__ == "__main__":
    app = App()
    app.mainloop()

