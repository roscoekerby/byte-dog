#!/usr/bin/env python3
"""
ByteDog - Lightweight System Resource Monitor
A minimal-resource system monitor following the Dog family design principles
Complete version with NetDog-style view toggling
FIXED: Arrow visibility in minimal view
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
import sys
import queue

from guardian import (
    DEFAULT_PROTECTED, GuardianConfig, EscalationEngine,
    fast_memory_snapshot, enrich_chromium, select_targets, group_by_name,
    harden_self, install_autostart, uninstall_autostart, autostart_installed,
)

# GPU backend: NVML via nvidia-ml-py + PDH per-process VRAM (both in-process,
# no subprocess — safe under pythonw, no console flashes)
import gpu as gpu_backend

GPU_AVAILABLE = gpu_backend.gpu_available()

# Hide console window on Windows when running as EXE
if platform.system() == 'Windows' and getattr(sys, 'frozen', False):
    import ctypes

    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

# Without an explicit AppUserModelID, Windows groups the window under the
# python.exe host process and shows the Python icon in the taskbar.
if platform.system() == 'Windows':
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'ROSCODETech.ByteDog')
    except Exception:
        pass


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
        self._total_ram = psutil.virtual_memory().total

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
        return gpu_backend.get_gpu_info()

    def get_process_list(self, use_cache=False):
        """Get list of running processes — pid+name only (fast).
        memory_percent and status are NOT fetched here; they take 4-17s on
        machines with security software intercepting handle opens.
        Call get_process_memory() separately, on demand."""
        if use_cache and self.process_cache and (time.time() - self.last_process_update < 30):
            return self.process_cache

        processes = []
        for proc in psutil.process_iter(['pid', 'name'], ad_value=None):
            try:
                pinfo = proc.info
                if pinfo.get('name') is None:
                    continue
                pinfo['cpu_percent'] = 0.0
                pinfo['memory_percent'] = 0.0
                pinfo['memory_bytes'] = 0
                pinfo['gpu_mb'] = 0.0
                pinfo['status'] = '—'
                processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue

        self.process_cache = processes  # unsorted until memory scan runs
        self.last_process_update = time.time()
        return self.process_cache

    def scan_process_memory(self):
        """Fetch memory_percent for all cached processes.
        Slow (~4s on restricted machines). Call in a background thread only."""
        total = self._total_ram
        gpu_vram = gpu_backend.get_process_vram() if GPU_AVAILABLE else {}
        enriched = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_percent'], ad_value=0):
            try:
                pinfo = proc.info
                if pinfo.get('name') is None:
                    continue
                pinfo['cpu_percent'] = 0.0
                pinfo['memory_bytes'] = int((pinfo.get('memory_percent') or 0) / 100.0 * total)
                pinfo['gpu_mb'] = gpu_vram.get(pinfo['pid'], 0.0)
                pinfo['status'] = '—'
                enriched.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        self.process_cache = sorted(enriched, key=lambda x: x.get('memory_percent') or 0, reverse=True)
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


class RAMGuardian:
    """Proactive RAM pressure monitor — escalating thrash prevention.
    Decision logic lives in guardian.EscalationEngine; this class holds
    runtime state (event log, suspended pids, leak tracking)."""

    PROTECTED = DEFAULT_PROTECTED

    def __init__(self, config=None):
        self.config = config or GuardianConfig.load()
        self.engine = EscalationEngine(self.config)
        self.enabled = True
        self.event_log = deque(maxlen=100)
        self.suspended = {}                  # pid -> name (auto-suspended)
        self.process_memory_history = {}     # pid -> deque of (timestamp, rss_bytes)
        self._lock = threading.Lock()
        self.total_ram = psutil.virtual_memory().total

    def save_config(self):
        try:
            self.config.save()
        except OSError as e:
            self.log_event('warn', f"Config save failed: {e}")

    def log_event(self, level, message):
        """Record an event. level: 'info', 'warn', 'critical', 'action'"""
        entry = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'level': level,
            'message': message,
        }
        with self._lock:
            self.event_log.appendleft(entry)
        return entry

    def get_top_hogs(self, processes, n=5):
        """Top N non-protected processes by memory %"""
        protected = self.config.protected_names()
        unprotected = [
            p for p in processes
            if p.get('name', '').lower() not in protected
            and (p.get('memory_percent') or 0) > 0.05
        ]
        return sorted(unprotected, key=lambda x: x.get('memory_percent') or 0, reverse=True)[:n]

    def track_memory_growth(self, processes):
        """Track per-process RSS over time for leak detection"""
        now = time.time()
        for p in processes:
            pid = p.get('pid')
            rss = p.get('rss') or p.get('memory_bytes') or 0
            if not pid or not rss:
                continue
            if pid not in self.process_memory_history:
                self.process_memory_history[pid] = deque(maxlen=30)
            self.process_memory_history[pid].append((now, rss))
        # Remove dead processes
        alive = {p.get('pid') for p in processes}
        for dead in list(self.process_memory_history):
            if dead not in alive:
                del self.process_memory_history[dead]

    def get_leak_suspects(self, processes):
        """Return processes growing faster than 50 MB/min"""
        suspects = []
        for p in processes:
            pid = p.get('pid')
            if not pid or pid not in self.process_memory_history:
                continue
            hist = list(self.process_memory_history[pid])
            if len(hist) < 4:
                continue
            t0, m0 = hist[0]
            t1, m1 = hist[-1]
            elapsed = t1 - t0
            if elapsed < 10:
                continue
            growth_mb_min = ((m1 - m0) / elapsed * 60) / (1024 * 1024)
            if growth_mb_min > 50:
                suspects.append({**p, 'growth_mb_min': growth_mb_min})
        return sorted(suspects, key=lambda x: x['growth_mb_min'], reverse=True)

    def check_ram(self, mem_info):
        """Check RAM pressure using only virtual_memory() — instant, no process scanning.
        Returns a guardian event dict when the escalation engine decides to act."""
        self.engine.enabled = self.enabled
        decision = self.engine.evaluate(
            mem_info['percent'], mem_info.get('swap_used', 0), time.time())
        if decision is None:
            return None

        ram_pct = mem_info['percent']
        used_gb = mem_info['used'] / (1024 ** 3)
        total_gb = mem_info['total'] / (1024 ** 3)

        level = 'critical' if decision.real_tier in ('suspend', 'kill') else 'warn'
        event = self.log_event(
            level, f"{decision.reason} ({used_gb:.1f}/{total_gb:.0f} GB) -> {decision.tier}")
        return {
            'type': decision.tier,          # 'warn' | 'suspend' | 'kill'
            'real_tier': decision.real_tier,
            'reason': decision.reason,
            'ram_pct': ram_pct,
            'used_gb': used_gb,
            'total_gb': total_gb,
            'event': event,
        }


class MinimalView(tk.Toplevel):
    """Minimal overlay view showing only essential metrics"""

    def __init__(self, parent, monitor):
        super().__init__(parent)
        self.monitor = monitor
        self.parent = parent

        self.title("ByteDog Mini")
        self.geometry("200x105" if GPU_AVAILABLE else "200x80")
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

        if GPU_AVAILABLE:
            self.gpu_label = tk.Label(self, text="GPU: 0%", fg='#00ff00', bg='#1e1e1e',
                                      font=('Consolas', 11))
            self.gpu_label.pack()

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

            if GPU_AVAILABLE and hasattr(self, 'gpu_label'):
                gpu_info = self.monitor.get_gpu_info()
                if gpu_info:
                    load = gpu_info['load']
                    gpu_color = '#00ff00' if load < 50 else '#ffff00' if load < 80 else '#ff0000'
                    self.gpu_label.config(text=f"GPU: {load:.1f}%", fg=gpu_color)
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
        self.guardian = RAMGuardian()
        self.guardian_queue = queue.Queue()
        self.guardian_alert_window = None

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
        """Create the minimal League of Legends style view - FIXED ARROW VISIBILITY"""
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

        # FIXED: More visible arrow button with better styling
        self.minimal_expand_btn = tk.Label(
            content_frame,
            text="►",  # Using solid right-pointing triangle
            font=("Arial", 14, "bold"),  # Larger font
            fg="#00ff00",  # Bright green color for visibility
            bg="black",
            cursor="hand2",
            padx=8, pady=2,
            relief="raised",  # Add some visual depth
            bd=1  # Border for better visibility
        )
        self.minimal_expand_btn.pack(side=tk.LEFT, padx=(6, 0))

        # FIXED: Add hover effects to make the button more interactive
        def on_enter(e):
            self.minimal_expand_btn.config(fg="#ffff00", relief="raised", bd=2)  # Yellow on hover

        def on_leave(e):
            self.minimal_expand_btn.config(fg="#00ff00", relief="raised", bd=1)  # Back to green

        def on_click(e):
            self.minimal_expand_btn.config(relief="sunken")  # Visual feedback
            self.root.after(100, lambda: self.minimal_expand_btn.config(relief="raised"))
            self.cycle_view_mode()

        self.minimal_expand_btn.bind("<Enter>", on_enter)
        self.minimal_expand_btn.bind("<Leave>", on_leave)
        self.minimal_expand_btn.bind("<Button-1>", on_click)

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

        # Guardian status badge (right side)
        self.guardian_compact_label = tk.Label(status_frame, text="Shield: --",
                                               bg=self.colors['bg'], fg=self.colors['success'],
                                               font=('Consolas', 8))
        self.guardian_compact_label.pack(side=tk.RIGHT, padx=(5, 0))

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

        # Guardian tab
        guardian_tab = ttk.Frame(notebook)
        notebook.add(guardian_tab, text='Guardian')
        self.create_guardian_tab(guardian_tab)

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
        tools_menu.add_separator()
        tools_menu.add_command(label="Toggle RAM Guardian", command=self._menu_toggle_guardian)
        tools_menu.add_command(label="Resume Suspended Processes", command=self._resume_all_suspended)
        tools_menu.add_separator()
        tools_menu.add_command(label="Install Auto-Start (login)", command=self._menu_install_autostart)
        tools_menu.add_command(label="Remove Auto-Start", command=self._menu_uninstall_autostart)

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
            self.root.geometry("340x345")
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
            # Restart guardian tab and performance graph refresh loops
            self.root.after(200, self.update_guardian_tab)
            self.root.after(200, self.update_performance_graph)

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
            color = self.colors['success'] if value < 50 else self.colors['warning'] if value < 80 else self.colors[
                'error']
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
        columns = ('PID', 'Name', 'CPU %', 'Memory %', 'GPU MB', 'Status')
        self.process_tree = ttk.Treeview(list_frame, columns=columns, show='headings',
                                         yscrollcommand=scrollbar.set)

        # Configure columns
        for col in columns:
            self.process_tree.heading(col, text=col, command=lambda c=col: self.sort_processes(c))
            if col in ['PID']:
                self.process_tree.column(col, width=80)
            elif col in ['CPU %', 'Memory %', 'GPU MB']:
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
                   'Memory %': 'memory_percent', 'GPU MB': 'gpu_mb', 'Status': 'status'}

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
        """Trigger async process memory scan then refresh the list."""
        if hasattr(self, 'status_label'):
            self.status_label.config(text="Scanning processes...", fg=self.colors['warning'])
        def _done(procs):
            self.update_process_list()
            if hasattr(self, 'status_label'):
                self.status_label.config(text=f"Found {len(procs)} processes", fg=self.colors['success'])
        self.trigger_process_scan(callback=_done)

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
                f"{proc.get('gpu_mb', 0):.0f}",
                proc['status']
            ))

    def update_simple_process_display(self):
        """Update simple process display for compact view"""
        if not hasattr(self, 'process_display'):
            return

        processes = self.monitor.get_process_list(use_cache=True)

        self.process_display.config(state='normal')
        self.process_display.delete(1.0, tk.END)

        has_memory = any(p.get('memory_percent', 0) > 0 for p in processes)

        if not processes:
            self.process_display.insert(1.0, "  Click Refresh to scan processes")
        elif not has_memory:
            self.process_display.insert(1.0, "TOP PROCESSES (click Refresh for memory)\n")
            self.process_display.insert(tk.END, "-" * 30 + "\n")
            for proc in processes[:8]:
                name = proc['name'][:28]
                self.process_display.insert(tk.END, f"  {name}\n")
        else:
            self.process_display.insert(1.0, "TOP PROCESSES (by Memory)\n")
            self.process_display.insert(tk.END, "-" * 30 + "\n")
            for proc in processes[:8]:
                name = proc['name'][:15]
                mem_pct = proc.get('memory_percent', 0)
                self.process_display.insert(tk.END, f"{name:<15} {mem_pct:>6.1f}%\n")

        self.process_display.config(state='disabled')

    def update_metrics(self):
        """Update all metrics displays"""
        try:
            # Get current data
            cpu = self.monitor.get_cpu_usage()
            mem = self.monitor.get_memory_info()
            gpu_info = self.monitor.get_gpu_info() if GPU_AVAILABLE else None

            # Accumulate history in every view mode so the Performance tab
            # is already populated when opened
            self.monitor.cpu_history.append(cpu)
            self.monitor.ram_history.append(mem['percent'])
            if gpu_info and self.monitor.gpu_history is not None:
                self.monitor.gpu_history.append(gpu_info['load'])

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

                # Update guardian compact badge
                if hasattr(self, 'guardian_compact_label'):
                    ram_pct = mem['percent']
                    cfg = self.guardian.config
                    if not self.guardian.enabled:
                        g_text = "Shield: OFF"
                        g_color = '#666666'
                    elif ram_pct >= cfg.act_pct:
                        g_text = f"Shield: {ram_pct:.0f}%!! ALERT"
                        g_color = self.colors['error']
                    elif ram_pct >= cfg.warn_pct:
                        g_text = f"Shield: {ram_pct:.0f}% WARN"
                        g_color = self.colors['warning']
                    else:
                        g_text = f"Shield: {ram_pct:.0f}/{cfg.warn_pct:.0f}% OK"
                        g_color = self.colors['success']
                    self.guardian_compact_label.config(text=g_text, fg=g_color)

            # Update detailed view
            elif self.view_mode.get() == "detailed":
                # Update CPU cores if visible
                if hasattr(self, 'core_labels'):
                    cores = self.monitor.get_cpu_per_core()
                    for i, label in enumerate(self.core_labels):
                        if i < len(cores):
                            usage = cores[i]
                            color = self.colors['success'] if usage < 50 else self.colors['warning'] if usage < 80 else \
                            self.colors['error']
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
        """Render performance history graphs (history accumulates in
        update_metrics; this only draws)."""
        if not hasattr(self, 'perf_text'):
            return

        # Create text-based graph
        graph_height = 15
        graph_width = 60

        # Redraw resets scroll to top; remember where the user was
        scroll_pos = self.perf_text.yview()[0]
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

        # Restore scroll position (content length is stable across redraws)
        self.perf_text.yview_moveto(scroll_pos)

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
        """Start the fast monitoring thread — CPU and RAM only, never blocks."""

        def fast_loop():
            while True:
                try:
                    data = {
                        'cpu': self.monitor.get_cpu_usage(),
                        'memory': self.monitor.get_memory_info(),
                    }
                    if GPU_AVAILABLE:
                        data['gpu'] = self.monitor.get_gpu_info()
                    self.data_queue.put(data)

                    # Guardian: RAM% check only — instant, no process scanning
                    g_event = self.guardian.check_ram(data['memory'])
                    if g_event:
                        self.guardian_queue.put(g_event)
                except Exception as e:
                    print(f"Monitoring error: {e}")
                time.sleep(self.monitor.update_interval)

        threading.Thread(target=fast_loop, daemon=True, name='ByteDogFast').start()

    def trigger_process_scan(self, callback=None):
        """Run a process memory scan in background. Calls callback(processes) when done."""
        def _scan():
            try:
                procs = self.monitor.scan_process_memory()
                if callback:
                    self.root.after(0, lambda: callback(procs))
            except Exception as e:
                print(f"Process scan error: {e}")
        threading.Thread(target=_scan, daemon=True, name='ByteDogScan').start()

    def process_queue(self):
        """Process data from monitoring thread"""
        try:
            while True:
                data = self.data_queue.get_nowait()
                # Update metrics based on current view
                self.update_metrics()
        except queue.Empty:
            pass

        # Process guardian events
        try:
            while True:
                g_event = self.guardian_queue.get_nowait()
                self.handle_guardian_event(g_event)
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

    # ── RAM Guardian methods ────────────────────────────────────────────────

    def _menu_install_autostart(self):
        ok, msg = install_autostart()
        (messagebox.showinfo if ok else messagebox.showerror)("Auto-Start", msg)
        self.guardian.log_event('info' if ok else 'warn', msg)

    def _menu_uninstall_autostart(self):
        if not autostart_installed():
            messagebox.showinfo("Auto-Start", "Auto-start is not installed.")
            return
        ok, msg = uninstall_autostart()
        (messagebox.showinfo if ok else messagebox.showerror)("Auto-Start", msg)
        self.guardian.log_event('info' if ok else 'warn', msg)

    def _menu_toggle_guardian(self):
        """Toggle guardian on/off from menu"""
        self.guardian.enabled = not self.guardian.enabled
        state = "enabled" if self.guardian.enabled else "disabled"
        self.guardian.log_event('info', f"Guardian {state} by user")
        if hasattr(self, 'status_label'):
            self.status_label.config(text=f"Guardian {state}")

    def handle_guardian_event(self, event):
        """Show guardian alert immediately, then snapshot + auto-act in background.
        The snapshot is one Norton-safe syscall (~5ms), so hogs appear instantly."""
        self.show_guardian_alert(event)
        tier = event['type']

        def _worker():
            try:
                procs = enrich_chromium(fast_memory_snapshot())
                groups = group_by_name(procs)[:6]
                action_msg = None
                if tier in ('suspend', 'kill'):
                    action_msg = self._auto_act(tier, procs)
                self.root.after(0, lambda: self.update_alert_hogs(groups, action_msg))
            except Exception as e:
                print(f"Guardian worker error: {e}")

        threading.Thread(target=_worker, daemon=True, name='ByteDogGuardianAct').start()

    def _auto_act(self, tier, procs):
        """Suspend or kill the top eligible hog. Runs on a worker thread.
        Walks down the target list on AccessDenied/vanished processes."""
        targets = select_targets(
            procs, self.guardian.config.protected_names(),
            suspended_pids=set(self.guardian.suspended), n=5, self_pid=os.getpid())
        if not targets:
            msg = "No eligible target (all processes protected)"
            self.guardian.log_event('warn', msg)
            return msg

        for t in targets:
            pid, name, gb = t['pid'], t['name'], t['rss'] / (1024 ** 3)
            if tier == 'kill':
                if self.process_manager.kill_process(pid):
                    self.guardian.engine.record_kill(time.time())
                    self.guardian.suspended.pop(pid, None)
                    msg = f"Killed {name} (PID {pid}, {gb:.1f} GB freed)"
                    self.guardian.log_event('action', msg)
                    return msg
            else:
                if self.process_manager.suspend_process(pid):
                    self.guardian.suspended[pid] = name
                    msg = f"Suspended {name} (PID {pid}, {gb:.1f} GB frozen)"
                    self.guardian.log_event('action', msg)
                    return msg
            self.guardian.log_event('warn', f"Could not {tier} {name} (PID {pid}), trying next")
        msg = f"All {tier} attempts failed (access denied?)"
        self.guardian.log_event('warn', msg)
        return msg

    def _resume_all_suspended(self):
        """Resume everything the guardian auto-suspended."""
        if not self.guardian.suspended:
            return
        for pid, name in list(self.guardian.suspended.items()):
            if self.process_manager.resume_process(pid):
                self.guardian.log_event('action', f"Resumed {name} (PID {pid})")
            self.guardian.suspended.pop(pid, None)
        if hasattr(self, 'status_label'):
            self.status_label.config(text="Resumed suspended processes")

    TIER_RANK = {'warn': 0, 'suspend': 1, 'kill': 2}

    def show_guardian_alert(self, event):
        """Non-blocking rescue alert. Warn alerts auto-close; suspend/kill
        alerts stay open. A more severe event replaces an open alert."""
        tier = event['type']
        existing = self.guardian_alert_window
        if existing and existing.winfo_exists():
            if self.TIER_RANK.get(tier, 0) <= self.TIER_RANK.get(
                    getattr(existing, 'tier', 'warn'), 0):
                return
            existing.destroy()

        alert = tk.Toplevel(self.root)
        alert.tier = tier
        alert.title("ByteDog Guardian")
        alert.attributes('-topmost', True)
        alert.configure(bg='#1a1a1a')
        alert.resizable(False, False)

        screen_w = alert.winfo_screenwidth()
        alert.geometry(f"360x300+{screen_w - 380}+80")

        headers = {
            'warn': ('#ff9800', '! WARNING'),
            'suspend': ('#e64a19', '!! SUSPENDING HOG'),
            'kill': ('#f44336', '!!! KILLING HOG'),
        }
        hdr_color, icon = headers.get(tier, headers['warn'])

        hdr = tk.Frame(alert, bg=hdr_color)
        hdr.pack(fill='x')
        tk.Label(hdr, text=f"  RAM GUARDIAN {icon}",
                 bg=hdr_color, fg='white', font=('Arial', 11, 'bold'),
                 anchor='w').pack(fill='x', padx=8, pady=6)

        body = tk.Frame(alert, bg='#1a1a1a')
        body.pack(fill='both', expand=True, padx=10, pady=8)

        ram_pct = event['ram_pct']
        used = event.get('used_gb', 0)
        total = event.get('total_gb', 0)
        tk.Label(body, text=f"RAM Usage: {ram_pct:.1f}%  ({used:.1f} / {total:.0f} GB)",
                 bg='#1a1a1a', fg='white', font=('Arial', 10, 'bold')).pack(anchor='w')

        bar_frame = tk.Frame(body, bg='#1a1a1a')
        bar_frame.pack(fill='x', pady=(2, 6))
        bar_bg = tk.Canvas(bar_frame, height=8, bg='#333333', highlightthickness=0)
        bar_bg.pack(fill='x')
        bar_bg.update_idletasks()
        bar_w = bar_bg.winfo_width() or 330
        fill_w = int(bar_w * ram_pct / 100)
        fill_color = '#f44336' if ram_pct >= self.guardian.config.act_pct else '#ff9800'
        bar_bg.create_rectangle(0, 0, fill_w, 8, fill=fill_color, outline='')

        tk.Label(body, text="Top memory users:",
                 bg='#1a1a1a', fg='#aaaaaa', font=('Arial', 8)).pack(anchor='w')
        self.guardian_alert_hogs = tk.Label(body, text="  scanning...",
                                            bg='#1a1a1a', fg='white',
                                            font=('Consolas', 8), justify='left')
        self.guardian_alert_hogs.pack(anchor='w')

        self.guardian_alert_action = tk.Label(body, text="",
                                              bg='#1a1a1a', fg='#4caf50',
                                              font=('Arial', 8, 'bold'),
                                              wraplength=330, justify='left')
        self.guardian_alert_action.pack(anchor='w', pady=(4, 0))

        # Rescue buttons
        btns = tk.Frame(alert, bg='#1a1a1a')
        btns.pack(fill='x', padx=10, pady=(0, 4))
        for label, cmd in (("Kill Top Hog", self._kill_top_hog_now),
                           ("Suspend Top", self._suspend_top_hog_now),
                           ("Resume All", self._resume_all_suspended)):
            tk.Button(btns, text=label, bg='#2d2d2d', fg='white',
                      relief='flat', font=('Arial', 8),
                      command=cmd).pack(side='left', padx=(0, 4))

        footer = tk.Frame(alert, bg='#1a1a1a')
        footer.pack(fill='x', padx=10, pady=(0, 8))
        countdown_lbl = tk.Label(footer, text="", bg='#1a1a1a', fg='#555555',
                                 font=('Arial', 7))
        countdown_lbl.pack(side='left')
        tk.Button(footer, text="Dismiss", bg='#2d2d2d', fg='white',
                  relief='flat', font=('Arial', 8),
                  command=alert.destroy).pack(side='right')

        self.guardian_alert_window = alert

        if tier == 'warn':
            def _countdown(n):
                if alert.winfo_exists():
                    if n > 0:
                        countdown_lbl.config(text=f"Closing in {n}s")
                        alert.after(1000, lambda: _countdown(n - 1))
                    else:
                        alert.destroy()
            alert.after(1000, lambda: _countdown(14))

    def update_alert_hogs(self, groups, action_msg=None):
        """Fill the open alert with grouped hog totals and the action taken."""
        alert = self.guardian_alert_window
        if not (alert and alert.winfo_exists()):
            return
        if hasattr(self, 'guardian_alert_hogs') and self.guardian_alert_hogs.winfo_exists():
            lines = []
            for g in groups:
                gb = g['rss'] / (1024 ** 3)
                count = f" x{g['count']}" if g['count'] > 1 else ""
                lines.append(f"  {g['name'][:22]:<22} {gb:5.1f} GB{count}")
            self.guardian_alert_hogs.config(text="\n".join(lines) or "  (no data)")
        if action_msg and hasattr(self, 'guardian_alert_action') \
                and self.guardian_alert_action.winfo_exists():
            self.guardian_alert_action.config(text=f"Action: {action_msg}")

    def create_guardian_tab(self, parent):
        """Create the RAM Guardian tab in the detailed view."""
        bg = self.colors['bg']
        fg = self.colors['fg']

        # ── Header ──
        hdr = tk.Frame(parent, bg=bg)
        hdr.pack(fill='x', padx=12, pady=(8, 4))

        tk.Label(hdr, text="RAM Guardian", bg=bg, fg=fg,
                 font=('Arial', 12, 'bold')).pack(side='left')

        self.guardian_enabled_var = tk.BooleanVar(value=self.guardian.enabled)
        tk.Checkbutton(hdr, text="Active", variable=self.guardian_enabled_var,
                       bg=bg, fg=fg, selectcolor=self.colors['button'],
                       activebackground=bg, activeforeground=fg,
                       command=self._toggle_guardian).pack(side='right')

        # ── Escalation thresholds ──
        cfg = self.guardian.config
        self.guardian_thresh_vars = {}
        for key, label, lo, hi in (('warn_pct', 'Warn (alert)', 50, 90),
                                   ('act_pct', 'Suspend hog', 60, 95),
                                   ('crit_pct', 'Kill hog', 70, 98)):
            row = tk.Frame(parent, bg=bg)
            row.pack(fill='x', padx=12, pady=0)
            tk.Label(row, text=f"{label}:", bg=bg, fg=fg,
                     font=('Arial', 8), width=12, anchor='w').pack(side='left')
            var = tk.DoubleVar(value=getattr(cfg, key))
            self.guardian_thresh_vars[key] = var
            tk.Scale(row, from_=lo, to=hi, resolution=1,
                     orient='horizontal', variable=var,
                     bg=bg, fg=fg, highlightthickness=0,
                     troughcolor=self.colors['button'],
                     command=lambda v, k=key: self._update_guardian_threshold(k, v)
                     ).pack(side='left', fill='x', expand=True, padx=6)

        # ── Mode ──
        act_frame = tk.Frame(parent, bg=bg)
        act_frame.pack(fill='x', padx=12, pady=2)

        tk.Label(act_frame, text="Mode:", bg=bg, fg=fg,
                 font=('Arial', 9)).pack(side='left')
        self.guardian_mode_var = tk.StringVar(value=cfg.mode)
        for val, lbl in [('escalate', 'Escalating auto-action'), ('alert_only', 'Alert only')]:
            tk.Radiobutton(act_frame, text=lbl, value=val,
                           variable=self.guardian_mode_var,
                           bg=bg, fg=fg, selectcolor=self.colors['button'],
                           activebackground=bg, activeforeground=fg,
                           command=self._update_guardian_mode
                           ).pack(side='left', padx=4)

        # ── Extra protected processes ──
        prot_frame = tk.Frame(parent, bg=bg)
        prot_frame.pack(fill='x', padx=12, pady=2)
        tk.Label(prot_frame, text="Never touch:", bg=bg, fg=fg,
                 font=('Arial', 8)).pack(side='left')
        self.guardian_protected_var = tk.StringVar(
            value=", ".join(cfg.user_protected))
        tk.Entry(prot_frame, textvariable=self.guardian_protected_var,
                 bg=self.colors['button'], fg=fg,
                 insertbackground=fg, font=('Consolas', 8)
                 ).pack(side='left', fill='x', expand=True, padx=6)
        tk.Button(prot_frame, text="Save", bg=self.colors['button'], fg=fg,
                  font=('Arial', 8), relief='flat',
                  command=self._save_guardian_protected).pack(side='right')

        # ── Current RAM status ──
        ram_row = tk.Frame(parent, bg=bg)
        ram_row.pack(fill='x', padx=12, pady=(6, 2))

        tk.Label(ram_row, text="RAM now:", bg=bg, fg=fg, font=('Arial', 9)).pack(side='left')
        self.guardian_ram_lbl = tk.Label(ram_row, text="--", bg=bg,
                                         fg=self.colors['success'],
                                         font=('Arial', 11, 'bold'))
        self.guardian_ram_lbl.pack(side='left', padx=6)

        # Manual kill button
        tk.Button(ram_row, text="Kill Top Hog Now",
                  bg='#5a1a1a', fg='white', font=('Arial', 8),
                  relief='flat', command=self._kill_top_hog_now
                  ).pack(side='right')

        # ── Divider ──
        tk.Frame(parent, height=1, bg=self.colors['select']).pack(fill='x', padx=12, pady=4)

        # ── Split: hogs + event log ──
        split = tk.Frame(parent, bg=bg)
        split.pack(fill='both', expand=True, padx=12)

        left = tk.Frame(split, bg=bg)
        left.pack(side='left', fill='both', expand=True)

        tk.Label(left, text="Top Memory Hogs", bg=bg, fg=fg,
                 font=('Arial', 9, 'bold')).pack(anchor='w')
        self.guardian_hogs_text = tk.Text(left, height=7, bg=self.colors['button'],
                                          fg=fg, font=('Consolas', 8),
                                          state='disabled', width=22, relief='flat')
        self.guardian_hogs_text.pack(fill='both', expand=True, pady=2)

        right = tk.Frame(split, bg=bg)
        right.pack(side='right', fill='both', expand=True, padx=(6, 0))

        tk.Label(right, text="Event Log", bg=bg, fg=fg,
                 font=('Arial', 9, 'bold')).pack(anchor='w')
        self.guardian_log_text = tk.Text(right, height=7, bg=self.colors['button'],
                                         fg=fg, font=('Consolas', 7),
                                         state='disabled', width=28, relief='flat')
        self.guardian_log_text.pack(fill='both', expand=True, pady=2)

        # ── Leak suspects ──
        tk.Frame(parent, height=1, bg=self.colors['select']).pack(fill='x', padx=12, pady=(4, 2))
        tk.Label(parent, text="Memory Leak Suspects  (growing >50 MB/min)",
                 bg=bg, fg=self.colors['warning'],
                 font=('Arial', 8, 'bold')).pack(anchor='w', padx=12)
        self.guardian_leak_text = tk.Text(parent, height=3, bg=self.colors['button'],
                                          fg=self.colors['warning'], font=('Consolas', 8),
                                          state='disabled', relief='flat')
        self.guardian_leak_text.pack(fill='x', padx=12, pady=(2, 6))

        # Defer first update — don't block main thread during setup
        self.root.after(2000, self.update_guardian_tab)

    def _toggle_guardian(self):
        self.guardian.enabled = self.guardian_enabled_var.get()
        state = "enabled" if self.guardian.enabled else "disabled"
        self.guardian.log_event('info', f"Guardian {state} by user")

    def _update_guardian_threshold(self, key, val):
        setattr(self.guardian.config, key, float(val))
        self.guardian.save_config()

    def _update_guardian_mode(self):
        self.guardian.config.mode = self.guardian_mode_var.get()
        self.guardian.save_config()
        self.guardian.log_event('info', f"Mode: {self.guardian.config.mode}")

    def _save_guardian_protected(self):
        raw = self.guardian_protected_var.get()
        names = [n.strip() for n in raw.split(',') if n.strip()]
        self.guardian.config.user_protected = names
        self.guardian.save_config()
        self.guardian.log_event('info', f"Protected list: {len(names)} extra names")

    def _select_top_target(self):
        """Fast snapshot -> top eligible target. Returns (target, groups) or (None, groups)."""
        procs = enrich_chromium(fast_memory_snapshot())
        targets = select_targets(
            procs, self.guardian.config.protected_names(),
            suspended_pids=set(self.guardian.suspended), self_pid=os.getpid())
        return (targets[0] if targets else None), group_by_name(procs)[:6]

    def _kill_top_hog_now(self):
        """Snapshot, confirm, kill the top hog."""
        def _worker():
            target, groups = self._select_top_target()
            def _confirm():
                self.update_alert_hogs(groups)
                if not target:
                    messagebox.showinfo("Guardian", "No killable processes found.")
                    return
                name, pid, gb = target['name'], target['pid'], target['rss'] / (1024 ** 3)
                if messagebox.askyesno("Kill Process",
                                       f"Kill '{name}' (PID {pid}, {gb:.1f} GB)?"):
                    if self.process_manager.kill_process(pid):
                        self.guardian.log_event('action', f"Manual kill: {name} (PID {pid})")
                    else:
                        messagebox.showerror("Guardian",
                                             f"Could not kill '{name}' — may need admin rights.")
            self.root.after(0, _confirm)
        threading.Thread(target=_worker, daemon=True, name='ByteDogManualKill').start()

    def _suspend_top_hog_now(self):
        """Snapshot and suspend the top hog (no confirm — it's reversible)."""
        def _worker():
            target, groups = self._select_top_target()
            msg = None
            if target:
                pid, name = target['pid'], target['name']
                if self.process_manager.suspend_process(pid):
                    self.guardian.suspended[pid] = name
                    msg = f"Suspended {name} (PID {pid}) — use Resume All to undo"
                    self.guardian.log_event('action', msg)
                else:
                    msg = f"Could not suspend {name} (PID {pid})"
            self.root.after(0, lambda: self.update_alert_hogs(groups, msg))
        threading.Thread(target=_worker, daemon=True, name='ByteDogManualSuspend').start()

    def update_guardian_tab(self):
        """Refresh guardian tab — RAM instantly, process hogs from cache only."""
        if not hasattr(self, 'guardian_hogs_text'):
            return

        mem = self.monitor.get_memory_info()
        ram_pct = mem['percent']
        used_gb = mem['used'] / (1024 ** 3)
        total_gb = mem['total'] / (1024 ** 3)
        cfg = self.guardian.config

        # RAM label (instant)
        if ram_pct >= cfg.act_pct:
            r_color = self.colors['error']
        elif ram_pct >= cfg.warn_pct:
            r_color = self.colors['warning']
        else:
            r_color = self.colors['success']
        self.guardian_ram_lbl.config(
            text=f"{ram_pct:.1f}%  ({used_gb:.1f} / {total_gb:.0f} GB)", fg=r_color)

        # Top hogs — live Norton-safe snapshot (~4ms), grouped per app
        try:
            snap = fast_memory_snapshot()
        except Exception:
            snap = []

        self.guardian_hogs_text.config(state='normal')
        self.guardian_hogs_text.delete(1.0, tk.END)
        if snap:
            for g in group_by_name(snap)[:8]:
                gb = g['rss'] / (1024 ** 3)
                count = f" x{g['count']}" if g['count'] > 1 else ""
                self.guardian_hogs_text.insert(
                    tk.END, f"{g['name'][:15]:<15} {gb:5.2f}GB{count}\n")
        else:
            self.guardian_hogs_text.insert(tk.END, "  snapshot unavailable\n")
        self.guardian_hogs_text.config(state='disabled')

        # Event log (instant — reads from deque)
        level_icons = {'info': ' ', 'warn': '!', 'critical': '!!', 'action': '>'}
        level_colors = {'info': '#888888', 'warn': '#ff9800',
                        'critical': '#f44336', 'action': '#4caf50'}
        self.guardian_log_text.config(state='normal')
        self.guardian_log_text.delete(1.0, tk.END)
        with self.guardian._lock:
            for entry in list(self.guardian.event_log)[:20]:
                icon = level_icons.get(entry['level'], ' ')
                line = f"{entry['time']} {icon} {entry['message'][:36]}\n"
                tag = entry['level']
                self.guardian_log_text.insert(tk.END, line, tag)
                self.guardian_log_text.tag_config(
                    tag, foreground=level_colors.get(entry['level'], '#888888'))
        self.guardian_log_text.config(state='disabled')

        # Leak suspects — history accumulates from the live snapshots above
        self.guardian_leak_text.config(state='normal')
        self.guardian_leak_text.delete(1.0, tk.END)
        if snap:
            self.guardian.track_memory_growth(snap)
            leaks = self.guardian.get_leak_suspects(snap)
            if leaks:
                for lk in leaks[:3]:
                    name = lk.get('name', '?')[:20]
                    rate = lk['growth_mb_min']
                    self.guardian_leak_text.insert(tk.END, f"  {name:<22}  +{rate:.0f} MB/min\n")
            else:
                self.guardian_leak_text.insert(
                    tk.END, f"  No leaks detected (watching {len(snap)} processes)\n")
        else:
            self.guardian_leak_text.insert(tk.END, "  Snapshot unavailable\n")
        self.guardian_leak_text.config(state='disabled')

        if self.view_mode.get() == 'detailed':
            self.root.after(2000, self.update_guardian_tab)

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
        print("⚠️  GPU monitoring not available (install: pip install nvidia-ml-py)")

    # Survive the thrash we're fighting: HIGH priority + pinned working set
    for result in harden_self():
        print(f"Guardian hardening: {result}")

    app = ByteDogApp()
    app.run()


if __name__ == "__main__":
    main()