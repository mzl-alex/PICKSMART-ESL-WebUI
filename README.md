# Picksmart NEMR E-Ink Price Tag – Reverse Engineered

Unofficial Python driver and browser-based designer for the **Picksmart NEMR 2.13″ tri-color BLE e-ink price tag** (black / red / white, 250 × 122 px).

We captured real BLE traffic with an HCI sniffer, reconstructed the actual wire protocol byte-by-byte.

---

## Features

| | |
|---|---|
| **WebUI designer** | Drag-and-drop canvas with text, QR codes, barcodes, and image upload |
| **Three colors** | Per-element color selection: black, red, white |
| **Layer preview** | Live split-view of black plane and red plane before sending |
| **CLI test tool** | Scan, send test patterns, push image files, clear display |
| **ESP32 support** | Works with an ESP32 flashed as a BT HCI controller (no dedicated dongle needed) |
| **Configurable** | BLE address and HTTP port via CLI flags or environment variables |

---

## Hardware

**Tag:** Picksmart NEMR (also labeled as *Gicisky*) 2.13″ tri-color e-ink price tag.
- Display: 250 × 122 px (landscape), black / red / white
- Protocol: Bluetooth Low Energy (BLE), proprietary binary protocol
- Identifier: Usually starts with `FF:FF:…`

**Bluetooth adapter:** Any Linux BLE adapter (hci0) works. We used an **ESP32 as a BT HCI controller** over USB-UART (CH340).

---

## Protocol Documentation

> This section documents the **actual** protocol, reconstructed from two HCI snoop captures (`btsnoop` / H4 format).

### Buffer layout

The tag accepts an **8 000-byte** binary payload per refresh:

```
Offset 0    – 3999  : Black plane  (4 000 bytes)
Offset 4000 – 7999  : Red plane    (4 000 bytes)
```

### Plane encoding

Each plane is a **portrait** bitmap:
- **128 px wide** × **250 rows** (stride = 16 bytes/row, MSB-first bit order)
- The visible area is **250 × 122 px landscape**; the bottom 6 rows are padding

**Bit polarity:**

| Plane | Bit = 1 | Bit = 0 |
|-------|---------|---------|
| Black | White   | Black   |
| Red   | Red     | Not red |

### Encoding pipeline

The display hardware rotates and mirrors the portrait buffer internally. To encode a landscape image correctly, apply the **inverse** transform before storing it:

```
Landscape input (250 × 122)
        │
        ▼
  mirror horizontally
        │
        ▼
  rotate 90° clockwise
        │
        ▼
  Portrait buffer (128 × 250)  →  stored into plane
```

### BLE wire format

**Characteristic UUIDs:**
- Command handle: `0000fef1-0000-1000-8000-00805f9b34fb`
- Data handle:    `0000fef2-0000-1000-8000-00805f9b34fb`

**Transfer sequence:**

1. Write to CMD handle (with response):
   ```
   01
   02 40 1f 00 00 01
   03
   ```

2. Write to DATA handle (without response), **34 packets** total:
   ```
   Packet format: [index : 1 byte] [00 00 00 : 3 bytes] [data : 240 bytes]
   Packets 0–32 : 240 bytes of payload  (total: 33 × 240 = 7 920 bytes)
   Packet  33   :  80 bytes of payload  (total:  1 ×  80 =    80 bytes)
                                                          = 8 000 bytes ✓
   ```
   A ~80 ms delay between packets is required for reliable delivery.

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/picksmart-eink.git
cd picksmart-eink
pip install -r requirements.txt
```

## Usage

### WebUI designer

```bash
python app.py --address AA:BB:CC:DD:EE:FF

# Or use an environment variable:
EINK_TAG_ADDRESS=AA:BB:CC:DD:EE:FF python app.py
```

Open **http://localhost:5000** in your browser.

**Designer features:**

| Tool | Description |
|------|-------------|
| **Text** | Freely positionable IText; choose font, size, weight, style, and color |
| **QR Code** | Generated server-side in black or red |
| **Barcode** | Code 128, EAN-13, EAN-8, Code 39, UPC-A — black or red |
| **Image** | Upload any image; color separation happens automatically on send |
| **Preview** | Shows combined view + black plane + red plane at 1:1 (250 × 122 px) |
| **Send** | One click transfers the canvas to the tag over BLE |

Keyboard shortcut: `Delete` / `Backspace` removes the selected object.

### CLI test tool

```bash
# Scan for nearby BLE devices and find your tag's MAC address
python test_tag.py scan

# Send a built-in test pattern (black/red split halves)
python test_tag.py test --address AA:BB:CC:DD:EE:FF

# Available patterns: split | corners | checkerboard
python test_tag.py test --address AA:BB:CC:DD:EE:FF --pattern corners

# Send an arbitrary image file (auto-resized to 250×122)
python test_tag.py send --address AA:BB:CC:DD:EE:FF --image photo.jpg

# Clear the display (all white)
python test_tag.py clear --address AA:BB:CC:DD:EE:FF
```

---

## Configuration

| CLI flag | Environment variable | Default | Description |
|----------|---------------------|---------|-------------|
| `--address` | `EINK_TAG_ADDRESS` | `AA:BB:CC:DD:EE:FF` | BLE MAC of the tag |
| `--port`    | `EINK_PORT`        | `5000`              | HTTP port for the WebUI |
| `--host`    | —                  | `0.0.0.0`           | Bind address |

---

## Color separation

When an image is sent to the tag (either from the WebUI or the CLI), pixels are classified as follows:

| Condition | Result |
|-----------|--------|
| `R > 160 and G < 110 and B < 110` | Red plane |
| `R < 128 and G < 128 and B < 128` | Black plane |
| Everything else | White (no ink) |

Design tip: use pure `#FF0000` (red), pure `#000000` (black), and `#FFFFFF` (white). Anti-aliased edges and gradients are mapped to white.

---

## Troubleshooting

**`BLE Error: … -110` (timeout)**
→ Tag is out of range, turned off, or already connected to another host.

**`hciattach` returns immediately without setting up `hci0`**
→ Try a lower baud rate: `./setup_bt.sh /dev/ttyUSB0 9600` or check the ESP32 firmware.

**Display shows garbled content**
→ Verify the BLE address belongs to the correct tag (`python test_tag.py scan`).

---

## Repository structure

```
picksmart-eink/
├── app.py          # Flask WebUI server
├── test_tag.py     # Standalone CLI test utility
├── setup_bt.sh     # ESP32 / serial BT adapter setup helper
├── requirements.txt
└── README.md
```

---

## Acknowledgements

Protocol reconstructed from HCI snoop captures using `btsnoop` / H4 format analysis. No official SDK or source code was used - ground truth is the captured traffic only.

This project was developed with the assistance of **[Claude](https://claude.ai)** (Anthropic).

---

## License

MIT License. See `LICENSE` for details.
