# ==============================================================================
# ShrinkZIP - In-Place Low-Disk Archive Extractor (GUI)
#
# Created & Designed by: Arman Ahmed
# Polished & assisted by: Claude
#
# The extraction engine lives in shrinkzip_engine.py (no GUI code, fully tested).
# ==============================================================================

import os
import shutil
import threading
import queue
import ctypes
import subprocess
import warnings
warnings.filterwarnings("ignore")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

from shrinkzip_engine import (
    ShrinkZipEngineWorker, analyze_archive, estimate_inplace_peak,
    first_volume, sidecar_path, get_7z_tool_path, get_resource_path,
    format_bytes, _hidden_startupinfo, HAS_PY7ZR, HAS_RARFILE,
)
if HAS_PY7ZR:
    import py7zr
if HAS_RARFILE:
    import rarfile

# ------------------------------------------------------------------------------
# Modern Polished User Interface
# Designed by Arman Ahmed (assisted by Claude Code & AI Agent)
# ------------------------------------------------------------------------------
class ShrinkZipApp(ctk.CTk):
    """
    Polished Next-Gen In-Place Archive Extractor GUI.
    
    Created & Designed by: Arman Ahmed
    Polished & assisted by: Claude Code & AI Agent
    """
    APP_THEMES = {
        "dark": {
            "root_bg": "#0B0C10",
            "frame_bg": "#12141A",
            "card_bg": "#181B22",
            "card_sub_bg": "#1E222B",
            "border_color": "#252936",
            "accent_blue": "#3B82F6",
            "accent_hover": "#2563EB",
            "accent_soft": "#60A5FA",
            "accent_mint": "#10B981",
            "accent_amber": "#F59E0B",
            "text_primary": "#E2E8F0",
            "text_secondary": "#94A3B8",
            "text_muted": "#64748B",
            "tree_bg": "#101217",
            "tree_fg": "#E2E8F0",
            "tree_select": "#1E3A8A",
            "log_bg": "#0D0E13",
            "log_fg": "#34D399"
        },
        "light": {
            "root_bg": "#F8FAFC",
            "frame_bg": "#FFFFFF",
            "card_bg": "#F1F5F9",
            "card_sub_bg": "#E2E8F0",
            "border_color": "#CBD5E1",
            "accent_blue": "#2563EB",
            "accent_hover": "#1D4ED8",
            "accent_soft": "#3B82F6",
            "accent_mint": "#059669",
            "accent_amber": "#D97706",
            "text_primary": "#0F172A",
            "text_secondary": "#475569",
            "text_muted": "#94A3B8",
            "tree_bg": "#FFFFFF",
            "tree_fg": "#0F172A",
            "tree_select": "#BFDBFE",
            "log_bg": "#F8FAFC",
            "log_fg": "#0F172A"
        }
    }

    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.title("ShrinkZIP - In-Place Archive Extractor")
        self.geometry("1000x840")
        self.minsize(880, 720)
        self.msg_queue = queue.Queue()
        self.worker = None
        self.theme_mode = "dark"
        self.all_archive_items = []
        self.items_by_path = {}
        self.total_uncompressed = 0
        self.total_compressed = 0
        self.inplace_supported = True      # False for 7Z/RAR and ZIPs that cannot be done in place
        self.inplace_note = ""
        self.plan_units = []               # [(offset, output_size)] ascending
        self.batch_bytes = 0
        self.archive_total = 0
        self.largest_file = ("", 0)
        self.resume_pending = False

        try:
            myappid = 'armanahmed.shrinkzip.v7'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

        icon_path = get_resource_path("app_icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self._build_ui_layout()
        self._apply_theme_colors()
        self._poll_queue()

    def _build_ui_layout(self):
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_container.pack(fill="both", expand=True, padx=14, pady=12)

        # ----------------- Top Header Bar -----------------
        self.header_frame = ctk.CTkFrame(self.main_container, corner_radius=8, height=52)
        self.header_frame.pack(fill="x", pady=(0, 6))

        title_box = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        title_box.pack(side="left", padx=14, pady=8)

        self.lbl_title = ctk.CTkLabel(
            title_box,
            text="⚡ ShrinkZIP",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold")
        )
        self.lbl_title.pack(anchor="w")

        self.lbl_sub = ctk.CTkLabel(
            title_box,
            text="In-Place Low-Disk Archive Extractor • Reclaims Space Dynamically",
            font=ctk.CTkFont(family="Segoe UI", size=9)
        )
        self.lbl_sub.pack(anchor="w")

        header_ctrl = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        header_ctrl.pack(side="right", padx=12, pady=8)

        self.btn_about = ctk.CTkButton(
            header_ctrl,
            text="ℹ About",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            width=70,
            height=28,
            corner_radius=6,
            cursor="hand2",
            command=self._show_about_dialog
        )
        self.btn_about.pack(side="left", padx=(0, 8))

        self.btn_theme = ctk.CTkButton(
            header_ctrl,
            text="☀️ Light Mode",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            width=88,
            height=28,
            corner_radius=6,
            cursor="hand2",
            command=self._toggle_theme
        )
        self.btn_theme.pack(side="left")

        # ----------------- Intake & Path Card -----------------
        self.intake_card = ctk.CTkFrame(self.main_container, corner_radius=8, border_width=1)
        self.intake_card.pack(fill="x", pady=3)

        # Archive Path Row
        arch_row = ctk.CTkFrame(self.intake_card, fg_color="transparent")
        arch_row.pack(fill="x", padx=12, pady=(8, 4))

        lbl_arch = ctk.CTkLabel(arch_row, text="Archive File:", width=120, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"))
        lbl_arch.pack(side="left")

        self.arch_var = tk.StringVar()
        self.entry_arch = ctk.CTkEntry(arch_row, textvariable=self.arch_var, font=ctk.CTkFont(family="Segoe UI", size=11), height=32, corner_radius=6)
        self.entry_arch.pack(side="left", fill="x", expand=True, padx=6)

        self.btn_browse_arch = ctk.CTkButton(
            arch_row,
            text="Select Archive",
            width=120,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            corner_radius=6,
            cursor="hand2",
            command=self._browse_archive
        )
        self.btn_browse_arch.pack(side="left")

        # Destination Folder Row
        dest_row = ctk.CTkFrame(self.intake_card, fg_color="transparent")
        dest_row.pack(fill="x", padx=12, pady=4)

        lbl_dest = ctk.CTkLabel(dest_row, text="Extract Folder:", width=120, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"))
        lbl_dest.pack(side="left")

        self.dest_var = tk.StringVar()
        self.entry_dest = ctk.CTkEntry(dest_row, textvariable=self.dest_var, font=ctk.CTkFont(family="Segoe UI", size=11), height=32, corner_radius=6)
        self.entry_dest.pack(side="left", fill="x", expand=True, padx=6)

        self.btn_browse_dest = ctk.CTkButton(
            dest_row,
            text="Destination...",
            width=120,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            corner_radius=6,
            cursor="hand2",
            command=self._browse_dest
        )
        self.btn_browse_dest.pack(side="left")

        # Extraction Options Row
        ctrl_row = ctk.CTkFrame(self.intake_card, fg_color="transparent")
        ctrl_row.pack(fill="x", padx=12, pady=(4, 6))

        lbl_mode = ctk.CTkLabel(ctrl_row, text="Mode:", width=120, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"))
        lbl_mode.pack(side="left")

        self.mode_var = tk.StringVar(value="shrink")
        self.rb_shrink = ctk.CTkRadioButton(
            ctrl_row,
            text="In-Place Extraction (Low Disk Space)",
            variable=self.mode_var,
            value="shrink",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._recalculate_space_plan
        )
        self.rb_shrink.pack(side="left", padx=(0, 16))

        self.rb_standard = ctk.CTkRadioButton(
            ctrl_row,
            text="Keep Archive Extraction (Standard)",
            variable=self.mode_var,
            value="standard",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._recalculate_space_plan
        )
        self.rb_standard.pack(side="left", padx=(0, 20))

        self.del_var = tk.BooleanVar(value=False)
        self.switch_del = ctk.CTkSwitch(
            ctrl_row,
            text="Delete archive after a fully successful extraction (Keep Archive mode)",
            variable=self.del_var,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold")
        )
        self.switch_del.pack(side="left")

        # ----------------- Preflight Capacity Card -----------------
        self.plan_card = ctk.CTkFrame(self.intake_card, corner_radius=6, border_width=1)
        self.plan_card.pack(fill="x", padx=12, pady=(2, 8))

        plan_grid = ctk.CTkFrame(self.plan_card, fg_color="transparent")
        plan_grid.pack(fill="x", padx=10, pady=5)

        self.lbl_plan_status = ctk.CTkLabel(
            plan_grid,
            text="Select an archive file to inspect capacity and calculate space plan.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            anchor="w"
        )
        self.lbl_plan_status.pack(side="left")

        self.pill_plan_badge = ctk.CTkLabel(
            plan_grid,
            text="STANDBY",
            font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
            corner_radius=4,
            padx=8,
            pady=2
        )
        self.pill_plan_badge.pack(side="right")

        # ----------------- Archive Member Explorer & Details Section -----------------
        self.content_card = ctk.CTkFrame(self.main_container, corner_radius=8, border_width=1)
        self.content_card.pack(fill="both", expand=True, pady=3)

        content_hdr = ctk.CTkFrame(self.content_card, fg_color="transparent")
        content_hdr.pack(fill="x", padx=12, pady=(6, 4))

        self.lbl_content_title = ctk.CTkLabel(
            content_hdr,
            text="📁 Archive Contents",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold")
        )
        self.lbl_content_title.pack(side="left")

        search_box = ctk.CTkFrame(content_hdr, fg_color="transparent")
        search_box.pack(side="right")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter_tree)
        self.entry_search = ctk.CTkEntry(
            search_box,
            textvariable=self.search_var,
            placeholder_text="🔍 Filter files...",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            width=200,
            height=26,
            corner_radius=6
        )
        self.entry_search.pack(side="left", padx=(0, 10))

        self.lbl_content_stats = ctk.CTkLabel(
            search_box,
            text="[ No archive loaded ]",
            font=ctk.CTkFont(family="Segoe UI", size=9)
        )
        self.lbl_content_stats.pack(side="left")

        # Explorer Treeview Table
        tree_holder = tk.Frame(self.content_card)
        tree_holder.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        cols = ("name", "size", "packed", "modified")
        self.tree = ttk.Treeview(tree_holder, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("name", text="Member Name", anchor="w")
        self.tree.heading("size", text="Uncompressed", anchor="e")
        self.tree.heading("packed", text="Packed Size", anchor="e")
        self.tree.heading("modified", text="Date Modified", anchor="center")

        self.tree.column("name", width=460, anchor="w")
        self.tree.column("size", width=120, anchor="e")
        self.tree.column("packed", width=120, anchor="e")
        self.tree.column("modified", width=150, anchor="center")

        tree_scroll = ttk.Scrollbar(tree_holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ----------------- Selected Item Details Panel -----------------
        self.details_card = ctk.CTkFrame(self.content_card, corner_radius=6, height=56)
        self.details_card.pack(fill="x", padx=8, pady=(0, 6))

        details_grid = ctk.CTkFrame(self.details_card, fg_color="transparent")
        details_grid.pack(fill="x", padx=10, pady=5)

        self.detail_name_lbl = ctk.CTkLabel(
            details_grid,
            text="Selected File: Click any item above to inspect detailed attributes.",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            anchor="w"
        )
        self.detail_name_lbl.pack(fill="x")

        self.detail_meta_lbl = ctk.CTkLabel(
            details_grid,
            text="Uncompressed: --  |  Packed: --  |  Ratio: --  |  Modified: --  |  CRC-32: --",
            font=ctk.CTkFont(family="Segoe UI", size=9),
            anchor="w"
        )
        self.detail_meta_lbl.pack(fill="x", pady=(2, 0))

        # ----------------- 4 Clean Telemetry Gauges -----------------
        self.telemetry_grid = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.telemetry_grid.pack(fill="x", pady=2)

        self.metric_cards = {}
        metrics = [
            ("ARCHIVE FOOTPRINT", "m_arch", "--", "ORIGINAL -> CURRENT"),
            ("TOTAL EXTRACTED", "m_extr", "--", "PAYLOAD ON DISK"),
            ("THROUGHPUT RATE", "m_speed", "-- MB/s", "REAL-TIME I/O"),
            ("ESTIMATED REMAINING", "m_eta", "--:--", "ETA COUNTDOWN")
        ]

        for idx, (label_title, key, def_val, sub_title) in enumerate(metrics):
            card = ctk.CTkFrame(self.telemetry_grid, corner_radius=8, border_width=1)
            card.grid(row=0, column=idx, sticky="ew", padx=3)
            self.telemetry_grid.columnconfigure(idx, weight=1)

            lbl_t = ctk.CTkLabel(card, text=label_title, font=ctk.CTkFont(family="Segoe UI", size=8, weight="bold"), anchor="w")
            lbl_t.pack(anchor="w", padx=10, pady=(4, 0))

            lbl_v = ctk.CTkLabel(card, text=def_val, font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"), anchor="w")
            lbl_v.pack(anchor="w", padx=10, pady=(0, 0))

            lbl_s = ctk.CTkLabel(card, text=sub_title, font=ctk.CTkFont(family="Segoe UI", size=7), anchor="w")
            lbl_s.pack(anchor="w", padx=10, pady=(0, 4))

            self.metric_cards[key] = (lbl_t, lbl_v, lbl_s, card)

        # ----------------- Progress Bar & File Stream -----------------
        prog_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        prog_frame.pack(fill="x", pady=2)

        self.lbl_stream_file = ctk.CTkLabel(
            prog_frame,
            text="Ready for archive selection.",
            font=ctk.CTkFont(family="Segoe UI", size=9),
            anchor="w"
        )
        self.lbl_stream_file.pack(fill="x", pady=(0, 2))

        self.progress_bar = ctk.CTkProgressBar(prog_frame, height=8, corner_radius=4)
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0)

        # ----------------- Activity Log -----------------
        self.log_card = ctk.CTkFrame(self.main_container, corner_radius=8, border_width=1)
        self.log_card.pack(fill="x", pady=2)

        self.log_text = tk.Text(self.log_card, height=3, wrap="word", font=("Consolas", 8), relief="flat")
        self.log_text.pack(fill="x", expand=True, side="left", padx=6, pady=4)

        log_scroll = ttk.Scrollbar(self.log_card, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y", pady=4, padx=(0, 4))
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # ----------------- Bottom Action Bar (No Footer Signature) -----------------
        action_dock = ctk.CTkFrame(self.main_container, fg_color="transparent")
        action_dock.pack(fill="x", pady=(4, 0))

        self.btn_open_folder = ctk.CTkButton(
            action_dock,
            text="📂 Open Destination Folder",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            height=32,
            corner_radius=6,
            cursor="hand2",
            command=self._open_destination_folder
        )
        # Positioned on left when extraction finishes

        self.btn_extract = ctk.CTkButton(
            action_dock,
            text="Start Extraction",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            height=34,
            corner_radius=6,
            cursor="hand2",
            command=self._start_extraction
        )
        self.btn_extract.pack(side="right", padx=4)

        self.btn_stop = ctk.CTkButton(
            action_dock,
            text="Cancel",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            height=34,
            width=80,
            corner_radius=6,
            cursor="hand2",
            state="disabled",
            command=self._stop_extraction
        )
        self.btn_stop.pack(side="right", padx=4)

    def _toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        ctk.set_appearance_mode("Light" if self.theme_mode == "light" else "Dark")
        self._apply_theme_colors()

    def _apply_theme_colors(self):
        c = self.APP_THEMES[self.theme_mode]

        self.configure(fg_color=c["root_bg"])
        self.header_frame.configure(fg_color=c["frame_bg"])
        self.intake_card.configure(fg_color=c["frame_bg"], border_color=c["border_color"])
        self.plan_card.configure(fg_color=c["card_bg"], border_color=c["border_color"])
        self.content_card.configure(fg_color=c["frame_bg"], border_color=c["border_color"])
        self.details_card.configure(fg_color=c["card_sub_bg"])
        self.log_card.configure(fg_color=c["frame_bg"], border_color=c["border_color"])

        self.lbl_title.configure(text_color=c["accent_soft"])
        self.lbl_sub.configure(text_color=c["text_muted"])

        btn_theme_txt = "🌙 Dark Mode" if self.theme_mode == "light" else "☀️ Light Mode"
        self.btn_theme.configure(
            text=btn_theme_txt,
            fg_color=c["card_bg"],
            text_color=c["text_primary"],
            hover_color=c["border_color"]
        )
        self.btn_about.configure(
            fg_color=c["card_bg"],
            text_color=c["text_primary"],
            hover_color=c["border_color"]
        )

        self.entry_arch.configure(fg_color=c["card_bg"], text_color=c["text_primary"], border_color=c["border_color"])
        self.entry_dest.configure(fg_color=c["card_bg"], text_color=c["text_primary"], border_color=c["border_color"])
        self.entry_search.configure(fg_color=c["card_bg"], text_color=c["text_primary"], border_color=c["border_color"])

        self.btn_browse_arch.configure(fg_color=c["accent_blue"], text_color="#FFFFFF", hover_color=c["accent_hover"])
        self.btn_browse_dest.configure(fg_color=c["card_bg"], text_color=c["text_primary"], hover_color=c["border_color"])
        self.btn_open_folder.configure(fg_color=c["card_bg"], text_color=c["accent_soft"], hover_color=c["border_color"])

        self.rb_shrink.configure(text_color=c["text_primary"], fg_color=c["accent_blue"])
        self.rb_standard.configure(text_color=c["text_primary"], fg_color=c["accent_blue"])
        self.switch_del.configure(text_color=c["text_primary"], progress_color=c["accent_blue"])

        self.lbl_plan_status.configure(text_color=c["text_primary"])
        self.pill_plan_badge.configure(fg_color=c["border_color"], text_color=c["text_secondary"])

        self.lbl_content_title.configure(text_color=c["accent_soft"])
        self.lbl_content_stats.configure(text_color=c["text_secondary"])

        self.detail_name_lbl.configure(text_color=c["text_primary"])
        self.detail_meta_lbl.configure(text_color=c["text_secondary"])

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=c["tree_bg"],
            foreground=c["tree_fg"],
            fieldbackground=c["tree_bg"],
            font=("Segoe UI", 9),
            rowheight=22
        )
        style.configure(
            "Treeview.Heading",
            background=c["card_bg"],
            foreground=c["text_primary"],
            font=("Segoe UI", 9, "bold")
        )
        style.map("Treeview", background=[('selected', c["tree_select"])])

        for key, (lbl_t, lbl_v, lbl_s, card) in self.metric_cards.items():
            card.configure(fg_color=c["card_bg"], border_color=c["border_color"])
            lbl_t.configure(text_color=c["text_secondary"])
            lbl_s.configure(text_color=c["text_muted"])
            if key == "m_eta":
                lbl_v.configure(text_color=c["accent_amber"])
            elif key == "m_speed":
                lbl_v.configure(text_color=c["accent_mint"])
            else:
                lbl_v.configure(text_color=c["accent_soft"])

        self.lbl_stream_file.configure(text_color=c["text_secondary"])
        self.progress_bar.configure(progress_color=c["accent_blue"])

        self.log_text.configure(bg=c["log_bg"], fg=c["log_fg"], insertbackground=c["text_primary"])

        self.btn_extract.configure(fg_color=c["accent_blue"], text_color="#FFFFFF", hover_color=c["accent_hover"])
        self.btn_stop.configure(fg_color="#DC2626", text_color="#FFFFFF", hover_color="#B91C1C")

    def _show_about_dialog(self):
        """Dedicated About Modal for Author Attribution and AI Polishing."""
        about_win = ctk.CTkToplevel(self)
        about_win.title("About ShrinkZIP")
        about_win.geometry("440x360")
        about_win.resizable(False, False)
        about_win.transient(self)
        about_win.grab_set()

        c = self.APP_THEMES[self.theme_mode]
        about_win.configure(fg_color=c["root_bg"])

        content = ctk.CTkFrame(about_win, corner_radius=10, fg_color=c["frame_bg"], border_width=1, border_color=c["border_color"])
        content.pack(fill="both", expand=True, padx=16, pady=16)

        lbl_app = ctk.CTkLabel(
            content,
            text="⚡ ShrinkZIP",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=c["accent_soft"]
        )
        lbl_app.pack(pady=(16, 2))

        lbl_v = ctk.CTkLabel(
            content,
            text="Version 2.1 • Portable Edition",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=c["text_muted"]
        )
        lbl_v.pack(pady=(0, 10))

        lbl_desc = ctk.CTkLabel(
            content,
            text="In-place archive extractor for low-disk systems.\nZIP, tar, 7z, RAR (non-solid) and split archives are\nextracted from the end while the archive is cut down.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=c["text_secondary"],
            justify="center"
        )
        lbl_desc.pack(padx=16, pady=(0, 12))

        div = ctk.CTkFrame(content, height=1, fg_color=c["border_color"])
        div.pack(fill="x", padx=20, pady=8)

        # Attribution
        attr_box = ctk.CTkFrame(content, fg_color="transparent")
        attr_box.pack(fill="x", padx=24, pady=4)

        lbl_author_t = ctk.CTkLabel(attr_box, text="Created & Designed by:", font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"), text_color=c["text_primary"])
        lbl_author_t.pack(anchor="w")

        lbl_author_v = ctk.CTkLabel(attr_box, text="Arman Ahmed", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"), text_color=c["accent_soft"])
        lbl_author_v.pack(anchor="w", padx=(10, 0), pady=(0, 6))

        lbl_ai_t = ctk.CTkLabel(attr_box, text="Polished & Assisted by:", font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"), text_color=c["text_primary"])
        lbl_ai_t.pack(anchor="w")

        lbl_ai_v = ctk.CTkLabel(attr_box, text="Claude Code & AI Agent", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=c["text_secondary"])
        lbl_ai_v.pack(anchor="w", padx=(10, 0))

        btn_close = ctk.CTkButton(
            content,
            text="Close",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            width=80,
            height=28,
            corner_radius=6,
            fg_color=c["card_bg"],
            text_color=c["text_primary"],
            hover_color=c["border_color"],
            command=about_win.destroy
        )
        btn_close.pack(pady=(14, 10))

    def _browse_archive(self):
        path = filedialog.askopenfilename(
            title="Select Archive File",
            filetypes=[
                ("Supported Archives", "*.zip *.7z *.rar *.tar *.tgz *.tar.gz *.tar.bz2 *.tar.xz *.001"),
                ("ZIP Archives", "*.zip"), ("7-Zip Archives", "*.7z"), ("RAR Archives", "*.rar"),
                ("tar Archives", "*.tar *.tgz *.tar.gz *.tar.bz2 *.tar.xz"),
                ("Split archives (first volume)", "*.001"), ("All Files", "*.*")
            ]
        )
        if path:
            norm_path = os.path.normpath(path)
            self.arch_var.set(norm_path)
            default_dest = os.path.splitext(norm_path)[0]
            self.dest_var.set(default_dest)

            threading.Thread(target=self._load_archive_contents, args=(norm_path,), daemon=True).start()

    def _fallback_listing(self, path, fmt):
        """Listing for formats the engine cannot plan in place (via 7-Zip, tarfile or py7zr)."""
        items = []
        tool_7z = get_7z_tool_path()
        if fmt in ("7z", "rar", "zip") and tool_7z:
            proc = subprocess.run([tool_7z, "l", "-slt", path], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, startupinfo=_hidden_startupinfo())
            cur, blocks = {}, []
            for line in proc.stdout.splitlines() + [""]:
                s = line.strip()
                if not s:
                    if cur:
                        blocks.append(cur)
                    cur = {}
                elif " = " in s:
                    k, v = s.split(" = ", 1)
                    cur[k] = v
            for b in blocks:
                p = b.get("Path")
                if not p or ("Type" in b and "Physical Size" in b):
                    continue
                is_dir = b.get("Folder") == "+" or p.endswith(("/", "\\"))
                items.append((p, int(b.get("Size") or 0), int(b.get("Packed Size") or 0),
                              b.get("Modified", "--")[:16], is_dir, b.get("CRC", "--"), b.get("Method", fmt)))
        elif fmt == "7z" and HAS_PY7ZR:
            with py7zr.SevenZipFile(path, mode='r') as z:
                for f in z.list():
                    items.append((f.filename, f.uncompressed or 0, f.compressed or 0, "--", f.is_directory, "--", "7z"))
        elif fmt == "rar" and HAS_RARFILE:
            with rarfile.RarFile(path) as rf:
                for f in rf.infolist():
                    items.append((f.filename, f.file_size, f.compress_size, "--", f.isdir(), "--", "RAR"))
        elif fmt not in ("tar.compressed",):
            raise RuntimeError("7-Zip engine (7z.exe) not found and no Python fallback is installed.")
        return items

    def _load_archive_contents(self, path):
        self.after(0, lambda: self.lbl_content_stats.configure(text="[ Reading archive... ]"))
        first = first_volume(path)
        items, resume_pending = [], False
        info = {"format": "?", "units": [], "inplace_ok": False, "reasons": [], "archive_size": 0,
                "batch_bytes": 0, "volumes": 1}
        note = ""
        try:
            if os.path.exists(sidecar_path(first)):
                resume_pending = True
                info["inplace_ok"] = True
                note = "Interrupted in-place extraction found. Press Start to resume it."
                info["archive_size"] = sum(os.path.getsize(p) for p in [first] if os.path.exists(first))
            else:
                info = analyze_archive(first)
                items = info["entries"]
                if items is None:
                    items = self._fallback_listing(first, info["format"])
                if info["format"] == "tar.compressed":
                    note = "Compressed tar: extracted normally (archive kept). Contents are not pre-listed."
                elif info["inplace_ok"]:
                    note = (f"Split archive: {info['volumes']} volumes, each deleted as it empties. "
                            if info["volumes"] > 1 else "")
                else:
                    note = "In-place not possible: " + "; ".join(info["reasons"]) + ". Use Keep Archive mode."

            by_path = {it[0]: it for it in items}
            total_u = sum(it[1] for it in items)
            total_c = sum(it[2] for it in items)
            largest = ("", 0)
            for it in items:
                if not it[4] and it[1] > largest[1]:
                    largest = (it[0], it[1])

            self.all_archive_items = items
            self.items_by_path = by_path
            self.total_uncompressed = total_u
            self.total_compressed = total_c
            self.plan_units = info["units"]
            self.batch_bytes = info["batch_bytes"]
            self.archive_total = info["archive_size"]
            self.inplace_supported = bool(info["inplace_ok"])
            self.inplace_note = note
            self.largest_file = largest
            self.resume_pending = resume_pending
            self.after(0, lambda: self._on_archive_loaded(path, items, total_u, total_c))
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self.lbl_content_stats.configure(text=f"[ Error reading archive: {msg} ]"))

    def _on_archive_loaded(self, path, items, total_uncompressed, total_compressed):
        archive_size = os.path.getsize(path) if os.path.exists(path) else 0
        items.sort(key=lambda x: (not x[4], x[0].lower()))

        self._filter_tree()

        file_count = sum(1 for x in items if not x[4])
        folder_count = sum(1 for x in items if x[4])

        stats_tag = (
            f"[ 📁 {folder_count:,} Folders ]  [ 📄 {file_count:,} Files ]  "
            f"[ Total: {format_bytes(total_uncompressed)} ]"
        )
        self.lbl_content_stats.configure(text=stats_tag)
        self.metric_cards["m_arch"][1].configure(text=format_bytes(archive_size))

        if self.inplace_supported:
            self.rb_shrink.configure(state="normal")
        else:
            self.mode_var.set("standard")
            self.rb_shrink.configure(state="disabled")
        self._recalculate_space_plan()

    def _filter_tree(self, *args):
        query = self.search_var.get().strip().lower()

        for item in self.tree.get_children():
            self.tree.delete(item)

        for name, size, packed, mod_time, is_dir, crc, method in self.all_archive_items:
            if not query or query in name.lower():
                prefix = "📁 " if is_dir else "📄 "
                size_str = "--" if is_dir else format_bytes(size)
                packed_str = "--" if is_dir else format_bytes(packed)
                self.tree.insert("", tk.END, values=(prefix + name, size_str, packed_str, mod_time))

    def _on_tree_select(self, event):
        """Populate the Details section for the clicked item."""
        selected = self.tree.selection()
        if not selected:
            return

        values = self.tree.item(selected[0], "values")
        if not values:
            return

        raw_name = values[0].replace("📁 ", "").replace("📄 ", "")
        item_tuple = self.items_by_path.get(raw_name)

        if item_tuple:
            name, size, packed, mod_time, is_dir, crc, method = item_tuple
            if is_dir:
                self.detail_name_lbl.configure(text=f"📁 Directory: {name}")
                self.detail_meta_lbl.configure(text=f"Type: Folder  |  Modified: {mod_time}")
            else:
                ratio = f"{(packed / size * 100):.1f}%" if size > 0 else "100%"
                self.detail_name_lbl.configure(text=f"📄 {name}")
                self.detail_meta_lbl.configure(
                    text=f"Uncompressed: {format_bytes(size)} ({size:,} B)  |  Packed: {format_bytes(packed)} ({ratio})  |  Method: {method}  |  CRC32: {crc}  |  Modified: {mod_time}"
                )

    def _browse_dest(self):
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            norm_folder = os.path.normpath(folder)
            self.dest_var.set(norm_folder)
            self._recalculate_space_plan()

    def _plan_numbers(self, dest_folder, archive_path):
        """Return (drive_label, free_disk, needed, is_inplace, base) for the current selection."""
        drive = os.path.splitdrive(os.path.abspath(dest_folder))[0] or "this disk"
        probe = dest_folder
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        _, _, free_disk = shutil.disk_usage(probe or ".")
        archive_sz = self.archive_total or os.path.getsize(archive_path)
        uncompressed_sz = self.total_uncompressed or archive_sz * 2
        margin = 1024 * 1024 * 1024
        is_shrink = (self.mode_var.get() == "shrink") and self.inplace_supported and bool(self.plan_units)
        if is_shrink:
            peak = estimate_inplace_peak(self.plan_units, archive_sz, batch_bytes=self.batch_bytes)
            return drive, free_disk, peak + margin, True, peak
        return drive, free_disk, uncompressed_sz + margin, False, uncompressed_sz

    def _recalculate_space_plan(self):
        dest_folder = self.dest_var.get().strip()
        archive_path = self.arch_var.get().strip()

        if not dest_folder or not archive_path or not os.path.isfile(archive_path):
            self.lbl_plan_status.configure(text="Select an archive file to inspect capacity and calculate space plan.")
            self.pill_plan_badge.configure(text="STANDBY", fg_color="#334155", text_color="#94A3B8")
            return

        try:
            if self.resume_pending:
                self.lbl_plan_status.configure(text=self.inplace_note)
                self.pill_plan_badge.configure(text="↻ RESUME READY", fg_color="#1E3A8A", text_color="#60A5FA")
                return

            drive, free_disk, needed, is_inplace, base = self._plan_numbers(dest_folder, archive_path)
            note = f"  {self.inplace_note}" if self.inplace_note else ""

            if is_inplace:
                big_name, big_size = self.largest_file
                hint = f" Largest file: {format_bytes(big_size)} (it must fit completely before its archive bytes are freed)." if big_size else ""
                if free_disk >= needed:
                    self.lbl_plan_status.configure(
                        text=f"Drive [{drive}] has {format_bytes(free_disk)} free. In-place peak need ~{format_bytes(base)} + 1 GB safety margin.{hint}")
                    self.pill_plan_badge.configure(text="✓ WILL FIT (IN-PLACE)", fg_color="#065F46", text_color="#34D399")
                else:
                    self.lbl_plan_status.configure(
                        text=f"Drive [{drive}] has {format_bytes(free_disk)} free, but in-place needs ~{format_bytes(base)} at its peak (+1 GB margin).{hint}")
                    self.pill_plan_badge.configure(text="⚠ LOW HEADROOM", fg_color="#7F1D1D", text_color="#F87171")
            else:
                if free_disk >= needed:
                    self.lbl_plan_status.configure(
                        text=f"Drive [{drive}] has {format_bytes(free_disk)} free. Standard extraction needs {format_bytes(base)}.{note}")
                    self.pill_plan_badge.configure(text="✓ WILL FIT (STANDARD)", fg_color="#1E3A8A", text_color="#60A5FA")
                else:
                    tip = " Switch to In-Place Extraction to fit!" if self.inplace_supported else ""
                    self.lbl_plan_status.configure(
                        text=f"Standard extraction needs {format_bytes(needed)} but [{drive}] has {format_bytes(free_disk)} free.{tip}{note}")
                    self.pill_plan_badge.configure(text="⚠ INSUFFICIENT SPACE", fg_color="#7F1D1D", text_color="#F87171")

        except Exception as e:
            self.lbl_plan_status.configure(text=f"[ Preflight Error ] {e}")

    def _log(self, text):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def _open_destination_folder(self):
        dest = self.dest_var.get().strip()
        if os.path.exists(dest):
            os.startfile(dest)

    def _start_extraction(self):
        archive_file = self.arch_var.get().strip()
        dest_folder = self.dest_var.get().strip()

        if not archive_file or not os.path.isfile(archive_file):
            messagebox.showerror("Error", "Please select a valid archive file.")
            return

        if not dest_folder:
            messagebox.showerror("Error", "Please specify an extraction folder.")
            return

        is_shrink = (self.mode_var.get() == "shrink") and self.inplace_supported
        delete_on_finish = self.del_var.get() and not is_shrink

        if not self.resume_pending:
            try:
                drive, free_space, needed, is_inplace, base = self._plan_numbers(dest_folder, archive_file)
                if free_space < needed:
                    what = (f"in-place extraction needs ~{format_bytes(base)} at its peak (plus a 1 GB margin)"
                            if is_inplace else f"standard extraction needs ~{format_bytes(base)} (plus a 1 GB margin)")
                    if not messagebox.askyesno(
                            "Low Disk Space Warning",
                            f"Drive {drive} has {format_bytes(free_space)} free, but {what}.\n\n"
                            "If the disk fills up the run stops safely and can be resumed after you free space.\n\n"
                            "Proceed anyway?"):
                        return
            except Exception:
                pass

        self.btn_extract.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_open_folder.pack_forget()
        self.entry_arch.configure(state="disabled")
        self.entry_dest.configure(state="disabled")
        self.btn_browse_arch.configure(state="disabled")
        self.btn_browse_dest.configure(state="disabled")
        self.rb_shrink.configure(state="disabled")
        self.rb_standard.configure(state="disabled")
        self.switch_del.configure(state="disabled")
        self.progress_bar.set(0)
        self.metric_cards["m_eta"][1].configure(text="CALCULATING...")
        self._log("=== Extraction Started ===")

        self.worker = ShrinkZipEngineWorker(
            archive_path=archive_file,
            extract_dir=dest_folder,
            mode_shrink=is_shrink,
            delete_on_finish=delete_on_finish,
            conflict_mode="overwrite",
            msg_queue=self.msg_queue
        )
        self.worker.start()

    def _stop_extraction(self):
        if self.worker and self.worker.is_alive():
            self._log("[CMD] Cancellation requested. Stopping safely...")
            self.worker.request_stop()
            self.btn_stop.configure(state="disabled")

    def _reset_ui(self):
        self.btn_extract.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.entry_arch.configure(state="normal")
        self.entry_dest.configure(state="normal")
        self.btn_browse_arch.configure(state="normal")
        self.btn_browse_dest.configure(state="normal")
        self.rb_shrink.configure(state="normal" if self.inplace_supported else "disabled")
        self.rb_standard.configure(state="normal")
        self.switch_del.configure(state="normal")
        self.worker = None

    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == 'log':
                    self._log(data)
                elif msg_type == 'progress':
                    self.progress_bar.set(data['progress_pct'])
                    pct_100 = int(data['progress_pct'] * 100)
                    self.lbl_stream_file.configure(
                        text=f"[{pct_100}%] [{data['extracted_count']:,}/{data['total_files']:,}] >> {data['current_file']}"
                    )
                    self.metric_cards["m_arch"][1].configure(text=format_bytes(data['current_archive_size']))
                    self.metric_cards["m_extr"][1].configure(text=format_bytes(data['extracted_bytes']))
                    self.metric_cards["m_speed"][1].configure(text=data['speed'])
                    self.metric_cards["m_eta"][1].configure(text=data['eta'])
                elif msg_type == 'done':
                    self._log(f"[SUCCESS] {data}")
                    self.progress_bar.set(1.0)
                    self.lbl_stream_file.configure(text="Extraction completed successfully.")
                    self.metric_cards["m_eta"][1].configure(text="COMPLETE")
                    self.btn_open_folder.pack(side="left", padx=4)
                    messagebox.showinfo("Extraction Finished", data)
                    self._reset_ui()
                elif msg_type == 'stopped':
                    self._log(f"[HALTED] {data}")
                    self.lbl_stream_file.configure(text="Process stopped by user.")
                    self.metric_cards["m_eta"][1].configure(text="PAUSED")
                    messagebox.showwarning("Extraction Stopped", data)
                    self._reset_ui()
                elif msg_type == 'error':
                    self._log(f"[ERROR] {data}")
                    messagebox.showerror("Extraction Error", f"Extraction halted due to error:\n{data}")
                    self._reset_ui()
        except queue.Empty:
            pass
        finally:
            self.after(80, self._poll_queue)


if __name__ == "__main__":
    app = ShrinkZipApp()
    app.mainloop()
