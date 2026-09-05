# TrioForge Plugins

Drop a single `.py` file here to extend TrioForge. Plugins are loaded at
startup and can add routes, register blueprints, or hook into the app.

## Anatomy of a plugin

```python
# my_plugin.py

MANIFEST = {
    "name": "my-plugin",           # required — unique id
    "title": "My Plugin",          # optional — display name
    "version": "1.0.0",
    "description": "What it does",
}

def register(app):
    # `app` is the Flask application. Add routes, blueprints, etc.
    from flask import jsonify

    @app.route("/api/plugin/my-plugin/ping")
    def _ping():
        return jsonify({"ok": True})
```

- `MANIFEST` is optional but recommended (gives the plugin a nice name).
- `register(app)` is optional — only define it if you need to add routes.
- Files starting with `_` are skipped.

## Loading

Plugins load automatically when the app starts. The UI lists them via
`GET /api/plugins`. A plugin that fails to import or whose `register()`
throws is **reported and skipped** — it never crashes the app.

## Security

Plugins are trusted Python code that runs inside the app process with the
same permissions as TrioForge itself. **Only install plugins you trust** —
the same rule as browser extensions or VSCode extensions.
