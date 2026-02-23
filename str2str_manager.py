#!/usr/bin/env python3
"""
GNSS Stack Manager
==================
Manages the two-layer GNSS data pipeline:

  Layer 1 – str2str input bridge:
    Serial port → TCP server (raw NMEA + RTCM3)
    Configurable via [str2str.input_bridge] in the TOML.

  Layer 2 – str2str (RTKLIB):
    TCP client (→ input bridge) → RTCM3 filter → multiple outputs

  Layer 3 – Web frontend (app.py):
    Runs as an independent service, completely decoupled from this manager.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# TOML
# ──────────────────────────────────────────────────────────────────────────────
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("ERROR: No TOML parser available. Python<3.11: pip install tomli",
              file=sys.stderr)
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
def setup_logging(log_file: str, level: str) -> logging.Logger:
    logger = logging.getLogger("gnss_manager")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ──────────────────────────────────────────────────────────────────────────────
# Managed Process  (Subprocess mit Reconnect-Loop)
# ──────────────────────────────────────────────────────────────────────────────
class ManagedProcess:
    def __init__(self, name: str, cmd: list[str], logger: logging.Logger,
                 restart_delay: int = 5, max_restarts: int = 0,
                 reconnect: bool = True):
        self.name          = name
        self.cmd           = cmd
        self.logger        = logger.getChild(name)
        self.restart_delay = restart_delay
        self.max_restarts  = max_restarts
        self.reconnect     = reconnect
        self._proc         = None
        self._running      = False
        self._restarts     = 0

    def start(self):
        self._running = True
        self._loop()

    def _loop(self):
        while self._running:
            self.logger.info("Starting: %s", " ".join(self.cmd))
            try:
                self._proc = subprocess.Popen(
                    self.cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in self._proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.logger.debug("  > %s", line)
                self._proc.wait()
                rc = self._proc.returncode
            except FileNotFoundError:
                self.logger.error("Binary not found: %s", self.cmd[0])
                self._running = False
                return
            except Exception as exc:
                self.logger.error("Error: %s", exc)
                rc = -1

            if not self._running:
                break

            self.logger.warning("Process exited (rc=%d).", rc)

            if not self.reconnect:
                self.logger.info("reconnect=false – not restarting.")
                break

            self._restarts += 1
            if self.max_restarts > 0 and self._restarts >= self.max_restarts:
                self.logger.error("Max restarts (%d) reached.", self.max_restarts)
                break

            self.logger.info("Restarting in %ds (attempt %d)…",
                             self.restart_delay, self._restarts)
            time.sleep(self.restart_delay)

    def stop(self):
        self._running = False
        if self._proc and self._proc.poll() is None:
            self.logger.info("Terminating PID %d…", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self.logger.info("Stopped.")

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ──────────────────────────────────────────────────────────────────────────────
# URL builder for str2str
# ──────────────────────────────────────────────────────────────────────────────
def build_str2str_input_url(inp: dict) -> str:
    t = inp.get("type", "tcpcli").lower()
    if t == "tcpcli":
        return (f"tcpcli://{inp.get('host','127.0.0.1')}"
                f":{inp.get('port',4001)}")
    elif t == "tcpsvr":
        return f"tcpsvr://:{inp.get('port',9000)}"
    elif t == "serial":
        return (f"serial://{inp.get('device','/dev/ttyUSB0')}"
                f":{inp.get('baudrate',115200)}:8:n:1:off")
    elif t == "ntrip":
        host, port = inp.get("host",""), inp.get("port",2101)
        mp   = inp.get("mountpoint","")
        user = inp.get("user","")
        pw   = inp.get("password","")
        if user:
            return f"ntrip://{user}:{pw}@{host}:{port}/{mp}"
        return f"ntrip://{host}:{port}/{mp}"
    elif t == "file":
        return f"file://{inp.get('path','/dev/stdin')}"
    else:
        raise ValueError(f"Unknown str2str input type: '{t}'")


def build_str2str_output_url(out: dict) -> str:
    t    = out.get("type", "").lower()
    bind = out.get("bind_addr", "")

    if t == "tcpsvr":
        port = out.get("port", 9001)
        addr = (f"{bind}:{port}"
                if bind and bind not in ("0.0.0.0","") else f":{port}")
        return f"tcpsvr://{addr}"
    elif t == "udpsvr":
        port = out.get("port", 9002)
        addr = (f"{bind}:{port}"
                if bind and bind not in ("0.0.0.0","") else f":{port}")
        return f"udpsvr://{addr}"
    elif t == "file":
        from datetime import datetime
        path = datetime.utcnow().strftime(out.get("path", "/tmp/rtcm3.rtcm3"))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return f"file://{path}"
    elif t == "ntrip":
        host, port = out.get("host",""), out.get("port",2101)
        mp   = out.get("mountpoint","")
        user = out.get("user","")
        pw   = out.get("password","")
        if user:
            return f"ntrips://{user}:{pw}@{host}:{port}/{mp}"
        return f"ntrips://{host}:{port}/{mp}"
    elif t == "ntripsvr":
        port = out.get("port", 2101)
        mp   = out.get("mountpoint","")
        user = out.get("user","")
        pw   = out.get("password","")
        if user:
            return f"ntripsvr://{user}:{pw}@:{port}/{mp}"
        return f"ntripsvr://:{port}/{mp}"
    else:
        raise ValueError(f"Unknown str2str output type: '{t}'")


def build_str2str_cmd(binary: str, input_url: str, out: dict) -> list[str]:
    output_url = build_str2str_output_url(out)
    cmd = [binary, "-in", input_url, "-out", output_url]
    msgs = out.get("rtcm_messages", "").strip()
    if msgs:
        cmd += ["-msg", msgs]
    if out.get("type") == "file":
        swap = out.get("swap_interval", 0)
        if swap > 0:
            cmd += ["-t", str(swap)]
    return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Main manager
# ──────────────────────────────────────────────────────────────────────────────
class GNSSStackManager:
    def __init__(self, config_path: str):
        with open(config_path, "rb") as f:
            self.config = tomllib.load(f)

        gen = self.config.get("general", {})
        self.logger = setup_logging(
            gen.get("log_file",    "/var/log/gnss/manager.log"),
            gen.get("log_level",   "INFO"),
        )
        self.restart_delay = gen.get("restart_delay", 5)
        self.max_restarts  = gen.get("max_restarts",  0)
        self._processes: list[ManagedProcess]  = []
        self._threads:   list[threading.Thread] = []

    # ── str2str input bridge ──────────────────────────────────────────────────
    def _start_input_bridge(self):
        cfg_s2s = self.config.get("str2str", {})
        bridge  = cfg_s2s.get("input_bridge", {})

        if not bridge.get("enabled", True):
            self.logger.info("str2str input bridge disabled – skipping.")
            return

        binary   = cfg_s2s.get("binary",   "/usr/local/bin/str2str")
        device   = bridge.get("device",   "/dev/ttyUSB0")
        baudrate = bridge.get("baudrate", 115200)
        port     = bridge.get("port",     4001)
        bind     = bridge.get("bind_addr", "127.0.0.1")

        in_url  = f"serial://{device}:{baudrate}:8:n:1:off"
        addr    = (f"{bind}:{port}"
                   if bind and bind not in ("0.0.0.0", "") else f":{port}")
        out_url = f"tcpsvr://{addr}"

        self.logger.info("str2str input bridge: %s @ %d baud → TCP %s:%d",
                         device, baudrate, bind, port)

        self._launch(ManagedProcess(
            name="str2str.input_bridge",
            cmd=[binary, "-in", in_url, "-out", out_url],
            logger=self.logger,
            restart_delay=self.restart_delay,
            max_restarts=self.max_restarts,
            reconnect=True,
        ))
        # Give the bridge a moment to open the TCP port before output processes connect
        time.sleep(1.5)

    # ── str2str ───────────────────────────────────────────────────────────────
    def _start_str2str(self):
        cfg_s2s = self.config.get("str2str", {})
        binary  = cfg_s2s.get("binary",  "/usr/local/bin/str2str")
        inp_cfg = cfg_s2s.get("input",   {})
        outputs = cfg_s2s.get("outputs", [])

        if not outputs:
            self.logger.warning("str2str: no [[str2str.outputs]] defined.")
            return

        try:
            input_url = build_str2str_input_url(inp_cfg)
        except ValueError as e:
            self.logger.error("str2str input URL: %s", e)
            return

        self.logger.info("str2str input: %s", input_url)

        for out in outputs:
            if not out.get("enabled", True):
                self.logger.info("str2str output '%s' disabled.",
                                 out.get("name"))
                continue
            try:
                cmd = build_str2str_cmd(binary, input_url, out)
            except ValueError as e:
                self.logger.error("str2str output '%s': %s",
                                  out.get("name"), e)
                continue

            self._launch(ManagedProcess(
                name=f"str2str.{out.get('name','out')}",
                cmd=cmd, logger=self.logger,
                restart_delay=self.restart_delay,
                max_restarts=self.max_restarts,
                reconnect=out.get("reconnect", True),
            ))
            self.logger.info("str2str %-22s → %s",
                             out.get("name"), " ".join(cmd))

    # ── Thread-Launcher ───────────────────────────────────────────────────────
    def _launch(self, proc: ManagedProcess):
        self._processes.append(proc)
        t = threading.Thread(target=proc.start, name=proc.name, daemon=True)
        self._threads.append(t)
        t.start()

    # ── Main loop ─────────────────────────────────────────────────────────────
    def start(self):
        self.logger.info("═══ GNSS Stack Manager starting ═══")
        self._start_input_bridge()
        self._start_str2str()

        # Keep running even without active processes – the user can adjust
        # the config in the dashboard and restart the service. systemd should
        # not consider the manager failed just because no data source is
        # configured yet.
        if not self._threads:
            self.logger.warning(
                "No processes started "
                "(input bridge disabled / no active outputs?).\n"
                "  Manager staying active – adjust the configuration in the "
                "dashboard and run 'systemctl restart gnss-stack'.")

        self.logger.info("Manager running. %d process(es) started.",
                         len(self._processes))
        try:
            while True:
                time.sleep(5)
                alive = sum(1 for t in self._threads if t.is_alive())
                if self._threads and alive == 0:
                    self.logger.warning(
                        "All process threads have exited – manager shutting down.")
                    break
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.logger.info("Stopping all processes…")
        for proc in self._processes:
            proc.stop()
        self.logger.info("All processes stopped.")

    def status(self) -> list[dict]:
        return [
            {
                "name":     p.name,
                "running":  p.is_running,
                "pid":      p._proc.pid if p._proc else None,
                "restarts": p._restarts,
                "cmd":      " ".join(p.cmd),
            }
            for p in self._processes
        ]


# ──────────────────────────────────────────────────────────────────────────────
# Signal handler for systemd
# ──────────────────────────────────────────────────────────────────────────────
def _handle_signal(manager: GNSSStackManager, signum, _frame):
    logging.getLogger("gnss_manager").info("Signal %d received – shutting down.", signum)
    manager.stop()
    sys.exit(0)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="GNSS Stack Manager (str2str)")
    parser.add_argument(
        "--config", default="/etc/gnss/str2str.toml",
        help="Path to the TOML configuration file")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"ERROR: Configuration file not found: {args.config}",
              file=sys.stderr)
        sys.exit(1)

    manager = GNSSStackManager(args.config)
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(manager, s, f))
    signal.signal(signal.SIGINT,  lambda s, f: _handle_signal(manager, s, f))
    manager.start()


if __name__ == "__main__":
    main()
