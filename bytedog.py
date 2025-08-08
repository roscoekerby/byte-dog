#!/usr/bin/env python3
"""
ByteDog - Lightweight System Resource Monitor
A minimal-resource system monitor following the Dog family design principles
"""

import tkinter as tk
from tkinter import ttk, messagebox, font
import psutil
import threading
import time
import platform
import os
import json
from datetime import datetime
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
        self.root.geometry("900x600")

        self.monitor = SystemMonitor()
        self.process_manager = ProcessManager()

        # Initialize CPU percent to prevent blocking
        psutil.cpu_percent(interval=None)

        self.current_view = 'compact'
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

        self.setup_styles()
        self.setup_ui()
        self.start_monitoring()
        self.process_queue()

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

        # Menu bar
        self.create_menu()

        # Top toolbar
        self.create_toolbar()

        # Main content area
        self.main_frame = ttk.Frame(self.root)
        self.main_frame.pack(fill='both', expand=True, padx=10, pady=5)

        # Show compact view by default
        self.show_compact_view()

    def create_menu(self):
        """Create menu bar"""
        menubar = tk.Menu(self.root, bg=self.colors['button'], fg=self.colors['fg'])
        self.root.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Export Data...", command=self.export_data)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Minimal", command=self.show_minimal_view)
        view_menu.add_command(label="Compact", command=self.show_compact_view)
        view_menu.add_command(label="Detailed", command=self.show_detailed_view)

        # Tools menu
        tools_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Settings", command=self.show_settings)
        tools_menu.add_command(label="Performance Report", command=self.generate_report)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0, bg=self.colors['button'], fg=self.colors['fg'])
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)

    def create_toolbar(self):
        """Create toolbar with view switcher"""
        toolbar = tk.Frame(self.root, bg=self.colors['button'], height=40)
        toolbar.pack(fill='x', padx=10, pady=(10, 5))
        toolbar.pack_propagate(False)

        # View buttons
        tk.Button(toolbar, text="⚡ Minimal", bg=self.colors['button'], fg=self.colors['fg'],
                  font=('Arial', 10), bd=1, command=self.show_minimal_view).pack(side='left', padx=2)

        tk.Button(toolbar, text="📊 Compact", bg=self.colors['button'], fg=self.colors['fg'],
                  font=('Arial', 10), bd=1, command=self.show_compact_view).pack(side='left', padx=2)

        tk.Button(toolbar, text="📈 Detailed", bg=self.colors['button'], fg=self.colors['fg'],
                  font=('Arial', 10), bd=1, command=self.show_detailed_view).pack(side='left', padx=2)

        # Status label
        self.status_label = tk.Label(toolbar, text="Ready", bg=self.colors['button'],
                                     fg=self.colors['success'], font=('Arial', 10))
        self.status_label.pack(side='right', padx=10)

    def show_minimal_view(self):
        """Show minimal overlay view"""
        if self.minimal_window and self.minimal_window.winfo_exists():
            self.minimal_window.lift()
        else:
            self.minimal_window = MinimalView(self.root, self.monitor)

    def show_compact_view(self):
        """Show compact view with essential metrics"""
        self.current_view = 'compact'
        self.clear_main_frame()

        # System overview frame
        overview_frame = ttk.Frame(self.main_frame)
        overview_frame.pack(fill='x', pady=10)

        # Store metric cards for updates
        self.metric_cards = {}

        # CPU card
        cpu_card = self.create_metric_card(overview_frame, "CPU", 0, "%")
        cpu_card.pack(side='left', padx=10)
        self.metric_cards['cpu'] = cpu_card

        # Memory card
        mem_card = self.create_metric_card(overview_frame, "Memory", 0, "%")
        mem_card.pack(side='left', padx=10)
        self.metric_cards['memory'] = mem_card

        # GPU card if available
        if GPU_AVAILABLE:
            gpu_card = self.create_metric_card(overview_frame, "GPU", 0, "%")
            gpu_card.pack(side='left', padx=10)
            self.metric_cards['gpu'] = gpu_card

        # Top processes
        top_frame = ttk.Frame(self.main_frame)
        top_frame.pack(fill='both', expand=True, pady=10)

        tk.Label(top_frame, text="Top Processes", bg=self.colors['bg'], fg=self.colors['fg'],
                 font=('Arial', 12, 'bold')).pack(anchor='w', pady=5)

        # Process list
        self.create_process_list(top_frame)

        # Start updating metrics
        self.update_compact_metrics()

    def show_detailed_view(self):
        """Show detailed view with all metrics and graphs"""
        self.current_view = 'detailed'
        self.clear_main_frame()

        # Create notebook for tabs
        notebook = ttk.Notebook(self.main_frame)
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

    def create_metric_card(self, parent, title, value, unit):
        """Create a metric display card"""
        card = tk.Frame(parent, bg=self.colors['button'], relief='raised', bd=1)
        card.configure(width=150, height=100)
        card.pack_propagate(False)

        card.title_label = tk.Label(card, text=title, bg=self.colors['button'], fg=self.colors['fg'],
                                    font=('Arial', 10))
        card.title_label.pack(pady=5)

        card.value_label = tk.Label(card, text=f"{value:.1f}{unit}",
                                    bg=self.colors['button'], fg=self.colors['success'],
                                    font=('Arial', 20, 'bold'))
        card.value_label.pack()

        return card

    def update_metric_card(self, card, value, unit="%"):
        """Update a metric card's value"""
        if isinstance(value, (int, float)):
            color = self.colors['success'] if value < 50 else self.colors['warning'] if value < 80 else self.colors[
                'error']
            card.value_label.config(text=f"{value:.1f}{unit}", fg=color)

    def create_process_list(self, parent):
        """Create process list view"""
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

        # Update process list
        self.update_process_list()

    def create_overview_tab(self, parent):
        """Create overview tab content"""
        # System info
        info_frame = ttk.Frame(parent)
        info_frame.pack(fill='x', padx=20, pady=10)

        system_info = f"System: {platform.system()} {platform.release()}\n"
        system_info += f"Processor: {platform.processor() or 'Unknown'}\n"
        system_info += f"CPU Cores: {psutil.cpu_count(logical=False)} physical, {psutil.cpu_count()} logical\n"

        mem = psutil.virtual_memory()
        system_info += f"Total Memory: {mem.total / (1024 ** 3):.1f} GB"

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

    def clear_main_frame(self):
        """Clear main frame content"""
        for widget in self.main_frame.winfo_children():
            widget.destroy()

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
        self.update_process_list()

    def refresh_processes(self):
        """Force refresh process list"""
        self.monitor.process_cache = []
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

    def update_compact_metrics(self):
        """Update compact view metrics"""
        if self.current_view != 'compact':
            return

        try:
            # Update CPU
            cpu = self.monitor.get_cpu_usage()
            if 'cpu' in self.metric_cards:
                self.update_metric_card(self.metric_cards['cpu'], cpu)

            # Update Memory
            mem = self.monitor.get_memory_info()
            if 'memory' in self.metric_cards:
                self.update_metric_card(self.metric_cards['memory'], mem['percent'])

            # Update GPU if available
            if GPU_AVAILABLE and 'gpu' in self.metric_cards:
                gpu_info = self.monitor.get_gpu_info()
                if gpu_info:
                    self.update_metric_card(self.metric_cards['gpu'], gpu_info['load'])
        except:
            pass

        # Schedule next update
        self.root.after(2000, self.update_compact_metrics)

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
        if self.current_view == 'detailed':
            self.root.after(2000, self.update_performance_graph)

    def create_text_graph(self, data, height, width):
        """Create ASCII graph from data"""
        if not data:
            return "No data available\n"

        graph = []

        # Scale data to fit height
        max_val = max(data) if data else 100
        min_val = 0

        # Create graph lines
        for h in range(height, -1, -1):
            threshold = (h / height) * max_val
            line = f"{threshold:3.0f}% |"

            for i, val in enumerate(list(data)[-width:]):
                if val >= threshold:
                    line += "█"
                else:
                    line += " "

            graph.append(line + "\n")

        # Add bottom axis
        graph.append("     +" + "-" * min(len(data), width) + "\n")
        graph.append(
            "      " + "".join([str(i % 10) if i % 10 == 0 else " " for i in range(min(len(data), width))]) + "\n")

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
        if self.current_view == 'detailed':
            self.root.after(2000, self.update_network_info)

    def update_detailed_view(self):
        """Update detailed view metrics"""
        if self.current_view != 'detailed':
            return

        # Update CPU cores
        if hasattr(self, 'core_labels'):
            cores = self.monitor.get_cpu_per_core()
            for i, label in enumerate(self.core_labels):
                if i < len(cores):
                    usage = cores[i]
                    color = self.colors['success'] if usage < 50 else self.colors['warning'] if usage < 80 else \
                    self.colors['error']
                    label.config(text=f"Core {i}: {usage:.1f}%", fg=color)

        # Schedule next update
        self.root.after(2000, self.update_detailed_view)

    def format_bytes(self, bytes):
        """Format bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes < 1024.0:
                return f"{bytes:.2f} {unit}"
            bytes /= 1024.0
        return f"{bytes:.2f} PB"

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

                self.status_label.config(text=f"Data exported to {os.path.basename(filename)}",
                                         fg=self.colors['success'])
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
                self.status_label.config(text=f"Report saved to {os.path.basename(filename)}",
                                         fg=self.colors['success'])
            except Exception as e:
                messagebox.showerror("Save Error", f"Failed to save report: {str(e)}")

    def show_settings(self):
        """Show settings dialog"""
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Settings")
        settings_window.geometry("400x300")
        settings_window.configure(bg=self.colors['bg'])

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
        tk.Checkbutton(settings_window, text="Always on top (minimal view)",
                       variable=always_top_var, bg=self.colors['bg'], fg=self.colors['fg'],
                       selectcolor=self.colors['button']).pack(pady=10)

        # Theme selection
        tk.Label(settings_window, text="Theme:", bg=self.colors['bg'],
                 fg=self.colors['fg']).pack(pady=5)

        theme_var = tk.StringVar(value="Dark")
        theme_frame = tk.Frame(settings_window, bg=self.colors['bg'])
        theme_frame.pack()

        tk.Radiobutton(theme_frame, text="Dark", variable=theme_var, value="Dark",
                       bg=self.colors['bg'], fg=self.colors['fg'],
                       selectcolor=self.colors['button']).pack(side='left')
        tk.Radiobutton(theme_frame, text="Light", variable=theme_var, value="Light",
                       bg=self.colors['bg'], fg=self.colors['fg'],
                       selectcolor=self.colors['button']).pack(side='left')

        # Save button
        def save_settings():
            self.monitor.update_interval = interval_var.get()
            settings_window.destroy()
            self.status_label.config(text="Settings saved", fg=self.colors['success'])

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
• Multiple view modes
• Cross-platform support

Created with Python and psutil
© 2025 - Following the Dog family tradition"""

        messagebox.showinfo("About ByteDog", about_text)

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
                except:
                    pass

                time.sleep(self.monitor.update_interval)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

    def process_queue(self):
        """Process data from monitoring thread"""
        try:
            while True:
                data = self.data_queue.get_nowait()

                # Update process list if visible
                if hasattr(self, 'process_tree'):
                    self.update_process_list()

                # Update view-specific elements
                if self.current_view == 'detailed':
                    self.update_detailed_view()
        except queue.Empty:
            pass

        # Schedule next check
        self.root.after(1000, self.process_queue)

    def run(self):
        """Start the application"""
        self.root.mainloop()


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

    app = ByteDogApp()
    app.run()


if __name__ == "__main__":
    main()