#!/usr/bin/python3
"""Desktop traffic light for Paseo agents (Linux port of kongkongyo/agent-status-light).

Red: an agent is waiting for permission or hit an error that has not been viewed.
Yellow: agents are running.  Green: top-level agents finished and not yet viewed in Paseo.
"""

import fcntl
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import quote

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

APP_NAME = "Agent Status Light"
APP_ID = "agent-status-light"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")
ICON_PATH = os.path.join(APP_DIR, "icon.png")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOCK_PATH = os.path.join(DATA_DIR, "instance.lock")
PID_PATH = os.path.join(DATA_DIR, "status-light.pid")
LOG_PATH = os.path.join(LOG_DIR, "status-light.log")
BRIDGE_SCRIPT = os.path.join(APP_DIR, "bridge", "paseo-bridge.mjs")
CLAUDE_PROJECTS_DIR = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                                   "projects")
TRANSCRIPT_CHUNK = 65536
TRANSCRIPT_SCAN_LIMIT = 8 * 1024 * 1024
TRANSCRIPT_POLL_SECONDS = 2

# Geometry and timing follow the original StatusLightForm.
UI_SCALE = 0.88
HORIZONTAL_SIZE = (167, 96)
VERTICAL_SIZE = (96, 167)
BREATHING_INTERVAL_MS = 60
BREATHING_CYCLE_TICKS = 36
BREATHING_MIN_INTENSITY = 0.65
FLASH_INTERVAL_MS = 180
FLASH_TICKS = 10
COUNT_FONT = "Ubuntu,Noto Sans,Sans Bold"

# A parent whose subagents just stopped is usually re-prompted within seconds;
# hold its green until then so a single completion does not chime twice.
CHILD_SETTLE_SECONDS = 6.0
ACTIVE_STATUSES = ("running", "initializing")
LAMPS = ("confirm", "working", "done")
TOOLTIP_AGENTS_PER_LAMP = 8
NOTIFY_BATCH_LIMIT = 3
SIZE_CHOICES = (100, 125, 150, 200)

DEFAULT_SETTINGS = {
    "skin": "system",
    "orientation": "horizontal",
    "sizePercent": 100,
    "breathing": True,
    "soundEnabled": True,
    "confirmSoundFile": "",
    "doneSoundFile": "",
    "desktopNotifications": True,
    "confirmColor": "#FF5252",
    "workingColor": "#FFC845",
    "doneColor": "#20C36A",
    "waitingColor": "",
    "windowX": None,
    "windowY": None,
}


def rgba(r, g, b, a=255):
    return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)


PALETTES = {
    "dark": {
        "body": True,
        "body_start": rgba(48, 52, 55),
        "body_end": rgba(12, 14, 15),
        "border": rgba(6, 8, 9),
        "rim": rgba(6, 8, 9),
        "inactive_lens": rgba(38, 43, 45),
        "lens_shadow": rgba(13, 15, 16),
        "inactive_shine_alpha": 30,
    },
    # Windows SystemColors used by the original light skin.
    "light": {
        "body": True,
        "body_start": rgba(255, 255, 255),
        "body_end": rgba(240, 240, 240),
        "border": rgba(160, 160, 160),
        "rim": rgba(105, 105, 105),
        "inactive_lens": rgba(227, 227, 227),
        "lens_shadow": rgba(160, 160, 160),
        "inactive_shine_alpha": 85,
    },
    "transparent": {
        "body": False,
        "rim": rgba(92, 104, 112, 135),
        "inactive_lens": rgba(214, 222, 228, 120),
        "lens_shadow": rgba(126, 138, 146, 110),
        "inactive_shine_alpha": 70,
    },
}


def log(message):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1000000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            settings.update({key: value for key, value in stored.items() if key in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        temp_path = SETTINGS_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, SETTINGS_PATH)
    except OSError as error:
        log("settings save failed: %s" % error)


def parse_hex_color(value, fallback):
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", (value or "").strip())
    if not match:
        return fallback
    digits = match.group(1)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def hex_color(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


# ---------------------------------------------------------------- rendering


def ellipse(cr, x, y, width, height):
    cr.save()
    cr.translate(x + width / 2.0, y + height / 2.0)
    cr.scale(width / 2.0, height / 2.0)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


def rounded_rect(cr, x, y, width, height, radius):
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def gdi_gradient(x, y, width, height, angle):
    """GDI+ LinearGradientBrush(rect, angle): the end lines touch the rect corners."""
    radians = math.radians(angle)
    dx, dy = math.cos(radians), math.sin(radians)
    cx, cy = x + width / 2.0, y + height / 2.0
    projections = [(px - cx) * dx + (py - cy) * dy
                   for px, py in ((x, y), (x + width, y), (x, y + height), (x + width, y + height))]
    low, high = min(projections), max(projections)
    return cairo.LinearGradient(cx + dx * low, cy + dy * low, cx + dx * high, cy + dy * high)


def scale_alpha(alpha, intensity):
    return max(0, min(255, int(round(alpha * intensity))))


def paint_count(cr, x, y, size, count):
    text = "99+" if count > 99 else str(count)
    pixels = 14 if len(text) == 1 else (12 if len(text) == 2 else 9)
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(COUNT_FONT)
    font.set_absolute_size(pixels * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_text(text, -1)
    ink, _logical = layout.get_pixel_extents()
    left = x + size / 2.0 - (ink.x + ink.width / 2.0)
    top = y + size / 2.0 - (ink.y + ink.height / 2.0)
    cr.move_to(left, top + 1)
    cr.set_source_rgba(0, 0, 0, 90 / 255.0)
    PangoCairo.show_layout(cr, layout)
    cr.move_to(left, top)
    cr.set_source_rgba(0, 0, 0, 1)
    PangoCairo.show_layout(cr, layout)


def paint_lamp(cr, x, y, size, color, active, count, palette, inactive_color, intensity):
    intensity = min(1.0, max(BREATHING_MIN_INTENSITY, intensity))
    red, green, blue = color
    ellipse(cr, x, y, size, size)
    cr.set_source_rgba(*palette["rim"])
    cr.fill()

    if active:
        for ring in (3, 2, 1):
            grow = 3 * ring
            alpha = scale_alpha(28 + 18 * (4 - ring), intensity)
            ellipse(cr, x - grow, y - grow, size + grow * 2, size + grow * 2)
            cr.set_source_rgba(red / 255.0, green / 255.0, blue / 255.0, alpha / 255.0)
            cr.fill()

    inner_x, inner_y, inner_size = x + 6, y + 6, size - 12
    if active:
        base = tuple(min(255, int(round(channel * intensity))) / 255.0 for channel in color) + (1.0,)
    else:
        base = inactive_color
    gradient = gdi_gradient(inner_x, inner_y, inner_size, inner_size, 65)
    gradient.add_color_stop_rgba(0, *base)
    gradient.add_color_stop_rgba(1, *palette["lens_shadow"])
    ellipse(cr, inner_x, inner_y, inner_size, inner_size)
    cr.set_source(gradient)
    cr.fill()

    shine_alpha = scale_alpha(155, intensity) if active else palette["inactive_shine_alpha"]
    ellipse(cr, x + 13, y + 11, 11, 7)
    cr.set_source_rgba(1, 1, 1, shine_alpha / 255.0)
    cr.fill()

    if active and count > 1:
        paint_count(cr, inner_x, inner_y, inner_size, count)


def body_bounds(vertical):
    return (19, 9, 58, 144) if vertical else (9, 19, 144, 58)


def lamp_positions(vertical):
    return [(32, 20), (32, 65), (32, 110)] if vertical else [(20, 32), (65, 32), (110, 32)]


def paint_status_light(cr, palette, vertical, lamps, inactive_color):
    """lamps: (rgb, active, count, intensity) for red, yellow, green, in unscaled units."""
    if palette["body"]:
        x, y, width, height = body_bounds(vertical)
        rounded_rect(cr, x, y, width, height, 13)
        if vertical:
            gradient = cairo.LinearGradient(x, y, x, y + height)
        else:
            gradient = cairo.LinearGradient(x, y, x + width, y)
        gradient.add_color_stop_rgba(0, *palette["body_start"])
        gradient.add_color_stop_rgba(1, *palette["body_end"])
        cr.set_source(gradient)
        cr.fill_preserve()
        cr.set_source_rgba(*palette["border"])
        cr.set_line_width(2)
        cr.stroke()
    for (x, y), (color, active, count, intensity) in zip(lamp_positions(vertical), lamps):
        paint_lamp(cr, x, y, 32, color, active, count, palette, inactive_color, intensity)


# ---------------------------------------------------------------- helpers


def run_detached(argv):
    try:
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
    except GLib.Error as error:
        log("spawn failed %s: %s" % (argv[0], error.message))


def sound_theme_file(name):
    themes = []
    source = Gio.SettingsSchemaSource.get_default()
    if source and source.lookup("org.gnome.desktop.sound", True):
        themes.append(Gio.Settings.new("org.gnome.desktop.sound").get_string("theme-name"))
    themes += ["freedesktop", "Yaru"]
    for theme in themes:
        for ext in (".oga", ".ogg", ".wav"):
            path = os.path.join("/usr/share/sounds", theme, "stereo", name + ext)
            if os.path.isfile(path):
                return path
    return None


def play_sound_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".wav", ".oga", ".ogg", ".flac") and shutil.which("paplay"):
        run_detached(["paplay", path])
    elif shutil.which("gst-play-1.0"):
        run_detached(["gst-play-1.0", "--no-interactive", "-q", path])
    elif shutil.which("canberra-gtk-play"):
        run_detached(["canberra-gtk-play", "-f", path])


def node_major_version(path):
    try:
        result = subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, timeout=5)
        return int(result.stdout.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def find_node():
    candidates = []
    if os.environ.get("ASL_NODE"):
        candidates.append(os.environ["ASL_NODE"])
    # Desktop sessions usually lack the nvm PATH, so look there explicitly.
    nvm_nodes = glob.glob(os.path.expanduser("~/.nvm/versions/node/v*/bin/node"))
    nvm_nodes.sort(key=lambda path: [int(part) for part in re.findall(r"\d+", path.split("/")[-3])], reverse=True)
    candidates += nvm_nodes
    if shutil.which("node"):
        candidates.append(shutil.which("node"))
    candidates += ["/usr/local/bin/node", "/usr/bin/node"]
    for path in candidates:
        if path and os.access(path, os.X_OK) and node_major_version(path) >= 18:
            return path
    return None


def truncate(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Bridge:
    """Runs paseo-bridge.mjs and hands its JSON lines to the GTK main loop."""

    def __init__(self, on_message, on_exit):
        self.on_message = on_message
        self.on_exit = on_exit
        self.proc = None

    def start(self):
        node = find_node()
        if not node:
            raise RuntimeError("找不到 Node.js 18 以上版本（可用环境变量 ASL_NODE 指定）")
        self.proc = subprocess.Popen([node, BRIDGE_SCRIPT], cwd=os.path.dirname(BRIDGE_SCRIPT),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log("bridge started pid=%d node=%s" % (self.proc.pid, node))
        threading.Thread(target=self._read_stdout, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()

    def _read_stdout(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                log("bridge stdout: " + line[:300])
                continue
            GLib.idle_add(self.on_message, message)
        proc.wait()
        GLib.idle_add(self.on_exit, proc, proc.returncode)

    @staticmethod
    def _read_stderr(proc):
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log("bridge: " + line[:500])

    def stop(self):
        proc, self.proc = self.proc, None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


class ClaudeTranscripts:
    """Reads the tail of Claude Code session files for API errors Paseo does not report.

    Claude Code logs each failed request it will retry as a system "api_error" entry,
    and a turn that gave up as an assistant message flagged isApiErrorMessage. Paseo
    keeps such agents "running" or "finished", so the light checks the files itself.
    """

    def __init__(self):
        self.paths = {}
        self.cache = {}

    def path_for(self, session_id, cwd):
        path = self.paths.get(session_id)
        if path and os.path.isfile(path):
            return path
        name = session_id + ".jsonl"
        candidates = []
        if cwd:
            candidates.append(os.path.join(CLAUDE_PROJECTS_DIR, re.sub(r"[^A-Za-z0-9]", "-", cwd), name))
        candidates.extend(glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", glob.escape(name))))
        path = next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
        if path:
            self.paths[session_id] = path
        return path

    def state(self, session_id, cwd):
        """Returns None, ("retrying", entry) or ("failed", entry) for the session's main chain."""
        path = self.path_for(session_id, cwd)
        if not path:
            return None
        try:
            info = os.stat(path)
        except OSError:
            return None
        key = (info.st_size, info.st_mtime_ns)
        cached = self.cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        result = self.read_last_state(path, info.st_size)
        self.cache[path] = (key, result)
        return result

    def forget_except(self, session_ids):
        self.paths = {key: value for key, value in self.paths.items() if key in session_ids}
        live = set(self.paths.values())
        self.cache = {key: value for key, value in self.cache.items() if key in live}

    @staticmethod
    def entry_state(entry):
        """Classifies one entry; returns "skip" for entries that do not end an exchange."""
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            return "skip"
        kind = entry.get("type")
        if kind == "system":
            subtype = entry.get("subtype")
            if subtype == "api_error":
                return ("retrying", entry)
            return None if subtype == "compact_boundary" else "skip"
        if kind == "assistant":
            return ("failed", entry) if entry.get("isApiErrorMessage") else None
        if kind == "user":
            return None
        return "skip"

    def read_last_state(self, path, size):
        position = size
        leftover = b""
        try:
            with open(path, "rb") as handle:
                while position > 0 and size - position < TRANSCRIPT_SCAN_LIMIT:
                    step = min(TRANSCRIPT_CHUNK, position)
                    position -= step
                    handle.seek(position)
                    lines = (handle.read(step) + leftover).split(b"\n")
                    # The first piece may be cut mid-line unless the file start was reached.
                    leftover = lines.pop(0) if position > 0 else b""
                    for line in reversed(lines):
                        if not line.strip():
                            continue
                        try:
                            state = self.entry_state(json.loads(line))
                        except ValueError:
                            continue
                        if state != "skip":
                            return state
        except OSError:
            return None
        return None


def describe_api_error(state):
    kind, entry = state
    if kind == "retrying":
        error = entry.get("error")
        if isinstance(error, dict):
            text = error.get("formatted") or error.get("message") or ""
        else:
            text = str(error or "")
        attempt = entry.get("retryAttempt")
        limit = entry.get("maxRetries")
        progress = "（第 %s/%s 次）" % (attempt, limit) if attempt and limit else ""
        return "API 报错，正在重试%s：%s" % (progress, truncate(text, 60))
    content = (entry.get("message") or {}).get("content")
    text = ""
    if isinstance(content, list):
        text = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    elif isinstance(content, str):
        text = content
    return "API 报错后停止：" + truncate(text.replace("API Error:", "").strip(), 60)


# ---------------------------------------------------------------- window


class StatusLight(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title=APP_NAME)
        self.settings = load_settings()
        self.quitting = False

        self.connected = False
        self.connection_detail = "正在连接 Paseo…"
        self.daemon_url = "ws://127.0.0.1:6767/ws"
        self.server_id = None
        self.providers = {}
        self.agents = {}
        self.baseline_pending = True

        self.groups = {lamp: [] for lamp in LAMPS}
        self.transcripts = ClaudeTranscripts()
        self.api_errors = {}
        self.child_busy_at = {}
        self.dismissed_done = set()
        self.settle_source = 0

        self.breathing_tick = 0
        self.breathing_source = 0
        self.flash_source = 0
        self.flash_ticks = 0
        self.flash_visible = True
        self.save_position_source = 0
        self.menu = None

        self.bridge = Bridge(self.on_bridge_message, self.on_bridge_exit)
        self.bridge_backoff = 2
        self.bridge_restart_source = 0

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_app_paintable(True)
        self.stick()
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_button_press)
        self.connect("configure-event", self.on_configure)
        self.connect("query-tooltip", self.on_query_tooltip)
        self.connect("delete-event", lambda *_args: self.quit_app() or True)
        self.set_has_tooltip(True)
        gtk_settings = Gtk.Settings.get_default()
        gtk_settings.connect("notify::gtk-theme-name", lambda *_args: self.apply_geometry())
        gtk_settings.connect("notify::gtk-application-prefer-dark-theme", lambda *_args: self.apply_geometry())

        self.apply_geometry()
        self.move(*self.startup_position())
        self.show_all()
        self.start_bridge()
        GLib.timeout_add_seconds(TRANSCRIPT_POLL_SECONDS, self.on_transcript_poll)

    # ------------------------------------------------------------ geometry

    def scale(self):
        return UI_SCALE * self.settings.get("sizePercent", 100) / 100.0

    def vertical(self):
        return self.settings.get("orientation") == "vertical"

    def window_size(self):
        base = VERTICAL_SIZE if self.vertical() else HORIZONTAL_SIZE
        return tuple(int(math.ceil(value * self.scale())) for value in base)

    def effective_skin(self):
        skin = self.settings.get("skin", "system")
        if skin in PALETTES:
            return skin
        gtk_settings = Gtk.Settings.get_default()
        theme = (gtk_settings.props.gtk_theme_name or "").lower()
        if "dark" in theme or gtk_settings.props.gtk_application_prefer_dark_theme:
            return "dark"
        return "light"

    def palette(self):
        return PALETTES[self.effective_skin()]

    def apply_geometry(self):
        width, height = self.window_size()
        self.set_size_request(width, height)
        self.resize(width, height)
        scale = self.scale()
        if self.palette()["body"]:
            x, y, w, h = body_bounds(self.vertical())
            boxes = [(x - 1, y - 1, w + 2, h + 2)]
        else:
            boxes = [(x, y, 32, 32) for x, y in lamp_positions(self.vertical())]
        rects = [cairo.RectangleInt(int(math.floor(x * scale)), int(math.floor(y * scale)),
                                    int(math.ceil(w * scale)), int(math.ceil(h * scale)))
                 for x, y, w, h in boxes]
        self.input_shape_combine_region(cairo.Region(rects))
        self.queue_draw()

    def monitor_workareas(self):
        display = Gdk.Display.get_default()
        return [display.get_monitor(i).get_workarea() for i in range(display.get_n_monitors())]

    def startup_position(self):
        width, height = self.window_size()
        x, y = self.settings.get("windowX"), self.settings.get("windowY")
        if isinstance(x, int) and isinstance(y, int):
            for area in self.monitor_workareas():
                if x < area.x + area.width and x + width > area.x and y < area.y + area.height and y + height > area.y:
                    return x, y
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        return area.x + max(0, (area.width - width) // 2), area.y + max(0, (area.height - height) // 2)

    def on_configure(self, _widget, _event):
        if self.save_position_source:
            GLib.source_remove(self.save_position_source)
        self.save_position_source = GLib.timeout_add(500, self.save_position)
        return False

    def save_position(self):
        self.save_position_source = 0
        x, y = self.get_position()
        if (x, y) != (self.settings.get("windowX"), self.settings.get("windowY")):
            self.settings["windowX"], self.settings["windowY"] = x, y
            save_settings(self.settings)
        return False

    # ------------------------------------------------------------ drawing

    def lamp_color(self, lamp):
        return parse_hex_color(self.settings.get(lamp + "Color"), parse_hex_color(DEFAULT_SETTINGS[lamp + "Color"], (0, 0, 0)))

    def inactive_color(self, palette):
        waiting = self.settings.get("waitingColor")
        if waiting:
            return tuple(channel / 255.0 for channel in parse_hex_color(waiting, (0, 0, 0))) + (1.0,)
        return palette["inactive_lens"]

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        scale = self.scale()
        cr.scale(scale, scale)
        palette = self.palette()
        breathing = self.breathing_intensity()
        done_intensity = 1.0 if self.flash_source else breathing
        counts = [len(self.groups[lamp]) for lamp in LAMPS]
        lamps = [
            (self.lamp_color("confirm"), counts[0] > 0, counts[0], breathing),
            (self.lamp_color("working"), counts[1] > 0, counts[1], breathing),
            (self.lamp_color("done"), counts[2] > 0 and self.flash_visible, counts[2], done_intensity),
        ]
        paint_status_light(cr, palette, self.vertical(), lamps, self.inactive_color(palette))
        return True

    def has_breathing_light(self):
        if not self.settings.get("breathing", True):
            return False
        return bool(self.groups["confirm"] or self.groups["working"]
                    or (self.groups["done"] and self.flash_visible and not self.flash_source))

    def breathing_intensity(self):
        if not self.has_breathing_light():
            return 1.0
        phase = 2 * math.pi * self.breathing_tick / BREATHING_CYCLE_TICKS
        ratio = (math.sin(phase - math.pi / 2) + 1) / 2
        return BREATHING_MIN_INTENSITY + (1 - BREATHING_MIN_INTENSITY) * ratio

    def update_breathing_timer(self):
        if self.has_breathing_light():
            if not self.breathing_source:
                self.breathing_source = GLib.timeout_add(BREATHING_INTERVAL_MS, self.on_breathing_tick)
            return
        self.breathing_tick = 0
        if self.breathing_source:
            GLib.source_remove(self.breathing_source)
            self.breathing_source = 0

    def on_breathing_tick(self):
        if not self.has_breathing_light():
            self.breathing_tick = 0
            self.breathing_source = 0
            self.queue_draw()
            return False
        self.breathing_tick = (self.breathing_tick + 1) % BREATHING_CYCLE_TICKS
        self.queue_draw()
        return True

    def start_done_flash(self):
        self.flash_ticks = FLASH_TICKS
        self.flash_visible = True
        if self.flash_source:
            GLib.source_remove(self.flash_source)
        self.flash_source = GLib.timeout_add(FLASH_INTERVAL_MS, self.on_flash_tick)
        self.update_breathing_timer()
        self.queue_draw()

    def on_flash_tick(self):
        if self.flash_ticks <= 0:
            self.flash_visible = True
            self.flash_source = 0
            self.update_breathing_timer()
            self.queue_draw()
            return False
        self.flash_visible = not self.flash_visible
        self.flash_ticks -= 1
        self.queue_draw()
        return True

    # ------------------------------------------------------------ Paseo state

    def start_bridge(self):
        self.bridge_restart_source = 0
        try:
            self.bridge.start()
        except (OSError, RuntimeError) as error:
            log("bridge start failed: %s" % error)
            self.set_disconnected(str(error))
            self.schedule_bridge_restart()
        return False

    def schedule_bridge_restart(self):
        if self.quitting or self.bridge_restart_source:
            return
        delay = self.bridge_backoff
        self.bridge_backoff = min(self.bridge_backoff * 2, 60)
        self.bridge_restart_source = GLib.timeout_add_seconds(delay, self.start_bridge)

    def on_bridge_exit(self, proc, code):
        if self.quitting or proc is not self.bridge.proc:
            return False
        self.bridge.proc = None
        log("bridge exited code=%s" % code)
        self.set_disconnected("桥接进程退出（代码 %s），正在重启" % code)
        self.schedule_bridge_restart()
        return False

    def on_bridge_message(self, message):
        kind = message.get("type")
        if kind == "connection":
            self.daemon_url = message.get("url") or self.daemon_url
            status = message.get("status")
            if status == "connected":
                self.bridge_backoff = 2
                self.server_id = message.get("serverId") or self.server_id
                log("connected to %s" % self.daemon_url)
            elif status == "disconnected":
                if self.connected:
                    log("disconnected: %s" % message.get("reason"))
                self.set_disconnected(message.get("reason") or "")
            elif status == "connecting" and not self.connected:
                self.connection_detail = "正在连接…"
        elif kind == "state":
            if not self.connected:
                self.connected = True
                self.baseline_pending = True
            self.server_id = message.get("serverId") or self.server_id
            self.providers = message.get("providers") or self.providers
            self.agents = {agent["id"]: agent for agent in message.get("agents", []) if agent.get("id")}
            self.refresh_api_errors()
            self.recompute(notify=not self.baseline_pending)
            self.baseline_pending = False
        return False

    def refresh_api_errors(self):
        """Updates API errors from Claude session files; returns True when they changed."""
        errors = {}
        sessions = set()
        for agent in self.agents.values():
            session_id = agent.get("sessionId")
            if agent.get("provider") != "claude" or not session_id:
                continue
            if agent.get("status") not in ACTIVE_STATUSES and not agent.get("requiresAttention"):
                continue
            sessions.add(session_id)
            state = self.transcripts.state(session_id, agent.get("cwd"))
            if state:
                errors[agent["id"]] = state
        self.transcripts.forget_except(sessions)
        changed = errors.keys() != self.api_errors.keys() or any(
            errors[key][0] != self.api_errors[key][0] or errors[key][1] is not self.api_errors[key][1]
            for key in errors)
        self.api_errors = errors
        return changed

    def on_transcript_poll(self):
        if self.quitting:
            return False
        if self.connected and self.refresh_api_errors():
            self.recompute()
        return True

    def set_disconnected(self, reason):
        self.connected = False
        self.connection_detail = reason
        self.agents = {}
        self.recompute(notify=False)

    def classify(self, now):
        agents = self.agents
        children = {}
        for agent in agents.values():
            parent = agent.get("parentAgentId")
            if parent:
                children.setdefault(parent, []).append(agent)

        def busy(agent):
            return (agent.get("status") in ACTIVE_STATUSES or agent.get("pendingPermissions", 0) > 0
                    or agent.get("runningSubagents", 0) > 0)

        def has_busy_descendant(agent_id, seen):
            for child in children.get(agent_id, ()):
                if child["id"] in seen:
                    continue
                seen.add(child["id"])
                if busy(child) or has_busy_descendant(child["id"], seen):
                    return True
            return False

        groups = {lamp: [] for lamp in LAMPS}
        finished = []
        next_settle = None
        for agent in agents.values():
            # Paseo keeps the first unread reason until the agent is viewed, so an
            # agent that errors after an unviewed finish still reads "finished".
            unread = bool(agent.get("requiresAttention"))
            running = agent.get("status") in ACTIVE_STATUSES
            api_error = self.api_errors.get(agent["id"], (None,))[0]
            if agent.get("pendingPermissions", 0) > 0:
                groups["confirm"].append(agent)
            elif running and api_error == "retrying":
                groups["confirm"].append(agent)
            elif running:
                groups["working"].append(agent)
            elif unread and (agent.get("attentionReason") == "error" or agent.get("status") == "error"
                             or api_error == "failed"):
                groups["confirm"].append(agent)
            elif agent.get("runningSubagents", 0) > 0:
                # Background subagents keep working after the parent's turn ended.
                groups["working"].append(agent)
                if unread and not agent.get("parentAgentId"):
                    self.child_busy_at[agent["id"]] = now
            elif unread and not agent.get("parentAgentId"):
                agent_id = agent["id"]
                if has_busy_descendant(agent_id, {agent_id}):
                    self.child_busy_at[agent_id] = now
                    continue
                wait = CHILD_SETTLE_SECONDS - (now - self.child_busy_at.get(agent_id, -CHILD_SETTLE_SECONDS))
                if wait > 0:
                    next_settle = wait if next_settle is None else min(next_settle, wait)
                    continue
                finished.append(agent)

        # A local dismissal lasts only while that agent stays green.
        self.dismissed_done &= {agent["id"] for agent in finished}
        groups["done"] = [agent for agent in finished if agent["id"] not in self.dismissed_done]
        self.child_busy_at = {key: value for key, value in self.child_busy_at.items() if key in agents}
        for lamp in LAMPS:
            groups[lamp].sort(key=lambda agent: agent.get("attentionTimestamp") or agent.get("updatedAt") or "",
                              reverse=True)
        return groups, next_settle

    def recompute(self, notify=True):
        groups, next_settle = self.classify(time.monotonic())
        if self.settle_source:
            GLib.source_remove(self.settle_source)
            self.settle_source = 0
        if next_settle is not None:
            self.settle_source = GLib.timeout_add(int(next_settle * 1000) + 100, self.on_settle_timeout)

        previous = {lamp: {agent["id"] for agent in self.groups[lamp]} for lamp in LAMPS}
        new_confirm = [agent for agent in groups["confirm"] if agent["id"] not in previous["confirm"]]
        new_done = [agent for agent in groups["done"] if agent["id"] not in previous["done"]]
        old_counts = [len(self.groups[lamp]) for lamp in LAMPS]
        self.groups = groups
        counts = [len(groups[lamp]) for lamp in LAMPS]
        if counts != old_counts:
            log("lamps red=%d yellow=%d green=%d" % tuple(counts))

        if old_counts[2] == 0 and counts[2] > 0:
            self.start_done_flash()
        if notify:
            if new_confirm:
                self.play_sound("confirm")
            elif new_done:
                self.play_sound("done")
            if self.settings.get("desktopNotifications"):
                self.send_notifications("confirm", new_confirm)
                self.send_notifications("done", new_done)
        self.update_breathing_timer()
        self.queue_draw()
        self.trigger_tooltip_query()

    def on_settle_timeout(self):
        self.settle_source = 0
        self.recompute()
        return False

    # ------------------------------------------------------------ describing agents

    def provider_label(self, provider):
        entry = self.providers.get(provider)
        if isinstance(entry, dict) and entry.get("label"):
            return entry["label"]
        return (provider or "?").capitalize()

    def agent_title(self, agent, limit=34):
        return truncate(agent.get("title") or "(未命名)", limit)

    def agent_note(self, agent, lamp):
        if lamp == "confirm":
            if agent.get("pendingPermissions", 0) > 0:
                return "等待权限确认"
            if agent["id"] in self.api_errors:
                return describe_api_error(self.api_errors[agent["id"]])
            return "出错" + ("：" + truncate(agent["lastError"], 40) if agent.get("lastError") else "")
        if lamp == "working":
            notes = []
            if agent.get("parentAgentId"):
                notes.append("子代理")
            subagents = agent.get("runningSubagents", 0)
            if subagents:
                running = agent.get("status") in ACTIVE_STATUSES
                notes.append(("含 %d 个子代理" if running else "主代理已停，%d 个子代理在后台跑") % subagents)
            return "，".join(notes)
        return ""

    def agent_line(self, agent, lamp):
        note = self.agent_note(agent, lamp)
        line = "%s · %s" % (self.provider_label(agent.get("provider")), self.agent_title(agent))
        return line + ("（%s）" % note if note else "")

    def tooltip_text(self):
        if not self.connected:
            lines = ["未连接 Paseo（%s）" % self.daemon_url]
            if self.connection_detail:
                lines.append(truncate(self.connection_detail, 80))
            lines.append("所有灯熄灭，正在自动重连…")
            return "\n".join(lines)
        titles = {"confirm": "红灯·需处理", "working": "黄灯·运行中", "done": "绿灯·已完成未查看"}
        lines = ["Paseo 代理状态"]
        for lamp in LAMPS:
            agents = self.groups[lamp]
            if not agents:
                lines.append("%s：0" % titles[lamp])
                continue
            per_provider = OrderedDict()
            for agent in agents:
                label = self.provider_label(agent.get("provider"))
                per_provider[label] = per_provider.get(label, 0) + 1
            breakdown = "，".join("%s %d" % item for item in per_provider.items())
            lines.append("%s：%d（%s）" % (titles[lamp], len(agents), breakdown))
            for agent in agents[:TOOLTIP_AGENTS_PER_LAMP]:
                lines.append("    " + self.agent_line(agent, lamp))
            if len(agents) > TOOLTIP_AGENTS_PER_LAMP:
                lines.append("    … 另有 %d 个" % (len(agents) - TOOLTIP_AGENTS_PER_LAMP))
        return "\n".join(lines)

    def on_query_tooltip(self, _widget, _x, _y, _keyboard, tooltip):
        tooltip.set_text(self.tooltip_text())
        return True

    # ------------------------------------------------------------ sound and notifications

    def play_sound(self, lamp):
        if not self.settings.get("soundEnabled", True):
            return
        path = self.settings.get(lamp + "SoundFile") or ""
        if not os.path.isfile(path):
            path = sound_theme_file("dialog-warning")
        if path:
            log("sound %s %s" % (lamp, path))
            play_sound_file(path)
        else:
            log("sound %s: no sound file found" % lamp)

    def send_notifications(self, lamp, agents):
        if not agents:
            return
        icon = "dialog-warning" if lamp == "confirm" else "emblem-default"
        if len(agents) > NOTIFY_BATCH_LIMIT:
            summary = ("%d 个代理需要处理" if lamp == "confirm" else "%d 个代理已完成") % len(agents)
            body = "\n".join(self.agent_line(agent, lamp) for agent in agents[:6])
            run_detached(["notify-send", "-a", APP_NAME, "-i", icon, summary, body])
            return
        for agent in agents:
            if lamp == "confirm":
                summary = "需要处理：" + self.agent_title(agent, 60)
                body = "%s · %s" % (self.provider_label(agent.get("provider")), self.agent_note(agent, lamp))
            else:
                summary = "已完成：" + self.agent_title(agent, 60)
                body = "%s · 在 Paseo 中查看后绿灯熄灭" % self.provider_label(agent.get("provider"))
            run_detached(["notify-send", "-a", APP_NAME, "-i", icon, summary, body])

    # ------------------------------------------------------------ input and menu

    def on_button_press(self, _widget, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        if event.button == 3:
            self.menu = self.build_menu()
            self.menu.popup_at_pointer(event)
            return True
        return False

    def open_agent(self, agent_id):
        if not self.server_id:
            return
        uri = "paseo://h/%s/agent/%s" % (quote(self.server_id, safe=""), quote(agent_id, safe=""))
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            log("open %s failed: %s" % (uri, error.message))

    @staticmethod
    def open_paseo():
        app = Gio.DesktopAppInfo.new("paseo.desktop")
        try:
            if app is not None:
                app.launch([], None)
            else:
                run_detached([os.path.expanduser("~/Applications/Paseo-x86_64.AppImage"), "--no-sandbox"])
        except GLib.Error as error:
            log("open Paseo failed: %s" % error.message)

    @staticmethod
    def menu_item(label, callback=None, sensitive=True):
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if callback is not None:
            item.connect("activate", lambda *_args: callback())
        return item

    @staticmethod
    def check_item(label, active, callback):
        item = Gtk.CheckMenuItem(label=label)
        item.set_active(active)
        item.connect("toggled", lambda widget: callback(widget.get_active()))
        return item

    @staticmethod
    def radio_submenu(label, choices, current, callback):
        parent = Gtk.MenuItem(label=label)
        submenu = Gtk.Menu()
        group = None
        for value, text in choices:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, text)
            group = group or item
            item.set_active(value == current)
            item.connect("toggled", lambda widget, value=value: widget.get_active() and callback(value))
            submenu.append(item)
        parent.set_submenu(submenu)
        return parent

    def build_menu(self):
        menu = Gtk.Menu()
        titles = {"confirm": "红灯·需处理", "working": "黄灯·运行中", "done": "绿灯·已完成"}
        if not self.connected:
            menu.append(self.menu_item("未连接 Paseo，正在重连…", sensitive=False))
        for lamp in LAMPS:
            agents = self.groups[lamp]
            item = Gtk.MenuItem(label="%s（%d）" % (titles[lamp], len(agents)))
            if agents:
                submenu = Gtk.Menu()
                for agent in agents[:20]:
                    submenu.append(self.menu_item(self.agent_line(agent, lamp),
                                                  lambda agent_id=agent["id"]: self.open_agent(agent_id)))
                item.set_submenu(submenu)
            else:
                item.set_sensitive(False)
            menu.append(item)
        menu.append(self.menu_item("打开 Paseo", self.open_paseo))
        menu.append(self.menu_item("清除绿灯（仅本灯，不改 Paseo）", self.dismiss_done, bool(self.groups["done"])))
        menu.append(Gtk.SeparatorMenuItem())

        menu.append(self.radio_submenu("皮肤", [("system", "跟随系统"), ("dark", "深色"), ("light", "浅色"),
                                              ("transparent", "透明")],
                                       self.settings.get("skin"), lambda value: self.set_setting("skin", value)))
        menu.append(self.menu_item("颜色设置…", self.show_color_dialog))
        sound = Gtk.MenuItem(label="提示音")
        sound_menu = Gtk.Menu()
        sound_menu.append(self.check_item("启用提示音", self.settings.get("soundEnabled", True),
                                          lambda active: self.set_setting("soundEnabled", active)))
        sound_menu.append(Gtk.SeparatorMenuItem())
        for lamp, text in (("confirm", "红灯"), ("done", "绿灯")):
            current = self.settings.get(lamp + "SoundFile") or ""
            label = "%s提示音：%s" % (text, os.path.basename(current) if current else "系统默认")
            sound_menu.append(self.menu_item(label + "…", lambda lamp=lamp: self.choose_sound_file(lamp)))
            sound_menu.append(self.menu_item("试听%s提示音" % text, lambda lamp=lamp: self.preview_sound(lamp)))
        sound_menu.append(self.menu_item("恢复默认提示音", self.reset_sounds))
        sound.set_submenu(sound_menu)
        menu.append(sound)
        menu.append(self.check_item("桌面通知（notify-send）", self.settings.get("desktopNotifications", True),
                                    lambda active: self.set_setting("desktopNotifications", active)))
        menu.append(self.check_item("呼吸灯效果", self.settings.get("breathing", True),
                                    lambda active: self.set_setting("breathing", active)))
        menu.append(self.radio_submenu("方向", [("horizontal", "横向"), ("vertical", "竖向")],
                                       self.settings.get("orientation"),
                                       lambda value: self.set_setting("orientation", value)))
        menu.append(self.radio_submenu("大小", [(value, "%d%%" % value) for value in SIZE_CHOICES],
                                       self.settings.get("sizePercent", 100),
                                       lambda value: self.set_setting("sizePercent", value)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self.menu_item("关于", self.show_about))
        menu.append(self.menu_item("退出", self.quit_app))
        menu.show_all()
        return menu

    def set_setting(self, key, value):
        if self.settings.get(key) == value:
            return
        self.settings[key] = value
        save_settings(self.settings)
        if key in ("skin", "orientation", "sizePercent"):
            self.apply_geometry()
        self.update_breathing_timer()
        self.queue_draw()

    def dismiss_done(self):
        self.dismissed_done |= {agent["id"] for agent in self.groups["done"]}
        self.recompute(notify=False)

    def preview_sound(self, lamp):
        enabled = self.settings.get("soundEnabled", True)
        self.settings["soundEnabled"] = True
        self.play_sound(lamp)
        self.settings["soundEnabled"] = enabled

    def reset_sounds(self):
        self.settings["confirmSoundFile"] = ""
        self.settings["doneSoundFile"] = ""
        save_settings(self.settings)

    def choose_sound_file(self, lamp):
        dialog = Gtk.FileChooserDialog(title="选择%s提示音" % ("红灯" if lamp == "confirm" else "绿灯"),
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
        dialog.set_keep_above(True)
        audio = Gtk.FileFilter()
        audio.set_name("音频文件")
        for pattern in ("*.wav", "*.oga", "*.ogg", "*.flac", "*.mp3"):
            audio.add_pattern(pattern)
        dialog.add_filter(audio)
        current = self.settings.get(lamp + "SoundFile") or ""
        if current:
            dialog.set_filename(current)
        if dialog.run() == Gtk.ResponseType.OK and dialog.get_filename():
            self.settings[lamp + "SoundFile"] = dialog.get_filename()
            save_settings(self.settings)
            self.preview_sound(lamp)
        dialog.destroy()

    def show_color_dialog(self):
        dialog = Gtk.Dialog(title="颜色设置")
        dialog.set_keep_above(True)
        dialog.add_buttons("恢复默认", 1, "取消", Gtk.ResponseType.CANCEL, "确定", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=12)
        buttons = {}
        rows = (("confirmColor", "红灯（需处理）"), ("workingColor", "黄灯（运行中）"), ("doneColor", "绿灯（已完成）"))
        for row, (key, label) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            buttons[key] = Gtk.ColorButton.new_with_rgba(self.gdk_color(self.settings.get(key) or DEFAULT_SETTINGS[key]))
            grid.attach(buttons[key], 1, row, 1, 1)
        waiting_check = Gtk.CheckButton(label="自定义熄灭颜色")
        waiting_check.set_active(bool(self.settings.get("waitingColor")))
        waiting_button = Gtk.ColorButton.new_with_rgba(self.gdk_color(self.settings.get("waitingColor") or "#262B2D"))
        waiting_button.set_sensitive(waiting_check.get_active())
        waiting_check.connect("toggled", lambda widget: waiting_button.set_sensitive(widget.get_active()))
        grid.attach(waiting_check, 0, len(rows), 1, 1)
        grid.attach(waiting_button, 1, len(rows), 1, 1)
        dialog.get_content_area().add(grid)
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            for key, button in buttons.items():
                self.settings[key] = self.hex_from_gdk(button.get_rgba())
            self.settings["waitingColor"] = (self.hex_from_gdk(waiting_button.get_rgba())
                                             if waiting_check.get_active() else "")
        elif response == 1:
            for key in ("confirmColor", "workingColor", "doneColor", "waitingColor"):
                self.settings[key] = DEFAULT_SETTINGS[key]
        dialog.destroy()
        if response in (Gtk.ResponseType.OK, 1):
            save_settings(self.settings)
            self.queue_draw()

    @staticmethod
    def gdk_color(value):
        color = Gdk.RGBA()
        color.parse(hex_color(parse_hex_color(value, (0, 0, 0))))
        return color

    @staticmethod
    def hex_from_gdk(color):
        return hex_color(int(round(channel * 255)) for channel in (color.red, color.green, color.blue))

    def show_about(self):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK,
                                   text="Agent Status Light（Linux 版）")
        dialog.set_keep_above(True)
        dialog.format_secondary_text(
            "数据来源：Paseo 守护进程 %s（只读）\n"
            "红灯：等待权限确认或出错未查看\n"
            "黄灯：正在运行（含 Paseo 子代理）\n"
            "绿灯：顶层代理已完成、尚未在 Paseo 中查看\n\n"
            "外观移植自 github.com/kongkongyo/agent-status-light\n"
            "设置：%s\n日志：%s" % (self.daemon_url, SETTINGS_PATH, LOG_PATH))
        dialog.run()
        dialog.destroy()

    def quit_app(self):
        if self.quitting:
            return False
        self.quitting = True
        if self.save_position_source:
            GLib.source_remove(self.save_position_source)
        self.save_position()
        self.bridge.stop()
        log("exit")
        Gtk.main_quit()
        return False


def acquire_instance_lock():
    os.makedirs(DATA_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    with open(PID_PATH, "w") as pid_file:
        pid_file.write(str(os.getpid()))
    return handle


def main():
    lock = acquire_instance_lock()
    if lock is None:
        print("Agent Status Light 已在运行", file=sys.stderr)
        return 0
    log("start pid=%d" % os.getpid())
    # WM_CLASS must match StartupWMClass in agent-status-light.desktop.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    Gdk.set_program_class(APP_ID)
    if os.path.isfile(ICON_PATH):
        try:
            Gtk.Window.set_default_icon_from_file(ICON_PATH)
        except GLib.Error as error:
            log("icon load failed: %s" % error.message)
    window = StatusLight()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, window.quit_app)
    Gtk.main()
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
