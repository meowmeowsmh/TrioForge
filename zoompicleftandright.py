# zoompicleftandright.py
# Image viewer – ultra‑smooth, GPU‑accelerated.

from flask import Blueprint, jsonify, Response

# ---------- internal state ----------
_get_conversation = None


# ---------- blueprint ----------
viewer_bp = Blueprint('viewer', __name__, url_prefix='/viewer')


@viewer_bp.route('/conversations/<cid>/images', methods=['GET'])
def get_conversation_images(cid):
    """Return all images (base64) from a conversation."""
    if _get_conversation is None:
        return jsonify({'error': 'Viewer not initialized'}), 500
    conv = _get_conversation(cid)
    if conv is None:
        return jsonify({'error': 'Conversation not found'}), 404
    images = []
    for msg in conv.get('messages', []):
        for img in msg.get('images', []):
            images.append({
                'b64': img.get('b64', ''),
                'name': img.get('name', 'image'),
                'mime': img.get('mime', 'image/png')
            })
    return jsonify(images)


@viewer_bp.route('/static/viewer.js')
def serve_viewer_js():
    """Serve the viewer JavaScript – ultra smooth."""
    js = """
// ─── Image Viewer – zoom-to-cursor, pinch, clamped pan, preloaded neighbors ────────
var viewer = {
    images: [],
    currentIndex: 0,
    scale: 1,
    panX: 0,
    panY: 0,
    minScale: 1,
    maxScale: 8,
    isDragging: false,
    startX: 0, startY: 0,
    startPanX: 0, startPanY: 0,
    imgElement: null,
    container: null,
    counterElement: null,
    loaderElement: null,
    currentSrc: null,
    rafId: null,                // for throttling drag/zoom updates
    _preloadCache: {},          // index -> HTMLImageElement, decoded ahead of time
    _pinch: null,               // active two-finger pinch state, or null

    init: function() {
        if (!document.getElementById('imageViewer')) this.buildModal();
        this.imgElement = document.getElementById('viewerImage');
        this.container = document.getElementById('viewerContainer');
        this.counterElement = document.getElementById('viewerCounter');
        this.loaderElement = document.getElementById('viewerLoader');
        this.attachEvents();
        // GPU acceleration hints
        this.imgElement.style.willChange = 'transform';
    },

    buildModal: function() {
        var modal = document.createElement('div');
        modal.id = 'imageViewer';
        modal.style.cssText = 'display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:10000; backdrop-filter:blur(5px); align-items:center; justify-content:center; flex-direction:column; touch-action:none;';
        modal.innerHTML = `
            <div style="position:absolute; top:20px; right:30px; z-index:10001;">
                <button onclick="viewer.close()" style="background:none; border:none; color:#fff; font-size:32px; cursor:pointer;">✕</button>
            </div>
            <div style="position:absolute; top:20px; left:30px; z-index:10001; color:#fff; font-size:18px;" id="viewerCounter">1 / 1</div>
            <div style="position:absolute; top:56px; left:30px; z-index:10001; color:#ccc; font-size:13px;" id="viewerZoomLabel">100%</div>
            <div style="display:flex; align-items:center; justify-content:center; width:100%; height:calc(100% - 120px);">
                <button onclick="viewer.prev()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">‹</button>
                <div style="position:relative; width:80%; height:100%; overflow:hidden; display:flex; align-items:center; justify-content:center;" id="viewerContainer">
                    <div id="viewerLoader" style="display:none; position:absolute; width:44px; height:44px; border:4px solid rgba(255,255,255,0.25); border-top-color:#fff; border-radius:50%; animation:viewerSpin 0.8s linear infinite;"></div>
                    <img id="viewerImage" src="" alt="Image" draggable="false" style="max-width:90%; max-height:90%; object-fit:contain; cursor:grab; transform-origin:center center; will-change:transform; backface-visibility:hidden; opacity:1; transition:opacity 0.15s ease;">
                </div>
                <button onclick="viewer.next()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">›</button>
            </div>
            <div style="position:absolute; bottom:30px; left:50%; transform:translateX(-50%); display:flex; gap:20px; color:#fff; font-size:16px;">
                <button onclick="viewer.zoomIn()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍+</button>
                <button onclick="viewer.zoomOut()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍−</button>
                <button onclick="viewer.reset()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">⟲ Reset</button>
                <button onclick="viewer.download()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">⬇ Save</button>
            </div>
        `;
        document.body.appendChild(modal);

        if (!document.getElementById('viewerSpinStyle')) {
            var style = document.createElement('style');
            style.id = 'viewerSpinStyle';
            style.textContent = '@keyframes viewerSpin { to { transform: rotate(360deg); } }';
            document.head.appendChild(style);
        }
    },

    open: function(images, index) {
        this.images = images;
        this.currentIndex = index || 0;
        this._preloadCache = {};
        this.currentSrc = null;
        this._loadCurrent(true);
        document.getElementById('imageViewer').style.display = 'flex';
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', this.keyHandler);
        this._preloadNeighbors();
    },

    close: function() {
        document.getElementById('imageViewer').style.display = 'none';
        document.body.style.overflow = '';
        document.removeEventListener('keydown', this.keyHandler);
        if (this.rafId) {
            cancelAnimationFrame(this.rafId);
            this.rafId = null;
        }
    },

    keyHandler: function(e) {
        if (e.key === 'Escape') viewer.close();
        else if (e.key === 'ArrowLeft') viewer.prev();
        else if (e.key === 'ArrowRight') viewer.next();
        else if (e.key === '+' || e.key === '=') viewer.zoomIn();
        else if (e.key === '-') viewer.zoomOut();
        else if (e.key === '0') viewer.reset();
    },

    _srcFor: function(img) {
        return 'data:' + (img.mime || 'image/png') + ';base64,' + img.b64;
    },

    // ─── Load the current image, preferring an already-decoded preload ──
    _loadCurrent: function(resetView) {
        if (!this.images.length) return;
        var img = this.images[this.currentIndex];
        var newSrc = this._srcFor(img);
        var self = this;

        if (resetView) {
            this.scale = this.minScale;
            this.panX = 0;
            this.panY = 0;
        }
        this.counterElement.textContent = (this.currentIndex + 1) + ' / ' + this.images.length;

        if (newSrc === this.currentSrc) {
            this._applyTransform();
            return;
        }

        var cached = this._preloadCache[this.currentIndex];
        if (cached && cached.complete) {
            this._swapSrc(newSrc);
            return;
        }

        this.loaderElement.style.display = 'block';
        this.imgElement.style.opacity = '0';
        var loader = new Image();
        loader.onload = function() {
            if (self.images[self.currentIndex] !== img) return; // moved on before this resolved
            self._preloadCache[self.currentIndex] = loader;
            self._swapSrc(newSrc);
        };
        loader.onerror = function() {
            self.loaderElement.style.display = 'none';
        };
        loader.src = newSrc;
    },

    _swapSrc: function(newSrc) {
        this.imgElement.src = newSrc;
        this.currentSrc = newSrc;
        this.loaderElement.style.display = 'none';
        this.imgElement.style.opacity = '1';
        this._applyTransform();
    },

    // Decode neighboring images ahead of time so next/prev feels instant
    _preloadNeighbors: function() {
        var self = this;
        [this.currentIndex - 1, this.currentIndex + 1].forEach(function(i) {
            if (i < 0 || i >= self.images.length || self._preloadCache[i]) return;
            var img = new Image();
            img.src = self._srcFor(self.images[i]);
            self._preloadCache[i] = img;
        });
    },

    // ─── Keep pan within bounds so the image can't be dragged fully off-screen ──
    _clampPan: function() {
        if (!this.container) return;
        var rect = this.container.getBoundingClientRect();
        var imgRect = this.imgElement.getBoundingClientRect();
        if (!imgRect.width || !imgRect.height) return;
        var baseW = imgRect.width / this.scale;
        var baseH = imgRect.height / this.scale;
        var scaledW = baseW * this.scale;
        var scaledH = baseH * this.scale;
        var maxX = Math.max(0, (scaledW - rect.width) / 2);
        var maxY = Math.max(0, (scaledH - rect.height) / 2);
        this.panX = Math.max(-maxX, Math.min(maxX, this.panX));
        this.panY = Math.max(-maxY, Math.min(maxY, this.panY));
    },

    _applyTransform: function() {
        this._clampPan();
        this.imgElement.style.transform = 'translate3d(' + this.panX + 'px, ' + this.panY + 'px, 0) scale(' + this.scale + ')';
        var label = document.getElementById('viewerZoomLabel');
        if (label) label.textContent = Math.round(this.scale * 100) + '%';
    },

    // Public update – schedules a transform update via RAF (throttles drag/wheel/pinch)
    update: function() {
        if (this.rafId) return;
        var self = this;
        this.rafId = requestAnimationFrame(function() {
            self._applyTransform();
            self.rafId = null;
        });
    },

    next: function() {
        if (this.currentIndex < this.images.length - 1) {
            this.currentIndex++;
            this._loadCurrent(true);
            this._preloadNeighbors();
        }
    },

    prev: function() {
        if (this.currentIndex > 0) {
            this.currentIndex--;
            this._loadCurrent(true);
            this._preloadNeighbors();
        }
    },

    // ─── Zoom centered on a viewport point (cursor, pinch midpoint, or button = center) ──
    zoomAt: function(clientX, clientY, newScale) {
        newScale = Math.max(this.minScale, Math.min(this.maxScale, newScale));
        var rect = this.container.getBoundingClientRect();
        var cx = clientX - (rect.left + rect.width / 2);
        var cy = clientY - (rect.top + rect.height / 2);
        var ratio = newScale / this.scale;
        this.panX = cx - (cx - this.panX) * ratio;
        this.panY = cy - (cy - this.panY) * ratio;
        this.scale = newScale;
        if (this.scale <= this.minScale) { this.panX = 0; this.panY = 0; }
        this.update();
    },

    _withTransition: function(fn) {
        var self = this;
        this.imgElement.style.transition = 'transform 0.15s ease, opacity 0.15s ease';
        fn();
        setTimeout(function() { self.imgElement.style.transition = 'opacity 0.15s ease'; }, 160);
    },

    zoomIn: function() {
        var rect = this.container.getBoundingClientRect();
        this._withTransition(function() {
            viewer.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, viewer.scale * 1.5);
        });
    },

    zoomOut: function() {
        var rect = this.container.getBoundingClientRect();
        this._withTransition(function() {
            viewer.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, viewer.scale / 1.5);
        });
    },

    reset: function() {
        this._withTransition(function() {
            viewer.scale = viewer.minScale;
            viewer.panX = 0;
            viewer.panY = 0;
            viewer.update();
        });
    },

    download: function() {
        if (!this.images.length) return;
        var img = this.images[this.currentIndex];
        var a = document.createElement('a');
        a.href = this._srcFor(img);
        a.download = img.name || 'image';
        document.body.appendChild(a);
        a.click();
        a.remove();
    },

    attachEvents: function() {
        var self = this;
        var imgEl = this.imgElement;
        var container = this.container;

        // ── Double‑click / double‑tap: zoom toward the point clicked ──
        imgEl.addEventListener('dblclick', function(e) {
            e.stopPropagation();
            self._withTransition(function() {
                if (self.scale <= self.minScale + 0.01) {
                    self.zoomAt(e.clientX, e.clientY, 2.5);
                } else {
                    self.scale = self.minScale;
                    self.panX = 0;
                    self.panY = 0;
                    self.update();
                }
            });
        });

        // ── Drag to pan (only meaningful when zoomed in) ──
        imgEl.addEventListener('mousedown', function(e) {
            if (self.scale <= self.minScale) return;
            self.isDragging = true;
            self.startX = e.clientX;
            self.startY = e.clientY;
            self.startPanX = self.panX;
            self.startPanY = self.panY;
            imgEl.style.transition = 'none';
            imgEl.style.cursor = 'grabbing';
            e.stopPropagation();
            e.preventDefault();
        });

        document.addEventListener('mousemove', function(e) {
            if (!self.isDragging) return;
            self.panX = self.startPanX + (e.clientX - self.startX);
            self.panY = self.startPanY + (e.clientY - self.startY);
            self.update();
            e.stopPropagation();
            e.preventDefault();
        });

        document.addEventListener('mouseup', function() {
            if (self.isDragging) {
                self.isDragging = false;
                imgEl.style.cursor = 'grab';
            }
        });

        // ── Mouse wheel zoom, centered on the cursor position ──
        var wheelTimeout = null;
        container.addEventListener('wheel', function(e) {
            e.preventDefault();
            e.stopPropagation();
            if (wheelTimeout) return;
            wheelTimeout = setTimeout(function() { wheelTimeout = null; }, 16);
            imgEl.style.transition = 'none';
            var factor = e.deltaY > 0 ? 0.9 : 1.1;
            self.zoomAt(e.clientX, e.clientY, self.scale * factor);
        }, { passive: false });

        // ── Touch: one-finger pan, two-finger pinch-to-zoom ──
        container.addEventListener('touchstart', function(e) {
            imgEl.style.transition = 'none';
            if (e.touches.length === 1 && self.scale > self.minScale) {
                var t = e.touches[0];
                self.isDragging = true;
                self.startX = t.clientX;
                self.startY = t.clientY;
                self.startPanX = self.panX;
                self.startPanY = self.panY;
            } else if (e.touches.length === 2) {
                self.isDragging = false;
                var t0 = e.touches[0], t1 = e.touches[1];
                self._pinch = {
                    startDist: Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY),
                    startScale: self.scale,
                    cx: (t0.clientX + t1.clientX) / 2,
                    cy: (t0.clientY + t1.clientY) / 2
                };
            }
        }, { passive: true });

        container.addEventListener('touchmove', function(e) {
            if (e.touches.length === 1 && self.isDragging) {
                e.preventDefault();
                var t = e.touches[0];
                self.panX = self.startPanX + (t.clientX - self.startX);
                self.panY = self.startPanY + (t.clientY - self.startY);
                self.update();
            } else if (e.touches.length === 2 && self._pinch) {
                e.preventDefault();
                var t0 = e.touches[0], t1 = e.touches[1];
                var dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
                var newScale = self._pinch.startScale * (dist / self._pinch.startDist);
                self.zoomAt(self._pinch.cx, self._pinch.cy, newScale);
            }
        }, { passive: false });

        container.addEventListener('touchend', function(e) {
            if (e.touches.length === 0) {
                self.isDragging = false;
                self._pinch = null;
            }
        }, { passive: true });
    }
};

// ─── Helper to open viewer from a chat image ──────────────
function openImageViewerFromChat(imageB64, mime) {
    if (!currentConv) {
        console.warn('No conversation selected');
        return;
    }
    fetch('/viewer/conversations/' + currentConv + '/images')
        .then(r => r.json())
        .then(images => {
            if (!images.length) return;
            var idx = images.findIndex(i => i.b64 === imageB64);
            if (idx === -1) idx = 0;
            viewer.open(images, idx);
        })
        .catch(err => console.warn('Could not load images:', err));
}

// ─── Initialise on load ────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    viewer.init();
});
"""
    return Response(js, mimetype='application/javascript')


# ---------- HTML snippet ----------
VIEWER_MODAL = ""
VIEWER_SCRIPT_TAG = '<script src="/viewer/static/viewer.js"></script>'


def setup_viewer(app, conv_getter):
    global _get_conversation
    _get_conversation = conv_getter
    app.register_blueprint(viewer_bp)


def get_viewer_html():
    return VIEWER_MODAL + VIEWER_SCRIPT_TAG