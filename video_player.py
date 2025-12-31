#!/usr/bin/env python3
"""
Video Player for Original Raspberry Pi (omxplayer)
- Plays video on multicast UDP command
- Supports LOAD/GO for synchronized playback
- Receives replacement video files over TCP (when not playing)
"""

import socket
import struct
import subprocess
import threading
import os
import sys
import signal
import logging
import time

# Configuration
CONFIG = {
    "video_file": "/home/pi/video/current_video.mp4",
    "temp_video_file": "/home/pi/video/incoming_video.tmp",
    "multicast_group": "239.255.42.1",
    "multicast_port": 5000,
    "file_transfer_port": 5001,
    "audio_output": "hdmi",
}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class VideoPlayer:
    """Manages omxplayer subprocess using FIFO for control."""

    FIFO_PATH = "/tmp/omxplayer_fifo"

    def __init__(self, video_path: str, audio_output: str = "hdmi"):
        self.video_path = video_path
        self.audio_output = audio_output
        self.process = None
        self.fifo_fd = None
        self._paused = False
        self._lock = threading.Lock()
        self._setup_fifo()

    def _setup_fifo(self):
        """Create the FIFO if it doesn't exist."""
        if os.path.exists(self.FIFO_PATH):
            os.remove(self.FIFO_PATH)
        os.mkfifo(self.FIFO_PATH)

    def _send_command(self, cmd: bytes) -> bool:
        """Send a command to omxplayer via FIFO."""
        try:
            if self.fifo_fd is not None:
                os.write(self.fifo_fd, cmd)
                return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
        return False

    def play(self, paused: bool = False) -> bool:
        """Start video playback.
        
        Args:
            paused: If True, pause immediately after start (use go() to play)
        """
        with self._lock:
            if self.process is not None:
                logger.warning("Already playing")
                return False

            if not os.path.exists(self.video_path):
                logger.error(f"Video file not found: {self.video_path}")
                return False

            try:
                # Recreate FIFO in case it was corrupted
                self._setup_fifo()
                
                cmd = [
                    "omxplayer",
                    "-o", self.audio_output,
                    "--no-osd",
                    "--aspect-mode", "letterbox",
                    self.video_path
                ]
                
                logger.info(f"Starting: {' '.join(cmd)}")
                
                # Open FIFO for writing (non-blocking open, then we have the fd)
                fifo_read = open(self.FIFO_PATH, 'r+b', buffering=0)
                self.fifo_fd = fifo_read.fileno()
                
                self.process = subprocess.Popen(
                    cmd,
                    stdin=fifo_read,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
                
                self._fifo_file = fifo_read  # Keep reference to prevent GC
                self._paused = False
                
                # If paused mode requested, wait for video to start then pause
                if paused:
                    time.sleep(0.5)  # Wait for omxplayer to initialize
                    self._send_command(b"p")
                    self._paused = True
                    logger.info("Video loaded and paused")
                
                return True
            except Exception as e:
                logger.error(f"Failed to start omxplayer: {e}")
                self.process = None
                self.fifo_fd = None
                return False

    def preload(self) -> bool:
        """Load video and pause, ready for go() command."""
        return self.play(paused=True)

    def go(self) -> bool:
        """Unpause a preloaded video."""
        with self._lock:
            if self.process is None:
                logger.warning("No video loaded")
                return False
            
            if not self._paused:
                logger.warning("Video not in paused state")
                return False

        if self._send_command(b"p"):
            self._paused = False
            logger.info("Playback started")
            return True
        return False

    def stop(self) -> bool:
        """Stop video playback."""
        with self._lock:
            if self.process is None:
                return False

            try:
                self._send_command(b"q")
                
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    self.process.wait()
                
                logger.info("Playback stopped")
            except Exception as e:
                logger.error(f"Error stopping playback: {e}")
            finally:
                if hasattr(self, '_fifo_file'):
                    try:
                        self._fifo_file.close()
                    except:
                        pass
                self.process = None
                self.fifo_fd = None
                self._paused = False
            return True

    def is_playing(self) -> bool:
        """Check if video is currently playing or loaded."""
        with self._lock:
            if self.process is None:
                return False
            
            poll = self.process.poll()
            if poll is not None:
                if hasattr(self, '_fifo_file'):
                    try:
                        self._fifo_file.close()
                    except:
                        pass
                self.process = None
                self.fifo_fd = None
                self._paused = False
                return False
            return True


class MulticastListener:
    """Listens for UDP multicast commands."""

    def __init__(self, group: str, port: int):
        self.group = group
        self.port = port
        self.socket = None
        self._running = False

    def start(self, callback):
        """Start listening for multicast messages."""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("", self.port))
        
        mreq = struct.pack("4sl", socket.inet_aton(self.group), socket.INADDR_ANY)
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.socket.settimeout(1.0)
        self._running = True
        
        logger.info(f"Listening for multicast on {self.group}:{self.port}")
        
        while self._running:
            try:
                data, addr = self.socket.recvfrom(1024)
                logger.debug(f"Received from {addr}: {data}")
                callback(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Multicast receive error: {e}")

    def stop(self):
        self._running = False
        if self.socket:
            self.socket.close()


class FileReceiver:
    """TCP server for receiving video files."""

    def __init__(self, port: int, dest_path: str, temp_path: str):
        self.port = port
        self.dest_path = dest_path
        self.temp_path = temp_path
        self.socket = None
        self._running = False
        self._receiving = False
        self._lock = threading.Lock()

    def is_receiving(self) -> bool:
        with self._lock:
            return self._receiving

    def start(self, can_receive_callback):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("", self.port))
        self.socket.listen(1)
        self.socket.settimeout(1.0)
        self._running = True
        
        logger.info(f"File receiver listening on port {self.port}")
        
        while self._running:
            try:
                conn, addr = self.socket.accept()
                
                if not can_receive_callback():
                    logger.warning(f"Rejecting file transfer from {addr} - playback in progress")
                    conn.send(b"BUSY\n")
                    conn.close()
                    continue
                
                logger.info(f"Accepting file transfer from {addr}")
                conn.send(b"READY\n")
                self._receive_file(conn, addr)
                
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"File receiver error: {e}")

    def _receive_file(self, conn: socket.socket, addr):
        with self._lock:
            self._receiving = True
        
        try:
            size_data = conn.recv(8)
            if len(size_data) != 8:
                logger.error("Failed to receive file size")
                conn.send(b"ERROR\n")
                return
            
            file_size = struct.unpack(">Q", size_data)[0]
            logger.info(f"Receiving file of {file_size} bytes")
            
            os.makedirs(os.path.dirname(self.temp_path), exist_ok=True)
            
            received = 0
            with open(self.temp_path, "wb") as f:
                while received < file_size:
                    chunk = conn.recv(min(65536, file_size - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
            
            if received == file_size:
                os.replace(self.temp_path, self.dest_path)
                logger.info(f"File received successfully: {self.dest_path}")
                conn.send(b"OK\n")
            else:
                logger.error(f"Incomplete transfer: {received}/{file_size}")
                conn.send(b"ERROR\n")
                if os.path.exists(self.temp_path):
                    os.remove(self.temp_path)
                    
        except Exception as e:
            logger.error(f"File receive error: {e}")
            conn.send(b"ERROR\n")
            if os.path.exists(self.temp_path):
                os.remove(self.temp_path)
        finally:
            with self._lock:
                self._receiving = False
            conn.close()

    def stop(self):
        self._running = False
        if self.socket:
            self.socket.close()


class VideoPlayerController:
    """Main controller coordinating all components."""

    def __init__(self, config: dict):
        self.config = config
        self.player = VideoPlayer(config["video_file"], config["audio_output"])
        self.multicast = MulticastListener(config["multicast_group"], config["multicast_port"])
        self.file_receiver = FileReceiver(
            config["file_transfer_port"],
            config["video_file"],
            config["temp_video_file"]
        )
        self._running = False
        self._threads = []

    def _handle_command(self, data: bytes, addr):
        command = data.strip().upper()
        
        if command == b"PLAY":
            if self.file_receiver.is_receiving():
                logger.warning("Cannot play - file transfer in progress")
                return
            if not self.player.is_playing():
                self.player.play()
            else:
                logger.info("Already playing")
                
        elif command == b"STOP":
            self.player.stop()
        
        elif command == b"LOAD":
            if self.file_receiver.is_receiving():
                logger.warning("Cannot load - file transfer in progress")
                return
            if not self.player.is_playing():
                self.player.preload()
            else:
                logger.info("Already playing/loaded")
        
        elif command == b"GO":
            self.player.go()
            
        else:
            logger.debug(f"Unknown command: {command}")

    def _can_receive_file(self) -> bool:
        return not self.player.is_playing()

    def start(self):
        self._running = True
        
        logger.info("Starting Video Player Controller")
        logger.info(f"Video file: {self.config['video_file']}")
        logger.info(f"Multicast: {self.config['multicast_group']}:{self.config['multicast_port']}")
        logger.info(f"File transfer port: {self.config['file_transfer_port']}")
        
        multicast_thread = threading.Thread(
            target=self.multicast.start,
            args=(self._handle_command,),
            daemon=True
        )
        multicast_thread.start()
        self._threads.append(multicast_thread)
        
        file_thread = threading.Thread(
            target=self.file_receiver.start,
            args=(self._can_receive_file,),
            daemon=True
        )
        file_thread.start()
        self._threads.append(file_thread)
        
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        
        self.stop()

    def stop(self):
        logger.info("Shutting down...")
        self._running = False
        self.player.stop()
        self.multicast.stop()
        self.file_receiver.stop()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Raspberry Pi Video Player (omxplayer)")
    parser.add_argument("--video", default=CONFIG["video_file"],
                        help="Path to video file")
    parser.add_argument("--multicast-group", default=CONFIG["multicast_group"],
                        help="Multicast group address")
    parser.add_argument("--multicast-port", type=int, default=CONFIG["multicast_port"],
                        help="Multicast port")
    parser.add_argument("--transfer-port", type=int, default=CONFIG["file_transfer_port"],
                        help="File transfer port")
    parser.add_argument("--audio", default="hdmi", choices=["hdmi", "local", "both"],
                        help="Audio output")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    config = CONFIG.copy()
    config["video_file"] = args.video
    config["temp_video_file"] = args.video + ".tmp"
    config["multicast_group"] = args.multicast_group
    config["multicast_port"] = args.multicast_port
    config["file_transfer_port"] = args.transfer_port
    config["audio_output"] = args.audio
    
    os.makedirs(os.path.dirname(config["video_file"]), exist_ok=True)
    
    controller = VideoPlayerController(config)
    controller.start()


if __name__ == "__main__":
    main()
