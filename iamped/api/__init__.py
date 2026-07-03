"""Route blueprints for the Flask backend, grouped by concern.

Each module defines a Blueprint named ``bp`` and registers itself on
``iamped.server.app`` at the bottom of its body. That self-registration (rather
than the more common register-in-create_app) is deliberate: it keeps imports
acyclic no matter whether ``iamped.server`` or an ``iamped.api.*`` module is
imported first (tests do both).
"""
