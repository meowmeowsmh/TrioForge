"""
Example TrioForge plugin — a tiny health-check endpoint.

Copy this file into the `plugins/` folder (it's already there as an example)
and restart the app. It adds:

    GET /api/plugin/example/hello  →  {"hello": "from plugin", "app": "TrioForge"}
"""

MANIFEST = {
    "name": "example-hello",
    "title": "Example Hello Plugin",
    "version": "1.0.0",
    "description": "Adds a /api/plugin/example/hello route.",
}


def register(app):
    from flask import jsonify

    @app.route("/api/plugin/example/hello")
    def _example_hello():
        return jsonify({"hello": "from plugin", "app": "TrioForge"})
