#!/usr/bin/env python3
"""
Picksmart NEMR E-Ink Tag – WebUI Server
----------------------------------------
Serves a browser-based designer for the 250×122 px tri-color
(black / red / white) Picksmart NEMR BLE e-ink price tag.

Usage:
  python app.py [--address <BLE-MAC>] [--port <port>] [--host <host>]

Example:
  python app.py --address FF:FF:92:80:80:02 --port 5000
"""

import argparse
import asyncio
import base64
import os
from io import BytesIO

from flask import Flask, request, jsonify
from PIL import Image, ImageOps
import qrcode
from bleak import BleakClient

try:
    import barcode as bc_lib
    from barcode.writer import ImageWriter
    HAS_BARCODE = True
except ImportError:
    HAS_BARCODE = False

# ── CLI configuration ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Picksmart NEMR E-Ink Tag WebUI")
parser.add_argument(
    "--address",
    default=os.environ.get("EINK_TAG_ADDRESS", "FF:FF:92:80:80:02"),
    help="BLE MAC address of the tag  (env: EINK_TAG_ADDRESS)",
)
parser.add_argument(
    "--port",
    type=int,
    default=int(os.environ.get("EINK_PORT", "5000")),
    help="HTTP port for the WebUI  (env: EINK_PORT)",
)
parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
args = parser.parse_args()

ADDRESS = args.address
app = Flask(__name__)

# ── Protocol constants (verified from HCI captures — do NOT trust the PDF) ───
UUID_CMD  = "0000fef1-0000-1000-8000-00805f9b34fb"
UUID_DATA = "0000fef2-0000-1000-8000-00805f9b34fb"

CANVAS_W   = 250    # visible landscape width
CANVAS_H   = 122    # visible landscape height
DISP_H     = 128    # memory height (122 visible + 6 padding rows)
STRIDE     = 16     # bytes per row  → 128 px, MSB-first
PLANE_ROWS = 250    # rows per plane
PLANE_SIZE = 4000   # bytes per plane (16 × 250)
BUF_SIZE   = 8000   # total buffer (black plane + red plane)
DATA_CHUNK = 240    # payload bytes per BLE packet (34 packets; last one 80 B)


# ── Encoding ──────────────────────────────────────────────────────────────────

def image_to_tag_format(img_bw: Image.Image, img_red: Image.Image) -> bytearray:
    """
    Encode two PIL mode-'1' images (250×122) into the 8000-byte tag buffer.

    Memory layout per plane: portrait 128 px wide (16-byte stride, MSB-first)
    × 250 rows.  The display shows the portrait buffer rotated 90° CW then
    mirrored horizontally.  Encoding = inverse: mirror(landscape).rotate(+90°).

    Buffer split:  0..3999 = black plane,  4000..7999 = red plane.
    Polarity – black: bit 1 = white, bit 0 = black.
               red:   bit 1 = red,   bit 0 = not-red.
    """
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
    """Split a 250×122 RGB image into (img_bw, img_red) mode-'1' planes."""
    img_bw  = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    img_red = Image.new("1", (CANVAS_W, CANVAS_H), 1)
    px = img.resize((CANVAS_W, CANVAS_H)).convert("RGB").load()
    for y in range(CANVAS_H):
        for x in range(CANVAS_W):
            r, g, b = px[x, y]
            if r > 160 and g < 110 and b < 110:
                img_red.putpixel((x, y), 0)
            elif r < 128 and g < 128 and b < 128:
                img_bw.putpixel((x, y), 0)
    return img_bw, img_red


# ── BLE transfer ──────────────────────────────────────────────────────────────

async def ble_send(data: bytearray) -> bool:
    try:
        async with BleakClient(ADDRESS, timeout=35.0) as client:
            await client.write_gatt_char(UUID_CMD, bytearray([0x01]), response=True)
            await client.write_gatt_char(UUID_CMD, bytearray([0x02, 0x40, 0x1f, 0x00, 0x00, 0x01]), response=True)
            await client.write_gatt_char(UUID_CMD, bytearray([0x03]), response=True)
            num_packets = (len(data) + DATA_CHUNK - 1) // DATA_CHUNK
            for i in range(num_packets):
                chunk = data[i * DATA_CHUNK:(i + 1) * DATA_CHUNK]
                payload = bytes([i & 0xFF, 0x00, 0x00, 0x00]) + chunk
                await client.write_gatt_char(UUID_DATA, payload, response=False)
                await asyncio.sleep(0.08)
        return True
    except Exception as e:
        print(f"BLE Error: {e}")
        return False


# ── WebUI HTML ────────────────────────────────────────────────────────────────

def build_html(address: str) -> str:
    return """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width">
  <title>E-Ink Tag Designer</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

    header { background: #1e293b; border-bottom: 2px solid #3b82f6; padding: 10px 20px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
    header h1 { font-size: 1em; color: #f1f5f9; letter-spacing: 0.02em; }
    .badge { font-size: 0.72em; background: #0f172a; color: #64748b; padding: 3px 10px; border-radius: 3px; border: 1px solid #334155; }
    .addr-badge { font-size: 0.72em; color: #34d399; background: #022c22; border: 1px solid #064e3b; padding: 3px 10px; border-radius: 3px; font-family: monospace; }

    .main { display: flex; flex: 1; overflow: hidden; }
    .left { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-start; padding: 20px; gap: 12px; overflow: auto; background: #0f172a; }
    .canvas-border { background: white; border: 3px solid #334155; border-radius: 4px; box-shadow: 0 4px 28px rgba(0,0,0,0.6); flex-shrink: 0; }

    .toolbar { display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }
    .tb { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 6px 13px; border-radius: 4px; cursor: pointer; font-size: 0.78em; }
    .tb:hover { background: #334155; }
    .tb.danger { border-color: #7f1d1d; color: #fca5a5; }
    .tb.danger:hover { background: #7f1d1d; }
    .hint { font-size: 0.67em; color: #475569; }

    .sidebar { width: 295px; flex-shrink: 0; background: #1e293b; border-left: 1px solid #334155; display: flex; flex-direction: column; overflow: hidden; }
    .sb-scroll { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 8px; }

    details { background: #0f172a; border: 1px solid #334155; border-radius: 6px; }
    details summary { padding: 9px 12px; cursor: pointer; font-size: 0.83em; font-weight: 600; color: #93c5fd; list-style: none; display: flex; align-items: center; user-select: none; }
    details summary::-webkit-details-marker { display: none; }
    details summary::after { content: '▶'; margin-left: auto; font-size: 0.65em; color: #475569; transition: transform 0.15s; }
    details[open] summary::after { transform: rotate(90deg); }
    details[open] summary { border-bottom: 1px solid #1e293b; }
    .form { padding: 10px 12px; display: flex; flex-direction: column; gap: 7px; }

    .lbl { font-size: 0.7em; color: #94a3b8; margin-bottom: 2px; display: block; }
    input[type=text], input[type=number], select { width: 100%; background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 6px 8px; border-radius: 4px; font-size: 0.82em; outline: none; }
    input[type=text]:focus, input[type=number]:focus, select:focus { border-color: #3b82f6; }
    .two { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }

    .cprow { display: flex; gap: 4px; }
    .cp { flex: 1; padding: 5px 4px; border: 2px solid #334155; border-radius: 4px; cursor: pointer; font-size: 0.73em; font-weight: 700; text-align: center; transition: border-color 0.1s; }
    .cp:hover { border-color: #64748b; }
    .cp.active { border-color: #60a5fa; }
    .cp-k { background: #111827; color: #e2e8f0; }
    .cp-r { background: #7f1d1d; color: #fca5a5; }
    .cp-w { background: #f1f5f9; color: #0f172a; }

    .slrow { display: flex; align-items: center; gap: 6px; }
    .slrow input[type=range] { flex: 1; accent-color: #3b82f6; }
    .slval { font-size: 0.75em; color: #64748b; min-width: 30px; text-align: right; }

    .add-btn { background: #1d4ed8; color: #fff; border: none; padding: 7px; border-radius: 4px; cursor: pointer; font-size: 0.82em; font-weight: 600; width: 100%; }
    .add-btn:hover { background: #2563eb; }
    .add-btn:active { background: #1e40af; }

    .send-area { border-top: 1px solid #334155; padding: 12px; display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }
    #send-btn { background: #059669; color: #fff; border: none; padding: 13px; border-radius: 6px; cursor: pointer; font-size: 0.95em; font-weight: 700; width: 100%; }
    #send-btn:hover { background: #10b981; }
    #send-btn:disabled { background: #334155; color: #64748b; cursor: not-allowed; }
    #status { text-align: center; font-size: 0.8em; min-height: 18px; color: #94a3b8; }

    #preview-wrap { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 100; align-items: center; justify-content: center; }
    #preview-wrap.show { display: flex; }
    .preview-box { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; display: flex; flex-direction: column; gap: 12px; max-width: 90vw; }
    .preview-box h3 { color: #93c5fd; font-size: 0.9em; }
    .preview-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .preview-col { display: flex; flex-direction: column; gap: 4px; align-items: center; }
    .preview-col span { font-size: 0.7em; color: #64748b; }
    .prev-canvas { border: 1px solid #475569; image-rendering: pixelated; }
    .close-btn { background: #334155; color: #e2e8f0; border: none; padding: 7px 16px; border-radius: 4px; cursor: pointer; font-size: 0.82em; align-self: flex-end; }
    .close-btn:hover { background: #475569; }
    input[type=file] { font-size: 0.78em; padding: 4px; color: #e2e8f0; width: 100%; }
    .note { font-size: 0.7em; color: #64748b; line-height: 1.4; }
  </style>
</head>
<body>

<header>
  <h1>E-Ink Tag Designer</h1>
  <span class="badge">250 × 122 px &nbsp;·&nbsp; Schwarz / Rot / Weiß</span>
  <span class="addr-badge">__ADDRESS__</span>
  <button class="tb" style="margin-left:auto" onclick="showPreview()">Vorschau</button>
</header>

<div class="main">
  <div class="left">
    <div class="canvas-border"><canvas id="c"></canvas></div>
    <div class="toolbar">
      <button class="tb" onclick="bringFwd()">Nach vorne</button>
      <button class="tb" onclick="sendBwd()">Nach hinten</button>
      <button class="tb" onclick="dupl()">Duplizieren</button>
      <button class="tb danger" onclick="delSel()">Löschen</button>
      <button class="tb danger" onclick="clearAll()">Alle löschen</button>
    </div>
    <p class="hint">Klicken = auswählen &nbsp;·&nbsp; Doppelklick Text = bearbeiten &nbsp;·&nbsp; Entf = löschen</p>
  </div>

  <div class="sidebar">
    <div class="sb-scroll">

      <details open>
        <summary>Text</summary>
        <div class="form">
          <div><span class="lbl">Inhalt</span><input type="text" id="t-text" placeholder="Text eingeben..."></div>
          <div>
            <span class="lbl">Farbe</span>
            <div class="cprow">
              <div class="cp cp-k active" id="tc-black" onclick="setColor('t','black')">Schwarz</div>
              <div class="cp cp-r"        id="tc-red"   onclick="setColor('t','red')">Rot</div>
              <div class="cp cp-w"        id="tc-white" onclick="setColor('t','white')">Weiß</div>
            </div>
          </div>
          <div class="two">
            <div><span class="lbl">Größe (px)</span><input type="number" id="t-size" value="16" min="6" max="60"></div>
            <div><span class="lbl">Schrift</span>
              <select id="t-font">
                <option value="Arial">Arial</option>
                <option value="Times New Roman">Times</option>
                <option value="Courier New">Mono</option>
                <option value="Impact">Impact</option>
                <option value="Georgia">Georgia</option>
              </select>
            </div>
          </div>
          <div class="two">
            <div><span class="lbl">Gewicht</span>
              <select id="t-weight"><option value="normal">Normal</option><option value="bold">Fett</option></select>
            </div>
            <div><span class="lbl">Stil</span>
              <select id="t-style"><option value="normal">Normal</option><option value="italic">Kursiv</option></select>
            </div>
          </div>
          <button class="add-btn" onclick="addText()">Text hinzufügen</button>
        </div>
      </details>

      <details>
        <summary>QR Code</summary>
        <div class="form">
          <div><span class="lbl">URL oder Text</span><input type="text" id="qr-text" placeholder="https://..."></div>
          <div>
            <span class="lbl">Farbe</span>
            <div class="cprow">
              <div class="cp cp-k active" id="qrc-black" onclick="setColor('qr','black')">Schwarz</div>
              <div class="cp cp-r"        id="qrc-red"   onclick="setColor('qr','red')">Rot</div>
            </div>
          </div>
          <div><span class="lbl">Größe (px)</span>
            <div class="slrow">
              <input type="range" id="qr-size" min="20" max="110" value="60" oninput="sv('qr-size-v',this.value)">
              <span class="slval" id="qr-size-v">60</span>
            </div>
          </div>
          <button class="add-btn" onclick="addQR()">QR Code hinzufügen</button>
        </div>
      </details>

      <details>
        <summary>Barcode</summary>
        <div class="form">
          <div><span class="lbl">Inhalt</span><input type="text" id="bc-text" placeholder="12345678901..."></div>
          <div><span class="lbl">Format</span>
            <select id="bc-type">
              <option value="code128">Code 128 (beliebig)</option>
              <option value="ean13">EAN-13 (12 Ziffern)</option>
              <option value="ean8">EAN-8 (7 Ziffern)</option>
              <option value="code39">Code 39</option>
              <option value="upc">UPC-A (11 Ziffern)</option>
            </select>
          </div>
          <div>
            <span class="lbl">Farbe</span>
            <div class="cprow">
              <div class="cp cp-k active" id="bcc-black" onclick="setColor('bc','black')">Schwarz</div>
              <div class="cp cp-r"        id="bcc-red"   onclick="setColor('bc','red')">Rot</div>
            </div>
          </div>
          <div><span class="lbl">Breite (px)</span>
            <div class="slrow">
              <input type="range" id="bc-width" min="60" max="240" value="160" oninput="sv('bc-width-v',this.value)">
              <span class="slval" id="bc-width-v">160</span>
            </div>
          </div>
          <button class="add-btn" onclick="addBarcode()">Barcode hinzufügen</button>
        </div>
      </details>

      <details>
        <summary>Bild</summary>
        <div class="form">
          <div><span class="lbl">Datei (JPG, PNG, GIF …)</span><input type="file" id="img-file" accept="image/*" onchange="uploadImg(event)"></div>
          <div><span class="lbl">Max. Breite (px)</span>
            <div class="slrow">
              <input type="range" id="img-width" min="20" max="250" value="120" oninput="sv('img-width-v',this.value)">
              <span class="slval" id="img-width-v">120</span>
            </div>
          </div>
          <p class="note">Tipp: Bilder mit klaren Schwarz-/Rot-/Weißbereichen werden am besten dargestellt.</p>
        </div>
      </details>

    </div>

    <div class="send-area">
      <button id="send-btn" onclick="sendToTag()">An E-Ink Tag senden</button>
      <div id="status"></div>
    </div>
  </div>
</div>

<div id="preview-wrap" onclick="closePreview(event)">
  <div class="preview-box">
    <h3>Vorschau der Ebenen (1:1, 250×122 px)</h3>
    <div class="preview-row">
      <div class="preview-col"><span>Kombiniert</span><canvas id="prev-combined" class="prev-canvas" width="250" height="122"></canvas></div>
      <div class="preview-col"><span>Schwarz-Ebene</span><canvas id="prev-bw" class="prev-canvas" width="250" height="122"></canvas></div>
      <div class="preview-col"><span>Rot-Ebene</span><canvas id="prev-red" class="prev-canvas" width="250" height="122"></canvas></div>
    </div>
    <button class="close-btn" onclick="closePreview(null)">Schließen</button>
  </div>
</div>

<script>
const TAG_W = 250, TAG_H = 122, ZOOM = 3;
const fc = new fabric.Canvas('c', { width: TAG_W * ZOOM, height: TAG_H * ZOOM });
fc.setZoom(ZOOM);
fc.backgroundColor = 'white';
fc.renderAll();

const CLR = { t: 'black', qr: 'black', bc: 'black' };
const CLR_OPTS = { t: ['black','red','white'], qr: ['black','red'], bc: ['black','red'] };

function setColor(pfx, color) {
  CLR[pfx] = color;
  CLR_OPTS[pfx].forEach(c => {
    const el = document.getElementById(`${pfx}c-${c}`);
    if (el) el.classList.toggle('active', c === color);
  });
}
function sv(id, val) { document.getElementById(id).innerText = val; }
function setStatus(msg, color) {
  const el = document.getElementById('status');
  el.innerText = msg;
  el.style.color = color || '#94a3b8';
}

function addText() {
  const obj = new fabric.IText(document.getElementById('t-text').value || 'Text', {
    left: 10, top: 10,
    fill: CLR.t,
    fontSize: parseInt(document.getElementById('t-size').value) || 16,
    fontFamily: document.getElementById('t-font').value,
    fontWeight: document.getElementById('t-weight').value,
    fontStyle: document.getElementById('t-style').value,
  });
  fc.add(obj); fc.setActiveObject(obj);
  setStatus('Text hinzugefügt', '#34d399');
}

async function addQR() {
  const text = document.getElementById('qr-text').value || 'https://example.com';
  setStatus('Generiere QR Code …', '#fbbf24');
  try {
    const res = await fetch('/gen_qr', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: text, color: CLR.qr }) });
    const j = await res.json();
    if (j.error) { setStatus('Fehler: ' + j.error, '#f87171'); return; }
    fabric.Image.fromURL(j.url, img => {
      img.scaleToWidth(parseInt(document.getElementById('qr-size').value));
      img.set({ left: 10, top: 10 }); fc.add(img); fc.setActiveObject(img);
      setStatus('QR Code hinzugefügt', '#34d399');
    });
  } catch { setStatus('Netzwerkfehler', '#f87171'); }
}

async function addBarcode() {
  const text = document.getElementById('bc-text').value;
  if (!text) { setStatus('Bitte Barcode-Inhalt eingeben', '#f87171'); return; }
  setStatus('Generiere Barcode …', '#fbbf24');
  try {
    const res = await fetch('/gen_barcode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: text, type: document.getElementById('bc-type').value, color: CLR.bc }) });
    const j = await res.json();
    if (j.error) { setStatus('Fehler: ' + j.error, '#f87171'); return; }
    fabric.Image.fromURL(j.url, img => {
      img.scaleToWidth(parseInt(document.getElementById('bc-width').value));
      img.set({ left: 10, top: 10 }); fc.add(img); fc.setActiveObject(img);
      setStatus('Barcode hinzugefügt', '#34d399');
    });
  } catch { setStatus('Netzwerkfehler', '#f87171'); }
}

function uploadImg(e) {
  const file = e.target.files[0]; if (!file) return;
  const reader = new FileReader();
  reader.onload = ev => {
    fabric.Image.fromURL(ev.target.result, img => {
      img.scaleToWidth(parseInt(document.getElementById('img-width').value));
      img.set({ left: 10, top: 10 }); fc.add(img); fc.setActiveObject(img);
      setStatus('Bild hinzugefügt', '#34d399');
    });
  };
  reader.readAsDataURL(file);
}

function delSel()  { const o = fc.getActiveObject(); if (o) { fc.remove(o); fc.discardActiveObject(); fc.renderAll(); } }
function clearAll(){ if (!confirm('Alle Elemente löschen?')) return; fc.clear(); fc.backgroundColor = 'white'; fc.renderAll(); }
function bringFwd(){ const o = fc.getActiveObject(); if (o) fc.bringToFront(o); }
function sendBwd() { const o = fc.getActiveObject(); if (o) fc.sendToBack(o); }
function dupl()    { const o = fc.getActiveObject(); if (!o) return; o.clone(cl => { cl.set({ left: o.left+8, top: o.top+8 }); fc.add(cl); fc.setActiveObject(cl); }); }

function showPreview() {
  fc.discardActiveObject(); fc.renderAll();
  const img = new Image();
  img.onload = () => {
    const tmp = document.createElement('canvas'); tmp.width = TAG_W; tmp.height = TAG_H;
    tmp.getContext('2d').drawImage(img, 0, 0, TAG_W, TAG_H);
    const src = tmp.getContext('2d').getImageData(0, 0, TAG_W, TAG_H);
    const cCtx = document.getElementById('prev-combined').getContext('2d');
    const bCtx = document.getElementById('prev-bw').getContext('2d');
    const rCtx = document.getElementById('prev-red').getContext('2d');
    const cD = cCtx.createImageData(TAG_W, TAG_H);
    const bD = bCtx.createImageData(TAG_W, TAG_H);
    const rD = rCtx.createImageData(TAG_W, TAG_H);
    for (let i = 0; i < TAG_W * TAG_H; i++) {
      const r = src.data[i*4], g = src.data[i*4+1], b = src.data[i*4+2];
      const isRed = r > 160 && g < 110 && b < 110;
      const isBk  = !isRed && r < 128 && g < 128 && b < 128;
      [cD.data[i*4],cD.data[i*4+1],cD.data[i*4+2],cD.data[i*4+3]] = [r,g,b,255];
      const bv = isBk ? 0 : 255;
      [bD.data[i*4],bD.data[i*4+1],bD.data[i*4+2],bD.data[i*4+3]] = [bv,bv,bv,255];
      [rD.data[i*4],rD.data[i*4+1],rD.data[i*4+2],rD.data[i*4+3]] = isRed ? [220,50,50,255] : [255,255,255,255];
    }
    cCtx.putImageData(cD,0,0); bCtx.putImageData(bD,0,0); rCtx.putImageData(rD,0,0);
    document.getElementById('preview-wrap').classList.add('show');
  };
  img.src = fc.toDataURL({ format: 'png' });
}
function closePreview(e) {
  if (e === null || e.target === document.getElementById('preview-wrap'))
    document.getElementById('preview-wrap').classList.remove('show');
}

async function sendToTag() {
  const btn = document.getElementById('send-btn');
  btn.disabled = true; setStatus('Verbinde mit Tag …', '#fbbf24');
  fc.discardActiveObject(); fc.renderAll();
  try {
    const res = await fetch('/send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: fc.toDataURL({ format: 'png' }) }) });
    const j = await res.json();
    setStatus(j.status, j.status.startsWith('Erfolg') ? '#34d399' : '#f87171');
  } catch { setStatus('Verbindungsfehler!', '#f87171'); }
  btn.disabled = false;
}

document.addEventListener('keydown', e => {
  if ((e.key === 'Delete' || e.key === 'Backspace') &&
      document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA')
    delSel();
});
</script>
</body>
</html>""".replace("__ADDRESS__", address)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return build_html(ADDRESS)


@app.route("/gen_qr", methods=["POST"])
def gen_qr():
    data = request.json
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(data.get("data", "https://example.com"))
    qr.make(fit=True)
    img = qr.make_image(fill_color=data.get("color", "black"), back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return jsonify({"url": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"})


@app.route("/gen_barcode", methods=["POST"])
def gen_barcode():
    if not HAS_BARCODE:
        return jsonify({"error": "python-barcode nicht installiert – pip install 'python-barcode[images]'"}), 500
    data = request.json
    fg = "#FF0000" if data.get("color") == "red" else "#000000"
    try:
        cls = bc_lib.get_barcode_class(data.get("type", "code128"))
        buf = BytesIO()
        cls(data.get("data", ""), writer=ImageWriter()).write(buf, options={
            "module_width": 0.6, "module_height": 12.0, "font_size": 5,
            "text_distance": 1.5, "background": "white", "foreground": fg, "write_text": True,
        })
        buf.seek(0)
        return jsonify({"url": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/send", methods=["POST"])
def send_data():
    img_data = base64.b64decode(request.json["image"].split(",")[1])
    img = Image.open(BytesIO(img_data)).convert("RGB").resize((CANVAS_W, CANVAS_H))
    img_bw, img_red = rgb_to_planes(img)
    buf = image_to_tag_format(img_bw, img_red)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    success = loop.run_until_complete(ble_send(buf))
    return jsonify({"status": "Erfolgreich übertragen!" if success else "BLE-Fehler beim Senden"})


if __name__ == "__main__":
    print(f"  Tag address : {ADDRESS}")
    print(f"  WebUI       : http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)
