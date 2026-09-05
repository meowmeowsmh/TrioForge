#!/usr/bin/env python3
"""
On-demand ComfyUI image generation with live workflow detection.

What this module does
---------------------
1. Detects whether ComfyUI (Comfy Desktop) is running on ``COMFYUI_URL``.
2. Auto-discovers the installed workflows/templates by their full paths on disk:
   * ``<ComfyUI>/blueprints/*.json``          -> bundled templates, e.g.
     ``Text to Image (Z-Image-Turbo).json`` (the ``image_z_image_turbo`` template)
   * ``<ComfyUI>/user/<id>/workflows/*.json`` -> user-saved workflows, e.g.
     ``FHDR-Setup.json``
3. Converts those files (legacy "graph" format or the newer blueprint/subgraph
   format) into the ComfyUI "prompt" API format.
4. Injects the prompt / size / seed / steps, queues the job, downloads the
   finished image and saves it.

No model filenames are hardcoded for discovery: the workflow files and the
model files are found live on the machine.
"""

import json
import os
import random
import time
import uuid
from urllib.parse import quote

import requests

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

# Optional explicit path to the ComfyUI root (the folder that contains
# ``blueprints/`` and ``user/``). When unset it is auto-located.
COMFYUI_INSTALL = os.environ.get("COMFYUI_INSTALL", "")

_APP_DATA = os.environ.get("APPDATA", "")
_LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA", "")

# Scalar widget types in ComfyUI's /object_info. Anything else (a list == COMBO)
# is also a widget; connection types (MODEL, CLIP, ...) are not.
_WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}

_object_info_cache = None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def is_comfyui_running(timeout=3):
    """Return True if a ComfyUI server answers on COMFYUI_URL."""
    try:
        r = requests.get(f"{COMFYUI_URL}/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def detect_comfyui(timeout=3):
    """Return a small status dict describing the running ComfyUI server."""
    info = {"url": COMFYUI_URL, "running": False}
    try:
        r = requests.get(f"{COMFYUI_URL}/system_stats", timeout=timeout)
        if r.status_code == 200:
            info["running"] = True
            system = (r.json() or {}).get("system") or {}
            for key in ("comfyui_version", "os", "device"):
                if key in system:
                    info[key] = system[key]
    except Exception as e:  # pragma: no cover - diagnostic only
        info["error"] = str(e)
    return info


def get_object_info(force=False):
    """Fetch (and cache) ComfyUI's /object_info — node type -> input schema."""
    global _object_info_cache
    if _object_info_cache is None or force:
        r = requests.get(f"{COMFYUI_URL}/object_info", timeout=30)
        r.raise_for_status()
        _object_info_cache = r.json()
    return _object_info_cache


# --------------------------------------------------------------------------- #
# Locating the ComfyUI install + workflows
# --------------------------------------------------------------------------- #

def find_comfyui_install():
    """Return the path to the ComfyUI root (the folder containing ``blueprints/``)."""
    if COMFYUI_INSTALL and os.path.isdir(COMFYUI_INSTALL):
        return COMFYUI_INSTALL

    candidates = []

    # 1) Comfy Desktop writes each install's path into installations.json.
    for base in (_APP_DATA, _LOCAL_APP_DATA):
        if not base:
            continue
        try:
            with open(os.path.join(base, "Comfy Desktop", "installations.json"),
                      encoding="utf-8") as f:
                installs = json.load(f)
            for inst in installs:
                p = inst.get("installPath")
                if p:
                    candidates.append(os.path.join(p, "ComfyUI"))
        except Exception:
            pass

    # 2) Standard Comfy Desktop install layout.
    if _LOCAL_APP_DATA:
        for root in (os.path.join(_LOCAL_APP_DATA, "Comfy-Desktop", "ComfyUI-Installs"),
                     os.path.join(_LOCAL_APP_DATA, "ComfyUI")):
            candidates.append(root)
            if os.path.isdir(root):
                for entry in sorted(os.listdir(root)):
                    candidates.append(os.path.join(root, entry))
                    candidates.append(os.path.join(root, entry, "ComfyUI"))

    # 3) Common standalone install locations.
    for base in (_LOCAL_APP_DATA, _APP_DATA, os.path.expanduser("~")):
        if base:
            candidates.append(os.path.join(base, "ComfyUI"))
    for drive in ("C:\\", "D:\\"):
        candidates.append(os.path.join(drive, "ComfyUI"))

    seen = set()
    for cand in candidates:
        cand = os.path.normpath(cand)
        if cand in seen:
            continue
        seen.add(cand)
        if os.path.isdir(cand) and os.path.isdir(os.path.join(cand, "blueprints")):
            return cand
    return None


def _workflow_entry(path, kind):
    name = os.path.splitext(os.path.basename(path))[0]
    lowered = name.lower()
    return {
        "id": name,
        "name": name,
        "kind": kind,           # "blueprint" or "user"
        "path": path,
        "is_z_image": "z-image" in lowered or "z_image" in lowered,
        "is_fhdr": "fhdr" in lowered,
    }


def discover_workflows(install_root=None):
    """Return every workflow/template file found under the ComfyUI install."""
    root = install_root or find_comfyui_install()
    if not root:
        return []

    found = []

    blueprints = os.path.join(root, "blueprints")
    if os.path.isdir(blueprints):
        for name in sorted(os.listdir(blueprints)):
            if name.lower().endswith(".json"):
                found.append(_workflow_entry(os.path.join(blueprints, name), "blueprint"))

    user_root = os.path.join(root, "user")
    if os.path.isdir(user_root):
        for user in sorted(os.listdir(user_root)):
            wf_dir = os.path.join(user_root, user, "workflows")
            if os.path.isdir(wf_dir):
                for name in sorted(os.listdir(wf_dir)):
                    if name.lower().endswith(".json"):
                        found.append(_workflow_entry(os.path.join(wf_dir, name), "user"))

    return found


def is_video_workflow(entry):
    """True for prompt-driven video workflows (text-to-video, image-to-video)."""
    return "to video" in entry["name"].lower() or "to-video" in entry["name"].lower()


def discover_video_workflows(install_root=None):
    """Return the prompt-driven video workflows (Text/Image-to-Video blueprints)."""
    return [wf for wf in discover_workflows(install_root) if is_video_workflow(wf)]


def list_models():
    """Live list of loadable model filenames exposed by ComfyUI loaders."""
    oi = get_object_info()
    result = {}
    loaders = ("UNETLoader", "UnetLoaderGGUF", "DiffusionModelLoader",
               "CLIPLoader", "DualCLIPLoaderGGUF", "VAELoader")
    for loader in loaders:
        spec = (oi.get(loader) or {}).get("input", {}).get("required", {})
        for name, val in spec.items():
            if isinstance(val, list) and val and isinstance(val[0], list):
                result.setdefault(loader, {})[name] = val[0]
    return result


# --------------------------------------------------------------------------- #
# Workflow (graph) -> prompt (API) conversion
# --------------------------------------------------------------------------- #

def _parse_links(links):
    """Normalise ComfyUI links into {link_id: {origin_id, origin_slot, ...}}.

    Legacy workflows store links as ``[id, origin, origin_slot, target,
    target_slot, type]``; the newer blueprint format stores them as objects.
    """
    by_id = {}
    for link in links or []:
        if isinstance(link, dict):
            by_id[link.get("id")] = {
                "origin_id": link.get("origin_id"),
                "origin_slot": link.get("origin_slot"),
                "target_id": link.get("target_id"),
                "target_slot": link.get("target_slot"),
            }
        else:
            by_id[link[0]] = {
                "origin_id": link[1],
                "origin_slot": link[2],
                "target_id": link[3],
                "target_slot": link[4],
            }
    return by_id


def _widget_names_for_type(object_info, class_type):
    """Ordered widget names for a node type, matching ComfyUI's UI order.

    ``control_after_generate`` widgets (auto-attached to seed inputs) are
    represented by a None placeholder so positional ``widgets_values`` stay
    aligned.
    """
    spec = (object_info.get(class_type) or {}).get("input", {})
    names = []
    for section in ("required", "optional"):
        for name, val in (spec.get(section) or {}).items():
            if not isinstance(val, list) or not val:
                continue
            t = val[0]
            if isinstance(t, list) or t in _WIDGET_TYPES:
                names.append(name)
                opts = val[1] if len(val) > 1 and isinstance(val[1], dict) else {}
                if isinstance(opts, dict) and opts.get("control_after_generate"):
                    names.append(None)
    return names


def _widget_value_map(object_info, class_type, widgets_values):
    mapping = {}
    names = _widget_names_for_type(object_info, class_type)
    for name, value in zip(names, widgets_values or []):
        if name:
            mapping[name] = value
    return mapping


def _default_for_type(comfy_type):
    return {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(comfy_type, "")


def _build_node(node, object_info, links_by_id, exposed_defaults=None):
    """Build one ``prompt``-format node from a workflow node."""
    class_type = node.get("type")
    result = {"class_type": class_type, "inputs": {}}
    named = node.get("widgets_values_named") or {}
    widget_map = _widget_value_map(object_info, class_type, node.get("widgets_values") or [])
    exposed_defaults = exposed_defaults or {}

    for inp in node.get("inputs", []):
        name = inp.get("name")
        if not name:
            continue
        link_id = inp.get("link")
        if link_id is not None:
            link = links_by_id.get(link_id)
            if not link:
                continue
            origin, slot = link["origin_id"], link["origin_slot"]
            if origin == -10:
                # Exposed input of a blueprint subgraph; fill from the subgraph
                # input default (or the node's own widget default).
                default = exposed_defaults.get((str(node.get("id")), name))
                if default is None:
                    default = named.get(name)
                if default is None:
                    default = widget_map.get(name, _default_for_type(inp.get("type", "STRING")))
                result["inputs"][name] = default
            else:
                result["inputs"][name] = [str(origin), slot]
            continue
        # Widget input (unconnected).
        if name in named:
            result["inputs"][name] = named[name]
        elif name in widget_map:
            result["inputs"][name] = widget_map[name]
    return result


def _legacy_to_prompt(graph, object_info):
    links = _parse_links(graph.get("links"))
    prompt = {}
    for node in graph.get("nodes", []):
        # Frontend-only nodes (Note / MarkdownNote / Reroute / ...) are not in
        # /object_info and have no server-side counterpart; skip them.
        if node.get("type") not in object_info:
            continue
        prompt[str(node["id"])] = _build_node(node, object_info, links)
    return prompt


def _video_save_node(object_info):
    """Return (class_type, video_input_name) for a node that can persist video.

    Prefers a native video save; falls back to an animated WEBP (a core node).
    """
    for ct, vin in (("SaveVideo", "video"), ("SaveAnimatedWEBP", "images")):
        if ct in object_info:
            return ct, vin
    return None, None


def _make_save_node(class_type, object_info, video_input_name, origin_id, origin_slot):
    """Build a save node for a video, wiring the video input and filling the rest
    of the required inputs from their spec defaults."""
    spec = (object_info.get(class_type) or {}).get("input", {}).get("required") or {}
    inputs = {}
    for name, val in spec.items():
        if name == video_input_name:
            inputs[name] = [str(origin_id), origin_slot]
            continue
        default = None
        if isinstance(val, list) and val:
            if isinstance(val[0], list):
                default = val[0][0]          # combo -> first option
            else:
                t = val[0]
                opts = val[1] if len(val) > 1 and isinstance(val[1], dict) else {}
                default = opts.get("default")
                if default is None:
                    default = {"INT": 0, "FLOAT": 0.0, "BOOLEAN": False, "STRING": ""}.get(t)
        inputs[name] = default
    inputs["filename_prefix"] = "TrioForge"
    return {"class_type": class_type, "inputs": inputs}


def _blueprint_to_prompt(graph, object_info):
    subgraphs = (graph.get("definitions") or {}).get("subgraphs") or []
    if not subgraphs:
        return None
    sub = subgraphs[0]

    exposed = sub.get("inputs") or []
    # Build defaults for exposed inputs from their target node's widget values,
    # so e.g. unet_name/clip_name/vae_name resolve to the template's own models.
    exposed_defaults = {}
    nodes_by_id = {n.get("id"): n for n in sub.get("nodes", [])}
    links = _parse_links(sub.get("links"))
    for link in links.values():
        if link.get("origin_id") == -10:
            slot = link["origin_slot"]
            if slot < len(exposed):
                target = nodes_by_id.get(link["target_id"])
                if target:
                    named = target.get("widgets_values_named") or {}
                    wmap = _widget_value_map(
                        object_info, target.get("type"), target.get("widgets_values") or []
                    )
                    inp_name = None
                    for inp in target.get("inputs", []):
                        if inp.get("link") == link.get("id"):
                            inp_name = inp.get("name")
                            break
                    if inp_name:
                        exposed_defaults[(str(target.get("id")), inp_name)] = \
                            named.get(inp_name, wmap.get(inp_name))

    prompt = {}
    for node in sub.get("nodes", []):
        if node.get("type") not in object_info:
            continue  # frontend-only node (Note / MarkdownNote / Reroute / ...)
        prompt[str(node["id"])] = _build_node(node, object_info, links, exposed_defaults)

    # The blueprint routes its final output to the boundary (-20); attach a save
    # node (image or video, depending on the output type) so there's a file to download.
    outputs = [l for l in links.values() if l.get("target_id") == -20]
    if outputs:
        origin_id, origin_slot = outputs[0]["origin_id"], outputs[0]["origin_slot"]
        out_type = (outputs[0].get("type") or "IMAGE").upper()
        sid = str(max([int(i) for i in prompt.keys() if i.lstrip("-").isdigit()] or [0]) + 1)
        if out_type == "VIDEO":
            ct, vin = _video_save_node(object_info)
            if not ct:
                raise ProviderError(
                    "ComfyUI has no video-save node (SaveVideo / SaveAnimatedWEBP). "
                    "Install VideoHelperSuite or a video save node, then retry.")
            prompt[sid] = _make_save_node(ct, object_info, vin, origin_id, origin_slot)
        else:
            prompt[sid] = {
                "class_type": "SaveImage",
                "inputs": {"images": [str(origin_id), origin_slot], "filename_prefix": "TrioForge"},
            }
    return prompt


def workflow_to_prompt(graph, object_info=None):
    """Convert a workflow file (legacy or blueprint) into a prompt graph."""
    object_info = object_info or get_object_info()
    if (graph.get("definitions") or {}).get("subgraphs"):
        prompt = _blueprint_to_prompt(graph, object_info)
        if prompt:
            return prompt
    return _legacy_to_prompt(graph, object_info)


# --------------------------------------------------------------------------- #
# Prompt injection + running
# --------------------------------------------------------------------------- #

def _inject(prompt, prompt_text=None, width=None, height=None, steps=None,
            seed=None, negative="", model_name=None, length=None):
    """Fill the user-controllable fields into a converted prompt graph."""
    if model_name:
        for n, d in prompt.items():
            if d.get("class_type") == "UnetLoaderGGUF" and "unet_name" in d["inputs"]:
                d["inputs"]["unet_name"] = model_name

    text_nodes = [n for n, d in prompt.items()
                  if d.get("class_type") in ("CLIPTextEncode", "CLIPTextEncodeSDXL")]
    samplers = [n for n, d in prompt.items() if d.get("class_type", "").startswith("KSampler")]

    # Which CLIPTextEncode feeds the sampler's "positive" input?
    positive_node = None
    for n in samplers:
        pos = prompt[n].get("inputs", {}).get("positive")
        if isinstance(pos, list) and pos and str(pos[0]) in text_nodes:
            positive_node = str(pos[0])
            break
    if positive_node is None and text_nodes:
        positive_node = text_nodes[0]

    if prompt_text is not None and positive_node:
        prompt[positive_node]["inputs"]["text"] = prompt_text
    for n in text_nodes:
        if n != positive_node and "text" in prompt[n]["inputs"]:
            prompt[n]["inputs"]["text"] = negative

    for n, d in prompt.items():
        ct = d.get("class_type")
        if ct in ("EmptySD3LatentImage", "EmptyLatentImage", "EmptySDXLImage",
                  "EmptyFluxLatentImage", "EmptyHunyuanLatentVideo", "EmptyLTXVLatentVideo"):
            if width:
                d["inputs"]["width"] = int(width)
            if height:
                d["inputs"]["height"] = int(height)
            if length and "length" in d["inputs"]:
                d["inputs"]["length"] = int(length)

    for n in samplers:
        if "seed" in prompt[n]["inputs"]:
            prompt[n]["inputs"]["seed"] = int(seed) if seed is not None \
                else random.randint(0, 2**32 - 1)
        if "noise_seed" in prompt[n]["inputs"] and seed is not None:
            prompt[n]["inputs"]["noise_seed"] = int(seed)
        if steps:
            prompt[n]["inputs"]["steps"] = int(steps)


def _error_from_history(history):
    status = history.get("status") or {}
    if status.get("status_str") == "error":
        messages = status.get("messages") or []
        detail = "; ".join(m.get("details") or str(m) for m in messages if isinstance(m, dict))
        return detail or "ComfyUI reported an execution error"
    return None


def run_prompt(prompt, output_path, timeout=900):
    """POST the prompt to ComfyUI, poll for completion, download the media file.

    Handles image (``images``) and video (``videos`` / ``gifs``) outputs.
    """
    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{COMFYUI_URL}/prompt",
        json={"prompt": prompt, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json()["prompt_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            history = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10).json()
        except Exception:
            time.sleep(1)
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            err = _error_from_history(entry)
            if err:
                raise RuntimeError(err)
            outputs = entry.get("outputs") or {}
            for node_out in outputs.values():
                for key in ("images", "videos", "gifs"):
                    media = node_out.get(key) or []
                    if not media:
                        continue
                    item = media[0]
                    filename = item["filename"]
                    subfolder = item.get("subfolder", "")
                    ftype = item.get("type", "output")
                    url = (f"{COMFYUI_URL}/view?filename={quote(filename)}"
                           f"&subfolder={quote(subfolder)}&type={quote(ftype)}")
                    media_resp = requests.get(url, timeout=120)
                    media_resp.raise_for_status()
                    out_dir = os.path.dirname(output_path)
                    if out_dir:
                        os.makedirs(out_dir, exist_ok=True)
                    with open(output_path, "wb") as f:
                        f.write(media_resp.content)
                    return filename
        time.sleep(1)

    raise TimeoutError("ComfyUI generation timed out")


# --------------------------------------------------------------------------- #
# High-level entry point
# --------------------------------------------------------------------------- #

def select_workflow(workflows, requested=None):
    """Pick a workflow from the discovered list.

    ``requested`` may be a workflow id/name (matched case-insensitively), the
    literal ``"z-image"`` / ``"fhdr"`` keyword, or None (prefer Z-Image-Turbo,
    then FHDR, then the first entry).
    """
    if not workflows:
        return None
    if requested:
        needle = requested.lower()
        for wf in workflows:
            if needle in wf["name"].lower() or needle in wf["id"].lower():
                return wf
        # keyword shortcuts
        if "fhdr" in needle:
            for wf in workflows:
                if wf["is_fhdr"]:
                    return wf
        if "z-image" in needle or "z_image" in needle or "turbo" in needle:
            for wf in workflows:
                if wf["is_z_image"]:
                    return wf
        return None
    for wf in workflows:
        if wf["is_z_image"]:
            return wf
    for wf in workflows:
        if wf["is_fhdr"]:
            return wf
    return workflows[0]


def generate_image(prompt, output_path, workflow=None, width=None, height=None,
                   steps=None, seed=None, model=None, timeout=900):
    """Detect ComfyUI, discover + convert a workflow, run it, save the image.

    Returns the workflow entry that was used.
    """
    if not is_comfyui_running():
        raise RuntimeError("ComfyUI is not running. Start Comfy Desktop first.")

    workflows = discover_workflows()
    if not workflows:
        raise RuntimeError("No ComfyUI workflows found. "
                           "Install ComfyUI and add/keep your workflows.")

    wf = select_workflow(workflows, workflow)
    if wf is None:
        raise RuntimeError(f"ComfyUI workflow not found: {workflow!r}")

    with open(wf["path"], encoding="utf-8") as f:
        graph = json.load(f)

    model_name = os.path.basename(model) if model else None
    prompt_graph = workflow_to_prompt(graph)
    _inject(prompt_graph, prompt_text=prompt, width=width, height=height,
            steps=steps, seed=seed, model_name=model_name)
    run_prompt(prompt_graph, output_path, timeout=timeout)
    return wf


def select_video_workflow(workflows, requested=None):
    """Pick a video workflow from the discovered list."""
    if not workflows:
        return None
    if requested:
        needle = requested.lower()
        for wf in workflows:
            if needle in wf["name"].lower() or needle in wf["id"].lower():
                return wf
        return None
    return workflows[0]


def generate_video(prompt, output_path, workflow=None, width=None, height=None,
                   length=None, steps=None, seed=None, timeout=1800):
    """Detect ComfyUI, discover + convert a video workflow, run it, save the video.

    Returns the workflow entry that was used.
    """
    if not is_comfyui_running():
        raise RuntimeError("ComfyUI is not running. Start Comfy Desktop first.")

    workflows = discover_video_workflows()
    if not workflows:
        raise RuntimeError("No ComfyUI video workflows found. "
                           "Install ComfyUI with a video model (e.g. Wan 2.2 / LTX).")

    wf = select_video_workflow(workflows, workflow)
    if wf is None:
        raise RuntimeError(f"ComfyUI video workflow not found: {workflow!r}")

    with open(wf["path"], encoding="utf-8") as f:
        graph = json.load(f)

    prompt_graph = workflow_to_prompt(graph)
    _inject(prompt_graph, prompt_text=prompt, width=width, height=height,
            length=length, steps=steps, seed=seed)
    media_name = run_prompt(prompt_graph, output_path, timeout=timeout)
    return wf, media_name


# --------------------------------------------------------------------------- #
# CLI self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ComfyUI detection + workflow discovery self-test")
    ap.add_argument("--prompt", default=None, help="If given, actually generate an image")
    ap.add_argument("--workflow", default=None, help="Workflow id/name to use")
    ap.add_argument("--output", default=None, help="Output PNG path (with --prompt)")
    args = ap.parse_args()

    print("== detect_comfyui ==")
    print(json.dumps(detect_comfyui(), indent=2))

    if not is_comfyui_running():
        print("ComfyUI is NOT running; discovery below may be empty.")
        raise SystemExit(0)

    install = find_comfyui_install()
    print(f"\n== ComfyUI install root ==\n{install}")

    print("\n== discovered workflows ==")
    for wf in discover_workflows(install):
        flags = []
        if wf["is_z_image"]:
            flags.append("Z-IMAGE")
        if wf["is_fhdr"]:
            flags.append("FHDR")
        print(f"  [{wf['kind']}] {wf['name']}  {' '.join(flags)}")
        print(f"        {wf['path']}")

    if args.prompt:
        out = args.output or "trioforge_comfy_test.png"
        used = generate_image(args.prompt, out, workflow=args.workflow)
        print(f"\n== generated via '{used['name']}' -> {out}")
