#!/usr/bin/env python3
"""
ByteDog - Lightweight System Resource Monitor
A minimal-resource system monitor following the Dog family design principles
Complete version with NetDog-style view toggling
"""

import tkinter as tk
from tkinter import ttk, messagebox, font
import psutil
import threading
import time
import platform
import os
import json
from datetime import datetime, timedelta
from collections import deque
import subprocess
import sys
import queue

# Try to import optional dependencies
try:
    import GPUtil

    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False

# Hide console window on Windows when running as EXE
if platform.system() == 'Windows' and getattr(sys, 'frozen', False):
    import ctypes

    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)


def resource_path(name: str) -> str:
    """
    Resolve bundled resource paths (works for PyInstaller and normal runs).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(__file__), name)


class SystemMonitor:
    """Core system monitoring functionality"""

    def __init__(self):
        self.cpu_history = deque(maxlen=60)
        self.ram_history = deque(maxlen=60)
        self.gpu_history = deque(maxlen=60) if GPU_AVAILABLE else None
        self.update_interval = 2.0
        self.process_cache = []
        self.last_process_update = 0

    def get_cpu_usage(self):
        """Get current CPU usage percentage"""
        return psutil.cpu_percent(interval=None)

    def get_cpu_per_core(self):
        """Get CPU usage per core"""
        return psutil.cpu_percent(interval=None, percpu=True)

    def get_memory_info(self):
        """Get memory usage information"""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            'percent': mem.percent,
            'used': mem.used,
            'available': mem.available,
            'total': mem.total,
            'swap_percent': swap.percent,
            'swap_used': swap.used,
            'swap_total': swap.total
        }

    def get_disk_info(self):
        """Get disk usage information"""
        disks = []
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append({
                    'device': partition.device,
                    'mountpoint': partition.mountpoint,
                    'percent': usage.percent,
                    'used': usage.used,
                    'total': usage.total
                })
            except:
                continue
        return disks

    def get_network_info(self):
        """Get network usage information"""
        net = psutil.net_io_counters()
        return {
            'bytes_sent': net.bytes_sent,
            'bytes_recv': net.bytes_recv,
            'packets_sent': net.packets_sent,
            'packets_recv': net.packets_recv
        }

    def get_gpu_info(self):
        """Get GPU usage information if available"""
        if not GPU_AVAILABLE:
            return None

        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu = gpus[0]
                return {
                    'name': gpu.name,
                    'load': gpu.load * 100,
                    'memory_used': gpu.memoryUsed,
                    'memory_total': gpu.memoryTotal,
                    'memory_percent': (gpu.memoryUsed / gpu.memoryTotal) * 100 if gpu.memoryTotal > 0 else 0,
                    'temperature': gpu.temperature
                }
        except:
            pass
        return None

    def get_process_list(self, use_cache=False):
        """Get list of running processes with details"""
        # Use cache if requested and cache is fresh
        if use_cache and self.process_cache and (time.time() - self.last_process_update < 2):
            return self.process_cache

        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_percent', 'status']):
            try:
                pinfo = proc.info
                # Don't calculate CPU percent here - it's too slow
                pinfo['cpu_percent'] = 0.0
                processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self.process_cache = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)
        self.last_process_update = time.time()
        return self.process_cache


class ProcessManager:
    """Process management functionality"""

    @staticmethod
    def kill_process(pid):
        """Kill a process by PID"""
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            time.sleep(0.5)
            if proc.is_running():
                proc.kill()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            return False

    @staticmethod
    def suspend_process(pid):
        """Suspend a process"""
        try:
            proc = psutil.Process(pid)
            proc.suspend()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    @staticmethod
    def resume_process(pid):
        """Resume a suspended process"""
        try:
            proc = psutil.Process(pid)
            proc.resume()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False


class MinimalView(tk.Toplevel):
    """Minimal overlay view showing only essential metrics"""

    def __init__(self, parent, monitor):
        super().__init__(parent)
        self.monitor = monitor
        self.parent = parent

        self.title("ByteDog Mini")
        self.geometry("200x80")
        self.resizable(False, False)
        self.attributes('-topmost', True)
        self.overrideredirect(True)

        # Window icon for Windows
        if platform.system() == "Windows":
            try:
                self.iconbitmap(resource_path("ByteDog_256.ico"))
            except Exception:
                pass

        self.configure(bg='#1e1e1e')

        self.setup_ui()
        self.make_draggable()
        self.update_data()

    def setup_ui(self):
        """Setup minimal UI"""
        # Title bar
        title_frame = tk.Frame(self, bg='#2d2d2d', height=20)
        title_frame.pack(fill='x')
        title_frame.pack_propagate(False)

        tk.Label(title_frame, text="ByteDog 🐕", fg='#ffffff', bg='#2d2d2d',
                 font=('Arial', 9, 'bold')).pack(side='left', padx=5)

        close_btn = tk.Button(title_frame, text="×", fg='#ffffff', bg='#2d2d2d',
                              font=('Arial', 12), bd=0, command=self.destroy)
        close_btn.pack(side='right', padx=5)

        # Metrics
        self.cpu_label = tk.Label(self, text="CPU: 0%", fg='#00ff00', bg='#1e1e1e',
                                  font=('Consolas', 11))
        self.cpu_label.pack(pady=5)

        self.ram_label = tk.Label(self, text="RAM: 0%", fg='#00ff00', bg='#1e1e1e',
                                  font=('Consolas', 11))
        self.ram_label.pack()

    def make_draggable(self):
        """Make window draggable"""

        def start_move(event):
            self.x = event.x
            self.y = event.y

        def on_move(event):
            x = self.winfo_x() + event.x - self.x
            y = self.winfo_y() + event.y - self.y
            self.geometry(f"+{x}+{y}")

        self.bind('<Button-1>', start_move)
        self.bind('<B1-Motion>', on_move)

    def update_data(self):
        """Update displayed metrics"""
        try:
            cpu = self.monitor.get_cpu_usage()
            mem = self.monitor.get_memory_info()

            # Color code based on usage
            cpu_color = '#00ff00' if cpu < 50 else '#ffff00' if cpu < 80 else '#ff0000'
            mem_color = '#00ff00' if mem['percent'] < 50 else '#ffff00' if mem['percent'] < 80 else '#ff0000'

            self.cpu_label.config(text=f"CPU: {cpu:.1f}%", fg=cpu_color)
            self.ram_label.config(text=f"RAM: {mem['percent']:.1f}%", fg=mem_color)
        except:
            pass

        if self.winfo_exists():
            self.after(2000, self.update_data)


class ByteDogApp:
    """Main ByteDog application"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ByteDog - System Resource Monitor 🐕")

        # Window icon for Windows
        if platform.system() == "Windows":
            try:
                self.root.iconbitmap(resource_path("ByteDog_256.ico"))
            except Exception:
                pass

        self.monitor = SystemMonitor()
        self.process_manager = ProcessManager()

        # Initialize CPU percent to prevent blocking
        psutil.cpu_percent(interval=None)

        self.view_mode = tk.StringVar(value="minimal")  # Start with minimal like NetDog
        self.minimal_window = None
        self.selected_process = None
        self.sort_column = 'memory_percent'
        self.sort_reverse = True

        # Queue for thread communication
        self.data_queue = queue.Queue()

        # Dark theme colors
        self.colors = {
            'bg': '#1e1e1e',
            'fg': '#ffffff',
            'button': '#2d2d2d',
            'select': '#404040',
            'accent': '#007acc',
            'success': '#4caf50',
            'warning': '#ff9800',
            'error': '#f44336'
        }

        # Dragging variables
        self.drag_start_x = 0
        self.drag_start_y = 0

        self.setup_window()
        self.setup_styles()
        self.setup_ui()
        self.start_monitoring()
        self.process_queue()

    def setup_window(self):
        """Configure the main window"""
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.9)

        # Position in top-right corner
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = screen_width - 200 - 20  # Start smaller for minimal view
        y = 50
        self.root.geometry(f"200x80+{x}+{y}")

        # Make window draggable
        self.root.bind('<Button-1>', self.start_drag)
        self.root.bind('<B1-Motion>', self.on_drag)

        # Bind dragging to all child widgets
        self.root.bind_all('<Button-1>', self.start_drag)
        self.root.bind_all('<B1-Motion>', self.on_drag)

    def start_drag(self, event):
        """Start dragging the window"""
        # Don't drag if clicking on expand button
        if hasattr(self, 'minimal_expand_btn') and event.widget == self.minimal_expand_btn:
            return
        if hasattr(self, 'toggle_btn') and event.widget == self.toggle_btn:
            return
        if hasattr(self, 'detailed_toggle_btn') and event.widget == self.detailed_toggle_btn:
            return
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def on_drag(self, event):
        """Handle window dragging"""
        x = self.root.winfo_pointerx() - self.drag_start_x
        y = self.root.winfo_pointery() - self.drag_start_y
        self.root.geometry(f"+{x}+{y}")

    def setup_styles(self):
        """Configure ttk styles for dark theme"""
        style = ttk.Style()
        style.theme_use('clam')

        # Configure colors
        style.configure('TFrame', background=self.colors['bg'])
        style.configure('TLabel', background=self.colors['bg'], foreground=self.colors['fg'])
        style.configure('TButton', background=self.colors['button'], foreground=self.colors['fg'])
        style.map('TButton', background=[('active', self.colors['select'])])

        # Treeview
        style.configure('Treeview', background=self.colors['button'],
                        foreground=self.colors['fg'], fieldbackground=self.colors['button'])
        style.configure('Treeview.Heading', background=self.colors['select'],
                        foreground=self.colors['fg'])
        style.map('Treeview', background=[('selected', self.colors['accent'])])

    def setup_ui(self):
        """Setup main UI"""
        self.root.configure(bg=self.colors['bg'])

        # Main content area
        self.main_frame = ttk.Frame(self.root)
        self.main_frame.pack(fill='both', expand=True)

        # Create all view frames
        self.create_minimal_view()
        self.create_compact_view()
        self.create_detailed_view()

        # Set initial view
        self.update_view_mode()

        # Context menu
        self.create_context_menu()
        self.root.bind('<Button-3>', self.show_context_menu)

    def create_minimal_view(self):
        """Create the minimal League of Legends style view"""
        self.minimal_frame = tk.Frame(self.root, bg='black', padx=8, pady=4)

        # Single frame to hold all elements in one line
        content_frame = tk.Frame(self.minimal_frame, bg='black')
        content_frame.pack()

        # Status dot (left side)
        self.minimal_dot_canvas = tk.Canvas(content_frame, width=12, height=12,
                                            bg='black', highlightthickness=0)
        self.minimal_dot_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self.minimal_dot = self.minimal_dot_canvas.create_oval(2, 2, 10, 10,
                                                               fill='gray', outline='white', width=1)

        # System metrics text (middle)
        self.minimal_metrics_label = tk.Label(content_frame, text="CPU: -- | RAM: --",
                                              fg='white', bg='black',
                                              font=('Arial', 10, 'bold'))
        self.minimal_metrics_label.pack(side=tk.LEFT)

        # Triangle button to expand/cycle view (right side)
        self.minimal_expand_btn = tk.Label(
            content_frame,
            text="▸",  # Triangle pointing right
            font=("Arial", 12, "bold"),
            fg="white",
            bg="black",
            cursor="hand2",
            padx=6, pady=0
        )
        self.minimal_expand_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.minimal_expand_btn.bind("<Button-1>", lambda e: self.cycle_view_mode())

    def create_compact_view(self):
        """Create the compact view with essential metrics"""
        self.compact_frame = ttk.Frame(self.root, padding="10")

        # Menu bar
        self.create_menu()

        # Title bar
        title_frame = ttk.Frame(self.compact_frame)
        title_frame.pack(fill='x', pady=(0, 10))

        title_label = ttk.Label(title_frame, text="ByteDog System Monitor",
            font=('Arial', 10, 'bold'))
        title_label.pack(side='left')

        # View toggle button
        self.toggle_btn = ttk.Button(title_frame, text="▼", width=3,
                                     command=self.cycle_view_mode)
        self.toggle_btn.pack(side='right')

        # Status indicator
        status_frame = ttk.Frame(self.compact_frame)
        status_frame.pack(fill='x', pady=(0, 10))

        self.status_canvas = tk.Canvas(status_frame, width=20, height=20)
        self.status_canvas.pack(side=tk.LEFT)
        self.status_indicator = self.status_canvas.create_oval(2, 2, 18, 18,
                                                               fill='gray', outline='')

        self.status_label = ttk.Label(status_frame, text="Ready")
        self.status_label.pack(side=tk.LEFT, padx=(10, 0))

        # System overview frame
        overview_frame = ttk.Frame(self.compact_frame)
        overview_frame.pack(fill='x', pady=10)

        # Store metric cards for updates
        self.metric_cards = {}

        # CPU card
        cpu_card = self.create_metric_card(overview_frame, "CPU", 0, "%")
        cpu_card.pack(side='left', padx=5)
        self.metric_cards['cpu'] = cpu_card

        # Memory card
        mem_card = self.create_metric_card(overview_frame, "Memory", 0, "%")
        mem_card.pack(side='left', padx=5)
        self.metric_cards['memory'] = mem_card

        # GPU card if available
        if GPU_AVAILABLE:
            gpu_card = self.create_metric_card(overview_frame, "GPU", 0, "%")
            gpu_card.pack(side='left', padx=5)
            self.metric_cards['gpu'] = gpu_card

        # Top processes
        top_frame = ttk.Frame(self.compact_frame)
        top_frame.pack(fill='both', expand=True, pady=10)

        tk.Label(top_frame, text="Top Processes", bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Arial', 12, 'bold')).pack(anchor='w', pady=5)

        # Simple process list for compact view
        self.create_simple_process_list(top_frame)

    def create_detailed_view(self):
        """Show detailed view with all metrics and full process management"""
        self.detailed_frame = ttk.Frame(self.root, padding="10")

        # Title bar
        title_frame = ttk.Frame(self.detailed_frame)
        title_frame.pack(fill='x', pady=(0, 10))

        title_label = ttk.Label(title_frame, text="ByteDog - Detailed View",
                                font=('Arial', 10, 'bold'))
        title_label.pack(side='left')

        # View toggle button
        self.detailed_toggle_btn = ttk.Button(title_frame, text="▲", width=3,
                                              command=self.cycle_view_mode)
        self.detailed_toggle_btn.pack(side='right')

        # Create notebook for tabs
        notebook = ttk.Notebook(self.detailed_frame)
        notebook.pack(fill='both', expand=True)

        # Overview tab
        overview_tab = ttk.Frame(notebook)
        notebook.add(overview_tab, text='Overview')
        self.create_overview_tab(overview_tab)

        # Processes tab
        process_tab = ttk.Frame(notebook)
        notebook.add(process_tab, text='Processes')
        self.create_process_tab(process_tab)

        # Performance tab
        perf_tab = ttk.Frame(notebook)
        notebook.add(perf_tab, text='Performance')
        self.create_performance_tab(perf_tab)

        # Network tab
        network_tab = ttk.Frame(notebook)
        notebook.add(network_tab, text='Network')
        self.create_network_tab(network_tab)

    def create_menu(self):
        """Create menu bar"""
        menubar = tk.Menu(self.root, bg=self.colors['button'], fg=self.colors['fg'])
        self.root.config(menu=menubar)
        self.menubar = menubar  # keep a handle so we can reattach after minimal

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Export Data...", command=self.export_data)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Minimal", command=lambda: self.set_view_mode("minimal"))
        view_menu.add_command(label="Compact", command=lambda: self.set_view_mode("compact"))
        view_menu.add_command(label="Detailed", command=lambda: self.set_view_mode("detailed"))

        # Tools menu
        tools_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Settings", command=self.show_settings)
        tools_menu.add_command(label="Performance Report", command=self.generate_report)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)

    def cycle_view_mode(self):
        """Cycle through view modes: minimal -> compact -> detailed -> minimal"""
        current_mode = self.view_mode.get()
        if current_mode == "minimal":
            self.view_mode.set("compact")
        elif current_mode == "compact":
            self.view_mode.set("detailed")
        else:
            self.view_mode.set("minimal")

        self.update_view_mode()

    def update_view_mode(self):
        """Update the display based on current view mode"""
        mode = self.view_mode.get()

        # Hide all frames first
        for widget in self.root.winfo_children():
            # Do not pack_forget menu; it's not a child widget, so safe regardless
            widget.pack_forget()

        if mode == "minimal":
            # Minimal mode - just metrics, dot, and tiny expand button
            self.minimal_frame.pack(fill='both', expand=True)
            self.root.update_idletasks()

            # Get the actual size needed and resize window (keeps it tiny on each toggle)
            req_width = self.minimal_frame.winfo_reqwidth()
            req_height = self.minimal_frame.winfo_reqheight()
            self.root.geometry(f"{req_width}x{req_height}")

            # Remove window decorations and hide the menubar entirely
            self.root.overrideredirect(True)
            self.root.config(menu="")  # hide menubar in minimal

        elif mode == "compact":
            # Compact mode - essential info with window decorations
            self.root.overrideredirect(False)
            self.compact_frame.pack(fill='both', expand=True)
            self.root.geometry("340x320")
            if hasattr(self, 'toggle_btn'):
                self.toggle_btn.config(text="▼")
            if hasattr(self, 'menubar'):
                self.root.config(menu=self.menubar)  # restore menubar

        else:  # detailed
            # Detailed mode - all information
            self.root.overrideredirect(False)
            self.detailed_frame.pack(fill='both', expand=True)
            self.root.geometry("450x700")
            if hasattr(self, 'detailed_toggle_btn'):
                self.detailed_toggle_btn.config(text="▲")
            if hasattr(self, 'menubar'):
                self.root.config(menu=self.menubar)  # restore menubar

    def create_metric_card(self, parent, title, value, unit):
        """Create a metric display card"""
        card = tk.Frame(parent, bg=self.colors['button'], relief='raised', bd=1)
        card.configure(width=90, height=70)
        card.pack_propagate(False)

        card.title_label = tk.Label(card, text=title, bg=self.colors['button'], fg=self.colors['fg'],
                                    font=('Arial', 9))
        card.title_label.pack(pady=2)

        card.value_label = tk.Label(card, text=f"{value:.0f}{unit}",
                                    bg=self.colors['button'], fg=self.colors['success'],
                                    font=('Arial', 14, 'bold'))
        card.value_label.pack()

        return card

    def update_metric_card(self, card, value, unit="%"):
        """Update a metric card's value"""
        if isinstance(value, (int, float)):
            color = self.colors['success'] if value < 50 else self.colors['warning'] if value < 80 else self.colors['error']
            card.value_label.config(text=f"{value:.0f}{unit}", fg=color)

    def create_simple_process_list(self, parent):
        """Create simple process list for compact view"""
        # Simple text display for top processes
        self.process_display = tk.Text(parent, height=8, bg=self.colors['button'],
                                       fg=self.colors['fg'], font=('Consolas', 8),
                                       state='disabled')
        self.process_display.pack(fill='both', expand=True)

    def create_process_list(self, parent):
        """Create detailed process list view"""
        # Frame for list and scrollbar
        list_frame = ttk.Frame(parent)
        list_frame.pack(fill='both', expand=True)

        # Scrollbar
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side='right', fill='y')

        # Treeview for processes
        columns = ('PID', 'Name', 'CPU %', 'Memory %', 'Status')
        self.process_tree = ttk.Treeview(list_frame, columns=columns, show='headings',
                                         yscrollcommand=scrollbar.set)

        # Configure columns
        for col in columns:
            self.process_tree.heading(col, text=col, command=lambda c=col: self.sort_processes(c))
            if col in ['PID']:
                self.process_tree.column(col, width=80)
            elif col in ['CPU %', 'Memory %']:
                self.process_tree.column(col, width=100)
            elif col == 'Status':
                self.process_tree.column(col, width=100)

        scrollbar.config(command=self.process_tree.yview)
        self.process_tree.pack(fill='both', expand=True)

        # Context menu
        self.process_tree.bind('<Button-3>', self.show_process_menu)

    def create_overview_tab(self, parent):
        """Create overview tab content"""
        # System info
        info_frame = ttk.Frame(parent)
        info_frame.pack(fill='x', padx=20, pady=10)

        system_info = f"System: {platform.system()} {platform.release()}\n"
        system_info += f"Processor: {platform.processor() or 'Unknown'}\n"
        system_info += f"CPU Cores: {psutil.cpu_count(logical=False)} physical, {psutil.cpu_count()} logical\n"

        mem = psutil.virtual_memory()
        system_info += f"Total Memory: {mem.total / (1024 ** 3):.1f} GB\n"

        if GPU_AVAILABLE:
            gpu_info = self.monitor.get_gpu_info()
            if gpu_info:
                system_info += f"GPU: {gpu_info['name']}\n"
                system_info += f"GPU Memory: {gpu_info['memory_total']:.0f} MB"

        tk.Label(info_frame, text=system_info, bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Consolas', 10), justify='left').pack(anchor='w')

        # Current metrics
        metrics_frame = ttk.Frame(parent)
        metrics_frame.pack(fill='both', expand=True, padx=20, pady=10)

        # CPU cores
        tk.Label(metrics_frame, text="CPU Cores Usage:", bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Arial', 11, 'bold')).pack(anchor='w', pady=5)

        self.core_labels = []
        cores_frame = ttk.Frame(metrics_frame)
        cores_frame.pack(fill='x')

        cpu_count = min(psutil.cpu_count(), 16)  # Limit display to 16 cores
        for i in range(cpu_count):
            label = tk.Label(cores_frame, text=f"Core {i}: 0%", bg=self.colors['bg'],
                             fg=self.colors['fg'], font=('Consolas', 9))
            label.grid(row=i // 4, column=i % 4, padx=10, pady=2, sticky='w')
            self.core_labels.append(label)

    def create_process_tab(self, parent):
        """Create process management tab"""
        # Control buttons
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill='x', padx=10, pady=10)

        tk.Button(control_frame, text="🔄 Refresh", bg=self.colors['button'], fg=self.colors['fg'],
                  command=self.refresh_processes).pack(side='left', padx=2)

        tk.Button(control_frame, text="❌ Kill Process", bg=self.colors['button'], fg=self.colors['fg'],
                  command=self.kill_selected_process).pack(side='left', padx=2)

        tk.Button(control_frame, text="⏸ Suspend", bg=self.colors['button'], fg=self.colors['fg'],
                  command=self.suspend_selected_process).pack(side='left', padx=2)

        tk.Button(control_frame, text="▶ Resume", bg=self.colors['button'], fg=self.colors['fg'],
                  command=self.resume_selected_process).pack(side='left', padx=2)

        # Search box
        tk.Label(control_frame, text="Search:", bg=self.colors['bg'], fg=self.colors['fg']).pack(side='left',
                                                                                                 padx=(20, 5))
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *args: self.filter_processes())
        search_entry = tk.Entry(control_frame, textvariable=self.search_var, bg=self.colors['button'],
                                fg=self.colors['fg'], insertbackground=self.colors['fg'])
        search_entry.pack(side='left')

        # Full process list
        self.create_process_list(parent)

    def create_performance_tab(self, parent):
        """Create performance graphs tab"""
        tk.Label(parent, text="Performance History", bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Arial', 14, 'bold')).pack(pady=10)

        # Simple text-based graph
        self.perf_text = tk.Text(parent, bg=self.colors['button'], fg=self.colors['fg'],
                                 font=('Consolas', 9), height=20)
        self.perf_text.pack(fill='both', expand=True, padx=20, pady=10)

        self.update_performance_graph()

    def create_network_tab(self, parent):
        """Create network monitoring tab"""
        tk.Label(parent, text="Network Statistics", bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Arial', 14, 'bold')).pack(pady=10)

        self.net_info_label = tk.Label(parent, text="", bg=self.colors['bg'], fg=self.colors['fg'],
                                       font=('Consolas', 10), justify='left')
        self.net_info_label.pack(pady=20)

        self.update_network_info()

    def create_context_menu(self):
        """Create right-click context menu"""
        self.context_menu = tk.Menu(self.root, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        self.context_menu.add_command(label="Minimal View", command=lambda: self.set_view_mode("minimal"))
        self.context_menu.add_command(label="Compact View", command=lambda: self.set_view_mode("compact"))
        self.context_menu.add_command(label="Detailed View", command=lambda: self.set_view_mode("detailed"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Always on Top", command=self.toggle_topmost)
        self.context_menu.add_command(label="Mini Window", command=self.show_minimal_view)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Exit", command=self.root.quit)

    def set_view_mode(self, mode):
        """Set specific view mode"""
        self.view_mode.set(mode)
        self.update_view_mode()

    def show_context_menu(self, event):
        """Show context menu"""
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def toggle_topmost(self):
        """Toggle always on top"""
        current = self.root.attributes('-topmost')
        self.root.attributes('-topmost', not current)

    def show_minimal_view(self):
        """Show minimal overlay view"""
        if self.minimal_window and self.minimal_window.winfo_exists():
            self.minimal_window.lift()
        else:
            self.minimal_window = MinimalView(self.root, self.monitor)

    def sort_processes(self, column):
        """Sort process list by column"""
        col_map = {'PID': 'pid', 'Name': 'name', 'CPU %': 'cpu_percent',
                   'Memory %': 'memory_percent', 'Status': 'status'}

        if column in col_map:
            if self.sort_column == col_map[column]:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_column = col_map[column]
                self.sort_reverse = True

            self.update_process_list()

    def filter_processes(self):
        """Filter processes based on search"""
        if hasattr(self, 'process_tree'):
            self.update_process_list()

    def refresh_processes(self):
        """Force refresh process list"""
        self.monitor.process_cache = []
        if hasattr(self, 'process_tree'):
            self.update_process_list()

    def show_process_menu(self, event):
        """Show context menu for process"""
        item = self.process_tree.identify('item', event.x, event.y)
        if item:
            self.process_tree.selection_set(item)

            menu = tk.Menu(self.root, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
            menu.add_command(label="Kill Process", command=self.kill_selected_process)
            menu.add_command(label="Suspend Process", command=self.suspend_selected_process)
            menu.add_command(label="Resume Process", command=self.resume_selected_process)
            menu.add_separator()
            menu.add_command(label="Process Details", command=self.show_process_details)

            menu.post(event.x_root, event.y_root)

    def kill_selected_process(self):
        """Kill selected process"""
        if hasattr(self, 'process_tree'):
            selection = self.process_tree.selection()
            if selection:
                item = self.process_tree.item(selection[0])
                pid = item['values'][0]
                name = item['values'][1]

                if messagebox.askyesno("Confirm", f"Kill process '{name}' (PID: {pid})?"):
                    if self.process_manager.kill_process(pid):
                        self.status_label.config(text=f"Killed process {pid}", fg=self.colors['success'])
                    else:
                        self.status_label.config(text=f"Failed to kill process {pid}", fg=self.colors['error'])
                    self.refresh_processes()

    def suspend_selected_process(self):
        """Suspend selected process"""
        if hasattr(self, 'process_tree'):
            selection = self.process_tree.selection()
            if selection:
                item = self.process_tree.item(selection[0])
                pid = item['values'][0]

                if self.process_manager.suspend_process(pid):
                    self.status_label.config(text=f"Suspended process {pid}", fg=self.colors['success'])
                else:
                    self.status_label.config(text=f"Failed to suspend process {pid}", fg=self.colors['error'])
                self.refresh_processes()

    def resume_selected_process(self):
        """Resume selected process"""
        if hasattr(self, 'process_tree'):
            selection = self.process_tree.selection()
            if selection:
                item = self.process_tree.item(selection[0])
                pid = item['values'][0]

                if self.process_manager.resume_process(pid):
                    self.status_label.config(text=f"Resumed process {pid}", fg=self.colors['success'])
                else:
                    self.status_label.config(text=f"Failed to resume process {pid}", fg=self.colors['error'])
                self.refresh_processes()

    def show_process_details(self):
        """Show detailed process information"""
        if hasattr(self, 'process_tree'):
            selection = self.process_tree.selection()
            if selection:
                item = self.process_tree.item(selection[0])
                pid = item['values'][0]

                try:
                    proc = psutil.Process(pid)
                    info = f"Process Details\n" + "=" * 50 + "\n"
                    info += f"PID: {pid}\n"
                    info += f"Name: {proc.name()}\n"
                    info += f"Status: {proc.status()}\n"
                    info += f"Created: {datetime.fromtimestamp(proc.create_time())}\n"
                    info += f"Memory %: {proc.memory_percent():.2f}\n"
                    info += f"Threads: {proc.num_threads()}\n"

                    try:
                        info += f"Path: {proc.exe()}\n"
                    except:
                        pass

                    messagebox.showinfo("Process Details", info)
                except:
                    messagebox.showerror("Error", "Could not retrieve process details")

    def update_process_list(self):
        """Update the process list display"""
        if not hasattr(self, 'process_tree'):
            return

        # Clear existing items
        for item in self.process_tree.get_children():
            self.process_tree.delete(item)

        # Get processes (use cache for better performance)
        processes = self.monitor.get_process_list(use_cache=True)

        # Apply search filter
        search_term = self.search_var.get().lower() if hasattr(self, 'search_var') else ""
        if search_term:
            processes = [p for p in processes if search_term in p['name'].lower()]

        # Sort processes
        processes.sort(key=lambda x: x.get(self.sort_column, 0) or 0, reverse=self.sort_reverse)

        # Add to tree (limit to top 100 for performance)
        for proc in processes[:100]:
            self.process_tree.insert('', 'end', values=(
                proc['pid'],
                proc['name'][:30],
                f"{proc.get('cpu_percent', 0):.1f}",
                f"{proc.get('memory_percent', 0):.1f}",
                proc['status']
            ))

    def update_simple_process_display(self):
        """Update simple process display for compact view"""
        if not hasattr(self, 'process_display'):
            return

        processes = self.monitor.get_process_list(use_cache=True)

        # Clear and update
        self.process_display.config(state='normal')
        self.process_display.delete(1.0, tk.END)

        text = "TOP PROCESSES (by Memory)\n"
        text += "-" * 30 + "\n"

        for i, proc in enumerate(processes[:8]):  # Top 8 processes
            name = proc['name'][:15] if len(proc['name']) > 15 else proc['name']
            mem_pct = proc.get('memory_percent', 0)
            text += f"{name:<15} {mem_pct:>6.1f}%\n"

        self.process_display.insert(1.0, text)
        self.process_display.config(state='disabled')

    def update_metrics(self):
        """Update all metrics displays"""
        try:
            # Get current data
            cpu = self.monitor.get_cpu_usage()
            mem = self.monitor.get_memory_info()
            gpu_info = self.monitor.get_gpu_info() if GPU_AVAILABLE else None

            # Update minimal view
            if self.view_mode.get() == "minimal":
                # Update minimal display
                metrics_text = f"CPU: {cpu:.0f}% | RAM: {mem['percent']:.0f}%"
                if GPU_AVAILABLE and gpu_info:
                    metrics_text += f" | GPU: {gpu_info['load']:.0f}%"

                self.minimal_metrics_label.config(text=metrics_text)

                # Update status dot color based on overall system load
                max_usage = max(cpu, mem['percent'])
                if GPU_AVAILABLE and gpu_info:
                    max_usage = max(max_usage, gpu_info['load'])

                if max_usage < 50:
                    dot_color = 'lime'
                elif max_usage < 80:
                    dot_color = 'yellow'
                else:
                    dot_color = 'red'

                self.minimal_dot_canvas.itemconfig(self.minimal_dot, fill=dot_color)

            # Update compact view metric cards
            elif self.view_mode.get() == "compact":
                if 'cpu' in self.metric_cards:
                    self.update_metric_card(self.metric_cards['cpu'], cpu)

                if 'memory' in self.metric_cards:
                    self.update_metric_card(self.metric_cards['memory'], mem['percent'])

                if GPU_AVAILABLE and 'gpu' in self.metric_cards and gpu_info:
                    self.update_metric_card(self.metric_cards['gpu'], gpu_info['load'])

                # Update simple process display
                self.update_simple_process_display()

                # Update status indicator
                overall_status = self.calculate_overall_status(cpu, mem['percent'], gpu_info)
                status_colors = {'good': 'green', 'fair': 'orange', 'poor': 'red'}
                if hasattr(self, 'status_canvas'):
                    self.status_canvas.itemconfig(self.status_indicator,
                                                  fill=status_colors.get(overall_status, 'gray'))
                    self.status_label.config(text=f"Status: {overall_status.title()}")

            # Update detailed view
            elif self.view_mode.get() == "detailed":
                # Update CPU cores if visible
                if hasattr(self, 'core_labels'):
                    cores = self.monitor.get_cpu_per_core()
                    for i, label in enumerate(self.core_labels):
                        if i < len(cores):
                            usage = cores[i]
                            color = self.colors['success'] if usage < 50 else self.colors['warning'] if usage < 80 else self.colors['error']
                            label.config(text=f"Core {i}: {usage:.1f}%", fg=color)

                # Update process list if visible
                if hasattr(self, 'process_tree'):
                    self.update_process_list()

        except Exception as e:
            print(f"Metrics update error: {e}")

    def calculate_overall_status(self, cpu, mem_percent, gpu_info):
        """Calculate overall system status"""
        poor_conditions = 0
        total_conditions = 2  # CPU and Memory

        if cpu > 80:
            poor_conditions += 1
        if mem_percent > 80:
            poor_conditions += 1

        if GPU_AVAILABLE and gpu_info:
            total_conditions += 1
            if gpu_info['load'] > 80:
                poor_conditions += 1

        if poor_conditions == 0:
            return 'good'
        elif poor_conditions < total_conditions:
            return 'fair'
        else:
            return 'poor'

    def update_performance_graph(self):
        """Update performance history graph"""
        if not hasattr(self, 'perf_text'):
            return

        # Add current values to history
        cpu = self.monitor.get_cpu_usage()
        mem = self.monitor.get_memory_info()

        self.monitor.cpu_history.append(cpu)
        self.monitor.ram_history.append(mem['percent'])

        if GPU_AVAILABLE:
            gpu_info = self.monitor.get_gpu_info()
            if gpu_info and self.monitor.gpu_history is not None:
                self.monitor.gpu_history.append(gpu_info['load'])

        # Create text-based graph
        graph_height = 15
        graph_width = 60

        # Clear text
        self.perf_text.delete(1.0, tk.END)

        # CPU Graph
        self.perf_text.insert(tk.END, "CPU Usage History\n", 'title')
        self.perf_text.insert(tk.END, self.create_text_graph(self.monitor.cpu_history, graph_height, graph_width))
        self.perf_text.insert(tk.END, "\n\n")

        # RAM Graph
        self.perf_text.insert(tk.END, "Memory Usage History\n", 'title')
        self.perf_text.insert(tk.END, self.create_text_graph(self.monitor.ram_history, graph_height, graph_width))

        # GPU Graph if available
        if GPU_AVAILABLE and self.monitor.gpu_history:
            self.perf_text.insert(tk.END, "\n\n")
            self.perf_text.insert(tk.END, "GPU Usage History\n", 'title')
            self.perf_text.insert(tk.END, self.create_text_graph(self.monitor.gpu_history, graph_height, graph_width))

        # Configure text tags
        self.perf_text.tag_config('title', foreground=self.colors['accent'], font=('Arial', 11, 'bold'))

        # Schedule next update
        if self.view_mode.get() == 'detailed':
            self.root.after(2000, self.update_performance_graph)

    def create_text_graph(self, data, height, width):
        """Create ASCII graph from data"""
        if not data:
            return "No data available\n"

        graph = []

        # Filter out None values
        valid_data = [v for v in data if v is not None]
        if len(valid_data) < 2:
            return "Collecting data...\n"

        # Scale data to fit height
        max_val = max(valid_data)
        min_val = min(valid_data)

        if max_val == min_val:
            max_val += 1

        # Create graph lines
        for h in range(height, -1, -1):
            threshold = (h / height) * max_val
            line = f"{threshold:3.0f}% |"

            for i, val in enumerate(list(valid_data)[-width:]):
                if val >= threshold:
                    line += "█"
                else:
                    line += " "

            graph.append(line + "\n")

        # Add bottom axis
        graph.append("     +" + "-" * min(len(valid_data), width) + "\n")
        graph.append("      " + "".join(
            [str(i % 10) if i % 10 == 0 else " " for i in range(min(len(valid_data), width))]) + "\n")

        return "".join(graph)

    def update_network_info(self):
        """Update network information display"""
        if not hasattr(self, 'net_info_label'):
            return

        net = self.monitor.get_network_info()

        info = f"Network Statistics\n" + "=" * 50 + "\n"
        info += f"Bytes Sent: {self.format_bytes(net['bytes_sent'])}\n"
        info += f"Bytes Received: {self.format_bytes(net['bytes_recv'])}\n"
        info += f"Packets Sent: {net['packets_sent']:,}\n"
        info += f"Packets Received: {net['packets_recv']:,}\n"

        # Get network interfaces
        info += "\n" + "Network Interfaces\n" + "-" * 30 + "\n"
        try:
            for interface, addrs in psutil.net_if_addrs().items():
                info += f"\n{interface}:\n"
                for addr in addrs:
                    if addr.family == 2:  # IPv4
                        info += f"  IPv4: {addr.address}\n"
        except:
            info += "  Unable to retrieve interface details\n"

        self.net_info_label.config(text=info)

        # Schedule next update
        if self.view_mode.get() == 'detailed':
            self.root.after(2000, self.update_network_info)

    def start_monitoring(self):
        """Start the monitoring thread"""

        def monitor_loop():
            while True:
                try:
                    # Collect data in background
                    data = {
                        'cpu': self.monitor.get_cpu_usage(),
                        'memory': self.monitor.get_memory_info(),
                        'processes': self.monitor.get_process_list(use_cache=False)
                    }

                    if GPU_AVAILABLE:
                        data['gpu'] = self.monitor.get_gpu_info()

                    # Queue data for UI update
                    self.data_queue.put(data)
                except Exception as e:
                    print(f"Monitoring error: {e}")

                time.sleep(self.monitor.update_interval)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

    def process_queue(self):
        """Process data from monitoring thread"""
        try:
            while True:
                data = self.data_queue.get_nowait()
                # Update metrics based on current view
                self.update_metrics()
        except queue.Empty:
            pass

        # Schedule next check
        self.root.after(1000, self.process_queue)

    def export_data(self):
        """Export system data to file"""
        from tkinter import filedialog

        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("CSV files", "*.csv"), ("All files", "*.*")]
        )

        if filename:
            data = {
                'timestamp': datetime.now().isoformat(),
                'system': {
                    'platform': platform.system(),
                    'release': platform.release(),
                    'processor': platform.processor() or 'Unknown'
                },
                'cpu': {
                    'usage': self.monitor.get_cpu_usage(),
                    'per_core': self.monitor.get_cpu_per_core(),
                    'count': psutil.cpu_count()
                },
                'memory': self.monitor.get_memory_info(),
                'disk': self.monitor.get_disk_info(),
                'network': self.monitor.get_network_info(),
                'processes': self.monitor.get_process_list()[:50]  # Top 50 processes
            }

            if GPU_AVAILABLE:
                data['gpu'] = self.monitor.get_gpu_info()

            try:
                if filename.endswith('.csv'):
                    # Export as CSV (simplified)
                    import csv
                    with open(filename, 'w', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(['Metric', 'Value'])
                        writer.writerow(['Timestamp', data['timestamp']])
                        writer.writerow(['CPU Usage', f"{data['cpu']['usage']:.1f}%"])
                        writer.writerow(['Memory Usage', f"{data['memory']['percent']:.1f}%"])
                        writer.writerow(['Process Count', len(data['processes'])])
                else:
                    # Export as JSON
                    with open(filename, 'w') as f:
                        json.dump(data, f, indent=2, default=str)

                if hasattr(self, 'status_label'):
                    self.status_label.config(text=f"Data exported to {os.path.basename(filename)}")
            except Exception as e:
                messagebox.showerror("Export Error", f"Failed to export data: {str(e)}")

    def generate_report(self):
        """Generate performance report"""
        report = "ByteDog Performance Report\n" + "=" * 50 + "\n"
        report += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        # System Info
        report += "System Information\n" + "-" * 30 + "\n"
        report += f"Platform: {platform.system()} {platform.release()}\n"
        report += f"Processor: {platform.processor() or 'Unknown'}\n"
        report += f"CPU Cores: {psutil.cpu_count(logical=False)} physical, {psutil.cpu_count()} logical\n\n"

        # Current Status
        report += "Current Status\n" + "-" * 30 + "\n"
        report += f"CPU Usage: {self.monitor.get_cpu_usage():.1f}%\n"

        mem = self.monitor.get_memory_info()
        report += f"Memory Usage: {mem['percent']:.1f}% ({self.format_bytes(mem['used'])} / {self.format_bytes(mem['total'])})\n"

        if GPU_AVAILABLE:
            gpu = self.monitor.get_gpu_info()
            if gpu:
                report += f"GPU Usage: {gpu['load']:.1f}%\n"

        # Top Processes
        report += "\nTop 10 Memory Consuming Processes\n" + "-" * 30 + "\n"
        processes = self.monitor.get_process_list()[:10]
        for proc in processes:
            report += f"{proc['name'][:30]:30} PID: {proc['pid']:7} MEM: {proc.get('memory_percent', 0):5.1f}%\n"

        # Show report
        report_window = tk.Toplevel(self.root)
        report_window.title("Performance Report")
        report_window.geometry("600x500")
        report_window.configure(bg=self.colors['bg'])

        # Window icon for Windows
        if platform.system() == "Windows":
            try:
                report_window.iconbitmap(resource_path("ByteDog_256.ico"))
            except Exception:
                pass

        text = tk.Text(report_window, bg=self.colors['button'], fg=self.colors['fg'],
                       font=('Consolas', 10))
        text.pack(fill='both', expand=True, padx=10, pady=10)
        text.insert(1.0, report)
        text.config(state='disabled')

        # Save button
        tk.Button(report_window, text="Save Report", bg=self.colors['button'], fg=self.colors['fg'],
                  command=lambda: self.save_report(report)).pack(pady=5)

    def save_report(self, report):
        """Save report to file"""
        from tkinter import filedialog

        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if filename:
            try:
                with open(filename, 'w') as f:
                    f.write(report)
                if hasattr(self, 'status_label'):
                    self.status_label.config(text=f"Report saved to {os.path.basename(filename)}")
            except Exception as e:
                messagebox.showerror("Save Error", f"Failed to save report: {str(e)}")

    def show_settings(self):
        """Show settings dialog"""
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Settings")
        settings_window.geometry("400x300")
        settings_window.configure(bg=self.colors['bg'])

        # Window icon for Windows
        if platform.system() == "Windows":
            try:
                settings_window.iconbitmap(resource_path("ByteDog_256.ico"))
            except Exception:
                pass

        # Update interval
        tk.Label(settings_window, text="Update Interval (seconds):", bg=self.colors['bg'],
                 fg=self.colors['fg']).pack(pady=10)

        interval_var = tk.DoubleVar(value=self.monitor.update_interval)
        interval_scale = tk.Scale(settings_window, from_=1.0, to=10, resolution=0.5,
                                  orient='horizontal', variable=interval_var,
                                  bg=self.colors['bg'], fg=self.colors['fg'])
        interval_scale.pack(pady=5)

        # Always on top option
        always_top_var = tk.BooleanVar()
        tk.Checkbutton(settings_window, text="Always on top",
                       variable=always_top_var, bg=self.colors['bg'], fg=self.colors['fg'],
                       selectcolor=self.colors['button']).pack(pady=10)

        # Save button
        def save_settings():
            self.monitor.update_interval = interval_var.get()
            if always_top_var.get():
                self.root.attributes('-topmost', True)
            settings_window.destroy()
            if hasattr(self, 'status_label'):
                self.status_label.config(text="Settings saved")

        tk.Button(settings_window, text="Save", bg=self.colors['button'], fg=self.colors['fg'],
                  command=save_settings).pack(pady=20)

    def show_about(self):
        """Show about dialog"""
        about_text = """ByteDog 🐕
System Resource Monitor v1.0

A lightweight, real-time system resource monitor
Part of the Dog family of utilities

Features:
• Minimal resource usage (<50MB RAM)
• Real-time CPU, Memory, GPU monitoring
• Process management capabilities
• Multiple view modes (NetDog-style toggling)
• Cross-platform support

Created with Python and psutil
© 2025 - Following the Dog family tradition"""

        messagebox.showinfo("About ByteDog", about_text)

    def format_bytes(self, bytes_val):
        """Format bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.2f} PB"

    def run(self):
        """Start the application"""
        # Start metric updates
        self.update_metrics()
        self.root.after(2000, self.schedule_metric_updates)

        # Handle window closing
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.mainloop()

    def schedule_metric_updates(self):
        """Schedule regular metric updates"""
        self.update_metrics()
        self.root.after(2000, self.schedule_metric_updates)

    def on_closing(self):
        """Handle application closing"""
        self.root.quit()
        self.root.destroy()


def main():
    """Main entry point"""
    # Check for admin privileges on Windows
    if platform.system() == 'Windows':
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("Note: Some features may require administrator privileges")
        except:
            pass

    # Check if GPU monitoring is available
    if GPU_AVAILABLE:
        print("✅ GPU monitoring available")
    else:
        print("⚠️  GPU monitoring not available (install: pip install gputil)")

    app = ByteDogApp()
    app.run()


if __name__ == "__main__":
    main()
