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
// ─── Image Viewer – ultra smooth, throttled drag ────────
var viewer = {
    images: [],
    currentIndex: 0,
    scale: 1,
    panX: 0,
    panY: 0,
    isDragging: false,
    startX: 0, startY: 0,
    startPanX: 0, startPanY: 0,
    imgElement: null,
    container: null,
    counterElement: null,
    currentSrc: null,
    rafId: null,                // for throttling drag updates

    init: function() {
        if (!document.getElementById('imageViewer')) this.buildModal();
        this.imgElement = document.getElementById('viewerImage');
        this.container = document.getElementById('viewerContainer');
        this.counterElement = document.getElementById('viewerCounter');
        this.attachEvents();
        // GPU acceleration hints
        this.imgElement.style.willChange = 'transform';
    },

    buildModal: function() {
        var modal = document.createElement('div');
        modal.id = 'imageViewer';
        modal.style.cssText = 'display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:10000; backdrop-filter:blur(5px); align-items:center; justify-content:center; flex-direction:column;';
        modal.innerHTML = `
            <div style="position:absolute; top:20px; right:30px; z-index:10001;">
                <button onclick="viewer.close()" style="background:none; border:none; color:#fff; font-size:32px; cursor:pointer;">✕</button>
            </div>
            <div style="position:absolute; top:20px; left:30px; z-index:10001; color:#fff; font-size:18px;" id="viewerCounter">1 / 1</div>
            <div style="display:flex; align-items:center; justify-content:center; width:100%; height:calc(100% - 120px);">
                <button onclick="viewer.prev()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">‹</button>
                <div style="position:relative; width:80%; height:100%; overflow:hidden; display:flex; align-items:center; justify-content:center;" id="viewerContainer">
                    <img id="viewerImage" src="" alt="Image" style="max-width:90%; max-height:90%; object-fit:contain; cursor:grab; transform-origin:center center; will-change:transform; backface-visibility:hidden;">
                </div>
                <button onclick="viewer.next()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">›</button>
            </div>
            <div style="position:absolute; bottom:30px; left:50%; transform:translateX(-50%); display:flex; gap:20px; color:#fff; font-size:16px;">
                <button onclick="viewer.zoomIn()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍+</button>
                <button onclick="viewer.zoomOut()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍−</button>
                <button onclick="viewer.reset()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">⟲ Reset</button>
            </div>
        `;
        document.body.appendChild(modal);
    },

    open: function(images, index) {
        this.images = images;
        this.currentIndex = index || 0;
        this.scale = 1;
        this.panX = 0;
        this.panY = 0;
        this.currentSrc = null;
        this._applyTransform(true);
        document.getElementById('imageViewer').style.display = 'flex';
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', this.keyHandler);
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
    },

    // ─── Apply transform (with optional image load) ──
    _applyTransform: function(forceLoad) {
        if (!this.images.length) return;
        var img = this.images[this.currentIndex];
        var newSrc = 'data:' + (img.mime || 'image/png') + ';base64,' + img.b64;
        if (newSrc !== this.currentSrc || forceLoad) {
            this.imgElement.src = newSrc;
            this.currentSrc = newSrc;
        }
        this.counterElement.textContent = (this.currentIndex+1) + ' / ' + this.images.length;
        this.imgElement.style.transform = 'translate3d(' + this.panX + 'px, ' + this.panY + 'px, 0) scale(' + this.scale + ')';
    },

    // Public update – schedules a transform update via RAF
    update: function(force) {
        if (this.rafId) {
            cancelAnimationFrame(this.rafId);
            this.rafId = null;
        }
        var self = this;
        this.rafId = requestAnimationFrame(function() {
            self._applyTransform(force || false);
            self.rafId = null;
        });
    },

    next: function() {
        if (this.currentIndex < this.images.length - 1) {
            this.currentIndex++;
            this.scale = 1;
            this.panX = 0;
            this.panY = 0;
            this.currentSrc = null;
            this.update(true);
        }
    },

    prev: function() {
        if (this.currentIndex > 0) {
            this.currentIndex--;
            this.scale = 1;
            this.panX = 0;
            this.panY = 0;
            this.currentSrc = null;
            this.update(true);
        }
    },

    zoomIn: function() {
        this.scale = Math.min(this.scale * 1.5, 10);
        this.update();
    },

    zoomOut: function() {
        this.scale = Math.max(this.scale / 1.5, 0.5);
        if (this.scale === 1) { this.panX = 0; this.panY = 0; }
        this.update();
    },

    reset: function() {
        this.scale = 1;
        this.panX = 0;
        this.panY = 0;
        this.update();
    },

    attachEvents: function() {
        var self = this;
        var imgEl = this.imgElement;
        var container = this.container;

        // ── Double‑click to toggle zoom ──
        imgEl.addEventListener('dblclick', function(e) {
            e.stopPropagation();
            if (self.scale === 1) {
                self.scale = 2;
            } else {
                self.scale = 1;
                self.panX = 0;
                self.panY = 0;
            }
            self.update();
        });

        // ── Drag to pan (only when zoomed > 1) ──
        imgEl.addEventListener('mousedown', function(e) {
            if (self.scale <= 1) return;
            self.isDragging = true;
            self.startX = e.clientX;
            self.startY = e.clientY;
            self.startPanX = self.panX;
            self.startPanY = self.panY;
            this.style.cursor = 'grabbing';
            e.stopPropagation();
            e.preventDefault();
        });

        // Use document-level mousemove/mouseup with RAF throttling
        document.addEventListener('mousemove', function(e) {
            if (!self.isDragging) return;
            var dx = e.clientX - self.startX;
            var dy = e.clientY - self.startY;
            self.panX = self.startPanX + dx;
            self.panY = self.startPanY + dy;
            self.update();   // scheduled via RAF
            e.stopPropagation();
            e.preventDefault();
        });

        document.addEventListener('mouseup', function(e) {
            if (self.isDragging) {
                self.isDragging = false;
                imgEl.style.cursor = 'grab';
                e.stopPropagation();
                e.preventDefault();
                // Flush any pending update
                if (self.rafId) {
                    cancelAnimationFrame(self.rafId);
                    self.rafId = null;
                    self._applyTransform();
                }
            }
        });

        // ── Mouse wheel zoom (throttled) ──
        var wheelTimeout = null;
        container.addEventListener('wheel', function(e) {
            e.preventDefault();
            e.stopPropagation();
            if (wheelTimeout) return;
            wheelTimeout = setTimeout(function() { wheelTimeout = null; }, 20);
            var delta = e.deltaY > 0 ? 0.9 : 1.1;
            self.scale = Math.min(Math.max(self.scale * delta, 0.5), 10);
            if (self.scale === 1) { self.panX = 0; self.panY = 0; }
            self.update();
        }, { passive: false });
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