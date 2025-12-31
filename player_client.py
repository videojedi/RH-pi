#!/usr/bin/env python3
"""
Client for controlling the Raspberry Pi Video Player.
- Send multicast play/stop/load/go commands
- Transfer video files to the player
"""

import socket
import struct
import sys
import os
import argparse


def send_multicast_command(command: str, group: str, port: int):
    """Send a multicast UDP command."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.sendto(command.upper().encode(), (group, port))
    sock.close()
    print(f"Sent '{command.upper()}' to {group}:{port}")


def send_file(filepath: str, host: str, port: int):
    """Send a video file to the player."""
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        return False
    
    file_size = os.path.getsize(filepath)
    print(f"Sending {filepath} ({file_size} bytes) to {host}:{port}")
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((host, port))
        
        response = sock.recv(16).decode().strip()
        if response == "BUSY":
            print("Error: Player is busy (playback in progress)")
            sock.close()
            return False
        elif response != "READY":
            print(f"Error: Unexpected response: {response}")
            sock.close()
            return False
        
        print("Player ready, starting transfer...")
        sock.send(struct.pack(">Q", file_size))
        
        sent = 0
        with open(filepath, "rb") as f:
            while sent < file_size:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
                progress = int((sent / file_size) * 100)
                print(f"\rProgress: {progress}%", end="", flush=True)
        
        print()
        sock.settimeout(30)
        response = sock.recv(16).decode().strip()
        
        if response == "OK":
            print("File transferred successfully")
            return True
        else:
            print(f"Transfer failed: {response}")
            return False
            
    except socket.timeout:
        print("Error: Connection timeout")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="Control Raspberry Pi Video Player",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s play                          # Start playback immediately
  %(prog)s stop                          # Stop playback
  %(prog)s load                          # Preload video (paused)
  %(prog)s go                            # Start preloaded video
  %(prog)s send video.mp4 192.168.1.100  # Send video file to specific Pi

For synchronized playback across multiple Pis:
  %(prog)s load                          # All Pis load and pause
  %(prog)s go                            # All Pis start together
"""
    )
    
    parser.add_argument("command", choices=["play", "stop", "load", "go", "send"],
                        help="Command to send")
    parser.add_argument("args", nargs="*", help="Additional arguments")
    parser.add_argument("-g", "--group", default="239.255.42.1",
                        help="Multicast group (default: 239.255.42.1)")
    parser.add_argument("-p", "--port", type=int, default=5000,
                        help="Port (default: 5000 for commands, 5001 for file transfer)")
    
    args = parser.parse_args()
    
    if args.command in ["play", "stop", "load", "go"]:
        send_multicast_command(args.command, args.group, args.port)
    elif args.command == "send":
        if len(args.args) < 2:
            print("Usage: send <file> <host> [-p port]")
            sys.exit(1)
        port = args.port if args.port != 5000 else 5001
        success = send_file(args.args[0], args.args[1], port)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
