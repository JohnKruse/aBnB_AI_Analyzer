#!/usr/bin/env python3

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from ttkthemes import ThemedTk
from PIL import Image, ImageTk
import os
import sys
import subprocess
import threading
from queue import Queue, Empty
from pathlib import Path
from src.logging_config import setup_logger, LAUNCHER_LOG

# Set up logger
logger = setup_logger(__name__, LAUNCHER_LOG)

class LauncherUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BnB AI Analyzer")
        
        # Set project root and searches directory
        self.project_root = Path(__file__).parent
        self.searches_dir = self.project_root / "searches"
        self.searches_dir.mkdir(exist_ok=True)
        
        # Queue for monitor output
        self.output_queue = Queue()
        
        # Set window size to 1200x1000 and center it
        window_width = 1200
        window_height = 1000
        
        # Get the screen dimensions
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        
        # Calculate position coordinates
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        
        # Set the window size and position
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Prevent window resizing
        self.root.resizable(False, False)
        
        # Configure ttk style
        self.style = ttk.Style()
        
        # Set theme-aware colors for specific widgets that need it
        self.style.configure('Header.TFrame', background='white')
        self.style.configure('Header.TLabel', background='white')
        
        # Create the header frame (200px tall)
        self.header_frame = ttk.Frame(self.root, height=200, style='Header.TFrame')
        self.header_frame.pack(fill='x', padx=10, pady=5)
        self.header_frame.pack_propagate(False)
        
        # Load and display the logo on the left side of header
        logo_path = os.path.join(self.project_root, 'graphics', 'BnB_AI_Analyzer_logo.png')
        self.logo_img = Image.open(logo_path)
        
        # Calculate height to maintain aspect ratio within 200px height
        aspect_ratio = self.logo_img.width / self.logo_img.height
        new_height = 180
        new_width = int(new_height * aspect_ratio)
        self.logo_img = self.logo_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        self.logo_photo = ImageTk.PhotoImage(self.logo_img)
        self.logo_label = ttk.Label(self.header_frame, image=self.logo_photo)
        self.logo_label.pack(side='left', padx=10, pady=10)
        
        # Create right side header area for dynamic content
        self.header_content = ttk.Frame(self.header_frame, style='Header.TFrame')
        self.header_content.pack(side='left', fill='both', expand=True, padx=10, pady=10)
        
        # Add header text
        self.header_text = ttk.Label(
            self.header_content,
            text="Welcome to BnB AI Analyzer\n\nManage your Airbnb property searches below.",
            wraplength=700,
            justify='left',
            style='Header.TLabel',
            font=('Arial', 16)
        )
        self.header_text.pack(anchor='w')
        
        # Main content area
        self.main_frame = tk.Frame(self.root, bg='white')
        self.main_frame.pack(fill='both', expand=True, padx=20, pady=10)
        
        # Create two main sections side by side with adjusted widths
        self.left_frame = tk.Frame(self.main_frame, bg='white', width=400)  # Reduced width
        self.left_frame.pack(side='left', fill='both', padx=10)
        self.left_frame.pack_propagate(False)  # Maintain fixed width
        
        self.right_frame = tk.Frame(self.main_frame, bg='white')
        self.right_frame.pack(side='left', fill='both', expand=True, padx=10)  # Expand to fill space
        
        # Left side - Existing Searches
        tk.Label(
            self.left_frame,
            text="Existing Searches",
            font=('Arial', 16, 'bold'),  # Increased font size
            bg='white'
        ).pack(pady=(0, 10))
        
        # Listbox for existing searches with scrollbar
        self.searches_frame = tk.Frame(self.left_frame, bg='white')
        self.searches_frame.pack(fill='both', expand=True)
        
        self.scrollbar = ttk.Scrollbar(self.searches_frame)
        self.scrollbar.pack(side='right', fill='y')
        
        self.searches_list = tk.Listbox(
            self.searches_frame,
            yscrollcommand=self.scrollbar.set,
            font=('Arial', 16),  # Increased font size
            selectmode='single',
            height=15
        )
        self.searches_list.pack(fill='both', expand=True)
        self.scrollbar.config(command=self.searches_list.yview)
        
        # Buttons for existing searches
        self.buttons_frame = tk.Frame(self.left_frame, bg='white')
        self.buttons_frame.pack(fill='x', pady=10)
        
        ttk.Button(
            self.buttons_frame,
            text="Open Selected Search",
            command=self.open_selected_search
        ).pack(side='left', padx=5)
        
        ttk.Button(
            self.buttons_frame,
            text="Delete Selected Search",
            command=self.delete_selected_search
        ).pack(side='left', padx=5)
        
        # Right side - New Search
        tk.Label(
            self.right_frame,
            text="Start New Search",
            font=('Arial', 16, 'bold'),  # Increased font size
            bg='white'
        ).pack(pady=(0, 10))
        
        # URL input frame
        self.url_frame = tk.Frame(self.right_frame, bg='white')
        self.url_frame.pack(fill='x', pady=10)
        
        tk.Label(
            self.url_frame,
            text="Paste Airbnb URL:",
            font=('Arial', 12),
            bg='white'
        ).pack(anchor='w')
        
        self.url_entry = ttk.Entry(self.url_frame, width=50)
        self.url_entry.pack(fill='x', pady=5)
        
        ttk.Button(
            self.url_frame,
            text="Start New Search",
            command=self.start_new_search
        ).pack(anchor='w')
        
        # Example URL
        tk.Label(
            self.right_frame,
            text="\nExample URL format:",
            font=('Arial', 10),
            bg='white'
        ).pack(anchor='w')
        
        tk.Label(
            self.right_frame,
            text="https://www.airbnb.com/s/London--United-Kingdom/homes?tab_id=home_tab&refinement_paths%5B%5D=%2Fhomes&query=London%2C%20United%20Kingdom&place_id=ChIJdd4hrwug2EcRmSrV3Vo6llI&date_picker_type=calendar&checkin=2024-02-01&checkout=2024-02-29",
            font=('Arial', 8),
            bg='white',
            wraplength=400,
            justify='left'
        ).pack(anchor='w')
        
        # Add status text area at the bottom
        self.status_frame = tk.Frame(self.root, bg='white')
        self.status_frame.pack(fill='x', padx=20, pady=10)
        
        tk.Label(
            self.status_frame,
            text="Monitor Status:",
            font=('Arial', 12, 'bold'),
            bg='white'
        ).pack(anchor='w')
        
        self.status_text = scrolledtext.ScrolledText(
            self.status_frame,
            height=10,
            width=100,
            font=('Courier', 10)
        )
        self.status_text.pack(fill='x', expand=True)
        
        # Start the output processing
        self.process_output()
        
        # Populate existing searches
        self.refresh_searches_list()

    def process_output(self):
        """Process any pending output in the queue"""
        try:
            while True:
                # Get output from queue without blocking
                try:
                    output = self.output_queue.get_nowait()
                    self.status_text.insert(tk.END, output + '\n')
                    self.status_text.see(tk.END)  # Scroll to bottom
                    self.status_text.update_idletasks()
                except Empty:
                    break
        finally:
            # Schedule next update
            self.root.after(100, self.process_output)

    def run_process_with_output(self, cmd, **kwargs):
        """Run a process and capture its output in real-time"""
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **kwargs
        )
        
        def reader_thread():
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    self.output_queue.put(line.rstrip())
            
            process.stdout.close()
            return_code = process.wait()
            if return_code != 0:
                self.output_queue.put(f"Process exited with code {return_code}")
                return False
            return True
        
        thread = threading.Thread(target=reader_thread)
        thread.daemon = True
        thread.start()
        return thread, process

    def open_selected_search(self):
        """Open the selected search and launch the monitor program"""
        selection = self.searches_list.curselection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a search to open.")
            return
        
        search_name = self.searches_list.get(selection[0])
        logger.info(f"Opening search: {search_name}")
        
        # Clear status text
        self.status_text.delete(1.0, tk.END)
        self.status_text.insert(tk.END, f"Opening search: {search_name}\n")
        
        # Verify config file exists
        config_path = self.searches_dir / search_name / "config.yaml"
        if not config_path.exists():
            error_msg = f"Config file not found at {config_path}"
            self.status_text.insert(tk.END, f"Error: {error_msg}\n")
            logger.error(error_msg)
            return
        
        # Set environment variable for the search
        os.environ['SEARCH_SUBDIR'] = search_name
        logger.info(f"Using search subdir: {search_name}")
        self.status_text.insert(tk.END, f"Using search subdir: {search_name}\n")
        
        try:
            # Run abnb_monitor.py
            logger.info("Running abnb_monitor.py")
            self.status_text.insert(tk.END, "Starting monitor program...\n")
            monitor_path = self.project_root / "abnb_monitor.py"
            
            # Run monitor and wait for it to complete
            monitor_thread, monitor_process = self.run_process_with_output(
                ["/opt/anaconda3/envs/airbnb/bin/python", str(monitor_path)],
                cwd=str(self.project_root)
            )
            
            # Wait for monitor to complete
            monitor_thread.join()
            
            # Check if monitor completed successfully
            if monitor_process.returncode != 0:
                error_msg = f"Monitor program failed with exit code {monitor_process.returncode}"
                logger.error(error_msg)
                self.status_text.insert(tk.END, f"Error: {error_msg}\n")
                return
            
            # Run review_app.py using streamlit
            logger.info("Running review_app.py")
            self.status_text.insert(tk.END, "Starting review app...\n")
            review_app_path = self.project_root / "review_app.py"
            
            # Run streamlit in a separate process
            subprocess.Popen(
                ["streamlit", "run", str(review_app_path)],
                cwd=str(self.project_root)
            )
            
            self.status_text.insert(tk.END, "Review app launched successfully.\n")
            
        except Exception as e:
            error_msg = f"Error running pipeline: {str(e)}"
            logger.exception(error_msg)
            self.status_text.insert(tk.END, f"Error: {error_msg}\n")
            return

    def delete_selected_search(self):
        """Delete the selected search"""
        selection = self.searches_list.curselection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a search to delete.")
            return
        
        search_name = self.searches_list.get(selection[0])
        if messagebox.askyesno("Confirm Delete", f"Are you sure you want to delete '{search_name}'?"):
            try:
                search_path = self.searches_dir / search_name
                if search_path.exists():
                    # TODO: Implement safe directory deletion
                    logger.info(f"Would delete search directory: {search_path}")
                    messagebox.showinfo("Not Implemented", "Search deletion not yet implemented.")
                self.refresh_searches_list()
            except Exception as e:
                error_msg = f"Error deleting search: {str(e)}"
                logger.error(error_msg)
                messagebox.showerror("Error", error_msg)

    def start_new_search(self):
        """Start a new search with the provided URL"""
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Please enter an Airbnb URL.")
            return
        # TODO: Implement URL validation and new search creation
        logger.info(f"Would start new search with URL: {url}")
        messagebox.showinfo("Not Implemented", "New search creation not yet implemented.")

    def refresh_searches_list(self):
        """Refresh the list of existing searches"""
        self.searches_list.delete(0, tk.END)
        # Get actual searches from the searches directory
        existing_searches = [d.name for d in self.searches_dir.iterdir() if d.is_dir()]
        for search in sorted(existing_searches):
            self.searches_list.insert(tk.END, search)

def main():
    root = ThemedTk(theme="arc")
    app = LauncherUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
