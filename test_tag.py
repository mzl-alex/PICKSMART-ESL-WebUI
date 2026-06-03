#!/usr/bin/env python3
"""
test_tag.py – Picksmart NEMR e-ink tag CLI test utility
---------------------------------------------------------
Scan for devices, send test patterns, clear the display, or push
an arbitrary image file — all without starting the WebUI.

Usage:
  python test_tag.py scan [--timeout 8]
  python test_tag.py test  --address <MAC> [--pattern split|corners|checkerboard]
  python test_tag.py send  --address <MAC> --image <file.jpg>
  python test_tag.py clear --address <MAC>
"""

import argparse
import asyncio
import sys
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps
from bleak import BleakScanner, BleakClient

# ── Protocol constants ────────────────────────────────────────────────────────
UUID_CMD  = "0000fef1-0000-1000-8000-00805f9b34fb"
UUID_DATA = "0000fef2-0000-1000-8000-00805f9b34fb"

CANVAS_W   = 250
CANVAS_H   = 122
DISP_H     = 128
STRIDE     = 16
PLANE_ROWS = 250
PLANE_SIZE = 4000
BUF_SIZE   = 8000
DATA_CHUNK = 240


# ── Encoding ──────────────────────────────────────────────────────────────────

def image_to_tag_format(img_bw: Image.Image, img_red: Image.Image) -> bytearray:
    buffer = bytearray(BUF_SIZE)

    def encode_bits(bit_img: Image.Image, plane_offset: int) -> None:
        portrait = ImageOps.mirror(bit_img).rotate(90, expand=True)
        px = portrait.load()
        for r in range(PLANE_ROWS):
            for bx in range(STRIDE):
                v = 0
                for b in range(8):
                    if px[bx * 8 + b, r]:
                        v |= (0x80 >> b)
                buffer[plane_offset + r * STRIDE + bx] = v

    land_bw = Image.new("1", (CANVAS_W, DISP_H), 1)
    land_bw.paste(img_bw.resize((CANVAS_W, CANVAS_H)).convert("1"), (0, 0))
    encode_bits(land_bw, 0)

    red_in = img_red.resize((CANVAS_W, CANVAS_H)).convert("1").point(lambda p: 0 if p else 1)
    land_red = Image.new("1", (CANVAS_W, DISP_H), 0)
    land_red.paste(red_in, (0, 0))
    encode_bits(land_red, PLANE_SIZE)

    return buffer


def rgb_to_planes(img: Image.Image):
    """Split an RGB image into (img_bw, img_red) mode-'1' planes."""
    img = img.resize((CANVAS_W, CANVAS_H)).convert("RGB")
    img_bw  = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    img_red = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    px = img.load()
    for y in range(CANVAS_H):
        for x in range(CANVAS_W):
            r, g, b = px[x, y]
            if r > 160 and g < 110 and b < 110:
                img_red.putpixel((x, y), 0)
            elif r < 128 and g < 128 and b < 128:
                img_bw.putpixel((x, y), 0)
    return img_bw, img_red


# ── BLE transfer ──────────────────────────────────────────────────────────────

async def ble_send(address: str, data: bytearray) -> None:
    print(f"  Connecting to {address} …")
    async with BleakClient(address, timeout=35.0) as client:
        print("  Connected. Starting transfer …")
        await client.write_gatt_char(UUID_CMD, bytearray([0x01]), response=True)
        await client.write_gatt_char(UUID_CMD, bytearray([0x02, 0x40, 0x1f, 0x00, 0x00, 0x01]), response=True)
        await client.write_gatt_char(UUID_CMD, bytearray([0x03]), response=True)
        num_packets = (len(data) + DATA_CHUNK - 1) // DATA_CHUNK
        for i in range(num_packets):
            chunk = data[i * DATA_CHUNK:(i + 1) * DATA_CHUNK]
            payload = bytes([i & 0xFF, 0x00, 0x00, 0x00]) + chunk
            await client.write_gatt_char(UUID_DATA, payload, response=False)
            await asyncio.sleep(0.08)
            pct = (i + 1) / num_packets * 100
            bar = "#" * int(pct / 5) + "." * (20 - int(pct / 5))
            print(f"\r  [{bar}] {pct:5.1f}%  ({i+1}/{num_packets})", end="", flush=True)
        print("\n  Transfer complete.")


# ── Test pattern factory ──────────────────────────────────────────────────────

def _font(size: int = 16) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_split_pattern() -> Image.Image:
    """Left half black, right half red, with white labels."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, CANVAS_W // 2 - 1, CANVAS_H - 1], fill="black")
    draw.rectangle([CANVAS_W // 2, 0, CANVAS_W - 1, CANVAS_H - 1], fill="red")
    font = _font(20)
    draw.text((8, 48), "BLACK", fill="white", font=font)
    draw.text((132, 48), "RED", fill="white", font=font)
    return img


def make_corners_pattern() -> Image.Image:
    """Color squares in each corner + centered status text."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    sz = 35
    draw.rectangle([0, 0, sz, sz], fill="black")
    draw.rectangle([CANVAS_W - sz - 1, 0, CANVAS_W - 1, sz], fill="red")
    draw.rectangle([0, CANVAS_H - sz - 1, sz, CANVAS_H - 1], fill="red")
    draw.rectangle([CANVAS_W - sz - 1, CANVAS_H - sz - 1, CANVAS_W - 1, CANVAS_H - 1], fill="black")
    font_big  = _font(18)
    font_small = _font(11)
    draw.text((CANVAS_W // 2, CANVAS_H // 2 - 14), "E-INK TAG OK",
              fill="black", font=font_big, anchor="mm")
    draw.text((CANVAS_W // 2, CANVAS_H // 2 + 8),  "picksmart-eink",
              fill="black", font=font_small, anchor="mm")
    return img


def make_checkerboard_pattern() -> Image.Image:
    """Alternating black/red/white checkerboard to stress-test encoding."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    cell = 16
    colors = ["black", "red", "white"]
    for row in range(CANVAS_H // cell + 1):
        for col in range(CANVAS_W // cell + 1):
            c = colors[(row + col) % 3]
            x0, y0 = col * cell, row * cell
            draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=c)
    return img


PATTERNS = {
    "split":        make_split_pattern,
    "corners":      make_corners_pattern,
    "checkerboard": make_checkerboard_pattern,
}


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_scan(timeout: float) -> None:
    print(f"Scanning for BLE devices ({timeout:.0f} s) …\n")
    devices = await BleakScanner.discover(timeout=timeout)
    if not devices:
        print("No devices found.")
        return
    print(f"{'Address':<22} {'RSSI':>6}  Name")
    print("─" * 60)
    for d in sorted(devices, key=lambda x: x.rssi or -999, reverse=True):
        print(f"{d.address:<22} {(str(d.rssi) + ' dBm'):>8}  {d.name or '—'}")


async def cmd_test(address: str, pattern: str) -> None:
    print(f"Pattern: {pattern}")
    img = PATTERNS[pattern]()
    img_bw, img_red = rgb_to_planes(img)
    buf = image_to_tag_format(img_bw, img_red)
    await ble_send(address, buf)


async def cmd_send(address: str, image_path: str) -> None:
    print(f"Loading image: {image_path}")
    img = Image.open(image_path).convert("RGB")
    img_bw, img_red = rgb_to_planes(img)
    buf = image_to_tag_format(img_bw, img_red)
    await ble_send(address, buf)


async def cmd_clear(address: str) -> None:
    print("Clearing display (all white) …")
    img_bw  = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    img_red = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    buf = image_to_tag_format(img_bw, img_red)
    await ble_send(address, buf)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Picksmart NEMR e-ink tag CLI test utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python test_tag.py scan
  python test_tag.py test  --address FF:FF:92:80:80:02
  python test_tag.py test  --address FF:FF:92:80:80:02 --pattern corners
  python test_tag.py send  --address FF:FF:92:80:80:02 --image photo.jpg
  python test_tag.py clear --address FF:FF:92:80:80:02
        """,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan for nearby BLE devices")
    p_scan.add_argument("--timeout", type=float, default=8.0, metavar="SEC")

    p_test = sub.add_parser("test", help="Send a built-in test pattern")
    p_test.add_argument("--address", required=True, metavar="MAC")
    p_test.add_argument("--pattern", choices=list(PATTERNS), default="split")

    p_send = sub.add_parser("send", help="Send an image file to the tag")
    p_send.add_argument("--address", required=True, metavar="MAC")
    p_send.add_argument("--image",   required=True, metavar="FILE")

    p_clear = sub.add_parser("clear", help="Clear the display (all white)")
    p_clear.add_argument("--address", required=True, metavar="MAC")

    args = parser.parse_args()

    try:
        if args.cmd == "scan":
            asyncio.run(cmd_scan(args.timeout))
        elif args.cmd == "test":
            asyncio.run(cmd_test(args.address, args.pattern))
        elif args.cmd == "send":
            asyncio.run(cmd_send(args.address, args.image))
        elif args.cmd == "clear":
            asyncio.run(cmd_clear(args.address))
    except KeyboardInterrupt:
        print("\nAborted.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
