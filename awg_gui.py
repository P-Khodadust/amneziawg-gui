#!/usr/bin/env python3
"""AmneziaWG GUI: one-click connect/disconnect for awg-quick@ tunnels.

Tunnels run as systemd services (awg-quick@NAME), so closing this window keeps the VPN up.
Configs stay root-only in /etc/amnezia/amneziawg; setup.sh's polkit rule lets this user
start/stop them without a password. Import/remove go through pkexec (password prompt).
"""
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QPointF, QProcess, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtNetwork import QLocalServer, QLocalSocket, QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

CONF_DIR = "/etc/amnezia/amneziawg"
LIST_FILE = Path.home() / ".config/amneziawg-gui/tunnels"  # names only: CONF_DIR is root-only, we can't list it
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")  # awg-quick's name rule minus = and +, which systemd unit names reject
HISTORY = 60  # seconds of traffic in the graph
INSTANCE = f"amneziawg-gui-{os.getuid()}"  # local socket name that keeps the app single-instance
IP_CHECK = "https://1.1.1.1/cdn-cgi/trace"  # no rate limit (ipinfo.io 429s shared VPN exits), no DNS needed

BG, CARD, LINE, TEXT, MUTED = "#0b0d12", "#151922", "#242a3a", "#e8ebf2", "#7d8496"
ON, BUSY, OFF, ERR, UP = "#2ee6a8", "#ffb547", "#3a4152", "#ff5d6c", "#8b7bff"

STYLE = f"""
* {{ color: {TEXT}; font-size: 10pt; }}
QWidget#root, QDialog {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 14pt; font-weight: 600; }}
QLabel#status {{ font-size: 20pt; font-weight: 600; }}
QLabel#muted, QLabel#caption {{ color: {MUTED}; }}
QLabel#caption {{ font-size: 8pt; font-weight: 600; }}
QLabel#big {{ font-size: 15pt; font-weight: 600; }}
QFrame#card {{ background: {CARD}; border-radius: 14px; }}
QComboBox, QPushButton, QLineEdit {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; padding: 7px 12px; }}
QComboBox:hover, QPushButton:hover, QComboBox:focus, QPushButton:focus, QLineEdit:focus {{ border-color: {ON}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({Path(__file__).with_name("arrow.svg")}); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {LINE}; selection-background-color: {LINE}; }}
QPushButton#glyph {{ font-size: 14pt; padding: 3px; }}
QPushButton#link {{ border: none; background: transparent; color: {MUTED}; padding: 2px; }}
QPushButton#link:hover, QPushButton#link:focus {{ color: {TEXT}; }}
QMenu {{ background: {CARD}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {LINE}; }}
"""


def unit(name):
    return f"awg-quick@{name}.service"


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=20)


def unit_states(names):
    """systemd ActiveState per tunnel: active / activating / deactivating / failed / inactive."""
    if not names:
        return {}
    return dict(zip(names, systemctl("is-active", *map(unit, names)).stdout.split()))


def known_tunnels():
    names = set()
    if LIST_FILE.exists():
        names |= {n for n in LIST_FILE.read_text().split() if NAME_RE.fullmatch(n)}
    # also show tunnels started outside the GUI (e.g. `systemctl start awg-quick@x`)
    out = systemctl("list-units", "--plain", "--no-legend", "--state=active,activating", "awg-quick@*").stdout
    names |= set(re.findall(r"awg-quick@(\S+)\.service", out))
    return sorted(names)


def active_since(name):
    out = systemctl("show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit(name)).stdout.strip()
    return int(out) / 1e6 if out.isdigit() and int(out) else time.monotonic()  # both CLOCK_MONOTONIC


def iface_bytes(name):
    stats = Path("/sys/class/net", name, "statistics")
    try:
        return int((stats / "rx_bytes").read_text()), int((stats / "tx_bytes").read_text())
    except OSError:
        return None


def human(n, suffix=""):
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}{suffix}" if i == 0 else f"{n:.1f} {units[i]}{suffix}"


def clock(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def suggest_name(path):
    return re.sub(r"[^A-Za-z0-9_.-]", "", Path(path).stem)[:15] or "awg0"


def pin_endpoint(text):
    """Keep traffic to the server itself out of the tunnel, like the Amnezia apps do.

    awg-quick only does that for AllowedIPs with a /0. With a split list that covers the server,
    the tunnel's own packets loop back into it: one handshake, then nothing.
    shortcut: IPv4-literal endpoints only; a hostname endpoint would need resolving inside the hook.
    """
    m = re.search(r"(?mi)^\s*Endpoint\s*=\s*(\d+\.\d+\.\d+\.\d+):\d+\s*$", text)
    if not m or f"route replace {m[1]}/32" in text:
        return text
    ip = ipaddress.ip_address(m[1])
    nets = [ipaddress.ip_network(a.strip(), strict=False)
            for line in re.findall(r"(?mi)^\s*AllowedIPs\s*=\s*(.+)$", text) for a in line.split(",") if a.strip()]
    v4 = [n for n in nets if n.version == 4]
    if any(n.prefixlen == 0 for n in v4) or not any(ip in n for n in v4):
        return text
    hook = ("# added by AmneziaWG GUI: route the server itself outside the tunnel\n"
            f"PreUp = ip -4 route replace {ip}/32 $(ip -4 route get {ip} | grep -oE '(via [0-9.]+ )?dev [^ ]+' | head -1)\n"
            f"PostDown = ip -4 route del {ip}/32 || true\n")
    return re.sub(r"(?mi)^\[Interface\][^\n]*\n", lambda h: h[0] + hook, text, count=1)


def draw_power(p, c, r, color, width):
    """Power glyph: an open ring with a bar through the gap."""
    p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 120 * 16, 300 * 16)
    p.drawLine(QPointF(c.x(), c.y() - r * 1.25), QPointF(c.x(), c.y() - r * 0.2))


def tray_icon(color):
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 60, 60)
    draw_power(p, QPointF(32, 34), 15, BG, 7)
    p.end()
    return QIcon(pix)


class PowerButton(QWidget):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedSize(220, 220)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)  # Space/Enter toggle too
        self.setAccessibleName("Connect")
        self.color, self.angle = OFF, 0
        self.anim = QTimer(self, interval=16)
        self.anim.timeout.connect(self.spin)

    def set_state(self, color, busy):
        self.color = color
        if busy != self.anim.isActive():
            self.anim.start() if busy else self.anim.stop()
        self.update()

    def spin(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor(self.color)
        disc = QRectF(self.rect()).adjusted(36, 36, -36, -36)
        c, radius = disc.center(), disc.width() / 2
        if self.color != OFF:  # soft halo
            halo = QRadialGradient(c, radius + 34)
            glow = QColor(color)
            glow.setAlpha(110)
            halo.setColorAt(0.7, glow)
            glow.setAlpha(0)
            halo.setColorAt(1.0, glow)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(c, radius + 34, radius + 34)
        p.setPen(QPen(QColor(MUTED if self.hasFocus() else LINE), 2))  # outline doubles as the focus ring
        p.setBrush(QColor(CARD))
        p.drawEllipse(disc)
        ring = disc.adjusted(10, 10, -10, -10)
        p.setPen(QPen(color, 6, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        if self.anim.isActive():
            p.drawArc(ring, -self.angle * 16, 110 * 16)
        else:
            p.drawEllipse(ring)
        draw_power(p, c, radius * 0.34, TEXT if self.color == OFF else self.color, 7)


class Graph(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(90)
        self.rx, self.tx = deque(maxlen=HISTORY), deque(maxlen=HISTORY)

    def push(self, rx, tx):
        self.rx.append(rx)
        self.tx.append(tx)
        self.update()

    def clear(self):
        self.rx.clear()
        self.tx.clear()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(LINE), 1))
        p.drawLine(0, h - 1, w, h - 1)
        peak = max([*self.rx, *self.tx, 10240])  # floor at 10 KB/s so idle chatter stays flat
        step = w / (HISTORY - 1)
        for data, color in ((self.tx, UP), (self.rx, ON)):
            if len(data) < 2:
                continue
            x0 = w - step * (len(data) - 1)
            pts = [QPointF(x0 + i * step, h - 3 - (h - 8) * v / peak) for i, v in enumerate(data)]
            line = QPainterPath(pts[0])
            for pt in pts[1:]:
                line.lineTo(pt)
            area = QPainterPath(line)
            area.lineTo(pts[-1].x(), h)
            area.lineTo(pts[0].x(), h)
            fade = QLinearGradient(0, 0, 0, h)
            tint = QColor(color)
            tint.setAlpha(80)
            fade.setColorAt(0, tint)
            tint.setAlpha(0)
            fade.setColorAt(1, tint)
            p.fillPath(area, fade)
            p.setPen(QPen(QColor(color), 2))
            p.drawPath(line)


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setWindowTitle("AmneziaWG")
        self.setWindowIcon(QIcon.fromTheme("network-vpn"))
        self.setMinimumSize(380, 660)
        self.names, self.state, self.since, self.last = [], None, 0.0, None
        self.silent = False  # up, but nothing received from the server yet
        self.net, self.ip_reply = QNetworkAccessManager(self), None

        self.combo = QComboBox(accessibleName="Tunnel", placeholderText="No tunnels")
        self.combo.currentTextChanged.connect(self.selected)
        add = QPushButton("+", objectName="glyph", toolTip="Import a .conf file", accessibleName="Import config")
        rem = QPushButton("−", objectName="glyph", toolTip="Remove this tunnel", accessibleName="Remove tunnel")
        add.clicked.connect(self.import_conf)
        rem.clicked.connect(self.remove)
        top = QHBoxLayout()
        top.addWidget(self.combo, 1)
        for b in (add, rem):
            b.setFixedWidth(42)
            top.addWidget(b)

        self.power = PowerButton()
        self.power.clicked.connect(self.toggle)
        self.status = QLabel(objectName="status", alignment=Qt.AlignCenter)
        self.detail = QLabel(objectName="muted", alignment=Qt.AlignCenter)
        self.ip = QPushButton(objectName="link", toolTip="Check the exit IP again")
        self.ip.clicked.connect(self.fetch_ip)

        self.graph = Graph()
        self.down, self.up = QLabel(objectName="big"), QLabel(objectName="big")
        self.down_total, self.up_total = QLabel(objectName="muted"), QLabel(objectName="muted")
        stats = QFrame(objectName="card")
        grid = QGridLayout(stats)
        grid.setContentsMargins(18, 14, 18, 14)
        for col, (caption, color, rate, total) in enumerate((("DOWNLOAD", ON, self.down, self.down_total),
                                                            ("UPLOAD", UP, self.up, self.up_total))):
            grid.addWidget(QLabel(f"<span style='color:{color}'>●</span> {caption}", objectName="caption"), 0, col)
            grid.addWidget(rate, 1, col)
            grid.addWidget(total, 2, col)
        grid.addWidget(self.graph, 3, 0, 1, 2)
        self.show_rates(None)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 20)
        lay.setSpacing(8)
        lay.addWidget(QLabel("AmneziaWG", objectName="title"))
        lay.addLayout(top)
        lay.addStretch()
        lay.addWidget(self.power, 0, Qt.AlignCenter)
        for w in (self.status, self.detail, self.ip):
            lay.addWidget(w, 0, Qt.AlignCenter)
        lay.addStretch()
        lay.addWidget(stats)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            menu = QMenu(self)
            self.tray_toggle = menu.addAction("Connect", self.toggle)
            menu.addAction("Open AmneziaWG", self.bring_up)
            menu.addSeparator()
            menu.addAction("Quit", QApplication.quit)
            self.tray = QSystemTrayIcon(self)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.tray_clicked)
            self.tray.show()

        self.reload_tunnels()
        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.fetch_ip()

    def tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.hide() if self.isVisible() else self.bring_up()

    def bring_up(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.tray:  # keep living in the tray; Quit is in its menu (the tunnel is a service either way)
            e.ignore()
            self.hide()

    def reload_tunnels(self, select=None):
        self.names = known_tunnels()
        states = unit_states(self.names)
        running = next((n for n in self.names if states[n] in ("active", "activating")), None)
        pick = select or running or self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(self.names)
        # explicit: with a placeholder set, Qt leaves the index at -1 after addItems
        self.combo.setCurrentIndex(self.names.index(pick) if pick in self.names else 0)
        self.combo.blockSignals(False)
        self.selected()

    def selected(self, *_):
        self.state, self.last = None, None
        self.graph.clear()
        self.poll()

    def poll(self):
        name = self.combo.currentText()
        state = unit_states([name])[name] if name else "none"
        if state != self.state:
            old, self.state = self.state, state  # set first: changed() may open a dialog that re-enters poll()
            self.changed(name, old, state)
        if state == "active":
            self.sample(name)
            uptime = time.monotonic() - self.since
            # systemd's "active" only means the interface is up; zero bytes in means the server never answered
            silent = uptime > 10 and self.last is not None and self.last[1] == 0
            if silent != self.silent:
                self.silent = silent
                self.show_status(name, *((BUSY, "No reply from server") if silent else (ON, "Connected")))
            self.detail.setText("Is another VPN or its firewall on?" if silent else clock(uptime))

    def changed(self, name, old, new):
        color, text = {"active": (ON, "Connected"), "activating": (BUSY, "Connecting…"),
                       "deactivating": (BUSY, "Disconnecting…"), "failed": (ERR, "Connection failed"),
                       "none": (OFF, "No tunnels yet")}.get(new, (OFF, "Disconnected"))
        self.silent = False
        self.power.setAccessibleName("Disconnect" if new in ("active", "activating") else "Connect")
        if self.tray:
            self.tray_toggle.setText(self.power.accessibleName())
        self.show_status(name, color, text, busy=new in ("activating", "deactivating"))
        self.detail.setText("Click + to import a .conf file" if new == "none" else "")
        if new == "active":
            self.since = active_since(name)
        else:
            self.last = None
            self.graph.clear()
            self.show_rates(None)
        if old is not None and "active" in (old, new):
            QTimer.singleShot(1500, self.fetch_ip)  # let routes and DNS settle first
        if new == "failed" and old in ("activating", "active"):
            log = subprocess.run(["journalctl", "-u", unit(name), "-n", "8", "-o", "cat", "--no-pager"],
                                 capture_output=True, text=True).stdout.strip()
            QMessageBox.warning(self, "Connection failed", f"'{name}' could not start.\n\n{log or 'No log available.'}")

    def show_status(self, name, color, text, busy=False):
        self.power.set_state(color, busy)
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color if color in (ON, ERR) else TEXT}")
        if self.tray:
            self.tray.setIcon(tray_icon(MUTED if color == OFF else color))  # OFF grey vanishes on a dark panel
            self.tray.setToolTip(f"AmneziaWG · {name}: {text}" if name else "AmneziaWG")

    def sample(self, name):
        now, totals = time.monotonic(), iface_bytes(name)
        if totals and self.last:
            dt = now - self.last[0]
            self.graph.push((totals[0] - self.last[1]) / dt, (totals[1] - self.last[2]) / dt)
            self.show_rates(totals)
        self.last = (now, *totals) if totals else None

    def show_rates(self, totals):
        if not totals:
            self.down.setText("—")
            self.up.setText("—")
            self.down_total.setText(" ")
            self.up_total.setText(" ")
            return
        self.down.setText(human(self.graph.rx[-1], "/s"))
        self.up.setText(human(self.graph.tx[-1], "/s"))
        self.down_total.setText(f"{human(totals[0])} total")
        self.up_total.setText(f"{human(totals[1])} total")

    def toggle(self):
        name = self.combo.currentText()
        if not name:
            return self.import_conf()
        if self.state in ("active", "activating"):
            r = systemctl("stop", "--no-block", unit(name))
        else:
            for other, state in unit_states(self.names).items():
                if other != name and state in ("active", "activating"):
                    systemctl("stop", unit(other))  # one tunnel at a time: two default routes would fight
            r = systemctl("start", "--no-block", unit(name))
        if r.returncode:
            QMessageBox.warning(self, "AmneziaWG", r.stderr.strip() or f"systemctl failed (exit {r.returncode})")
        self.poll()

    def fetch_ip(self):
        self.ip.setText("checking exit IP…")
        req = QNetworkRequest(QUrl(IP_CHECK))
        req.setTransferTimeout(8000)
        self.net.clearConnectionCache()  # a kept-alive connection still uses the route from before (dis)connecting
        reply = self.ip_reply = self.net.get(req)
        reply.finished.connect(lambda: self.got_ip(reply))

    def got_ip(self, reply):
        reply.deleteLater()
        if reply is not self.ip_reply:  # a newer check is already running
            return
        body = bytes(reply.readAll()).decode(errors="replace")
        trace = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)  # "ip=…\nloc=NL\n…"
        if reply.error() == QNetworkReply.NoError and "ip" in trace:
            self.ip.setText(f"{trace['ip']}  ·  {trace.get('loc', '?')}")
        else:
            self.ip.setText("exit IP unavailable · click to retry")

    def import_conf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import AmneziaWG config", str(Path.home()),
                                              "WireGuard/AmneziaWG config (*.conf);;All files (*)")
        if not path:
            return
        text = Path(path).read_text(errors="replace")
        if "[Interface]" not in text or "[Peer]" not in text:
            return QMessageBox.warning(self, "Import", "That doesn't look like a WireGuard/AmneziaWG config.")
        name, ok = QInputDialog.getText(self, "Import", "Tunnel name (up to 15 letters, digits, _ . -):",
                                        QLineEdit.EchoMode.Normal, suggest_name(path))
        if not ok:
            return
        if not NAME_RE.fullmatch(name):
            return QMessageBox.warning(self, "Import", f"'{name}' isn't a valid tunnel name.")
        if name in self.names and QMessageBox.question(self, "Import", f"Replace the existing '{name}'?") != QMessageBox.Yes:
            return
        fixed, src = pin_endpoint(text), path
        if fixed != text:
            with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp:  # 0600: holds the private key
                tmp.write(fixed)
            src = tmp.name
        self.as_root(["/usr/bin/install", "-m", "600", "-o", "root", "-g", "root", src, f"{CONF_DIR}/{name}.conf"],
                     lambda: self.save_names({*self.names, name}, select=name),
                     always=lambda: src != path and os.unlink(src))

    def remove(self):
        name = self.combo.currentText()
        if not name or QMessageBox.question(self, "Remove", f"Delete tunnel '{name}' and its config?") != QMessageBox.Yes:
            return
        systemctl("stop", unit(name))
        self.as_root(["/usr/bin/rm", "-f", f"{CONF_DIR}/{name}.conf"],
                     lambda: self.save_names(set(self.names) - {name}))

    def as_root(self, args, done, always=lambda: None):
        """Run one command as root via pkexec without freezing the window."""
        proc = QProcess(self)

        def finished(code, _status):
            always()
            if code == 0:
                done()
            elif code != 126:  # 126: the password dialog was dismissed
                err = bytes(proc.readAllStandardError()).decode().strip()
                QMessageBox.warning(self, "AmneziaWG", err or f"Failed (exit {code})")
            proc.deleteLater()

        proc.finished.connect(finished)
        proc.start("pkexec", args)

    def save_names(self, names, select=None):
        LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIST_FILE.write_text("".join(f"{n}\n" for n in sorted(names)))
        self.reload_tunnels(select)


def selftest():
    assert human(0) == "0 B" and human(1536) == "1.5 KB" and human(5 * 1024**3) == "5.0 GB"
    assert human(2048, "/s") == "2.0 KB/s" and clock(3725) == "01:02:05"
    assert NAME_RE.fullmatch("home-vpn.1") and not NAME_RE.fullmatch("a/b") and not NAME_RE.fullmatch("a+b")
    assert not NAME_RE.fullmatch("x" * 16) and not NAME_RE.fullmatch("")
    assert suggest_name("/dl/My VPN (1).conf") == "MyVPN1" and suggest_name("/dl/((()).conf") == "awg0"
    assert NAME_RE.fullmatch(suggest_name("/dl/a-very-long-config-name-here.conf"))
    conf = "[Interface]\nPrivateKey = x\n\n[Peer]\nEndpoint = 198.51.100.7:51820\nAllowedIPs = 1.0.0.0/8, 128.0.0.0/1, ::/0\n"
    pinned = pin_endpoint(conf)
    assert pinned.startswith("[Interface]\n# added by") and "replace 198.51.100.7/32" in pinned
    assert pinned.index("PreUp") < pinned.index("PrivateKey") and pin_endpoint(pinned) == pinned  # once only
    assert pin_endpoint(conf.replace("128.0.0.0/1", "0.0.0.0/0")) == conf.replace("128.0.0.0/1", "0.0.0.0/0")
    assert pin_endpoint(conf.replace("128.0.0.0/1", "2.0.0.0/8")) == conf.replace("128.0.0.0/1", "2.0.0.0/8")
    assert pin_endpoint(conf.replace("198.51.100.7", "vpn.example.com")) == conf.replace("198.51.100.7", "vpn.example.com")
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    app = QApplication(sys.argv)
    app.setApplicationName("AmneziaWG")  # tray entry's Id/Title in Plasma (default: "PySideApp")
    ping = QLocalSocket()
    ping.connectToServer(INSTANCE)
    if ping.waitForConnected(500):  # already running: the connection itself makes it show its window
        return
    app.setDesktopFileName("amneziawg-gui")  # lets Plasma match the window to its .desktop icon
    app.setStyleSheet(STYLE)
    win = Main()

    def second_launch():
        server.nextPendingConnection().deleteLater()
        win.bring_up()

    QLocalServer.removeServer(INSTANCE)  # stale socket left by a crash (no one answered above)
    server = QLocalServer(win)
    server.newConnection.connect(second_launch)
    server.listen(INSTANCE)
    app.setQuitOnLastWindowClosed(win.tray is None)  # else closing a dialog while hidden would quit
    win.show()
    win.power.setFocus()  # after show(), or Qt hands focus to the first widget instead
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
