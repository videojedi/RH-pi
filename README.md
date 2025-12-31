# Raspberry Pi Video Player

A lightweight video player for the **original Raspberry Pi** that:
- Plays a single video file on command
- Triggers playback via **multicast UDP** (allowing synchronized start across multiple Pis)
- Supports **LOAD/GO** for tight synchronization (preload paused, then trigger)
- Receives replacement video files over **TCP** (blocked during playback)

## Requirements

- Original Raspberry Pi (Pi 1) with Raspberry Pi OS Legacy (Buster)
- `omxplayer` for hardware-accelerated video playback
- Python 3.7+

## Installation

### 1. Update APT Sources (Buster is archived)

Buster has been archived, so update `/etc/apt/sources.list`:

```bash
sudo nano /etc/apt/sources.list
```

Change to:
```
deb http://legacy.raspbian.org/raspbian/ buster main contrib non-free rpi
```

### 2. Install Dependencies

```bash
sudo apt update
sudo apt install omxplayer
```

### 3. Copy Files to the Pi

```bash
# Create directories
mkdir -p /home/pi/video

# Copy the player script
scp video_player.py pi@<pi-ip>:/home/pi/
```

### 4. Install as System Service

```bash
# Copy service file
sudo cp video-player.service /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable video-player
sudo systemctl start video-player

# Check status
sudo systemctl status video-player

# View logs
journalctl -u video-player -f
```

## Usage

### Running Manually

```bash
# Basic usage
python3 video_player.py

# With options
python3 video_player.py \
    --video /home/pi/video/my_video.mp4 \
    --multicast-group 239.255.42.1 \
    --multicast-port 5000 \
    --transfer-port 5001 \
    --audio hdmi \
    --verbose
```

### Command Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--video` | `/home/pi/video/current_video.mp4` | Path to video file |
| `--multicast-group` | `239.255.42.1` | Multicast group address |
| `--multicast-port` | `5000` | UDP port for commands |
| `--transfer-port` | `5001` | TCP port for file transfer |
| `--audio` | `hdmi` | Audio output: `hdmi`, `local`, or `both` |
| `-v, --verbose` | Off | Enable debug logging |

## Client Usage

Use `player_client.py` from any machine on the network:

### Commands

```bash
# Immediate playback
python3 player_client.py play

# Stop playback
python3 player_client.py stop

# Preload video (paused on first frame)
python3 player_client.py load

# Start preloaded video
python3 player_client.py go

# Send video file to specific Pi
python3 player_client.py send /path/to/video.mp4 192.168.1.100
```

### Synchronized Playback Across Multiple Pis

For tight synchronization:

```bash
# Step 1: All Pis load and pause on first frame
python3 player_client.py load

# Step 2: When ready, trigger all at once
python3 player_client.py go
```

This gives tighter sync since the video is already loaded and buffered — the GO command just sends a single byte to unpause.

### Custom Multicast Settings

```bash
python3 player_client.py play -g 239.255.42.1 -p 5000
```

## Protocol Details

### Multicast Commands (UDP)

- **Address**: Configurable, default `239.255.42.1:5000`
- **Commands**: `PLAY`, `STOP`, `LOAD`, `GO` (case-insensitive)

| Command | Description |
|---------|-------------|
| `PLAY` | Start playback immediately |
| `STOP` | Stop playback |
| `LOAD` | Load video and pause on first frame |
| `GO` | Unpause a loaded video |

### File Transfer Protocol (TCP)

1. Client connects to port 5001
2. Server responds with `READY\n` or `BUSY\n`
3. If ready, client sends 8-byte file size (big-endian uint64)
4. Client sends file data
5. Server responds with `OK\n` or `ERROR\n`

## Video Recommendations

For the original Pi's limited hardware:

- **Codec**: H.264 (hardware decoded)
- **Resolution**: 720p or lower recommended
- **Bitrate**: 5-10 Mbps max
- **Container**: MP4 or MKV

Test playback with:
```bash
omxplayer -o hdmi /path/to/video.mp4
```

## Cloning SD Cards

To deploy to multiple Pis:

### Create Image (on Mac)

```bash
# Find SD card
diskutil list

# Unmount (don't eject)
diskutil unmountDisk /dev/disk4

# Create image (use rdisk for speed)
sudo dd if=/dev/rdisk4 of=~/pi_image.img bs=4m status=progress
```

### Write to New Cards

```bash
diskutil unmountDisk /dev/disk4
sudo dd if=~/pi_image.img of=/dev/rdisk4 bs=4m status=progress
```

## Network Setup

### Multicast Requirements

Ensure your network supports multicast:
- Most home routers work by default
- Enterprise networks may need IGMP snooping enabled
- For multiple VLANs, configure multicast routing

### Firewall Rules

```bash
# If using ufw
sudo ufw allow 5000/udp  # Multicast commands
sudo ufw allow 5001/tcp  # File transfer
```

## Troubleshooting

### Service Commands

```bash
sudo systemctl restart video-player
sudo systemctl status video-player
journalctl -u video-player -f
journalctl -u video-player -n 50
```

### No Video Output

```bash
# Check HDMI is the default output
sudo raspi-config
# Advanced Options -> Audio -> Force HDMI

# Test omxplayer directly
omxplayer -o hdmi test.mp4
```

### Multicast Not Received

```bash
# Check multicast group membership
netstat -g

# Test with netcat
nc -lu 5000
```

### Permission Issues

```bash
# Ensure user is in video/audio groups
sudo usermod -a -G video,audio pi
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Video Player (Pi)                     │
│  ┌─────────────────┐  ┌─────────────────┐              │
│  │   Multicast     │  │  File Receiver  │              │
│  │   Listener      │  │  (TCP:5001)     │              │
│  │  (UDP:5000)     │  │                 │              │
│  └────────┬────────┘  └────────┬────────┘              │
│           │                    │                        │
│           ▼                    ▼                        │
│  ┌─────────────────────────────────────────┐           │
│  │          VideoPlayerController          │           │
│  │  - State management (IDLE/PLAYING)      │           │
│  │  - Coordinates playback & transfers     │           │
│  └─────────────────────┬───────────────────┘           │
│                        │                                │
│                        ▼                                │
│  ┌─────────────────────────────────────────┐           │
│  │              omxplayer                   │           │
│  │  (Hardware-accelerated H.264 decode)    │           │
│  │  Controlled via FIFO pipe               │           │
│  └─────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────┘
```

## Technical Notes

- **FIFO Control**: omxplayer is controlled via a named pipe (`/tmp/omxplayer_fifo`) for reliable command input
- **Preload**: The `LOAD` command starts playback then immediately pauses, buffering the video ready for instant start
- **Multicast**: UDP multicast allows a single packet to trigger all Pis simultaneously

## License

MIT License - Use freely for any purpose.
